# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

"""`showme`: create sessions, attach a terminal to one, and inspect them.

Sessions are recorded here rather than by a client, because the root a
launcher clamps an agent to has to come from the human. Which session an agent
ends up on is not decided here: a launcher cannot know the conversation it is
about to start, so it prepares one per launch and the broker settles it when
the client says who it is.

This is also where a client on the host takes the key that reads outside that
root: it asks over the admin socket, and being able to reach it is the proof.
This shell's environment goes along, so the session's nvim finds the same
language servers and display the human's everyday one does; the broker that
spawns it was started from some other shell.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import textwrap
import time
import tty
from pathlib import Path
from typing import Any, NoReturn

from . import install, lifecycle, paths, splice


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
    with paths.broker_log().open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "showme.broker"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _ask({"cmd": "ping"}, timeout=1.0)
        except (OSError, RuntimeError):
            time.sleep(0.05)
        else:
            return
    raise SystemExit("showme: broker did not start")


def cmd_new(args: argparse.Namespace) -> int:
    _ensure_broker()
    reply = _ask(
        {
            "cmd": "new",
            # Resolved here: the broker's cwd is whichever shell first started
            # it, and a relative root would be taken against that.
            "root": str(Path(args.root).expanduser().resolve()),
            "clean": args.clean,
            "background": args.background or os.environ.get("SHOWME_BACKGROUND"),
            "env": session_env(),
        }
    )
    if not reply["ok"]:
        raise SystemExit(f"showme: {reply['error']}")
    print(f"session {reply['id']}  root {reply['root']}")
    print(f"attach:  showme {reply['id']}")
    print(f"give the agent this key:  {reply['key']}")
    return 0


#: What a session's nvim is given of the shell that asked for it. The whole
#: environment used to go, and it is written to the state file with the
#: session: an interactive shell carries tokens that have no business being
#: there. These are what an editor needs to look and behave like the human's
#: own -- where its tools are, what its terminal is, which display to open a
#: window on -- plus the prefixes a session legitimately reads.
ENV_KEEP = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "TERM",
        "COLORTERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "TMUX",
        "TZ",
        "LANG",
        "LANGUAGE",
        "EDITOR",
        "VISUAL",
        "PAGER",
    }
)
ENV_KEEP_PREFIXES = ("LC_", "XDG_", "NVIM_", "VIM", "SHOWME_")


def session_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name in ENV_KEEP or name.startswith(ENV_KEEP_PREFIXES)
    }


def _session_for(args: argparse.Namespace, command: str) -> dict[str, Any]:
    _ensure_broker()
    reply = _ask(
        {
            "cmd": command,
            "root": str(Path(args.root).expanduser().resolve()),
            "clean": args.clean,
            "background": args.background or os.environ.get("SHOWME_BACKGROUND"),
            "env": session_env(),
        },
        timeout=30.0,
    )
    if not reply["ok"]:
        raise SystemExit(f"showme: {reply['error']}")
    return reply


def cmd_ensure(args: argparse.Namespace) -> int:
    print(json.dumps(_session_for(args, "ensure")))
    return 0


#: Box directories left by a launcher that died before it could clean up. The
#: key inside one names a session that is probably gone, and nothing reads a
#: stale directory, so age is the only signal worth acting on.
BOX_MAX_AGE = 24 * 60 * 60


def cmd_box(args: argparse.Namespace) -> int:
    """Prepare one sandbox launch and print the bind spec that carries it.

    The session comes first, because the root is the human's to choose and a
    launcher knows it: the directory the human started the agent in. The key
    goes in a directory of its own, which the caller binds read-only into that
    box and no other, so the agent inside never handles a key and cannot name
    a session it was not given.
    """
    reply = _session_for(args, "ensure")
    _sweep_boxes()
    token = secrets.token_hex(8)
    directory = paths.box_dir() / token
    directory.mkdir(mode=0o700, parents=True)
    key = directory / "key"
    key.touch(mode=0o600)
    key.write_text(reply["key"])
    spec = paths.box_dir() / f"{token}.toml"
    # Only the key directory. Everything else a box needs is in the specs the
    # caller already passes, and an entry here would apply to one launch.
    spec.write_text(f'ro = [\n    "{directory}",\n]\n')
    # stdout is the one thing a launcher consumes; the rest is for the human.
    print(spec)
    # Resume may select a different session id; attach by root instead.
    print(
        f"showme: session for {reply['root']}\n"
        f"showme: attach with:  showme {reply['root']}",
        file=sys.stderr,
    )
    return 0


def _sweep_boxes() -> None:
    now = time.time()
    for path in paths.box_dir().glob("*"):
        with contextlib.suppress(OSError):
            if now - path.stat().st_mtime < BOX_MAX_AGE:
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()


def cmd_ls(_args: argparse.Namespace) -> int:
    try:
        sessions = _ask({"cmd": "ls"})["sessions"]
    except OSError:
        print("no broker running")
        return 0
    for session in sessions:
        state = "attached" if session["attached"] else "detached"
        # Frame summaries distinguish conversations in the same tree.
        showing = session.get("showing") or "nothing shown"
        print(
            f"{session['id']:>3}  {state:<8}  {session['root']}\n"
            f"     {showing}  key {session['key']}"
        )
    if not sessions:
        print("no sessions")
    return 0


#: OSC 11 asks the terminal for its background colour; the reply names it as
#: one to four hex digits per channel.
_OSC11_QUERY = b"\x1b]11;?\x07"
_OSC11_REPLY = re.compile(
    rb"\x1b\]11;rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})"
)


def _background_of(red: str, green: str, blue: str) -> str:
    """Classify an OSC 11 reply as 'light' or 'dark' by luma.

    Channels carry one to four hex digits and each scales against its own
    width, so "f", "ff" and "ffff" all mean full intensity.
    """
    r, g, b = (int(c, 16) / (16 ** len(c) - 1) for c in (red, green, blue))
    return "light" if (0.299 * r + 0.587 * g + 0.114 * b) > 0.5 else "dark"


def _detect_background(timeout: float = 0.15) -> str | None:
    """Ask the terminal this process is attached to for its background.

    The session's nvim is headless and never sees a terminal, so nothing there
    can answer this. Only the process the human runs can, which is why the
    query belongs here and not in the broker. Returns None when there is no
    terminal or it stays silent, leaving the session's own setting to stand.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        # Not a terminal, so there is nothing to ask.
        os.close(fd)
        return None
    try:
        return _read_osc11(fd, timeout)
    except OSError:
        return None
    finally:
        # Raw mode and the descriptor are ours for the length of the query
        # only; the human's terminal has to go back the way it was.
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.close(fd)


def _read_osc11(fd: int, timeout: float) -> str | None:
    """Send the query on `fd` and read until the reply or `timeout`."""
    tty.setraw(fd)
    os.write(fd, _OSC11_QUERY)
    buf = b""
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        buf += chunk
        if found := _OSC11_REPLY.search(buf):
            return _background_of(*(g.decode() for g in found.groups()))
    return None


def cmd_attach(args: argparse.Namespace) -> NoReturn:
    _ensure_broker()
    # The terminal is here, and the reply is what it looks like right now, so
    # ask before handing the process over to nvim. The broker keeps an explicit
    # --light or --dark ahead of this.
    background = os.environ.get("SHOWME_BACKGROUND") or _detect_background()
    # The broker starts the session's nvim if it has to; a socket path alone
    # would be an error when nothing listens on it.
    # Resolve against this shell's cwd, which can differ from the broker's.
    reply = _ask(
        {"cmd": "attach", "id": _attach_target(args.id), "background": background},
        timeout=30.0,
    )
    if not reply["ok"]:
        raise SystemExit(f"showme: {reply['error']}")
    nvim = shutil.which("nvim")
    if nvim is None:
        raise SystemExit("showme: nvim is not on PATH")
    # A resolved path, a fixed argv, and a socket path the broker chose.
    # Replacing this process is what makes the terminal the session's UI.
    os.execv(nvim, [nvim, "--remote-ui", "--server", reply["socket"]])  # noqa: S606


def cmd_kill(args: argparse.Namespace) -> int:
    reply = _ask({"cmd": "kill", "id": args.id})
    print("killed" if reply["ok"] else f"no session {args.id}")
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    """Serve MCP on stdio, for a client on the host or inside a sandbox.

    The splice outlives the broker: when nothing answers the agent socket, this
    starts one and the new broker adopts the sessions. A sandboxed client runs
    the same command with the agent directory mounted read-only, where the
    broker started here cannot claim the socket, so the one already serving it
    on the host stays the only one -- and cannot ask for a broker at all,
    which is why it is not offered the chance to wait for one.
    """
    sandboxed = paths.sandboxed()
    key = paths.box_key() if sandboxed else _session_here()
    return splice.splice(
        str(paths.agent_socket()),
        revive=None if sandboxed else _revive_broker,
        key=key,
    )


def _session_here() -> str | None:
    """Prepare a launcher session and return its host key.

    The admin socket is inaccessible to sandboxes. Its host key permits reads
    outside the root, which still sets relative paths and nvim's cwd. The hello
    selects an existing session when the harness supplies a conversation id.
    """
    try:
        _ensure_broker()
        reply = _ask(
            {
                "cmd": "ensure",
                "root": str(Path.cwd()),
                "env": session_env(),
                "open": True,
            }
        )
    except (OSError, RuntimeError, SystemExit):
        # No broker and no way to start one. The client still comes up, and
        # says what it cannot do when a tool is called.
        return None
    if not reply.get("ok"):
        # A root the human would not have chosen, most likely. Nothing is
        # refused outright: they can still name a session by hand.
        print(f"showme: {reply.get('error')}", file=sys.stderr)
        return None
    return str(reply["key"])


def _revive_broker() -> None:
    with contextlib.suppress(SystemExit):
        _ensure_broker()


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"showme: {question} -- not a terminal, so nothing was changed.")
        print("showme: pass --yes to write it anyway.")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def cmd_install(args: argparse.Namespace) -> int:
    """Register the tools with one harness and say what to do next."""
    harness = install.HARNESSES[args.harness]
    if shutil.which("nvim") is None:
        raise SystemExit(
            "showme: nvim is not on PATH, and it is the whole point.\n"
            "showme: install it first, then run this again."
        )

    command = install.showme_command()
    try:
        path, shown, text = install.plan(harness, command)
    except install.Unparsable as e:
        # Better a snippet the human pastes than a file this one rewrote by
        # guessing at what it meant.
        print(f"showme: {e}")
        print("showme: add this to it yourself:\n")
        print(textwrap.indent(install.snippet(harness, command), "    "))
        return 1

    if text is None:
        print(f"showme: {harness.label} already starts {install.NAME} from {path}")
    else:
        print(f"showme: this goes in {path}:\n")
        print(textwrap.indent(shown, "    "))
        print()
        if not _confirm(f"Add it to {harness.label}?", args.yes):
            return 1
        backup = install.apply(path, text)
        print(f"showme: written{f'; previous copy at {backup}' if backup else ''}")

    _offer_terminal(args.yes)
    print(GREETING.format(harness=harness.label))
    return 0


def _offer_terminal(assume_yes: bool) -> None:
    """Set up the window a session opens when nobody is watching it."""
    if os.environ.get(lifecycle.TERMINAL):
        return
    suggestion = install.terminal_suggestion()
    if suggestion is None:
        return
    line = f'export {lifecycle.TERMINAL}="{suggestion}"'
    print(
        f"\nshowme: with {lifecycle.TERMINAL} set, a session opens its own window\n"
        "showme: the first time an agent shows you something and nothing is\n"
        f"showme: on screen. For this terminal that is:\n\n    {line}\n"
    )
    rc = install.shell_rc()
    if rc is not None and rc.exists() and lifecycle.TERMINAL in rc.read_text():
        # Already written by an earlier run, or by hand. Appending a second
        # export would only be confusing.
        print(f"showme: {rc} already sets it; start a new shell to pick it up.")
        return
    if rc is None or not _confirm(f"Append it to {rc}?", assume_yes):
        print("showme: add it to your shell yourself, or leave it out.")
        return
    with rc.open("a") as handle:
        handle.write(
            f"\n# Opens a window for a showme session nobody is watching.\n{line}\n"
        )
    print(f"showme: appended to {rc}; it applies to new shells.")


GREETING = """
showme is set up for {harness}. Restart it so it picks the server up.

  Then just ask it to show you something. It gets a session for whatever
  directory you started it in, nvim starts with the first thing it shows,
  and a window opens for it. Nothing to start, nothing to paste.

  Your side, in that window:
    :Ask why this?   hand the line and your question back to the agent
    :3,5Ask ...      hand a range
    :Ref             copy a reference to the line, to paste into the chat
    :AgentPop        drop the top frame of notes
    :q               safe -- the session comes back with the notes where
                     your edits left them

  `showme ls` lists what is running, if you ever want to look.
"""


def cmd_restart_broker(_args: argparse.Namespace) -> int:
    """Replace the running broker with one built from the code on disk.

    A broker holds `session.lua` and its own modules from the moment it
    started, so a change to either reaches a session only after this. nvim is
    untouched: the sessions are adopted by the new broker, and a session whose
    Lua changed needs its nvim restarted too, which `:q` does.
    """
    try:
        reply = _ask({"cmd": "stop"})
    except OSError:
        print("showme: no broker running")
    else:
        if reply.get("ok"):
            print(f"showme: stopping, {reply['sessions']} session(s) to hand over")
        else:
            # A broker old enough not to know the command is exactly the one
            # worth replacing, so ask the socket who is listening and signal
            # it. State is written as it changes, not at exit, so what the
            # next broker restores is the same either way.
            _terminate_broker(reply.get("error", "stop refused"))
        # The lock, not the socket: the old broker unlinks its sockets before
        # it releases the lock, and a new one that starts too early finds the
        # lock held and exits without a word.
        if not _lock_free(10.0):
            raise SystemExit("showme: the broker is still running")
    _ensure_broker()
    sessions = _ask({"cmd": "ls"})["sessions"]
    print(f"showme: broker restarted with {len(sessions)} session(s)")
    return 0


def _terminate_broker(reason: str) -> None:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(str(paths.admin_socket()))
        credentials = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    pid, _, _ = struct.unpack("3i", credentials)
    print(f"showme: {reason}; signalling broker {pid}")
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)


def _lock_free(timeout: float) -> bool:
    """Wait for the broker's lock to be released, which happens as it exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with paths.lock_path().open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True
    return False


def cmd_broker(_args: argparse.Namespace) -> int:
    from . import broker

    return broker.main()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="showme", description="talk to nvim sessions")
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

    for name, func, help_text in (
        (
            "ensure",
            cmd_ensure,
            "print the session for a root as JSON, making one if needed",
        ),
        ("box", cmd_box, "prepare a sandbox launch and print the bind spec"),
    ):
        rooted = sub.add_parser(name, help=help_text)
        rooted.add_argument("root", nargs="?", default=".")
        rooted.add_argument("--clean", action="store_true", help=argparse.SUPPRESS)
        rooted.set_defaults(func=func, background=None)

    sub.add_parser("ls", help="list sessions").set_defaults(func=cmd_ls)

    attach = sub.add_parser("attach", help="attach this terminal to a session")
    attach.add_argument("id", help="a session id, or a directory one is rooted in")
    attach.set_defaults(func=cmd_attach)

    kill = sub.add_parser("kill", help="stop a session")
    kill.add_argument("id")
    kill.set_defaults(func=cmd_kill)

    installer = sub.add_parser("install", help="register the tools with a harness")
    installer.add_argument("harness", choices=sorted(install.HARNESSES))
    installer.add_argument(
        "--yes", action="store_true", help="do not ask before writing"
    )
    installer.set_defaults(func=cmd_install)

    sub.add_parser(
        "restart-broker", help="replace the broker with one built from the code on disk"
    ).set_defaults(func=cmd_restart_broker)

    sub.add_parser("mcp", help="serve MCP on stdio").set_defaults(func=cmd_mcp)
    sub.add_parser("broker", help="run the broker in the foreground").set_defaults(
        func=cmd_broker
    )

    # `showme 3` and `showme ~/src/thing` are the common cases and should not
    # need a subcommand. A command name always wins, so a directory that
    # happens to be called `ls` is reached by spelling out `showme attach ls`.
    if argv and argv[0] not in sub.choices and _attachable(argv[0]):
        argv = ["attach", *argv]

    args = parser.parse_args(argv)
    return args.func(args)


def _attach_target(argument: str) -> str:
    if argument.isdigit():
        return argument
    return str(Path(argument).expanduser().resolve())


def _attachable(argument: str) -> bool:
    if not argument:
        return False
    if argument.isdigit():
        return True
    try:
        return Path(argument).expanduser().is_dir()
    except (OSError, RuntimeError):
        # A `~someone` with no such user, which expanduser raises on rather
        # than leaving alone, or a word too long to be a path at all.
        return False


if __name__ == "__main__":
    raise SystemExit(main())
