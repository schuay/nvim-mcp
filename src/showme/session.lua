-- Copyright 2026 The showme developers
-- SPDX-License-Identifier: MIT

-- Render broker-owned session state inside nvim.
--
-- Report anchor positions and human questions to the broker. Pass client values
-- as nvim_exec_lua arguments so they never become executable Lua.

local opts, frames = ...
local background = opts.background
local group = vim.api.nvim_create_augroup('showme', { clear = true })

_G.ShowMe = _G.ShowMe or {}
local M = _G.ShowMe
--- Broker channel used to deliver human events immediately.
M.chan = opts.chan
M.text_limit = opts.text_limit
--- Cached broker frames, bottom first. Keep these across adoption so buffers
--- can redraw without a round trip.
M.frames = frames or M.frames or {}
--- Persistent anchor extmark per note ID, used to follow human edits.
M.anchors = M.anchors or {}
--- Marks created while the broker channel was unavailable, sent on next sync.
M.pending = M.pending or {}
--- Sign extmark per question. Keep it until the broker reports acknowledgement.
M.asks = M.asks or {}
M.ask_seq = M.ask_seq or 0
M.ns = vim.api.nvim_create_namespace('showme-show')
M.anchor_ns = vim.api.nvim_create_namespace('showme-anchor')
M.ask_ns = vim.api.nvim_create_namespace('showme-ask')


local function options()
  -- Headless nvim needs the supplied background to choose the correct palette.
  if background ~= nil and vim.o.background ~= background then
    vim.o.background = background
  end
  -- Notify an attaching UI to use truecolor before it detects server settings.
  vim.o.termguicolors = true
  -- Make quickfix navigation switch tabs instead of replacing a window's buffer.
  vim.o.switchbuf = 'usetab,newtab'
  -- Avoid swapfile conflicts with the human's other nvim instance.
  vim.o.swapfile = false
  -- Agent-selected files must not run modelines or project-local host code.
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

-- Give notes a distinct background band derived from the active colourscheme.
-- A foreground-only change can be indistinguishable from code highlighting.
--
-- Replace derived groups after colourscheme changes. Link public groups only by
-- default so human overrides survive.
local function styles()
  local info = vim.api.nvim_get_hl(0, { name = 'DiagnosticInfo', link = false })
  local normal = vim.api.nvim_get_hl(0, { name = 'Normal', link = false })
  local note = { italic = true }
  if info.fg and normal.bg and normal.fg then
    -- Choose the band farthest from the editor background while retaining
    -- readable text contrast.
    --
    -- Cap the threshold at 90% of the colourscheme's body-text contrast because
    -- some schemes already render body text near 4.5:1.
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
    -- Always use the most readable candidate when none meets the threshold.
    best = best or fallback
    note.bg, note.fg = best.bg, best.fg
  else
    note.link = 'DiagnosticInfo'
  end
  vim.api.nvim_set_hl(0, 'ShowMeNoteDefault', note)
  vim.api.nvim_set_hl(0, 'ShowMeShowDefault', { link = 'Visual' })
  vim.api.nvim_set_hl(0, 'ShowMeAskDefault', { link = 'DiagnosticWarn' })
  vim.api.nvim_set_hl(0, 'ShowMeNote', { link = 'ShowMeNoteDefault', default = true })
  vim.api.nvim_set_hl(0, 'ShowMeShow', { link = 'ShowMeShowDefault', default = true })
  vim.api.nvim_set_hl(0, 'ShowMeAsk', { link = 'ShowMeAskDefault', default = true })
end

-- Virtual lines do not wrap or mark overflow. Wrap to the displaying window's
-- width and redraw after resize.
local function textwidth(buf)
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == buf then
      local info = vim.fn.getwininfo(win)[1]
      return math.max(40, info.width - info.textoff - 4)
    end
  end
  return math.max(40, vim.o.columns - 4)
end

-- Cut to a byte length without creating invalid UTF-8 for msgpack.
local function cut(text, bytes)
  if bytes < 1 then return '' end
  return text:sub(1, bytes + vim.str_utf_start(text, bytes + 1))
end


-- Preserve explicit line breaks and wrap only lines wider than the window.
local function wrap(text, width, prefix)
  -- Hang continuations under the text and preserve indentation on wrapped lines.
  local lines = {}
  local first, hang = '  ' .. prefix, string.rep(' ', 2 + #prefix)
  for line in vim.gsplit(text, '\n', { plain = true }) do
    local indent = #lines == 0 and first or hang
    local broken = hang .. line:match('^%s*') .. '  '
    local body = line
    repeat
      local room = math.max(8, width - #indent)
      if #body <= room then
        lines[#lines + 1] = indent .. body
        body = ''
      else
        -- Break late whitespace when available; never reflow snippet words.
        local head = cut(body, room)
        local at = head:match('^.*()%s')
        if not at or at < room / 2 then at = #head + 1 end
        lines[#lines + 1] = indent .. (head:sub(1, at - 1):gsub('%s+$', ''))
        body = (body:sub(at):gsub('^%s+', ''))
        indent = broken
      end
    until body == ''
  end
  return lines
end


-- Quickfix entries neither wrap nor mark overflow. Clip to the space after
-- nvim's prefix; the virtual-line band retains the full note.
local function clip(text, room)
  if #text <= room then return text end
  return (cut(text, math.max(1, room - 3)):gsub('%s+$', '')) .. '...'
end


local function loaded_buffers()
  local out = {}
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) then out[#out + 1] = buf end
  end
  return out
end

local function notes()
  local out = {}
  for _, frame in ipairs(M.frames) do
    for _, note in ipairs(frame.notes) do out[#out + 1] = { note = note, frame = frame } end
  end
  return out
end

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

--- Draw notes for one buffer. Buffer unload drops extmarks, so redraw after each
--- load. Refresh and retain anchors before rebuilding decorations to preserve
--- positions moved by human edits.
function M.render(buf)
  if not vim.api.nvim_buf_is_loaded(buf) then return end
  local name = vim.api.nvim_buf_get_name(buf)
  refresh(buf)
  vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local last = vim.api.nvim_buf_line_count(buf)
  -- Combine bands at one line because nvim does not define extmark draw order.
  -- Keep bottom frames first so answers appear below their questions.
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
          end_row = end_row, end_col = end_col, hl_group = 'ShowMeShow', hl_eol = true,
        })
      end
      if note.text and note.text ~= '' then
        if not bands[first] then
          bands[first] = {}
          rows[#rows + 1] = first
        end
        for _, text in ipairs(wrap(note.text, textwidth(buf), note.id .. '  ')) do
          -- An empty final chunk extends the background to the screen edge.
          table.insert(bands[first], { { text, 'ShowMeNote' }, { '', 'ShowMeNote' } })
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

function M.positions()
  refresh()
  local out = {}
  for _, entry in ipairs(notes()) do
    local note = entry.note
    out[#out + 1] = { id = note.id, line = note.line, end_line = note.end_line }
  end
  return out
end

function M.ids()
  local out = {}
  for _, entry in ipairs(notes()) do out[#out + 1] = entry.note.id end
  return out
end

--- Return nvim-owned state and clear marks queued for the broker.
function M.sync()
  local marks = M.pending
  M.pending = {}
  return { positions = M.positions(), marks = marks }
end

--- Select the newest note covering a line for question threading.
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

local function open_buffer(file, buf, seen, opened, focus)
  vim.fn.bufload(buf)
  -- `bufadd` creates an unlisted buffer, so list it for tab queries and `:ls`.
  vim.bo[buf].buflisted = true
  if seen[file] then return end
  seen[file] = true
  opened[#opened + 1] = file
  if displayed(buf) then return end
  -- Reuse an empty window only for a focused show. Background redraws must not
  -- replace the buffer in front of the human.
  if not (focus and scratch(vim.api.nvim_get_current_win())) then vim.cmd('tabnew') end
  vim.api.nvim_win_set_buf(0, buf)
end

--- Remove signs for acknowledged questions so pending replies remain visible.
local function settle_asks(pending)
  if pending == nil then return end
  local waiting = {}
  for _, id in ipairs(pending) do waiting[id] = true end
  for id, ask in pairs(M.asks) do
    if not waiting[id] then
      pcall(vim.api.nvim_buf_del_extmark, ask.buf, M.ask_ns, ask.mark)
      M.asks[id] = nil
    end
  end
end

function M.show(new_frames, show_opts)
  local previous = vim.api.nvim_get_current_tabpage()
  -- Read anchors before replacing frames so retained notes keep positions moved
  -- by human edits.
  local live = {}
  for _, at in ipairs(M.positions()) do live[at.id] = at end
  for _, buf in ipairs(loaded_buffers()) do
    vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  end
  M.frames = new_frames
  local kept = {}
  for _, entry in ipairs(notes()) do
    local note = entry.note
    kept[note.id] = true
    local at = live[note.id]
    if at then note.line, note.end_line = at.line, at.end_line end
  end
  for id, existing in pairs(M.anchors) do
    if not kept[id] then
      pcall(vim.api.nvim_buf_del_extmark, existing.buf, M.anchor_ns, existing.mark)
      M.anchors[id] = nil
    end
  end
  settle_asks(show_opts.pending)
  local top = new_frames[#new_frames]
  local items, opened, seen = {}, {}, {}
  for _, note in ipairs(top and top.notes or {}) do
    -- `bufadd` treats names literally; Ex commands expand backticks as shell code.
    local buf = vim.fn.bufadd(note.file)
    if show_opts.open then
      open_buffer(note.file, buf, seen, opened, show_opts.focus)
    end
    if vim.api.nvim_buf_is_loaded(buf) then
      local lnum = math.max(1, math.min(note.line or 1, vim.api.nvim_buf_line_count(buf)))
      local label = note.id .. '  '
      -- Reserve space for nvim's `name|lnum col 1 note| ` prefix.
      local name = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(buf), ':.')
      local room = vim.o.columns - #name - #tostring(lnum) - 14 - #label
      items[#items + 1] = {
        bufnr = buf,
        lnum = lnum,
        col = 1,
        type = 'N',
        text = label .. clip(note.text or '', math.max(20, room)),
      }
    end
  end

  -- A fresh nvim must load lower-frame files or their recorded notes stay hidden.
  if show_opts.open_all then
    for _, entry in ipairs(notes()) do
      local file = entry.note.file
      if not seen[file] then
        open_buffer(file, vim.fn.bufadd(file), seen, opened, false)
      end
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
    -- Exclude terminals and browser buffers whose contents are not file text.
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

-- The socket accepts calls before startup and user configuration finish. Apply
-- options and styles now, then reapply them at VimEnter.
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
-- Rewrap virtual lines after their window width changes.
vim.api.nvim_create_autocmd('VimResized', { group = group, callback = M.render_all })
vim.api.nvim_create_autocmd({ 'BufReadPost', 'BufWinEnter' }, {
  group = group,
  callback = function(ev) M.render(ev.buf) end,
})
-- Cache anchor positions before buffer unload discards the extmarks.
vim.api.nvim_create_autocmd('BufUnload', {
  group = group,
  callback = function(ev) refresh(ev.buf) end,
})
-- Use a request for the final sync so exit waits for the broker to receive it.
vim.api.nvim_create_autocmd('VimLeavePre', {
  group = group,
  callback = function() pcall(vim.rpcrequest, M.chan, 'showme', 'sync', M.sync()) end,
})

-- Resolve file-change prompts that would otherwise block RPC behind a UI dialog.
vim.api.nvim_create_autocmd('FileChangedShell', {
  group = group,
  callback = function(ev)
    vim.v.fcs_choice = vim.bo[ev.buf].modified and '' or 'reload'
  end,
})

-- Ask the broker to mutate its frame record, then let its redraw update nvim.
local function request_pop(letter)
  if not pcall(vim.rpcnotify, M.chan, 'showme', 'pop', letter) then
    vim.notify('showme: broker away, cannot pop')
  end
end
vim.api.nvim_create_user_command('AgentPop', function() request_pop(nil) end,
  { desc = 'Drop the top frame of agent notes' })
vim.api.nvim_create_user_command('AgentDrop', function(o) request_pop(o.args) end,
  { nargs = 1, desc = 'Drop the named frame of agent notes' })

-- Wrap quickfix navigation at frame boundaries. Use Ctrl-Alt because terminals
-- commonly reserve Ctrl-Shift-PageUp for scrollback.
local function walk(step, wrap)
  return function()
    if vim.fn.getqflist({ size = 0 }).size == 0 then
      vim.notify('showme: no notes')
    elseif not pcall(vim.cmd, step) then
      vim.cmd(wrap)
    end
  end
end
vim.keymap.set('n', '<C-M-PageDown>', walk('cnext', 'cfirst'),
  { desc = 'Jump to the next agent note' })
vim.keymap.set('n', '<C-M-PageUp>', walk('cprevious', 'clast'),
  { desc = 'Jump to the previous agent note' })

-- `:Ask` sends the selected range and question to the agent.
vim.api.nvim_create_user_command('Ask', function(o)
  local buf = vim.api.nvim_get_current_buf()
  refresh(buf)
  -- Capture displayed text because disk may differ or be outside sandbox access.
  local text = table.concat(vim.api.nvim_buf_get_lines(buf, o.line1 - 1, o.line2, false), '\n')
  local truncated = #text > M.text_limit
  if truncated then text = cut(text, M.text_limit) end
  M.ask_seq = M.ask_seq + 1
  local mark = {
    ask = M.ask_seq,
    file = vim.api.nvim_buf_get_name(buf),
    line1 = o.line1,
    line2 = o.line2,
    note = o.args,
    modified = vim.bo[buf].modified,
    note_id = note_at(buf, o.line1),
    text = text,
    truncated = truncated,
  }
  -- Deliver immediately when possible; otherwise retain the mark for next sync.
  local delivered = pcall(vim.rpcnotify, M.chan, 'showme', 'ask', mark)
  if not delivered then M.pending[#M.pending + 1] = mark end
  M.asks[M.ask_seq] = {
    buf = buf,
    mark = vim.api.nvim_buf_set_extmark(buf, M.ask_ns, o.line1 - 1, 0, {
      end_row = o.line2 - 1, end_col = 0, sign_text = '?>', sign_hl_group = 'ShowMeAsk',
      line_hl_group = 'ShowMeAsk', strict = false,
    }),
  }
  vim.notify(delivered and 'showme: question handed to the agent'
             or 'showme: broker away, question kept for it')
end, { range = true, nargs = '*', desc = 'Hand the selected lines to the agent' })

-- `:Ref` copies a root-relative, 1-based, inclusive reference for chat.
vim.api.nvim_create_user_command('Ref', function(o)
  local name = vim.api.nvim_buf_get_name(0)
  if name == '' then
    vim.notify('showme: buffer has no file')
    return
  end
  local path = vim.fn.fnamemodify(name, ':.')
  local ref = o.line1 == o.line2 and string.format('%s:%d', path, o.line1)
    or string.format('%s:%d-%d', path, o.line1, o.line2)
  -- Let nvim choose the clipboard provider for the saved human environment.
  if vim.fn.has('clipboard') == 0 then
    vim.notify('showme: no clipboard provider; ' .. ref)
    return
  end
  vim.fn.setreg('+', ref)
  vim.notify('showme: copied ' .. ref)
end, { range = true, desc = 'Copy a reference to the current line or range' })
