# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Locate the broker's sockets, lock, and state.

Two directories with different exposure. The agent directory is meant to be
bind-mounted into a sandbox, so it holds only the broker's own socket and the
splice client. Every nvim listen socket stays in the runtime directory, which is
never mounted: raw nvim RPC runs arbitrary Lua, so an nvim socket reachable from
a sandbox hands out the host.
"""

from __future__ import annotations

import os
from pathlib import Path


def _mkdir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    """Return the private directory for sockets that no sandbox may reach."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nvim-mcp-{os.getuid()}"  # noqa: S108
    return _mkdir(Path(base) / "nvim-mcp")


def agent_dir() -> Path:
    """Return the directory a sandbox binds read-only to reach the broker."""
    base = os.environ.get("NVIM_MCP_AGENT_DIR")
    if base:
        return _mkdir(Path(base))
    share = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return _mkdir(Path(share) / "nvim-mcp")


def state_dir() -> Path:
    """Return the directory holding session state that outlives the broker."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return _mkdir(Path(base) / "nvim-mcp")


def admin_socket() -> Path:
    return runtime_dir() / "admin.sock"


def agent_socket() -> Path:
    return agent_dir() / "agent.sock"


def splice_script() -> Path:
    return agent_dir() / "splice.py"


def nvim_socket(session_key: str) -> Path:
    return runtime_dir() / f"nvim-{session_key}.sock"


def lock_path() -> Path:
    return runtime_dir() / "broker.lock"
