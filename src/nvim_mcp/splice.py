# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Relay this process's stdin and stdout to the broker's socket, across
broker restarts.

An MCP client launches this as its stdio server. It forwards JSON-RPC lines
and holds no schema, so it cannot drift from the broker, and it imports
nothing outside the standard library because it also runs inside a sandbox
that cannot install anything. The broker writes a copy of this file next to
its agent socket.

The client sees one MCP session for as long as it keeps this process. When
the broker goes away, the connection is made again on the client's next
message: the handshake the client sent at the start is replayed and its
second answer dropped, so the client never learns the other side changed.
A request that was in flight when the connection broke gets an error
telling the caller to retry, since the broker that took it is gone.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

#: How long to keep trying the socket after a loss. A broker restarted by
#: `nv` answers within a second; longer means nobody is restarting it.
RECONNECT_WINDOW = float(os.environ.get("NVIM_MCP_RECONNECT_WINDOW", "5"))
CONNECTION_LOST = -32000

#: The field naming the line a client sends before any JSON-RPC to say which
#: session it was launched for, and the broker's answer to it. Connection
#: setup, the same layer as the handshake replayed below: no tool is named and
#: no argument is rewritten, so this side still holds no schema.
HELLO = "nvim_mcp"


def write_all(fd: int, data: bytes) -> None:
    """Write every byte.

    os.write may write fewer bytes than it was given, and a short write here
    truncates an MCP message mid-JSON.
    """
    while data:
        data = data[os.write(fd, data) :]


class Lines:
    """Split a byte stream into newline-terminated messages."""

    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        return [line + b"\n" for line in lines if line]


def _message(line: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Splice:
    def __init__(
        self,
        socket_path: str,
        revive: Callable[[], None] | None = None,
        key: str | None = None,
    ):
        self.socket_path = socket_path
        #: The session this client was launched for, presented on every
        #: connection so a client in a sandbox never has to be told a key.
        self.key = key
        #: Asked for a broker when nothing answers the socket. On the host this
        #: starts one; a sandbox either has no way to ask (None) or starts one
        #: that cannot claim the read-only agent directory.
        self.revive = revive
        self.sock: socket.socket | None = None
        self.from_sock = Lines()
        #: The client's `initialize` request and `initialized` notification,
        #: replayed to a new broker so the client need not know about it.
        self.handshake: list[bytes] = []
        self.initialize_id: Any = None
        #: Requests sent to the broker and not yet answered, by id.
        self.pending: set[Any] = set()
        self.selector = selectors.DefaultSelector()
        self.out = sys.stdout.buffer.fileno()

    def run(self) -> int:
        if not self.connect(first=True):
            print(
                f"nvim-mcp: cannot reach the broker at {self.socket_path}",
                file=sys.stderr,
            )
            print(
                "nvim-mcp: ask the human to run `nv new <root>` on the host.",
                file=sys.stderr,
            )
            return 1
        stdin = sys.stdin.buffer.fileno()
        from_client = Lines()
        self.selector.register(stdin, selectors.EVENT_READ, "client")
        try:
            while True:
                for key, _ in self.selector.select():
                    if key.data == "client":
                        chunk = os.read(stdin, 65536)
                        if not chunk:
                            return 0
                        for line in from_client.feed(chunk):
                            self.from_client(line)
                    else:
                        try:
                            chunk = self.sock.recv(65536) if self.sock else b""
                        except OSError:
                            # A reset counts the same as a clean close: the
                            # broker is gone either way.
                            chunk = b""
                        if not chunk:
                            self.lost()
                            continue
                        for line in self.from_sock.feed(chunk):
                            self.from_broker(line)
        except BrokenPipeError:
            return 0
        finally:
            self.selector.close()
            if self.sock is not None:
                self.sock.close()

    def from_client(self, line: bytes) -> None:
        message = _message(line)
        method = message.get("method") if message else None
        if method == "initialize":
            self.handshake = [line]
            self.initialize_id = message.get("id") if message else None
        elif method == "notifications/initialized" and len(self.handshake) == 1:
            self.handshake.append(line)
        if self.sock is None and not self.connect(first=False):
            if message is not None and "id" in message and method is not None:
                self.answer(
                    message["id"],
                    "nvim-mcp: the broker is not running; ask the human to run `nv` "
                    "on the host, then retry",
                )
            return
        if message is not None and "id" in message and method is not None:
            self.pending.add(message["id"])
        try:
            assert self.sock is not None
            self.sock.sendall(line)
        except OSError:
            self.lost()
            if message is not None and "id" in message and method is not None:
                self.pending.discard(message["id"])
                self.answer(message["id"], "nvim-mcp: broker connection lost; retry")

    def from_broker(self, line: bytes) -> None:
        message = _message(line)
        if message is not None and "id" in message and "method" not in message:
            self.pending.discard(message["id"])
        write_all(self.out, line)

    def lost(self) -> None:
        """The broker went away. Nothing it was working on will be answered."""
        if self.sock is not None:
            self.selector.unregister(self.sock.fileno())
            self.sock.close()
            self.sock = None
        self.from_sock = Lines()
        for request_id in sorted(self.pending, key=str):
            self.answer(
                request_id,
                "nvim-mcp: the broker restarted while handling this request; retry",
            )
        self.pending.clear()

    def answer(self, request_id: Any, text: str) -> None:
        error = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": CONNECTION_LOST, "message": text},
        }
        write_all(self.out, json.dumps(error).encode() + b"\n")

    def connect(self, first: bool) -> bool:
        """Connect to the broker, and bring a new one up to date.

        A broker already listening is taken as it stands, and one is asked for
        only when nothing answers. That is what lets a client in a sandbox run
        this against the host's broker: the socket it was given is served
        already, so no second broker is ever started behind it.
        """
        sock = self._dial(0)
        if sock is None:
            if self.revive is not None:
                self.revive()
            elif first:
                # Nothing answers, nothing to ask, and no restart is under way.
                return False
            sock = self._dial(RECONNECT_WINDOW)
        if sock is None:
            return False
        if self.key is not None and not self._hello(sock):
            sock.close()
            return False
        if self.handshake and not self._replay(sock):
            sock.close()
            return False
        self.sock = sock
        self.selector.register(sock.fileno(), selectors.EVENT_READ, "broker")
        return True

    def _dial(self, window: float) -> socket.socket | None:
        """Try the socket until it answers or `window` seconds have passed.

        A broker coming up takes a moment to listen.
        """
        deadline = time.monotonic() + window
        while True:
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(self.socket_path)
            except OSError:
                sock.close()
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.1)
            else:
                return sock

    def _hello(self, sock: socket.socket) -> bool:
        """Name the session before the MCP stream starts, and wait to be told.

        A refusal here is worth failing on: the client would otherwise come up
        with tools that answer every call with "no such session".
        """
        try:
            sock.sendall(json.dumps({HELLO: {"key": self.key}}).encode() + b"\n")
            sock.settimeout(RECONNECT_WINDOW)
            lines = Lines()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return False
                for line in lines.feed(chunk):
                    message = _message(line)
                    if message is None or HELLO not in message:
                        return False
                    answer = message[HELLO]
                    if not answer.get("ok"):
                        print(
                            f"nvim-mcp: {answer.get('error', 'key refused')}",
                            file=sys.stderr,
                        )
                        return False
                    # Nothing else can have arrived: the broker sends nothing
                    # until it is spoken to.
                    self.from_sock = lines
                    sock.settimeout(None)
                    return True
        except OSError:
            return False

    def _replay(self, sock: socket.socket) -> bool:
        """Repeat the client's handshake and swallow the broker's answer.

        The client already has its answer from the first broker; a second one
        would be a protocol error on its side.
        """
        try:
            for line in self.handshake:
                sock.sendall(line)
            sock.settimeout(RECONNECT_WINDOW)
            lines = Lines()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return False
                for line in lines.feed(chunk):
                    message = _message(line)
                    if message is not None and message.get("id") == self.initialize_id:
                        self.from_sock = lines
                        sock.settimeout(None)
                        return True
        except OSError:
            return False


def splice(
    socket_path: str,
    revive: Callable[[], None] | None = None,
    key: str | None = None,
) -> int:
    return Splice(socket_path, revive, key).run()


def main() -> int:
    # The key is a file rather than an argument: an MCP client config is
    # readable inside the box, and a secret in it would be too.
    if not 2 <= len(sys.argv) <= 3:
        print("usage: splice.py <socket> [key-file]", file=sys.stderr)
        return 2
    key = None
    if len(sys.argv) == 3:
        with open(sys.argv[2]) as handle:
            key = handle.read().strip()
    return splice(sys.argv[1], key=key)


if __name__ == "__main__":
    raise SystemExit(main())
