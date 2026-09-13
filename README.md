# nvim-mcp

nvim-mcp gives a coding agent and the human reviewing its work one shared view
of the code. The agent opens the locations it is talking about and annotates
them at the line; the human marks a range in the editor and asks about it.

```sh
nv new ~/src/v8      # create a session, print its id and attach command
nv 3                 # attach a terminal to session 3
```

The agent reaches the same session through MCP and puts code in front of you
instead of quoting line numbers at you.

## How it fits together

A host-side broker daemon owns every session. nvim runs headless, so a session
outlives the terminal you attach to and the agent that writes to it:

```
broker (host)  --msgpack-RPC-->  nvim --headless --listen
   ^                                  ^
   | MCP over a UNIX socket           | nvim --remote-ui
   |                                  |
MCP client, in a sandbox or not       your terminal, any terminal
```

Clients speak MCP itself over a UNIX socket, and the client side is a byte
splice between stdin/stdout and that socket. The broker's tool list is
therefore its entire exposed surface.

## Confinement

The broker is built to be reachable from inside a sandbox that holds a
possibly hostile agent, so it exposes no way to write:

- No command writes a file, saves a buffer, or edits buffer text.
- Every read an agent asks for is clamped to the session's root. A human mark
  is the one exception, because only the human creates one.
- No agent-supplied string reaches an Ex command line. A file named
  ``a`touch /tmp/x`.c`` runs the shell through `:edit` and through
  `nvim_cmd`'s structured arguments alike, so paths are opened with `bufadd`.
- The session id is the capability. `nv new` sets the root on the host and
  prints an unguessable id; handing that id to an agent is what authorizes it.

## Install

```sh
uv sync
./scripts/install-hooks
```
