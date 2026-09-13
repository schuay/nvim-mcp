# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nvim_mcp.broker import Broker
from nvim_mcp.nvimrpc import NvimRPC
from nvim_mcp.session import Session

pytestmark = pytest.mark.nvim


async def test_concurrent_creations_get_distinct_sessions(
    runtime: Path, repo: Path
) -> None:
    broker = Broker()
    created = await asyncio.gather(
        broker.new_session(str(repo), clean=True),
        broker.new_session(str(repo), clean=True),
    )
    try:
        assert sorted(s.sid for s in created) == ["1", "2"]
        assert set(broker.sessions) == {"1", "2"}
        assert all(s.alive for s in created)
    finally:
        await broker.close()


async def test_concurrent_calls_on_a_dead_session_start_one_nvim(
    runtime: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session.create("1", repo, clean=True)
    spawned: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def counting(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting)
    try:
        results = await asyncio.gather(
            session.read("tabs", {}), session.read("tabs", {})
        )
        assert all("buffers" in r for r in results)
        assert len(spawned) == 1, "each caller started its own nvim"
    finally:
        await session.close()
        for process in spawned:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_asking_whether_a_dead_session_is_attached_does_not_revive_it(
    runtime: Path, repo: Path
) -> None:
    session = Session.create("1", repo, clean=True)
    await session.ensure()
    first = session.process
    assert first is not None

    nvim = await NvimRPC.connect(session.socket)
    await nvim.notify("nvim_command", "qall!")
    await nvim.close()
    await first.wait()

    assert await session.attached() is False
    assert session.process is first, "listing the session started a new nvim"
    await session.close()
