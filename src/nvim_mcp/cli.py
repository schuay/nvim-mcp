# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""`nv`: create sessions, attach a terminal to one, and inspect them.

Sessions are created here rather than by a client, because the root a session is
clamped to has to come from the human. The broker spawns nvim, so the session
inherits the broker's environment, not this shell's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

from . import paths, splice


def _ask(request: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        sock.connect(str(paths.admin_socket()))
        sock.sendall(json.dumps(request).encode() + b"\n")
        with sock.makefile("rb") as stream:
            line = stream.readline()
    if not line:
        raise RuntimeError("broker closed the connection")
    return json.loads(line)


def _ensure_broker(timeout: float = 10.0) -> None:
    try:
        _ask({"cmd": "ping"}, timeout=2.0)
    except (OSError, RuntimeError):
        pass
    else:
        return
    # start_new_session detaches the broker from this terminal, so it survives
    # the shell that started it.
    subprocess.Popen(
        [sys.executable, "-m", "nvim_mcp.broker"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _ask({"cmd": "ping"}, timeout=1.0)
        except (OSError, RuntimeError):
            time.sleep(0.05)
        else:
            return
    raise SystemExit("nvim-mcp: broker did not start")


def _session(sid: str) -> dict[str, Any]:
    for session in _ask({"cmd": "ls"})["sessions"]:
        if session["id"] == sid:
            return session
    raise SystemExit(f"nvim-mcp: no session {sid}")


def cmd_new(args: argparse.Namespace) -> int:
    _ensure_broker()
    reply = _ask(
        {
            "cmd": "new",
            # Resolved here: the broker's cwd is whichever shell first started
            # it, and a relative root would be taken against that.
            "root": str(Path(args.root).expanduser().resolve()),
            "clean": args.clean,
            "background": args.background or os.environ.get("NVIM_MCP_BACKGROUND"),
        }
    )
    if not reply["ok"]:
        raise SystemExit(f"nvim-mcp: {reply['error']}")
    print(f"session {reply['id']}  root {reply['root']}")
    print(f"attach:  nv {reply['id']}")
    print(f"give the agent this key:  {reply['key']}")
    return 0


def cmd_ls(_args: argparse.Namespace) -> int:
    try:
        sessions = _ask({"cmd": "ls"})["sessions"]
    except OSError:
        print("no broker running")
        return 0
    for session in sessions:
        state = "attached" if session["attached"] else "detached"
        print(
            f"{session['id']:>3}  {state:<8}  {session['root']}  key {session['key']}"
        )
    if not sessions:
        print("no sessions")
    return 0


def cmd_attach(args: argparse.Namespace) -> NoReturn:
    session = _session(args.id)
    nvim = shutil.which("nvim")
    if nvim is None:
        raise SystemExit("nvim-mcp: nvim is not on PATH")
    # A resolved path, a fixed argv, and a socket path the broker chose.
    # Replacing this process is what makes the terminal the session's UI.
    os.execv(nvim, [nvim, "--remote-ui", "--server", session["socket"]])  # noqa: S606


def cmd_kill(args: argparse.Namespace) -> int:
    reply = _ask({"cmd": "kill", "id": args.id})
    print("killed" if reply["ok"] else f"no session {args.id}")
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    """Serve MCP on stdio for a client running on the host."""
    _ensure_broker()
    return splice.splice(str(paths.agent_socket()))


def cmd_broker(_args: argparse.Namespace) -> int:
    from . import broker

    return broker.main()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `nv 3` is the common case and should not need a subcommand.
    if argv and argv[0].isdigit():
        argv = ["attach", *argv]

    parser = argparse.ArgumentParser(prog="nv", description="talk to nvim sessions")
    sub = parser.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="create a session rooted at a directory")
    new.add_argument("root", nargs="?", default=".")
    new.add_argument(
        "--clean",
        action="store_true",
        help="start nvim without your config and plugins",
    )
    # A headless nvim cannot ask the terminal, so it renders the dark palette
    # into a light one unless told.
    background = new.add_mutually_exclusive_group()
    background.add_argument(
        "--light",
        dest="background",
        action="store_const",
        const="light",
        help="the terminal you will attach from has a light background",
    )
    background.add_argument(
        "--dark",
        dest="background",
        action="store_const",
        const="dark",
        help="the terminal you will attach from has a dark background",
    )
    new.set_defaults(func=cmd_new, background=None)

    sub.add_parser("ls", help="list sessions").set_defaults(func=cmd_ls)

    attach = sub.add_parser("attach", help="attach this terminal to a session")
    attach.add_argument("id")
    attach.set_defaults(func=cmd_attach)

    kill = sub.add_parser("kill", help="stop a session")
    kill.add_argument("id")
    kill.set_defaults(func=cmd_kill)

    sub.add_parser("mcp", help="serve MCP on stdio").set_defaults(func=cmd_mcp)
    sub.add_parser("broker", help="run the broker in the foreground").set_defaults(
        func=cmd_broker
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
