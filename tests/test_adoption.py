# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""A broker comes and goes; the session's nvim, and the human in it, stay."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker
from mcpwire import Wire

from nvim_mcp import paths, store
from nvim_mcp.nvimrpc import NvimRPC
from nvim_mcp.session import Location, Session

pytestmark = pytest.mark.nvim


async def call(wire: Wire, tool: str, **arguments: object) -> dict:
    result = await wire.call(tool, arguments)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def new_shown_session(repo: Path) -> dict:
    session = await admin({"cmd": "new", "root": str(repo), "clean": True})
    wire = await Wire.connect(paths.agent_socket())
    await call(
        wire,
        "show",
        session=session["key"],
        title="review",
        locations=[{"file": "src/main.c", "line": 3, "text": "note"}],
    )
    await wire.close()
    return session


async def pid_of(socket: str) -> int:
    nvim = await NvimRPC.connect(Path(socket))
    try:
        return await nvim.lua("return vim.fn.getpid()")
    finally:
        await nvim.close()


async def test_a_stopped_broker_leaves_nvim_and_the_next_one_adopts_it(
    runtime: Path, repo: Path
) -> None:
    stop, task = await start_broker()
    session = await new_shown_session(repo)
    pid = await pid_of(session["socket"])

    # An unsaved edit: the one thing a respawn could never bring back.
    human = await NvimRPC.connect(Path(session["socket"]))
    await human.lua("vim.api.nvim_buf_set_lines(0, 0, 1, false, { '// unsaved' })")
    await stop_broker(stop, task)
    assert await human.lua("return vim.bo.modified") is True, "the broker killed it"

    stop, task = await start_broker()
    try:
        assert await pid_of(session["socket"]) == pid
        wire = await Wire.connect(paths.agent_socket())
        payload = await call(
            wire, "read", session=session["key"], what="range", file="src/main.c"
        )
        assert payload["range"]["modified"] is True
        assert payload["range"]["text"].startswith("// unsaved")
        assert payload["marks_pending"] == 0
        await wire.close()
    finally:
        await human.close()
        await stop_broker(stop, task)


async def test_a_crashed_broker_is_replaced_without_losing_the_editor(
    runtime: Path, repo: Path
) -> None:
    # A real process, so it can be killed the way a crash kills it, with no
    # chance to hand anything over.
    broker = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nvim_mcp.broker", env=dict(os.environ)
    )
    for _ in range(500):
        if paths.agent_socket().exists() and paths.admin_socket().exists():
            break
        await asyncio.sleep(0.01)
    session = await new_shown_session(repo)
    pid = await pid_of(session["socket"])

    human = await NvimRPC.connect(Path(session["socket"]))
    # Asked while the broker is dead: nvim holds it for the next one.
    broker.send_signal(signal.SIGKILL)
    await broker.wait()
    await human.request("nvim_command", "3Ask anyone there?")
    await human.lua("vim.api.nvim_buf_set_lines(0, 0, 0, false, { '// above' })")

    stop, task = await start_broker()
    try:
        assert await pid_of(session["socket"]) == pid
        wire = await Wire.connect(paths.agent_socket())
        payload = await call(wire, "read", session=session["key"], what="marks")
        assert [m["note"] for m in payload["marks"]] == ["anyone there?"]
        listing = (await admin({"cmd": "ls"}))["sessions"]
        assert [s["key"] for s in listing] == [session["key"]]
        await wire.close()
        # The new broker's record has caught up with the edit made meanwhile.
        state = json.loads((paths.state_dir() / "sessions.json").read_text())
        assert state["sessions"][0]["frames"][0]["notes"][0]["line"] == 4
    finally:
        await human.close()
        await stop_broker(stop, task)


async def test_attach_starts_a_session_whose_nvim_is_gone(
    running_broker: Path, repo: Path
) -> None:
    session = await new_shown_session(repo)
    human = await NvimRPC.connect(Path(session["socket"]))
    await human.notify("nvim_command", "qall!")
    await human.close()
    for _ in range(200):
        if not Path(session["socket"]).exists():
            break
        await asyncio.sleep(0.01)
    listing = (await admin({"cmd": "ls"}))["sessions"]
    assert listing[0]["attached"] is False

    reply = await admin({"cmd": "attach", "id": session["id"]})
    assert reply["ok"], reply
    assert Path(reply["socket"]).exists()
    assert await pid_of(reply["socket"]) > 0
    assert (await admin({"cmd": "attach", "id": "9"}))["ok"] is False


async def test_nvim_runs_with_the_callers_environment(
    runtime: Path, repo: Path
) -> None:
    env = dict(os.environ, NVIM_MCP_PROBE="from the shell")
    session = Session.create("1", repo, clean=True, env=env)
    await session.ensure()
    assert await session.rpc.lua("return vim.env.NVIM_MCP_PROBE") == "from the shell"

    # And again after :q, which respawns from the record rather than from
    # whatever the broker happens to have.
    human = await NvimRPC.connect(session.socket)
    await human.notify("nvim_command", "qall!")
    await human.close()
    process = session.editor.process
    assert process is not None
    await process.wait()
    restored = Session.restore(session.state())
    await restored.ensure()
    assert await restored.rpc.lua("return vim.env.NVIM_MCP_PROBE") == "from the shell"
    await restored.close()


async def test_stopping_a_session_ends_an_adopted_nvim(
    runtime: Path, repo: Path
) -> None:
    first = Session.create("1", repo, clean=True)
    await first.show([Location(repo / "src/main.c", line=3, text="n")], "t", True)
    pid = first.editor.pid
    assert pid is not None
    await first.detach()

    second = Session.restore(first.state())
    await second.ensure(spawn=False)
    assert second.alive
    assert second.editor.pid == pid
    assert second.editor.process is None
    await second.close()
    for _ in range(300):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the adopted nvim is still running")
    assert not first.socket.exists()


async def test_a_session_with_no_nvim_is_kept_until_needed(
    runtime: Path, repo: Path
) -> None:
    session = Session.create("1", repo, clean=True)
    await session.ensure(spawn=False)
    assert not session.alive
    assert not session.socket.exists()
    await session.read("tabs", {})
    assert session.alive
    await session.close()


async def test_a_restored_session_whose_nvim_is_gone_waits_for_a_caller(
    runtime: Path, repo: Path
) -> None:
    gone = Session.create("1", repo, clean=True)
    store.save([gone.state()])

    stop, task = await start_broker()
    try:
        listing = (await admin({"cmd": "ls"}))["sessions"]
        assert [s["attached"] for s in listing] == [False]
        assert not gone.socket.exists(), "restore started an nvim nobody asked for"

        wire = await Wire.connect(paths.agent_socket())
        await call(wire, "read", session=gone.key, what="tabs")
        await wire.close()
        assert gone.socket.exists()
    finally:
        await stop_broker(stop, task)
