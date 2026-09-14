# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Which session a client is given, and when one is taken away.

A session belongs to a conversation. A launcher cannot know which conversation
it is starting, so the client says so when it connects: a named one gets its
session back when it is resumed, and one that had to make its name up gets a
session of its own and keeps its notes to itself.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from showme import broker as broker_module
from showme import paths
from showme.broker import Broker
from showme.nvimrpc import NvimRPC

pytestmark = pytest.mark.nvim

CLAUDE = "CLAUDE_CODE_SESSION_ID:a2b1"


async def launch(repo: Path) -> dict:
    """One launcher run, as `showme box` makes it."""
    reply = await admin({"cmd": "ensure", "root": str(repo), "clean": True})
    assert reply["ok"], reply
    return reply


async def show(wire: Wire, file: str, text: str) -> dict:
    result = await wire.call("show", {"locations": [{"file": file, "text": text}]})
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def notes_of(wire: Wire) -> list[str]:
    result = await wire.call("read", {"what": "notes"})
    payload = json.loads(result["content"][0]["text"])
    return [note["text"] for frame in payload["frames"] for note in frame["notes"]]


async def test_a_resumed_conversation_is_given_its_session_back(
    running_broker: Path, repo: Path
) -> None:
    first = await launch(repo)
    one = await Wire.connect(
        paths.agent_socket(), key=first["key"], agent=CLAUDE, stable=True
    )
    await show(one, "src/main.c", "from the first run")
    await one.close()

    # Resuming starts a fresh client, which the launcher prepares a fresh
    # session for. The conversation is the same one, so the session is too.
    second = await launch(repo)
    two = await Wire.connect(
        paths.agent_socket(), key=second["key"], agent=CLAUDE, stable=True
    )
    assert two.hello["session"] == one.hello["session"]
    assert await notes_of(two) == ["from the first run"]

    # And the session the second launch prepared is gone: it was never shown
    # anything, and leaving it would be the pile this change is about.
    listing = await admin({"cmd": "ls"})
    assert [s["id"] for s in listing["sessions"]] == [one.hello["session"]]
    await two.close()


async def test_the_resumed_session_answers_to_the_key_the_client_holds(
    running_broker: Path, repo: Path
) -> None:
    """The client knows one key: the one its own launch was given. The session
    it is handed back was created with another, and has to take the new one."""
    first = await launch(repo)
    one = await Wire.connect(
        paths.agent_socket(), key=first["key"], agent=CLAUDE, stable=True
    )
    await show(one, "src/main.c", "first")
    await one.close()

    second = await launch(repo)
    two = await Wire.connect(
        paths.agent_socket(), key=second["key"], agent=CLAUDE, stable=True
    )
    # Naming the session by key explicitly, which is what an agent holding a
    # key does; the session param goes through the same lookup.
    result = await two.call(
        "show",
        {"session": second["key"], "locations": [{"file": "README.md", "text": "b"}]},
    )
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["session"] == one.hello["session"]
    await two.close()


async def test_a_conversation_that_cannot_be_named_keeps_to_itself(
    running_broker: Path, repo: Path
) -> None:
    """Without a harness id the client makes one up, so a second launch in the
    same tree is a second session rather than the first one's leftovers."""
    first = await launch(repo)
    one = await Wire.connect(paths.agent_socket(), key=first["key"])
    await show(one, "src/main.c", "from the first run")
    await one.close()

    second = await launch(repo)
    two = await Wire.connect(paths.agent_socket(), key=second["key"])
    assert two.hello["session"] != one.hello["session"]
    assert await notes_of(two) == []
    shown = await show(two, "README.md", "from the second run")
    # Its own frame, from the beginning: continuing the first run's letter and
    # numbering is what made ids look arbitrary.
    assert shown["ids"] == ["A1"]
    await two.close()


async def test_two_harnesses_numbering_from_one_do_not_name_each_other(
    running_broker: Path, repo: Path
) -> None:
    first = await launch(repo)
    second = await launch(repo)
    one = await Wire.connect(
        paths.agent_socket(), key=first["key"], agent="one:1", stable=True
    )
    two = await Wire.connect(
        paths.agent_socket(), key=second["key"], agent="other:1", stable=True
    )
    assert one.hello["session"] != two.hello["session"]
    await one.close()
    await two.close()


async def stale(broker: Broker, sid: str) -> None:
    """Age a session past every collection window."""
    broker.sessions[sid].last_seen = time.time() - broker_module.RESUMABLE_SECONDS - 1


async def test_a_session_nobody_is_using_is_collected(
    runtime: Path, repo: Path
) -> None:
    broker = Broker()
    session = await broker.launch_session(str(repo), clean=True)
    await broker.collect()
    assert broker.sessions  # young enough to still be waiting for its client

    await stale(broker, session.sid)
    await broker.collect()
    assert broker.sessions == {}


async def test_a_launch_is_collected_sooner_than_a_conversation(
    runtime: Path, repo: Path
) -> None:
    """A named conversation may come back; one that made its name up cannot,
    so it is kept only long enough for a human to read what it left."""
    broker = Broker()
    named = await broker.launch_session(str(repo), clean=True)
    named.agent, named.stable = CLAUDE, True
    unnamed = await broker.launch_session(str(repo), clean=True)
    unnamed.agent, unnamed.stable = "launch:beef", False

    aged = time.time() - broker_module.LAUNCH_SECONDS - 1
    named.last_seen = unnamed.last_seen = aged
    await broker.collect()
    assert list(broker.sessions) == [named.sid]
    await broker.kill(named.sid)


async def test_a_session_a_client_holds_is_never_collected(
    runtime: Path, repo: Path
) -> None:
    broker = Broker()
    session = await broker.launch_session(str(repo), clean=True)
    broker.held.add(session.sid)
    await stale(broker, session.sid)
    await broker.collect()
    assert list(broker.sessions) == [session.sid]
    await broker.kill(session.sid)


async def test_a_session_with_a_terminal_on_it_is_kept(
    runtime: Path, repo: Path
) -> None:
    """The review usually outlives the agent, and the human reading it is the
    one thing that says so."""
    broker = Broker()
    session = await broker.new_session(str(repo), clean=True)
    ui = await NvimRPC.connect(session.socket)
    await ui.request("nvim_ui_attach", 80, 24, {})
    try:
        assert await session.attached() is True
        await stale(broker, session.sid)
        await broker.collect()
        assert list(broker.sessions) == [session.sid]
        # And the clock starts again from the moment it was found in use.
        assert time.time() - session.last_seen < 5
    finally:
        await ui.close()
        await broker.kill(session.sid)


async def test_a_tree_attaches_to_the_session_last_used_in_it(
    runtime: Path, repo: Path
) -> None:
    """Several conversations can be working in one tree, so a root no longer
    names a single session. The one the human means is the live one."""
    broker = Broker()
    older = await broker.launch_session(str(repo), clean=True)
    newer = await broker.launch_session(str(repo), clean=True)
    older.last_seen = time.time() - 60

    assert broker.session_in(str(repo)) is newer
    # And a directory above the root finds it too, which is how a human
    # standing in a parent reaches the session in a worktree below.
    assert broker.session_in(str(repo.parent)) is newer
    assert broker.session_in(str(repo / "src")) is None
    newer.last_seen = time.time() - 120
    assert broker.session_in(str(repo)) is older
