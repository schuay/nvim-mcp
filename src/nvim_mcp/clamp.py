# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Resolve a client-supplied path against a session root.

This is the security boundary for a sandboxed client: it may read only inside
the root its session was created for. Symlinks resolve before the check, so a
link planted inside the root cannot point at a credential elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class Refused(ValueError):
    """A path a client may not open, carrying the reason to report back."""


@dataclass(frozen=True)
class Root:
    path: Path

    @classmethod
    def of(cls, raw: str | Path) -> Root:
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_dir():
            raise Refused(f"root is not a directory: {resolved}")
        return cls(resolved)

    def resolve(self, raw: str) -> Path:
        """Return the file `raw` names inside this root, or raise Refused.

        `~` is not expanded: a client's path is data, not shell input, and
        expansion would resolve outside the root by design.
        """
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        resolved = candidate.resolve()
        if resolved != self.path and self.path not in resolved.parents:
            raise Refused("outside the session root")
        if not resolved.is_file():
            raise Refused("not a regular file")
        return resolved
