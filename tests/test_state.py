# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker
from mcpwire import Wire

from nvim_mcp import paths
from nvim_mcp.nvimrpc import NvimRPC

pytestmark = pytest.mark.nvim


async def call(wire: Wire, tool: str, **arguments: object) -> dict:
    result = await wire.call(tool, arguments)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def review(wire: Wire, key: str) -> dict:
    return await call(
        wire,
        "show",
        session=key,
        title="review",
        locations=[
            {"file": "src/main.c", "line": 3, "end_line": 5, "text": "look here"}
        ],
    )


async def test_a_review_survives_a_broker_restart(runtime: Path, repo: Path) -> None:
    stop, task = await start_broker()
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])
    await wire.close()
    await stop_broker(stop, task)

    stop, task = await start_broker()
    try:
        listing = (await admin({"cmd": "ls"}))["sessions"]
        assert [s["key"] for s in listing] == [session["key"]], (
            "session was not restored"
        )

        # The same key still works, and the notes are back in the new nvim.
        wire = await Wire.connect(paths.agent_socket())
        payload = await call(wire, "read", session=session["key"], what="tabs")
        assert [Path(b["file"]).name for b in payload["buffers"]] == ["main.c"]
        await wire.close()

        nvim = await NvimRPC.connect(Path(listing[0]["socket"]))
        notes = await nvim.lua("return NvimMcp.notes")
        assert [note["text"] for note in notes] == ["look here"]
        await nvim.close()
    finally:
        await stop_broker(stop, task)


async def test_a_review_survives_the_human_quitting_nvim(
    running_broker: Path, repo: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])

    # `:q` in an attached UI kills the server outright.
    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.notify("nvim_command", "qall!")
    await nvim.close()

    payload = await review(wire, session["key"])
    assert [Path(p).name for p in payload["opened"]] == ["main.c"]
    await wire.close()


async def test_notes_come_back_when_a_buffer_is_reopened(
    running_broker: Path, repo: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    marks = await nvim.lua("""
        local function count()
          return #vim.api.nvim_buf_get_extmarks(vim.fn.bufnr('src/main.c'),
            vim.api.nvim_create_namespace('nvim-mcp-show'), 0, -1, {})
        end
        local before = count()
        vim.cmd('bdelete! ' .. vim.fn.bufnr('src/main.c'))
        vim.cmd('edit src/main.c')
        return { before = before, after = count() }
    """)
    assert marks["before"] > 0
    assert marks["after"] == marks["before"], "unloading the buffer lost the notes"
    await nvim.close()
    await wire.close()


async def test_ask_hands_a_range_to_the_agent(running_broker: Path, repo: Path) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.request("nvim_command", "edit src/main.c")
    await nvim.request("nvim_command", "3,5Ask why is this here?")
    await nvim.close()

    payload = await call(wire, "read", session=session["key"], what="marks")
    assert len(payload["marks"]) == 1
    mark = payload["marks"][0]
    assert mark["note"] == "why is this here?"
    assert (mark["line1"], mark["line2"]) == (3, 5)
    assert Path(mark["file"]).name == "main.c"
    # The note the question was asked on, so a reply threads onto it.
    assert mark["note_id"] == 1
    # Reading is not answering: the question stays pending until acknowledged,
    # so a client that reads and then dies does not lose it.
    again = await call(wire, "read", session=session["key"], what="marks")
    assert [m["id"] for m in again["marks"]] == [mark["id"]]
    assert again["marks_pending"] == 1

    answered = await call(
        wire, "read", session=session["key"], what="marks", ack=[mark["id"]]
    )
    assert answered["marks"] == []
    assert answered["marks_pending"] == 0
    await wire.close()


async def test_an_unanswered_question_survives_a_reconnect(
    running_broker: Path, repo: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.request("nvim_command", "edit src/main.c")
    await nvim.request("nvim_command", "2Ask look at this")
    await nvim.request("nvim_command", "3Ask and this")
    await nvim.close()

    first = await call(wire, "read", session=session["key"], what="marks")
    await call(
        wire,
        "read",
        session=session["key"],
        what="marks",
        ack=[first["marks"][0]["id"]],
    )
    await wire.close()

    # Every MCP client that reconnects starts a new session, so delivery cannot
    # be tracked per connection: the answered question must stay answered and
    # the other must come back.
    wire = await Wire.connect(paths.agent_socket())
    payload = await call(wire, "read", session=session["key"], what="marks")
    assert [m["note"] for m in payload["marks"]] == ["and this"]
    assert payload["marks_pending"] == 1
    await wire.close()


async def test_two_clients_each_see_every_mark(
    running_broker: Path, repo: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    first = await Wire.connect(paths.agent_socket())
    second = await Wire.connect(paths.agent_socket())
    await review(first, session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.request("nvim_command", "edit src/main.c")
    await nvim.request("nvim_command", "2Ask look at this")
    await nvim.close()

    mine = await call(first, "read", session=session["key"], what="marks")
    theirs = await call(second, "read", session=session["key"], what="marks")
    assert len(mine["marks"]) == 1
    assert len(theirs["marks"]) == 1, "the first reader consumed the other's mark"
    await first.close()
    await second.close()


async def test_range_reads_unsaved_text_and_refuses_outside_the_root(
    running_broker: Path, repo: Path, tmp_path: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    await review(wire, session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.lua("""
        local buf = vim.fn.bufnr('src/main.c')
        vim.api.nvim_buf_set_lines(buf, 0, 1, false, { 'int main(void) { return 42; }' })
    """)
    await nvim.close()

    payload = await call(
        wire,
        "read",
        session=session["key"],
        what="range",
        file="src/main.c",
        start_line=1,
        end_line=1,
    )
    assert payload["range"]["modified"] is True
    assert "42" in payload["range"]["text"], "read the disk instead of the buffer"

    outside = tmp_path / "secret"
    outside.write_text("token\n")
    result = await wire.call(
        "read", {"session": session["key"], "what": "range", "file": str(outside)}
    )
    assert result["isError"] is True
    await wire.close()
