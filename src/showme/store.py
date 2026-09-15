# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Persist session reviews across broker restarts.

The state file records frames, human replies, and session keys. Store it in the
private, ownership-checked state directory because each key grants access to a
session.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .paths import state_dir

log = logging.getLogger(__name__)

#: 2: notes live in frames.
#: 3: sessions belong to conversations. Older state mixes conversations' frames.
#: 4: save socket paths because key rotation prevents deriving them from keys.
VERSION = 4


def path() -> Path:
    return state_dir() / "sessions.json"


def load() -> list[dict[str, Any]]:
    """Return the saved sessions, or none if the file cannot be honoured.

    Move unreadable or unsupported state aside before returning an empty list;
    otherwise the next save would overwrite recoverable sessions. Refuse to
    start if the file cannot be moved.
    """
    target = path()
    try:
        text = target.read_text()
    except FileNotFoundError:
        return []
    except OSError as e:
        _set_aside(target, "unreadable", e)
        return []
    try:
        document = json.loads(text)
        version = document["version"]
        sessions = document["sessions"]
    except (ValueError, TypeError, KeyError) as e:
        _set_aside(target, "malformed", e)
        return []
    if version != VERSION:
        _set_aside(target, f"v{version}", None)
        return []
    if not isinstance(sessions, list):
        _set_aside(target, "malformed", None)
        return []
    return sessions


def _set_aside(target: Path, reason: str, cause: Exception | None) -> None:
    aside = target.with_name(f"{target.name}.{reason}-{int(time.time())}")
    target.rename(aside)
    log.warning("moved session state to %s: %s", aside, cause or reason)


def save(sessions: list[dict[str, Any]]) -> None:
    """Replace the state file, never leaving a half-written one behind."""
    target = path()
    document = json.dumps({"version": VERSION, "sessions": sessions}, indent=2)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".sessions-")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
    # The rename is durable only once the directory entry is.
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
