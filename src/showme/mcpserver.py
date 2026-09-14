# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Serve MCP to one client connection.

The tool list is the broker's entire exposed surface, so nothing here writes: a
client can put code in front of the human and read what the human is looking
at, and nothing else. Sessions are addressed by their secret key, because the
broker cannot tell one sandboxed client from another.

What a read may reach comes from the key it arrived with, not from the session:
a sandboxed client is clamped to the session root, and a client that took its
key on the host reads what the human reads. A mark is outside both: only the
human makes one, and `:Ask` on a range is them handing it over deliberately.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from . import models
from .clamp import Refused, Root
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
    ShownFrame,
    ShowRequest,
    ShowResult,
)
from .session import Location, Session

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "showme is a shared editor surface: the nvim the human is sitting in, "
    "which you draw on and read back. Two tools, and nothing writes -- no "
    "file, no buffer, no saved edit.\n"
    "\n"
    "show puts locations in front of them: a tab per file, the positions in "
    "the quickfix list, a highlight over any range, and a note above each "
    "line under an id like A2 that both of you can say. Reach for it when "
    "they ask to see or be pointed at code, and when your answer is about "
    "lines they would rather read in place than in chat. Batch the locations "
    "of one answer into one call, and do not open a tab to quote a value "
    "back at them.\n"
    "\n"
    "read reports their side: where the cursor is and what they last "
    "selected, an open buffer including edits they have not saved, the open "
    "tabs, the notes on screen, and the ranges they handed over with :Ask. "
    "Reach for it when they say 'this', 'here' or 'what I marked'. A mark "
    "stays pending until you pass its id back in ack, so acknowledge one once "
    "you have answered it.\n"
    "\n"
    "Nothing you show is seen until a terminal is attached to the session. "
    "Where the human's shell can open a window one opens by itself; where it "
    "cannot -- over ssh, most often -- a show comes back with unseen, and they "
    "go on seeing nothing until you relay it. It names the command they run, "
    "and no tool of yours attaches a terminal for them.\n"
    "\n"
    "A call names its session by key, unless this client was launched for one "
    "session and already holds it."
)

SHOW_TOOL = types.Tool(
    name="show",
    title="Show code to the human",
    description=(
        "Put code in front of the human, in the editor they are sitting in. "
        "Reach for it whenever they ask to see, show, open, look at or be "
        "pointed at code -- 'show me the parser', 'open that in nvim', 'where "
        "does this happen' -- and whenever your answer is about particular "
        "lines they would rather read in place than in chat. Opening a tab "
        "they did not ask for is the thing to avoid: this is for code the two of "
        "you are discussing, not for quoting a value or a command back to them. "
        "It opens a tab per file, fills the quickfix list with the positions, "
        "highlights any range, and renders each note above its line under an "
        "id like A2 that you and the human can both say. Batch every location "
        "you are discussing into one call. Notes live in frames: the default "
        "replaces the top frame, push starts a frame for a digression, pop "
        "drops it when answered. Never closes a tab. A show that lands on a "
        "session nobody is watching comes back with unseen: relay that to the "
        "human before you go on, or they never see what you showed."
    ),
    input_schema=models.schema(ShowRequest),
    output_schema=models.schema(ShowResult),
)

READ_TOOL = types.Tool(
    name="read",
    title="Read what the human is looking at",
    description=(
        "Read the human's session: what they are looking at, and what they "
        "have handed you. Reach for it when they refer to something as 'this', "
        "'here' or 'what I marked', or when you need the state of a buffer "
        "they have been editing. "
        "'marks' collects the ranges they handed over "
        "with :Ask, each with their question; call it when they refer to "
        "something they marked, and pass the ids back in 'ack' once you have "
        "answered them, or they stay pending and come back. 'cursor' is where "
        "they are now and what they last selected. 'range' reads an open "
        "buffer, including edits they have not saved. 'tabs' lists what is "
        "open. 'notes' gives back the frames and the text of every note on "
        "screen, which is how an agent that did not write them learns what "
        "A2 says. Everything but 'marks' is limited to what this session's "
        "key reaches: a sandboxed key sees only inside the session root, and "
        "a key taken on the host sees whatever the human sees."
    ),
    input_schema=models.schema(ReadRequest),
    output_schema=models.schema(ReadResult),
)

SessionLookup = Callable[[str], tuple[Session, Root] | None]


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
        "attach_cmd": f"showme {session.sid}",
        "marks_pending": len(pending(session)),
        "attached": attached,
    }


async def _show(session: Session, root: Root, request: ShowRequest) -> ShowResult:
    locations, refused = [], []
    for spec in request.locations:
        try:
            # Resolve once. The path that goes to nvim is the one that passed
            # the clamp, so there is no second resolution to disagree with it.
            resolved = root.resolve(spec.file)
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
    popped: str | None = None
    opened_ui: bool | None = None
    frame = session.frames[-1].letter if session.frames else None
    frames = [
        FrameSummary(letter=f.letter, title=f.title, notes=len(f.notes))
        for f in session.frames
    ]
    attached = await session.attached()
    if locations or request.frame == "pop":
        result = await session.show(
            locations, title=request.title, focus=request.focus, frame=request.frame
        )
        opened = result.get("opened", [])
        ids = result["ids"]
        frame = result["frame"]
        popped = result["popped"]
        opened_ui = result["opened_ui"] or None
        # Taken under the session's lock, so it describes this call rather
        # than whatever another agent did while this one was answering.
        frames = [FrameSummary(**summary) for summary in result["frames"]]
        # nvim reports how many UIs it has while applying the show, which saves
        # a second round trip for the same fact.
        attached = bool(result.get("uis"))

    # Nobody saw it: nothing was attached and no window opened for it. The
    # broker cannot reach the human, so the agent is told to hand the command
    # over -- over ssh, where no window can open, that is the only way a
    # session ever gets a screen.
    unseen = None
    if locations and not attached and not opened_ui:
        unseen = (
            "Nobody is watching this session, so the human has not seen what "
            f"you just showed. Tell them to run `showme {session.sid}` in "
            "another terminal on the machine this session runs on; over ssh "
            "that is a second shell into the same host."
        )

    return ShowResult(
        **_envelope(session, attached),
        frame=frame,
        popped=popped,
        opened_ui=opened_ui,
        unseen=unseen,
        ids=ids,
        frames=frames,
        opened=opened,
        refused=refused,
    )


async def _read(session: Session, root: Root, request: ReadRequest) -> ReadResult:
    acknowledged = set(request.ack)
    for mark in session.marks:
        if mark.get("id") in acknowledged:
            mark["acked"] = True
    # An id that names no mark is a mistake worth hearing about: the agent
    # believes it answered a question that is still waiting.
    unknown = sorted(acknowledged - {mark.get("id") for mark in session.marks})
    options: dict[str, Any] = {}
    target: Path | None = None
    if request.what == "range":
        if not request.file:
            raise Refused("read(what='range') needs a file")
        # Located, not resolved: a buffer the human is editing outlives the
        # file, and an agent that just renamed it is the likeliest reason to
        # be asking. Whether anything is there is settled after nvim answers.
        target = root.locate(request.file)
        options = {
            "file": str(target),
            "start_line": request.start_line,
            "end_line": request.end_line,
        }

    result = await session.read(request.what, options)
    attached = await session.attached()
    envelope = _envelope(session, attached)
    if unknown:
        envelope["unknown_ack"] = unknown

    if request.what == "notes":
        # Read from the record rather than from nvim, which holds no text of
        # its own; the sync that just ran brought the lines up to date.
        return ReadResult(
            **envelope,
            frames=[
                ShownFrame(
                    letter=frame.letter,
                    title=frame.title,
                    notes=[asdict(note) for note in frame.notes],
                )
                for frame in session.frames
            ],
        )
    if request.what == "marks":
        marks = [Mark.model_validate(mark) for mark in pending(session)]
        return ReadResult(**envelope, marks=marks)
    if request.what == "cursor":
        state = result.get("cursor") or {}
        cursor = (
            Cursor.model_validate(state) if _within(root, state) else _outside(state)
        )
        return ReadResult(**envelope, cursor=cursor)
    if request.what == "range":
        state = result.get("range")
        if not state:
            if not target.is_file():
                raise Refused("no such file, and no buffer holding it")
            return ReadResult(**envelope, range=NotOpen(open=False, file=str(target)))
        span = Range.model_validate(state) if _within(root, state) else _outside(state)
        return ReadResult(**envelope, range=span)
    buffers = [
        Buffer.model_validate(state)
        for state in result.get("buffers") or []
        if _within(root, state)
    ]
    return ReadResult(**envelope, buffers=buffers)


def _within(root: Root, state: dict[str, Any]) -> bool:
    """Whether the agent may be told about this buffer.

    The human can navigate anywhere; a clamped key hears only about what is
    inside the root it was given.
    """
    file = state.get("file")
    if not file:
        return False
    try:
        # Where it is, not whether it is there: nvim is reporting a buffer it
        # holds, which may no longer have a file behind it.
        return root.contains(root.locate(file))
    except Refused:
        return False


def _outside(state: dict[str, Any]) -> OutsideRoot:
    return OutsideRoot(file=state.get("file"), refused="outside the session root")


def build(
    lookup: SessionLookup,
    save: Callable[[], None] | None = None,
    default_key: str | None = None,
) -> Server:
    """Serve the two tools over one connection.

    `default_key` is the session this connection was opened for, if it named
    one. It stands in for an omitted `session` argument, so a client launched
    for a single session never handles a key and a client serving several
    still picks per call.
    """

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
        key = request.session or default_key
        found = lookup(key) if key else None
        if found is None:
            return _error(
                "no such session. Ask the human to run `showme new <root>` and "
                "paste the key it prints."
                if key
                else "no session for this client. It was started somewhere "
                "without one -- ask the human to run `showme new <root>` in the "
                "directory they want you looking at, and paste the key."
            )
        session, root = found
        try:
            payload: Envelope
            if isinstance(request, ShowRequest):
                payload = await _show(session, root, request)
            else:
                payload = await _read(session, root, request)
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

    return Server(
        "showme",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


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
