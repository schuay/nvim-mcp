# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Keep sessions across broker restarts.

A session holds a review the human has not read yet, so losing the broker must
not lose the review. The file records what each session shows and what the
human has handed back; the nvim processes themselves are started again from it.

Session keys are in here, and a key authorizes a client to use its session.
The file is written inside the state directory, which is created private and
checked for ownership.
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
VERSION = 2


def path() -> Path:
    return state_dir() / "sessions.json"


def load() -> list[dict[str, Any]]:
    """Return the saved sessions, or none if the file cannot be honoured.

    A file this broker cannot read or does not understand is moved aside, not
    ignored: the next save would otherwise replace it with an empty document
    and the sessions in it would be gone for good. If it cannot be moved, the
    error propagates and the broker does not start.
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
