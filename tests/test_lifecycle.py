# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import fcntl
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker

from showme import paths
from showme.broker import Broker
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


async def test_a_launcher_may_root_a_session_in_a_plain_directory(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory with no repository above it is a root like any other.

    The launcher hands the agent that tree either way, so the shape the broker
    must not turn down is the one a human keeps a couple of config files in.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    loose = tmp_path / "notes"
    loose.mkdir()
    broker = Broker()

    session, created = await broker.ensure_session(str(loose), clean=True, spawn=False)
    try:
        assert created is True
        assert session.root.path == loose
        # And a second launcher in the same tree joins it.
        again, created = await broker.ensure_session(str(loose), spawn=False)
        assert created is False
        assert again is session
    finally:
        await session.close()


async def test_new_and_ensure_meet_in_the_same_tree(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`showme new` and a launcher in that root end up on one session."""
    monkeypatch.setenv("HOME", str(tmp_path))
    loose = tmp_path / "notes"
    loose.mkdir()
    broker = Broker()

    session = await broker.new_session(str(loose), clean=True)
    try:
        found, created = await broker.ensure_session(str(loose), spawn=False)
        assert created is False
        assert found is session
    finally:
        await session.close()
