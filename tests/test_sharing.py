# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Two agents working in one tree. They are two conversations, so they get a
session each and neither can write into the other's frames; within one session,
two calls that overlap still have to be answered separately."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from showme import paths
from showme.session import Location, Session

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


async def test_two_agents_in_one_tree_get_a_session_each(
    running_broker: Path, repo: Path
) -> None:
    created = await admin({"cmd": "ensure", "root": str(repo)})
    again = await admin({"cmd": "ensure", "root": str(repo)})

    one = await Wire.connect(paths.agent_socket(), key=created["key"])
    two = await Wire.connect(paths.agent_socket(), key=again["key"])
    assert one.hello["session"] != two.hello["session"]

    results = await asyncio.gather(
        one.call("show", {"locations": [{"file": "src/main.c", "text": "from one"}]}),
        two.call("show", {"locations": [{"file": "README.md", "text": "from two"}]}),
    )
    shown = [json.loads(r["content"][0]["text"]) for r in results]
    # Each starts its own frame at its own beginning. Sharing a session left
    # the second agent continuing the first one's letter and numbering.
    assert [s["ids"] for s in shown] == [["A1"], ["A1"]]
    assert [len(s["frames"]) for s in shown] == [1, 1]
    await one.close()
    await two.close()
