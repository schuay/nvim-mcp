# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""Relay MCP stdio to the broker across broker restarts.

Forward complete JSON-RPC lines without interpreting tool schemas. This file
uses only the standard library because the broker also copies it beside the
agent socket for sandbox clients.

Send the launch key and conversation ID before JSON-RPC so the broker can claim
the correct session. After a disconnect, reconnect on the next client message,
replay the MCP handshake, and discard its duplicate response. Fail requests
that were in flight because the previous broker cannot answer them.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

#: Allow enough time for a broker restarted by `showme` to begin listening.
RECONNECT_WINDOW = float(os.environ.get("SHOWME_RECONNECT_WINDOW", "5"))
CONNECTION_LOST = -32000

#: Field for the pre-JSON-RPC session claim and the broker's response.
HELLO = "showme"

#: Harness variables supplying conversation ids that survive resume.
SESSION_VARS = ("CLAUDE_CODE_SESSION_ID",)


def identify() -> tuple[str, bool]:
    """Return a harness conversation id, or a unique id for this process.

    Only harness ids support resuming across MCP process restarts.
    """
    for name in SESSION_VARS:
        value = os.environ.get(name)
        if value:
            # Include the variable name to prevent IDs from colliding across harnesses.
            return f"{name}:{value}", True
    return f"launch:{uuid.uuid4().hex}", False


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
        #: Present this launch key on every connection without exposing it to MCP.
        self.key = key
        #: Keep one conversation identity across broker reconnects.
        self.agent, self.stable = identify()
        #: Host callback that starts a missing broker; sandboxes receive none.
        self.revive = revive
        self.sock: socket.socket | None = None
        self.from_sock = Lines()
        #: MCP handshake replayed after broker reconnects.
        self.handshake: list[bytes] = []
        self.initialize_id: Any = None
        #: Requests sent to the broker and not yet answered, by id.
        self.pending: set[Any] = set()
        self.selector = selectors.DefaultSelector()
        self.out = sys.stdout.buffer.fileno()

    def run(self) -> int:
        if not self.connect(first=True):
            print(
                f"showme: cannot reach the broker at {self.socket_path}",
                file=sys.stderr,
            )
            print(
                "showme: ask the human to run `showme new <root>` on the host.",
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
                            # Treat a reset as broker loss.
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
                    "showme: the broker is not running; ask the human to run `showme` "
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
                self.answer(message["id"], "showme: broker connection lost; retry")

    def from_broker(self, line: bytes) -> None:
        message = _message(line)
        if message is not None and "id" in message and "method" not in message:
            self.pending.discard(message["id"])
        write_all(self.out, line)

    def lost(self) -> None:
        """Close the broker connection and fail every pending request."""
        if self.sock is not None:
            self.selector.unregister(self.sock.fileno())
            self.sock.close()
            self.sock = None
        self.from_sock = Lines()
        for request_id in sorted(self.pending, key=str):
            self.answer(
                request_id,
                "showme: the broker restarted while handling this request; retry",
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
        """Connect to the broker and replay state after a restart.

        Invoke the host's restart callback only when no broker answers. Sandbox
        clients have no callback and therefore cannot start a second broker.
        """
        sock = self._dial(0)
        if sock is None:
            if self.revive is not None:
                self.revive()
            elif first:
                # A sandbox's initial failure has no restart to wait for.
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
            opening = {"key": self.key, "agent": self.agent, "stable": self.stable}
            sock.sendall(json.dumps({HELLO: opening}).encode() + b"\n")
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
                            f"showme: {answer.get('error', 'key refused')}",
                            file=sys.stderr,
                        )
                        return False
                    # The broker sends no unsolicited data before MCP begins.
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
    # Keep the key out of sandbox-readable MCP client arguments.
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
