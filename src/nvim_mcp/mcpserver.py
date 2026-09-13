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
from pydantic import ValidationError

from . import models
from .clamp import Refused
from .models import (
    Buffer,
    Cursor,
    Envelope,
    FrameSummary,
    Mark,
    NotOpen,
    OutsideRoot,
    Range,
    ReadRequest,
    ReadResult,
    Refusal,
    ShowRequest,
    ShowResult,
)
from .session import Location, Session

log = logging.getLogger(__name__)

SHOW_TOOL = types.Tool(
    name="show",
    title="Show code to the human",
    description=(
        "Open locations in the human's nvim session: a tab per file, a quickfix "
        "list of the positions, and a highlight over any range, with each note "
        "rendered above its line under an id like A2 that you and the human "
        "can both say. Batch every location you are discussing into one call. "
        "Notes live in frames: the default replaces the top frame, push starts "
        "a frame for a digression, pop drops it when answered. Never closes a "
        "tab."
    ),
    input_schema=models.schema(ShowRequest),
    output_schema=models.schema(ShowResult),
)

READ_TOOL = types.Tool(
    name="read",
    title="Read what the human is looking at",
    description=(
        "Read the human's session. 'marks' collects the ranges they handed over "
        "with :Ask, each with their question; call it when they refer to "
        "something they marked, and pass the ids back in 'ack' once you have "
        "answered them, or they stay pending and come back. 'cursor' is where "
        "they are now and what they last selected. 'range' reads an open "
        "buffer, including edits they have not saved. 'tabs' lists what is "
        "open. Everything but 'marks' is limited to the session root."
    ),
    input_schema=models.schema(ReadRequest),
    output_schema=models.schema(ReadResult),
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


def _envelope(session: Session, attached: bool) -> dict[str, Any]:
    return {
        "session": session.sid,
        "attach_cmd": f"nv {session.sid}",
        "marks_pending": len(pending(session)),
        "attached": attached,
    }


async def _show(session: Session, request: ShowRequest) -> ShowResult:
    locations, refused = [], []
    for spec in request.locations:
        try:
            # Resolve once. The path that goes to nvim is the one that passed
            # the clamp, so there is no second resolution to disagree with it.
            resolved = session.root.resolve(spec.file)
        except Refused as e:
            # One bad path does not spoil the rest of a review.
            refused.append(Refusal(file=spec.file, reason=str(e)))
            continue
        locations.append(
            Location(
                file=resolved, line=spec.line, end_line=spec.end_line, text=spec.text
            )
        )

    opened: list[str] = []
    ids: list[str] = []
    frame = session.frames[-1].letter if session.frames else None
    attached = await session.attached()
    if locations or request.frame == "pop":
        result = await session.show(
            locations, title=request.title, focus=request.focus, frame=request.frame
        )
        opened = result.get("opened", [])
        ids = result["ids"]
        frame = result["frame"]
        # nvim reports how many UIs it has while applying the show, which saves
        # a second round trip for the same fact.
        attached = bool(result.get("uis"))

    return ShowResult(
        **_envelope(session, attached),
        frame=frame,
        ids=ids,
        frames=[
            FrameSummary(letter=f.letter, title=f.title, notes=len(f.notes))
            for f in session.frames
        ],
        opened=opened,
        refused=refused,
    )


async def _read(session: Session, request: ReadRequest) -> ReadResult:
    acknowledged = set(request.ack)
    for mark in session.marks:
        if mark.get("id") in acknowledged:
            mark["acked"] = True
    options: dict[str, Any] = {}
    if request.what == "range":
        if not request.file:
            raise Refused("read(what='range') needs a file")
        options = {
            "file": str(session.root.resolve(request.file)),
            "start_line": request.start_line,
            "end_line": request.end_line,
        }

    result = await session.read(request.what, options)
    attached = await session.attached()
    envelope = _envelope(session, attached)

    if request.what == "marks":
        marks = [Mark.model_validate(mark) for mark in pending(session)]
        return ReadResult(**envelope, marks=marks)
    if request.what == "cursor":
        state = result.get("cursor") or {}
        cursor = (
            Cursor.model_validate(state) if _within(session, state) else _outside(state)
        )
        return ReadResult(**envelope, cursor=cursor)
    if request.what == "range":
        state = result.get("range")
        if not state:
            return ReadResult(**envelope, range=NotOpen(open=False, file=request.file))
        span = (
            Range.model_validate(state) if _within(session, state) else _outside(state)
        )
        return ReadResult(**envelope, range=span)
    buffers = [
        Buffer.model_validate(state)
        for state in result.get("buffers") or []
        if _within(session, state)
    ]
    return ReadResult(**envelope, buffers=buffers)


def _within(session: Session, state: dict[str, Any]) -> bool:
    """Whether the agent may be told about this buffer.

    The human can navigate anywhere; the agent may only be told about what is
    inside the root it was given.
    """
    file = state.get("file")
    if not file:
        return False
    try:
        return session.root.contains(session.root.resolve(file))
    except Refused:
        return False


def _outside(state: dict[str, Any]) -> OutsideRoot:
    return OutsideRoot(file=state.get("file"), refused="outside the session root")


def build(lookup: SessionLookup, save: Callable[[], None] | None = None) -> Server:
    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[SHOW_TOOL, READ_TOOL])

    async def on_call_tool(
        _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        try:
            if params.name == "show":
                request: ShowRequest | ReadRequest = ShowRequest.model_validate(
                    arguments
                )
            elif params.name == "read":
                request = ReadRequest.model_validate(arguments)
            else:
                return _error(f"unknown tool: {params.name}")
        except ValidationError as e:
            return _error(models.invalid(e.errors()))
        session = lookup(request.session)
        if session is None:
            return _error(
                "no such session. Ask the human to run `nv new <root>` and paste the key it prints."
            )
        try:
            payload: Envelope
            if isinstance(request, ShowRequest):
                payload = await _show(session, request)
            else:
                payload = await _read(session, request)
        except Refused as e:
            return _error(str(e))
        except Exception as e:
            log.exception("%s failed", params.name)
            return _error(str(e))
        if save is not None:
            save()
        document = payload.model_dump(exclude_none=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(document))],
            structured_content=document,
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
