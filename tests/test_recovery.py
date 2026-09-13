# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Losing contact with nvim must not cost the human anything.

An editor holds work nobody has saved. A broken connection says the broker
cannot see it, never that it has gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from nvim_mcp import paths
from nvim_mcp.models import LOCATION_LIMIT
from nvim_mcp.nvimrpc import NvimRPC
from nvim_mcp.session import MARK_TEXT_LIMIT, NOTE_LIMIT, Location, Session

pytestmark = pytest.mark.nvim


UNSAVED = "int unsaved(void) { return 1; }"


async def edited(session: Session) -> None:
    """Leave work in the editor that exists nowhere else."""
    assert session.rpc is not None
    await session.rpc.lua(
        """
        vim.cmd('edit src/main.c')
        vim.api.nvim_buf_set_lines(0, 0, 1, false, { ... })
        """,
        UNSAVED,
    )


async def first_line(session: Session) -> list[str]:
    assert session.rpc is not None
    return await session.rpc.lua(
        "return vim.api.nvim_buf_get_lines(vim.fn.bufnr('src/main.c'), 0, 1, true)"
    )


async def test_a_dropped_connection_reconnects_to_the_same_editor(
    runtime: Path, repo: Path
) -> None:
    session = Session.create("1", repo, clean=True)
    await session.ensure()
    pid = session.editor.pid
    await edited(session)

    # The broker loses contact. nvim knows nothing about it and is still
    # listening on its socket with the edit in it.
    assert session.rpc is not None
    await session.rpc.close()

    await session.show(
        [Location(repo / "src" / "main.c", line=1, text="x")], "t", False
    )
    assert session.editor.pid == pid, "a live editor was replaced to get a connection"
    assert await first_line(session) == [UNSAVED], "unsaved work was lost"
    await session.close()


async def test_bytes_that_are_not_utf8_do_not_end_the_connection(
    runtime: Path, repo: Path
) -> None:
    session = Session.create("1", repo, clean=True)
    await session.ensure()
    pid = session.editor.pid
    await edited(session)

    # A latin-1 file or a half-finished edit reaches the decoder as bytes that
    # are not UTF-8. They are the human's content to lose, not the
    # connection's.
    decoded = await session.rpc.lua("return string.char(0xC3)")
    assert decoded is not None
    assert session.rpc is not None and not session.rpc.closed

    await session.show(
        [Location(repo / "src" / "main.c", line=1, text="x")], "t", False
    )
    assert session.editor.pid == pid
    assert await first_line(session) == [UNSAVED]
    await session.close()


async def test_a_question_about_multibyte_text_is_not_cut_in_half(
    running_broker: Path, repo: Path
) -> None:
    # Long enough to be truncated, with a two-byte character straddling the
    # cut wherever it falls.
    (repo / "src" / "wide.c").write_text("// " + "ä" * MARK_TEXT_LIMIT + "\n")
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket(), key=session["key"])

    nvim = await NvimRPC.connect(Path(session["socket"]))
    await nvim.request("nvim_command", "edit src/wide.c")
    await nvim.request("nvim_command", "1Ask what is this")
    await nvim.close()

    result = await wire.call("read", {"what": "marks"})
    payload = json.loads(result["content"][0]["text"])
    mark = payload["marks"][0]
    assert mark["truncated"] is True
    assert mark["note"] == "what is this"
    # Whole characters only, and no replacement for a byte that was cut.
    assert "�" not in mark["text"]
    assert mark["text"].endswith("ä")
    await wire.close()


async def test_a_call_at_the_limits_of_the_contract_is_answered(
    running_broker: Path, repo: Path
) -> None:
    """The transport has to carry what the tools accept.

    Fifty locations of two thousand characters is a legal call and several
    times asyncio's default line limit; it used to arrive as a hangup.
    """
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket(), key=session["key"])
    result = await wire.call(
        "show",
        {
            "locations": [
                {"file": "src/main.c", "line": 1, "text": "word " * (NOTE_LIMIT // 5)}
                for _ in range(LOCATION_LIMIT)
            ]
        },
    )
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert len(payload["ids"]) == LOCATION_LIMIT
    await wire.close()
