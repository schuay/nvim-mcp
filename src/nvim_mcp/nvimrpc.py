# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Speak msgpack-RPC to a running nvim over a UNIX socket.

nvim answers requests in reverse order of arrival, so responses are matched by
message id rather than by position. Notifications arrive unsolicited on the same
connection and go to a callback.
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


class NvimError(RuntimeError):
    """An error returned by nvim for a request."""


class NvimRPC:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._task = asyncio.create_task(self._read_loop())
        self.on_notification: Callable[[str, list[Any]], None] | None = None

    @classmethod
    async def connect(cls, socket: Path) -> NvimRPC:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        return cls(reader, writer)

    async def request(self, method: str, *params: Any) -> Any:
        msgid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msgid] = future
        self._writer.write(msgpack.packb([REQUEST, msgid, method, list(params)]))
        await self._writer.drain()
        return await future

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
        return await self.request("nvim_exec_lua", code, list(args))

    async def close(self) -> None:
        self._task.cancel()
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        unpacker = msgpack.Unpacker(raw=False)
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
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(NvimError("nvim connection closed"))
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
