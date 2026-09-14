# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Speak msgpack-RPC to a running nvim over a UNIX socket.

nvim answers requests in reverse order of arrival, so responses are matched by
message id rather than by position. Notifications and requests from nvim arrive
unsolicited on the same connection and go to callbacks; a request is answered
with whatever the callback returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

import msgpack

log = logging.getLogger(__name__)

REQUEST = 0
RESPONSE = 1
NOTIFICATION = 2

#: A wedged nvim must not wedge the client waiting on it. Generous, because a
#: cold buffer load on a large file is legitimately slow.
TIMEOUT = 30.0


class NvimError(RuntimeError):
    """An error returned by nvim for a request."""


class NvimGone(NvimError):
    """The connection to nvim is closed or broke while sending.

    Distinct from an error nvim itself reported, because a caller can recover
    from this one by starting nvim again; retrying the other would just repeat
    a failure.
    """


class NvimRPC:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._closed = False
        self._task = asyncio.create_task(self._read_loop())
        self.on_notification: Callable[[str, list[Any]], None] | None = None
        #: Answers a request nvim makes of us. Runs on the read loop, so it
        #: must not wait on anything that needs the loop.
        self.on_request: Callable[[str, list[Any]], Any] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    async def connect(cls, socket: Path) -> NvimRPC:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        return cls(reader, writer)

    async def request(self, method: str, *params: Any, timeout: float = TIMEOUT) -> Any:
        # Once the read loop is gone nothing will ever complete a new future, so
        # a request registered after that would wait forever.
        if self._closed:
            raise NvimGone("nvim connection is closed")
        msgid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msgid] = future
        try:
            self._writer.write(msgpack.packb([REQUEST, msgid, method, list(params)]))
            await self._writer.drain()
        except OSError as e:
            # A failed write means this connection is finished. Say so now, or
            # a caller that reconnects on NvimGone will find it still looking
            # alive and retry down the same dead socket.
            self._closed = True
            self._pending.pop(msgid, None)
            raise NvimGone(f"nvim connection broke: {e}") from e
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise NvimError(f"nvim did not answer {method} within {timeout}s") from None
        finally:
            # Also on cancellation: a caller under an outer deadline is dropped
            # here without ever timing out, and its entry would sit in the map
            # until the connection ends.
            self._pending.pop(msgid, None)

    async def notify(self, method: str, *params: Any) -> None:
        """Send a call that expects no answer.

        Needed for anything that makes nvim exit: a request would wait forever
        for a response the dying process never sends.
        """
        self._writer.write(msgpack.packb([NOTIFICATION, method, list(params)]))
        await self._writer.drain()

    async def lua(self, code: str, *args: Any) -> Any:
        """Run broker-owned Lua with client values passed as arguments.

        Client values must never be formatted into `code`. nvim expands
        backticks in a path given to an Ex command, including through
        `nvim_cmd`'s structured arguments, so a filename the agent controls
        would run a shell.
        """
        return await self.request("nvim_exec_lua", code, [lua_value(a) for a in args])

    async def close(self) -> None:
        self._closed = True
        self._task.cancel()
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        # A buffer can hold bytes that are not UTF-8, from a latin-1 file or
        # a half-finished edit, and they reach here in a mark or a range. They
        # are the human's content to lose, not the connection's.
        unpacker = msgpack.Unpacker(raw=False, unicode_errors="replace")
        try:
            while chunk := await self._reader.read(65536):
                unpacker.feed(chunk)
                for message in unpacker:
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nvim connection failed")
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(NvimGone("nvim connection closed"))
            self._pending.clear()

    def _dispatch(self, message: list[Any]) -> None:
        kind = message[0]
        if kind == RESPONSE:
            _, msgid, error, result = message
            future = self._pending.pop(msgid, None)
            if future is None or future.done():
                return
            if error is not None:
                future.set_exception(NvimError(str(error)))
            else:
                future.set_result(result)
        elif kind == NOTIFICATION and self.on_notification:
            self.on_notification(message[1], message[2])
        elif kind == REQUEST:
            _, msgid, method, params = message
            error, result = None, None
            if self.on_request is None:
                error = f"unhandled request {method}"
            else:
                try:
                    result = self.on_request(method, params)
                except Exception as e:
                    error = str(e)
            self._writer.write(msgpack.packb([RESPONSE, msgid, error, result]))


def lua_value(value: Any) -> Any:
    """Prepare a Python value for a Lua argument.

    An absent optional field is left out rather than sent as null: nvim decodes
    msgpack NIL as vim.NIL, which is truthy in Lua, so a default written as
    `opts.x or 1` would never apply.
    """
    if isinstance(value, dict):
        return {k: lua_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [lua_value(v) for v in value]
    return value
