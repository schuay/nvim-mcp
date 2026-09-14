# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""What a key reaches. The key a launcher hands a sandboxed client is clamped
to the session root; the one a client on the host takes over the admin socket
reads whatever the human reads, because it is the human."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin, start_broker, stop_broker
from mcpwire import Wire

from showme import paths
from showme.nvimrpc import NvimRPC

pytestmark = pytest.mark.nvim


async def call(wire: Wire, tool: str, key: str, **arguments: object) -> dict:
    result = await wire.call(tool, {"session": key, **arguments})
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def keys(repo: Path) -> tuple[str, str]:
    """The host key for a session rooted at `repo`, and its clamped one."""
    opened = await admin({"cmd": "ensure", "root": str(repo), "open": True})
    assert opened["ok"], opened
    clamped = await admin({"cmd": "ensure", "root": str(repo)})
    assert (clamped["id"], clamped["created"]) == (opened["id"], False)
    assert clamped["key"] != opened["key"]
    return opened["key"], clamped["key"]


async def test_the_host_key_shows_a_file_outside_the_root(
    running_broker: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "notes.md"
    outside.write_text("what the agent is reading anyway\n")
    host, clamped = await keys(repo)
    wire = await Wire.connect(paths.agent_socket())

    payload = await call(
        wire,
        "show",
        host,
        locations=[{"file": str(outside)}, {"file": "src/main.c", "line": 2}],
    )
    assert payload["refused"] == []
    assert sorted(Path(p).name for p in payload["opened"]) == ["main.c", "notes.md"]

    # The same session, the key a box would have been given.
    payload = await call(wire, "show", clamped, locations=[{"file": str(outside)}])
    assert payload["refused"] == [
        {"file": str(outside), "reason": "outside the session root"}
    ]
    await wire.close()


async def test_what_each_key_is_told_is_open(
    running_broker: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "notes.md"
    outside.write_text("hello\n")
    host, clamped = await keys(repo)
    wire = await Wire.connect(paths.agent_socket())
    await call(
        wire,
        "show",
        host,
        locations=[{"file": str(outside)}, {"file": "src/main.c"}],
    )

    payload = await call(wire, "read", host, what="tabs")
    assert sorted(Path(b["file"]).name for b in payload["buffers"]) == [
        "main.c",
        "notes.md",
    ]
    # A clamped key hears about the human's editor only as far as its root.
    payload = await call(wire, "read", clamped, what="tabs")
    assert [Path(b["file"]).name for b in payload["buffers"]] == ["main.c"]

    payload = await call(wire, "read", host, what="range", file=str(outside))
    assert payload["range"]["text"] == "hello"
    result = await wire.call(
        "read", {"session": clamped, "what": "range", "file": str(outside)}
    )
    assert result.get("isError") is True
    assert "outside the session root" in result["content"][0]["text"]
    await wire.close()


async def test_the_host_key_is_nowhere_a_launcher_or_a_human_can_copy_it(
    running_broker: Path, repo: Path
) -> None:
    host, clamped = await keys(repo)
    listing = (await admin({"cmd": "ls"}))["sessions"]
    assert [session["key"] for session in listing] == [clamped]

    # `showme new` prints a key for the human to pass on, so it is the clamped
    # one too, even for a root a launcher could never have picked.
    made = await admin({"cmd": "new", "root": str(repo / "src")})
    wire = await Wire.connect(paths.agent_socket(), key=made["key"])
    payload = await call(wire, "show", made["key"], locations=[{"file": str(repo)}])
    assert payload["refused"] == [
        {"file": str(repo), "reason": "outside the session root"}
    ]
    await wire.close()
    assert made["key"] != host


async def test_the_host_key_survives_a_broker_restart(
    runtime: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "notes.md"
    outside.write_text("hello\n")
    stop, task = await start_broker()
    host, _ = await keys(repo)
    await stop_broker(stop, task)

    stop, task = await start_broker()
    try:
        # A splice replays the key it was handed when the broker it was talking
        # to comes back, so a key minted again on restore would leave a running
        # client with no session.
        wire = await Wire.connect(paths.agent_socket(), key=host)
        assert wire.hello["ok"] is True
        payload = await call(wire, "show", host, locations=[{"file": str(outside)}])
        assert payload["refused"] == []
        await wire.close()
    finally:
        await stop_broker(stop, task)


async def test_a_clamped_key_shows_a_diff_it_wrote_inside_the_root(
    running_broker: Path, repo: Path
) -> None:
    """The whole of what a box can do when it is asked to annotate a diff.

    It has nowhere outside the root to write and nowhere outside the root to
    show from, so the one recipe the tool description gives has to work with
    the clamped key and not only with the host one.
    """
    patch = repo / ".git" / "review.diff"
    patch.write_text("--- a/src/main.c\n+++ b/src/main.c\n@@ -1 +1 @@\n-was\n+is\n")
    _, clamped = await keys(repo)
    wire = await Wire.connect(paths.agent_socket())

    payload = await call(
        wire,
        "show",
        clamped,
        locations=[{"file": ".git/review.diff", "line": 4, "text": "why"}],
    )
    assert payload["refused"] == []
    assert payload["opened"] == [str(patch)]

    # The extension is the whole of what makes it readable: bufload runs
    # filetype detection, so the diff arrives highlighted rather than flat.
    reply = await admin({"cmd": "ensure", "root": str(repo), "open": True})
    nvim = await NvimRPC.connect(Path(reply["socket"]))
    filetype = await nvim.lua("""
        for _, buf in ipairs(vim.api.nvim_list_bufs()) do
          if vim.api.nvim_buf_get_name(buf):match('review%.diff$') then
            return vim.bo[buf].filetype
          end
        end
    """)
    assert filetype == "diff"
    await nvim.close()
    await wire.close()
