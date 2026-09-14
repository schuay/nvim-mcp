# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Own one headless nvim and apply agent requests to it.

The session outlives both the terminal a human attaches and the agent that
writes to it. A key is the capability, and a session has two: the one a
launcher hands a sandboxed client, clamped to the root the human chose, and
the one only the admin socket gives out, which reads anywhere because the
client holding it is the human. Which key a call arrived with decides what it
may open; the session itself decides nothing.

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

from .clamp import Anywhere, Refused, Root
from .lifecycle import Editor
from .nvimrpc import NvimGone, NvimRPC
from .paths import nvim_log, nvim_socket

#: A note may explain itself, but it still has to leave the code visible. At 80
#: columns a wrapped line carries about 65 characters, so this is roughly three
#: quarters of a 40-row terminal for a single note.
NOTE_LIMIT = 2000

#: Lines a note may draw. The character limit alone does not bound the band
#: now that line breaks survive: a snippet of short lines would spend the same
#: characters on three times the rows and bury the code it annotates.
NOTE_LINES = 30

#: A tab in a virtual line expands against the window's own tab stops, which
#: are not the ones the snippet was written against and count from the band's
#: left edge rather than the code's. Expand here instead, at nvim's default.
TAB_WIDTH = 8

#: Bytes of buffer text a mark carries. A question is about a passage, and an
#: agent that needs more can read the file.
MARK_TEXT_LIMIT = 16 * 1024

#: The Lua half of the session, run once per nvim. It is shipped beside this
#: module rather than embedded in it so it reads as Lua.
SESSION_INIT = resources.files(__package__).joinpath("session.lua").read_text()

SHOW = "return ShowMe.show(...)"
READ = "return ShowMe.read(...)"
SYNC = "return ShowMe.sync()"


def clean(text: str) -> str:
    """Reduce a note to printable lines within the note's limits.

    Line breaks are the one piece of layout a note keeps, because reflowing
    them away is what turns a snippet into a paragraph. The Lua half draws the
    lines as they are and breaks only what overruns the window.

    Everything else goes. A newline inside a virt_text chunk is not a line
    break to nvim, which draws it as ^@, so the breaks have to arrive as
    separate lines; an escape sequence would reach the human's terminal.
    """
    lines: list[str] = []
    for line in text.expandtabs(TAB_WIDTH).split("\n"):
        printable = "".join(ch if ch.isprintable() else " " for ch in line)
        stripped = printable.rstrip()
        # Leading and repeated blank lines would be spent on empty rows of
        # band, which cost the same as a row carrying text.
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
    #: Already resolved against the session root. Resolving once keeps the
    #: window between the check and nvim's open as small as it can be here.
    file: Path
    line: int = 1
    end_line: int | None = None
    text: str = ""


#: Frames a session may hold. At the cap a push is refused rather than the
#: bottom frame dropped, and the cap is what keeps notes from pushing the code
#: off the screen.
FRAME_LIMIT = 4
FRAME_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class Note:
    """One annotated location, as the session records it.

    The id is the frame's letter and a number the frame never reuses, so a
    mark that names the note it was asked on keeps naming it after later
    shows. The line is where nvim last reported the note, which follows the
    human's edits.
    """

    id: str
    file: str
    line: int
    end_line: int | None
    text: str


@dataclass
class Frame:
    """One set of locations with its notes.

    Frames stack: a review is a frame, a question asked in the middle of it is
    a digression pushed above it and popped when answered. The letter is
    taken at push time and held until the frame is popped, so popping a frame
    below never renames the ones above.
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
    #: The key a client takes for itself over the admin socket, which reads
    #: outside the root. Never printed: `ls`, `new` and `box` all hand out the
    #: clamped one, so there is nowhere to copy this from into a box.
    host_key: str
    #: Where a relative path is taken from and what nvim runs in. The boundary
    #: for a clamped key, and only a base for the other.
    root: Root
    socket: Path
    #: Start nvim without the human's config. Their plugins run in this session
    #: too, and one that authenticates or installs on startup blocks it.
    clean: bool = False
    #: 'light' or 'dark', taken from the human's terminal. A headless nvim
    #: cannot detect it.
    background: str | None = None
    #: The environment nvim runs with: the shell that ran `showme new`, so the
    #: session sees the same PATH and display the human does. Kept for the
    #: respawn after `:q`.
    env: dict[str, str] | None = None
    #: What the session shows, bottom frame first, and what the human has
    #: handed back. nvim draws this; it does not own it.
    frames: list[Frame] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    #: Called when the record changes outside a tool call, so the broker can
    #: save it. Marks and positions can arrive from nvim on their own.
    on_change: Callable[[], None] | None = None
    editor: Editor = field(init=False, repr=False)
    #: Orders every exchange with nvim, including starting it. Two callers
    #: that find the session dead at the same moment would otherwise each
    #: spawn an nvim, and only one of them would be the session's.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Work started by a notification from nvim, which cannot be awaited on
    #: the read loop. Kept so it is not collected before it runs.
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
        """Return what `key` reaches here, or None if it is not one of ours.

        Both keys are compared without an early exit: the short id alone is
        guessable, and the secret is what authorizes. Encoded first, because
        `compare_digest` refuses a string with a character outside ASCII, and a
        key arrives as whatever a client put in its JSON.
        """
        offered = key.encode()
        if secrets.compare_digest(offered, self.key.encode()):
            return self.root
        if secrets.compare_digest(offered, self.host_key.encode()):
            return Anywhere(self.root.path)
        return None

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
    ) -> Session:
        # The short id is for humans to type; the secret is what authorizes.
        key = key or f"{sid}-{secrets.token_hex(12)}"
        return cls(
            sid=sid,
            clean=clean,
            background=background,
            env=env,
            key=key,
            host_key=host_key or f"{sid}-{secrets.token_hex(12)}",
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
            "host_key": self.host_key,
            "root": str(self.root.path),
            "clean": self.clean,
            "background": self.background,
            "env": self.env,
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
            # A file written before there were two keys gets a fresh one. No
            # client can be holding the key it never had, and a splice that
            # reconnects after a restart replays the one it was given.
            host_key=state.get("host_key"),
        )
        session.frames = [Frame.restore(frame) for frame in state.get("frames", [])]
        session.marks = list(state.get("marks", []))
        return session

    @property
    def notes(self) -> list[Note]:
        """Every live note, bottom frame first."""
        return [note for frame in self.frames for note in frame.notes]

    async def _setup(self, frames: list[dict[str, Any]] | None) -> None:
        """Install the session's Lua in the connected nvim.

        With frames, nvim starts drawing them; without, it keeps whatever it
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
            SESSION_INIT, options, *([frames] if frames is not None else [])
        )

    def _frames_for_lua(self) -> list[dict[str, Any]]:
        return [frame.state() for frame in self.frames]

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
            await self._setup(self._frames_for_lua())
            # After a restart this is what puts the review back in front of
            # the human. Nobody is attached yet, so there is no view to preserve.
            if self.frames:
                await self._draw(focus=True, restoring=True)
        elif outcome == "adopted":
            # nvim has been on its own: it may hold moved notes and questions
            # asked while no broker was listening.
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
                # Every frame's files, not just the top one's: nothing is
                # loaded yet, and a frame that draws into no buffer is gone
                # from the screen while still in the record.
                "open_all": restoring,
                # A question is marked until an agent acknowledges it, so nvim
                # has to be told which are still waiting.
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

        A session can die between two calls, and the failure surfaces only when
        the next one is sent. The caller holds the lock: an operation that
        changes the record has to hold it from before the change until after
        the answer is built, or a second caller changes the record underneath
        it and both are answered with the same result.
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

        Held under the lock from the change to the answer. Two agents can
        share a session -- two boxes in one tree do, deliberately -- and
        changing the frames before taking it left both calls reporting the
        same notes, with one caller's gone.
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
            # Agent edits reach disk without passing through the broker, so
            # refresh before showing anything.
            await self.rpc.request("nvim_command", "checktime")
            return await self._draw(focus=focus, open_files=frame != "pop")

        result = await self._exchange(run)
        # Nothing on screen and something worth seeing: the show already told
        # us how many UIs nvim has, so this costs no extra round trip.
        result["opened_ui"] = not result.get("uis") and self.editor.open_window()
        result["frame"] = top.letter if top else None
        result["popped"] = popped
        result["ids"] = (
            [note.id for note in top.notes] if top and frame != "pop" else []
        )
        # The stack as this call left it. Read after the lock, it would be
        # whatever the next caller has done since.
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

    async def _tell_human(self, message: str) -> None:
        assert self.rpc is not None
        with contextlib.suppress(Exception):
            await self.rpc.lua("vim.notify(...)", f"showme: {message}")

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

    async def attached(self) -> bool:
        """Report whether a UI is on this session's nvim.

        A dead nvim has no UI. Asking must not start one: `showme ls` asks
        about every session, and listing them is not a reason to bring them
        back.
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
