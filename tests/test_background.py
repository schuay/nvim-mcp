# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""The background an attaching terminal reports.

A session's nvim is headless and never sees a terminal, so it cannot ask what
the background is. Only `showme attach` can, and only before it becomes nvim.
"""

from __future__ import annotations

import argparse
import os
import pty
import re
import select
import time
from pathlib import Path

import pytest
from conftest import admin

from showme import cli
from showme.nvimrpc import NvimRPC
from showme.session import Session

WHITE = b"\x1b]11;rgb:ffff/ffff/ffff\x07"
BLACK = b"\x1b]11;rgb:0000/0000/0000\x07"


@pytest.mark.parametrize(
    ("channels", "expected"),
    [
        (("ffff", "ffff", "ffff"), "light"),
        (("0000", "0000", "0000"), "dark"),
        # Solarized light and a near-black grey, as those terminals report them.
        (("fdf6", "e3e3", "0000"), "light"),
        (("2222", "2222", "2222"), "dark"),
        # One to four hex digits per channel, each scaled against its own width.
        (("f", "f", "f"), "light"),
        (("ff", "ff", "ff"), "light"),
        (("fff", "fff", "fff"), "light"),
        (("0", "0", "0"), "dark"),
    ],
)
def test_a_reply_is_classified_by_luma(
    channels: tuple[str, str, str], expected: str
) -> None:
    assert cli._background_of(*channels) == expected


def _detect_under_terminal(reply: bytes | None) -> str | None:
    """Run the query in a child whose controlling terminal answers `reply`."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs out of the test process
        result = cli._detect_background(timeout=1.0)
        os.write(1, f"RESULT:{result}\n".encode())
        os._exit(0)

    buf = b""
    answered = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.05)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if reply is not None and cli._OSC11_QUERY in buf and not answered:
            answered = True
            os.write(fd, reply)
        if b"RESULT:" in buf:
            break
    os.waitpid(pid, 0)
    found = re.search(rb"RESULT:(\w+)", buf)
    return found.group(1).decode() if found else None


def test_a_light_terminal_is_detected() -> None:
    assert _detect_under_terminal(WHITE) == "light"


def test_a_dark_terminal_is_detected() -> None:
    assert _detect_under_terminal(BLACK) == "dark"


def test_a_silent_terminal_leaves_it_undecided() -> None:
    # Terminals that do not implement OSC 11 answer nothing at all. The query
    # has to give up rather than hold the attach open.
    assert _detect_under_terminal(None) == "None"


def test_attach_sends_what_the_terminal_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_ensure_broker", lambda: None)
    monkeypatch.delenv("SHOWME_BACKGROUND", raising=False)
    monkeypatch.setattr(cli, "_detect_background", lambda: "light")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/nvim")
    sent: list[dict] = []

    def ask(request: dict, timeout: float = 0) -> dict:
        sent.append(request)
        return {"ok": True, "socket": "/tmp/x.sock"}

    monkeypatch.setattr(cli, "_ask", ask)
    execs: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execv", lambda _p, argv: execs.append(argv))

    cli.cmd_attach(argparse.Namespace(id="1"))
    assert sent[0]["background"] == "light"


def test_an_explicit_choice_outranks_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `showme new --dark` on a light terminal is the human overriding what the
    # terminal would say, so attaching from one must not undo it.
    monkeypatch.setenv("SHOWME_BACKGROUND", "dark")
    monkeypatch.setattr(cli, "_ensure_broker", lambda: None)
    monkeypatch.setattr(
        cli, "_detect_background", lambda: pytest.fail("should not be asked")
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/nvim")
    sent: list[dict] = []
    monkeypatch.setattr(
        cli,
        "_ask",
        lambda r, timeout=0: (sent.append(r), {"ok": True, "socket": "/s"})[1],
    )
    monkeypatch.setattr(cli.os, "execv", lambda _p, argv: None)

    cli.cmd_attach(argparse.Namespace(id="1"))
    assert sent[0]["background"] == "dark"


async def test_a_session_keeps_the_background_it_was_created_with(
    tmp_path: Path,
) -> None:
    session = Session.create("1", tmp_path, background="dark")
    calls: list[tuple] = []

    class Stub:
        async def request(self, *params: object) -> None:
            calls.append(params)

    session.editor.rpc = Stub()  # type: ignore[assignment]
    await session.wear_background("light")
    assert calls == []


async def test_a_session_without_one_takes_the_terminals(tmp_path: Path) -> None:
    session = Session.create("1", tmp_path)
    calls: list[tuple] = []

    class Stub:
        async def request(self, *params: object) -> None:
            calls.append(params)

    session.editor.rpc = Stub()  # type: ignore[assignment]
    await session.wear_background("light")
    assert calls == [("nvim_set_option_value", "background", "light", {})]


@pytest.mark.nvim
async def test_attaching_sets_the_background_on_the_session_nvim(
    running_broker: Path, repo: Path
) -> None:
    # The whole path: the terminal's answer rides on the attach command, the
    # broker hands it to the session, and the session's nvim wears it.
    reply = await admin({"cmd": "new", "root": str(repo)})
    assert reply["ok"], reply

    nvim = await NvimRPC.connect(Path(reply["socket"]))
    assert await nvim.request("nvim_get_option_value", "background", {}) == "dark"

    attached = await admin({"cmd": "attach", "id": reply["id"], "background": "light"})
    assert attached["ok"], attached
    assert await nvim.request("nvim_get_option_value", "background", {}) == "light"
    await nvim.close()


@pytest.mark.nvim
async def test_attaching_does_not_undo_an_explicit_choice(
    running_broker: Path, repo: Path
) -> None:
    reply = await admin({"cmd": "new", "root": str(repo), "background": "dark"})
    assert reply["ok"], reply

    nvim = await NvimRPC.connect(Path(reply["socket"]))
    attached = await admin({"cmd": "attach", "id": reply["id"], "background": "light"})
    assert attached["ok"], attached
    assert await nvim.request("nvim_get_option_value", "background", {}) == "dark"
    await nvim.close()
