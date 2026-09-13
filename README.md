# nvim-mcp

nvim-mcp gives a coding agent and the human reviewing its work one shared view
of the code. The agent opens the locations it is talking about and annotates
them at the line; the human marks a range in the editor and asks about it.

```sh
nv new ~/src/v8      # create a session, print its key and attach command
nv 3                 # attach a terminal to session 3
```

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

Set `NVIM_MCP_TERMINAL` in the shell you start sessions from, to a terminal
and whatever it needs before a command -- `ghostty -e`, `kitty`, `alacritty
-e` -- and a session gets a window of its own the first time an agent shows
something to it while nothing is on screen. One window per nvim, so closing it
closes it. No setting, or no display in that shell, and you get the attach
command instead. An agent cannot ask for a window: it only ever happens
because it showed you something you were not looking at.

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
after changing either, `nv restart-broker` puts a new one on the same sockets
and hands it the sessions. Changed Lua reaches a session when its nvim next
starts, which `:q` does.

## What the broker will not do

- No command writes a file, saves a buffer, or edits buffer text. The agent
  edits on disk with its own tools, and the broker reloads unmodified buffers.
- Every read an agent asks for is clamped to the session's root. A human mark
  is the one exception, because only the human creates one.
- No agent-supplied string reaches an Ex command line. A file named
  ``a`touch /tmp/x`.c`` runs the shell through `:edit` and through
  `nvim_cmd`'s structured arguments alike, so paths are opened with `bufadd`.
- The session key is the capability. `nv new` sets the root on the host and
  prints an unguessable key; handing that key to an agent is what lets it use
  the session.

## From a sandbox

The MCP client can run confined. The box needs one mount, the directory holding
the agent socket, read-only:

```
~/.local/share/nvim-mcp
```

`connect` works through a read-only mount and `bind` does not, so the box
reaches the broker on the host, and the broker a client starts when nothing
answers dies in there instead of serving a second, empty set of sessions under
the key you pasted. Nothing else has to go in, and the nvim listen sockets must
not: raw nvim RPC runs arbitrary Lua, so one of those sockets hands over the
host. `nv mcp` is the command on both sides. The directory also holds a
standalone copy of the relay, for a box without nvim-mcp installed:

```sh
python3 ~/.local/share/nvim-mcp/splice.py ~/.local/share/nvim-mcp/agent.sock
```

## Install

```sh
uv tool install .                                # nv on PATH
claude mcp add --scope user nvim -- nv mcp       # or your client's equivalent
```

For development:

```sh
uv sync
./scripts/install-hooks
.venv/bin/python -m pytest -q      # most tests drive a real nvim
```

State lives under `~/.local/state/nvim-mcp`: the session record, and logs for
the broker and each session's nvim.
