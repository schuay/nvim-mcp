# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Run the host-side daemon that owns every nvim session.

One broker per user, held by a lock file. It listens on two sockets: an admin
socket for `showme`, which creates and kills sessions, and an agent socket
carrying MCP. Only the agent socket is meant to be reachable from a sandbox, so
session administration stays off the surface a sandboxed client can see.

Reaching the admin socket is itself the proof that a client is on the host,
which is what lets it take the key that reads outside its session's root. A
sandboxed one never gets to make the claim: its key is put where it will find
it by a launcher running out here.
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

from . import mcpserver, paths, splice, store
from .clamp import Refused, Root
from .session import Session

log = logging.getLogger(__name__)

#: Exit once nothing has needed the broker for this long.
IDLE_EXIT_SECONDS = 15 * 60

#: How long a line a client may send. One tool call is one line, and the
#: contract accepts 50 locations carrying 2000 characters of note each, which
#: is several times asyncio's 64 KiB default: a call the tools would have
#: accepted arrived as a closed connection instead. `showme` sends the whole
#: environment of the shell it ran in, which is smaller but not by much.
LINE_LIMIT = 4 * 1024 * 1024


def _guard_root(root: Path) -> None:
    """Refuse a root a launcher should never have picked on its own.

    `showme new` takes what the human typed. `showme ensure` takes the
    directory they happened to be standing in, and for the key it hands a
    sandboxed agent the root is a second access list: that agent reads
    everything under it through the broker, whatever its sandbox mounts.

    It catches a home directory and a tree with no repository at or above it,
    which is usually a parent holding several. It does not catch a repository
    that contains repositories -- a checkout with worktrees or vendored
    subrepos under it -- because nothing here distinguishes that from a
    project with submodules. For those, the root `showme box` prints is the
    check.
    """
    home = Path.home()
    if root == home or root in home.parents:
        raise Refused(f"{root} is too broad a root; name a project directory")
    for directory in (root, *root.parents):
        if (directory / ".git").exists():
            return
        if directory == home:
            break
    raise Refused(
        f"no repository at or above {root}; `showme new {root}` if you mean it"
    )


class Broker:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        #: Live client connections. Closing a listener waits for these, so
        #: shutdown has to end them itself or a connected client pins the broker.
        self.connections: set[asyncio.Task[None]] = set()
        #: Held while the registry changes. Creating a session awaits nvim
        #: startup, and two creations that pick an id before either has
        #: registered would pick the same one.
        self._lock = asyncio.Lock()
        #: Set to end this broker. `showme restart-broker` uses it so a broker
        #: carrying stale code goes away the way one that idles out does:
        #: state saved, sessions detached, nvim left running for the next.
        self.stop = asyncio.Event()

    @property
    def clients(self) -> int:
        return len(self.connections)

    def save(self) -> None:
        store.save([session.state() for session in self.sessions.values()])

    async def restore(self) -> None:
        """Take over the sessions a previous broker left behind.

        Their nvims are usually still running, with the human attached, and
        are adopted now so their questions have somewhere to go. One that has
        gone is started again when something next needs it.
        """
        async with self._lock:
            for state in store.load():
                try:
                    session = Session.restore(state)
                except (KeyError, TypeError, ValueError, Refused):
                    log.exception("cannot restore session %s", state.get("sid"))
                    continue
                session.on_change = self.save
                self.sessions[session.sid] = session
                try:
                    await session.ensure(spawn=False)
                except (OSError, RuntimeError):
                    log.exception("session %s: could not adopt its nvim", session.sid)
            if self.sessions:
                log.info("restored %d session(s)", len(self.sessions))

    def session_by_key(self, key: str) -> tuple[Session, Root] | None:
        """Return the session `key` names and what that key reaches in it."""
        for session in self.sessions.values():
            root = session.authorize(key) if key else None
            if root is not None:
                return session, root
        return None

    async def new_session(
        self,
        root: str,
        clean: bool = False,
        background: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Session:
        async with self._lock:
            return await self._create(root, clean, background, env)

    async def ensure_session(
        self,
        root: str,
        clean: bool = False,
        background: str | None = None,
        env: dict[str, str] | None = None,
        *,
        spawn: bool = True,
        unclamped: bool = False,
    ) -> tuple[Session, bool]:
        """Return the session already rooted here, or make one.

        A launcher runs this every time it starts an agent, so a second one in
        the same tree joins the review already on the human's screen instead of
        opening an editor nobody is looking at. The lookup and the create share
        one lock: two launchers racing in the same tree must not end up with a
        session each.

        `unclamped` asks for the key that reads outside the root, which only a
        client on the host can ask for and which the guard has no say over.
        """
        async with self._lock:
            # A clamped key is only ever issued for a root the guard allows,
            # whether this call makes the session or finds one: a host client
            # answers to no guard, and a launcher must not inherit the root it
            # picked. `showme new` still sets any root the human names, and
            # prints the key for them to pass on by hand.
            if not unclamped:
                _guard_root(Path(root))
            for session in self.sessions.values():
                if str(session.root.path) == root:
                    return session, False
            return await self._create(root, clean, background, env, spawn), True

    async def _create(
        self,
        root: str,
        clean: bool,
        background: str | None,
        env: dict[str, str] | None,
        spawn: bool = True,
    ) -> Session:
        sid = self._next_id()
        session = Session.create(sid, root, clean=clean, background=background, env=env)
        session.on_change = self.save
        # Without `spawn` the session is only recorded: an agent that never
        # shows anything should not cost the human an editor process.
        await session.ensure(spawn)
        self.sessions[sid] = session
        self.save()
        return session

    def _next_id(self) -> str:
        used = {int(sid) for sid in self.sessions}
        candidate = 1
        while candidate in used:
            candidate += 1
        return str(candidate)

    async def kill(self, sid: str) -> bool:
        async with self._lock:
            session = self.sessions.pop(sid, None)
            if session is None:
                return False
            await session.close()
            self.save()
            return True

    async def attach(self, sid: str, background: str | None = None) -> Session | None:
        """Have a session's nvim running so a terminal can attach to it.

        `background` is what the attaching terminal answered, which only that
        process could ask; the session decides whether it outranks its own.
        """
        session = self.sessions.get(sid)
        if session is not None:
            await session.ensure()
            if background is not None:
                await session.wear_background(background)
        return session

    async def close(self) -> None:
        """Let go of every session's nvim without stopping it.

        The human may be attached, with unsaved edits. The next broker adopts
        what this one leaves running.
        """
        for session in list(self.sessions.values()):
            await session.detach()
        self.sessions.clear()


async def _admin_client(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one `showme` command.

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
    handler = ADMIN_COMMANDS.get(str(command))
    if handler is None:
        return {"ok": False, "error": f"unknown command: {command}"}
    try:
        return {"ok": True, **await handler(broker, request)}
    except Refused as e:
        return {"ok": False, "error": str(e)}


async def _ping(_broker: Broker, _request: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _new(broker: Broker, request: dict[str, Any]) -> dict[str, Any]:
    session = await broker.new_session(
        request["root"],
        bool(request.get("clean")),
        request.get("background"),
        request.get("env"),
    )
    return {
        "id": session.sid,
        "key": session.key,
        "root": str(session.root.path),
        "socket": str(session.socket),
    }


async def _ensure(broker: Broker, request: dict[str, Any]) -> dict[str, Any]:
    # Only a client that reached this socket can ask for the key that reads
    # outside the root, and reaching it is the proof: the admin socket lives in
    # the runtime directory, which no sandbox mounts.
    unclamped = bool(request.get("open"))
    session, created = await broker.ensure_session(
        request["root"],
        bool(request.get("clean")),
        request.get("background"),
        request.get("env"),
        spawn=bool(request.get("spawn", True)),
        unclamped=unclamped,
    )
    return {
        "id": session.sid,
        "key": session.host_key if unclamped else session.key,
        "root": str(session.root.path),
        "socket": str(session.socket),
        "created": created,
    }


async def _stop(broker: Broker, _request: dict[str, Any]) -> dict[str, Any]:
    # The reply goes out before the loop notices: closing the listeners waits
    # for the connection this arrived on.
    broker.stop.set()
    return {"sessions": len(broker.sessions)}


async def _ls(broker: Broker, _request: dict[str, Any]) -> dict[str, Any]:
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
    return {"sessions": listing}


async def _attach(broker: Broker, request: dict[str, Any]) -> dict[str, Any]:
    background = request.get("background")
    session = await broker.attach(
        str(request["id"]), str(background) if background is not None else None
    )
    if session is None:
        raise Refused(f"no session {request['id']}")
    return {"socket": str(session.socket)}


async def _kill(broker: Broker, request: dict[str, Any]) -> dict[str, Any]:
    if not await broker.kill(str(request["id"])):
        raise Refused(f"no session {request['id']}")
    return {}


ADMIN_COMMANDS = {
    "ping": _ping,
    "new": _new,
    "ensure": _ensure,
    "stop": _stop,
    "ls": _ls,
    "attach": _attach,
    "kill": _kill,
}


class _Pushback:
    """A reader handing back the line already taken off the stream.

    Only `readline` is used by the MCP framing, so this is the whole surface.
    """

    def __init__(self, reader: asyncio.StreamReader, line: bytes) -> None:
        self._reader = reader
        self._line: bytes | None = line

    async def readline(self) -> bytes:
        line, self._line = self._line, None
        return line if line is not None else await self._reader.readline()


async def _hello(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[Any, str | None]:
    """Take the client's opening line and, if it names a session, answer it.

    A client launched for one session presents its key here instead of putting
    it in every call, which is what lets a sandboxed one work without ever
    being told a key. A client that sends JSON-RPC straight away gets its line
    handed back unread.
    """
    line = await reader.readline()
    try:
        opening = json.loads(line)
    except ValueError:
        opening = None
    if not isinstance(opening, dict) or splice.HELLO not in opening:
        return _Pushback(reader, line), None
    key = str(opening[splice.HELLO].get("key") or "")
    found = broker.session_by_key(key)
    answer: dict[str, Any] = {"ok": found is not None}
    if found is not None:
        session, root = found
        answer["session"] = session.sid
        # Nothing about the connection says whether this client is sandboxed,
        # and nothing here needs to: which key it presented is what it reaches,
        # and is the only account of it worth keeping.
        log.info("session %s: client holds the %s key", session.sid, root.scope)
    else:
        answer["error"] = "no such session"
    writer.write(json.dumps({splice.HELLO: answer}).encode() + b"\n")
    await writer.drain()
    return reader, key if found is not None else None


async def _agent_client(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    task = asyncio.current_task()
    if task is not None:
        broker.connections.add(task)
    try:
        stream, key = await _hello(broker, reader, writer)
    except (OSError, asyncio.IncompleteReadError):
        broker.connections.discard(task)
        writer.close()
        return
    server = mcpserver.build(broker.session_by_key, broker.save, default_key=key)
    try:
        await mcpserver.serve(stream, writer, server)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("agent connection failed")
    finally:
        broker.connections.discard(task)
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
    if stop is not None:
        broker.stop = stop
    await broker.restore()
    for socket_path in (paths.admin_socket(), paths.agent_socket()):
        socket_path.unlink(missing_ok=True)

    admin = await asyncio.start_unix_server(
        lambda r, w: _admin_client(broker, r, w),
        path=str(paths.admin_socket()),
        limit=LINE_LIMIT,
    )
    agent = await asyncio.start_unix_server(
        lambda r, w: _agent_client(broker, r, w),
        path=str(paths.agent_socket()),
        limit=LINE_LIMIT,
    )
    paths.admin_socket().chmod(0o600)
    paths.agent_socket().chmod(0o600)
    _publish_splice()
    log.info(
        "broker listening on %s and %s", paths.admin_socket(), paths.agent_socket()
    )

    try:
        async with admin, agent:
            await _until_idle(broker)
            for connection in list(broker.connections):
                connection.cancel()
            await asyncio.gather(*broker.connections, return_exceptions=True)
    finally:
        # Record where the sessions stood before they go, so the next broker
        # brings back the same review.
        broker.save()
        await broker.close()
        for socket_path in (paths.admin_socket(), paths.agent_socket()):
            socket_path.unlink(missing_ok=True)


async def _until_idle(broker: Broker) -> None:
    """Wait while the broker has work.

    A session with no client is not idle: its review may be finished and waiting
    for a human who has not arrived yet.
    """
    idle_for = 0.0
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(broker.stop.wait(), 5)
        if broker.stop.is_set():
            return
        if broker.sessions or broker.clients:
            idle_for = 0.0
            continue
        idle_for += 5
        if idle_for >= IDLE_EXIT_SECONDS:
            return


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="showme: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
