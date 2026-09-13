# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from nvim_mcp import cli


def test_new_sends_the_root_as_seen_from_the_callers_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The broker resolves paths against its own cwd, which is wherever the
    # first `nv` happened to run. A relative root has to be made absolute
    # before it leaves this process.
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setattr(cli, "_ensure_broker", lambda: None)
    sent: list[dict] = []

    def ask(request: dict, timeout: float = 0) -> dict:
        sent.append(request)
        return {"ok": True, "id": "1", "root": request["root"], "key": "k"}

    monkeypatch.setattr(cli, "_ask", ask)
    cli.cmd_new(argparse.Namespace(root=".", clean=True, background=None))
    assert sent[0]["root"] == str(caller.resolve())


@pytest.mark.parametrize("command", ["ensure", "box"])
def test_a_root_reaches_the_command_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    # Reached through main, not by calling the function: the subcommands are
    # built in a loop, and a name bound there once shadowed the parser that
    # parses the whole command line.
    seen: list[str] = []
    monkeypatch.setattr(cli, "cmd_ensure", lambda args: seen.append(args.root) or 0)
    monkeypatch.setattr(cli, "cmd_box", lambda args: seen.append(args.root) or 0)
    assert cli.main([command, str(tmp_path)]) == 0
    assert seen == [str(tmp_path)]
