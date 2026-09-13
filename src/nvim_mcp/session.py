# Copyright 2026 The nvim-mcp developers
# SPDX-License-Identifier: MIT

"""Own one headless nvim and apply agent requests to it.

The session outlives both the terminal a human attaches and the agent that
writes to it. Its id is the capability: it is created on the host with a root
the human chooses, and holding the id is what authorizes a client to use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clamp import Root
from .nvimrpc import NvimRPC
from .paths import nvim_socket

# Applied after the human's config loads, so it wins over what the config sets.
SESSION_INIT = """
-- Tabs hold files, the quickfix list holds positions; 'switchbuf' is what makes
-- :cnext move between tabs instead of displacing the window the human is in.
vim.o.switchbuf = 'usetab,newtab'
-- The human opens these same files in their everyday nvim; a session swapfile
-- would meet them with an E325 prompt.
vim.o.swapfile = false
-- The agent chooses which files open here. A modeline or project-local config
-- in one of them would run on the host at a moment the agent picks.
vim.o.modeline = false
vim.o.exrc = false
vim.api.nvim_set_hl(0, 'NvimMcpShow', { link = 'Visual', default = true })
-- Answer the file-changed prompt ourselves. Left to nvim it blocks the RPC
-- behind a dialog when a UI is attached and 'autoread' is off.
vim.api.nvim_create_autocmd('FileChangedShell', {
  group = vim.api.nvim_create_augroup('nvim-mcp', { clear = true }),
  callback = function(ev)
    vim.v.fcs_choice = vim.bo[ev.buf].modified and '' or 'reload'
  end,
})
"""

SHOW = """
local locs, opts = ...
local ns = vim.api.nvim_create_namespace('nvim-mcp-show')
local previous = vim.api.nvim_get_current_tabpage()

-- One live show set: drop the marks of the last one everywhere.
for _, buf in ipairs(vim.api.nvim_list_bufs()) do
  vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
end

local function displayed(buf)
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == buf then return true end
  end
  return false
end

local function scratch(win)
  local buf = vim.api.nvim_win_get_buf(win)
  return vim.api.nvim_buf_get_name(buf) == ''
    and not vim.bo[buf].modified
    and vim.api.nvim_buf_line_count(buf) == 1
    and vim.api.nvim_buf_get_lines(buf, 0, 1, true)[1] == ''
end

local items, opened, seen = {}, {}, {}
for _, loc in ipairs(locs) do
  -- bufadd takes the name literally. :edit and nvim_cmd would expand backticks
  -- in it and run a shell.
  local buf = vim.fn.bufadd(loc.file)
  vim.fn.bufload(buf)
  if not seen[loc.file] then
    seen[loc.file] = true
    opened[#opened + 1] = loc.file
    if not displayed(buf) then
      if not scratch(vim.api.nvim_get_current_win()) then vim.cmd('tabnew') end
      vim.api.nvim_win_set_buf(0, buf)
    end
  end
  items[#items + 1] = {
    bufnr = buf, lnum = loc.line or 1, col = 1, type = 'N', text = loc.text or '',
  }
  if loc.end_line then
    vim.api.nvim_buf_set_extmark(buf, ns, (loc.line or 1) - 1, 0, {
      end_row = loc.end_line, end_col = 0, hl_group = 'NvimMcpShow',
      hl_eol = true, strict = false,
    })
  end
end

vim.fn.setqflist(items, 'r')
vim.fn.setqflist({}, 'a', { title = opts.title })
if opts.focus then
  vim.cmd('cfirst')
else
  vim.api.nvim_set_current_tabpage(previous)
end
return { opened = opened, uis = #vim.api.nvim_list_uis() }
"""


@dataclass
class Location:
    file: str
    line: int = 1
    end_line: int | None = None
    text: str = ""


@dataclass
class Session:
    sid: str
    key: str
    root: Root
    socket: Path
    #: Start nvim without the human's config. Their plugins run in this session
    #: too, and one that authenticates or installs on startup blocks it.
    clean: bool = False
    process: asyncio.subprocess.Process | None = None
    rpc: NvimRPC | None = None
    #: Files refused by the clamp since the session started, for `nv ls`.
    refusals: int = field(default=0)

    @classmethod
    def create(cls, sid: str, root: str | Path, clean: bool = False) -> Session:
        return cls(
            sid=sid,
            clean=clean,
            # The short id is for humans to type; the secret is what authorizes.
            key=f"{sid}-{secrets.token_hex(12)}",
            root=Root.of(root),
            socket=nvim_socket(sid),
        )

    async def start(self) -> None:
        self.socket.unlink(missing_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            "nvim",
            *(["--clean"] if self.clean else []),
            "--headless",
            "--listen",
            str(self.socket),
            cwd=str(self.root.path),
        )
        await self._await_socket()
        self.rpc = await NvimRPC.connect(self.socket)
        await self.rpc.lua(SESSION_INIT)

    async def _await_socket(self, timeout: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.socket.exists():
                return
            await asyncio.sleep(0.02)
        raise RuntimeError(f"nvim did not listen on {self.socket} within {timeout}s")

    async def show(
        self, locations: list[Location], title: str, focus: bool
    ) -> dict[str, Any]:
        assert self.rpc is not None
        # Omit an absent end_line rather than sending nil: nvim decodes msgpack
        # NIL as vim.NIL, which is truthy in Lua.
        payload = [
            {
                "file": str(self.root.resolve(loc.file)),
                "line": loc.line,
                "text": loc.text,
            }
            | ({"end_line": loc.end_line} if loc.end_line is not None else {})
            for loc in locations
        ]
        # Agent edits reach disk without passing through the broker, so refresh
        # before showing anything.
        await self.rpc.request("nvim_command", "checktime")
        return await self.rpc.lua(SHOW, payload, {"title": title, "focus": focus})

    async def attached(self) -> bool:
        assert self.rpc is not None
        return bool(await self.rpc.request("nvim_list_uis"))

    async def close(self) -> None:
        if self.rpc is not None:
            with contextlib.suppress(Exception):
                await self.rpc.notify("nvim_command", "qall!")
            await self.rpc.close()
            self.rpc = None
        if self.process is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self.process = None
        self.socket.unlink(missing_ok=True)
