-- Copyright 2026 The nvim-mcp developers
-- SPDX-License-Identifier: MIT

-- The session's half inside nvim. Run once by the broker after connecting,
-- with the terminal background and the notes to draw as arguments. Client
-- values arrive as arguments to nvim_exec_lua calls, never inside the code.

local background, notes = ...
local group = vim.api.nvim_create_augroup('nvim-mcp', { clear = true })

_G.NvimMcp = _G.NvimMcp or {}
local M = _G.NvimMcp
M.notes = notes or {}
M.marks = M.marks or {}
M.ns = vim.api.nvim_create_namespace('nvim-mcp-show')
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

--- Draw the notes belonging to one buffer.
---
--- Called again whenever a buffer is read, because unloading a buffer drops
--- every extmark in it and the human closing a file must not lose the review.
function M.render(buf)
  if not vim.api.nvim_buf_is_loaded(buf) then return end
  local name = vim.api.nvim_buf_get_name(buf)
  vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local last = vim.api.nvim_buf_line_count(buf)
  for _, note in ipairs(M.notes) do
    if note.file == name then
      local first = math.max(1, math.min(note.line or 1, last))
      if note.end_line then
        local final = math.max(first, math.min(note.end_line, last))
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
        local lines = {}
        for _, text in ipairs(fold(note.text, textwidth(buf))) do
          -- An empty final chunk extends the highlight to the end of the screen
          -- line, so the note reads as a band rather than coloured text.
          lines[#lines + 1] = { { text, 'NvimMcpNote' }, { '', 'NvimMcpNote' } }
        end
        note.mark = vim.api.nvim_buf_set_extmark(buf, M.ns, first - 1, 0, {
          virt_lines = lines, virt_lines_above = true,
        })
      end
    end
  end
end

function M.render_all()
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do M.render(buf) end
end

--- Take the notes' current lines from their extmarks.
---
--- The human edits around a note, and the mark moves with the text while the
--- line number the agent sent does not.
function M.positions()
  for _, note in ipairs(M.notes) do
    local buf = vim.fn.bufnr(note.file)
    if note.mark and buf ~= -1 and vim.api.nvim_buf_is_loaded(buf) then
      local at = vim.api.nvim_buf_get_extmark_by_id(buf, M.ns, note.mark, {})
      if at[1] then
        local moved = at[1] + 1 - note.line
        note.line = at[1] + 1
        if note.end_line then note.end_line = note.end_line + moved end
      end
    end
  end
  return M.notes
end

function M.note_at(buf, line)
  local name = vim.api.nvim_buf_get_name(buf)
  for _, note in ipairs(M.notes) do
    if note.file == name and note.line <= line and line <= (note.end_line or note.line) then
      return note.id
    end
  end
  return nil
end

--- Hand the marks the human has made to the broker, and forget them here.
function M.drain()
  local taken = M.marks
  M.marks = {}
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(buf) then
      vim.api.nvim_buf_clear_namespace(buf, M.ask_ns, 0, -1)
    end
  end
  return taken
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

function M.show(locs, opts)
  local previous = vim.api.nvim_get_current_tabpage()
  local items, opened, seen, moved = {}, {}, {}, {}
  M.notes = {}
  for _, loc in ipairs(locs) do
    -- The path was resolved before this call. Resolve it again here, because
    -- the agent holds the root read-write and could have pointed a component
    -- somewhere else in between. This narrows that window; it does not close
    -- it, since nvim opens by name and not by descriptor.
    local real = vim.uv.fs_realpath(loc.file)
    if real == opts.root or (real and real:sub(1, #opts.root + 1) == opts.root .. '/') then
      -- bufadd takes the name literally. :edit and nvim_cmd would expand
      -- backticks in it and run a shell.
      local buf = vim.fn.bufadd(loc.file)
      vim.fn.bufload(buf)
      -- bufadd leaves a buffer unlisted, which hides it from :ls and from
      -- anything asking nvim what is open.
      vim.bo[buf].buflisted = true
      if not seen[loc.file] then
        seen[loc.file] = true
        opened[#opened + 1] = loc.file
        if not displayed(buf) then
          if not scratch(vim.api.nvim_get_current_win()) then vim.cmd('tabnew') end
          vim.api.nvim_win_set_buf(0, buf)
        end
      end
      M.notes[#M.notes + 1] = loc
      items[#items + 1] = {
        bufnr = buf,
        lnum = math.max(1, math.min(loc.line or 1, vim.api.nvim_buf_line_count(buf))),
        col = 1,
        type = 'N',
        text = loc.text or '',
      }
    else
      moved[#moved + 1] = loc.file
    end
  end

  M.render_all()
  vim.fn.setqflist(items, 'r')
  vim.fn.setqflist({}, 'a', { title = opts.title })
  if opts.focus then
    vim.cmd('cfirst')
  else
    vim.api.nvim_set_current_tabpage(previous)
  end
  return { opened = opened, moved = moved, uis = #vim.api.nvim_list_uis(),
           marks = M.drain(), notes = M.notes }
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

function M.read(what, opts)
  if what == 'marks' then
    return { marks = M.drain() }
  elseif what == 'cursor' then
    local buf = vim.api.nvim_get_current_buf()
    local at = vim.api.nvim_win_get_cursor(0)
    local state = buffer_state(buf)
    state.line, state.col = at[1], at[2] + 1
    local from, to = vim.fn.line("'<"), vim.fn.line("'>")
    if from > 0 and to > 0 then state.selection = { from, to } end
    return { cursor = state, marks = M.drain() }
  elseif what == 'range' then
    local buf = vim.fn.bufnr(opts.file)
    if buf == -1 or not vim.api.nvim_buf_is_loaded(buf) then
      return { open = false, marks = M.drain() }
    end
    local state = buffer_state(buf)
    if not state.readable then return { open = false, marks = M.drain() } end
    local last = vim.api.nvim_buf_line_count(buf)
    local first = math.max(1, math.min(opts.start_line or 1, last))
    local final = math.max(first, math.min(opts.end_line or last, last))
    state.open = true
    state.start_line, state.end_line = first, final
    state.text = table.concat(vim.api.nvim_buf_get_lines(buf, first - 1, final, false), '\n')
    return { range = state, marks = M.drain() }
  elseif what == 'tabs' then
    local buffers = {}
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
      if vim.bo[buf].buflisted and vim.api.nvim_buf_is_loaded(buf) then
        buffers[#buffers + 1] = buffer_state(buf)
      end
    end
    return { buffers = buffers, marks = M.drain() }
  end
  return { marks = M.drain() }
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
vim.api.nvim_create_autocmd({ 'BufUnload', 'BufWritePost' }, {
  group = group,
  callback = function() M.positions() end,
})

-- Answer the file-changed prompt ourselves. Left to nvim it blocks the RPC
-- behind a dialog when a UI is attached and 'autoread' is off.
vim.api.nvim_create_autocmd('FileChangedShell', {
  group = group,
  callback = function(ev)
    vim.v.fcs_choice = vim.bo[ev.buf].modified and '' or 'reload'
  end,
})

-- The human's half of the conversation. `:Ask` queues a range for the agent,
-- which collects it on its next call. Queuing locally rather than notifying the
-- agent's channel keeps a question through a broker restart, and keeps this
-- working in any client, not only ones that can be woken.
vim.api.nvim_create_user_command('Ask', function(o)
  local buf = vim.api.nvim_get_current_buf()
  local mark = {
    file = vim.api.nvim_buf_get_name(buf),
    line1 = o.line1,
    line2 = o.line2,
    note = o.args,
    modified = vim.bo[buf].modified,
    note_id = M.note_at(buf, o.line1),
  }
  -- Send the text only when the file on disk no longer has it. The agent can
  -- read an unmodified file itself.
  if mark.modified then
    mark.text = table.concat(vim.api.nvim_buf_get_lines(buf, o.line1 - 1, o.line2, false), '\n')
  end
  M.marks[#M.marks + 1] = mark
  vim.api.nvim_buf_set_extmark(buf, M.ask_ns, o.line1 - 1, 0, {
    end_row = o.line2 - 1, end_col = 0, sign_text = '?>', sign_hl_group = 'NvimMcpAsk',
    line_hl_group = 'NvimMcpAsk', strict = false,
  })
  vim.notify(('nvim-mcp: %d question(s) waiting for the agent'):format(#M.marks))
end, { range = true, nargs = '*', desc = 'Hand the selected lines to the agent' })
