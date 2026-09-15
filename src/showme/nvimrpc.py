# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Speak msgpack-RPC to nvim over a UNIX socket.

Match responses by message ID because nvim may answer requests out of order.
Dispatch unsolicited notifications and requests from the same connection to
callbacks.
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

#: Bound a wedged nvim while allowing slow cold loads of large files.
TIMEOUT = 30.0


class NvimError(RuntimeError):
    """An error returned by nvim for a request."""


class NvimGone(NvimError):
    """The connection to nvim is closed or broke while sending.

    Callers may recover from a lost connection by restarting nvim. An error
    reported by nvim would recur after a restart.
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
        #: Runs on the read loop and must not wait for work that needs that loop.
        self.on_request: Callable[[str, list[Any]], Any] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    async def connect(cls, socket: Path) -> NvimRPC:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        return cls(reader, writer)

    async def request(self, method: str, *params: Any, timeout: float = TIMEOUT) -> Any:
        # The closed read loop cannot complete a newly registered future.
        if self._closed:
            raise NvimGone("nvim connection is closed")
        msgid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msgid] = future
        try:
            self._writer.write(msgpack.packb([REQUEST, msgid, method, list(params)]))
            await self._writer.drain()
        except OSError as e:
            # Mark the connection closed before callers handle NvimGone, or a
            # retry will reuse the same dead socket.
            self._closed = True
            self._pending.pop(msgid, None)
            raise NvimGone(f"nvim connection broke: {e}") from e
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise NvimError(f"nvim did not answer {method} within {timeout}s") from None
        finally:
            # An outer cancellation bypasses this request's timeout, so always
            # remove its pending entry.
            self._pending.pop(msgid, None)

    async def notify(self, method: str, *params: Any) -> None:
        """Send a call that expects no answer.

        Use this for calls that make nvim exit, because it cannot answer them.
        """
        self._writer.write(msgpack.packb([NOTIFICATION, method, list(params)]))
        await self._writer.drain()

    async def lua(self, code: str, *args: Any) -> Any:
        """Run broker-owned Lua with client values passed as arguments.

        Never format client values into `code`. nvim expands backticks in Ex
        command paths, including `nvim_cmd` arguments, which could run shell
        commands from an agent-controlled filename.
        """
        return await self.request("nvim_exec_lua", code, [lua_value(a) for a in args])

    async def close(self) -> None:
        self._closed = True
        self._task.cancel()
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        # Preserve the connection when marks or ranges contain non-UTF-8 bytes
        # from a file or unfinished edit.
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

    Omit absent optional fields. nvim decodes msgpack NIL as truthy `vim.NIL`,
    which prevents Lua defaults such as `opts.x or 1` from applying.
    """
    if isinstance(value, dict):
        return {k: lua_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [lua_value(v) for v in value]
    return value
