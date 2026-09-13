# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Serve MCP to one client connection.

The tool list is the broker's entire exposed surface, so nothing here writes: a
client can put code in front of the human and read what the human is looking
at, and nothing else. Sessions are addressed by their secret key, because the
broker cannot tell one sandboxed client from another.

Every read an agent asks for is clamped to the session root. A mark is the one
exception: only the human makes one, and `:Ask` on a range is them handing it
over deliberately.
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
            "description": "One-line note, shown above the line and in the quickfix list",
        },
    },
    "required": ["file"],
    "additionalProperties": False,
}

SESSION_ARG = {"type": "string", "description": "Session key, as printed by `nv new`"}

SHOW_TOOL = types.Tool(
    name="show",
    title="Show code to the human",
    description=(
        "Open locations in the human's nvim session: a tab per file, a quickfix "
        "list of the positions, and a highlight over any range, with each note "
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
            "session": SESSION_ARG,
        },
        "required": ["locations", "session"],
        "additionalProperties": False,
    },
)

READ_TOOL = types.Tool(
    name="read",
    title="Read what the human is looking at",
    description=(
        "Read the human's session. 'marks' collects the ranges they handed over "
        "with :Ask, each with their question; call it when they refer to "
        "something they marked, and pass the ids back in 'ack' once you have "
        "answered them, or they stay pending and come back. 'cursor' is where they are now and what they "
        "last selected. 'range' reads an open buffer, including edits they have "
        "not saved. 'tabs' lists what is open. Everything but 'marks' is limited "
        "to the session root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "what": {"type": "string", "enum": ["marks", "cursor", "range", "tabs"]},
            "file": {"type": "string", "description": "With what='range'"},
            "start_line": {"type": "integer", "description": "1-based, inclusive"},
            "end_line": {"type": "integer", "description": "1-based, inclusive"},
            "ack": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Mark ids you have now dealt with. Until you acknowledge a "
                    "mark it stays pending and is handed to you again."
                ),
            },
            "session": SESSION_ARG,
        },
        "required": ["what", "session"],
        "additionalProperties": False,
    },
)

SessionLookup = Callable[[str], Session | None]


def pending(session: Session) -> list[dict[str, Any]]:
    """Return the questions nobody has answered yet.

    A mark stays pending until an agent acknowledges it, not until some agent
    reads it. Reading is not answering: a client that reads and then
    disconnects, or a second agent looking on, must not be what makes the
    human's question disappear. A reconnecting client is the common case, since
    each one starts a new MCP session.
    """
    return [mark for mark in session.marks if not mark.get("acked")]


def _envelope(session: Session, **extra: Any) -> dict[str, Any]:
    """Add the state every result carries.

    An agent learns about a waiting question from any call it happens to make,
    so noticing one costs nothing extra.
    """
    return {
        "session": session.sid,
        "attach_cmd": f"nv {session.sid}",
        "marks_pending": len(pending(session)),
        **extra,
    }


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
        # nvim reports how many UIs it has while applying the show, which saves
        # a second round trip for the same fact.
        attached = bool(result.get("uis"))
        for path in result.get("moved", []):
            refused.append({"file": path, "reason": "path changed while opening"})

    return _envelope(session, attached=attached, opened=opened, refused=refused)


async def _read(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    what = params["what"]
    acknowledged = set(params.get("ack") or [])
    for mark in session.marks:
        if mark.get("id") in acknowledged:
            mark["acked"] = True
    options: dict[str, Any] = {}
    if what == "range":
        if not params.get("file"):
            raise Refused("read(what='range') needs a file")
        options = {
            "file": str(session.root.resolve(params["file"])),
            "start_line": params.get("start_line"),
            "end_line": params.get("end_line"),
        }

    result = await session.read(what, options)
    attached = await session.attached()

    if what == "marks":
        return _envelope(session, attached=attached, marks=pending(session))

    payload: dict[str, Any] = {}
    if what == "cursor":
        cursor = result.get("cursor") or {}
        payload["cursor"] = _inside(session, cursor)
    elif what == "range":
        payload["range"] = (
            _inside(session, result.get("range") or {})
            if result.get("range")
            else {"open": False, "file": params.get("file")}
        )
    elif what == "tabs":
        payload["buffers"] = [
            state
            for state in result.get("buffers") or []
            if _within(session, state.get("file"))
        ]
    return _envelope(session, attached=attached, **payload)


def _within(session: Session, file: str | None) -> bool:
    if not file:
        return False
    try:
        return session.root.contains(session.root.resolve(file))
    except Refused:
        return False


def _inside(session: Session, state: dict[str, Any]) -> dict[str, Any]:
    """Blank out content the agent may not read.

    The human can navigate anywhere; the agent may only be told about what is
    inside the root it was given.
    """
    if _within(session, state.get("file")):
        return state
    return {"refused": "outside the session root", "file": state.get("file")}


def build(lookup: SessionLookup, save: Callable[[], None] | None = None) -> Server:
    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[SHOW_TOOL, READ_TOOL])

    async def on_call_tool(
        _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        session = lookup(str(arguments.get("session", "")))
        if session is None:
            return _error(
                "no such session. Ask the human to run `nv new <root>` and paste the key it prints."
            )
        try:
            if params.name == "show":
                payload = await _show(session, arguments)
            elif params.name == "read":
                payload = await _read(session, arguments)
            else:
                return _error(f"unknown tool: {params.name}")
        except Refused as e:
            return _error(str(e))
        except Exception as e:
            log.exception("%s failed", params.name)
            return _error(str(e))
        if save is not None:
            save()
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
