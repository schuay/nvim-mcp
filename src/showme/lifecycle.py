# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Start, adopt, and stop the nvim behind a session.

A running nvim need not be this broker's child: the broker that started it
may have exited or crashed since, and the human may still be attached to it.
Bringing a session up therefore tries its socket first and spawns only when
nobody answers. nvim is spawned in its own process group and with the
caller's environment, so it outlives the broker and finds the language
servers and display the human's shell would.

Stopping is deliberate. A broker going away detaches and leaves nvim running
for the next broker to adopt; only `showme kill` stops one.

A session nobody is watching can also be given a window here. That is an
administrative act, so it is not in the tool list: an agent cannot ask for it,
it happens because the agent showed something and nothing was on screen. What
runs comes from the environment the human's shell handed over when the session
was made, never from anything a client sends. Where that environment can open
nothing -- over ssh -- the tool layer hands the agent the attach command to
pass on, which is the only way such a session reaches a screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Literal

from .nvimrpc import NvimError, NvimRPC

log = logging.getLogger(__name__)

Outcome = Literal["alive", "adopted", "started", "absent"]

#: Set this in the shell you start sessions from, to a terminal and whatever
#: it needs before a command -- `ghostty -e`, `kitty`, `alacritty -e` -- and a
#: window opens when an agent shows something to a session nobody is watching.
#: Unset means no window, which is also the answer over ssh.
TERMINAL = "SHOWME_TERMINAL"


class Editor:
    def __init__(
        self,
        socket: Path,
        root: Path,
        log: Path,
        clean: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        self.socket = socket
        self.root = root
        self.log = log
        self.clean = clean
        self.env = env
        self.rpc: NvimRPC | None = None
        #: Set only for an nvim this broker spawned. An adopted one is known
        #: by pid alone.
        self.process: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        #: Whether this nvim has been given a window already. One per nvim
        #: process: the human who closes it has quit nvim too, and the next
        #: show starts a fresh one that may open a window of its own.
        self.windowed = False

    @property
    def alive(self) -> bool:
        # Only nvim exiting closes its side of the connection, and the read
        # loop sees that at once, before any exit status could.
        return self.rpc is not None and not self.rpc.closed

    async def ensure(self, spawn: bool = True) -> Outcome:
        """Have a live connection, adopting or starting nvim as needed.

        Returns what it took, because the caller has to set up a fresh nvim
        and reconcile with an adopted one.
        """
        if self.alive:
            return "alive"
        # Drop the connection, not the editor. Losing contact says nothing
        # about nvim, which may be listening on its socket with edits in it
        # that nobody has saved; reconnecting is tried before replacing.
        await self._disconnect()
        if await self._adopt():
            return "adopted"
        await self._forget()
        if not spawn:
            return "absent"
        await self._spawn()
        return "started"

    async def _disconnect(self) -> None:
        if self.rpc is not None:
            await self.rpc.close()
            self.rpc = None

    async def _forget(self) -> None:
        """Give up on the nvim behind this session, having failed to reach it.

        Only after a reconnection has been tried: nvim that answers its socket
        is this session's nvim, however the last connection ended.
        """
        await self._disconnect()
        if self.process is not None:
            if self.process.returncode is None:
                # Its connection is gone, so it is exiting or wedged. Either
                # way it is not coming back as this session's nvim.
                self.process.kill()
                await self.process.wait()
            self.process = None
        self.pid = None

    async def _adopt(self) -> bool:
        if not self.socket.exists():
            return False
        try:
            rpc = await NvimRPC.connect(self.socket)
        except OSError:
            # A socket file with nobody behind it, left by a crash.
            self.socket.unlink(missing_ok=True)
            return False
        try:
            self.pid = await rpc.lua("return vim.fn.getpid()")
        except NvimError:
            await rpc.close()
            return False
        self.rpc = rpc
        return True

    async def _spawn(self) -> None:
        self.socket.unlink(missing_ok=True)
        with self.log.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                "nvim",
                *(["--clean"] if self.clean else []),
                "--headless",
                "--listen",
                str(self.socket),
                cwd=str(self.root),
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
        self.pid = self.process.pid
        self.windowed = False
        await self._await_socket()
        self.rpc = await NvimRPC.connect(self.socket)

    def open_window(self) -> bool:
        """Put this session on the human's screen, at most once per nvim.

        The terminal runs nvim directly rather than `showme`: the socket is
        known here, so there is nothing to ask the admin socket for, and which
        `showme` is on a path stops mattering.
        """
        env = self.env or {}
        command = shlex.split(env.get(TERMINAL, ""))
        if self.windowed or not command:
            return False
        if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            log.info("no display in the session environment; not opening a window")
            return False
        # Set whatever happens next. A terminal that fails to start would
        # otherwise be retried on every show.
        self.windowed = True
        try:
            subprocess.Popen(  # noqa: S603
                [*command, "nvim", "--remote-ui", "--server", str(self.socket)],
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.warning("could not open a window with %s: %s", command[0], e)
            return False
        return True

    async def _await_socket(self, timeout: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.socket.exists():
                return
            # A configuration error kills nvim in milliseconds. Without this the
            # failure is reported as a timeout, naming the wrong cause.
            if self.process is not None and self.process.returncode is not None:
                raise RuntimeError(
                    f"nvim exited with status {self.process.returncode}; see {self.log}"
                )
            await asyncio.sleep(0.02)
        raise RuntimeError(f"nvim did not listen on {self.socket} within {timeout}s")

    async def detach(self) -> None:
        """Drop the connection and leave nvim running."""
        if self.rpc is not None:
            await self.rpc.close()
            self.rpc = None
        self.process = None

    async def stop(self, grace: float = 3.0) -> None:
        """Ask nvim to exit, and make sure it does."""
        if self.rpc is not None:
            with contextlib.suppress(Exception):
                await self.rpc.notify("nvim_command", "qall!")
            await self.rpc.close()
            self.rpc = None
        if self.process is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), grace)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self.process = None
        elif self.pid is not None:
            await self._await_exit(self.pid, grace)
        self.pid = None
        self.socket.unlink(missing_ok=True)

    @staticmethod
    async def _await_exit(pid: int, grace: float) -> None:
        deadline = asyncio.get_running_loop().time() + grace
        while asyncio.get_running_loop().time() < deadline:
            if not _running(pid):
                return
            await asyncio.sleep(0.02)
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
