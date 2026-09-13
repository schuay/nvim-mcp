# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Start, adopt, and stop the nvim behind a session.

A running nvim need not be this broker's child: the broker that started it
may have exited or crashed since, and the human may still be attached to it.
Bringing a session up therefore tries its socket first and spawns only when
nobody answers. nvim is spawned in its own process group and with the
caller's environment, so it outlives the broker and finds the language
servers and display the human's shell would.

Stopping is deliberate. A broker going away detaches and leaves nvim running
for the next broker to adopt; only `nv kill` stops one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import Literal

from .nvimrpc import NvimError, NvimRPC

Outcome = Literal["alive", "adopted", "started", "absent"]


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
        await self._forget()
        if await self._adopt():
            return "adopted"
        if not spawn:
            return "absent"
        await self._spawn()
        return "started"

    async def _forget(self) -> None:
        if self.rpc is not None:
            await self.rpc.close()
            self.rpc = None
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
        await self._await_socket()
        self.rpc = await NvimRPC.connect(self.socket)

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
