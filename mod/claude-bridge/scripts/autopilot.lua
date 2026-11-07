-- Autopilot ("Let Claude play", docs/design/AUTOPILOT.md section 3.1 and 5): the host turns it on; the companion then
-- observes, decides and plans, and this file holds what must live in the game:
--   storage.autopilot = {on, by, since_tick, epoch, phase, goal, step, next_step, note, calls = {n, cap}, seen_tick,
--     milestones = {{tick, text}}, approve_queue = {[plan id] = {due, epoch, deferred, first_tick, told}},
--     keepout = {{x, y, tick}}, baseline = {[unit] = true}, baseline_n, owned = {[unit] = plan id},
--     events = {{tick, kind, plan, reason}}, mall_chests = {[unit] = true}, mall_machines = {[unit] = true}}
-- Plans whose owner is "autopilot" are approved here (never by Claude) once the safety checks pass, after a grace
-- period in which Reject still stops them. Only the host (or the server console / RCON) can switch it on or off.
-- Ops: autopilot_get, autopilot_set {on}, autopilot_status {...}, autopilot_milestone {text}, autopilot_keepout,
-- autopilot_observe, autopilot_baseline_check, autopilot_power_gaps.

local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")
local access = require("scripts.access")
local plans = require("scripts.plans")
local agent = require("scripts.agent")

local autopilot = {}
autopilot.on_change = function() end   -- gui.lua overrides it (refresh the Helper tab and the status line)

local KEEPOUT_TTL = 60 * 60 * 3    -- a player's build keeps its ground for 3 game minutes
-- a plan deferred this long is applied anyway: ghosts don't get in the player's way, and a player who plays in the
-- middle of the base kept every stage waiting for ever (live, 0.6.0: "waiting for you to finish nearby" all session)
local MAX_DEFER_TICKS = 60 * 60 * 3
local KEEPOUT_MAX = 200
local MILESTONES_MAX = 50
local EVENTS_MAX = 40
local DEFER_TICKS = 60 * 60       -- a deferred plan is checked again after 60 s
local WAIT_TELL_TICKS = 60 * 60 * 30
local MILESTONE_GAP = 60 * 60 * 2 -- at most one milestone chat line per 2 game minutes
local PREFIX = "[color=0.85,0.47,0.34][Claude autopilot][/color] "

local function setting(name, default)
  local s = settings.global[name]
  if s == nil then return default end
  return s.value
end
autopilot.setting = setting

local function init()
  local s = storage.autopilot
  if not s then
    s = {on = false, epoch = 0, phase = "off", milestones = {}, approve_queue = {}, keepout = {}, owned = {},
      events = {}, mall_chests = {}, mall_machines = {}, calls = {n = 0, cap = 0}}
    storage.autopilot = s
  end
  s.milestones = s.milestones or {}
  s.approve_queue = s.approve_queue or {}
  s.keepout = s.keepout or {}
  s.owned = s.owned or {}
  s.events = s.events or {}
  s.mall_chests = s.mall_chests or {}
  s.mall_machines = s.mall_machines or {}
  s.calls = s.calls or {n = 0, cap = 0}
  return s
end
autopilot.state = init

local function changed()
  local ok, err = pcall(autopilot.on_change)
  if not ok then log("claude-bridge autopilot on_change: " .. tostring(err)) end
end

local function say(text, player)
  if player and player.valid then
    player.print({"", PREFIX, text})
  else
    game.print({"", PREFIX, text})
  end
end

local function event(kind, plan, reason)
  local s = init()
  s.events[#s.events + 1] = {tick = game.tick, kind = kind, plan = plan, reason = reason}
  while #s.events > EVENTS_MAX do table.remove(s.events, 1) end
end

function autopilot.is_on()
  local s = storage.autopilot
  return s ~= nil and s.on == true
end

---------------------------------------------------------------------------
-- the player's builds: baseline + later player builds; keep-out ring
---------------------------------------------------------------------------

local function record_baseline(s)
  s.baseline, s.baseline_n = {}, 0
  local force = game.forces.player
  for _, surface in pairs(game.surfaces) do
    for _, e in pairs(surface.find_entities_filtered{force = force}) do
      if e.valid and e.unit_number and e.type ~= "character" and e.type ~= "entity-ghost" and e.type ~= "resource" then
        s.baseline[e.unit_number] = e.name
        s.baseline_n = s.baseline_n + 1
      end
    end
  end
  -- the helper character is not the player's build
  local a = agent.get()
  if a and a.entity.unit_number and s.baseline[a.entity.unit_number] then
    s.baseline[a.entity.unit_number] = nil
    s.baseline_n = s.baseline_n - 1
  end
end

local function add_keepout(pos)
  local s = init()
  s.keepout[#s.keepout + 1] = {x = pos.x, y = pos.y, tick = game.tick}
  while #s.keepout > KEEPOUT_MAX do table.remove(s.keepout, 1) end
end

-- keep-out boxes {x0, y0, x1, y1}: around each connected player and every recent player build / mine / rotate
function autopilot.keepout_boxes()
  local s = init()
  local r = tonumber(setting("claude-autopilot-keepout", 16)) or 16
  local out, keep = {}, {}
  for _, k in ipairs(s.keepout) do
    if game.tick - k.tick <= KEEPOUT_TTL then
      keep[#keep + 1] = k
      out[#out + 1] = {k.x - r, k.y - r, k.x + r, k.y + r, "build"}
    end
  end
  s.keepout = keep
  for _, p in pairs(game.connected_players) do
    if p.character and p.character.valid then
      local q = p.position
      out[#out + 1] = {q.x - r, q.y - r, q.x + r, q.y + r, "player"}
    end
  end
  return out
end

local function boxes_hit(bb, boxes)
  if not bb then return nil end
  for _, b in ipairs(boxes) do
    if bb[1] < b[3] and b[1] < bb[3] and bb[2] < b[4] and b[2] < bb[4] then return b end
  end
  return nil
end
autopilot.boxes_hit = boxes_hit

-- a mall chest / machine of a plan (ghost tags claude_mall, claude_bar; planner/expand.py): the helper's first
-- material source (agent.lua mall_sets), a chest's inventory limited to `bar` slots
local function mall_built(ent, tags)
  if type(tags) ~= "table" or not (tags.claude_mall or tags.claude_bar) then return end
  local s = autopilot.state()
  local at = {ent.position.x, ent.position.y, ent.surface.name}
  if tags.claude_mall == "chest" then s.mall_chests[ent.unit_number] = at
  elseif tags.claude_mall == "machine" then s.mall_machines[ent.unit_number] = at end
  local bar = tonumber(tags.claude_bar)
  if bar and bar > 0 then
    local inv = ent.get_inventory(defines.inventory.chest)
    if inv and inv.supports_bar() then pcall(inv.set_bar, bar + 1) end
  end
end
autopilot.mall_built = mall_built

local function on_player_build(e)
  local ent = e.entity
  if not (ent and ent.valid) then return end
  add_keepout(ent.position)
  if ent.unit_number then pcall(mall_built, ent, e.tags) end
  local s = storage.autopilot
  if not s then return end
  -- built over an autopilot ghost: it belongs to that plan; anything else a player builds is theirs
  local tags = e.tags
  local pid = type(tags) == "table" and tags.claude_plan
  local p = pid and plans.get(pid)
  if p and p.owner == "autopilot" and ent.unit_number then
    s.owned[ent.unit_number] = pid
  elseif ent.unit_number and s.baseline and ent.type ~= "entity-ghost" then
    if not s.baseline[ent.unit_number] then
      s.baseline[ent.unit_number] = ent.name
      s.baseline_n = (s.baseline_n or 0) + 1
    end
  end
end

local function on_script_built(e)
  local ent = e.entity
  if not (ent and ent.valid and ent.unit_number) then return end
  local tags = e.tags
  pcall(mall_built, ent, tags)
  local s = storage.autopilot
  if not s then return end
  local pid = type(tags) == "table" and tags.claude_plan
  local p = pid and plans.get(pid)
  if p and p.owner == "autopilot" then s.owned[ent.unit_number] = pid end
end

events.on(defines.events.on_built_entity, function(e)
  if not e.player_index then return end
  local ok, err = pcall(on_player_build, e)
  if not ok then log("claude-bridge autopilot built: " .. tostring(err)) end
end)
for _, id in ipairs({defines.events.on_player_mined_entity, defines.events.on_player_rotated_entity}) do
  events.on(id, function(e)
    local ent = e.entity
    if ent and ent.valid then pcall(add_keepout, ent.position) end
  end)
end
events.on(defines.events.script_raised_revive, function(e)
  local ok, err = pcall(on_script_built, e)
  if not ok then log("claude-bridge autopilot revive: " .. tostring(err)) end
end)
events.on(defines.events.script_raised_built, function(e)
  local ok, err = pcall(on_script_built, e)
  if not ok then log("claude-bridge autopilot script built: " .. tostring(err)) end
end)

---------------------------------------------------------------------------
-- on / off
---------------------------------------------------------------------------

local function cancel_helper_tasks()
  local a = agent.get()
  if not a then return 0 end
  local n, keep = 0, {}
  for _, t in ipairs(a.queue or {}) do
    if t.autopilot then n = n + 1 else keep[#keep + 1] = t end
  end
  a.queue = keep
  if a.cur and a.cur.autopilot then
    a.cur = nil
    n = n + 1
    a.entity.walking_state = {walking = false}
    a.path_state, a.path, a.path_id = nil, nil, nil
  end
  return n
end

-- -> ok, message. player nil = server console / RCON (the host's own machine)
function autopilot.set(on, player)
  local s = init()
  if not access.is_host(player) then return false, "Only the host can switch the autopilot on or off." end
  on = on and true or false
  if s.on == on then return true, on and "The autopilot is already on." or "The autopilot is already off." end
  s.epoch = (s.epoch or 0) + 1
  s.on = on
  local who = player and player.name or "the host"
  if on then
    s.by, s.since_tick, s.phase, s.halted = who, game.tick, "start", nil
    s.seen_tick = nil
    if not s.baseline then record_baseline(s) end
    -- plans the autopilot proposed earlier (before a save, or before it was switched off) are queued again
    for _, p in pairs(storage.plans or {}) do
      if p.owner == "autopilot" and p.status == "proposed" and not p.superseded then autopilot.on_proposed(p) end
    end
    say("Claude autopilot on (by " .. who .. "). Claude now plays by itself; switch it off in the Claude panel " ..
      "(Helper tab) or with @claude autopilot off.")
  else
    s.approve_queue = {}
    local n = cancel_helper_tasks()
    s.phase = "off"
    if s.hold then           -- a planner hold (autopilot_hold) ends with the autopilot
      game.speed = s.hold.speed or 1
      s.hold = nil
    end
    say("Claude autopilot off (by " .. who .. "). It stopped at: " .. tostring(s.goal or "-") .. " / " ..
      tostring(s.step or "-") .. "." .. (n > 0 and (" The helper's " .. n .. " autopilot task(s) were cancelled.") or ""))
  end
  event(on and "on" or "off", nil, who)
  changed()
  return true, on and "Autopilot on." or "Autopilot off."
end

---------------------------------------------------------------------------
-- self-approval (section 5.2)
---------------------------------------------------------------------------

local function grace_ticks()
  return math.max(0, tonumber(setting("claude-autopilot-grace-s", 30)) or 30) * 60
end

-- plans.lua calls this when a plan becomes "proposed" (preview or the roadmap offering a stage)
function autopilot.on_proposed(p)
  local s = storage.autopilot
  if not (s and s.on and p and p.owner == "autopilot") then return end
  local due = game.tick + grace_ticks()
  s.approve_queue[p.id] = {due = due, epoch = s.epoch, deferred = 0, first_tick = game.tick}
  say("Autopilot builds " .. tostring(p.title or p.id) .. " in " .. math.floor(grace_ticks() / 60) ..
    " s. Reject it in the Plans tab to stop it.")
  event("queued", p.id)
  changed()
end
plans.on_proposed = function(p)
  local ok, err = pcall(autopilot.on_proposed, p)
  if not ok then log("claude-bridge autopilot on_proposed: " .. tostring(err)) end
end
plans.autopilot_quiet = function(p) return p.owner == "autopilot" and autopilot.is_on() end

-- a planned entity row is part of an APPLIED plan of the same master (a master swap of something an approved stage
-- of that master built; a stage that was only proposed was never approved, so its rows do not count)
local function master_built(p, rm)
  if not p.master then return false end
  for _, q in pairs(storage.plans or {}) do
    if q ~= p and q.master == p.master and (q.status == "applied" or q.status == "applying") then
      for _, e in ipairs(q.entities or {}) do
        if e[1] == rm[1] and e[2] == rm[2] and e[3] == rm[3] then return true end
      end
    end
  end
  return false
end

-- every listed removal is allowed: an autopilot-owned entity, a master swap, or the removal setting is on
local function removals_ok(p)
  if not (p.removes and #p.removes > 0) then return true end
  if setting("claude-autopilot-allow-removal", false) then return true end
  local s = init()
  local surface = game.get_surface(p.surface)
  for _, rm in ipairs(p.removes) do
    local e = surface and plans.find_listed(surface, p, rm)
    local unit = e and e.unit_number
    local own = unit and s.owned[unit]
    if not own and not master_built(p, rm) then return false, rm end
  end
  return true
end

-- tiles of the plan that overlap a player ghost or an entity of the force (other than what the plan removes or
-- already built itself). -> number of conflicts, first conflict
local function conflicts(p)
  local surface, force = game.get_surface(p.surface), game.forces[p.force]
  if not (surface and force) then return 0 end
  local removing = {}
  for _, rm in ipairs(p.removes or {}) do removing[rm[1] .. "@" .. rm[2] .. "," .. rm[3]] = true end
  local n, first = 0, nil
  for _, e in ipairs(p.entities) do
    local proto = prototypes.entity[e[1]]
    local w, h = 1, 1
    if proto then w, h = proto.tile_width, proto.tile_height end
    if e[4] == defines.direction.east or e[4] == defines.direction.west then w, h = h, w end
    local x0, y0 = math.floor(e[2] - w / 2 + 0.5), math.floor(e[3] - h / 2 + 0.5)
    for _, f in pairs(surface.find_entities_filtered{area = {{x0 + 0.1, y0 + 0.1}, {x0 + w - 0.1, y0 + h - 0.1}},
        force = force}) do
      if f.valid and f.type ~= "character" then
        local same = (f.type == "entity-ghost" and f.ghost_name or f.name) == e[1] and f.position.x == e[2] and
          f.position.y == e[3]
        local tagged = false
        if f.type == "entity-ghost" then
          local ok, tags = pcall(function() return f.tags end)
          tagged = ok and type(tags) == "table" and tags.claude_plan ~= nil
        end
        if not same and not removing[f.name .. "@" .. f.position.x .. "," .. f.position.y] and
            not (f.type == "entity-ghost" and tagged) and not f.to_be_deconstructed() then
          n = n + 1
          first = first or {name = f.type == "entity-ghost" and ("ghost of " .. f.ghost_name) or f.name,
            x = f.position.x, y = f.position.y, planned = e[1]}
        end
      end
    end
    if n >= 20 then break end
  end
  return n, first
end
autopilot.conflicts = conflicts

local function drop(id, why, detail)
  local s = init()
  s.approve_queue[id] = nil
  event("dropped", id, why)
  local p = plans.get(id)
  log("claude-bridge autopilot: not applying " .. tostring(id) .. " (" .. why .. ") " ..
    (detail and helpers.table_to_json(detail) or ""))
  if p then p.autopilot_blocked = why end
end

local function apply(p)
  local s = init()
  s.approve_queue[p.id] = nil
  -- the listed removals it may make (master swaps of what an approved plan built; checked above): remembered, so the
  -- baseline check tells them apart from anything else of the player's that disappears
  if p.removes and #p.removes > 0 then
    s.swapped = s.swapped or {}
    local surface = game.get_surface(p.surface)
    for _, rm in ipairs(p.removes) do
      local e = surface and plans.find_listed(surface, p, rm)
      if e and e.unit_number then s.swapped[e.unit_number] = p.id end
    end
  end
  local ok, r = pcall(plans.apply, p.id)
  if not ok then
    event("apply_failed", p.id, tostring(r):sub(1, 120))
    return
  end
  local hok, msg = pcall(agent.work_area, nil, p.bbox, "build", p.title, plans.fuel_at(p), p.id, {autopilot = true})
  if not hok then log("claude-bridge autopilot build: " .. tostring(msg)) end
  event("applied", p.id, hok and "helper building" or ("helper: " .. tostring(msg):sub(1, 80)))
  say("Building " .. tostring(p.title or p.id) .. ".")
  local okt, transport = pcall(require, "scripts.transport")
  if okt and transport.post_event then pcall(transport.post_event, "autopilot_applied " .. p.id) end
end

local function process_queue()
  local s = storage.autopilot
  if not (s and s.on) or next(s.approve_queue) == nil then return end
  local boxes = nil
  for id, q in pairs(s.approve_queue) do
    if game.tick >= q.due then
      local p = plans.get(id)
      if q.epoch ~= s.epoch then
        drop(id, "stale")
      elseif not p or p.status ~= "proposed" or p.superseded then
        drop(id, p and (p.superseded and "superseded" or p.status) or "gone")
      else
        boxes = boxes or autopilot.keepout_boxes()
        local hit = boxes_hit(p.bbox, boxes)
        if hit and game.tick - q.first_tick < MAX_DEFER_TICKS then
          q.due = game.tick + DEFER_TICKS
          q.deferred = (q.deferred or 0) + 1
          s.waiting_for = {x = (hit[1] + hit[3]) / 2, y = (hit[2] + hit[4]) / 2, why = hit[5]}
          if not q.told and game.tick - q.first_tick > WAIT_TELL_TICKS then
            q.told = true
            say("The autopilot is waiting for you to leave [gps=" .. math.floor(s.waiting_for.x) .. "," ..
              math.floor(s.waiting_for.y) .. "] before it builds " .. tostring(p.title or id) .. ".")
          end
          event("deferred", id, hit[5])
        else
          s.waiting_for = nil
          local rok, rm = removals_ok(p)
          if not rok then
            drop(id, "removal_blocked", rm)
          else
            local n, first = conflicts(p)
            if n > 0 then drop(id, "conflict", first) else apply(p) end
          end
        end
      end
      changed()
    end
  end
end

events.on_nth_tick(60, function()
  local ok, err = pcall(process_queue)
  if not ok then log("claude-bridge autopilot queue: " .. tostring(err)) end
end)

---------------------------------------------------------------------------
-- status line + companion state
---------------------------------------------------------------------------

local function companion_offline(s)
  local seen = s.seen_tick
  -- about 3 real minutes without a word from the companion (a planner run takes a minute or two)
  return not seen or game.tick - seen > 10800 * math.max(1, game.speed)
end

-- the one-line status shown next to the Claude button while the autopilot is on (nil when off)
function autopilot.status_line()
  local s = storage.autopilot
  if not (s and s.on) then return nil end
  local parts = {"Autopilot: "}
  if companion_offline(s) then
    parts[#parts + 1] = "on, waiting for the companion"
  elseif s.halted then
    parts[#parts + 1] = "halted: " .. tostring(s.halted)
  elseif s.waiting_for then
    parts[#parts + 1] = "waiting for you to leave " .. math.floor(s.waiting_for.x) .. "," .. math.floor(s.waiting_for.y)
  elseif s.phase == "paused" or s.phase == "stuck" or s.phase == "done" then
    -- done: nothing left this version can build here; the note says why (and what the next goal would need)
    parts[#parts + 1] = s.phase .. (s.note and (": " .. s.note) or "")
  else
    parts[#parts + 1] = tostring(s.goal or "starting") .. " > " .. tostring(s.next_step or s.step or "...")
  end
  local st = agent.handcraft_share and agent.handcraft_share(60 * 60 * 60)
  local calls = s.calls or {}
  local tail = " | " .. (calls.n or 0) .. "/" .. (calls.cap or 0) .. " calls"
  if st and st.total > 0 then tail = tail .. " | hand " .. math.floor(st.share * 100 + 0.5) .. "%" end
  local text = table.concat(parts) .. tail
  if #text > 110 then text = text:sub(1, 107) .. "..." end
  return text
end

local function clean(v, n)
  if v == nil then return nil end
  local t = util.strip_markdown and util.strip_markdown(tostring(v)) or tostring(v)
  t = t:gsub("[%c]", " ")
  if #t > (n or 80) then t = t:sub(1, (n or 80) - 3) .. "..." end
  return t
end

-- {goal?, step?, next_step?, note?, phase?, calls = {n, cap}?, halted?, mall_chests?, mall_machines?}: the
-- companion's view (cannot change on/off)
api.register("autopilot_status", function(args)
  local s = init()
  s.seen_tick = game.tick
  if not s.on then return {on = false} end
  if args.epoch ~= nil and tonumber(args.epoch) ~= s.epoch then return {on = s.on, stale = true, epoch = s.epoch} end
  for _, k in ipairs({"goal", "step", "next_step", "note"}) do
    if args[k] ~= nil then s[k] = clean(args[k], 80) end
  end
  if type(args.phase) == "string" and args.phase:match("^[a-z_]+$") then s.phase = args.phase end
  if args.halted ~= nil then s.halted = args.halted ~= false and clean(args.halted, 80) or nil end
  if type(args.calls) == "table" then
    s.calls = {n = tonumber(args.calls.n) or 0, cap = tonumber(args.calls.cap) or 0}
  end
  for _, key in ipairs({"mall_chests", "mall_machines"}) do
    if type(args[key]) == "table" then
      local surface = game.get_surface("nauvis")
      s[key] = {}
      for _, q in pairs(args[key]) do
        if type(q) == "table" and type(q[1]) == "number" and type(q[2]) == "number" and surface then
          local e = surface.find_entities_filtered{position = {q[1], q[2]}, radius = 0.6, force = "player", limit = 1}[1]
          -- keyed by unit number (a source found by a scan is recognised), the value is where it stands
          if e and e.unit_number then s[key][e.unit_number] = {e.position.x, e.position.y, e.surface.name} end
        end
      end
    end
  end
  changed()
  return {on = s.on, epoch = s.epoch}
end)

api.register("autopilot_milestone", function(args)
  local s = init()
  local text = clean(args.text, 160)
  if not text or text == "" then error("text required") end
  s.milestones[#s.milestones + 1] = {tick = game.tick, text = text}
  while #s.milestones > MILESTONES_MAX do table.remove(s.milestones, 1) end
  local printed = false
  if s.on and (not s.last_print or game.tick - s.last_print >= MILESTONE_GAP or args.important) then
    s.last_print = game.tick
    say(text)
    printed = true
  end
  changed()
  return {printed = printed}
end)

-- {on}: the host's toggle (UDP/RCON callers are the host's own machine; in-game players go through the GUI / chat,
-- which pass their LuaPlayer)
api.register("autopilot_set", function(args)
  local ok, msg = autopilot.set(args.on and true or false, nil)
  return {ok = ok, msg = msg, on = autopilot.is_on(), epoch = storage.autopilot.epoch}
end)

local function count(t)
  local n = 0
  for _ in pairs(t or {}) do n = n + 1 end
  return n
end

-- mall chests that hold something (the mall works), and every item they hold (the helper's mall stock)
local function mall_stock(s)
  local n, items = 0, {}
  for u, at in pairs(s.mall_chests or {}) do
    local surface = type(at) == "table" and game.get_surface(at[3] or "nauvis")
    local e = surface and surface.find_entities_filtered{position = {at[1], at[2]}, radius = 0.6, limit = 1,
      type = {"container", "logistic-container"}}[1]
    if e and e.unit_number == u then
      local inv = e.get_inventory(defines.inventory.chest)
      if inv and not inv.is_empty() then
        n = n + 1
        for _, it in pairs(inv.get_contents()) do items[it.name] = (items[it.name] or 0) + it.count end
      end
    elseif not e then
      s.mall_chests[u] = nil       -- gone (taken apart): no longer a source
    end
  end
  return n, items
end

-- companion = true: the companion's own poll (the status line counts it as online)
function autopilot.get(companion, stock_too)
  local s = init()
  if companion then s.seen_tick = game.tick end
  local q = {}
  for id, e in pairs(s.approve_queue) do q[#q + 1] = {id = id, due_in = math.max(0, e.due - game.tick), deferred = e.deferred} end
  local host
  for _, p in pairs(game.connected_players) do if access.is_host(p) then host = p.index end end
  local ms = {}
  for i = math.max(1, #s.milestones - 9), #s.milestones do ms[#ms + 1] = s.milestones[i] end
  local stocked, stock = 0, {}
  if companion or stock_too then stocked, stock = mall_stock(s) end
  return {on = s.on, epoch = s.epoch, phase = s.phase, goal = s.goal, step = s.step, next_step = s.next_step,
    note = s.note, halted = s.halted, by = s.by, since_tick = s.since_tick, tick = game.tick, speed = game.speed,
    paused = game.tick_paused, host_connected = host ~= nil, keepout = autopilot.keepout_boxes(), queue = q,
    events = s.events, milestones = ms, owned = count(s.owned), baseline = s.baseline_n or 0,
    waiting_for = s.waiting_for, calls = s.calls, mall_chests = count(s.mall_chests),
    mall_stocked = (companion or stock_too) and stocked or nil, mall_stock = (companion or stock_too) and stock or nil,
    held = s.hold ~= nil,
    settings = {grace_s = grace_ticks() / 60, keepout = setting("claude-autopilot-keepout", 16),
      calls_per_hour = setting("claude-autopilot-calls-per-hour", 6),
      allow_removal = setting("claude-autopilot-allow-removal", false),
      use_player_items = setting("claude-autopilot-use-player-items", false)},
    helper = agent.counters and agent.counters() or nil, status_line = autopilot.status_line()}
end
api.register("autopilot_get", function(args) return autopilot.get(args.companion == true, args.stock == true) end)
api.register("autopilot_keepout", function() return autopilot.keepout_boxes() end)

-- the baseline (the player's entities when the autopilot was first turned on) still standing? (live harness, Q1)
api.register("autopilot_baseline_check", function()
  local s = init()
  if not s.baseline then return {baseline = 0, missing = 0} end
  local alive = {}
  for _, surface in pairs(game.surfaces) do
    for _, e in pairs(surface.find_entities_filtered{force = "player"}) do
      if e.valid and e.unit_number then alive[e.unit_number] = e end
    end
  end
  local missing, list, swapped = 0, {}, {}
  for unit, name in pairs(s.baseline) do
    if not alive[unit] then
      if s.swapped and s.swapped[unit] then
        swapped[#swapped + 1] = {unit = unit, name = name, plan = s.swapped[unit]}
      else
        missing = missing + 1
        if #list < 50 then list[#list + 1] = {unit = unit, name = name} end
      end
    end
  end
  -- missing: the player's entities gone for another reason than an approved in-place swap (should be 0)
  return {baseline = s.baseline_n or 0, missing = missing, missing_units = list, swapped = swapped}
end)

---------------------------------------------------------------------------
-- one-call observation for the companion (section 3.2 Observer)
---------------------------------------------------------------------------

local ITEMS = {"iron-ore", "copper-ore", "coal", "stone", "iron-plate", "copper-plate", "steel-plate", "stone-brick",
  "iron-gear-wheel", "copper-cable", "electronic-circuit", "automation-science-pack", "logistic-science-pack",
  "military-science-pack", "chemical-science-pack", "transport-belt", "inserter"}
local BURNERS = {"furnace", "mining-drill", "inserter", "boiler"}
local POWERED = {"assembling-machine", "lab", "inserter", "mining-drill", "furnace", "radar", "pump"}

local function production(force, surface)
  local is = force.get_item_production_statistics(surface)
  local out, used = {}, {}
  local p = defines.flow_precision_index.ten_minutes
  for _, n in ipairs(ITEMS) do
    local ok, v = pcall(is.get_flow_count, {name = n, category = "input", precision_index = p})
    if ok and v > 0.001 then out[n] = util.round(v, 1) end
    local ok2, u = pcall(is.get_flow_count, {name = n, category = "output", precision_index = p})
    if ok2 and u > 0.001 then used[n] = util.round(u, 1) end
  end
  local made = {}
  for _, n in ipairs({"automation-science-pack", "logistic-science-pack", "iron-plate", "copper-plate"}) do
    made[n] = is.get_input_count(n)
  end
  return out, used, made
end

local function research(force)
  local cur = force.current_research
  local o = {current = cur and cur.name or nil, progress = util.round(force.research_progress or 0, 3), queue = {},
    done = {}, units_done = 0}
  for _, t in pairs(force.research_queue or {}) do o.queue[#o.queue + 1] = type(t) == "string" and t or t.name end
  for name, t in pairs(force.technologies) do
    if t.researched and t.enabled then
      o.done[#o.done + 1] = name
      if not t.prototype.research_trigger then o.units_done = o.units_done + (t.research_unit_count or 0) end
    end
  end
  if cur then o.units_done = o.units_done + (cur.research_unit_count or 0) * (force.research_progress or 0) end
  o.units_done = util.round(o.units_done, 2)
  return o
end

-- each electric network: generation (kW), consumers, whether it has generators; the sample pole positions
local function power(surface, force)
  local nets, order = {}, {}
  for _, pole in pairs(surface.find_entities_filtered{type = "electric-pole", force = force}) do
    local id = pole.electric_network_id
    if id then
      local n = nets[id]
      if not n then
        n = {id = id, poles = 0, sample = util.pos_out(pole.position), gen_kw = 0, use_kw = 0}
        nets[id] = n
        order[#order + 1] = n
        local st = pole.electric_network_statistics
        local fp = defines.flow_precision_index.one_minute
        for name in pairs(st.output_counts) do
          n.gen_kw = n.gen_kw + st.get_flow_count{name = name, category = "output", precision_index = fp} * 60 / 1000
        end
        for name in pairs(st.input_counts) do
          n.use_kw = n.use_kw + st.get_flow_count{name = name, category = "input", precision_index = fp} * 60 / 1000
        end
      end
      n.poles = n.poles + 1
    end
  end
  local gens, cap_kw = 0, 0
  for _, g in pairs(surface.find_entities_filtered{type = {"generator", "solar-panel", "burner-generator", "electric-energy-interface"}, force = force}) do
    gens = gens + 1
    local ok, v = pcall(function() return g.prototype.get_max_energy_production() * 60 / 1000 end)
    if ok and v then cap_kw = cap_kw + v end
    local id = g.electric_network_id
    if id and nets[id] then nets[id].generators = (nets[id].generators or 0) + 1 end
  end
  local low, nopower = 0, 0
  for _, e in pairs(surface.find_entities_filtered{type = POWERED, force = force}) do
    local st = e.status
    if st == defines.entity_status.low_power then low = low + 1
    elseif st == defines.entity_status.no_power then nopower = nopower + 1 end
  end
  for _, n in ipairs(order) do n.gen_kw, n.use_kw = util.round(n.gen_kw, 1), util.round(n.use_kw, 1) end
  return {networks = order, generators = gens, capacity_kw = util.round(cap_kw, 1), low_power = low,
    no_power = nopower}
end

-- networks with consumers but no generator, and the nearest pole pair to a generating network (R4)
function autopilot.power_gaps(surface, force, max_d)
  max_d = max_d or 40
  local by, gen = {}, {}
  for _, g in pairs(surface.find_entities_filtered{type = {"generator", "solar-panel", "burner-generator", "electric-energy-interface"}, force = force}) do
    if g.electric_network_id then gen[g.electric_network_id] = true end
  end
  for _, pole in pairs(surface.find_entities_filtered{type = "electric-pole", force = force}) do
    local id = pole.electric_network_id
    if id then
      by[id] = by[id] or {}
      table.insert(by[id], pole)
    end
  end
  local gaps = {}
  for id, poles in pairs(by) do
    if not gen[id] then
      -- consumers on this network (a network of bare poles is nothing to fix)
      local consumers = 0
      for _, pole in ipairs(poles) do
        local r = pole.prototype.get_supply_area_distance() or 2.5
        local p = pole.position
        consumers = consumers + surface.count_entities_filtered{area = {{p.x - r, p.y - r}, {p.x + r, p.y + r}},
          type = POWERED, force = force}
        if consumers > 0 then break end
      end
      if consumers > 0 then
        local best
        for gid, gpoles in pairs(by) do
          if gen[gid] then
            for _, a in ipairs(poles) do
              for _, b in ipairs(gpoles) do
                local d = util.dist(a.position, b.position)
                if d <= max_d and (not best or d < best.d) then
                  best = {d = d, a = util.pos_out(a.position), b = util.pos_out(b.position), a_name = a.name,
                    b_name = b.name, net = id, to = gid}
                end
              end
            end
          end
        end
        gaps[#gaps + 1] = best or {net = id, unreachable = true, sample = util.pos_out(poles[1].position)}
      end
    end
  end
  -- labs and assemblers no pole reaches at all (no network): a pole line from the nearest powered pole (cover = the
  -- last pole goes next to the machine)
  local lone = {}
  for _, e in pairs(surface.find_entities_filtered{type = {"lab", "assembling-machine"}, force = force}) do
    if e.status == defines.entity_status.no_power and (e.electric_network_id == nil) and #lone < 6 then
      local best
      for gid, gpoles in pairs(by) do
        if gen[gid] then
          for _, b in ipairs(gpoles) do
            local d = util.dist(e.position, b.position)
            if d <= max_d and (not best or d < best.d) then
              best = {d = d, a = util.pos_out(b.position), b = util.pos_out(e.position), net = "lone" .. (e.unit_number or 0),
                to = gid, cover = true, a_name = b.name, b_name = e.name}
            end
          end
        end
      end
      if best and not lone[best.a.x .. "," .. best.a.y] then
        lone[best.a.x .. "," .. best.a.y] = true
        gaps[#gaps + 1] = best
      end
    end
  end
  return gaps
end

api.register("autopilot_power_gaps", function(args)
  local surface = util.get_surface(args)
  return autopilot.power_gaps(surface, util.get_force(args), tonumber(args.max_d) or 40)
end)

-- free tiles for small poles on the line between two points (R4): one every `step` tiles, each the free tile
-- nearest to its spot on the line. -> {{x, y}} or nil when a spot has no free tile
api.register("autopilot_pole_spots", function(args)
  local surface = util.get_surface(args)
  local force = util.get_force(args)
  local a, b = args.a, args.b
  local name = args.name or "small-electric-pole"
  local step = tonumber(args.step) or 6
  local d = util.dist(a, b)
  -- cover: b is a machine to power, not a pole: the line ends with a pole 3 tiles short of its center
  if args.cover and d > 3 then
    b = {x = b.x + (a.x - b.x) * 3 / d, y = b.y + (a.y - b.y) * 3 / d}
    d = d - 3
  end
  local n = math.max(1, math.ceil(d / step) - 1)
  local pts = {}
  for i = 1, n do pts[#pts + 1] = i / (n + 1) end
  if args.cover then pts[#pts + 1] = 1 end
  local out = {}
  for _, t in ipairs(pts) do
    local cx, cy = a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t
    local found
    for r = 0, 3 do
      for dx = -r, r do
        for dy = -r, r do
          if not found and (math.abs(dx) == r or math.abs(dy) == r) then
            local p = {x = math.floor(cx + dx) + 0.5, y = math.floor(cy + dy) + 0.5}
            if surface.can_place_entity{name = name, position = p, force = force,
                build_check_type = defines.build_check_type.manual_ghost} and
                surface.count_entities_filtered{position = p, radius = 0.6, type = "entity-ghost", limit = 1} == 0 then
              found = p
            end
          end
        end
      end
    end
    if not found then return {ok = false, at = {x = cx, y = cy}} end
    out[#out + 1] = {found.x, found.y}
  end
  return {ok = true, spots = out}
end)

local function burners(surface, force)
  local out, samples = 0, {}
  for _, e in pairs(surface.find_entities_filtered{type = BURNERS, force = force}) do
    if e.burner and not e.burner.currently_burning and e.burner.inventory.is_empty() and
        e.status ~= defines.entity_status.no_minable_resources then
      out = out + 1
      if #samples < 12 then samples[#samples + 1] = {e.position.x, e.position.y, e.name} end
    end
  end
  return out, samples
end

local function plan_rows(force)
  local ghosts = {}
  for _, surface in pairs(game.surfaces) do
    for _, g in pairs(surface.find_entities_filtered{type = "entity-ghost", force = force}) do
      local ok, tags = pcall(function() return g.tags end)
      local pid = ok and type(tags) == "table" and tags.claude_plan
      if pid then ghosts[pid] = (ghosts[pid] or 0) + 1 end
    end
  end
  local s = storage.autopilot or {}
  local out = {}
  for _, p in ipairs(plans.list(force.name)) do
    local row = {id = p.id, status = p.status, owner = p.owner, master = p.master, stage = p.stage,
      superseded = p.superseded, ghosts = ghosts[p.id] or 0, entities = #p.entities, blocked = p.autopilot_blocked,
      queued = s.approve_queue and s.approve_queue[p.id] and true or nil, bbox = p.bbox}
    if p.status == "applied" then row.built = plans.stage_built(p) end
    if p.future then row.gate = p.future.gate; row.after = p.future.after end
    if p.result then row.placed, row.result_blocked = p.result.placed, p.result.blocked end
    out[#out + 1] = row
  end
  return out
end

local function labs(surface, force)
  local out = {}
  for _, e in pairs(surface.find_entities_filtered{type = "lab", force = force}) do
    if #out < 40 then
      out[#out + 1] = {e.position.x, e.position.y, e.status == defines.entity_status.working and 1 or 0,
        e.status ~= defines.entity_status.no_power and 1 or 0}
    end
  end
  return out
end

-- {hold = true|false}: while the companion's planner runs (a minute or two of real time), a sped-up game (game.speed
-- > 1, a test server) runs at normal speed so the base does not race ahead of the autopilot; restored on hold=false,
-- when the autopilot is switched off, or after HOLD_MAX game ticks at the latest (a companion that died while holding)
local HOLD_MAX = 60 * 60 * 6
api.register("autopilot_hold", function(args)
  local s = init()
  if args.hold then
    if not s.hold and game.speed > 1 then
      s.hold = {speed = game.speed, tick = game.tick}
      game.speed = 1
    elseif s.hold then
      s.hold.tick = game.tick
    end
  elseif s.hold then
    game.speed = s.hold.speed or 1
    s.hold = nil
  end
  return {speed = game.speed, held = s.hold ~= nil}
end)
local function release_hold()
  local s = storage.autopilot
  if s and s.hold and (not s.on or game.tick - (s.hold.tick or 0) > HOLD_MAX) then
    game.speed = s.hold.speed or 1
    s.hold = nil
  end
end
events.on_nth_tick(600, function() pcall(release_hold) end)

-- {queue = {tech, ...}}: the research queue becomes exactly these (the companion drops techs whose science packs
-- the base does not make, so the labs never sit blocked behind one). -> the queue now
api.register("autopilot_research", function(args)
  local force = util.get_force(args)
  local want = {}
  for _, name in ipairs(args.queue or {}) do
    local t = force.technologies[name]
    if t and t.enabled and not t.researched and #want < 7 then want[#want + 1] = name end
  end
  local ok, err = pcall(function() force.research_queue = want end)
  local q = {}
  for _, t in pairs(force.research_queue or {}) do q[#q + 1] = type(t) == "string" and t or t.name end
  return {ok = ok, error = ok and nil or tostring(err), queue = q}
end)

-- {surface?}: everything the companion's Observer needs in one call (numbers and prototype names only)
api.register("autopilot_observe", function(args)
  local surface = util.get_surface(args)
  local force = util.get_force(args)
  local made, used, totals = production(force, surface)
  local nofuel, nofuel_at = burners(surface, force)
  local census = {}
  for _, e in pairs(surface.find_entities_filtered{force = force}) do
    if e.type ~= "entity-ghost" and e.type ~= "character" then census[e.name] = (census[e.name] or 0) + 1 end
  end
  local problems = api.ops.problems({surface = surface.name, samples = 2})
  local a = agent.get()
  local helper = {alive = a ~= nil}
  if a then
    local st = agent.status()
    helper = {alive = true, pos = st.pos, halted = st.halted, current = st.current and st.current.type or nil,
      queue = #(a.queue or {}), log = {}, counters = agent.counters()}
    for i = math.max(1, #(a.log or {}) - 4), #(a.log or {}) do helper.log[#helper.log + 1] = a.log[i].msg end
  end
  local peaceful = false
  pcall(function() peaceful = surface.peaceful_mode end)
  local spawners = surface.count_entities_filtered{force = "enemy", type = "unit-spawner", limit = 1}
  return {tick = game.tick, speed = game.speed, research = research(force), made = made, used = used,
    totals = totals, power = power(surface, force), burners_out = nofuel, burners_out_at = nofuel_at,
    census = census, problems = problems, helper = helper, plans = plan_rows(force), labs = labs(surface, force),
    enemies = {peaceful = peaceful, spawners = spawners}, autopilot = autopilot.get(true),
    defense = autopilot.defense_status and select(2, pcall(autopilot.defense_status, force)) or nil}
end)

---------------------------------------------------------------------------
-- events
---------------------------------------------------------------------------

events.on_init(init)
events.on_configuration_changed(function() init() end)

return autopilot
