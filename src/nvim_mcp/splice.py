# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Splice this process's stdin and stdout onto a UNIX socket.

An MCP client launches this as its stdio server. It copies bytes and holds no
schema, so it cannot drift from the broker, and it imports nothing outside the
standard library because it also runs inside a sandbox that cannot install
anything. The broker writes a copy of this file next to its agent socket.
"""

from __future__ import annotations

import os
import selectors
import socket
import sys


def write_all(fd: int, data: bytes) -> None:
    """Write every byte.

    os.write may write fewer bytes than it was given, and a short write here
    truncates an MCP message mid-JSON.
    """
    while data:
        data = data[os.write(fd, data) :]


def splice(socket_path: str) -> int:
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.connect(socket_path)
    except OSError as e:
        print(
            f"nvim-mcp: cannot reach the broker at {socket_path}: {e}", file=sys.stderr
        )
        print(
            "nvim-mcp: ask the human to run `nv new <root>` on the host.",
            file=sys.stderr,
        )
        return 1

    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.buffer.fileno(), selectors.EVENT_READ, "in")
    selector.register(sock.fileno(), selectors.EVENT_READ, "out")
    try:
        while True:
            for key, _ in selector.select():
                if key.data == "in":
                    chunk = os.read(key.fileobj, 65536)
                    if not chunk:
                        sock.shutdown(socket.SHUT_WR)
                        selector.unregister(key.fileobj)
                        continue
                    sock.sendall(chunk)
                else:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return 0
                    write_all(sys.stdout.buffer.fileno(), chunk)
    except (BrokenPipeError, ConnectionResetError):
        return 0
    finally:
        selector.close()
        sock.close()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: splice.py <socket>", file=sys.stderr)
        return 2
    return splice(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
