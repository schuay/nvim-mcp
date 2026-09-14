# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Resolve a client-supplied path against a session root.

This is the security boundary for a sandboxed client: it may read only inside
the root its session was created for. Symlinks resolve before the check, so a
link planted inside the root cannot point at a credential elsewhere, and a path
outside it is refused before anything is stat'd, so the refusals cannot be read
as an answer to whether a file elsewhere exists.

The boundary binds a key rather than a session, which is why `Anywhere` is
here too: a client that took its key over the admin socket runs as the human
and there is nothing to confine it to. Both kinds resolve the same way, so the
callers do not know which one they hold.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


class Refused(ValueError):
    """A path a client may not open, carrying the reason to report back."""


@dataclass(frozen=True)
class Root:
    path: Path

    #: Which reach a key bound to this one has. The broker logs it: a
    #: connection says nothing about whether its client is sandboxed, and the
    #: key it presented is the closest thing to an answer.
    scope = "root"

    @classmethod
    def of(cls, raw: str | Path) -> Root:
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError as e:
            raise Refused(f"cannot resolve root {raw}: {e}") from e
        if not resolved.is_dir():
            raise Refused(f"root is not a directory: {resolved}")
        return cls(resolved)

    def resolve(self, raw: str) -> Path:
        """Return the file `raw` names inside this root, or raise Refused.

        `~` is not expanded: a client's path is data, not shell input, and
        expansion would resolve outside the root by design.
        """
        # The root check first: whether a path outside the root exists is not
        # this client's to learn. A symlink loop or an unreadable component
        # refuses this one path rather than failing the whole call.
        resolved = self.locate(raw)
        try:
            mode = resolved.stat().st_mode
        except FileNotFoundError:
            # Distinct from a directory or a device. A client that mistook the
            # root spells a real file wrong, and needs to be told which it is.
            raise Refused("no such file") from None
        except OSError as e:
            raise Refused(f"cannot resolve: {e.strerror or e}") from e
        if not stat.S_ISREG(mode):
            raise Refused("not a regular file")
        return resolved

    def locate(self, raw: str) -> Path:
        """Return the path `raw` names inside this root, existing or not.

        For a buffer the human has open: an agent that renamed or deleted the
        file on disk must still be able to read what they are looking at. The
        root check is the boundary and is unchanged; only the assertion that
        something is there is left to the caller.
        """
        resolved = self._under(raw)
        if resolved != self.path and self.path not in resolved.parents:
            raise Refused("outside the session root")
        return resolved

    def _under(self, raw: str) -> Path:
        """Return where `raw` points, taking a relative path against this root."""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        try:
            return candidate.resolve()
        except OSError as e:
            raise Refused(f"cannot resolve: {e.strerror or e}") from e

    def contains(self, resolved: Path) -> bool:
        return resolved == self.path or self.path in resolved.parents


class Anywhere(Root):
    """A root that is a base to resolve against and not a boundary.

    What the key issued over the admin socket reaches. That client runs as the
    human: it reads their files with its own tools and edits them there, so
    refusing it the header next door or the sibling worktree confines nothing
    and only makes the tools useless for the work they are for. The path still
    matters -- it is what a relative path is taken against, and nvim's cwd, so
    a note and a `:Ref` keep naming files the way the two of them do.
    """

    scope = "open"

    def locate(self, raw: str) -> Path:
        return self._under(raw)

    def contains(self, resolved: Path) -> bool:
        return True
