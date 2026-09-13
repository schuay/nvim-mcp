# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Own one headless nvim and apply agent requests to it.

The session outlives both the terminal a human attaches and the agent that
writes to it. Its id is the capability: it is created on the host with a root
the human chooses, and holding the id is what authorizes a client to use it.

The broker holds the durable copy of what a session shows and what the human
has handed back, because nvim loses extmarks when a buffer unloads and loses
everything when someone types `:q`. nvim keeps a copy of its own so it can
redraw notes when a buffer is read again without asking the broker.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .clamp import Root
from .nvimrpc import NvimGone, NvimRPC
from .paths import nvim_socket

#: A note is a label, not a document. Longer than this and it buries the code
#: it points at, however the editor folds it.
NOTE_LIMIT = 400

#: The Lua half of the session, run once per nvim. It is shipped beside this
#: module rather than embedded in it so it reads as Lua.
SESSION_INIT = resources.files(__package__).joinpath("session.lua").read_text()

SHOW = "return NvimMcp.show(...)"
READ = "return NvimMcp.read(...)"
POSITIONS = "return NvimMcp.positions()"


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
    #: What the session currently shows, and what the human has handed back.
    #: Held here because nvim loses both when someone types `:q`.
    notes: list[dict[str, Any]] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    title: str = "agent"
    process: asyncio.subprocess.Process | None = None
    rpc: NvimRPC | None = None
    #: Orders every exchange with nvim, including starting it. Two callers
    #: that find the session dead at the same moment would otherwise each
    #: spawn an nvim, and only one of them would be the session's.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def create(
        cls,
        sid: str,
        root: str | Path,
        clean: bool = False,
        background: str | None = None,
        key: str | None = None,
    ) -> Session:
        # The short id is for humans to type; the secret is what authorizes.
        key = key or f"{sid}-{secrets.token_hex(12)}"
        return cls(
            sid=sid,
            clean=clean,
            background=background,
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
            "notes": self.notes,
            "marks": self.marks,
            "title": self.title,
        }

    @classmethod
    def restore(cls, state: dict[str, Any]) -> Session:
        session = cls.create(
            state["sid"],
            state["root"],
            clean=bool(state.get("clean")),
            background=state.get("background"),
            key=state["key"],
        )
        session.notes = list(state.get("notes", []))
        session.marks = list(state.get("marks", []))
        session.title = state.get("title", "agent")
        return session

    async def _start(self) -> None:
        self.socket.unlink(missing_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            "nvim",
            *(["--clean"] if self.clean else []),
            "--headless",
            "--listen",
            str(self.socket),
            cwd=str(self.root.path),
        )
        await self._await_socket()
        self.rpc = await NvimRPC.connect(self.socket)
        # Hand nvim the notes it should be drawing. After a restart this is what
        # puts the review back in front of the human.
        await self.rpc.lua(SESSION_INIT, self.background, self.notes)
        if self.notes:
            await self.rpc.lua("NvimMcp.render_all()")
            await self._requeue()

    async def _requeue(self) -> None:
        """Rebuild the quickfix list from the notes held here."""
        assert self.rpc is not None
        await self.rpc.lua(
            """
            local title = ...
            local items = {}
            for _, note in ipairs(NvimMcp.notes) do
              local buf = vim.fn.bufadd(note.file)
              vim.fn.bufload(buf)
              vim.bo[buf].buflisted = true
              items[#items + 1] = { bufnr = buf, lnum = note.line, col = 1,
                                    type = 'N', text = note.text or '' }
            end
            vim.fn.setqflist(items, 'r')
            vim.fn.setqflist({}, 'a', { title = title })
            """,
            self.title,
        )

    async def _await_socket(self, timeout: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.socket.exists():
                return
            # A configuration error kills nvim in milliseconds. Without this the
            # failure is reported as a timeout, naming the wrong cause.
            if self.process is not None and self.process.returncode is not None:
                raise RuntimeError(f"nvim exited with status {self.process.returncode}")
            await asyncio.sleep(0.02)
        raise RuntimeError(f"nvim did not listen on {self.socket} within {timeout}s")

    @property
    def alive(self) -> bool:
        # The read loop notices the socket close as soon as nvim exits, while
        # the process return code lands a moment later.
        return (
            self.process is not None
            and self.process.returncode is None
            and self.rpc is not None
            and not self.rpc.closed
        )

    async def ensure(self) -> None:
        """Bring the session's nvim back if it is gone.

        `:q` in an attached UI kills the server outright, and the human has no
        reason to know that ends the review.
        """
        async with self._lock:
            await self._ensure()

    async def _ensure(self) -> None:
        if self.alive:
            return
        if self.rpc is not None:
            await self.rpc.close()
            self.rpc = None
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await self._start()

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
        payload = [
            {
                "id": index,
                "file": str(loc.file),
                "line": loc.line,
                "end_line": loc.end_line,
                "text": one_line(loc.text),
            }
            for index, loc in enumerate(locations, start=1)
        ]

        async def run() -> Any:
            assert self.rpc is not None
            # Agent edits reach disk without passing through the broker, so
            # refresh before showing anything.
            await self.rpc.request("nvim_command", "checktime")
            return await self.rpc.lua(
                SHOW,
                payload,
                {"title": title, "focus": focus, "root": str(self.root.path)},
            )

        result = await self._attempt(run)
        self._absorb(result)
        return result

    async def read(self, what: str, options: dict[str, Any]) -> dict[str, Any]:
        async def run() -> Any:
            assert self.rpc is not None
            await self.rpc.request("nvim_command", "checktime")
            return await self.rpc.lua(READ, what, options)

        result = await self._attempt(run)
        self._absorb(result)
        return result

    def _absorb(self, result: dict[str, Any]) -> None:
        """Take everything nvim reports back into the durable copy."""
        if isinstance(result.get("notes"), list):
            self.notes = result["notes"]
        for mark in result.get("marks") or []:
            mark["id"] = len(self.marks) + 1
            self.marks.append(mark)

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

    async def close(self) -> None:
        async with self._lock:
            if self.rpc is not None:
                with contextlib.suppress(Exception):
                    await self.rpc.notify("nvim_command", "qall!")
                await self.rpc.close()
                self.rpc = None
            if self.process is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.process.wait(), 3)
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
                self.process = None
            self.socket.unlink(missing_ok=True)
