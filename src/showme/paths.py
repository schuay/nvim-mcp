# Copyright 2026 The showme developers
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
import stat
from pathlib import Path


def _mkdir(path: Path) -> Path:
    """Create a private directory, tightening one left by an older version.

    ``mkdir`` applies its mode only when it creates the directory, so a
    directory already there keeps whatever permissions it has. These hold a
    socket that talks to the human's editor.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"not a directory: {path}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"directory is not owned by this user: {path}")
    if info.st_mode & 0o077:
        path.chmod(0o700)
    return path


def runtime_dir() -> Path:
    """Return the private directory for sockets that no sandbox may reach."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/showme-{os.getuid()}"  # noqa: S108
    return _mkdir(Path(base) / "showme")


def agent_dir() -> Path:
    """Return the directory a sandbox binds read-only to reach the broker."""
    base = os.environ.get("SHOWME_AGENT_DIR")
    if base:
        return _mkdir(Path(base))
    share = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return _mkdir(Path(share) / "showme")


def box_dir() -> Path:
    """Return the directory holding one entry per sandbox launch.

    A launcher leaves a session key in a private subdirectory and binds that
    subdirectory, and only it, into the box it is starting. The parent is
    deliberately somewhere no bind spec mounts, so a box sees its own entry and
    cannot enumerate anyone else's. Not created here: inside a box this path is
    read-only.
    """
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "showme-box"


def sandboxed() -> bool:
    """Whether this client is behind a sandbox mount rather than on the host.

    The agent directory is bind-mounted read-only into a box, which is the
    same property that stops a broker starting in there. A client that can
    write it can also reach the admin socket and everything else this user
    owns, so there is nothing a sandbox is keeping from it.
    """
    return not os.access(agent_dir(), os.W_OK)


def box_key() -> str | None:
    """Return the session key a launcher left for this client, if any.

    Only a sandboxed client takes one: on the host the directory holds an
    entry per live box, none of which belongs to the client asking.
    """
    if not sandboxed():
        return None
    keys = sorted(box_dir().glob("*/key"))
    if len(keys) != 1:
        return None
    try:
        return keys[0].read_text().strip() or None
    except OSError:
        return None


def state_dir() -> Path:
    """Return the directory holding session state that outlives the broker."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return _mkdir(Path(base) / "showme")


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


def broker_log() -> Path:
    return state_dir() / "broker.log"


def nvim_log(sid: str) -> Path:
    return state_dir() / f"nvim-{sid}.log"
