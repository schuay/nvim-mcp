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
local background = ...
local group = vim.api.nvim_create_augroup('nvim-mcp', { clear = true })

local function options()
  -- The session is headless, so it never queried the terminal. Without this it
  -- renders the dark palette into a light terminal.
  if background ~= nil and vim.o.background ~= background then
    vim.o.background = background
  end
  -- The UI attaches before it knows the server's colour depth. Setting this
  -- sends it a termguicolors option event, which switches it to truecolor.
  vim.o.termguicolors = true
  -- Tabs hold files, the quickfix list holds positions; 'switchbuf' is what
  -- makes :cnext move between tabs instead of displacing the human's window.
  vim.o.switchbuf = 'usetab,newtab'
  -- The human opens these same files in their everyday nvim; a session
  -- swapfile would meet them with an E325 prompt.
  vim.o.swapfile = false
  -- The agent chooses which files open here. A modeline or project-local
  -- config in one of them would run on the host at a moment the agent picks.
  vim.o.modeline = false
  vim.o.exrc = false
end

-- A note must not read as code, and a foreground colour does not carry that:
-- a colourscheme's diagnostic blue against its comment green is a small
-- difference, and both sit on the same background as the code. Give the note
-- its own band instead, mixed from the diagnostic colour and the editor
-- background so it follows whatever colourscheme the human runs, and pick the
-- text colour that reads better on the result.
--
-- The derived groups are force-set so a colourscheme change re-colours them,
-- while the groups the extmarks name only link to them by default, which keeps
-- a human's own NvimMcpNote or NvimMcpShow across colourscheme changes.
local function channels(rgb)
  return math.floor(rgb / 65536) % 256, math.floor(rgb / 256) % 256, rgb % 256
end

local function mix(top, bottom, alpha)
  local tr, tg, tb = channels(top)
  local br, bg, bb = channels(bottom)
  local function blend(a, b) return math.floor(a * alpha + b * (1 - alpha) + 0.5) end
  return blend(tr, br) * 65536 + blend(tg, bg) * 256 + blend(tb, bb)
end

local function luminance(rgb)
  local r, g, b = channels(rgb)
  local function channel(value)
    value = value / 255
    if value <= 0.03928 then return value / 12.92 end
    return ((value + 0.055) / 1.055) ^ 2.4
  end
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
end

local function contrast(a, b)
  local high, low = math.max(luminance(a), luminance(b)), math.min(luminance(a), luminance(b))
  return (high + 0.05) / (low + 0.05)
end

local function styles()
  local info = vim.api.nvim_get_hl(0, { name = 'DiagnosticInfo', link = false })
  local normal = vim.api.nvim_get_hl(0, { name = 'Normal', link = false })
  local note = { italic = true }
  if info.fg and normal.bg and normal.fg then
    -- Two constraints, not one: the band has to stand off the code background,
    -- and its own text has to stay readable on it. Mixing in ever more
    -- diagnostic colour satisfies the first and destroys the second, because a
    -- saturated mid-tone band sits far from the editor foreground and its
    -- background alike. So take the most separated band whose text still reads.
    --
    -- The readability bar follows the colourscheme rather than a fixed 4.5:1.
    -- Some schemes set their own body text near 4.5, and holding the band to a
    -- higher standard than the code around it would reject every candidate.
    local readable = math.min(4.5, contrast(normal.fg, normal.bg) * 0.9)
    local best, fallback
    for _, alpha in ipairs({ 0.12, 0.2, 0.3, 0.45, 0.6, 0.75, 0.9 }) do
      local band = mix(info.fg, normal.bg, alpha)
      local fg = contrast(normal.fg, band) >= contrast(normal.bg, band) and normal.fg
        or normal.bg
      local candidate = { bg = band, fg = fg, read = contrast(fg, band),
                          apart = contrast(band, normal.bg) }
      if candidate.read >= readable and (not best or candidate.apart > best.apart) then
        best = candidate
      end
      if not fallback or candidate.read > fallback.read then
        fallback = candidate
      end
    end
    -- Always produce a band. A note that falls back to coloured text is the
    -- thing this whole derivation exists to avoid.
    best = best or fallback
    note.bg, note.fg = best.bg, best.fg
  else
    note.link = 'DiagnosticInfo'
  end
  vim.api.nvim_set_hl(0, 'NvimMcpNoteDefault', note)
  vim.api.nvim_set_hl(0, 'NvimMcpShowDefault', { link = 'Visual' })
  vim.api.nvim_set_hl(0, 'NvimMcpNote', { link = 'NvimMcpNoteDefault', default = true })
  vim.api.nvim_set_hl(0, 'NvimMcpShow', { link = 'NvimMcpShowDefault', default = true })
end

-- nvim listens on its socket before it has finished starting, so this runs
-- while highlight groups are still undefined and before the human's config has
-- had its say about these options. Repeat both once startup is done.
options()
styles()
vim.api.nvim_create_autocmd('VimEnter', {
  group = group,
  callback = function()
    options()
    styles()
  end,
})
vim.api.nvim_create_autocmd('ColorScheme', { group = group, callback = styles })

-- Answer the file-changed prompt ourselves. Left to nvim it blocks the RPC
-- behind a dialog when a UI is attached and 'autoread' is off.
vim.api.nvim_create_autocmd('FileChangedShell', {
  group = group,
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

-- Virtual lines ignore 'wrap' and this nvim offers no wrapping overflow mode,
-- so a long note is cut off at the window edge with nothing to show it
-- continued. Fold it here instead, against the width of a window showing the
-- buffer. The width is taken once, so resizing the terminal does not re-fold.
local function textwidth(buf)
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == buf then
      local info = vim.fn.getwininfo(win)[1]
      return math.max(40, info.width - info.textoff - 4)
    end
  end
  return math.max(40, vim.o.columns - 4)
end

local function fold(text, width)
  local lines, line, indent = {}, '', '  '
  for word in text:gmatch('%S+') do
    if line == '' then
      line = indent .. word
    elseif #line + 1 + #word <= width then
      line = line .. ' ' .. word
    else
      lines[#lines + 1] = line
      indent = '     '
      line = indent .. word
    end
  end
  if line ~= '' then lines[#lines + 1] = line end
  return lines
end

local function scratch(win)
  local buf = vim.api.nvim_win_get_buf(win)
  return vim.api.nvim_buf_get_name(buf) == ''
    and not vim.bo[buf].modified
    and vim.api.nvim_buf_line_count(buf) == 1
    and vim.api.nvim_buf_get_lines(buf, 0, 1, true)[1] == ''
end

local items, opened, seen, moved = {}, {}, {}, {}
for _, loc in ipairs(locs) do
  -- The path was resolved before this call. Resolve it again here, because the
  -- agent holds the root read-write and could have pointed a component
  -- somewhere else in between. This narrows that window; it does not close it,
  -- since nvim opens by name and not by descriptor.
  local real = vim.uv.fs_realpath(loc.file)
  if real ~= opts.root and (real == nil or real:sub(1, #opts.root + 1) ~= opts.root .. '/') then
    moved[#moved + 1] = loc.file
    goto continue
  end
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
  -- Clamp to the buffer. An end past the last line does not truncate the
  -- highlight: nvim collapses it onto the start row and nothing is drawn.
  local last = vim.api.nvim_buf_line_count(buf)
  local first = math.max(1, math.min(loc.line or 1, last))
  items[#items + 1] = {
    bufnr = buf, lnum = first, col = 1, type = 'N', text = loc.text or '',
  }
  if loc.end_line then
    local final = math.max(first, math.min(loc.end_line, last))
    local end_row, end_col = final, 0
    if final >= last then
      end_row = last - 1
      end_col = #(vim.api.nvim_buf_get_lines(buf, last - 1, last, true)[1] or '')
    end
    vim.api.nvim_buf_set_extmark(buf, ns, first - 1, 0, {
      end_row = end_row, end_col = end_col, hl_group = 'NvimMcpShow', hl_eol = true,
    })
  end
  -- The label goes above the code as virtual lines: not buffer text, so the
  -- file is untouched and the human cannot edit or yank it by accident.
  if loc.text ~= '' then
    local lines = {}
    for _, text in ipairs(fold(loc.text, textwidth(buf))) do
      -- An empty final chunk extends the highlight to the end of the screen
      -- line, so the note reads as a band rather than a run of coloured text.
      lines[#lines + 1] = { { text, 'NvimMcpNote' }, { '', 'NvimMcpNote' } }
    end
    vim.api.nvim_buf_set_extmark(buf, ns, first - 1, 0, {
      virt_lines = lines,
      virt_lines_above = true,
    })
  end
  ::continue::
end

vim.fn.setqflist(items, 'r')
vim.fn.setqflist({}, 'a', { title = opts.title })
if opts.focus then
  vim.cmd('cfirst')
else
  vim.api.nvim_set_current_tabpage(previous)
end
return { opened = opened, moved = moved, uis = #vim.api.nvim_list_uis() }
"""


#: A note is a label, not a document. Longer than this and it buries the code
#: it points at, however the editor folds it.
NOTE_LIMIT = 400


def one_line(text: str) -> str:
    """Reduce a label to printable characters within the length limit.

    virt_lines accepts newlines and escape sequences without complaint, and the
    label is rendered in the human's terminal.
    """
    printable = "".join(ch for ch in text if ch.isprintable()).strip()
    if len(printable) <= NOTE_LIMIT:
        return printable
    return printable[: NOTE_LIMIT - 3] + "..."


@dataclass
class Location:
    #: Already resolved against the session root. Resolving once keeps the
    #: window between the check and nvim's open as small as it can be here.
    file: Path
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
    #: 'light' or 'dark', taken from the human's terminal. A headless nvim
    #: cannot detect it.
    background: str | None = None
    process: asyncio.subprocess.Process | None = None
    rpc: NvimRPC | None = None
    #: Files refused by the clamp since the session started, for `nv ls`.
    refusals: int = field(default=0)

    @classmethod
    def create(
        cls,
        sid: str,
        root: str | Path,
        clean: bool = False,
        background: str | None = None,
    ) -> Session:
        # The short id is for humans to type; the secret is what authorizes.
        key = f"{sid}-{secrets.token_hex(12)}"
        return cls(
            sid=sid,
            clean=clean,
            background=background,
            key=key,
            root=Root.of(root),
            # Name the socket after the key, not the reusable id. nvim unlinks
            # its listen socket when it exits, and a dying predecessor sharing
            # the path would delete a live successor's socket.
            socket=nvim_socket(key),
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
        await self.rpc.lua(SESSION_INIT, self.background)

    async def _await_socket(self, timeout: float = 10.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.socket.exists():
                return
            # A configuration error kills nvim in milliseconds. Without this the
            # failure is reported as a timeout, naming the wrong cause.
            if self.process is not None and self.process.returncode is not None:
                raise RuntimeError(f"nvim exited with status {self.process.returncode}")
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
                "file": str(loc.file),
                "line": loc.line,
                "text": one_line(loc.text),
            }
            | ({"end_line": loc.end_line} if loc.end_line is not None else {})
            for loc in locations
        ]
        # Agent edits reach disk without passing through the broker, so refresh
        # before showing anything.
        await self.rpc.request("nvim_command", "checktime")
        return await self.rpc.lua(
            SHOW, payload, {"title": title, "focus": focus, "root": str(self.root.path)}
        )

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
