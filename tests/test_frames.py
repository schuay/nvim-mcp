# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Frames: a stack of note sets, each with a letter, that both sides can name."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from nvim_mcp import paths
from nvim_mcp.clamp import Refused
from nvim_mcp.models import ShowResult
from nvim_mcp.nvimrpc import NvimRPC
from nvim_mcp.session import FRAME_LIMIT, Location, Session

pytestmark = pytest.mark.nvim

NOTE_NS = "vim.api.nvim_create_namespace('nvim-mcp-show')"


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
async def session(runtime: Path, repo: Path) -> AsyncIterator[Session]:
    session = Session.create("1", repo, clean=True)
    yield session
    await session.close()


@pytest.fixture
async def human(session: Session) -> AsyncIterator[NvimRPC]:
    await session.ensure()
    rpc = await NvimRPC.connect(session.socket)
    yield rpc
    await rpc.close()


def main_c(repo: Path, line: int, text: str) -> Location:
    return Location(repo / "src/main.c", line=line, text=text)


async def bands(human: NvimRPC) -> list[str]:
    """Every band line in main.c, in the order nvim will draw them."""
    marks = await human.lua(f"""
        local buf = vim.fn.bufnr('src/main.c')
        local out = {{}}
        for _, m in ipairs(vim.api.nvim_buf_get_extmarks(buf, {NOTE_NS}, 0, -1,
                                                         {{ details = true }})) do
          for _, line in ipairs(m[4].virt_lines or {{}}) do out[#out + 1] = line[1][1] end
        end
        return out
    """)
    return [band.strip() for band in marks]


async def quickfix(human: NvimRPC) -> tuple[str, list[str]]:
    state = await human.lua("""
        return { title = vim.fn.getqflist({ title = 0 }).title,
                 items = vim.tbl_map(function(i) return i.text end, vim.fn.getqflist()) }
    """)
    return state["title"], list(state["items"] or [])


async def test_the_first_show_opens_frame_a(session: Session, repo: Path) -> None:
    result = await session.show(
        [main_c(repo, 2, "one"), main_c(repo, 8, "two")], "review", True
    )
    assert (result["frame"], result["ids"]) == ("A", ["A1", "A2"])
    assert [f.letter for f in session.frames] == ["A"]


async def test_push_stacks_a_frame_and_every_frame_renders(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 2, "review note")], "review", True)
    result = await session.show(
        [main_c(repo, 2, "the answer"), main_c(repo, 6, "aside")],
        "question",
        True,
        frame="push",
    )
    assert (result["frame"], result["ids"]) == ("B", ["B1", "B2"])
    # Bottom frame first at the shared line, so the answer reads under the
    # note it answers.
    assert await bands(human) == ["A1  review note", "B1  the answer", "B2  aside"]
    assert await quickfix(human) == ("question", ["B1  the answer", "B2  aside"])


async def test_replace_keeps_the_letter_and_never_reuses_a_number(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 2, "first")], "t", True)
    await session.show([main_c(repo, 3, "aside")], "t", True, frame="push")
    result = await session.show([main_c(repo, 4, "second")], "t", True)
    assert (result["frame"], result["ids"]) == ("B", ["B2"])
    assert await bands(human) == ["A1  first", "B2  second"]


async def test_pop_drops_the_top_frame_and_its_notes(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 2, "review")], "review", True)
    await session.show([main_c(repo, 5, "aside")], "aside", True, frame="push")
    result = await session.show([], "ignored", True, frame="pop")
    assert (result["frame"], result["popped"], result["ids"]) == ("A", "B", [])
    assert await bands(human) == ["A1  review"]
    assert await quickfix(human) == ("review", ["A1  review"])

    result = await session.show([], "ignored", True, frame="pop")
    # Emptying the stack once looked the same as doing nothing at all.
    assert (result["frame"], result["popped"]) == (None, "A")
    assert await bands(human) == []
    assert await quickfix(human) == ("", [])
    with pytest.raises(Refused, match="no frame to pop"):
        await session.show([], "ignored", True, frame="pop")


async def test_the_stack_is_capped_and_letters_are_the_lowest_free(
    session: Session, repo: Path
) -> None:
    for _ in range(FRAME_LIMIT):
        await session.show([main_c(repo, 2, "n")], "t", True, frame="push")
    assert [f.letter for f in session.frames] == ["A", "B", "C", "D"]
    with pytest.raises(Refused, match="pop D first"):
        await session.show([main_c(repo, 2, "n")], "t", True, frame="push")

    session._pop("B")
    result = await session.show([main_c(repo, 2, "n")], "t", True, frame="push")
    assert result["frame"] == "B"
    assert [f.letter for f in session.frames] == ["A", "C", "D", "B"]


async def test_a_question_names_the_topmost_note_under_it(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show(
        [Location(repo / "src/main.c", line=2, end_line=6, text="range")], "t", True
    )
    await session.show([main_c(repo, 4, "reply")], "t", True, frame="push")
    await human.request("nvim_command", "4Ask on the reply")
    await human.request("nvim_command", "5Ask on the range")
    await human.request("nvim_command", "9Ask on nothing")
    await until(lambda: len(session.marks) == 3)
    assert [m.get("note_id") for m in session.marks] == ["B1", "A1", None]


async def test_the_human_pops_from_the_editor(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    saves: list[int] = []
    session.on_change = lambda: saves.append(1)
    await session.show([main_c(repo, 2, "review")], "review", True)
    await session.show([main_c(repo, 4, "aside")], "aside", True, frame="push")
    await session.show([main_c(repo, 6, "deeper")], "deeper", True, frame="push")

    await human.request("nvim_command", "AgentDrop B")
    await until(lambda: [f.letter for f in session.frames] == ["A", "C"])
    await until(lambda: saves == [1])
    assert await bands(human) == ["A1  review", "C1  deeper"]

    await human.request("nvim_command", "AgentPop")
    await until(lambda: [f.letter for f in session.frames] == ["A"])
    assert await bands(human) == ["A1  review"]
    assert await quickfix(human) == ("review", ["A1  review"])

    await human.request("nvim_command", "AgentDrop Z")
    await asyncio.sleep(0.1)
    assert [f.letter for f in session.frames] == ["A"], "a bad drop changed the stack"
    assert saves == [1, 1]


async def test_edits_move_notes_in_every_frame(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 3, "a")], "t", True)
    await session.show([main_c(repo, 5, "b")], "t", True, frame="push")
    await human.lua("vim.api.nvim_buf_set_lines(0, 0, 0, false, { '// above' })")
    await session.read("tabs", {})
    assert [(n.id, n.line) for n in session.notes] == [("A1", 4), ("B1", 6)]


async def test_frames_survive_restart_and_the_record(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 2, "review")], "review", True)
    await session.show([main_c(repo, 4, "aside")], "aside", True, frame="push")
    restored = Session.restore(session.state())
    assert [(f.letter, f.title, f.next_number) for f in restored.frames] == [
        ("A", "review", 2),
        ("B", "aside", 2),
    ]

    process = session.editor.process
    assert process is not None
    await human.notify("nvim_command", "qall!")
    await process.wait()
    await session.ensure()
    again = await NvimRPC.connect(session.socket)
    assert await bands(again) == ["A1  review", "B1  aside"]
    assert await quickfix(again) == ("aside", ["B1  aside"])
    await again.close()


async def test_the_wire_carries_frames(running_broker: Path, repo: Path) -> None:
    created = await admin({"cmd": "new", "root": str(repo), "clean": True})
    wire = await Wire.connect(paths.agent_socket())
    key = created["key"]

    result = await wire.call("show", {"session": key, "frame": "push"})
    assert result["isError"] is True
    assert "locations" in result["content"][0]["text"]

    shown = await wire.call(
        "show",
        {"session": key, "title": "review", "locations": [{"file": "src/main.c"}]},
    )
    payload = ShowResult.model_validate(shown["structuredContent"])
    assert (payload.frame, payload.ids) == ("A", ["A1"])
    assert [(f.letter, f.title, f.notes) for f in payload.frames] == [
        ("A", "review", 1)
    ]

    popped = await wire.call("show", {"session": key, "frame": "pop"})
    payload = ShowResult.model_validate(popped["structuredContent"])
    assert (payload.frame, payload.ids, payload.frames) == (None, [], [])
    assert payload.popped == "A"
    body = json.loads(popped["content"][0]["text"])
    assert "frame" not in body
    assert body["popped"] == "A"

    empty = await wire.call("show", {"session": key, "frame": "pop"})
    assert empty["isError"] is True
    assert "no frame to pop" in empty["content"][0]["text"]
    await wire.close()


async def test_a_frame_change_keeps_where_the_human_moved_a_note(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 3, "review")], "review", True)
    await human.lua("""
        vim.cmd('edit src/main.c')
        vim.api.nvim_buf_set_lines(0, 0, 0, false, { 'inserted' })
    """)

    # A push, a pop and a replace all redraw. Each used to clear the anchors
    # before anything read them, putting the note back on the line the broker
    # last saw it on and the explanation against the wrong code.
    aside = Location(repo / "README.md", line=1, text="aside")
    await session.show([aside], "aside", False, frame="push")
    assert session.frames[0].notes[0].line == 4
    assert await bands(human) == ["A1  review"]

    await session.show([], "ignored", False, frame="pop")
    assert session.frames[0].notes[0].line == 4

    await human.lua("vim.api.nvim_buf_set_lines(0, 0, 0, false, { 'another' })")
    await session.show([main_c(repo, 3, "second look")], "again", False)
    # A replace gives the top frame new notes, so the old anchor goes with the
    # note it belonged to.
    assert session.frames[0].notes[0].line == 3


async def test_a_restart_opens_every_frames_files(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([main_c(repo, 2, "review")], "review", False)
    aside = Location(repo / "README.md", line=1, text="aside")
    await session.show([aside], "aside", False, frame="push")

    process = session.editor.process
    assert process is not None
    await human.notify("nvim_command", "qall!")
    await process.wait()
    await session.ensure()

    again = await NvimRPC.connect(session.socket)
    loaded = await again.lua("""
        return {
          main = vim.fn.bufloaded(vim.fn.bufnr('src/main.c')) == 1,
          readme = vim.fn.bufloaded(vim.fn.bufnr('README.md')) == 1,
        }
    """)
    # A restart loads nothing of its own, so the base frame used to come back
    # in the record and nowhere else: no buffer, no band, no code to read.
    assert loaded == {"main": True, "readme": True}
    assert await bands(again) == ["A1  review"]
    await again.close()
