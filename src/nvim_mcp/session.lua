-- Copyright 2026 The nvim-mcp developers
-- SPDX-License-Identifier: MIT

-- The session's half inside nvim. Run once by the broker after connecting.
--
-- The broker holds the record of what a session shows and what the human has
-- handed back; this side draws it and reports what only nvim can know: where a
-- note's anchor has moved to as the human edits, and what the human asked.
-- Client values arrive as arguments to nvim_exec_lua calls, never inside the
-- code.

local opts, frames = ...
local background = opts.background
local group = vim.api.nvim_create_augroup('nvim-mcp', { clear = true })

_G.NvimMcp = _G.NvimMcp or {}
local M = _G.NvimMcp
--- The broker's channel. Human events go straight to it while it is open.
M.chan = opts.chan
M.text_limit = opts.text_limit
--- The frames the broker last sent, bottom first, cached so a buffer read
--- again can be redrawn without a round trip. Note lines follow the anchors.
--- A broker that adopts a running nvim sends none and takes what is here.
M.frames = frames or M.frames or {}
--- Anchor extmark per note id, in the buffer holding the note. Created when
--- the buffer is first drawn and never cleared by a redraw, so it is the one
--- thing that keeps tracking the human's edits.
M.anchors = M.anchors or {}
--- Marks the broker has not received: only those made while its channel was
--- closed. They go out with the next sync.
M.pending = M.pending or {}
M.ns = vim.api.nvim_create_namespace('nvim-mcp-show')
M.anchor_ns = vim.api.nvim_create_namespace('nvim-mcp-anchor')
M.ask_ns = vim.api.nvim_create_namespace('nvim-mcp-ask')


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

-- A note must not read as code, and a foreground colour does not carry that:
-- a colourscheme's diagnostic blue against its comment green is a small
-- difference, and both sit on the same background as the code. Give the note
-- its own band instead, mixed from the diagnostic colour and the editor
-- background so it follows whatever colourscheme the human runs.
--
-- The derived groups are force-set so a colourscheme change re-colours them,
-- while the groups the extmarks name only link to them by default, which keeps
-- a human's own NvimMcpNote or NvimMcpShow across colourscheme changes.
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
      if not fallback or candidate.read > fallback.read then fallback = candidate end
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
  vim.api.nvim_set_hl(0, 'NvimMcpAskDefault', { link = 'DiagnosticWarn' })
  vim.api.nvim_set_hl(0, 'NvimMcpNote', { link = 'NvimMcpNoteDefault', default = true })
  vim.api.nvim_set_hl(0, 'NvimMcpShow', { link = 'NvimMcpShowDefault', default = true })
  vim.api.nvim_set_hl(0, 'NvimMcpAsk', { link = 'NvimMcpAskDefault', default = true })
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

local function fold(text, width, prefix)
  -- The prefix, the note's id, leads the first line; later lines hang under
  -- the text so the id stays the one thing in its column.
  local lines, line = {}, ''
  local indent = '  ' .. prefix
  for word in text:gmatch('%S+') do
    if line == '' then
      line = indent .. word
    elseif #line + 1 + #word <= width then
      line = line .. ' ' .. word
    else
      lines[#lines + 1] = line
      indent = string.rep(' ', 2 + #prefix)
      line = indent .. word
    end
  end
  if line ~= '' then lines[#lines + 1] = line end
  return lines
end


local function loaded_buffers()
  local out = {}
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) then out[#out + 1] = buf end
  end
  return out
end

--- Every live note with its frame, bottom frame first.
local function notes()
  local out = {}
  for _, frame in ipairs(M.frames) do
    for _, note in ipairs(frame.notes) do out[#out + 1] = { note = note, frame = frame } end
  end
  return out
end

--- Where a note's anchor is now, or nil if its buffer is not loaded.
local function anchored(note)
  local anchor = M.anchors[note.id]
  if not anchor or not vim.api.nvim_buf_is_loaded(anchor.buf) then return nil end
  local at = vim.api.nvim_buf_get_extmark_by_id(anchor.buf, M.anchor_ns, anchor.mark,
                                                { details = true })
  if not at[1] then return nil end
  local line = at[1] + 1
  if not note.end_line then return line, nil end
  return line, math.max(line, (at[3].end_row or at[1]) + 1)
end

--- Bring the cached lines up to date with the anchors, for one buffer or all.
local function refresh(buf)
  for _, entry in ipairs(notes()) do
    local note = entry.note
    local anchor = M.anchors[note.id]
    if anchor and (buf == nil or anchor.buf == buf) then
      local line, end_line = anchored(note)
      if line then note.line, note.end_line = line, end_line end
    end
  end
end

local function anchor(buf, note, first, final)
  local existing = M.anchors[note.id]
  if existing and existing.buf == buf
     and vim.api.nvim_buf_get_extmark_by_id(buf, M.anchor_ns, existing.mark, {})[1] then
    return
  end
  local extra = {}
  if final then extra.end_row, extra.end_col = final - 1, 0 end
  M.anchors[note.id] = {
    buf = buf, mark = vim.api.nvim_buf_set_extmark(buf, M.anchor_ns, first - 1, 0, extra),
  }
end

--- Draw the notes belonging to one buffer.
---
--- Called again whenever a buffer is read, because unloading a buffer drops
--- every extmark in it and the human closing a file must not lose the review.
--- Decorations are rebuilt from scratch; anchors are kept, and read first, so
--- a redraw lands where the human's edits have moved the note.
function M.render(buf)
  if not vim.api.nvim_buf_is_loaded(buf) then return end
  local name = vim.api.nvim_buf_get_name(buf)
  refresh(buf)
  vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local last = vim.api.nvim_buf_line_count(buf)
  -- All bands above one line go in a single extmark, bottom frame first, so
  -- an answer sits under the question it answers. nvim draws several marks
  -- at one position in an order it does not promise.
  local bands, rows = {}, {}
  for _, entry in ipairs(notes()) do
    local note = entry.note
    if note.file == name then
      local first = math.max(1, math.min(note.line or 1, last))
      local final = note.end_line and math.max(first, math.min(note.end_line, last)) or nil
      anchor(buf, note, first, final)
      if final then
        local end_row, end_col = final, 0
        if final >= last then
          end_row = last - 1
          end_col = #(vim.api.nvim_buf_get_lines(buf, last - 1, last, true)[1] or '')
        end
        vim.api.nvim_buf_set_extmark(buf, M.ns, first - 1, 0, {
          end_row = end_row, end_col = end_col, hl_group = 'NvimMcpShow', hl_eol = true,
        })
      end
      if note.text and note.text ~= '' then
        if not bands[first] then
          bands[first] = {}
          rows[#rows + 1] = first
        end
        for _, text in ipairs(fold(note.text, textwidth(buf), note.id .. '  ')) do
          -- An empty final chunk extends the highlight to the end of the screen
          -- line, so the note reads as a band rather than coloured text.
          table.insert(bands[first], { { text, 'NvimMcpNote' }, { '', 'NvimMcpNote' } })
        end
      end
    end
  end
  for _, row in ipairs(rows) do
    vim.api.nvim_buf_set_extmark(buf, M.ns, row - 1, 0, {
      virt_lines = bands[row], virt_lines_above = true,
    })
  end
end

function M.render_all()
  for _, buf in ipairs(loaded_buffers()) do M.render(buf) end
end

--- The current line of every note, by id.
function M.positions()
  refresh()
  local out = {}
  for _, entry in ipairs(notes()) do
    local note = entry.note
    out[#out + 1] = { id = note.id, line = note.line, end_line = note.end_line }
  end
  return out
end

--- The ids of every live note, bottom frame first.
function M.ids()
  local out = {}
  for _, entry in ipairs(notes()) do out[#out + 1] = entry.note.id end
  return out
end

--- Everything the broker's record is missing. Returned with every call and
--- handed over on exit.
function M.sync()
  local marks = M.pending
  M.pending = {}
  for _, buf in ipairs(loaded_buffers()) do
    vim.api.nvim_buf_clear_namespace(buf, M.ask_ns, 0, -1)
  end
  return { positions = M.positions(), marks = marks }
end

--- The topmost note under a line, so a question lands on the newest thread.
local function note_at(buf, line)
  local name = vim.api.nvim_buf_get_name(buf)
  local found = nil
  for _, entry in ipairs(notes()) do
    local note = entry.note
    if note.file == name and note.line <= line and line <= (note.end_line or note.line) then
      found = note.id
    end
  end
  return found
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

--- Draw the broker's frames. The top frame's files are opened and its
--- positions fill the quickfix list; lower frames render wherever their
--- files are already loaded.
function M.show(new_frames, show_opts)
  local previous = vim.api.nvim_get_current_tabpage()
  for _, buf in ipairs(loaded_buffers()) do
    vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
    vim.api.nvim_buf_clear_namespace(buf, M.anchor_ns, 0, -1)
  end
  M.anchors = {}
  M.frames = new_frames
  local top = new_frames[#new_frames]
  local items, opened, seen = {}, {}, {}
  for _, note in ipairs(top and top.notes or {}) do
    -- bufadd takes the name literally. :edit and nvim_cmd would expand
    -- backticks in it and run a shell.
    local buf = vim.fn.bufadd(note.file)
    if show_opts.open then
      vim.fn.bufload(buf)
      -- bufadd leaves a buffer unlisted, which hides it from :ls and from
      -- anything asking nvim what is open.
      vim.bo[buf].buflisted = true
      if not seen[note.file] then
        seen[note.file] = true
        opened[#opened + 1] = note.file
        if not displayed(buf) then
          if not scratch(vim.api.nvim_get_current_win()) then vim.cmd('tabnew') end
          vim.api.nvim_win_set_buf(0, buf)
        end
      end
    end
    if vim.api.nvim_buf_is_loaded(buf) then
      items[#items + 1] = {
        bufnr = buf,
        lnum = math.max(1, math.min(note.line or 1, vim.api.nvim_buf_line_count(buf))),
        col = 1,
        type = 'N',
        text = note.id .. '  ' .. (note.text or ''),
      }
    end
  end

  M.render_all()
  vim.fn.setqflist(items, 'r')
  vim.fn.setqflist({}, 'a', { title = top and top.title or '' })
  if show_opts.focus and #items > 0 then
    vim.cmd('cfirst')
  else
    vim.api.nvim_set_current_tabpage(previous)
  end
  return { opened = opened, uis = #vim.api.nvim_list_uis(), sync = M.sync() }
end

local function buffer_state(buf)
  return {
    file = vim.api.nvim_buf_get_name(buf),
    modified = vim.bo[buf].modified,
    -- Only a real file buffer may be read. A terminal buffer holds the human's
    -- shell scrollback, and the browsers keep their own listings in buffers
    -- whose names are not paths at all.
    readable = vim.bo[buf].buftype == '' and vim.api.nvim_buf_is_loaded(buf),
    visible = displayed(buf),
    lines = vim.api.nvim_buf_line_count(buf),
  }
end

function M.read(what, read_opts)
  local result = {}
  if what == 'cursor' then
    local buf = vim.api.nvim_get_current_buf()
    local at = vim.api.nvim_win_get_cursor(0)
    local state = buffer_state(buf)
    state.line, state.col = at[1], at[2] + 1
    local from, to = vim.fn.line("'<"), vim.fn.line("'>")
    if from > 0 and to > 0 then state.selection = { from, to } end
    result.cursor = state
  elseif what == 'range' then
    local buf = vim.fn.bufnr(read_opts.file)
    if buf ~= -1 and vim.api.nvim_buf_is_loaded(buf) then
      local state = buffer_state(buf)
      if state.readable then
        local last = vim.api.nvim_buf_line_count(buf)
        local first = math.max(1, math.min(read_opts.start_line or 1, last))
        local final = math.max(first, math.min(read_opts.end_line or last, last))
        state.open = true
        state.start_line, state.end_line = first, final
        state.text = table.concat(vim.api.nvim_buf_get_lines(buf, first - 1, final, false), '\n')
        result.range = state
      end
    end
  elseif what == 'tabs' then
    local buffers = {}
    for _, buf in ipairs(loaded_buffers()) do
      if vim.bo[buf].buflisted then buffers[#buffers + 1] = buffer_state(buf) end
    end
    result.buffers = buffers
  end
  result.sync = M.sync()
  return result
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
    M.render_all()
  end,
})
vim.api.nvim_create_autocmd('ColorScheme', { group = group, callback = styles })
vim.api.nvim_create_autocmd({ 'BufReadPost', 'BufWinEnter' }, {
  group = group,
  callback = function(ev) M.render(ev.buf) end,
})
-- The anchors go with the buffer. Take their last positions into the cache so
-- the notes come back on the right lines when it is read again.
vim.api.nvim_create_autocmd('BufUnload', {
  group = group,
  callback = function(ev) refresh(ev.buf) end,
})
-- Hand over what the broker has not seen before this nvim is gone. A request
-- rather than a notification, so exit waits for the broker to take it; if the
-- broker is away there is nobody to wait for.
vim.api.nvim_create_autocmd('VimLeavePre', {
  group = group,
  callback = function() pcall(vim.rpcrequest, M.chan, 'nvim-mcp', 'sync', M.sync()) end,
})

-- Answer the file-changed prompt ourselves. Left to nvim it blocks the RPC
-- behind a dialog when a UI is attached and 'autoread' is off.
vim.api.nvim_create_autocmd('FileChangedShell', {
  group = group,
  callback = function(ev)
    vim.v.fcs_choice = vim.bo[ev.buf].modified and '' or 'reload'
  end,
})

-- Frames are the broker's to change. The human's pop goes to it as a request
-- and comes back as a redraw; with the broker away there is nothing to change.
local function request_pop(letter)
  if not pcall(vim.rpcnotify, M.chan, 'nvim-mcp', 'pop', letter) then
    vim.notify('nvim-mcp: broker away, cannot pop')
  end
end
vim.api.nvim_create_user_command('AgentPop', function() request_pop(nil) end,
  { desc = 'Drop the top frame of agent notes' })
vim.api.nvim_create_user_command('AgentDrop', function(o) request_pop(o.args) end,
  { nargs = 1, desc = 'Drop the named frame of agent notes' })

-- The human's half of the conversation. `:Ask` hands a range to the agent.
vim.api.nvim_create_user_command('Ask', function(o)
  local buf = vim.api.nvim_get_current_buf()
  refresh(buf)
  -- The text as the human saw it. The file may change before the agent
  -- reads it, and an agent in a sandbox may not be able to read it at all.
  local text = table.concat(vim.api.nvim_buf_get_lines(buf, o.line1 - 1, o.line2, false), '\n')
  local truncated = #text > M.text_limit
  if truncated then text = text:sub(1, M.text_limit) end
  local mark = {
    file = vim.api.nvim_buf_get_name(buf),
    line1 = o.line1,
    line2 = o.line2,
    note = o.args,
    modified = vim.bo[buf].modified,
    note_id = note_at(buf, o.line1),
    text = text,
    truncated = truncated,
  }
  -- Straight to the broker while its channel is open, so the question is
  -- recorded before this nvim can be quit. Otherwise held for the next sync.
  local delivered = pcall(vim.rpcnotify, M.chan, 'nvim-mcp', 'ask', mark)
  if not delivered then M.pending[#M.pending + 1] = mark end
  vim.api.nvim_buf_set_extmark(buf, M.ask_ns, o.line1 - 1, 0, {
    end_row = o.line2 - 1, end_col = 0, sign_text = '?>', sign_hl_group = 'NvimMcpAsk',
    line_hl_group = 'NvimMcpAsk', strict = false,
  })
  vim.notify(delivered and 'nvim-mcp: question handed to the agent'
             or 'nvim-mcp: broker away, question kept for it')
end, { range = true, nargs = '*', desc = 'Hand the selected lines to the agent' })
