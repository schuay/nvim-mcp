# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Two agents on one session. `nv box` reuses the session rooted at a tree, so
a second box working there shares the human's editor rather than opening its
own, and the two can call at the same moment."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from nvim_mcp import paths
from nvim_mcp.session import Location, Session

pytestmark = pytest.mark.nvim


async def test_overlapping_shows_each_get_their_own_notes(
    runtime: Path, repo: Path
) -> None:
    session = Session.create("1", repo, clean=True)
    await session.ensure()
    first, second = await asyncio.gather(
        session.show([Location(repo / "src/main.c", line=1, text="A")], "one", False),
        session.show([Location(repo / "README.md", line=1, text="B")], "two", False),
    )
    # Distinct ids: the record used to be changed before the lock was taken,
    # so both callers were answered with the second one's note.
    assert first["ids"] != second["ids"]
    assert len(first["ids"]) == len(second["ids"]) == 1

    # And each summary describes the stack that call left, not the other's.
    titles = [(f["title"], f["notes"]) for f in first["frames"]]
    assert titles == [("one", 1)]
    assert [(f["title"], f["notes"]) for f in second["frames"]] == [("two", 1)]
    await session.close()


async def test_two_clients_share_one_session(running_broker: Path, repo: Path) -> None:
    created = await admin({"cmd": "ensure", "root": str(repo)})
    again = await admin({"cmd": "ensure", "root": str(repo)})
    assert again["id"] == created["id"]

    one = await Wire.connect(paths.agent_socket(), key=created["key"])
    two = await Wire.connect(paths.agent_socket(), key=again["key"])
    results = await asyncio.gather(
        one.call("show", {"locations": [{"file": "src/main.c", "text": "from one"}]}),
        two.call(
            "show",
            {"frame": "push", "locations": [{"file": "README.md", "text": "from two"}]},
        ),
    )
    ids = [json.loads(r["content"][0]["text"])["ids"] for r in results]
    assert ids[0] != ids[1], ids
    await one.close()
    await two.close()
