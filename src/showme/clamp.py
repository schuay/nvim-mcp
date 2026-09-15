# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Resolve client paths and enforce the access granted by a session key.

A sandbox key may read only inside its session root. Resolve symlinks before
checking containment so a link inside the root cannot expose a file outside
it. Reject outside paths before statting them so the error does not reveal
whether they exist. Errors for paths inside the root include the resolved path
to expose an incorrect root or relative path.

Host keys use `Anywhere`, which resolves relative paths against the session
root without enforcing it as a boundary. Both implementations expose the same
interface so callers cannot bypass the key's access policy.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


class Refused(ValueError):
    """Report why a client path cannot be opened."""


@dataclass(frozen=True)
class Root:
    path: Path

    #: Access granted by a key bound to this root, for broker logging.
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

        Do not expand `~`: client paths are data, and expansion could escape
        the root.
        """
        # Check containment before stat so errors do not reveal whether an
        # outside path exists. Resolution errors reject only this path.
        resolved = self.locate(raw)
        try:
            mode = resolved.stat().st_mode
        except FileNotFoundError:
            # Name the resolved path to distinguish a missing file from a
            # directory or device and expose an incorrect relative path.
            raise Refused(f"no such file: {resolved}") from None
        except OSError as e:
            raise Refused(f"cannot resolve: {e.strerror or e}") from e
        if not stat.S_ISREG(mode):
            raise Refused(f"not a regular file: {resolved}")
        return resolved

    def locate(self, raw: str) -> Path:
        """Return the path `raw` names inside this root, existing or not.

        This supports open buffers whose files were renamed or deleted. It
        still enforces the root boundary but leaves existence checks to the
        caller.
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
    """Resolve paths for a host key without enforcing the root boundary.

    A host client already has the human's file access. The root still controls
    relative paths and nvim's working directory, which keeps notes and `:Ref`
    paths consistent.
    """

    scope = "open"

    def locate(self, raw: str) -> Path:
        return self._under(raw)

    def contains(self, resolved: Path) -> bool:
        return True
