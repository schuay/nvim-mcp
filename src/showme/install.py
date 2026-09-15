# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Register the tools in an agent harness's configuration.

Each harness uses a different path and MCP server format. Preserve unrelated
configuration, preview and confirm changes, and back up existing files. Show a
manual snippet when a configuration cannot be parsed safely.
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

#: The name shown for this server in harness configuration and server lists.
NAME = "showme"


@dataclass(frozen=True)
class Harness:
    key: str
    label: str
    config: str
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


def showme_command() -> str:
    """Return an absolute executable path so harnesses do not depend on PATH."""
    found = shutil.which(sys.argv[0]) or shutil.which("showme")
    return str(Path(found).resolve()) if found else "showme"


def opencode_path() -> Path:
    """Use either opencode filename, preferring the existing variant."""
    jsonc = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    plain = Path.home() / ".config" / "opencode" / "opencode.json"
    return jsonc if jsonc.exists() and not plain.exists() else plain


class Unparsable(Exception):
    """Prevent an unsafe configuration rewrite."""


def snippet(harness: Harness, command: str) -> str:
    """Render the configuration entry for manual installation."""
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
    # Append because tomllib cannot preserve comments or ordering in a rewrite.
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    return block, existing + separator + block


def apply(path: Path, text: str) -> Path | None:
    """Write the configuration and return the backup path, if one was made."""
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".showme.bak")
        shutil.copyfile(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return backup


#: Supported terminal commands; suggestions include only installed terminals.
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
    """Suggest SHOWME_TERMINAL from the current terminal environment."""
    program = (os.environ.get("TERM_PROGRAM") or "").lower()
    candidates = [program, os.environ.get("TERMINAL", ""), *TERMINALS]
    for candidate in candidates:
        name = Path(candidate).name
        if name in TERMINALS and shutil.which(name):
            return TERMINALS[name]
    return None


def shell_rc() -> Path | None:
    """Return the startup file for shells that use a one-line export."""
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "bash":
        return Path.home() / ".bashrc"
    if shell == "zsh":
        return Path.home() / ".zshrc"
    return None
