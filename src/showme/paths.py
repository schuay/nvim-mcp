# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Locate broker sockets, locks, and persistent state.

The agent directory may be mounted into a sandbox and contains only the broker
socket and splice client. The private runtime directory contains every nvim
socket. Exposing an nvim socket would let a sandbox run arbitrary Lua on the
host.
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path


def _mkdir(path: Path) -> Path:
    """Create a private directory, tightening one left by an older version.

    ``mkdir`` applies its mode only to new directories. Existing directories
    may retain permissions that expose the human's editor socket.
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
    """Return the private directory for sockets that no sandbox may reach.

    Some MCP clients omit XDG_RUNTIME_DIR. Prefer its usual `/run/user` location
    when available so those clients find sockets created from a normal shell.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        run = Path(f"/run/user/{os.getuid()}")
        base = str(run) if run.is_dir() else f"/tmp/showme-{os.getuid()}"  # noqa: S108
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

    Each launcher binds one private key subdirectory into its sandbox. The
    parent is outside every bind specification, so a sandbox cannot enumerate
    other launch keys. Do not create the parent here because it is read-only
    inside a sandbox.
    """
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "showme-box"


def sandboxed() -> bool:
    """Return whether the agent directory is mounted read-only.

    A read-only mount identifies sandbox clients and prevents them from
    starting a broker. A writable directory identifies host clients, which can
    also reach the admin socket.
    """
    return not os.access(agent_dir(), os.W_OK)


def box_key() -> str | None:
    """Return the session key a launcher left for this client, if any.

    Host clients cannot select one entry from the directory because it contains
    keys for every live sandbox.
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
    """Return the file that admits one broker.

    Keep the lock beside the agent socket it guards. Environment-dependent
    runtime paths could otherwise admit multiple brokers for one socket.
    """
    return agent_dir() / "broker.lock"


def answering(socket_path: Path) -> bool:
    """Return whether a process accepts connections on a possibly stale socket."""
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(str(socket_path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def broker_log() -> Path:
    return state_dir() / "broker.log"


def nvim_log(sid: str) -> Path:
    return state_dir() / f"nvim-{sid}.log"
