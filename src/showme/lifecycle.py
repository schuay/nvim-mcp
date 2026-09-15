# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Start, adopt, and stop the nvim owned by a session.

Connect to an existing nvim before spawning one because it may outlive the
broker that started it. Spawn nvim in its own process group with the session's
saved environment so it can outlive this broker and reach the human's tools and
display. Broker shutdown detaches; only `showme kill` stops nvim.

Open a terminal window after an agent shows something to an unattended session.
Only the saved human environment supplies the terminal command. If that
environment has no display, the tool result tells the agent which attach
command to give the human.
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

#: Terminal command used to open a window for an unattended session.
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
        #: Set only for nvim processes spawned by this broker.
        self.process: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        #: Limit automatic windows to one per nvim process.
        self.windowed = False

    @property
    def alive(self) -> bool:
        # The connection closes as soon as nvim exits, before process status is useful.
        return self.rpc is not None and not self.rpc.closed

    async def ensure(self, spawn: bool = True) -> Outcome:
        """Have a live connection, adopting or starting nvim as needed.

        Return the outcome so callers can initialize a new nvim or reconcile
        an adopted one.
        """
        if self.alive:
            return "alive"
        # Reconnect before replacing nvim because it may still hold unsaved edits.
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

        Call only after reconnection fails. An nvim that answers the session
        socket still owns the session regardless of how the connection ended.
        """
        await self._disconnect()
        if self.process is not None:
            if self.process.returncode is None:
                # A spawned process that cannot reconnect is exiting or wedged.
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
            # Remove a stale socket left by a crash.
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

        Run nvim directly because the socket is already known. This avoids an
        admin-socket lookup and any dependency on which `showme` is on PATH.
        """
        env = self.env or {}
        command = shlex.split(env.get(TERMINAL, ""))
        if self.windowed or not command:
            return False
        if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            log.info("no display in the session environment; not opening a window")
            return False
        # Do not retry a failed terminal command on every show.
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
            # Report early nvim exit directly instead of misclassifying it as a timeout.
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
