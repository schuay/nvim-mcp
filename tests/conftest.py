# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from showme import broker, paths


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text("int main(void) { return 0; }\n" * 20)
    (root / "README.md").write_text("hello\n")
    return root


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the broker at private directories with short paths.

    A UNIX socket path is capped near 108 bytes, which a pytest tmp_path plus a
    session socket name can exceed.
    """
    base = Path(tempfile.mkdtemp(prefix="nvmcp-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "state"))
    monkeypatch.setenv("SHOWME_AGENT_DIR", str(base / "agent"))
    yield base
    # A stopped broker leaves its nvims running on purpose. Nothing adopts
    # them after the test, so end them by the socket path only they listen on.
    subprocess.run(["pkill", "-f", f"^nvim .*--listen {base}/"], check=False)
    shutil.rmtree(base, ignore_errors=True)


async def start_broker() -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    task = asyncio.create_task(broker.serve(stop))
    # Ready means answering, not a socket file: a broker that was killed
    # leaves its files behind, and the next one unlinks them only as it binds.
    for _ in range(500):
        try:
            if (await admin({"cmd": "ping"}))["ok"]:
                return stop, task
        except OSError:
            pass
        await asyncio.sleep(0.01)
    task.cancel()
    pytest.fail("broker did not start")


async def stop_broker(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.wait_for(task, 30)


@pytest.fixture
async def running_broker(runtime: Path) -> AsyncIterator[Path]:
    stop, task = await start_broker()
    yield runtime
    # Stop rather than cancel: shutting the sessions down runs inside the task.
    await stop_broker(stop, task)


async def admin(request: dict) -> dict:
    """Send one admin command.

    Async because the broker runs as a task in the test's own event loop: a
    blocking socket call here would deadlock against it.
    """
    reader, writer = await asyncio.open_unix_connection(str(paths.admin_socket()))
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 30)
    writer.close()
    return json.loads(line)
