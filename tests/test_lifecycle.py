# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import fcntl
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker

from showme import broker as broker_module
from showme import paths
from showme.broker import Broker
from showme.clamp import Refused
from showme.nvimrpc import NvimRPC
from showme.session import Session

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
    first = session.editor.process
    assert first is not None

    nvim = await NvimRPC.connect(session.socket)
    await nvim.notify("nvim_command", "qall!")
    await nvim.close()
    await first.wait()

    assert await session.attached() is False
    assert session.editor.process is first, "listing the session started a new nvim"
    await session.close()


async def test_stop_hands_the_sessions_to_the_next_broker(
    runtime: Path, repo: Path
) -> None:
    """`showme restart-broker` is this, then a broker on the same sockets."""
    stop, task = await start_broker()
    created = await admin({"cmd": "new", "root": str(repo)})
    reply = await admin({"cmd": "stop"})
    assert reply["sessions"] == 1
    await asyncio.wait_for(task, 30)

    # The lock is what a restart waits on: the sockets go before it does, so a
    # broker started on their absence would find the lock held and exit.
    with paths.lock_path().open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)

    stop, task = await start_broker()
    try:
        # Same key, and the nvim the first broker started is still the one
        # behind it.
        sessions = (await admin({"cmd": "ls"}))["sessions"]
        assert [s["key"] for s in sessions] == [created["key"]]
    finally:
        await stop_broker(stop, task)


def test_a_launcher_cannot_root_a_session_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "src" / "project"
    (project / ".git").mkdir(parents=True)

    broker_module._guard_root(project)
    broker_module._guard_root(project / "deep" / "inside")

    # The home directory itself, and anything above it.
    with pytest.raises(Refused, match="too broad"):
        broker_module._guard_root(tmp_path)
    with pytest.raises(Refused, match="too broad"):
        broker_module._guard_root(tmp_path.parent)
    # A parent holding several projects, which is what `cl` in the wrong
    # terminal tab would otherwise hand over.
    with pytest.raises(Refused, match="no repository"):
        broker_module._guard_root(tmp_path / "src")


async def test_ensure_applies_the_guard_and_new_does_not(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    loose = tmp_path / "src"
    loose.mkdir()
    broker = Broker()
    with pytest.raises(Refused, match="no repository"):
        await broker.ensure_session(str(loose))

    # `showme new` is the human naming a root, and is left alone.
    session = await broker.new_session(str(loose), clean=True)
    try:
        assert session.root.path == loose
    finally:
        await session.close()
