# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""The session is the record; nvim reports positions and questions into it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from showme.nvimrpc import NvimRPC
from showme.session import MARK_TEXT_LIMIT, Location, Note, Session

pytestmark = pytest.mark.nvim

NOTE_NS = "vim.api.nvim_create_namespace('showme-show')"


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for something nvim sends on its own to arrive on the read loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
async def session(runtime: Path, repo: Path) -> AsyncIterator[Session]:
    session = Session.create("1", repo, clean=True)
    await session.show(
        [Location(repo / "src/main.c", line=3, end_line=5, text="note")],
        title="review",
        focus=True,
    )
    yield session
    await session.close()


@pytest.fixture
async def human(session: Session) -> AsyncIterator[NvimRPC]:
    """A second connection, standing in for the human's keystrokes."""
    rpc = await NvimRPC.connect(session.socket)
    yield rpc
    await rpc.close()


async def insert_above(human: NvimRPC, count: int = 1) -> None:
    lines = ["// inserted"] * count
    await human.lua("vim.api.nvim_buf_set_lines(0, 0, 0, false, ...)", lines)


async def decoration_rows(human: NvimRPC) -> list[int]:
    marks = await human.lua(
        f"return vim.api.nvim_buf_get_extmarks(0, {NOTE_NS}, 0, -1, {{}})"
    )
    return sorted(mark[1] for mark in marks)


async def test_edits_move_the_note_in_the_record(
    session: Session, human: NvimRPC
) -> None:
    await insert_above(human, 2)
    assert session.notes[0].line == 3, "nothing has been reported yet"
    await session.read("tabs", {})
    assert (session.notes[0].line, session.notes[0].end_line) == (5, 7)


async def test_a_redraw_lands_where_the_note_has_moved(
    session: Session, human: NvimRPC
) -> None:
    await insert_above(human)
    assert await decoration_rows(human) == [3, 3], "the anchor did not move"
    await human.lua("vim.api.nvim_exec_autocmds('BufWinEnter', { buffer = 0 })")
    assert await decoration_rows(human) == [3, 3], "the redraw used the stale line"


async def test_a_note_comes_back_on_the_moved_line_after_unload(
    session: Session, human: NvimRPC
) -> None:
    await insert_above(human)
    await human.lua("vim.cmd('write | bdelete | edit src/main.c')")
    assert await decoration_rows(human) == [3, 3]
    await session.read("tabs", {})
    assert session.notes[0].line == 4


async def test_positions_survive_the_human_quitting(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await insert_above(human)
    await human.lua("vim.cmd('write')")
    first = session.editor.process
    assert first is not None
    await human.notify("nvim_command", "qall!")
    await first.wait()
    # Handed over on the way out, without a tool call in between.
    assert session.notes[0].line == 4

    await session.ensure()
    again = await NvimRPC.connect(session.socket)
    assert await again.lua("return ShowMe.frames[1].notes[1].line") == 4
    assert await decoration_rows(again) == [3, 3]
    await again.close()


async def test_a_question_reaches_the_record_as_it_is_asked(
    session: Session, human: NvimRPC
) -> None:
    saves: list[int] = []
    session.on_change = lambda: saves.append(1)
    await human.request("nvim_command", "4Ask why?")
    await until(lambda: bool(session.marks))
    mark = session.marks[0]
    assert (mark["id"], mark["note"], mark["note_id"]) == (1, "why?", "A1")
    assert mark["text"] == "int main(void) { return 0; }"
    assert mark["truncated"] is False
    assert saves == [1], "the broker was not told to save"


async def test_a_question_survives_the_human_quitting_at_once(
    session: Session, human: NvimRPC
) -> None:
    first = session.editor.process
    assert first is not None
    await human.request("nvim_command", "3Ask gone?")
    await human.notify("nvim_command", "qall!")
    await first.wait()
    assert [m["note"] for m in session.marks] == ["gone?"]


async def test_a_question_asked_while_the_broker_is_away_waits_for_it(
    session: Session, human: NvimRPC
) -> None:
    await human.lua("ShowMe.chan = 9999")
    await human.request("nvim_command", "3Ask later")
    assert await human.lua("return #ShowMe.pending") == 1
    assert session.marks == []

    await session.read("marks", {})
    assert [m["note"] for m in session.marks] == ["later"]
    assert await human.lua("return #ShowMe.pending") == 0
    await session.read("marks", {})
    assert len(session.marks) == 1, "delivered twice"


async def test_nvim_still_exits_when_the_broker_is_away(
    session: Session, human: NvimRPC
) -> None:
    await human.lua("ShowMe.chan = 9999")
    process = session.editor.process
    assert process is not None
    await human.notify("nvim_command", "qall!")
    await asyncio.wait_for(process.wait(), 5)


async def test_note_ids_are_never_reused(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show(
        [
            Location(repo / "src/main.c", line=2, text="first"),
            Location(repo / "src/main.c", line=8, text="second"),
        ],
        title="review",
        focus=True,
    )
    assert [n.id for n in session.notes] == ["A2", "A3"]
    await human.request("nvim_command", "8Ask about the second")
    await until(lambda: bool(session.marks))
    assert session.marks[0]["note_id"] == "A3"

    await session.show([Location(repo / "README.md", text="third")], "t", True)
    assert [n.id for n in session.notes] == ["A4"]
    assert session.marks[0]["note_id"] == "A3", "an old question now names a new note"

    restored = Session.restore(session.state())
    assert restored.frames[0].next_number == 5


async def test_a_long_question_carries_a_bounded_snapshot(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    (repo / "big.txt").write_text(("x" * 100 + "\n") * 200)
    await human.request("nvim_command", "edit big.txt")
    await human.request("nvim_command", "1,200Ask all of it")
    await until(lambda: bool(session.marks))
    mark = session.marks[0]
    assert mark["truncated"] is True
    assert len(mark["text"]) == MARK_TEXT_LIMIT


async def test_the_record_holds_nothing_of_nvims(
    session: Session, human: NvimRPC
) -> None:
    await insert_above(human)
    await session.read("tabs", {})
    (note,) = session.state()["frames"][0]["notes"]
    assert set(note) == {"id", "file", "line", "end_line", "text"}
    assert Session.restore(session.state()).notes == [
        Note(id="A1", file=str(session.notes[0].file), line=4, end_line=6, text="note")
    ]


async def test_ref_copies_a_reference_the_agent_can_take_back(
    session: Session, human: NvimRPC, repo: Path
) -> None:
    await session.show([Location(repo / "src" / "main.c", line=3)], "review", True)
    # A fake provider: the test host has a real clipboard tool, and a test has
    # no business putting anything on the human's clipboard.
    await human.lua("""
        _G.copied = nil
        vim.g.clipboard = {
          name = 'test',
          copy = { ['+'] = function(lines) _G.copied = lines end, ['*'] = function() end },
          paste = {
            ['+'] = function() return { '' } end,
            ['*'] = function() return { '' } end,
          },
        }
    """)

    copied = await human.lua("""
        vim.cmd('edit src/main.c')
        vim.cmd('4Ref')
        local one = _G.copied
        vim.cmd('7,9Ref')
        return { one = one, range = _G.copied }
    """)
    # Root-relative and inclusive, which is how show takes file, line and
    # end_line back.
    assert copied["one"] == ["src/main.c:4"]
    assert copied["range"] == ["src/main.c:7-9"]
