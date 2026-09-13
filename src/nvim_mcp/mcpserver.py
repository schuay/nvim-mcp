# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Serve MCP to one client connection.

The tool list is the broker's entire exposed surface, so nothing here writes: a
client can put code in front of the human and nothing else. Sessions are
addressed by their secret key, because the broker cannot tell one sandboxed
client from another.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage

from .clamp import Refused
from .session import Location, Session

log = logging.getLogger(__name__)

#: A review the human has to walk, not a dump. Beyond this the quickfix list
#: stops being something anyone reads to the end.
LOCATION_LIMIT = 50

LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {
            "type": "string",
            "description": "Path, absolute or relative to the session root",
        },
        "line": {"type": "integer", "description": "1-based line; defaults to 1"},
        "end_line": {
            "type": "integer",
            "description": "Last line of a highlighted range",
        },
        "text": {
            "type": "string",
            "description": "One-line label shown in the quickfix list",
        },
    },
    "required": ["file"],
    "additionalProperties": False,
}

SHOW_TOOL = types.Tool(
    name="show",
    title="Show code to the human",
    description=(
        "Open locations in the human's nvim session: a tab per file, a quickfix "
        "list of the positions, a highlight over any range, and each note "
        "rendered above its line. Batch every location you are discussing into "
        "one call. Replaces the previous show; never closes a tab."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "locations": {
                "type": "array",
                "items": LOCATION_SCHEMA,
                "minItems": 1,
                "maxItems": LOCATION_LIMIT,
            },
            "title": {"type": "string", "description": "Label for the quickfix list"},
            "focus": {
                "type": "boolean",
                "description": "Jump the human's view to the first location. Default true.",
            },
            "session": {
                "type": "string",
                "description": "Session key, as printed by `nv new`",
            },
        },
        "required": ["locations", "session"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "session": {"type": "string"},
            "attached": {"type": "boolean"},
            "attach_cmd": {"type": "string"},
            "marks_pending": {"type": "integer"},
            "opened": {"type": "array", "items": {"type": "string"}},
            "refused": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["session", "attached", "marks_pending", "opened", "refused"],
    },
)

SessionLookup = Callable[[str], Session | None]


async def _show(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    locations, refused = [], []
    requested = params["locations"]
    for raw in requested[:LOCATION_LIMIT]:
        try:
            # Resolve once. The path that goes to nvim is the one that passed
            # the clamp, so there is no second resolution to disagree with it.
            resolved = session.root.resolve(raw["file"])
        except Refused as e:
            # One bad path does not spoil the rest of a review.
            session.refusals += 1
            refused.append({"file": raw["file"], "reason": str(e)})
            continue
        locations.append(
            Location(
                file=resolved,
                line=raw.get("line", 1),
                end_line=raw.get("end_line"),
                text=raw.get("text", ""),
            )
        )
    for raw in requested[LOCATION_LIMIT:]:
        refused.append(
            {"file": raw["file"], "reason": f"over the {LOCATION_LIMIT} location limit"}
        )

    opened: list[str] = []
    attached = await session.attached()
    if locations:
        result = await session.show(
            locations,
            title=params.get("title", "agent"),
            focus=params.get("focus", True),
        )
        opened = result.get("opened", [])
        # nvim reports how many UIs it has while it is applying the show, which
        # saves a second round trip for the same fact.
        attached = bool(result.get("uis"))
        for path in result.get("moved", []):
            refused.append({"file": path, "reason": "path changed while opening"})

    return {
        "session": session.sid,
        "attached": attached,
        "attach_cmd": f"nv {session.sid}",
        "marks_pending": 0,
        "opened": opened,
        "refused": refused,
    }


def build(lookup: SessionLookup) -> Server:
    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[SHOW_TOOL])

    async def on_call_tool(
        _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name != "show":
            return _error(f"unknown tool: {params.name}")
        session = lookup(str(arguments.get("session", "")))
        if session is None:
            return _error(
                "no such session. Ask the human to run `nv new <root>` and paste the key it prints."
            )
        try:
            payload = await _show(session, arguments)
        except Exception as e:
            log.exception("show failed")
            return _error(str(e))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

    return Server("nvim", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


async def serve(
    reader: Any,
    writer: Any,
    server: Server,
    on_done: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run one MCP session over an asyncio stream pair.

    MCP defines stdio and HTTP transports, not a socket one. This is stdio's
    framing on a socket, so the client side stays a byte splice with no schema
    of its own.
    """
    read_w, read_r = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_w, write_r = anyio.create_memory_object_stream[SessionMessage](0)

    async def pump_in() -> None:
        async with read_w:
            while line := await reader.readline():
                try:
                    message = types.jsonrpc_message_adapter.validate_json(
                        line, by_name=False
                    )
                except Exception as e:
                    await read_w.send(e)
                    continue
                await read_w.send(SessionMessage(message))

    async def pump_out() -> None:
        async with write_r:
            async for message in write_r:
                body = message.message.model_dump_json(
                    by_alias=True, exclude_unset=True
                )
                writer.write(body.encode() + b"\n")
                await writer.drain()

    async with anyio.create_task_group() as tg:
        tg.start_soon(pump_in)
        tg.start_soon(pump_out)
        try:
            await server.run(read_r, write_w, server.create_initialization_options())
        finally:
            tg.cancel_scope.cancel()
            if on_done is not None:
                await on_done()
