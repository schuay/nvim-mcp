# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest

from showme import store


def _aside(reason: str) -> list[Path]:
    return sorted(store.path().parent.glob(f"sessions.json.{reason}-*"))


def test_state_round_trips(runtime: Path) -> None:
    store.save([{"sid": "1", "key": "k"}])
    assert store.load() == [{"sid": "1", "key": "k"}]


def test_a_file_from_another_version_is_kept_not_overwritten(runtime: Path) -> None:
    original = {"version": store.VERSION + 1, "sessions": [{"sid": "7"}]}
    store.path().write_text(json.dumps(original))

    assert store.load() == []
    store.save([])

    # The next save must not have replaced what the newer version wrote.
    (kept,) = _aside(f"v{store.VERSION + 1}")
    assert json.loads(kept.read_text()) == original


@pytest.mark.parametrize("text", ["{not json", '{"version": 1}', "[]"])
def test_a_malformed_file_is_kept_not_overwritten(runtime: Path, text: str) -> None:
    store.path().write_text(text)
    assert store.load() == []
    store.save([])
    (kept,) = _aside("malformed")
    assert kept.read_text() == text
