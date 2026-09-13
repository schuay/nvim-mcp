# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Launching an agent for one session: `nv ensure`, `nv box`, and the key a
sandboxed client presents instead of being told."""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from nvim_mcp import cli, paths

pytestmark = pytest.mark.nvim


def box_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(root=str(root), clean=True, background=None)


async def test_ensure_reuses_the_session_rooted_here(
    running_broker: Path, repo: Path
) -> None:
    first = await admin({"cmd": "ensure", "root": str(repo)})
    again = await admin({"cmd": "ensure", "root": str(repo)})
    assert first["created"] is True
    # A launcher runs this on every start; a second agent in the same tree has
    # to land in the editor the human already has open.
    assert again["created"] is False
    assert (again["id"], again["key"]) == (first["id"], first["key"])

    elsewhere = repo / "src"
    other = await admin({"cmd": "ensure", "root": str(elsewhere)})
    assert other["created"] is True
    assert other["id"] != first["id"]


def test_box_leaves_a_key_and_a_spec_that_binds_only_it(
    runtime: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_ensure_broker", lambda: None)
    monkeypatch.setattr(
        cli,
        "_ask",
        lambda request, timeout=0: {
            "ok": True,
            "id": "3",
            "key": "the-key",
            "root": request["root"],
            "created": False,
        },
    )
    assert cli.cmd_box(box_args(repo)) == 0
    captured = capsys.readouterr()
    spec = Path(captured.out.strip())
    assert spec.parent == paths.box_dir()
    # stdout is what a launcher consumes, so nothing else may appear on it.
    assert captured.out.splitlines() == [str(spec)]
    assert "attach with:  nv " in captured.err

    directory = spec.parent / spec.stem
    body = spec.read_text()
    assert body == f'ro = [\n    "{directory}",\n]\n'
    key = directory / "key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    assert key.read_text() == "the-key"


async def test_a_client_that_presented_a_key_names_no_session(
    running_broker: Path, repo: Path
) -> None:
    session = await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket(), key=session["key"])
    assert wire.hello == {"ok": True, "session": session["id"]}

    result = await wire.call("show", {"locations": [{"file": "src/main.c"}]})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["session"] == session["id"]

    # An explicit key still wins, so one client can serve several sessions.
    other = await admin({"cmd": "new", "root": str(repo / "src")})
    result = await wire.call(
        "show", {"session": other["key"], "locations": [{"file": "main.c"}]}
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["session"] == other["id"]
    await wire.close()


async def test_a_key_that_names_no_session_is_refused_at_connect(
    running_broker: Path, repo: Path
) -> None:
    await admin({"cmd": "new", "root": str(repo)})
    wire = Wire(*await asyncio.open_unix_connection(str(paths.agent_socket())))
    answer = await wire.present("not-a-key")
    # Better here than as an error on every call the agent goes on to make.
    assert answer == {"ok": False, "error": "no such session"}
    await wire.close()


async def test_a_client_with_no_key_is_told_what_to_do(
    running_broker: Path, repo: Path
) -> None:
    await admin({"cmd": "new", "root": str(repo)})
    wire = await Wire.connect(paths.agent_socket())
    result = await wire.call("show", {"locations": [{"file": "src/main.c"}]})
    assert result.get("isError") is True
    assert "nv new" in result["content"][0]["text"]
    await wire.close()


def test_a_key_is_read_only_where_the_agent_directory_is_not_writable(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = paths.agent_dir()
    box = paths.box_dir() / "token"
    box.mkdir(mode=0o700, parents=True)
    (box / "key").write_text("secret\n")

    # On the host the directory holds an entry per live box and none of them
    # belongs to this client.
    assert paths.box_key() is None

    # A box has it read-only, which is the same thing that stops a broker
    # starting in there.
    agent.chmod(0o500)
    try:
        assert paths.box_key() == "secret"
    finally:
        agent.chmod(0o700)
