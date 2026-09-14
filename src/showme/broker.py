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

Launchers prepare separate sessions. The client identifies its conversation
at hello, allowing the broker to select an existing session on resume.
Separate conversations keep separate frames even when they share a root.

The collector expires automatic sessions with no clients or attached UIs.
Manual sessions persist until explicitly killed.
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
from .nvimrpc import NvimError
from .session import Session

log = logging.getLogger(__name__)

#: Exit once nothing has needed the broker for this long.
IDLE_EXIT_SECONDS = 15 * 60

#: Interval between collection passes.
TICK_SECONDS = 5.0

#: Retention for sessions with harness ids and process-local ids, respectively.
RESUMABLE_SECONDS = 24 * 60 * 60
LAUNCH_SECONDS = 30 * 60

#: Grace period for a terminal connecting to a released session.
RELEASED_SECONDS = 60.0

#: Timeout for the nvim UI request.
PROBE_SECONDS = 5.0

#: Accept batches of 50 locations with 2000-character notes, which exceed
#: asyncio's default 64 KiB line limit.
LINE_LIMIT = 4 * 1024 * 1024


@dataclass
class Claim:
    """The claimed session and the access granted by the presented key.

    Preserve the presented key so host clients keep access outside the root.
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
        #: Connection counts prevent collection while any client holds a session.
        #: MCP process restarts can leave overlapping connections.
        self.held: dict[str, int] = {}
        #: What each live connection presented. Keyed on the connection rather
        #: than the session id because ids are handed out again: a client of
        #: the session that had an id must not keep a key alive in the session
        #: that has it now.
        self.presented: dict[asyncio.Task[None], Claim] = {}
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

    def hold(self, session: Session) -> None:
        self.held[session.sid] = self.held.get(session.sid, 0) + 1

    def release(self, session: Session) -> None:
        """Release a connection's hold.

        Check object identity because session ids are reused after deletion.
        """
        if self.sessions.get(session.sid) is not session:
            return
        if self.held.get(session.sid, 0) <= 1:
            self.held.pop(session.sid, None)
        else:
            self.held[session.sid] -= 1

    def live_keys(self, session: Session) -> set[str]:
        """The keys connections to this session are still answering with.

        By identity, like `release`, and for the same reason.
        """
        return {
            claim.key for claim in self.presented.values() if claim.session is session
        }

    def in_use(self, key: str) -> tuple[Session, Root] | None:
        """Resolve a tool-call key and refresh the session expiry time."""
        found = self.session_by_key(key)
        if found is not None:
            found[0].touch()
        return found

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
        """Create a manual session that persists until `showme kill`."""
        async with self._lock:
            return await self._create(root, clean, background, env, human=True)

    async def launch_session(
        self,
        root: str,
        clean: bool = False,
        background: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Session:
        """Prepare a separate session for each launcher.

        The client identifies its conversation at hello, when claim() can
        select an existing session. Defer nvim startup until the first show,
        using the root and environment supplied by this launcher.
        """
        async with self._lock:
            return await self._create(root, clean, background, env, spawn=False)

    async def claim(self, key: str, agent: str, stable: bool) -> Claim | None:
        """Select the conversation's session and preserve the presented key's access.

        A resumed conversation can replace an unused launcher session. Transfer
        its keys so clients can keep using the credentials they received.
        """
        async with self._lock:
            found = self.session_by_key(key)
            if found is None:
                return None
            prepared, root = found
            if not agent:
                # Legacy clients use the session named by their key.
                return Claim(key, prepared, root)
            session = prepared
            if prepared.spare:
                # Only replace an unused launcher session. Preserve explicit
                # session keys and keys from earlier connections.
                mine = self._session_of(agent, besides=prepared)
                if mine is not None:
                    keep = self.live_keys(mine)
                    for pair in prepared.pairs:
                        mine.accept(*pair, keep=keep)
                    root = mine.authorize(key) or root
                    self._release(prepared)
                    session = mine
            session.agent, session.stable = agent, stable
            # Reclaiming a released session restores its normal retention period.
            session.released = False
            session.touch()
            self.save()
            return Claim(key, session, root)

    def _session_of(self, agent: str, besides: Session) -> Session | None:
        """Find this conversation in the same root.

        A different root requires a separate session because the root controls
        file access and nvim's working directory.
        """
        return next(
            (
                session
                for session in self.sessions.values()
                if session.agent == agent
                and session is not besides
                and session.root.path == besides.root.path
            ),
            None,
        )

    def _release(self, session: Session) -> None:
        """Rotate the spare session's keys and mark it for collection.

        Leave nvim running: a terminal may already be attached or still
        connecting. The collector checks for attached UIs after a grace period.
        """
        session.revoke()
        session.released = True
        # The grace is for a terminal on its way in, so it starts here. A spare
        # prepared minutes ago is already older than the window, and would go
        # on the collector's next pass with nothing having had time to draw.
        session.touch()

    async def _create(
        self,
        root: str,
        clean: bool,
        background: str | None,
        env: dict[str, str] | None,
        *,
        spawn: bool = True,
        human: bool = False,
    ) -> Session:
        sid = self._next_id()
        session = Session.create(
            sid, root, clean=clean, background=background, env=env, human=human
        )
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
            # The id is handed out again, and a hold left behind would keep
            # the next session to take it from ever being collected.
            self.held.pop(sid, None)
            await session.close()
            self.save()
            return True

    def session_in(self, directory: str) -> Session | None:
        """Select a session for a directory.

        Prefer the nearest ancestor root, then a connected client, then recent
        use. A collector probe refreshes last_seen for attached terminals, so
        timestamps alone can select an older review over an active client.
        """
        wanted = Path(directory)
        if not wanted.is_absolute():
            # The client resolves it; anything relative that reaches here was
            # taken against some other shell's directory already.
            return None
        rooted = [
            session
            for session in self.sessions.values()
            if not session.released
            and (session.root.path == wanted or session.root.path in wanted.parents)
        ]
        # Prefer connected clients over timestamps refreshed by UI probes.
        return max(
            rooted,
            key=lambda s: (
                len(s.root.path.parts),
                bool(self.held.get(s.sid)),
                s.last_seen,
            ),
            default=None,
        )

    async def attach(self, sid: str, background: str | None = None) -> Session | None:
        """Have a session's nvim running so a terminal can attach to it.

        `background` is what the attaching terminal answered, which only that
        process could ask; the session decides whether it outranks its own.
        """
        session = self.sessions.get(sid) or self.session_in(sid)
        if session is not None:
            # Refresh expiry before awaiting nvim startup so collection cannot race attach.
            session.touch()
            await session.ensure()
            if background is not None:
                await session.wear_background(background)
        return session

    async def collect(self) -> None:
        """Collect expired sessions with no client or attached terminal.

        Recheck eligibility under the registry lock after probing nvim: a
        client may connect during the probe. Leave unresponsive editors alive.
        """
        for session in await self._stale():
            try:
                busy = await session.attached(timeout=PROBE_SECONDS)
            except NvimError:
                # Preserve unresponsive editors and defer the next probe.
                session.touch()
                continue
            if busy:
                # Refresh retention while a terminal is attached.
                session.touch()
                if session.released and not session.warned:
                    # Tell the attached user how to reach the resumed conversation.
                    session.warned = True
                    session.warn(
                        f"this session was handed over -- "
                        f"run: showme {session.root.path}"
                    )
                continue
            async with self._lock:
                if self.sessions.get(session.sid) is not session:
                    continue
                if not self._is_stale(session):
                    continue
                self.sessions.pop(session.sid, None)
            log.info("session %s: collected, nothing was using it", session.sid)
            await session.close()
            self.save()

    async def _stale(self) -> list[Session]:
        async with self._lock:
            return [s for s in self.sessions.values() if self._is_stale(s)]

    def _is_stale(self, session: Session) -> bool:
        if session.human or self.held.get(session.sid):
            return False
        age = time.time() - session.last_seen
        if age < 0:
            # The clock moved backwards. Taking it as contact costs a session
            # one window; the alternative is never collecting it again.
            session.touch()
            return False
        if session.released:
            # Allow time for a terminal to connect before collecting the spare.
            return age >= RELEASED_SECONDS
        return age >= (RESUMABLE_SECONDS if session.stable else LAUNCH_SECONDS)

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
    # A snapshot: the collector can take a session while this awaits nvim.
    for session in list(broker.sessions.values()):
        listing.append(
            {
                "id": session.sid,
                "key": session.key,
                "root": str(session.root.path),
                "socket": str(session.socket),
                "attached": await _attached(session),
                "showing": session.showing,
            }
        )
    return {"sessions": listing}


async def _attached(session: Session) -> bool:
    """Report UI attachment for listings; report probe failures as detached."""
    try:
        return await session.attached(timeout=PROBE_SECONDS)
    except NvimError:
        return False


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
    """Read the optional session claim before serving MCP.

    Clients without a hello send JSON-RPC directly; return their first line
    to the MCP reader.
    """
    line = await reader.readline()
    try:
        opening = json.loads(line)
    except ValueError:
        opening = None
    if not isinstance(opening, dict) or splice.HELLO not in opening:
        return _Pushback(reader, line), None
    body = opening[splice.HELLO]
    if not isinstance(body, dict):
        # Whatever a client puts under the field, from the one socket a
        # sandbox can reach. It gets the same answer as a key nobody issued.
        body = {}
    key = str(body.get("key") or "")
    claimed = await broker.claim(
        key, str(body.get("agent") or ""), bool(body.get("stable"))
    )
    answer: dict[str, Any] = {"ok": claimed is not None}
    if claimed is not None:
        answer["session"] = claimed.session.sid
        # Access is determined by the presented key.
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
    session = None
    # Remove the connection on every exit path so failures cannot prevent idle exit.
    try:
        try:
            stream, claimed = await _hello(broker, reader, writer)
        except (OSError, asyncio.IncompleteReadError):
            return
        session = claimed.session if claimed is not None else None
        if claimed is not None:
            broker.hold(claimed.session)
            if task is not None:
                broker.presented[task] = claimed
        server = mcpserver.build(
            broker.in_use,
            broker.save,
            default_key=claimed.key if claimed is not None else None,
        )
        await mcpserver.serve(stream, writer, server)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("agent connection failed")
    finally:
        if session is not None:
            # The conversation may be back -- resumed, or its server restarted
            # -- so the session stays, and this is when it starts ageing.
            broker.release(session)
            session.touch()
            broker.save()
        if task is not None:
            broker.presented.pop(task, None)
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
    """Keep the broker running while clients or retained sessions exist."""
    idle_for = 0.0
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(broker.stop.wait(), TICK_SECONDS)
        if broker.stop.is_set():
            return
        await broker.collect()
        if broker.sessions or broker.clients:
            idle_for = 0.0
            continue
        idle_for += TICK_SECONDS
        if idle_for >= IDLE_EXIT_SECONDS:
            return


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="showme: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
