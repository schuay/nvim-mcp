# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""The tool contract: what a client may send and what it gets back.

The MCP schemas are generated from these models and arguments are validated
against them before anything reaches nvim, so the schema the model reads and
the check the broker makes cannot disagree. The SDK's server validates the
JSON-RPC envelope only, not tool arguments.

Strict mode, so a string where an integer belongs is an error rather than a
coercion, and no unknown fields, so a misspelled option is caught instead of
silently ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import GenerateJsonSchema

#: A review the human has to walk, not a dump. Beyond this the quickfix list
#: stops being something anyone reads to the end.
LOCATION_LIMIT = 50


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Result(BaseModel):
    # Results are built here, not parsed, so unknown keys from nvim's tables
    # are dropped rather than rejected.
    model_config = ConfigDict(extra="ignore")


SESSION_FIELD = Field(
    default=None,
    description=(
        "Session key, as printed by `showme new`. Omit it when this client was "
        "launched for one session and already holds its key."
    ),
)


class LocationSpec(Request):
    file: str = Field(
        description=(
            "The file to open. A relative path is taken against the session "
            "root; an absolute path anywhere on the host works, unless this "
            "session's key is a sandbox key, which reaches only inside it."
        )
    )
    line: int = Field(default=1, ge=1, description="1-based line; defaults to 1")
    end_line: int | None = Field(
        default=None, ge=1, description="Last line of a highlighted range"
    )
    text: str = Field(
        default="",
        description=(
            "Note shown above the line and in the quickfix list. Drawn with "
            "the line breaks you write, so a snippet keeps its shape; only a "
            "line wider than the window is broken. Write a paragraph of prose "
            "as one line and let the window break it."
        ),
    )

    @model_validator(mode="after")
    def _range_runs_forward(self) -> LocationSpec:
        # nvim would collapse a backwards range to its first line, which looks
        # like a highlight that landed rather than an argument to fix.
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(f"end_line {self.end_line} is before line {self.line}")
        return self


class ShowRequest(Request):
    locations: list[LocationSpec] = Field(
        default_factory=list, max_length=LOCATION_LIMIT
    )
    frame: Literal["replace", "push", "pop"] = Field(
        default="replace",
        description=(
            "replace swaps the top frame's notes; push opens a new frame above "
            "it for a digression; pop drops the top frame and its notes"
        ),
    )
    title: str = Field(
        default="agent", description="Label for the frame and the quickfix list"
    )
    focus: bool = Field(
        default=True, description="Jump the human's view to the first location"
    )
    session: str | None = SESSION_FIELD

    @model_validator(mode="after")
    def _locations_unless_pop(self) -> ShowRequest:
        if self.frame != "pop" and not self.locations:
            raise ValueError("locations are required unless frame is pop")
        return self


class ReadRequest(Request):
    what: Literal["marks", "cursor", "range", "tabs", "notes"]
    file: str | None = Field(default=None, description="With what='range'")
    start_line: int | None = Field(default=None, ge=1, description="1-based, inclusive")
    end_line: int | None = Field(default=None, ge=1, description="1-based, inclusive")
    ack: list[int] = Field(
        default_factory=list,
        description=(
            "Mark ids you have now dealt with. Until you acknowledge a mark it "
            "stays pending and is handed to you again."
        ),
    )
    session: str | None = SESSION_FIELD


class Envelope(Result):
    """What every result carries.

    An agent learns about a waiting question from any call it happens to make,
    so noticing one costs nothing extra.
    """

    session: str
    attach_cmd: str = Field(
        description="The command the human runs to attach a terminal to this session"
    )
    marks_pending: int = Field(description="Questions waiting in read(what='marks')")
    attached: bool = Field(description="Whether a human is looking right now")


class Refusal(Result):
    file: str
    reason: str


class FrameSummary(Result):
    letter: str
    title: str
    notes: int


class ShowResult(Envelope):
    frame: str | None = Field(
        default=None, description="The frame acted on; absent once all are popped"
    )
    popped: str | None = Field(
        default=None, description="The frame this call dropped, if any"
    )
    opened_ui: bool | None = Field(
        default=None,
        description="A window was opened for this session; the human is about to see it",
    )
    unseen: str | None = Field(
        default=None,
        description=(
            "Present only when the human cannot see what you just showed: "
            "nothing is attached to this session and no window opened for it. "
            "Pass it on to them as it stands -- it names the command they run, "
            "and nothing you can call attaches a terminal for them. Until one "
            "is attached, every show goes to a screen nobody is looking at."
        ),
    )
    ids: list[str] = Field(
        description="Ids of the notes just shown, in the order given, to refer to them by"
    )
    frames: list[FrameSummary] = Field(description="The stack, bottom first")
    opened: list[str] = Field(description="Files now shown, resolved")
    refused: list[Refusal] = Field(
        description="Locations not shown; one bad path does not spoil the rest"
    )


class Mark(Result):
    id: int
    file: str
    line1: int
    line2: int
    note: str = Field(description="What the human typed after :Ask")
    note_id: str | None = Field(
        default=None, description="The note the question was asked on, like A2"
    )
    modified: bool = Field(description="Whether the buffer had unsaved edits")
    text: str = Field(description="The lines as the human saw them")
    truncated: bool = Field(default=False, description="Whether text was cut short")


class ShownNote(Result):
    """A note as the session holds it, with the line nvim last reported."""

    id: str
    file: str
    line: int
    end_line: int | None = None
    text: str


class ShownFrame(Result):
    letter: str
    title: str
    notes: list[ShownNote]


class Buffer(Result):
    file: str
    modified: bool
    readable: bool
    visible: bool
    lines: int


class Cursor(Buffer):
    line: int
    col: int
    selection: tuple[int, int] | None = Field(
        default=None, description="Last visual selection, first and last line"
    )


class Range(Buffer):
    open: Literal[True]
    start_line: int
    end_line: int
    text: str


class NotOpen(Result):
    open: Literal[False]
    #: Resolved, so it reads the same as the path an open buffer comes back
    #: with. The two used to differ by whatever the caller happened to send.
    file: str
    hint: str = Field(
        default="not open in this session; show it first, or read it from disk",
        description="Why there is no text",
    )


class OutsideRoot(Result):
    """A buffer the human navigated to that the agent may not be told about."""

    file: str | None
    refused: str


class ReadResult(Envelope):
    unknown_ack: list[int] | None = Field(
        default=None,
        description="Acked ids that name no mark, so a mis-ack is not silence",
    )
    marks: list[Mark] | None = None
    frames: list[ShownFrame] | None = Field(
        default=None, description="With what='notes': the stack, bottom first"
    )
    cursor: Cursor | OutsideRoot | None = None
    range: Range | NotOpen | OutsideRoot | None = None
    buffers: list[Buffer] | None = None


class _Schema(GenerateJsonSchema):
    # Tool schemas are resident in every request the model makes. A title per
    # property only repeats the property name.
    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(schema_generator=_Schema)


def invalid(errors: list[dict[str, Any]]) -> str:
    """Render validation errors as one line an agent can act on."""
    return "invalid arguments: " + "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) or 'arguments'}: {e['msg']}"
        for e in errors
    )
