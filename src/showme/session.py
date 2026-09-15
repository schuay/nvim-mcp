# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Own one headless nvim and apply agent requests to it.

Each conversation owns a session, including across resume. Sessions outlive
their agent and attached terminal so the human can review frames later.

A sandbox key restricts access to the human-selected root. A host key from the
admin socket permits access outside that root. Each call uses the access of the
key it presented.

The session records frames and human marks. nvim renders that state and reports
updated note positions and new marks on every exchange and before exit. The
`Editor` owns nvim startup, adoption, and shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .clamp import Anywhere, Refused, Root
from .lifecycle import Editor
from .nvimrpc import NvimError, NvimGone, NvimRPC
from .paths import nvim_log, nvim_socket

#: At 80 columns this occupies about 30 rows, leaving code visible.
NOTE_LIMIT = 2000

#: Bound short explicit lines that could exceed the character-based row estimate.
NOTE_LINES = 30

#: Expand tabs before display because virtual-line tab stops start at the band edge.
TAB_WIDTH = 8

#: Bound text copied into a mark; agents can read more from the file.
MARK_TEXT_LIMIT = 16 * 1024

#: Keep the nvim half as readable Lua and load it once per editor process.
SESSION_INIT = resources.files(__package__).joinpath("session.lua").read_text()

SHOW = "return ShowMe.show(...)"
READ = "return ShowMe.read(...)"
SYNC = "return ShowMe.sync()"


def clean(text: str) -> str:
    """Reduce a note to printable lines within the note's limits.

    Preserve line breaks so snippets retain their layout; Lua wraps only lines
    wider than the window. Remove other control characters because nvim renders
    embedded newlines as `^@` and could pass escape sequences to the terminal.
    """
    lines: list[str] = []
    for line in text.expandtabs(TAB_WIDTH).split("\n"):
        printable = "".join(ch if ch.isprintable() else " " for ch in line)
        stripped = printable.rstrip()
        # Remove blank rows that consume the limited note height without content.
        if stripped or (lines and lines[-1]):
            lines.append(stripped)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > NOTE_LINES:
        lines = [*lines[:NOTE_LINES], "..."]
    note = "\n".join(lines)
    if len(note) <= NOTE_LIMIT:
        return note
    return note[: NOTE_LIMIT - 3].rstrip() + "..."


@dataclass
class Location:
    #: Reuse the path that passed the root check when opening it in nvim.
    file: Path
    line: int = 1
    end_line: int | None = None
    text: str = ""


#: Refuse pushes at this depth so notes cannot push all code off screen.
FRAME_LIMIT = 4

#: Maximum number of additional launch key pairs retained.
LAUNCHES_REMEMBERED = 8
FRAME_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class Note:
    """One annotated location, as the session records it.

    Frames never reuse note numbers, so marks retain valid note references
    after later shows. The line follows nvim's anchor as the human edits.
    """

    id: str
    file: str
    line: int
    end_line: int | None
    text: str


@dataclass
class Frame:
    """One set of locations with its notes.

    Push digressions above the current review and pop them when answered. A
    frame keeps its letter until removal, so popping lower frames does not
    rename references above them.
    """

    letter: str
    title: str = "agent"
    notes: list[Note] = field(default_factory=list)
    next_number: int = 1

    def take_id(self) -> str:
        note_id = f"{self.letter}{self.next_number}"
        self.next_number += 1
        return note_id

    def state(self) -> dict[str, Any]:
        return {
            "letter": self.letter,
            "title": self.title,
            "notes": [asdict(note) for note in self.notes],
            "next_number": self.next_number,
        }

    @classmethod
    def restore(cls, state: dict[str, Any]) -> Frame:
        return cls(
            letter=state["letter"],
            title=state.get("title", "agent"),
            notes=[Note(**note) for note in state.get("notes", [])],
            next_number=int(state.get("next_number", 1)),
        )


@dataclass
class Session:
    sid: str
    key: str
    #: Admin-socket key that grants access outside the root. User-facing
    #: commands print only the clamped key to keep this out of sandboxes.
    host_key: str
    #: Base for relative paths and nvim's cwd; also the clamped-key boundary.
    root: Root
    socket: Path
    #: Skip plugins that could block startup for authentication or installation.
    clean: bool = False
    #: Human terminal background, which headless nvim cannot detect.
    background: str | None = None
    #: Saved human environment for nvim startup and respawn.
    env: dict[str, str] | None = None
    agent: str | None = None
    stable: bool = False
    last_seen: float = field(default_factory=time.time)
    #: Additional [clamped, unclamped] key pairs from resumed launches.
    also: list[list[str]] = field(default_factory=list)
    human: bool = False
    released: bool = False
    warned: bool = field(default=False, repr=False)
    #: Broker-owned display state and human replies, with frames bottom first.
    frames: list[Frame] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    #: Persist marks and positions received outside a tool call.
    on_change: Callable[[], None] | None = None
    editor: Editor = field(init=False, repr=False)
    #: Serialize state exchanges and prevent concurrent nvim startup.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Retain tasks spawned from synchronous nvim callbacks until completion.
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.editor = Editor(
            self.socket,
            self.root.path,
            nvim_log(self.sid),
            clean=self.clean,
            env=self.env,
        )

    @property
    def rpc(self) -> NvimRPC | None:
        return self.editor.rpc

    def authorize(self, key: str) -> Root | None:
        """Return the access granted by a matching key, or None.

        Encode client input because compare_digest rejects non-ASCII strings.
        """
        offered = key.encode()
        for clamped, unclamped in self.pairs:
            if secrets.compare_digest(offered, clamped.encode()):
                return self.root
            if secrets.compare_digest(offered, unclamped.encode()):
                return Anywhere(self.root.path)
        return None

    @property
    def pairs(self) -> list[tuple[str, str]]:
        """Return the original key pair followed by accepted launch pairs."""
        return [(self.key, self.host_key), *(tuple(p) for p in self.also)]  # type: ignore[misc]

    @property
    def spare(self) -> bool:
        """Whether an unused launcher session is eligible for replacement."""
        return not (self.agent or self.frames or self.human or self.released)

    def touch(self) -> None:
        self.last_seen = time.time()

    def accept(self, key: str, host_key: str, keep: Collection[str] = ()) -> None:
        """Accept another launch's keys while retaining the original pair.

        `keep` contains keys presented by live connections. Never evict a pair
        containing one of those keys when bounding the history across resumes.
        """
        if (key, host_key) not in self.pairs:
            self.also.append([key, host_key])
            self._forget(keep)

    def _forget(self, keep: Collection[str]) -> None:
        """Drop the oldest pairs no connection is answering with."""
        surplus = len(self.also) - LAUNCHES_REMEMBERED
        if surplus <= 0:
            return
        remaining = []
        for pair in self.also:
            if surplus and not any(key in keep for key in pair):
                surplus -= 1
                continue
            remaining.append(pair)
        self.also = remaining

    def revoke(self) -> None:
        """Replace keys transferred to another session to avoid duplicate matches."""
        self.key = f"{self.sid}-{secrets.token_hex(12)}"
        self.host_key = f"{self.sid}-{secrets.token_hex(12)}"
        self.also.clear()

    @classmethod
    def create(
        cls,
        sid: str,
        root: str | Path,
        *,
        clean: bool = False,
        background: str | None = None,
        env: dict[str, str] | None = None,
        key: str | None = None,
        host_key: str | None = None,
        socket: Path | None = None,
        human: bool = False,
    ) -> Session:
        # The short ID is only a label; the random key authorizes access.
        key = key or f"{sid}-{secrets.token_hex(12)}"
        return cls(
            sid=sid,
            clean=clean,
            human=human,
            background=background,
            env=env,
            key=key,
            host_key=host_key or f"{sid}-{secrets.token_hex(12)}",
            root=Root.of(root),
            # A unique path prevents an exiting nvim from unlinking its
            # successor's socket. Restore saved paths across key changes.
            socket=socket or nvim_socket(key),
        )

    def state(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "key": self.key,
            "host_key": self.host_key,
            "root": str(self.root.path),
            "clean": self.clean,
            "background": self.background,
            "env": self.env,
            "socket": str(self.socket),
            "agent": self.agent,
            "stable": self.stable,
            "human": self.human,
            "released": self.released,
            "also": self.also,
            "last_seen": self.last_seen,
            "frames": [frame.state() for frame in self.frames],
            "marks": self.marks,
        }

    @classmethod
    def restore(cls, state: dict[str, Any]) -> Session:
        session = cls.create(
            state["sid"],
            state["root"],
            clean=bool(state.get("clean")),
            background=state.get("background"),
            env=state.get("env"),
            key=state["key"],
            # Older state has no host key, so generate one that no client can hold.
            host_key=state.get("host_key"),
            socket=Path(state["socket"]) if state.get("socket") else None,
        )
        session.agent = state.get("agent")
        session.stable = bool(state.get("stable"))
        session.human = bool(state.get("human"))
        session.released = bool(state.get("released"))
        session.also = [list(pair) for pair in state.get("also", [])]
        # Give restored sessions a full retention period to reconnect.
        session.last_seen = time.time()
        session.frames = [Frame.restore(frame) for frame in state.get("frames", [])]
        session.marks = list(state.get("marks", []))
        return session

    @property
    def showing(self) -> str:
        """What the top frame holds, for a human picking between sessions."""
        if not self.frames:
            return ""
        top = self.frames[-1]
        return f"{top.letter}  {top.title} ({len(top.notes)})"

    @property
    def notes(self) -> list[Note]:
        """Every live note, bottom frame first."""
        return [note for frame in self.frames for note in frame.notes]

    async def _setup(self, frames: list[dict[str, Any]] | None) -> None:
        """Install the session's Lua in the connected nvim.

        Send frames to a new nvim. Omit them for an adopted nvim so its current
        display remains available for reconciliation.
        """
        assert self.rpc is not None
        self.rpc.on_notification = self._on_notification
        self.rpc.on_request = self._on_request
        channel, _ = await self.rpc.request("nvim_get_api_info")
        options = {
            "chan": channel,
            "background": self.background,
            "text_limit": MARK_TEXT_LIMIT,
        }
        # Omit absent frames because msgpack nil becomes truthy `vim.NIL` in Lua.
        await self.rpc.lua(
            SESSION_INIT, options, *([frames] if frames is not None else [])
        )

    async def wear_background(self, background: str) -> None:
        """Take the background an attaching terminal reported.

        Preserve an explicit `--light` or `--dark` setting. Do not save detected
        values because the next terminal may have a different background.
        """
        if self.background is not None or self.rpc is None:
            return
        await self.rpc.request("nvim_set_option_value", "background", background, {})

    def _frames_for_lua(self) -> list[dict[str, Any]]:
        return [frame.state() for frame in self.frames]

    @property
    def alive(self) -> bool:
        return self.editor.alive

    async def ensure(self, spawn: bool = True) -> None:
        """Bring the session's nvim back if it is gone.

        `:q` kills the server, but the session review must survive it. With
        `spawn=False`, adopt only an existing nvim.
        """
        async with self._lock:
            await self._ensure(spawn)

    async def _ensure(self, spawn: bool = True) -> None:
        outcome = await self.editor.ensure(spawn)
        if outcome == "started":
            await self._setup(self._frames_for_lua())
            # Restore the review with focus before any UI attaches.
            if self.frames:
                await self._draw(focus=True, restoring=True)
        elif outcome == "adopted":
            # Absorb positions and questions recorded while the broker was absent.
            await self._setup(None)
            assert self.rpc is not None
            self._absorb(await self.rpc.lua(SYNC))
            shown = await self.rpc.lua("return ShowMe.ids()")
            if list(shown or []) != [note.id for note in self.notes]:
                await self._draw(focus=False)

    async def _draw(
        self, focus: bool, open_files: bool = True, restoring: bool = False
    ) -> dict[str, Any]:
        assert self.rpc is not None
        result = await self.rpc.lua(
            SHOW,
            self._frames_for_lua(),
            {
                "focus": focus,
                "open": open_files,
                # A fresh nvim must load lower-frame files to render their notes.
                "open_all": restoring,
                # Keep question signs until the broker records an acknowledgement.
                "pending": [
                    mark["ask"]
                    for mark in self.marks
                    if mark.get("ask") and not mark.get("acked")
                ],
            },
        )
        self._absorb(result["sync"])
        return result

    async def _attempt(self, action: Callable[[], Awaitable[Any]]) -> Any:
        """Run one exchange with nvim under the session lock."""
        async with self._lock:
            return await self._exchange(action)

    async def _exchange(self, action: Callable[[], Awaitable[Any]]) -> Any:
        """Run one exchange with nvim, starting it again if it has gone.

        Retry once when nvim dies between calls. The caller holds the lock
        across record changes and response construction so concurrent callers
        cannot receive results for the same final state.
        """
        await self._ensure()
        try:
            return await action()
        except NvimGone:
            await self._ensure()
            return await action()

    async def show(
        self,
        locations: list[Location],
        title: str,
        focus: bool,
        frame: str = "replace",
    ) -> dict[str, Any]:
        """Change the frame stack and have nvim draw it.

        `replace` swaps the top frame's contents, `push` opens a frame above
        it, `pop` removes it. The record changes first: if nvim has to be
        started for this call, startup draws the record, and this call then
        draws it again.

        Hold the session lock from the record change through response creation.
        Multiple clients may share a session, and releasing it earlier can make
        both calls report the later state.
        """
        async with self._lock:
            return await self._show(locations, title, focus, frame)

    async def _show(
        self,
        locations: list[Location],
        title: str,
        focus: bool,
        frame: str,
    ) -> dict[str, Any]:
        popped = None
        if frame == "pop":
            popped = self._pop(None).letter
            top = self.frames[-1] if self.frames else None
        else:
            if frame == "push" or not self.frames:
                self._push()
            top = self.frames[-1]
            top.title = title
            top.notes = [
                Note(
                    id=top.take_id(),
                    file=str(loc.file),
                    line=loc.line,
                    end_line=loc.end_line,
                    text=clean(loc.text),
                )
                for loc in locations
            ]

        async def run() -> Any:
            assert self.rpc is not None
            # Load agent edits from disk before drawing the review.
            await self.rpc.request("nvim_command", "checktime")
            return await self._draw(focus=focus, open_files=frame != "pop")

        result = await self._exchange(run)
        # Open a window only when the atomic show reports no attached UI.
        result["opened_ui"] = not result.get("uis") and self.editor.open_window()
        result["frame"] = top.letter if top else None
        result["popped"] = popped
        result["ids"] = (
            [note.id for note in top.notes] if top and frame != "pop" else []
        )
        # Capture the stack before releasing the lock to a later caller.
        result["frames"] = [
            {"letter": f.letter, "title": f.title, "notes": len(f.notes)}
            for f in self.frames
        ]
        return result

    def _push(self) -> Frame:
        if len(self.frames) >= FRAME_LIMIT:
            raise Refused(f"frame stack is full; pop {self.frames[-1].letter} first")
        taken = {frame.letter for frame in self.frames}
        letter = next(letter for letter in FRAME_LETTERS if letter not in taken)
        frame = Frame(letter=letter)
        self.frames.append(frame)
        return frame

    def _pop(self, letter: str | None) -> Frame:
        if not self.frames:
            raise Refused("no frame to pop")
        if letter is None:
            return self.frames.pop()
        for index, frame in enumerate(self.frames):
            if frame.letter == letter:
                return self.frames.pop(index)
        raise Refused(f"no frame {letter}")

    async def _human_pop(self, letter: str | None) -> None:
        """Drop a frame at the human's request and redraw."""
        async with self._lock:
            if not self.alive:
                return
            try:
                popped = self._pop(letter)
            except Refused as e:
                await self._tell_human(str(e))
                return
            await self._draw(focus=False, open_files=False)
            self._changed()
            await self._tell_human(f"popped {popped.letter}")

    def warn(self, message: str) -> None:
        """Schedule an nvim notification without waiting for the editor."""
        if not self.alive:
            return
        task = asyncio.create_task(self._tell_human(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _tell_human(self, message: str) -> None:
        """Schedule the notification in nvim so RPC can return before dismissal.

        A long notification can make nvim wait for the user to press Enter.
        """
        assert self.rpc is not None
        with contextlib.suppress(Exception):
            await self.rpc.lua(
                "local message = ...\nvim.schedule(function() vim.notify(message) end)",
                f"showme: {message}",
            )

    async def read(self, what: str, options: dict[str, Any]) -> dict[str, Any]:
        async def run() -> Any:
            assert self.rpc is not None
            await self.rpc.request("nvim_command", "checktime")
            return await self.rpc.lua(READ, what, options)

        result = await self._attempt(run)
        self._absorb(result["sync"])
        return result

    def _absorb(self, sync: dict[str, Any]) -> bool:
        """Take what nvim reports into the record.

        Apply positions by note ID so stale reports cannot move replacement
        notes.
        """
        by_id = {note.id: note for note in self.notes}
        for position in sync.get("positions") or []:
            note = by_id.get(position.get("id"))
            if note is not None:
                note.line = position["line"]
                note.end_line = position.get("end_line")
        marks = sync.get("marks") or []
        for mark in marks:
            mark["id"] = len(self.marks) + 1
            self.marks.append(mark)
        return bool(marks)

    def _on_notification(self, method: str, params: list[Any]) -> None:
        if method != "showme" or not params:
            return
        if params[0] == "ask":
            self._absorb({"marks": [params[1]]})
            self._changed()
        elif params[0] == "pop":
            letter = params[1] if len(params) > 1 else None
            task = asyncio.create_task(self._human_pop(letter))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _on_request(self, method: str, params: list[Any]) -> Any:
        if method == "showme" and params and params[0] == "sync":
            self._absorb(params[1])
            self._changed()
            return True
        raise ValueError(f"unknown request {method} {params[:1]}")

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def attached(self, timeout: float = 30.0) -> bool:
        """Query attached UIs without starting nvim.

        Return False for a dead editor. Report a busy editor as an error so the
        collector preserves it. Apply one deadline to the lock and RPC request
        so a call waiting on nvim cannot also block the collector indefinitely.
        """
        try:
            async with asyncio.timeout(timeout):
                async with self._lock:
                    if not self.alive:
                        return False
                    assert self.rpc is not None
                    return bool(
                        await self.rpc.request("nvim_list_uis", timeout=timeout)
                    )
        except NvimGone:
            return False
        except TimeoutError:
            raise NvimError(f"session {self.sid} was busy for {timeout}s") from None

    async def _hand_over(self) -> None:
        """Take a final sync before dropping the connection.

        nvim cannot deliver its exit sync after the broker disconnects.
        """
        if self.rpc is not None:
            with contextlib.suppress(Exception):
                self._absorb(await self.rpc.lua(SYNC))

    async def detach(self) -> None:
        """Leave nvim running for the next broker."""
        async with self._lock:
            await self._hand_over()
            await self.editor.detach()

    async def close(self) -> None:
        """Stop nvim. The human's attached terminal goes with it."""
        async with self._lock:
            await self._hand_over()
            await self.editor.stop()
