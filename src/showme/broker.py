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

A session belongs to one agent conversation. A launcher prepares one per
launch because it cannot know which conversation it is starting; the client
names that conversation when it connects, and this is where the two are put
together -- handing back the session a resumed conversation already had, and
dropping the spare. Keying sessions on the tree instead is what let one
conversation's notes land in a frame another had abandoned there.

Nothing marks the end of a conversation, so the collector decides: a session
no client holds, with no terminal on it, is taken once its window has passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import mcpserver, paths, splice, store
from .clamp import Refused, Root
from .session import Session

log = logging.getLogger(__name__)

#: Exit once nothing has needed the broker for this long.
IDLE_EXIT_SECONDS = 15 * 60

#: How long a detached session is kept before the collector takes it. A named
#: conversation may be resumed, and is given a day to come back; one that made
#: its own name cannot be resumed at all, so it is kept only long enough for
#: the human to finish reading a review the agent left behind.
RESUMABLE_SECONDS = 24 * 60 * 60
LAUNCH_SECONDS = 30 * 60

#: How long a line a client may send. One tool call is one line, and the
#: contract accepts 50 locations carrying 2000 characters of note each, which
#: is several times asyncio's 64 KiB default: a call the tools would have
#: accepted arrived as a closed connection instead. `showme` sends the whole
#: environment of the shell it ran in, which is smaller but not by much.
LINE_LIMIT = 4 * 1024 * 1024


@dataclass
class Claim:
    """The session a client reached, and what the key it presented reaches.

    The key is kept as the client sent it: a host client presents the
    unclamped one, and answering its later calls with the session's clamped
    key instead would quietly take away the reach it connected with.
    """

    key: str
    session: Session
    root: Root


class Broker:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        #: Live client connections. Closing a listener waits for these, so
        #: shutdown has to end them itself or a connected client pins the broker.
        self.connections: set[asyncio.Task[None]] = set()
        #: Sessions a client is holding right now, by sid. The collector
        #: leaves these alone however old their last contact is.
        self.held: set[str] = set()
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

    async def launch_session(
        self,
        root: str,
        clean: bool = False,
        background: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Session:
        """Record a session for a launcher about to start an agent.

        One per launch, never shared: a launcher cannot know which
        conversation it is starting -- the harness has not assigned an id yet
        -- so it cannot pick the session, only prepare one. Which session the
        agent ends up on is settled at the hello, where its own name is
        finally known, and this one is handed over or dropped there.

        nvim waits until something is shown. An agent that opens nothing
        should not cost the human an editor process, and the launch context
        collected here -- the shell's environment, the terminal's background --
        is what that nvim will be started with whenever it is.

        The root is the directory the launcher was standing in, and nothing
        here second-guesses it. The tree it names is the one that launcher has
        already handed the agent, so a key clamped to it reaches nothing the
        agent could not reach with its own tools, and a directory that holds no
        repository is a project like any other.
        """
        async with self._lock:
            return await self._create(root, clean, background, env, spawn=False)

    async def claim(self, key: str, agent: str, stable: bool) -> Claim | None:
        """Give a client the session its conversation is on.

        The key says which launch this is and what that launch may reach; the
        agent says whose conversation it serves. A conversation that already
        has a session takes it back -- this is a resumed one, or a client
        reconnecting after its MCP server was restarted -- and the session
        left by the launch is dropped, having never been shown anything. The
        session that survives answers to the keys the client actually holds,
        since the ones it was created with are the only pair that client
        knows.
        """
        async with self._lock:
            found = self.session_by_key(key)
            if found is None:
                return None
            session, root = found
            if not agent:
                # A client that does not name itself gets what its key names
                # and nothing more: with no conversation to speak of, there is
                # nothing to hand back to it later.
                return Claim(key, session, root)
            held = self._session_of(agent, besides=session)
            if held is not None:
                held.rekey(session.key, session.host_key)
                root = held.authorize(key) or root
                await self._drop(session)
                session = held
            session.agent, session.stable = agent, stable
            session.touch()
            self.save()
            return Claim(key, session, root)

    def _session_of(self, agent: str, besides: Session) -> Session | None:
        return next(
            (
                session
                for session in self.sessions.values()
                if session.agent == agent and session is not besides
            ),
            None,
        )

    async def _drop(self, session: Session) -> None:
        self.sessions.pop(session.sid, None)
        await session.close()

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

    def session_in(self, directory: str) -> Session | None:
        """The session to attach to for a tree, newest use first.

        A launcher prints the root rather than an id, because the id it made
        is not always the one the agent ends up on. A tree with several
        conversations working in it answers with the one most recently used,
        and `showme ls` is how to reach any of the others.
        """
        try:
            wanted = Path(directory).expanduser().resolve()
        except OSError:
            return None
        rooted = [
            session
            for session in self.sessions.values()
            if session.root.path == wanted or wanted in session.root.path.parents
        ]
        return max(rooted, key=lambda s: s.last_seen, default=None)

    async def attach(self, sid: str, background: str | None = None) -> Session | None:
        """Have a session's nvim running so a terminal can attach to it.

        `background` is what the attaching terminal answered, which only that
        process could ask; the session decides whether it outranks its own.
        """
        session = self.sessions.get(sid) or self.session_in(sid)
        if session is not None:
            await session.ensure()
            if background is not None:
                await session.wear_background(background)
        return session

    async def collect(self) -> None:
        """Stop sessions nothing is using any more.

        A conversation ends without telling anyone: the client goes away and
        the session it was on stays, holding an editor and a set of frames
        nobody will add to. Age alone is not enough to act on, because the
        review may have outlived the agent on purpose and the human may still
        be reading it -- so a session with a UI on it is in use whatever its
        client did, and one with none is taken once its window has passed.
        """
        for session in list(self.sessions.values()):
            if session.sid in self.held:
                continue
            window = RESUMABLE_SECONDS if session.stable else LAUNCH_SECONDS
            if time.time() - session.last_seen < window:
                continue
            if await session.attached():
                # Being read counts as being used, and the human closing that
                # terminal is what starts the clock again.
                session.touch()
                continue
            log.info("session %s: collected, nothing was using it", session.sid)
            await self.kill(session.sid)

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
    session = await broker.launch_session(
        request["root"],
        bool(request.get("clean")),
        request.get("background"),
        request.get("env"),
    )
    return {
        "id": session.sid,
        "key": session.host_key if unclamped else session.key,
        "root": str(session.root.path),
        "socket": str(session.socket),
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
                "showing": session.showing,
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
) -> tuple[Any, Claim | None]:
    """Take the client's opening line and, if it names a session, answer it.

    A client launched for one session presents its key here instead of putting
    it in every call, which is what lets a sandboxed one work without ever
    being told a key. It names its conversation here too, and this is where a
    session is finally given to one. A client that sends JSON-RPC straight
    away gets its line handed back unread.
    """
    line = await reader.readline()
    try:
        opening = json.loads(line)
    except ValueError:
        opening = None
    if not isinstance(opening, dict) or splice.HELLO not in opening:
        return _Pushback(reader, line), None
    body = opening[splice.HELLO]
    key = str(body.get("key") or "")
    claimed = await broker.claim(
        key, str(body.get("agent") or ""), bool(body.get("stable"))
    )
    answer: dict[str, Any] = {"ok": claimed is not None}
    if claimed is not None:
        answer["session"] = claimed.session.sid
        # Nothing about the connection says whether this client is sandboxed,
        # and nothing here needs to: which key it presented is what it reaches,
        # and is the only account of it worth keeping.
        log.info(
            "session %s: client holds the %s key",
            claimed.session.sid,
            claimed.root.scope,
        )
    else:
        answer["error"] = "no such session"
    writer.write(json.dumps({splice.HELLO: answer}).encode() + b"\n")
    await writer.drain()
    return reader, claimed


async def _agent_client(
    broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    task = asyncio.current_task()
    if task is not None:
        broker.connections.add(task)
    try:
        stream, claimed = await _hello(broker, reader, writer)
    except (OSError, asyncio.IncompleteReadError):
        broker.connections.discard(task)
        writer.close()
        return
    session = claimed.session if claimed is not None else None
    key = claimed.key if claimed is not None else None
    server = mcpserver.build(broker.session_by_key, broker.save, default_key=key)
    if session is not None:
        broker.held.add(session.sid)
    try:
        await mcpserver.serve(stream, writer, server)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("agent connection failed")
    finally:
        if session is not None:
            # The conversation may be back -- resumed, or its server restarted
            # -- so the session stays, and this is when it starts ageing.
            broker.held.discard(session.sid)
            session.touch()
            broker.save()
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

    A session with no client is not idle: its review may be finished and
    waiting for a human who has not arrived yet. The collector is what
    eventually decides one is nobody's, and until it does the broker stays.
    """
    idle_for = 0.0
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(broker.stop.wait(), 5)
        if broker.stop.is_set():
            return
        await broker.collect()
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
