# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from nvim_mcp import broker, paths


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
    monkeypatch.setenv("NVIM_MCP_AGENT_DIR", str(base / "agent"))
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def running_broker(runtime: Path) -> AsyncIterator[Path]:
    stop = asyncio.Event()
    task = asyncio.create_task(broker.serve(stop))
    for _ in range(200):
        if paths.agent_socket().exists():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        pytest.fail("broker did not start")
    yield runtime
    # Stop rather than cancel: shutting the sessions down runs inside the task.
    stop.set()
    await asyncio.wait_for(task, 30)


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
