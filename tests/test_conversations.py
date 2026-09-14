# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Which session a client is given, and when one is taken away.

A session belongs to a conversation. A launcher cannot know which conversation
it is starting, so the client says so when it connects: a named one gets its
session back when it is resumed, and one that had to make its name up gets a
session of its own and keeps its notes to itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker
from mcpwire import Wire

from showme import broker as broker_module
from showme import cli, paths, splice, store
from showme import session as session_module
from showme.broker import Broker
from showme.nvimrpc import NvimError, NvimRPC
from showme.session import Session

pytestmark = pytest.mark.nvim

CLAUDE = "CLAUDE_CODE_SESSION_ID:a2b1"


async def running_broker_object() -> Broker:
    """The Broker the `running_broker` fixture started, for tests that have to
    reach past the socket to drive the collector."""
    tasks = [
        task
        for task in asyncio.all_tasks()
        if task.get_coro().__qualname__ == "serve"  # type: ignore[union-attr]
    ]
    assert len(tasks) == 1, tasks
    frame = tasks[0].get_coro().cr_frame  # type: ignore[union-attr]
    return frame.f_locals["broker"]


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


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

    # The spare loses its original key and becomes eligible for collection.
    broker = await running_broker_object()
    spare = broker.sessions[second["id"]]
    assert spare.released
    assert broker.session_by_key(second["key"])[0].sid == one.hello["session"]
    spare.last_seen = time.time() - broker_module.RELEASED_SECONDS - 1
    await broker.collect()
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
    broker.hold(session)
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
    session = await broker.launch_session(str(repo), clean=True)
    await session.ensure()
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
    # From inside the tree too: this is `showme .` typed anywhere under it.
    assert broker.session_in(str(repo / "src")) is newer
    newer.last_seen = time.time() - 120
    assert broker.session_in(str(repo)) is older


async def test_a_tree_above_a_session_is_not_the_tree_it_is_in(
    runtime: Path, repo: Path
) -> None:
    """`showme /` and `showme ~` used to answer with whatever ran last
    anywhere on the machine."""
    broker = Broker()
    await broker.launch_session(str(repo), clean=True)
    assert broker.session_in("/") is None
    assert broker.session_in(str(repo.parent)) is None


async def test_the_nearest_root_wins(runtime: Path, repo: Path) -> None:
    """A worktree inside a checkout has its own session, and standing in it
    means that one rather than the one on the tree around it."""
    broker = Broker()
    outer = await broker.launch_session(str(repo), clean=True)
    inner = await broker.launch_session(str(repo / "src"), clean=True)
    outer.touch()  # and the outer one is the more recently used of the two

    assert broker.session_in(str(repo / "src")) is inner
    assert broker.session_in(str(repo)) is outer


async def test_a_relative_directory_never_reaches_the_broker(
    runtime: Path, repo: Path
) -> None:
    """The broker's cwd is whichever shell first started it, so a path taken
    against it would name a tree the human is not standing in."""
    broker = Broker()
    await broker.launch_session(str(repo), clean=True)
    assert broker.session_in(".") is None
    assert cli._attach_target(str(repo / "src" / "..")) == str(repo)


async def test_a_conversation_keeps_its_session_when_a_second_client_connects(
    running_broker: Path, repo: Path
) -> None:
    """A harness that restarts its MCP server can leave two clients of one
    conversation connected at once. Taking the keys away from the first left
    it answering "no such session" for the rest of its life."""
    first = await launch(repo)
    one = await Wire.connect(
        paths.agent_socket(), key=first["key"], agent=CLAUDE, stable=True
    )
    await show(one, "src/main.c", "from the first client")

    second = await launch(repo)
    two = await Wire.connect(
        paths.agent_socket(), key=second["key"], agent=CLAUDE, stable=True
    )
    assert two.hello["session"] == one.hello["session"]

    still = await show(one, "README.md", "the first client is still here")
    assert still["session"] == one.hello["session"]
    await one.close()
    await two.close()


async def test_a_client_whose_server_restarted_keeps_its_conversation(
    running_broker: Path, repo: Path
) -> None:
    """A harness with no session id to publish names each client process
    instead, so restarting its MCP server mid-conversation presents the same
    key under a new name. Refusing that left the conversation with no session
    for the rest of its life."""
    first = await launch(repo)
    one = await Wire.connect(paths.agent_socket(), key=first["key"])
    await show(one, "src/main.c", "before the restart")
    await one.close()

    # The same box, the same key, a new process with a new name.
    two = await Wire.connect(paths.agent_socket(), key=first["key"])
    assert two.hello["session"] == one.hello["session"]
    assert await notes_of(two) == ["before the restart"]
    await two.close()


async def test_a_key_the_human_handed_out_names_the_session_they_made(
    runtime: Path, repo: Path
) -> None:
    """`showme new` prints a key to hand to an agent. A conversation that
    already had a session went on using that one, and -- worse -- the key the
    human was holding quietly started naming it too."""
    broker = Broker()
    launched = await broker.launch_session(str(repo), clean=True)
    await broker.claim(launched.key, CLAUDE, True)
    handmade = await broker.new_session(str(repo), clean=True)
    try:
        claimed = await broker.claim(handmade.key, CLAUDE, True)
        assert claimed is not None
        assert claimed.session is handmade
        # And it is still the human's: theirs to kill, not the collector's.
        assert broker.session_by_key(handmade.key)[0] is handmade
        assert handmade.human and not handmade.released
    finally:
        for session in list(broker.sessions.values()):
            await session.close()


async def test_a_resumed_conversation_in_another_tree_gets_its_own_session(
    runtime: Path, repo: Path, tmp_path: Path
) -> None:
    """The root is what a key is clamped to and where nvim runs. Handing a
    conversation resumed elsewhere the session it had silently kept the reach
    of the tree it started in."""
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    broker = Broker()
    first = await broker.launch_session(str(repo), clean=True)
    claimed = await broker.claim(first.key, CLAUDE, True)
    assert claimed is not None

    second = await broker.launch_session(str(elsewhere), clean=True)
    resumed = await broker.claim(second.key, CLAUDE, True)
    assert resumed is not None
    assert resumed.session is not first
    assert resumed.root.path == elsewhere
    for session in list(broker.sessions.values()):
        await session.close()


async def test_a_resume_does_not_stop_an_editor_the_human_is_in(
    runtime: Path, repo: Path
) -> None:
    """Over ssh the human is told to attach before asking for anything, which
    lands them on the session the launch prepared. The hello then dropped it,
    and `close` takes their terminal with it."""
    broker = Broker()
    mine = await broker.launch_session(str(repo), clean=True)
    mine.agent, mine.stable = CLAUDE, True
    spare = await broker.launch_session(str(repo), clean=True)
    await broker.attach(str(repo))
    ui = await NvimRPC.connect(spare.socket)
    await ui.request("nvim_ui_attach", 80, 24, {})
    try:
        assert await spare.attached() is True
        claimed = await broker.claim(spare.key, CLAUDE, True)
        assert claimed is not None and claimed.session is mine
        assert spare.alive, "the human's editor was stopped"
        # And nothing resolves to it any more, so the key the client presented
        # cannot name two sessions at once.
        assert broker.session_by_key(claimed.key)[0] is mine
    finally:
        await ui.close()
        for session in list(broker.sessions.values()):
            await session.close()


async def test_a_busy_nvim_is_left_alone_rather_than_collected(
    runtime: Path, repo: Path
) -> None:
    """An nvim that does not answer is busy, not gone. Taking silence for
    permission killed a session someone was in -- and the error escaped the
    broker's own loop, which took the broker with it."""
    broker = Broker()
    session = await broker.launch_session(str(repo), clean=True)
    await session.ensure()
    await stale(broker, session.sid)

    async def wedged(timeout: float = 30.0) -> bool:
        raise NvimError("nvim did not answer nvim_list_uis within 5.0s")

    session.attached = wedged  # type: ignore[method-assign]
    await broker.collect()
    assert list(broker.sessions) == [session.sid]
    del session.attached  # type: ignore[attr-defined]
    await broker.kill(session.sid)


async def test_a_session_the_human_made_is_not_collected(
    runtime: Path, repo: Path
) -> None:
    """`showme new` is the human's own editor, holding whatever they have been
    doing in it. Stopping it sends `qall!`, and unwritten buffers go with it."""
    broker = Broker()
    session = await broker.new_session(str(repo), clean=True)
    await stale(broker, session.sid)
    await broker.collect()
    assert list(broker.sessions) == [session.sid]
    await broker.kill(session.sid)


async def test_a_client_that_does_not_name_itself_keeps_its_own_session(
    running_broker: Path, repo: Path
) -> None:
    """An older published splice sends a hello with no agent at all."""
    first = await launch(repo)
    one = await Wire.connect(paths.agent_socket(), key=first["key"], agent="")
    assert one.hello == {"ok": True, "session": "1"}
    await show(one, "src/main.c", "from a client that says nothing")

    second = await launch(repo)
    two = await Wire.connect(paths.agent_socket(), key=second["key"], agent="")
    assert two.hello["session"] != "1", "it took over the first one's session"
    assert await notes_of(two) == []
    await one.close()
    await two.close()


async def test_a_connected_client_holds_its_session_against_the_collector(
    running_broker: Path, repo: Path
) -> None:
    """`held` is the only thing protecting a conversation that has been quiet
    for longer than its window, and nothing filled it in a test before."""
    reply = await launch(repo)
    wire = await Wire.connect(paths.agent_socket(), key=reply["key"])
    await show(wire, "src/main.c", "still working")

    broker = await running_broker_object()
    session = broker.sessions[wire.hello["session"]]
    session.last_seen = time.time() - broker_module.RESUMABLE_SECONDS - 1
    await broker.collect()
    assert wire.hello["session"] in broker.sessions

    # And once it goes, the same session is collectable.
    await wire.close()
    await until(lambda: not broker.held)
    session.last_seen = time.time() - broker_module.RESUMABLE_SECONDS - 1
    await broker.collect()
    assert wire.hello["session"] not in broker.sessions


async def test_a_tool_call_counts_as_contact(running_broker: Path, repo: Path) -> None:
    """A conversation can hold one connection open for hours. Only the hello
    and the disconnect used to move the clock, so a long quiet one aged out
    from under its client."""
    reply = await launch(repo)
    wire = await Wire.connect(paths.agent_socket(), key=reply["key"])
    broker = await running_broker_object()
    session = broker.sessions[wire.hello["session"]]
    session.last_seen = time.time() - 10_000
    await show(wire, "src/main.c", "a call an hour later")
    assert time.time() - session.last_seen < 5
    await wire.close()


async def test_the_broker_collects_on_its_own(
    runtime: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise collection through the broker loop."""
    monkeypatch.setattr(broker_module, "TICK_SECONDS", 0.05)
    stop, task = await start_broker()
    try:
        reply = await admin({"cmd": "ensure", "root": str(repo), "clean": True})
        broker = await running_broker_object()
        session = broker.sessions[reply["id"]]
        session.last_seen = time.time() - broker_module.RESUMABLE_SECONDS - 1
        await until(lambda: reply["id"] not in broker.sessions)
    finally:
        await stop_broker(stop, task)


async def test_a_session_whose_keys_changed_keeps_its_nvim_across_a_restart(
    runtime: Path, repo: Path
) -> None:
    """The socket is named after the key a session was made with, and a
    session can stop answering to that key -- a prepared one the human is
    attached to has its keys taken away when the conversation lands
    elsewhere. Deriving the socket again from the current key hands the next
    broker a path nobody is listening on: it adopts nothing, the record is
    marked dead, and the human's editor is left running with no owner."""
    broker = Broker()
    session = await broker.launch_session(str(repo), clean=True)
    await session.ensure()
    listening = session.socket
    session.revoke()

    restored = Session.restore(session.state())
    assert restored.socket == listening
    try:
        await restored.ensure(spawn=False)
        assert restored.alive, "the running nvim was not adopted"
    finally:
        await restored.detach()
        await session.close()


async def test_one_client_leaving_does_not_unhold_a_session_another_is_on(
    running_broker: Path, repo: Path
) -> None:
    """Two connections of one conversation overlap when a harness restarts
    its MCP server. A set could only remember that someone was holding it."""
    first = await launch(repo)
    one = await Wire.connect(
        paths.agent_socket(), key=first["key"], agent=CLAUDE, stable=True
    )
    second = await launch(repo)
    two = await Wire.connect(
        paths.agent_socket(), key=second["key"], agent=CLAUDE, stable=True
    )
    sid = one.hello["session"]
    assert two.hello["session"] == sid

    broker = await running_broker_object()
    await one.close()
    await until(lambda: broker.held.get(sid, 0) == 1)

    broker.sessions[sid].last_seen = time.time() - broker_module.RESUMABLE_SECONDS - 1
    await broker.collect()
    assert sid in broker.sessions, "collected while a client was still on it"
    await two.close()


async def test_a_hello_that_is_not_an_object_is_refused_and_let_go_of(
    running_broker: Path, repo: Path
) -> None:
    """Whatever a client puts under the field, from the one socket a sandbox
    can reach. It used to raise past the handler, leaving the connection in
    the broker's list forever -- and a broker with a client it thinks is
    connected never idles out."""
    broker = await running_broker_object()
    for body in ("nonsense", None, [], 7):
        reader, writer = await asyncio.open_unix_connection(str(paths.agent_socket()))
        writer.write(json.dumps({splice.HELLO: body}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 10)
        assert json.loads(line)[splice.HELLO] == {
            "ok": False,
            "error": "no such session",
        }
        writer.close()
    await until(lambda: broker.clients == 0)


async def test_a_listing_does_not_wait_on_an_nvim_that_is_busy(
    running_broker: Path, repo: Path
) -> None:
    """`showme ls` asks every session whether a terminal is on it. One that
    cannot answer must not fail the listing or hold it up."""
    reply = await admin({"cmd": "ensure", "root": str(repo), "clean": True})
    broker = await running_broker_object()
    session = broker.sessions[reply["id"]]

    async def wedged(timeout: float = 30.0) -> bool:
        raise NvimError("nvim did not answer nvim_list_uis within 5.0s")

    session.attached = wedged  # type: ignore[method-assign]
    listing = await admin({"cmd": "ls"})
    assert listing["ok"], listing
    assert [s["id"] for s in listing["sessions"]] == [reply["id"]]
    assert listing["sessions"][0]["attached"] is False
    del session.attached  # type: ignore[attr-defined]


async def test_a_resume_does_not_race_an_attach_that_has_started(
    runtime: Path, repo: Path
) -> None:
    """Over ssh the human attaches before asking for anything, which lands
    them on the session the launch prepared. Stopping it the moment the agent
    connects raced their terminal: nvim had been asked for but had not drawn
    yet, so the session did not look alive."""
    broker = Broker()
    mine = await broker.launch_session(str(repo), clean=True)
    mine.agent, mine.stable = CLAUDE, True
    spare = await broker.launch_session(str(repo), clean=True)

    attaching = asyncio.create_task(broker.attach(str(repo)))
    await asyncio.sleep(0)  # the attach has started; nvim has not answered yet
    claimed = await broker.claim(spare.key, CLAUDE, True)
    assert claimed is not None and claimed.session is mine
    await attaching
    try:
        # Keep the editor alive while the terminal connects its UI.
        assert spare.alive
        await broker.collect()
        assert spare.sid in broker.sessions, "collected while a terminal came up"

        # Once the grace has passed with nobody in it, it goes.
        spare.last_seen = time.time() - broker_module.RELEASED_SECONDS - 1
        await broker.collect()
        assert spare.sid not in broker.sessions
    finally:
        for session in list(broker.sessions.values()):
            await session.close()
        await spare.close()


async def test_a_released_session_is_not_what_a_tree_means(
    runtime: Path, repo: Path
) -> None:
    broker = Broker()
    kept = await broker.launch_session(str(repo), clean=True)
    kept.agent, kept.stable = CLAUDE, True
    spare = await broker.launch_session(str(repo), clean=True)
    await broker.claim(spare.key, CLAUDE, True)
    # The most recently made session in the tree, and the one a human would
    # be offered by age alone -- but it answers to nothing and is on its way
    # out, so attaching to it would show them an editor nothing can reach.
    spare.touch()
    assert spare.released
    assert broker.session_in(str(repo)) is kept


async def test_a_session_an_agent_is_on_outranks_one_left_open(
    running_broker: Path, repo: Path
) -> None:
    """The collector touches a session it finds a terminal on, so ranking by
    age alone offered yesterday's abandoned review over today's work."""
    stale_reply = await launch(repo)
    live_reply = await launch(repo)
    wire = await Wire.connect(paths.agent_socket(), key=live_reply["key"])
    await show(wire, "src/main.c", "today")
    broker = await running_broker_object()
    # As a collect pass that found a terminal on the old one leaves things.
    broker.sessions[stale_reply["id"]].touch()

    assert broker.session_in(str(repo)).sid == wire.hello["session"]
    await wire.close()


async def test_a_hold_does_not_outlive_the_session_it_was_taken_on(
    runtime: Path, repo: Path
) -> None:
    """Ids are handed out again, so a hold left behind by a killed session
    would keep whatever takes its id next from ever being collected."""
    broker = Broker()
    first = await broker.launch_session(str(repo), clean=True)
    broker.hold(first)
    await broker.kill(first.sid)

    second = await broker.launch_session(str(repo), clean=True)
    assert second.sid == first.sid
    broker.hold(second)
    # The first session's client goes away now, naming a session that no
    # longer exists. Letting go by id would let go of the new one's hold.
    broker.release(first)
    assert broker.held == {second.sid: 1}

    await stale(broker, second.sid)
    await broker.collect()
    assert list(broker.sessions) == [second.sid]
    broker.release(second)
    await broker.collect()
    assert broker.sessions == {}


async def test_a_session_the_human_made_is_recorded_as_theirs_at_once(
    runtime: Path, repo: Path
) -> None:
    """Set after the record was written, it was absent from the file a crash
    would leave behind, and the session came back collectable."""
    broker = Broker()
    session = await broker.new_session(str(repo), clean=True)
    try:
        saved = next(s for s in store.load() if s["sid"] == session.sid)
        assert saved["human"] is True
    finally:
        await session.close()


async def test_a_session_stops_answering_to_the_launches_it_has_outlived(
    runtime: Path, repo: Path
) -> None:
    broker = Broker()
    session = await broker.launch_session(str(repo), clean=True)
    await broker.claim(session.key, CLAUDE, True)
    keys = []
    for _ in range(session_module.LAUNCHES_REMEMBERED + 3):
        spare = await broker.launch_session(str(repo), clean=True)
        keys.append(spare.key)
        await broker.claim(spare.key, CLAUDE, True)
    try:
        assert len(session.also) == session_module.LAUNCHES_REMEMBERED
        assert broker.session_by_key(keys[-1])[0] is session
        assert broker.session_by_key(keys[0]) is None
    finally:
        for live in list(broker.sessions.values()):
            await live.close()


async def test_the_human_in_a_handed_over_session_is_told_so(
    runtime: Path, repo: Path
) -> None:
    """Nothing will ever draw there again, and an editor that cannot answer
    looks exactly like an agent that has gone quiet."""
    broker = Broker()
    mine = await broker.launch_session(str(repo), clean=True)
    mine.agent, mine.stable = CLAUDE, True
    spare = await broker.launch_session(str(repo), clean=True)
    await spare.ensure()
    said: list[str] = []

    async def remember(message: str) -> None:
        said.append(message)

    spare._tell_human = remember  # type: ignore[method-assign]

    await broker.claim(spare.key, CLAUDE, True)
    try:
        assert said == [], "not before anyone is known to be there"

        # A terminal on it is what says a human is, and the collector -- which
        # keeps it for that reason -- is the one caller that asks.
        async def has_a_terminal(timeout: float = 30.0) -> bool:
            return True

        spare.attached = has_a_terminal  # type: ignore[method-assign]
        spare.last_seen = time.time() - broker_module.RELEASED_SECONDS - 1
        await broker.collect()
        assert spare.sid in broker.sessions
        # Said without waiting for the editor: the collector is on the
        # broker's own loop and does not stop for a notice.
        await until(lambda: len(said) == 1)
        assert str(repo) in said[0]
        # And only once, however long they sit there.
        spare.last_seen = time.time() - broker_module.RELEASED_SECONDS - 1
        await broker.collect()
        await asyncio.sleep(0.05)
        assert len(said) == 1
    finally:
        del spare.attached  # type: ignore[attr-defined]
        for session in list(broker.sessions.values()):
            await session.close()
        await spare.close()


async def test_a_session_claimed_again_is_no_longer_on_its_way_out(
    runtime: Path, repo: Path
) -> None:
    """`showme ls` prints the key a released session was given when it lost
    the one it had, so a human can hand it out. A client that reaches it puts
    it back in use, and it gets a window like any other rather than going on
    the next pass."""
    broker = Broker()
    mine = await broker.launch_session(str(repo), clean=True)
    mine.agent, mine.stable = CLAUDE, True
    spare = await broker.launch_session(str(repo), clean=True)
    await broker.claim(spare.key, CLAUDE, True)
    assert spare.released

    claimed = await broker.claim(spare.key, "other:1", True)
    assert claimed is not None and claimed.session is spare
    assert not spare.released

    spare.last_seen = time.time() - broker_module.RELEASED_SECONDS - 1
    await broker.collect()
    assert spare.sid in broker.sessions
    for session in list(broker.sessions.values()):
        await session.close()
