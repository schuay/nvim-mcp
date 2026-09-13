# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import msgpack
import pytest

from nvim_mcp import paths, splice
from nvim_mcp.clamp import Refused, Root
from nvim_mcp.nvimrpc import NvimError, NvimRPC
from nvim_mcp.session import NOTE_LIMIT, one_line


@pytest.fixture
async def silent_server() -> AsyncIterator[Path]:
    """A socket that accepts a connection and never answers."""
    directory = Path(tempfile.mkdtemp(prefix="nvmcp-", dir="/tmp"))
    path = directory / "s.sock"
    held: list[asyncio.StreamWriter] = []
    server = await asyncio.start_unix_server(
        lambda _r, w: held.append(w), path=str(path)
    )
    yield path
    # Close the accepted connections first: wait_closed() waits for them.
    for writer in held:
        writer.close()
    server.close()
    await server.wait_closed()


async def test_a_request_from_nvim_is_answered_on_the_read_loop() -> None:
    directory = Path(tempfile.mkdtemp(prefix="nvmcp-", dir="/tmp"))
    replies: asyncio.Queue[list] = asyncio.Queue()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(msgpack.packb([0, 7, "nvim-mcp", ["sync", {}]]))
        writer.write(msgpack.packb([0, 8, "other", []]))
        unpacker = msgpack.Unpacker(raw=False)
        while (chunk := await reader.read(4096)) and replies.qsize() < 2:
            unpacker.feed(chunk)
            for message in unpacker:
                await replies.put(message)
        writer.close()

    server = await asyncio.start_unix_server(serve, path=str(directory / "s.sock"))
    rpc = await NvimRPC.connect(directory / "s.sock")
    rpc.on_request = lambda method, params: params[0] if method == "nvim-mcp" else 1 / 0
    assert await replies.get() == [1, 7, None, "sync"]
    assert (await replies.get())[:3] == [1, 8, "division by zero"]
    await rpc.close()
    server.close()
    await server.wait_closed()


async def test_a_request_to_a_silent_nvim_gives_up(silent_server: Path) -> None:
    rpc = await NvimRPC.connect(silent_server)
    with pytest.raises(NvimError, match="did not answer"):
        await rpc.request("nvim_get_mode", timeout=0.2)
    await rpc.close()


async def test_a_request_after_close_fails_instead_of_hanging(
    silent_server: Path,
) -> None:
    rpc = await NvimRPC.connect(silent_server)
    await rpc.close()
    # Without the closed check this future is never completed by anyone, and
    # the caller waits for the full timeout or forever.
    with pytest.raises(NvimError, match="closed"):
        await asyncio.wait_for(rpc.request("nvim_get_mode"), 1.0)


def test_write_all_finishes_a_short_write(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:1]))
    try:
        splice.write_all(write_fd, b"0123456789")
        assert os.read(read_fd, 100) == b"0123456789"
    finally:
        monkeypatch.undo()
        os.close(read_fd)
        os.close(write_fd)


def test_a_symlink_loop_is_refused_not_raised(repo: Path) -> None:
    # Path.resolve() is non-strict, so a loop comes back as a path that is not
    # a file rather than as an OSError. Either way the caller gets a Refused it
    # can report, never an exception that fails the whole call.
    (repo / "a").symlink_to(repo / "b")
    (repo / "b").symlink_to(repo / "a")
    with pytest.raises(Refused):
        Root.of(repo).resolve("a")


def test_a_long_note_is_truncated() -> None:
    assert one_line("x" * (NOTE_LIMIT * 2)).endswith("...")
    assert len(one_line("x" * (NOTE_LIMIT * 2))) == NOTE_LIMIT
    assert one_line("short\x07 note") == "short note"
    # A dropped newline would have run the two words together.
    assert one_line("wrapped\nprose\tand  spaces") == "wrapped prose and spaces"


def test_a_loose_directory_is_tightened(monkeypatch: pytest.MonkeyPatch) -> None:
    base = Path(tempfile.mkdtemp(prefix="nvmcp-", dir="/tmp"))
    loose = base / "nvim-mcp"
    loose.mkdir(mode=0o777)
    monkeypatch.setenv("NVIM_MCP_AGENT_DIR", str(loose))
    assert paths.agent_dir() == loose
    assert stat.S_IMODE(loose.lstat().st_mode) == 0o700
