# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import admin
from mcpwire import Wire

from showme import paths
from showme.models import ReadResult, ShowResult

pytestmark = pytest.mark.nvim


async def session_and_wire(repo: Path) -> tuple[dict, Wire]:
    session = await admin({"cmd": "new", "root": str(repo), "clean": True})
    wire = await Wire.connect(paths.agent_socket())
    result = await wire.call(
        "show",
        {
            "session": session["key"],
            "locations": [{"file": "src/main.c", "line": 3, "text": "note"}],
        },
    )
    assert not result.get("isError"), result
    return session, wire


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"what": "typo"}, "what"),
        ({"what": "marks", "unexpected": True}, "unexpected"),
        ({"what": "range", "file": "src/main.c", "start_line": "3"}, "start_line"),
        ({"what": "range", "file": "src/main.c", "start_line": 0}, "start_line"),
        ({"what": "marks", "ack": "1"}, "ack"),
    ],
)
async def test_invalid_read_arguments_are_refused_before_reaching_nvim(
    running_broker: Path, repo: Path, arguments: dict, field: str
) -> None:
    session, wire = await session_and_wire(repo)
    result = await wire.call("read", {"session": session["key"], **arguments})
    assert result.get("isError") is True, result
    assert field in result["content"][0]["text"]
    await wire.close()


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"locations": []}, "locations"),
        ({"locations": [{"file": "src/main.c"}], "frame": "peek"}, "frame"),
        ({"locations": [{"file": "src/main.c", "line": "3"}]}, "line"),
        ({"locations": [{"file": "src/main.c", "col": 1}]}, "col"),
        ({"locations": [{"file": "src/main.c"}], "focus": "yes"}, "focus"),
        # nvim would collapse this to line 30 and report a highlight, so it is
        # caught here where the caller can still see which argument was wrong.
        (
            {"locations": [{"file": "src/main.c", "line": 30, "end_line": 10}]},
            "end_line",
        ),
    ],
)
async def test_invalid_show_arguments_are_refused(
    running_broker: Path, repo: Path, arguments: dict, field: str
) -> None:
    session, wire = await session_and_wire(repo)
    result = await wire.call("show", {"session": session["key"], **arguments})
    assert result.get("isError") is True, result
    assert field in result["content"][0]["text"]
    await wire.close()


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({}, (1, 20)),
        ({"start_line": 5}, (5, 20)),
        ({"end_line": 5}, (1, 5)),
        ({"start_line": 5, "end_line": 7}, (5, 7)),
        ({"start_line": 15, "end_line": 99}, (15, 20)),
    ],
)
async def test_range_bounds_default_when_omitted(
    running_broker: Path, repo: Path, bounds: dict, expected: tuple[int, int]
) -> None:
    session, wire = await session_and_wire(repo)
    result = await wire.call(
        "read",
        {"session": session["key"], "what": "range", "file": "src/main.c", **bounds},
    )
    assert not result.get("isError"), result
    span = result["structuredContent"]["range"]
    assert (span["start_line"], span["end_line"]) == expected
    assert len(span["text"].splitlines()) == expected[1] - expected[0] + 1
    await wire.close()


async def test_tools_declare_output_schemas_and_results_match_them(
    running_broker: Path, repo: Path
) -> None:
    session, wire = await session_and_wire(repo)
    tools = {t["name"]: t for t in (await wire.request("tools/list"))["tools"]}
    assert set(tools) == {"show", "read"}
    for tool in tools.values():
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "properties" in tool["outputSchema"]

    shown = await wire.call(
        "show",
        {
            "session": session["key"],
            "locations": [{"file": "src/main.c", "line": 2, "end_line": 4}],
        },
    )
    assert shown["structuredContent"] == json.loads(shown["content"][0]["text"])
    ShowResult.model_validate(shown["structuredContent"])
    for what in ("marks", "cursor", "tabs"):
        got = await wire.call("read", {"session": session["key"], "what": what})
        assert not got.get("isError"), got
        ReadResult.model_validate(got["structuredContent"])
    await wire.close()
