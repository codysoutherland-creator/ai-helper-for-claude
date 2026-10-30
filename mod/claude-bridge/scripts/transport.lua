-- UDP API server + inbox for Claude-bound messages + reply history.
-- Datagrams (text, <= 4000 bytes): request part "Q|<id>|<seq>|<total>|<chunk>", reply part "A|<id>|<seq>|<total>|<chunk>".
-- All parts of a request -> JSON {"op","args"} -> api.call -> JSON {ok, result|error} -> reply parts to source_port.
-- Needs launch flag --enable-lua-udp <port>; without it everything here is a silent no-op.

local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")
local access = require("scripts.access")

local transport = {}

local CHUNK = 3900        -- payload bytes per datagram (header fits in the rest of 4000)
local PART_TTL = 600      -- ticks before an incomplete request is dropped
-- multi-part requests are buffered in storage (so they end up in the save): bound what a local process can pile up
local MAX_PARTS = 256             -- per request (~1 MB)
local MAX_OPEN = 16               -- incomplete requests at once (oldest dropped)
local MAX_BUFFERED = 1024 * 1024  -- bytes across all incomplete requests (oldest dropped)
local ONLINE_TTL = 600    -- companion counts as online if it polled within this many ticks
local OFFLINE_MSG = "Claude companion isn't running - launch via the Factorio (Claude) shortcut. " ..
  "Free commands still work: @claude help"

-- set by control.lua (avoids a require cycle if local_commands needs transport)
transport.local_commands = nil
-- GUI refresh hook (gui.lua overrides)
transport.on_change = function() end

local function init()
  storage.inbox = storage.inbox or {}
  storage.chat_seq = storage.chat_seq or 0
  storage.udp_parts = storage.udp_parts or {}
  storage.claude_history = storage.claude_history or {}
  storage.claude_status = storage.claude_status or "idle"
  storage.claude_warned = storage.claude_warned or {}
  storage.reply_rids = storage.reply_rids or {}
  storage.claude_confirms = storage.claude_confirms or {}  -- request_id -> {text, to?, tick}
  -- identifies this save's id sequence, so a companion's ack from another save/map is ignored
  if not storage.inbox_epoch then
    local s1 = game.get_surface(1)
    storage.inbox_epoch = (s1 and tostring(s1.map_gen_settings.seed) or "0") .. "-" .. game.tick
  end
end
events.on_init(init)
events.on_configuration_changed(init)

local function changed() transport.on_change() end

local function claude_print(target, text)
  local surface = target and target.valid and target.surface and target.surface.name
  local msg = {"", "[color=0.85,0.47,0.34][Claude][/color] ", util.linkify(util.strip_markdown(text), surface)}
  if target then target.print(msg) else game.print(msg, {skip = defines.print_skip.never}) end
end

---------------------------------------------------------------------------
-- UDP
---------------------------------------------------------------------------

-- split into <= CHUNK-byte pieces without cutting a UTF-8 sequence
local function split(s)
  local parts, i, n = {}, 1, #s
  while i <= n do
    local j = math.min(i + CHUNK - 1, n)
    while j < n and j > i do
      local b = s:byte(j + 1)
      if b < 0x80 or b >= 0xC0 then break end
      j = j - 1
    end
    parts[#parts + 1] = s:sub(i, j)
    i = j + 1
  end
  if #parts == 0 then parts[1] = "" end
  return parts
end

local function send(port, player_index, id, data)
  local parts = split(data)
  for i, part in ipairs(parts) do
    local pkt = "A|" .. id .. "|" .. i .. "|" .. #parts .. "|" .. part
    if player_index and player_index > 0 then
      helpers.send_udp(port, pkt, player_index)
    else
      helpers.send_udp(port, pkt)
    end
  end
end

local function handle(data)
  local ok, req = pcall(helpers.json_to_table, data)
  local resp
  if not ok or type(req) ~= "table" then
    resp = {ok = false, error = "bad json"}
  else
    resp = api.call(req.op, req.args)
  end
  local ok2, out = pcall(helpers.table_to_json, resp)
  if not ok2 then out = helpers.table_to_json{ok = false, error = "result not serializable: " .. tostring(out)} end
  return out
end

-- drop the oldest incomplete requests until there is room for `extra` more bytes in a new/open one
local function trim_parts(parts, keep_key, extra)
  while true do
    local open, bytes, oldest, oldest_key = 0, 0, nil, nil
    for key, b in pairs(parts) do
      open = open + 1
      bytes = bytes + (b.bytes or 0)
      if key ~= keep_key and (not oldest or b.tick < oldest.tick) then oldest, oldest_key = b, key end
    end
    if (open <= MAX_OPEN and bytes + extra <= MAX_BUFFERED) or not oldest_key then return end
    parts[oldest_key] = nil
  end
end

-- Only the server / single-player instance polls UDP (recv_udp without a player index). Should a packet ever carry a
-- player index, accept it only from the host or an admin.
local function udp_sender_ok(e)
  if e.player_index == nil or e.player_index == 0 then return true end
  local p = game.get_player(e.player_index)
  return p ~= nil and access.is_trusted(p)
end

events.on(defines.events.on_udp_packet_received, function(e)
  if not udp_sender_ok(e) then return end
  local kind, id, seq, total, chunk = e.payload:match("^(%a)|([^|]*)|(%d+)|(%d+)|(.*)$")
  if kind ~= "Q" then return end
  seq, total = tonumber(seq), tonumber(total)
  if seq < 1 or seq > total or total > MAX_PARTS or #id > 64 then return end
  local data = chunk
  if total > 1 then
    init()
    local key = e.source_port .. "|" .. id
    local b = storage.udp_parts[key]
    if not b then
      trim_parts(storage.udp_parts, nil, #chunk)
      b = {tick = game.tick, total = total, n = 0, parts = {}, bytes = 0}
      storage.udp_parts[key] = b
    end
    if total ~= b.total or b.parts[seq] then return end  -- inconsistent or duplicate part
    trim_parts(storage.udp_parts, key, #chunk)
    if (b.bytes or 0) + #chunk > MAX_BUFFERED then storage.udp_parts[key] = nil; return end
    b.parts[seq] = chunk
    b.n = b.n + 1
    b.bytes = (b.bytes or 0) + #chunk
    if b.n < b.total then return end
    storage.udp_parts[key] = nil
    for i = 1, b.total do if not b.parts[i] then return end end
    data = table.concat(b.parts, "", 1, b.total)
  end
  send(e.source_port, e.player_index, id, handle(data))
end)

events.on(defines.events.on_tick, function()
  pcall(helpers.recv_udp)
end)

events.on_nth_tick(60, function()
  local parts = storage.udp_parts
  if not (parts and next(parts)) then return end
  for key, b in pairs(parts) do
    if game.tick - b.tick > PART_TTL then parts[key] = nil end
  end
end)

---------------------------------------------------------------------------
-- Claude inbox + history
---------------------------------------------------------------------------

local function push_history(who, text, cost)
  local h = storage.claude_history
  h[#h + 1] = {who = who, text = text, tick = game.tick, cost = cost}
  while #h > 50 do table.remove(h, 1) end
end

function transport.companion_online()
  local t = storage.companion_seen_tick
  return t ~= nil and game.tick - t <= ONLINE_TTL
end

-- "thinking" / "idle" / custom text set by the companion
function transport.status()
  return storage.claude_status or "idle"
end

-- last n {who, text, tick, cost?}, oldest first
function transport.history(n)
  init()
  local h, out = storage.claude_history, {}
  for i = math.max(1, #h - (n or 8) + 1), #h do out[#out + 1] = h[i] end
  return out
end

local MAX_TEXT = 1000  -- characters of one question kept for Claude (the GUI box allows much more)

-- Queue a message for Claude (costs tokens once the companion picks it up). source = "gui"|"chat"|"plan".
-- Returns the inbox id, or nil when this player may not use Claude here (they get a free one-line explanation).
function transport.ask(player, text, source)
  init()
  if not access.can_ask(player) then
    access.deny(player)
    return nil
  end
  text = tostring(text or "")
  if #text > MAX_TEXT then
    local j = MAX_TEXT
    while j > 1 do  -- never cut inside a UTF-8 sequence
      local b = text:byte(j + 1)
      if b < 0x80 or b >= 0xC0 then break end
      j = j - 1
    end
    text = text:sub(1, j)
  end
  storage.chat_seq = storage.chat_seq + 1
  table.insert(storage.inbox, {id = storage.chat_seq, tick = game.tick, player = player and player.name or "server",
    player_index = player and player.index or 0, role = access.role(player),
    pos = player and util.pos_out(player.position) or {x = 0, y = 0},
    surface = player and player.surface.name or "nauvis", text = text, source = source or "chat"})
  while #storage.inbox > 50 do table.remove(storage.inbox, 1) end
  push_history(player and player.name or "server", text)
  if not transport.companion_online() then
    local key = player and player.index or 0
    if not storage.claude_warned[key] then
      storage.claude_warned[key] = true
      claude_print(player, OFFLINE_MSG)
    end
  end
  changed()
  return storage.chat_seq
end

-- A free event for the companion (source "plan": it never asks Claude about it), e.g. "stage_ready <plan id>".
function transport.post_event(text)
  init()
  storage.chat_seq = storage.chat_seq + 1
  table.insert(storage.inbox, {id = storage.chat_seq, tick = game.tick, player = "server", player_index = 0,
    role = "server", pos = {x = 0, y = 0}, surface = "nauvis", text = tostring(text), source = "plan"})
  while #storage.inbox > 50 do table.remove(storage.inbox, 1) end
  return storage.chat_seq
end

local function seen()
  local was = transport.companion_online()
  storage.companion_seen_tick = game.tick
  storage.claude_warned = {}
  return not was
end

-- Pending Claude-bound messages (the companion polls this).
-- Acknowledged form {ack = highest id the caller already received, epoch?} -> {messages, epoch}: drops only ids <= ack
-- and returns the rest, so a reply lost to a UDP timeout (pause/autosave) is simply delivered again on the next poll.
-- An ack from another save (epoch mismatch or ack beyond our sequence) drops nothing.
-- Without ack (v0.1 callers / MCP chat_pop) it pops everything and returns the bare list.
local function inbox_pop(args)
  init()
  local ack = args and tonumber(args.ack)
  if seen() then changed() end
  if not ack then
    local msgs = storage.inbox
    storage.inbox = {}
    return msgs
  end
  if (args.epoch and args.epoch ~= storage.inbox_epoch) or ack > storage.chat_seq then ack = 0 end
  local msgs = {}
  for _, m in ipairs(storage.inbox) do
    if m.id > ack then msgs[#msgs + 1] = m end
  end
  storage.inbox = msgs
  return {messages = msgs, epoch = storage.inbox_epoch}
end
api.register("inbox_pop", inbox_pop)
api.register("chat_pop", inbox_pop)  -- v0.1 name

-- {text, to?, rid?, cost?}: show Claude's answer in chat (to = player name/index for a private reply).
-- rid makes a retried delivery idempotent (a timed-out datagram may still arrive late).
-- cost (e.g. "cost ≈ $0.02 · 4 s") is kept in the GUI history only, never printed to chat. Markdown is stripped.
api.register("claude_reply", function(args)
  init()
  if type(args.text) ~= "string" or args.text == "" then error("text required") end
  local text = util.strip_markdown(args.text)
  if text == "" then text = "(no answer)" end
  if args.rid then
    local rids = storage.reply_rids
    for _, r in ipairs(rids) do if r == args.rid then return true end end
    rids[#rids + 1] = args.rid
    while #rids > 20 do table.remove(rids, 1) end
  end
  push_history("Claude", text, type(args.cost) == "string" and args.cost or nil)
  local p = args.to and game.get_player(args.to)
  claude_print(p, text)
  seen()
  changed()
  return true
end)

-- {x, y, text?, seconds?=60, surface?}: point at a spot - ring + label in the world and on the map,
-- plus a clickable map pin in chat. Free (no tokens); Claude uses it to show the player what it means.
api.register("mark", function(args)
  local surface = util.get_surface(args)
  local x, y = tonumber(args.x), tonumber(args.y)
  if not (x and y) then error("x and y required") end
  local pos = {x = x, y = y}
  util.mark(surface, pos, args.text, tonumber(args.seconds) or 60)
  local gps = "[gps=" .. util.round(x, 1) .. "," .. util.round(y, 1) .. "," .. surface.name .. "]"
  game.print({"", "[color=0.85,0.47,0.34][Claude][/color] ", (args.text and args.text ~= "" and (args.text .. " ") or ""), gps},
    {skip = defines.print_skip.never})
  return {marked = pos, gps = gps}
end)

-- {status}: "thinking" / "idle" / custom
api.register("claude_status", function(args)
  init()
  storage.claude_status = args.status or "idle"
  seen()
  changed()
  return true
end)

---------------------------------------------------------------------------
-- cost gate: the companion asks before running an expensive (planning) request
---------------------------------------------------------------------------

local CONFIRM_TTL = 60 * 60 * 15  -- unanswered questions disappear after 15 min (the companion forgets them too)

-- pending confirmations visible to this player (to = nil -> everyone; the host/admins see all; all = every one),
-- oldest first: {{id, text, to, tick}}
function transport.confirms(player, all)
  init()
  local out = {}
  local sees_all = all or (player ~= nil and access.can_confirm(player))
  for id, c in pairs(storage.claude_confirms) do
    if game.tick - c.tick > CONFIRM_TTL then
      storage.claude_confirms[id] = nil
    elseif sees_all or not c.to or (player and c.to == player.name) then
      out[#out + 1] = {id = id, text = c.text, to = c.to, tick = c.tick}
    end
  end
  table.sort(out, function(a, b) return a.tick < b.tick end)
  return out
end

-- Answer a confirmation: run -> inbox "run <id>", else "cancel <id>" (source = "confirm"; free, no tokens).
-- Only the host (or an admin, when allowed to use Claude) may answer: a guest can't approve their own expensive job.
function transport.confirm(player, id, run)
  init()
  id = tostring(id)
  local c = storage.claude_confirms[id]
  if not c then return false, "That request is no longer waiting." end
  if not access.can_confirm(player) then
    return false, "Only the host" .. (access.policy() == "host-only" and "" or " or an admin") ..
      " can run or cancel Claude jobs here."
  end
  storage.claude_confirms[id] = nil
  storage.chat_seq = storage.chat_seq + 1
  table.insert(storage.inbox, {id = storage.chat_seq, tick = game.tick, player = player and player.name or "server",
    player_index = player and player.index or 0, role = access.role(player),
    pos = player and util.pos_out(player.position) or {x = 0, y = 0},
    surface = player and player.surface.name or "nauvis", text = (run and "run " or "cancel ") .. id, source = "confirm"})
  push_history(player and player.name or "server", run and "Run it." or "Cancel.")
  if not run then push_history("Claude", "Cancelled - nothing was sent to Claude.") end
  changed()
  return true, run and "Running it." or "Cancelled - nothing was sent to Claude."
end

-- {request_id, estimate_text, to?}: ask the player to confirm an expensive request. Shown in the Ask Claude tab with
-- Run / Cancel, and printed once to the asking player (chat users answer with "@claude run" / "@claude cancel").
api.register("claude_confirm", function(args)
  init()
  local id = args.request_id and tostring(args.request_id)
  if not id or id == "" then error("request_id required") end
  local text = util.strip_markdown(tostring(args.estimate_text or "This looks like a bigger job. Run it?"))
  local p = args.to and game.get_player(args.to)
  if p and not access.can_confirm(p) then p = nil end  -- a guest's job: shown to everyone, only the host can run it
  storage.claude_confirms[id] = {text = text, to = p and p.name or nil, tick = game.tick}
  claude_print(p, text .. " Click Run in the Claude panel (Ask Claude tab), or type @claude run / @claude cancel.")
  seen()
  changed()
  return {pending = id}
end)

-- {request_id?}: drop a pending confirmation (all without id), e.g. when the companion forgot it
api.register("claude_confirm_drop", function(args)
  init()
  if args.request_id then storage.claude_confirms[tostring(args.request_id)] = nil else storage.claude_confirms = {} end
  changed()
  return true
end)

-- test op: {player?, request_id, run} - same path as the Run / Cancel buttons
api.register_debug("confirm_simulate", function(args)
  local p = args.player and util.get_player(args) or nil
  local ok, msg = transport.confirm(p, args.request_id, args.run and true or false)
  return {ok = ok, msg = msg}
end)

local CONFIRM_WORDS = {run = true, yes = true, go = true, confirm = true}
local CANCEL_WORDS = {cancel = true, no = true, nevermind = true}

-- "@claude run" / "@claude cancel" answers this player's oldest pending confirmation (free); with none pending,
-- run/cancel/confirm are answered here too, so they never become a paid Claude message
local function chat_confirm(p, text)
  local w = text:lower():gsub("[%p%s]+$", ""):gsub("^%s+", "")
  if not (CONFIRM_WORDS[w] or CANCEL_WORDS[w]) then return nil end
  local list = transport.confirms(p)
  if #list == 0 then
    -- "yes"/"no" may answer a question Claude asked; only the gate's own words are caught here
    if w == "run" or w == "cancel" or w == "confirm" then return "Nothing is waiting for confirmation - ask again." end
    return nil
  end
  local _, msg = transport.confirm(p, list[1].id, CONFIRM_WORDS[w] ~= nil)
  return msg
end

---------------------------------------------------------------------------
-- chat routing: free keyword commands first, otherwise Claude
---------------------------------------------------------------------------

-- Route one chat line. Returns "ignored" | "local" | "claude" (and the local reply, if any).
function transport.route_chat(player_index, msg)
  if not msg then return "ignored" end
  local lower = msg:lower()
  -- "@claude" must end the line or be followed by , : or whitespace (not "@claudette")
  local prefix = lower:match("^@claude[,:]?%s+") or lower:match("^@claude[,:]?$") or lower:match("^@claude[,:]")
    or lower:match("^claude[,:]%s*")
  if not prefix then return "ignored" end
  local text = msg:sub(#prefix + 1)
  local p = player_index and game.get_player(player_index)  -- nil = server console
  if p and not access.can_ask(p) then  -- free commands drive the shared helper too
    access.deny(p)
    return "denied", access.denied_text()
  end
  local lc = transport.local_commands
  -- a bare "@claude" gets the free help text instead of a (token-spending) empty question
  if not text:find("%S") then
    if not p then return "ignored" end
    text = "help"
  end
  if p then
    local ok, msg = pcall(chat_confirm, p, text)
    if not ok then
      log("claude-bridge chat confirm error: " .. tostring(msg))
    elseif msg then
      claude_print(p, msg)
      return "local", msg
    end
  end
  if p and lc then
    local ok, handled, reply = pcall(lc.try, p, text)
    if not ok then
      log("claude-bridge local_commands error: " .. tostring(handled))
    elseif handled then
      if reply and reply ~= "" then claude_print(p, reply) end
      return "local", reply
    end
  end
  if not transport.ask(p, text, "chat") then return "denied", access.denied_text() end
  return "claude"
end

events.on(defines.events.on_console_chat, function(e) transport.route_chat(e.player_index, e.message) end)

-- test op: {player, text} runs the exact on_console_chat routing for that player
api.register_debug("chat_simulate", function(args)
  local p = util.get_player(args)
  if not p then error("no player") end
  local route, reply = transport.route_chat(p.index, args.text)
  return {route = route, reply = reply}
end)

-- test op: {host_index?, policy?} pretend another player index is the host (nil = back to normal) and/or set the
-- "Who can use Claude" setting (only the mod itself may change it); returns the roles + open UDP request buffers
api.register_debug("access_test", function(args)
  init()
  storage.access_test_host_index = tonumber(args.host_index)
  if args.policy then settings.global["claude-who-can-ask"] = {value = tostring(args.policy)} end
  local udp = {}
  for key, b in pairs(storage.udp_parts) do udp[#udp + 1] = {key = key, n = b.n, total = b.total, bytes = b.bytes} end
  local roles = {}
  for _, p in pairs(game.players) do
    roles[p.name] = {role = access.role(p), can_ask = access.can_ask(p), can_confirm = access.can_confirm(p),
      admin = p.admin, index = p.index}
  end
  return {policy = access.policy(), roles = roles, udp_open = udp}
end)

-- {n?}: last n history entries {who, text, tick, cost?} + status/online + all pending confirmations
api.register("claude_history", function(args)
  return {history = transport.history(tonumber(args.n) or 8), status = transport.status(),
    online = transport.companion_online(), confirms = transport.confirms(nil, true)}
end)

return transport
