# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import zipfile
from pathlib import Path

from hatchling.builders.wheel import WheelBuilder

from nvim_mcp.nvimrpc import lua_value

ROOT = Path(__file__).resolve().parent.parent


def test_the_wheel_ships_the_lua(tmp_path: Path) -> None:
    # An editable install reads session.lua from the checkout, so only a built
    # wheel shows whether the package data is actually included.
    (wheel,) = WheelBuilder(str(ROOT)).build(directory=str(tmp_path))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "nvim_mcp/session.lua" in names
        assert archive.read("nvim_mcp/session.lua").startswith(b"-- Copyright")


def test_lua_values_omit_absent_fields() -> None:
    assert lua_value({"a": None, "b": [{"c": None, "d": 1}], "e": (None,)}) == {
        "b": [{"d": 1}],
        "e": [None],
    }
