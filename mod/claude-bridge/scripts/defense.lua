-- Defence: attacks on the base are noticed (chat alert + map marker), what the biters destroy is rebuilt (a ghost and
-- the helper), gun turrets are kept in ammo, and turret groups are placed where attacks came from or where the
-- pollution cloud reaches a nest (ops for the autopilot and Claude).
-- storage.claude_defense = {attacks = {{tick, x, y, surface, what}}, alerts = {[area] = tick}, lost, rebuilt}
-- Ops: defense_status, defense_threats {radius?}, defense_place_turrets {x, y, toward_x, toward_y, n?, plan?}

local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")
local agent = require("scripts.agent")

local defense = {}
local ATTACKS_KEEP = 40
local AREA = 32                  -- alerts and rebuild orders are grouped per 32x32 area
local ALERT_GAP = 60 * 30        -- one chat line per area per 30 s
local REARM_EVERY = 60 * 60      -- turret ammo is checked every minute
local LOW_AMMO = 10
local TURRET = "gun-turret"
local AMMO = "firearm-magazine"
local RED = {r = 1, g = 0.3, b = 0.25}
local PREFIX = "[color=1,0.4,0.3][Claude defence][/color] "

local function state()
  local s = storage.claude_defense
  if not s then
    s = {attacks = {}, alerts = {}, lost = 0, rebuilt = 0}
    storage.claude_defense = s
  end
  return s
end

local function setting(name, default)
  local s = settings.global[name]
  if s == nil then return default end
  return s.value
end

local function own(force) return force and force.name ~= "enemy" and force.name ~= "neutral" end

local function area_key(surface, p)
  return surface.name .. ":" .. math.floor(p.x / AREA) .. ":" .. math.floor(p.y / AREA)
end

-- the helper's queue already holds a task like `t` (same type, same area)?
local function queued(a, kind, key)
  for _, t in ipairs(a.queue or {}) do
    if t.type == kind and (key == nil or t.defense == key) then return true end
  end
  return a.cur and a.cur.type == kind and (key == nil or a.cur.defense == key)
end

local function helper_do(task, key)
  local ok, a = pcall(agent.get)
  if not (ok and a and a.entity and a.entity.valid) then return false end
  if queued(a, task.type, key) then return true end
  task.defense, task.soft = key, true
  pcall(agent.queue, {task}, false)
  return true
end

---------------------------------------------------------------------------
-- attacks: alert, marker, rebuild
---------------------------------------------------------------------------

-- unit -> {direction, recipe, force}: on_entity_died runs before on_post_entity_died in the same tick (whose `force`
-- is the killer's, not the owner's)
local dying = {}

local function on_died(e)
  local ent = e.entity
  if not (ent and ent.valid and own(ent.force)) then return end
  if not (e.force and e.force.name == "enemy") then return end
  local s = state()
  local p = {x = math.floor(ent.position.x * 10) / 10, y = math.floor(ent.position.y * 10) / 10}
  s.attacks[#s.attacks + 1] = {tick = game.tick, x = p.x, y = p.y, surface = ent.surface.name, what = ent.name}
  while #s.attacks > ATTACKS_KEEP do table.remove(s.attacks, 1) end
  s.lost = s.lost + 1
  if ent.unit_number then
    local rec
    if ent.type == "assembling-machine" then
      local r = ent.get_recipe()
      rec = r and r.name
    end
    dying[ent.unit_number] = {direction = ent.direction, recipe = rec, force = ent.force}
  end
  local key = area_key(ent.surface, p)
  if game.tick - (s.alerts[key] or -ALERT_GAP) >= ALERT_GAP then
    s.alerts[key] = game.tick
    ent.force.print(PREFIX .. "Biters are attacking at [gps=" .. math.floor(p.x) .. "," .. math.floor(p.y) .. "," ..
      ent.surface.name .. "] (lost a " .. ent.name .. "). Rebuilding it.")
    pcall(util.mark, ent.surface, p, "Attack", 30, RED)
  end
end

local function on_post_died(e)
  local info = e.unit_number and dying[e.unit_number]
  if e.unit_number then dying[e.unit_number] = nil end
  if not info or not setting("claude-defense-rebuild", true) then return end
  local surface = game.get_surface(e.surface_index)
  local proto = e.prototype
  if not (surface and proto and proto.items_to_place_this and #proto.items_to_place_this > 0) then return end
  local ghost = e.ghost
  if ghost and ghost.valid and ghost.force ~= info.force then ghost = nil end
  if not (ghost and ghost.valid) then
    local ok, g = pcall(surface.create_entity, {name = "entity-ghost", inner_name = proto.name, position = e.position,
      direction = info.direction, force = info.force, recipe = info.recipe, quality = e.quality})
    ghost = ok and g or nil
  end
  if not (ghost and ghost.valid) then return end
  local tags = ghost.tags or {}
  tags.claude_plan = tags.claude_plan or "rebuild"
  ghost.tags = tags
  state().rebuilt = state().rebuilt + 1
  helper_do({type = "build_ghosts", x = e.position.x, y = e.position.y, radius = 24}, "rebuild:" ..
    area_key(surface, e.position))
end

events.on(defines.events.on_entity_died, function(e)
  local ok, err = pcall(on_died, e)
  if not ok then log("claude-bridge defense died: " .. tostring(err)) end
end)
events.on(defines.events.on_post_entity_died, function(e)
  local ok, err = pcall(on_post_died, e)
  if not ok then log("claude-bridge defense post_died: " .. tostring(err)) end
end)

---------------------------------------------------------------------------
-- ammo upkeep
---------------------------------------------------------------------------

local function low_turrets(force)
  local n, total = 0, 0
  for _, surface in pairs(game.surfaces) do
    for _, t in pairs(surface.find_entities_filtered{type = "ammo-turret", force = force}) do
      total = total + 1
      local inv = t.get_inventory(defines.inventory.turret_ammo)
      if not inv or inv.get_item_count() < LOW_AMMO then n = n + 1 end
    end
  end
  return n, total
end

events.on_nth_tick(REARM_EVERY, function()
  local ok, a = pcall(agent.get)
  if not (ok and a and a.entity and a.entity.valid) then return end
  if setting("claude-defense-rearm", true) and low_turrets(a.entity.force) > 0 then
    helper_do({type = "rearm", fill = 25, low = LOW_AMMO}, "rearm")
  end
  -- rebuild ghosts left standing (the order at the attack can come too early, or the helper was busy and gave up)
  if setting("claude-defense-rebuild", true) then
    for _, g in pairs(a.entity.surface.find_entities_filtered{type = "entity-ghost", force = a.entity.force}) do
      local tok, tags = pcall(function() return g.tags end)
      if tok and type(tags) == "table" and tags.claude_plan == "rebuild" then
        helper_do({type = "build_ghosts", x = g.position.x, y = g.position.y, radius = 24}, "rebuild:" ..
          area_key(g.surface, g.position))
        break
      end
    end
  end
end)

---------------------------------------------------------------------------
-- threats and turret groups
---------------------------------------------------------------------------

-- nests near the base: pollution reaching them is what sends attacks
function defense.threats(surface, force, radius, center)
  local out = {}
  center = center or {x = 0, y = 0}
  for _, sp in pairs(surface.find_entities_filtered{type = "unit-spawner", force = "enemy", position = center,
      radius = radius or 400}) do
    out[#out + 1] = {x = sp.position.x, y = sp.position.y, name = sp.name, pollution = surface.get_pollution(sp.position)}
  end
  table.sort(out, function(a, b) return util.dist(a, center) < util.dist(b, center) end)
  local list = {}
  for i = 1, math.min(#out, 12) do
    local t = out[i]
    -- the force's nearest entity toward this nest (what it would attack first)
    local near = surface.find_entities_filtered{position = t, radius = 160, force = force, limit = 400}
    local best, bd
    for _, e in pairs(near) do
      if e.type ~= "character" and e.type ~= "entity-ghost" then
        local d = util.dist(e.position, t)
        if not bd or d < bd then best, bd = e, d end
      end
    end
    if best then t.base = {x = best.position.x, y = best.position.y}; t.dist = math.floor(bd) end
    list[#list + 1] = t
  end
  return list
end

local function turrets_near(surface, force, p, r)
  return surface.count_entities_filtered{position = p, radius = r, force = force, type = "ammo-turret"} +
    surface.count_entities_filtered{position = p, radius = r, force = force, ghost_type = "ammo-turret"}
end

function defense.status(force)
  local s = state()
  local surface = util.get_surface({})
  local attacks = {}
  for i = #s.attacks, math.max(1, #s.attacks - 9), -1 do
    local t = s.attacks[i]
    local srf = game.get_surface(t.surface)
    attacks[#attacks + 1] = {x = t.x, y = t.y, surface = t.surface, what = t.what,
      age_s = math.floor((game.tick - t.tick) / 60),
      turrets_near = srf and turrets_near(srf, force, t, 24) or 0}
  end
  local low, total = low_turrets(force)
  local peaceful = false
  pcall(function() peaceful = surface.peaceful_mode end)
  return {attacks = attacks, lost = s.lost, rebuilt = s.rebuilt, turrets = total, low_ammo = low,
    peaceful = peaceful, researched = force.recipes[TURRET] and force.recipes[TURRET].enabled or false}
end

-- n gun-turret ghosts in a row across the line from (x, y) toward the threat, 6 tiles out; free spots only
function defense.place_turrets(surface, force, args)
  local p = {x = args.x, y = args.y}
  local tx, ty = args.toward_x or p.x, args.toward_y or p.y
  local dx, dy = tx - p.x, ty - p.y
  local d = math.sqrt(dx * dx + dy * dy)
  if d < 1 then dx, dy, d = 1, 0, 1 end
  dx, dy = dx / d, dy / d
  local out = math.min(6, d / 2)
  local cx, cy = p.x + dx * out, p.y + dy * out
  local n = math.max(1, math.min(tonumber(args.n) or 4, 8))
  local plan = args.plan or ("defense-" .. game.tick)
  local placed = {}
  if args.skip_if_turrets and turrets_near(surface, force, {x = cx, y = cy}, tonumber(args.skip_if_turrets)) > 0 then
    return {plan = plan, placed = placed, at = {x = cx, y = cy}, skipped = "turrets already there"}
  end
  for i = 1, n do
    local off = (i - (n + 1) / 2) * 3
    local want = {x = math.floor(cx - dy * off) + 0.0, y = math.floor(cy + dx * off) + 0.0}
    local spot = surface.find_non_colliding_position(TURRET, want, 5, 1, true)
    if spot and surface.can_place_entity{name = "entity-ghost", inner_name = TURRET, position = spot, force = force,
        build_check_type = defines.build_check_type.manual_ghost} then
      local ok, g = pcall(surface.create_entity, {name = "entity-ghost", inner_name = TURRET, position = spot,
        force = force})
      if ok and g then
        g.tags = {claude_plan = plan}
        placed[#placed + 1] = {x = spot.x, y = spot.y}
      end
    end
  end
  if #placed > 0 then
    helper_do({type = "build_ghosts", x = cx, y = cy, radius = 12}, "turrets:" .. plan)
    helper_do({type = "rearm", x = cx, y = cy, radius = 16, fill = 25, low = LOW_AMMO}, "rearm:" .. plan)
    force.print(PREFIX .. "Placing " .. #placed .. " gun turrets at [gps=" .. math.floor(cx) .. "," .. math.floor(cy) ..
      "," .. surface.name .. "].")
  end
  return {plan = plan, placed = placed, at = {x = cx, y = cy}}
end

api.register("defense_status", function(args)
  return defense.status(util.get_force(args))
end)

api.register("defense_threats", function(args)
  local surface = util.get_surface(args)
  local c = util.pos_of(args.position) or (args.x and {x = args.x, y = args.y}) or nil
  return defense.threats(surface, util.get_force(args), args.radius, c)
end)

api.register("defense_place_turrets", function(args)
  if not (args.x and args.y) then error("x, y required") end
  return defense.place_turrets(util.get_surface(args), util.get_force(args), args)
end)

-- the autopilot's observation carries the defence status (defense.lua loads after autopilot.lua)
require("scripts.autopilot").defense_status = defense.status

return defense
