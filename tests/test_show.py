# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from nvim_mcp import paths
from nvim_mcp.nvimrpc import NvimRPC

pytestmark = pytest.mark.nvim


async def show(wire: Wire, key: str, **arguments: object) -> dict:
    result = await wire.call("show", {"session": key, **arguments})
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def new_session(repo: Path) -> dict:
    reply = await admin({"cmd": "new", "root": str(repo)})
    assert reply["ok"], reply
    return reply


async def test_show_opens_a_tab_per_file_and_lists_positions(
    running_broker: Path, repo: Path
) -> None:
    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    payload = await show(
        wire,
        session["key"],
        locations=[
            {"file": "src/main.c", "line": 3, "end_line": 5, "text": "here"},
            {"file": "README.md", "line": 1},
        ],
        title="review",
    )
    assert sorted(Path(p).name for p in payload["opened"]) == ["README.md", "main.c"]
    assert payload["attached"] is False
    assert payload["refused"] == []

    nvim = await NvimRPC.connect(Path(session["socket"]))
    state = await nvim.lua("""
        return {
          tabs = vim.fn.tabpagenr('$'),
          title = vim.fn.getqflist({ title = 0 }).title,
          items = #vim.fn.getqflist(),
          switchbuf = vim.o.switchbuf,
          marks = vim.api.nvim_buf_get_extmarks(
            vim.fn.bufnr('src/main.c'),
            vim.api.nvim_create_namespace('nvim-mcp-show'), 0, -1, { details = true }),
        }
    """)
    assert state["tabs"] == 2
    assert state["title"] == "review"
    assert state["items"] == 2
    assert state["switchbuf"] == "usetab,newtab"
    details = [mark[3] for mark in state["marks"]]
    assert [d for d in details if d.get("hl_group") == "NvimMcpShow"], (
        "no range highlight"
    )
    notes = [d["virt_lines"] for d in details if "virt_lines" in d]
    # The trailing empty chunk stretches the note's background to the end of the
    # screen line, so it reads as a band instead of a run of coloured text.
    assert notes == [[[["  A1  here", "NvimMcpNote"], ["", "NvimMcpNote"]]]]
    await nvim.close()
    await wire.close()


async def test_a_backtick_in_a_filename_does_not_reach_a_shell(
    running_broker: Path, repo: Path
) -> None:
    # The session runs with the root as its cwd, so a shell would drop the
    # marker there. An agent can create this file: it has the root read-write.
    marker = repo / "pwned"
    hostile = repo / "a`touch pwned`.c"
    hostile.write_text("int x;\n")

    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    payload = await show(wire, session["key"], locations=[{"file": hostile.name}])

    assert not marker.exists(), "opening the file ran a shell"
    assert payload["opened"] == [str(hostile)]

    nvim = await NvimRPC.connect(Path(session["socket"]))
    names = await nvim.lua(
        "return vim.tbl_map(vim.api.nvim_buf_get_name, vim.api.nvim_list_bufs())"
    )
    assert str(hostile) in names
    await nvim.close()
    await wire.close()


async def test_a_path_outside_the_root_is_refused_without_spoiling_the_call(
    running_broker: Path, repo: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "id_ed25519"
    secret.write_text("PRIVATE KEY\n")

    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    payload = await show(
        wire,
        session["key"],
        locations=[{"file": str(secret)}, {"file": "src/main.c", "line": 2}],
    )

    assert [Path(p).name for p in payload["opened"]] == ["main.c"]
    assert payload["refused"] == [
        {"file": str(secret), "reason": "outside the session root"}
    ]

    nvim = await NvimRPC.connect(Path(session["socket"]))
    names = await nvim.lua(
        "return vim.tbl_map(vim.api.nvim_buf_get_name, vim.api.nvim_list_bufs())"
    )
    assert not any("id_ed25519" in name for name in names)
    await nvim.close()
    await wire.close()


async def test_an_unknown_session_key_is_rejected(
    running_broker: Path, repo: Path
) -> None:
    await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    result = await wire.call(
        "show", {"session": "1", "locations": [{"file": "README.md"}]}
    )
    assert result["isError"] is True
    await wire.close()


async def test_a_range_past_the_end_of_the_file_still_highlights(
    running_broker: Path, repo: Path
) -> None:
    lines = len((repo / "src" / "main.c").read_text().splitlines())
    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    await show(
        wire,
        session["key"],
        locations=[{"file": "src/main.c", "line": lines - 1, "end_line": lines + 40}],
    )

    nvim = await NvimRPC.connect(Path(session["socket"]))
    mark = await nvim.lua("""
        return vim.api.nvim_buf_get_extmarks(
          vim.fn.bufnr('src/main.c'),
          vim.api.nvim_create_namespace('nvim-mcp-show'), 0, -1, { details = true })[1]
    """)
    row, details = mark[1], mark[3]
    assert row == lines - 2
    assert details["end_row"] == lines - 1, (
        "the highlight collapsed instead of truncating"
    )
    await nvim.close()
    await wire.close()
