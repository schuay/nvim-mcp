# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Own one headless nvim and apply agent requests to it.

The session outlives both the terminal a human attaches and the agent that
writes to it. Its id is the capability: it is created on the host with a root
the human chooses, and holding the id is what authorizes a client to use it.

The session is the record of what it shows and what the human has handed
back. nvim draws that record and reports the two things only it can know:
where a note has moved to as the human edits, and what the human asked. Both
arrive as a sync with every exchange, and nvim hands over a final one before
it exits. Notes carry nothing of nvim's, so the record survives it.

The nvim itself is the Editor's concern: it may be one this broker spawned or
one left running by a previous broker, and the session only has to know which
it got, to set up a fresh one or catch up with an adopted one.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .clamp import Root
from .lifecycle import Editor
from .nvimrpc import NvimGone, NvimRPC
from .paths import nvim_log, nvim_socket

#: A note is a label, not a document. Longer than this and it buries the code
#: it points at, however the editor folds it.
NOTE_LIMIT = 400

#: Bytes of buffer text a mark carries. A question is about a passage, and an
#: agent that needs more can read the file.
MARK_TEXT_LIMIT = 16 * 1024

#: The Lua half of the session, run once per nvim. It is shipped beside this
#: module rather than embedded in it so it reads as Lua.
SESSION_INIT = resources.files(__package__).joinpath("session.lua").read_text()

SHOW = "return NvimMcp.show(...)"
READ = "return NvimMcp.read(...)"
SYNC = "return NvimMcp.sync()"


def one_line(text: str) -> str:
    """Reduce a label to printable characters within the length limit.

    virt_lines accepts newlines and escape sequences without complaint, and the
    label is rendered in the human's terminal.
    """
    printable = "".join(ch for ch in text if ch.isprintable()).strip()
    if len(printable) <= NOTE_LIMIT:
        return printable
    return printable[: NOTE_LIMIT - 3] + "..."


@dataclass
class Location:
    #: Already resolved against the session root. Resolving once keeps the
    #: window between the check and nvim's open as small as it can be here.
    file: Path
    line: int = 1
    end_line: int | None = None
    text: str = ""


@dataclass
class Note:
    """One annotated location, as the session records it.

    The id is assigned by the session and never reused within it, so a mark
    that names the note it was asked on keeps naming it after later shows.
    The line is where nvim last reported the note, which follows the human's
    edits.
    """

    id: int
    file: str
    line: int
    end_line: int | None
    text: str


@dataclass
class Session:
    sid: str
    key: str
    root: Root
    socket: Path
    #: Start nvim without the human's config. Their plugins run in this session
    #: too, and one that authenticates or installs on startup blocks it.
    clean: bool = False
    #: 'light' or 'dark', taken from the human's terminal. A headless nvim
    #: cannot detect it.
    background: str | None = None
    #: The environment nvim runs with: the shell that ran `nv new`, so the
    #: session sees the same PATH and display the human does. Kept for the
    #: respawn after `:q`.
    env: dict[str, str] | None = None
    #: What the session shows and what the human has handed back. nvim draws
    #: this; it does not own it.
    notes: list[Note] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    next_note_id: int = 1
    title: str = "agent"
    #: Called when the record changes outside a tool call, so the broker can
    #: save it. Marks and positions can arrive from nvim on their own.
    on_change: Callable[[], None] | None = None
    editor: Editor = field(init=False, repr=False)
    #: Orders every exchange with nvim, including starting it. Two callers
    #: that find the session dead at the same moment would otherwise each
    #: spawn an nvim, and only one of them would be the session's.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

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
    ) -> Session:
        # The short id is for humans to type; the secret is what authorizes.
        key = key or f"{sid}-{secrets.token_hex(12)}"
        return cls(
            sid=sid,
            clean=clean,
            background=background,
            env=env,
            key=key,
            root=Root.of(root),
            # Name the socket after the key, not the reusable id. nvim unlinks
            # its listen socket when it exits, and a dying predecessor sharing
            # the path would delete a live successor's socket.
            socket=nvim_socket(key),
        )

    def state(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "key": self.key,
            "root": str(self.root.path),
            "clean": self.clean,
            "background": self.background,
            "env": self.env,
            "notes": [asdict(note) for note in self.notes],
            "marks": self.marks,
            "next_note_id": self.next_note_id,
            "title": self.title,
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
        )
        session.notes = [Note(**note) for note in state.get("notes", [])]
        session.marks = list(state.get("marks", []))
        session.next_note_id = int(state.get("next_note_id", 1))
        session.title = state.get("title", "agent")
        return session

    async def _setup(self, notes: list[dict[str, Any]] | None) -> None:
        """Install the session's Lua in the connected nvim.

        With notes, nvim starts drawing them; without, it keeps whatever it
        already draws, which is what an adopted nvim should do.
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
        # No notes means no argument at all: a nil positional would reach Lua
        # as vim.NIL, which is not nil.
        await self.rpc.lua(
            SESSION_INIT, options, *([notes] if notes is not None else [])
        )

    def _notes_for_lua(self) -> list[dict[str, Any]]:
        return [asdict(note) for note in self.notes]

    @property
    def alive(self) -> bool:
        return self.editor.alive

    async def ensure(self, spawn: bool = True) -> None:
        """Bring the session's nvim back if it is gone.

        `:q` in an attached UI kills the server outright, and the human has no
        reason to know that ends the review. Without `spawn`, only an nvim
        that is already running is taken.
        """
        async with self._lock:
            await self._ensure(spawn)

    async def _ensure(self, spawn: bool = True) -> None:
        outcome = await self.editor.ensure(spawn)
        if outcome == "started":
            await self._setup(self._notes_for_lua())
            # After a restart this is what puts the review back in front of
            # the human. Nobody is attached yet, so there is no view to preserve.
            if self.notes:
                await self._draw(focus=True)
        elif outcome == "adopted":
            # nvim has been on its own: it may hold moved notes and questions
            # asked while no broker was listening.
            await self._setup(None)
            assert self.rpc is not None
            self._absorb(await self.rpc.lua(SYNC))
            shown = await self.rpc.lua(
                "return vim.tbl_map(function(n) return n.id end, NvimMcp.notes)"
            )
            if list(shown or []) != [note.id for note in self.notes]:
                await self._draw(focus=False)

    async def _draw(self, focus: bool) -> None:
        assert self.rpc is not None
        result = await self.rpc.lua(
            SHOW, self._notes_for_lua(), {"title": self.title, "focus": focus}
        )
        self._absorb(result["sync"])

    async def _attempt(self, action: Callable[[], Awaitable[Any]]) -> Any:
        """Run one exchange with nvim, starting it again if it has gone.

        A session can die between two calls, and the failure surfaces only when
        the next one is sent.
        """
        async with self._lock:
            await self._ensure()
            try:
                return await action()
            except NvimGone:
                await self._ensure()
                return await action()

    async def show(
        self, locations: list[Location], title: str, focus: bool
    ) -> dict[str, Any]:
        self.title = title
        # The record changes first. If nvim has to be started for this call,
        # startup draws the record, and this call then draws it again.
        self.notes = [
            Note(
                id=self._take_note_id(),
                file=str(loc.file),
                line=loc.line,
                end_line=loc.end_line,
                text=one_line(loc.text),
            )
            for loc in locations
        ]

        async def run() -> Any:
            assert self.rpc is not None
            # Agent edits reach disk without passing through the broker, so
            # refresh before showing anything.
            await self.rpc.request("nvim_command", "checktime")
            return await self.rpc.lua(
                SHOW, self._notes_for_lua(), {"title": title, "focus": focus}
            )

        result = await self._attempt(run)
        self._absorb(result["sync"])
        return result

    def _take_note_id(self) -> int:
        note_id = self.next_note_id
        self.next_note_id += 1
        return note_id

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

        Positions are applied by note id, so a report about a note set that a
        later show has already replaced changes nothing.
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
        if method == "nvim-mcp" and params and params[0] == "ask":
            self._absorb({"marks": [params[1]]})
            self._changed()

    def _on_request(self, method: str, params: list[Any]) -> Any:
        if method == "nvim-mcp" and params and params[0] == "sync":
            self._absorb(params[1])
            self._changed()
            return True
        raise ValueError(f"unknown request {method} {params[:1]}")

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def attached(self) -> bool:
        """Report whether a UI is on this session's nvim.

        A dead nvim has no UI. Asking must not start one: `nv ls` asks about
        every session, and listing them is not a reason to bring them back.
        """
        async with self._lock:
            if not self.alive:
                return False
            assert self.rpc is not None
            try:
                return bool(await self.rpc.request("nvim_list_uis"))
            except NvimGone:
                return False

    async def _hand_over(self) -> None:
        """Take a final sync before dropping the connection.

        Once the connection closes, nvim's own hand-over on exit has nobody
        to give it to.
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
