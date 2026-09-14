# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from showme import paths
from showme.nvimrpc import NvimRPC

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
            vim.api.nvim_create_namespace('showme-show'), 0, -1, { details = true }),
        }
    """)
    assert state["tabs"] == 2
    assert state["title"] == "review"
    assert state["items"] == 2
    assert state["switchbuf"] == "usetab,newtab"
    details = [mark[3] for mark in state["marks"]]
    assert [d for d in details if d.get("hl_group") == "ShowMeShow"], (
        "no range highlight"
    )
    notes = [d["virt_lines"] for d in details if "virt_lines" in d]
    # The trailing empty chunk stretches the note's background to the end of the
    # screen line, so it reads as a band instead of a run of coloured text.
    assert notes == [[[["  A1  here", "ShowMeNote"], ["", "ShowMeNote"]]]]
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
          vim.api.nvim_create_namespace('showme-show'), 0, -1, { details = true })[1]
    """)
    row, details = mark[1], mark[3]
    assert row == lines - 2
    assert details["end_row"] == lines - 1, (
        "the highlight collapsed instead of truncating"
    )
    await nvim.close()
    await wire.close()


async def test_a_long_note_folds_to_the_window_width(
    running_broker: Path, repo: Path
) -> None:
    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    # Prose as an agent writes it: hard-wrapped, and longer than one screen
    # line. The note keeps its words and the editor decides where they break.
    paragraph = "\n".join([" ".join(["word"] * 8)] * 40)
    await show(
        wire, session["key"], locations=[{"file": "src/main.c", "text": paragraph}]
    )

    nvim = await NvimRPC.connect(Path(session["socket"]))
    state = await nvim.lua("""
        local buf = vim.fn.bufnr('src/main.c')
        local out = {}
        for _, mark in ipairs(vim.api.nvim_buf_get_extmarks(
            buf, vim.api.nvim_create_namespace('showme-show'), 0, -1,
            { details = true })) do
          for _, line in ipairs(mark[4].virt_lines or {}) do
            out[#out + 1] = line[1][1]
          end
        end
        return { lines = out, columns = vim.o.columns }
    """)
    lines = state["lines"]
    assert len(lines) > 1, lines
    assert max(len(line) for line in lines) <= state["columns"]
    assert lines[0].startswith("  A1  word")
    # Every word survives the fold, none run together across a source newline.
    assert " ".join(lines).split() == ["A1"] + ["word"] * 320
    await nvim.close()
    await wire.close()


async def test_a_long_note_is_clipped_in_the_quickfix_list(
    running_broker: Path, repo: Path
) -> None:
    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    await show(
        wire,
        session["key"],
        locations=[{"file": "src/main.c", "line": 3, "text": "word " * 300}],
    )

    nvim = await NvimRPC.connect(Path(session["socket"]))
    state = await nvim.lua("""
        vim.cmd('copen')
        local line = vim.api.nvim_buf_get_lines(0, 0, 1, true)[1]
        vim.cmd('cclose')
        return { line = line, columns = vim.o.columns }
    """)
    line = state["line"]
    # nvim draws 'src/main.c|3 col 1 note| ' ahead of the entry's own text, and
    # the whole row has to fit the screen line it gets.
    assert line.startswith("src/main.c|3 col 1 note| A1  word")
    assert len(line) <= state["columns"]
    assert line.endswith("...")
    await nvim.close()
    await wire.close()


async def test_the_keys_walk_the_notes_and_wrap_at_the_ends(
    running_broker: Path, repo: Path
) -> None:
    session = await new_session(repo)
    wire = await Wire.connect(paths.agent_socket())
    await show(
        wire,
        session["key"],
        locations=[
            {"file": "src/main.c", "line": 1},
            {"file": "src/main.c", "line": 3},
            {"file": "README.md", "line": 1},
        ],
    )

    nvim = await NvimRPC.connect(Path(session["socket"]))
    walk = """
        local keys = vim.api.nvim_replace_termcodes(..., true, false, true)
        vim.api.nvim_feedkeys(keys, 'x', false)
        return {
          idx = vim.fn.getqflist({ idx = 0 }).idx,
          file = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(0), ':t'),
        }
    """
    down, up = "<C-M-PageDown>", "<C-M-PageUp>"
    assert await nvim.lua(walk, down) == {"idx": 2, "file": "main.c"}
    # The walk crosses tabs rather than displacing the window it starts in.
    assert await nvim.lua(walk, down) == {"idx": 3, "file": "README.md"}
    assert await nvim.lua("return vim.fn.tabpagenr('$')") == 2
    # Past either end it comes round instead of reporting E553.
    assert await nvim.lua(walk, down) == {"idx": 1, "file": "main.c"}
    assert await nvim.lua(walk, up) == {"idx": 3, "file": "README.md"}
    await nvim.close()
    await wire.close()
