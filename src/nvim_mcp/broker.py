# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Run the host-side daemon that owns every nvim session.

One broker per user, held by a lock file. It listens on two sockets: an admin
socket for `nv`, which creates and kills sessions, and an agent socket carrying
MCP. Only the agent socket is meant to be reachable from a sandbox, so session
administration stays off the surface a sandboxed client can see.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from . import mcpserver, paths, splice
from .clamp import Refused
from .session import Session

log = logging.getLogger(__name__)

#: Exit once nothing has needed the broker for this long.
IDLE_EXIT_SECONDS = 15 * 60


class Broker:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.clients = 0
        self._idle_since: float | None = None

    def session_by_key(self, key: str) -> Session | None:
        for session in self.sessions.values():
            # Compare the whole key: the short id alone is guessable.
            if key and key == session.key:
                return session
        return None

    async def new_session(self, root: str, clean: bool = False) -> Session:
        sid = self._next_id()
        session = Session.create(sid, root, clean=clean)
        await session.start()
        self.sessions[sid] = session
        return session

    def _next_id(self) -> str:
        used = {int(sid) for sid in self.sessions}
        candidate = 1
        while candidate in used:
            candidate += 1
        return str(candidate)

    async def kill(self, sid: str) -> bool:
        session = self.sessions.pop(sid, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()


async def _admin_client(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one `nv` command.

    A small JSON line protocol rather than MCP: these are host-only operations
    that must stay off the agent surface.
    """
    try:
        line = await reader.readline()
        if not line:
            return
        request = json.loads(line)
        reply = await _admin_command(broker, request)
    except Exception as e:
        log.exception("admin command failed")
        reply = {"ok": False, "error": str(e)}
    writer.write(json.dumps(reply).encode() + b"\n")
    with contextlib.suppress(OSError):
        await writer.drain()
    writer.close()


async def _admin_command(broker: Broker, request: dict[str, Any]) -> dict[str, Any]:
    command = request.get("cmd")
    if command == "ping":
        return {"ok": True}
    if command == "new":
        try:
            session = await broker.new_session(
                request["root"], bool(request.get("clean"))
            )
        except Refused as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "id": session.sid,
            "key": session.key,
            "root": str(session.root.path),
            "socket": str(session.socket),
        }
    if command == "ls":
        listing = []
        for session in broker.sessions.values():
            listing.append(
                {
                    "id": session.sid,
                    "key": session.key,
                    "root": str(session.root.path),
                    "socket": str(session.socket),
                    "attached": await session.attached(),
                }
            )
        return {"ok": True, "sessions": listing}
    if command == "kill":
        return {"ok": await broker.kill(str(request["id"]))}
    return {"ok": False, "error": f"unknown command: {command}"}


async def _agent_client(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    broker.clients += 1
    server = mcpserver.build(broker.session_by_key)
    try:
        await mcpserver.serve(reader, writer, server)
    except Exception:
        log.exception("agent connection failed")
    finally:
        broker.clients -= 1
        writer.close()


def _publish_splice() -> None:
    """Put the splice client beside the agent socket.

    A sandbox binds that directory and runs this copy; the broker's own package
    is not installed in there.
    """
    shutil.copyfile(Path(splice.__file__), paths.splice_script())


async def serve(stop: asyncio.Event | None = None) -> None:
    lock = paths.lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another broker holds %s", paths.lock_path())
        return

    broker = Broker()
    for socket_path in (paths.admin_socket(), paths.agent_socket()):
        socket_path.unlink(missing_ok=True)

    admin = await asyncio.start_unix_server(
        lambda r, w: _admin_client(broker, r, w), path=str(paths.admin_socket())
    )
    agent = await asyncio.start_unix_server(
        lambda r, w: _agent_client(broker, r, w), path=str(paths.agent_socket())
    )
    paths.admin_socket().chmod(0o600)
    paths.agent_socket().chmod(0o600)
    _publish_splice()
    log.info(
        "broker listening on %s and %s", paths.admin_socket(), paths.agent_socket()
    )

    try:
        async with admin, agent:
            await _until_idle(broker, stop)
    finally:
        await broker.close()
        for socket_path in (paths.admin_socket(), paths.agent_socket()):
            socket_path.unlink(missing_ok=True)


async def _until_idle(broker: Broker, stop: asyncio.Event | None) -> None:
    """Wait while the broker has work.

    A session with no client is not idle: its review may be finished and waiting
    for a human who has not arrived yet.
    """
    idle_for = 0.0
    while True:
        if stop is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)
            if stop.is_set():
                return
        else:
            await asyncio.sleep(5)
        if broker.sessions or broker.clients:
            idle_for = 0.0
            continue
        idle_for += 5
        if idle_for >= IDLE_EXIT_SECONDS:
            return


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="nvim-mcp: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
