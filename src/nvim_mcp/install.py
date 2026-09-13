# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Register the tools with an agent harness, in the human's own config file.

Every harness keeps its MCP servers somewhere different and in a different
shape, so this writes the one entry each of them wants and leaves the rest of
the file alone. It is a config file belonging to the human: nothing is written
without showing the change and asking, a copy of the file is kept beside it,
and a file this cannot parse is described rather than rewritten.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The server's name in a harness config. The tools are `show` and `read`;
#: this is what the human sees in a server list.
NAME = "nvim"


@dataclass(frozen=True)
class Harness:
    key: str
    label: str
    #: Relative to the home directory, because that is where all of them are.
    config: str
    #: The key holding servers, and the shape of one entry.
    section: str

    @property
    def path(self) -> Path:
        return Path.home() / self.config

    def entry(self, command: str) -> dict[str, Any]:
        if self.key == "opencode":
            return {"type": "local", "command": [command, "mcp"], "enabled": True}
        if self.key == "claude":
            return {"type": "stdio", "command": command, "args": ["mcp"]}
        return {"command": command, "args": ["mcp"]}


HARNESSES = {
    harness.key: harness
    for harness in (
        Harness("claude", "Claude Code", ".claude.json", "mcpServers"),
        Harness("codex", "Codex", ".codex/config.toml", "mcp_servers"),
        Harness("gemini", "Gemini CLI", ".gemini/settings.json", "mcpServers"),
        Harness("opencode", "opencode", ".config/opencode/opencode.json", "mcp"),
    )
}


def nv_command() -> str:
    """The absolute path of this `nv`, so a harness does not need it on PATH."""
    found = shutil.which(sys.argv[0]) or shutil.which("nv")
    return str(Path(found).resolve()) if found else "nv"


def opencode_path() -> Path:
    """opencode reads either spelling; take whichever the human already has."""
    jsonc = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    plain = Path.home() / ".config" / "opencode" / "opencode.json"
    return jsonc if jsonc.exists() and not plain.exists() else plain


class Unparsable(Exception):
    """The config is there but not something to rewrite safely."""


def snippet(harness: Harness, command: str) -> str:
    """The entry to add, as the human would write it themselves."""
    if harness.key == "codex":
        return f'[{harness.section}.{NAME}]\ncommand = "{command}"\nargs = ["mcp"]\n'
    return json.dumps({harness.section: {NAME: harness.entry(command)}}, indent=2)


def plan(harness: Harness, command: str) -> tuple[Path, str, str | None]:
    """Return the file, the change to show, and the text to write.

    A `None` third element means the entry is already there.
    """
    path = opencode_path() if harness.key == "opencode" else harness.path
    if harness.key == "codex":
        return (path, *_toml_plan(path, harness, command))
    return (path, *_json_plan(path, harness, command))


def _json_plan(path: Path, harness: Harness, command: str) -> tuple[str, str | None]:
    shown = snippet(harness, command)
    document: dict[str, Any] = {}
    if path.exists():
        try:
            document = json.loads(path.read_text() or "{}")
        except ValueError as e:
            raise Unparsable(f"{path} is not plain JSON ({e})") from e
        if not isinstance(document, dict):
            raise Unparsable(f"{path} is not an object")
    servers = document.setdefault(harness.section, {})
    if not isinstance(servers, dict):
        raise Unparsable(f"{path} has a {harness.section} that is not an object")
    entry = harness.entry(command)
    if servers.get(NAME) == entry:
        return shown, None
    servers[NAME] = entry
    return shown, json.dumps(document, indent=2) + "\n"


def _toml_plan(path: Path, harness: Harness, command: str) -> tuple[str, str | None]:
    block = snippet(harness, command)
    existing = path.read_text() if path.exists() else ""
    if existing:
        try:
            document = tomllib.loads(existing)
        except tomllib.TOMLDecodeError as e:
            raise Unparsable(f"{path} is not valid TOML ({e})") from e
        if NAME in document.get(harness.section, {}):
            return block, None
    # Appended rather than rewritten: tomllib only reads, and a rewrite would
    # cost the human every comment and every bit of ordering in the file.
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    return block, existing + separator + block


def apply(path: Path, text: str) -> Path | None:
    """Write the config, keeping a copy of what was there. Returns the copy."""
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".nvim-mcp.bak")
        shutil.copyfile(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return backup


#: Terminals this can name, and what each needs before a command. Only one
#: that is actually installed is ever suggested.
TERMINALS = {
    "ghostty": "ghostty -e",
    "kitty": "kitty",
    "alacritty": "alacritty -e",
    "wezterm": "wezterm start --",
    "foot": "foot",
    "konsole": "konsole -e",
    "gnome-terminal": "gnome-terminal --",
    "xterm": "xterm -e",
}


def terminal_suggestion() -> str | None:
    """A value for NVIM_MCP_TERMINAL, from the terminal the human is in."""
    program = (os.environ.get("TERM_PROGRAM") or "").lower()
    candidates = [program, os.environ.get("TERMINAL", ""), *TERMINALS]
    for candidate in candidates:
        name = Path(candidate).name
        if name in TERMINALS and shutil.which(name):
            return TERMINALS[name]
    return None


def shell_rc() -> Path | None:
    """The file an export belongs in, for the shells where it is one line."""
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "bash":
        return Path.home() / ".bashrc"
    if shell == "zsh":
        return Path.home() / ".zshrc"
    return None
