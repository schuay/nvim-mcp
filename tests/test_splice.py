# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""The splice keeps the client's MCP session across broker restarts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import start_broker, stop_broker

from showme import paths, splice


class Client:
    """Drive a splice process the way an MCP client would, over its pipes."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 0

    @classmethod
    async def start(cls, socket_path: Path, window: float = 5.0) -> Client:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(splice.__file__)),
            str(socket_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env={**os.environ, "SHOWME_RECONNECT_WINDOW": str(window)},
        )
        return cls(process)

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict | None = None) -> int:
        self.next_id += 1
        await self.send(
            {
                "jsonrpc": "2.0",
                "id": self.next_id,
                "method": method,
                "params": params or {},
            }
        )
        return self.next_id

    async def receive(self, timeout: float = 10.0) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        assert line, "the splice closed its stdout"
        return json.loads(line)

    async def initialize(self) -> dict[str, Any]:
        request_id = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        reply = await self.receive()
        assert reply["id"] == request_id
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    async def close(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        return await asyncio.wait_for(self.process.wait(), 10)


class FakeBroker:
    """Answers initialize, and does with later requests what the test says."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.server: asyncio.AbstractServer | None = None
        self.writers: set[asyncio.StreamWriter] = set()
        self.seen: list[dict[str, Any]] = []
        self.answer = True

    async def start(self) -> None:
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writers.add(writer)
        while line := await reader.readline():
            message = json.loads(line)
            self.seen.append(message)
            if message.get("method") == "initialize":
                reply = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"fake": True},
                }
                writer.write(json.dumps(reply).encode() + b"\n")
            elif "id" in message:
                if not self.answer:
                    break
                reply = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"echo": message["method"]},
                }
                writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        writer.close()
        self.writers.discard(writer)

    async def stop(self) -> None:
        """Go away the way a killed broker does: connections included.

        wait_closed() waits for open connections, and an idle one only ends
        when the splice ends, which is after the test.
        """
        assert self.server is not None
        self.server.close()
        for writer in list(self.writers):
            writer.close()
        await self.server.wait_closed()
        self.path.unlink(missing_ok=True)


@pytest.fixture
async def fake() -> AsyncIterator[FakeBroker]:
    directory = Path(tempfile.mkdtemp(prefix="nvmcp-", dir="/tmp"))
    broker = FakeBroker(directory / "agent.sock")
    await broker.start()
    yield broker
    if broker.server is not None and broker.server.is_serving():
        await broker.stop()


async def test_a_lost_broker_is_replayed_the_handshake_and_the_client_sees_one_session(
    fake: FakeBroker,
) -> None:
    client = await Client.start(fake.path)
    first = await client.initialize()
    assert first["result"] == {"fake": True}
    await client.request("tools/list")
    assert (await client.receive())["result"] == {"echo": "tools/list"}

    # The broker drops the connection while a request is in flight.
    fake.answer = False
    in_flight = await client.request("tools/call")
    failed = await client.receive()
    assert failed["id"] == in_flight
    assert failed["error"]["code"] == splice.CONNECTION_LOST
    assert "retry" in failed["error"]["message"]
    await fake.stop()

    # Back, under the same path. The next request goes through, after a
    # handshake the client did not send again and whose answer it never sees.
    fake.answer = True
    fake.seen.clear()
    await fake.start()
    retried = await client.request("tools/call")
    reply = await client.receive()
    assert (reply["id"], reply["result"]) == (retried, {"echo": "tools/call"})
    assert [m.get("method") for m in fake.seen] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert fake.seen[0]["params"]["clientInfo"] == {"name": "test", "version": "0"}
    assert await client.close() == 0


async def test_a_broker_that_stays_away_answers_with_an_error_and_the_splice_lives(
    fake: FakeBroker,
) -> None:
    client = await Client.start(fake.path, window=0.5)
    await client.initialize()
    await fake.stop()

    started = asyncio.get_running_loop().time()
    request_id = await client.request("tools/list")
    reply = await client.receive()
    assert reply["id"] == request_id
    assert "run `showme`" in reply["error"]["message"]
    assert asyncio.get_running_loop().time() - started >= 0.5
    assert client.process.returncode is None, "the splice gave up on the client"
    assert await client.close() == 0


async def test_the_real_broker_can_be_restarted_under_a_client(runtime: Path) -> None:
    stop, task = await start_broker()
    client = await Client.start(paths.agent_socket())
    await client.initialize()
    await client.request("tools/list")
    tools = (await client.receive())["result"]["tools"]
    assert [t["name"] for t in tools] == ["show", "read"]
    await stop_broker(stop, task)

    stop, task = await start_broker()
    try:
        await client.request("tools/list")
        again = (await client.receive())["result"]["tools"]
        assert again == tools
    finally:
        assert await client.close() == 0
        await stop_broker(stop, task)


@contextlib.contextmanager
def connected(client: splice.Splice) -> Iterator[None]:
    try:
        yield
    finally:
        client.selector.close()
        if client.sock is not None:
            client.sock.close()


def listen(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    return listener


@pytest.mark.parametrize("first", [True, False])
def test_a_broker_that_is_already_serving_is_used_and_no_second_one_is_started(
    tmp_path: Path, first: bool
) -> None:
    """What a sandboxed client rests on: the socket it was given is served, so
    the broker it would otherwise start never is."""
    path = tmp_path / "agent.sock"
    started: list[None] = []
    client = splice.Splice(str(path), revive=lambda: started.append(None))

    with listen(path), connected(client):
        assert client.connect(first=first)

    assert started == []


def test_a_broker_that_is_not_there_is_asked_for(tmp_path: Path) -> None:
    path = tmp_path / "agent.sock"
    listeners: list[socket.socket] = []
    client = splice.Splice(str(path), revive=lambda: listeners.append(listen(path)))

    with connected(client):
        assert client.connect(first=True)

    assert len(listeners) == 1
    listeners[0].close()


def test_a_client_that_cannot_start_a_broker_gives_up_on_a_socket_nobody_serves(
    tmp_path: Path,
) -> None:
    client = splice.Splice(str(tmp_path / "agent.sock"))

    with connected(client):
        assert not client.connect(first=True)


def test_a_host_client_takes_the_session_rooted_where_it_started(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from showme import cli

    asked: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_ensure_broker", lambda: None)
    monkeypatch.setattr(
        cli,
        "_ask",
        lambda request, timeout=0: asked.update(request) or {"ok": True, "key": "k-1"},
    )
    monkeypatch.setattr(
        cli.splice,
        "splice",
        lambda path, revive=None, key=None: (
            captured.update(revive=revive, key=key) or 0
        ),
    )
    cli.cmd_mcp(argparse.Namespace())

    # Nothing for the human to create and no key to paste: the client asks for
    # the session rooted where it was started, over the admin socket that only
    # a host client can reach.
    assert (asked["cmd"], asked["root"]) == ("ensure", str(Path.cwd()))
    # And without an editor, which starts with the first thing an agent shows.
    assert asked["spawn"] is False
    assert captured["key"] == "k-1"
    assert captured["revive"] is cli._revive_broker


def test_a_sandboxed_client_asks_for_no_broker_and_no_session(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box cannot serve the agent socket it was handed, and cannot reach the
    admin socket at all: starting a broker there wasted fifteen seconds and
    then failed."""
    import argparse

    from showme import cli

    # Read-only is what a box has, and what this asks about.
    paths.agent_dir().chmod(0o500)
    started: list[None] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_ensure_broker", lambda: started.append(None))
    monkeypatch.setattr(
        cli,
        "_ask",
        lambda request, timeout=0: started.append(None) or {"ok": False},
    )
    monkeypatch.setattr(
        cli.splice,
        "splice",
        lambda path, revive=None, key=None: (
            captured.update(revive=revive, key=key) or 0
        ),
    )
    try:
        cli.cmd_mcp(argparse.Namespace())
    finally:
        paths.agent_dir().chmod(0o700)

    assert started == []
    assert captured["revive"] is None
    assert captured["key"] is None
