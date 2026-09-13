# Copyright 2026 The nvim-mcp developers
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
from pathlib import Path
from typing import Any

from .paths import state_dir

log = logging.getLogger(__name__)

VERSION = 1


def path() -> Path:
    return state_dir() / "sessions.json"


def load() -> list[dict[str, Any]]:
    try:
        document = json.loads(path().read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # State the broker cannot read is state it cannot honour. Say so and
        # start empty rather than refusing to run at all.
        log.exception("ignoring unreadable session state at %s", path())
        return []
    if document.get("version") != VERSION:
        log.info("ignoring session state written by another version")
        return []
    sessions = document.get("sessions")
    return sessions if isinstance(sessions, list) else []


def save(sessions: list[dict[str, Any]]) -> None:
    """Replace the state file, never leaving a half-written one behind."""
    target = path()
    document = json.dumps({"version": VERSION, "sessions": sessions}, indent=2)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".sessions-")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(document)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
