# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""A minimal MCP client for tests: JSON-RPC lines over the agent socket.

Hand-written rather than driven through the SDK so the tests exercise the bytes
a real client sends across the socket.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from showme import broker, splice


class Wire:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.next_id = 0
        self.hello: dict[str, Any] | None = None

    @classmethod
    async def connect(cls, socket: Path, key: str | None = None) -> Wire:
        # A result can carry a mark of buffer text or fifty long notes, which
        # is past asyncio's default line limit. The splice a real client runs
        # reads in chunks and has no such limit.
        reader, writer = await asyncio.open_unix_connection(
            str(socket), limit=broker.LINE_LIMIT
        )
        wire = cls(reader, writer)
        if key is not None:
            wire.hello = await wire.present(key)
        await wire.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        await wire.notify("notifications/initialized")
        return wire

    async def present(self, key: str) -> dict[str, Any]:
        """Name a session before the MCP stream starts, as the splice does."""
        body = {splice.HELLO: {"key": key}}
        self.writer.write(json.dumps(body).encode() + b"\n")
        await self.writer.drain()
        line = await asyncio.wait_for(self.reader.readline(), 30)
        return json.loads(line)[splice.HELLO]

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
            "params": params or {},
        }
        self.writer.write(json.dumps(body).encode() + b"\n")
        await self.writer.drain()
        line = await asyncio.wait_for(self.reader.readline(), 30)
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.writer.write(json.dumps(body).encode() + b"\n")
        await self.writer.drain()

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": tool, "arguments": arguments})

    async def close(self) -> None:
        self.writer.close()
