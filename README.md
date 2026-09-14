# showme

showme gives a coding agent and the human reviewing its work one shared view
of the code. The agent opens the locations it is talking about and annotates
them at the line; the human marks a range in the editor and asks about it.

```sh
uv tool install git+https://github.com/schuay/showme-mcp
showme install claude # or codex, gemini, opencode -- shows the change and asks
```

Then ask your agent to show you something. It takes a session for the
directory it was started in, nvim starts with the first thing it shows, and a
window opens for it if `SHOWME_TERMINAL` names your terminal. There is
nothing to start and no key to paste.

`showme new <root>` still makes a session by hand, `showme ls` lists them, and
`showme 3` attaches a terminal to one. A sandboxed agent cannot reach that side
at all, so its launcher passes it a key instead; see below.

The agent reaches the same session through MCP and puts code in front of you
instead of quoting line numbers at you.

## Using it

The agent has two tools. `show` opens a set of locations: a tab per file, a
quickfix list of the positions, a highlight over any range, and a note above
each line. `read` reports what you are looking at: your cursor and selection,
the contents of an open buffer including unsaved edits, the open buffers, and
the questions you have asked.

Notes live in frames. A show replaces the top frame by default; the agent can
push a frame for a digression and pop it when answered, and every live frame
stays on screen with the quickfix list walking the top one. Each note carries
an id, `A2` or `B1`, that you and the agent can both say.

On your side:

- Ctrl-Alt-PageDown and Ctrl-Alt-PageUp walk the quickfix list of notes,
  wrapping at the ends; Ctrl-Shift is left alone because terminals take it for
  their own scrollback.
- `:3,5Ask why is this here?` hands lines 3 to 5 and your question to the
  agent, tagged with the note it sits on. The agent collects questions with
  `read`, and a question stays pending until the agent has answered it.
- `:Ref` copies a reference to the current line, `:7,9Ref` to a range, in the
  form the agent reads them back: `src/main.c:7-9`, relative to the session
  root. It goes to the clipboard register, so nvim picks the tool your session
  uses.
- `:AgentPop` drops the top frame of notes; `:AgentDrop B` drops a named one.
- A question keeps its `?>` sign until an agent acknowledges it, so a batch of
  replies shows which ones have come back.
- `:q` is safe. The broker holds the record of what is shown and starts nvim
  again, with the notes on the lines your edits moved them to.

Set `SHOWME_TERMINAL` in the shell you start sessions from, to a terminal
and whatever it needs before a command -- `ghostty -e`, `kitty`, `alacritty
-e` -- and a session gets a window of its own the first time an agent shows
something to it while nothing is on screen. One window per nvim, so closing it
closes it. An agent cannot ask for a window: it only ever happens because it
showed you something you were not looking at.

Over ssh nothing can open, so the agent hands you the command instead. A show
to a session with no terminal on it comes back saying nobody is watching and
naming `showme 3`, which you run in a second shell on the same host; from then
on the session is on your screen and shows land there. Run it before you ask
for anything and there is nothing to hand over: the session exists from the
moment the agent's client starts, so `showme ls` already lists it.

## How it fits together

A host-side broker daemon owns every session. nvim runs headless, so a session
outlives the terminal you attach to and the agent that writes to it, and the
broker keeps the record of notes and questions, so it outlives nvim too:

```
broker (host)  --msgpack-RPC-->  nvim --headless --listen
   ^                                  ^
   | MCP over a UNIX socket           | nvim --remote-ui
   |                                  |
MCP client, in a sandbox or not       your terminal, any terminal
```

Clients speak MCP itself over a UNIX socket. The client side is a small relay
between stdin/stdout and that socket that knows the framing and nothing of
the tools, so the broker's tool list is its entire exposed surface. The relay
survives a broker restart: it reconnects on the next call and replays the
handshake, so the client never notices. The broker in turn survives its own
restart by adopting the nvim it left running, with your unsaved edits in it.

A broker holds `session.lua` and its own code from the moment it started, so
after changing either, `showme restart-broker` puts a new one on the same
sockets and hands it the sessions. Changed Lua reaches a session when its nvim
next starts, which `:q` does.

## What the broker will not do

- No command writes a file, saves a buffer, or edits buffer text. The agent
  edits on disk with its own tools, and the broker reloads unmodified buffers.
- A sandboxed agent reads only inside the session's root. An agent on the host
  is not held to it: it reads your files with its own tools and edits them
  there, so a clamp would only refuse it the worktree next door. The root still
  says where its relative paths are taken from and where nvim runs. A human
  mark is outside both, because only the human creates one.
- No agent-supplied string reaches an Ex command line. A file named
  ``a`touch /tmp/x`.c`` runs the shell through `:edit` and through
  `nvim_cmd`'s structured arguments alike, so paths are opened with `bufadd`.
- The session key is the capability. `showme new` sets the root on the host and
  prints an unguessable key; handing that key to an agent is what lets it use
  the session. Every key you can see is clamped to the root. The one that is
  not belongs to a client that asked for it over the admin socket, which no
  sandbox can reach -- reaching it is the proof that the client is you.

## From a sandbox

The MCP client can run confined. The box needs one mount, the directory holding
the agent socket, read-only:

```
~/.local/share/showme
```

`connect` works through a read-only mount and `bind` does not, so the box
reaches the broker on the host, and the broker a client starts when nothing
answers dies in there instead of serving a second, empty set of sessions under
the key you pasted. Nothing else has to go in, and the nvim listen sockets must
not: raw nvim RPC runs arbitrary Lua, so one of those sockets hands over the
host. `showme mcp` is the command on both sides. The directory also holds a
standalone copy of the relay, for a box without showme installed:

```sh
python3 ~/.local/share/showme/splice.py ~/.local/share/showme/agent.sock
```

## Install

```sh
uv tool install git+https://github.com/schuay/showme-mcp
claude mcp add --scope user showme -- showme mcp  # or your client's equivalent
```

From a clone, `uv tool install .` installs that working tree instead.

For development:

```sh
uv sync
./scripts/install-hooks
.venv/bin/python -m pytest -q      # most tests drive a real nvim
```

State lives under `~/.local/state/showme`: the session record, and logs for
the broker and each session's nvim.
