# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""`nv install <harness>` writes one entry into a config file that belongs to
the human, and has to leave the rest of it alone."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from nvim_mcp import install


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("key", sorted(install.HARNESSES))
def test_every_harness_gets_an_entry_naming_this_nv(home: Path, key: str) -> None:
    harness = install.HARNESSES[key]
    path, shown, text = install.plan(harness, "/opt/nv")
    assert text is not None
    assert "/opt/nv" in shown
    install.apply(path, text)

    # And a second run is a no-op rather than a second entry.
    _, _, again = install.plan(harness, "/opt/nv")
    assert again is None


def test_the_rest_of_a_config_survives(home: Path) -> None:
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"ui": {"theme": "dark"}, "mcpServers": {"x": {}}}))

    path, _, text = install.plan(install.HARNESSES["gemini"], "/opt/nv")
    assert text is not None
    backup = install.apply(path, text)

    document = json.loads(settings.read_text())
    assert document["ui"] == {"theme": "dark"}
    assert sorted(document["mcpServers"]) == ["nvim", "x"]
    # What was there is kept beside it, because this is not our file.
    assert backup is not None
    assert json.loads(backup.read_text())["mcpServers"] == {"x": {}}


def test_a_toml_config_is_appended_to_not_rewritten(home: Path) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        '# keep me\nmodel = "gpt-5.5"\n\n[mcp_servers.other]\ncommand = "x"\n'
    )

    path, _, text = install.plan(install.HARNESSES["codex"], "/opt/nv")
    assert text is not None
    install.apply(path, text)

    body = config.read_text()
    # tomllib only reads, and a rewrite would cost every comment in the file.
    assert body.startswith("# keep me\n")
    servers = tomllib.loads(body)["mcp_servers"]
    assert servers["other"]["command"] == "x"
    assert servers["nvim"] == {"command": "/opt/nv", "args": ["mcp"]}


def test_a_config_this_cannot_parse_is_described_not_rewritten(home: Path) -> None:
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{\n  // opinions\n  "ui": {}\n}\n')
    before = settings.read_text()

    with pytest.raises(install.Unparsable):
        install.plan(install.HARNESSES["gemini"], "/opt/nv")
    assert settings.read_text() == before
    # There is still something to tell the human to paste.
    assert "nvim" in install.snippet(install.HARNESSES["gemini"], "/opt/nv")


def test_the_terminal_suggestion_is_one_that_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install.shutil,
        "which",
        lambda name: "/usr/bin/kitty" if name == "kitty" else None,
    )
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.delenv("TERMINAL", raising=False)
    # Asked for ghostty, which is not here; kitty is.
    assert install.terminal_suggestion() == "kitty"

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert install.terminal_suggestion() is None
