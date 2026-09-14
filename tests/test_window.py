# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Giving a session a window when an agent shows to one nobody is watching."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from showme.session import Location, Session

pytestmark = pytest.mark.nvim


@pytest.fixture
def terminal(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for a terminal emulator: it records its arguments.

    A test that opened a real window would need a display and would put one on
    the screen of whoever ran the suite.
    """
    log = tmp_path / "opened"
    script = tmp_path / "fake-terminal"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n')
    script.chmod(0o755)
    return script, log


def env_with(script: Path, **extra: str) -> dict[str, str]:
    return {**os.environ, "SHOWME_TERMINAL": str(script), **extra}


async def recorded(log: Path, timeout: float = 5.0) -> list[str]:
    """Wait for the stand-in to run: Popen returns before it has."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not log.exists():
        assert asyncio.get_running_loop().time() < deadline, "no terminal was started"
        await asyncio.sleep(0.02)
    return log.read_text().splitlines()


async def shown(session: Session, repo: Path) -> dict:
    return await session.show(
        [Location(repo / "src" / "main.c", line=1, text="look")], "review", False
    )


@pytest.fixture
async def closing() -> AsyncIterator[list[Session]]:
    sessions: list[Session] = []
    yield sessions
    for session in sessions:
        await session.close()


async def test_a_show_to_an_empty_screen_opens_a_window_once(
    runtime: Path, repo: Path, terminal: tuple[Path, Path], closing: list[Session]
) -> None:
    script, log = terminal
    session = Session.create(
        "1", repo, clean=True, env=env_with(script, WAYLAND_DISPLAY="wayland-0")
    )
    closing.append(session)

    result = await shown(session, repo)
    assert result["opened_ui"] is True
    # The terminal runs nvim against this session's socket directly, so there
    # is no second question to the admin socket and no `showme` to find.
    opened = await recorded(log)
    assert opened == [f"nvim --remote-ui --server {session.socket}"]

    # Once per nvim. The human who closes the window has quit nvim with it,
    # and a window that came back on the next note could not be got rid of.
    again = await shown(session, repo)
    assert again["opened_ui"] is False
    await asyncio.sleep(0.2)
    assert log.read_text().splitlines() == opened


async def test_no_terminal_and_no_display_open_nothing(
    runtime: Path, repo: Path, terminal: tuple[Path, Path], closing: list[Session]
) -> None:
    script, log = terminal
    unset = Session.create("1", repo, clean=True, env={**os.environ})
    closing.append(unset)
    assert (await shown(unset, repo))["opened_ui"] is False

    # Configured, but the session was made somewhere with no display: over ssh
    # the answer is the attach command, not a window nobody can see.
    headless = {k: v for k, v in env_with(script).items() if k != "WAYLAND_DISPLAY"}
    headless.pop("DISPLAY", None)
    blind = Session.create("2", repo, clean=True, env=headless)
    closing.append(blind)
    assert (await shown(blind, repo))["opened_ui"] is False
    await asyncio.sleep(0.2)
    assert not log.exists()
