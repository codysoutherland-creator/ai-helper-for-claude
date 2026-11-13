-- Defence: attacks on the base are noticed (chat alert + map marker), what the biters destroy is rebuilt (a ghost and
-- the helper), gun turrets are kept in ammo, and turret groups are placed where attacks came from or where the
-- pollution cloud reaches a nest (ops for the autopilot and Claude). Defence v2: once Stone wall is researched each
-- turret group gets a short wall arc toward its nest (only when the arc has no gap); every minute the cloud's total is
-- sampled (its trend: how fast the cloud grows) and the nests near the base are summed up (how near, how many polluted)
-- so the autopilot researches turrets before the first attack and defends nests the cloud is about to reach.
-- storage.claude_defense = {attacks = {{tick, x, y, surface, what}}, alerts = {[area] = tick}, lost, rebuilt,
--   groups = {{x, y, tx, ty, surface, plan, tick, walled, wall_try}}, groups_seeded, poll = {{tick, total}},
--   nests = {tick, dist, polluted, n}}
-- Ops: defense_status, defense_threats {radius?}, defense_place_turrets {x, y, toward_x, toward_y, n?, plan?, walls?},
--   defense_place_walls {x, y, toward_x, toward_y, plan?, radius?}

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
local WALL = "stone-wall"
local GROUPS_KEEP = 40
local POLL_KEEP = 16             -- pollution samples kept (one a minute)
local NEST_SCAN = 400            -- nests this far from the force's spawn count as "near the base" candidates
local RADARS_SCANNED = 32        -- radars whose charted area is searched for nests too (radar network)
local NEAR_STEPS = {32, 64, 96, 128, 160, 224}   -- coarse distance buckets from a nest to the base's nearest building
local CLOUD_SCAN = 4             -- chunks around a nest searched for the edge of the pollution cloud
local WALL_GAP = 2               -- free tiles between the turrets and the wall (room to rebuild, no spitter splash)
local WALL_PAD = 2               -- the wall runs this far past the outer turrets
local WALL_BACK = 3              -- wings bent back toward the base at both ends
local WALL_SHIFTS = {0, 1, 2}    -- the arc moves this far further out when its first line has a gap
local RED = {r = 1, g = 0.3, b = 0.25}
local PREFIX = "[color=1,0.4,0.3][Claude defence][/color] "

local function state()
  local s = storage.claude_defense
  if not s then
    s = {attacks = {}, alerts = {}, lost = 0, rebuilt = 0}
    storage.claude_defense = s
  end
  s.groups = s.groups or {}
  s.poll = s.poll or {}
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
      -- (round 5: a ghost the construction robots will build is theirs - agent.lua robots_cover)
      if tok and type(tags) == "table" and tags.claude_plan == "rebuild" and not agent.robots_cover(g) then
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

-- chunks (ring distance, 0 = its own chunk) from a nest to the nearest polluted chunk, nil beyond CLOUD_SCAN: how
-- soon the cloud reaches it as the base grows (defence v2: the autopilot defends nests at the cloud's edge too)
local function cloud_dist(surface, p)
  local cx, cy = math.floor(p.x / 32), math.floor(p.y / 32)
  for r = 0, CLOUD_SCAN do
    for dx = -r, r do
      for dy = -r, r do
        if math.max(math.abs(dx), math.abs(dy)) == r and
            surface.get_pollution({x = (cx + dx) * 32 + 16, y = (cy + dy) * 32 + 16}) > 0 then
          return r
        end
      end
    end
  end
  return nil
end
defense.cloud_dist = cloud_dist

-- does the force have a building (not its characters or ghosts) within r of p?
local function building_within(surface, force, p, r)
  local all = surface.count_entities_filtered{position = p, radius = r, force = force}
  if all == 0 then return false end
  return all > surface.count_entities_filtered{position = p, radius = r, force = force,
    type = {"character", "entity-ghost"}}
end

-- tiles around a radar it keeps charted ((2 N + 1) chunks square; 2.1: N = 3), as a search radius
local function radar_reach(r)
  local ok, n = pcall(function() return r.prototype.get_max_distance_of_nearby_sector_revealed(r.quality) end)
  return ((ok and type(n) == "number") and n or 3) * 32 + 16
end

-- spawners within `radius` of center, and (radar network) within the area each of the force's radars keeps charted
-- beyond it: a radar on the base's far edge charts nests the spawn-centred search never reached (the long-range scan
-- also generates new chunks, so nests appear there that did not exist before). Each nest once.
local function nests_near(surface, force, center, radius)
  local out, seen, anchors = {}, {}, {center}
  local function add(list)
    for _, sp in pairs(list) do
      local key = sp.unit_number or (sp.position.x .. ":" .. sp.position.y)
      if not seen[key] then seen[key] = true; out[#out + 1] = sp end
    end
  end
  add(surface.find_entities_filtered{type = "unit-spawner", force = "enemy", position = center, radius = radius})
  for _, r in pairs(surface.find_entities_filtered{type = "radar", force = force, limit = RADARS_SCANNED}) do
    local reach = radar_reach(r)
    if util.dist(r.position, center) + reach > radius then
      anchors[#anchors + 1] = r.position
      add(surface.find_entities_filtered{type = "unit-spawner", force = "enemy", position = r.position, radius = reach})
    end
  end
  return out, anchors
end
defense.nests_near = nests_near

-- nests near the base: pollution reaching them is what sends attacks (around center - default the force's spawn - and
-- every radar; charted: whether the force's map shows the nest's chunk)
function defense.threats(surface, force, radius, center)
  local out = {}
  center = center or force.get_spawn_position(surface)
  local nests, anchors = nests_near(surface, force, center, radius or 400)
  for _, sp in pairs(nests) do
    out[#out + 1] = {x = sp.position.x, y = sp.position.y, name = sp.name, pollution = surface.get_pollution(sp.position),
      charted = force.is_chunk_charted(surface, {math.floor(sp.position.x / 32), math.floor(sp.position.y / 32)})}
  end
  -- nearest first, to the centre or a radar's (the 12 kept: a radar's nests are not crowded out by the spawn's)
  local function near(t)
    local d
    for _, a in ipairs(anchors) do
      local x = util.dist(t, a)
      if not d or x < d then d = x end
    end
    return d
  end
  table.sort(out, function(a, b) return near(a) < near(b) end)
  local list = {}
  for i = 1, math.min(#out, 12) do
    local t = out[i]
    -- the force's nearest entity toward this nest (what it would attack first)
    -- (224: a nest at the cloud's edge may still be a few chunks out; growing radii: `limit` returns any 400 of
    -- the base's entities, not the nearest - one search over 224 tiles of a big base could pick a spot deep inside)
    local best, bd
    for _, r in ipairs({32, 64, 128, 224}) do
      for _, e in pairs(surface.find_entities_filtered{position = t, radius = r, force = force, limit = 400}) do
        if e.type ~= "character" and e.type ~= "entity-ghost" then
          local d = util.dist(e.position, t)
          if not bd or d < bd then best, bd = e, d end
        end
      end
      if best then break end
    end
    if best then t.base = {x = best.position.x, y = best.position.y}; t.dist = math.floor(bd) end
    t.cloud = cloud_dist(surface, t)
    list[#list + 1] = t
  end
  return list
end

local function turrets_near(surface, force, p, r)
  return surface.count_entities_filtered{position = p, radius = r, force = force, type = "ammo-turret"} +
    surface.count_entities_filtered{position = p, radius = r, force = force, ghost_type = "ammo-turret"}
end

local function enabled(force, recipe)
  local r = force.recipes[recipe]
  return r ~= nil and r.enabled or false
end

-- the turret group placed nearest p (within 8 tiles), or nil
local function group_at(s, surface, p, plan)
  local best, bd
  for _, g in ipairs(s.groups) do
    if g.surface == surface.name then
      if plan and g.plan == plan then return g end
      local d = util.dist(g, p)
      if d <= 8 and (not bd or d < bd) then best, bd = g, d end
    end
  end
  return best
end

local function add_group(s, surface, p, toward, plan)
  local g = group_at(s, surface, p)
  if g then return g end
  g = {x = p.x, y = p.y, tx = toward.x, ty = toward.y, surface = surface.name, plan = plan, tick = game.tick}
  s.groups[#s.groups + 1] = g
  while #s.groups > GROUPS_KEEP do table.remove(s.groups, 1) end
  return g
end

-- turret groups placed before defence v2 (0.7.x) are not in storage: cluster the force's turrets once (8 tiles), each
-- cluster facing the nest nearest to it, so they get their walls too
local function seed_groups(surface, force)
  local s = state()
  if s.groups_seeded then return end
  s.groups_seeded = true
  local turrets = surface.find_entities_filtered{force = force, type = "ammo-turret", limit = 400}
  local n = 0
  for _, t in pairs(turrets) do
    if not group_at(s, surface, t.position) then
      local nest = surface.find_entities_filtered{type = "unit-spawner", force = "enemy", position = t.position,
        radius = 400}
      local best, bd
      for _, n in pairs(nest) do
        local d = util.dist(n.position, t.position)
        if not bd or d < bd then best, bd = n, d end
      end
      -- (one plan name per group: place_walls finds its group by it, a shared name would mark the first one walled)
      if best then
        n = n + 1
        add_group(s, surface, t.position, best.position, "seeded-" .. n)
      end
    end
  end
end

---------------------------------------------------------------------------
-- walls (defence v2)
---------------------------------------------------------------------------

-- the wall arc in front of turret boxes facing (dx, dy): a straight line across the threat direction, WALL_GAP free
-- tiles past the turrets (+ shift), WALL_PAD tiles past the outer ones, with WALL_BACK-tile wings bent back toward the
-- base at both ends (open at the back: the helper walks in to rearm). Axis-aligned so neighbouring walls join.
-- -> {{x, y}, ...} tile corners
function defense.wall_arc(boxes, dx, dy, shift)
  local horiz = math.abs(dx) >= math.abs(dy)       -- nest east or west: a north-south wall
  local s = (horiz and dx or dy) >= 0 and 1 or -1
  local front, lo, hi
  for _, b in ipairs(boxes) do
    local a0, a1 = horiz and b.left_top.x or b.left_top.y, horiz and b.right_bottom.x or b.right_bottom.y
    local b0, b1 = horiz and b.left_top.y or b.left_top.x, horiz and b.right_bottom.y or b.right_bottom.x
    -- the first free tile line past this turret, toward the nest
    local f = s > 0 and math.ceil(a1 - 0.01) or math.floor(a0 + 0.01) - 1
    if not front or (s > 0 and f > front) or (s < 0 and f < front) then front = f end
    local t0, t1 = math.floor(b0 + 0.01), math.ceil(b1 - 0.01) - 1
    lo = lo and math.min(lo, t0) or t0
    hi = hi and math.max(hi, t1) or t1
  end
  if not front then return {} end
  front = front + s * (WALL_GAP + (shift or 0))
  lo, hi = lo - WALL_PAD, hi + WALL_PAD
  local out = {}
  local function add(a, b) out[#out + 1] = horiz and {a, b} or {b, a} end
  for b = lo, hi do add(front, b) end
  for i = 1, WALL_BACK do
    add(front - s * i, lo)
    add(front - s * i, hi)
  end
  return out
end

-- one arc tile: "have" (a wall, gate or their ghost stands there), "water" (biters cannot cross it either), "free" (a
-- wall ghost fits) or nil: a gap (a belt, a pole, a rock, a cliff: biters walk through or around it)
local function wall_tile(surface, force, x, y)
  local p = {x = x + 0.5, y = y + 0.5}
  local area = {{x + 0.1, y + 0.1}, {x + 0.9, y + 0.9}}
  if surface.count_entities_filtered{area = area, force = force, type = {"wall", "gate"}} > 0 or
      surface.count_entities_filtered{area = area, force = force, ghost_type = {"wall", "gate"}} > 0 then
    return "have"
  end
  local ok, water = pcall(function() return surface.get_tile(x, y).collides_with("water_tile") end)
  if ok and water then return "water" end
  if surface.can_place_entity{name = "entity-ghost", inner_name = WALL, position = p, force = force,
      build_check_type = defines.build_check_type.manual_ghost} then
    return "free"
  end
  -- engine check (2.1): a wall ghost is refused on the tile north or west of water although the wall itself fits
  -- there; the wall fits (by hand) and no other ghost stands on the tile: free too
  if surface.can_place_entity{name = WALL, position = p, force = force,
      build_check_type = defines.build_check_type.manual} and
      surface.count_entities_filtered{area = area, type = "entity-ghost"} == 0 then
    return "free"
  end
  return nil
end

-- first arc (WALL_SHIFTS further out each try) with no gap: -> tiles to place, nil + the first gap
function defense.plan_walls(surface, force, boxes, dx, dy)
  local gap
  for _, sh in ipairs(WALL_SHIFTS) do
    local free, bad = {}, nil
    for _, t in ipairs(defense.wall_arc(boxes, dx, dy, sh)) do
      local st = wall_tile(surface, force, t[1], t[2])
      if st == nil then bad = t; break end
      if st == "free" then free[#free + 1] = t end
    end
    if not bad then return free end
    gap = gap or bad
  end
  return nil, gap
end

-- stone wall ghosts in an arc in front of the turret group at (x, y) facing (toward_x, toward_y); the group's turrets
-- (built or ghosts) within `radius`. Placed only when the whole arc has no gap (a wall with a hole only funnels the
-- biters); the helper builds the ghosts.
function defense.place_walls(surface, force, args)
  local p = {x = args.x, y = args.y}
  local plan = args.plan or ("defense-wall-" .. game.tick)
  local s = state()
  local g = group_at(s, surface, p, args.group)
  if g then g.wall_try = game.tick end
  if not enabled(force, WALL) then return {plan = plan, placed = {}, skipped = "stone wall not researched"} end
  local boxes, cx, cy = {}, 0, 0
  local r = tonumber(args.radius) or 10
  for _, f in ipairs({{type = "ammo-turret"}, {ghost_type = "ammo-turret"}}) do
    f.position, f.radius, f.force = p, r, force
    for _, e in pairs(surface.find_entities_filtered(f)) do
      boxes[#boxes + 1] = e.bounding_box
      cx, cy = cx + e.position.x, cy + e.position.y
    end
  end
  if #boxes == 0 then return {plan = plan, placed = {}, skipped = "no turrets there"} end
  cx, cy = cx / #boxes, cy / #boxes
  local dx, dy = (args.toward_x or cx + 1) - cx, (args.toward_y or cy) - cy
  if dx == 0 and dy == 0 then dx = 1 end
  local tiles, gap = defense.plan_walls(surface, force, boxes, dx, dy)
  if not tiles then
    return {plan = plan, placed = {}, at = {x = cx, y = cy}, skipped = "no gap-free wall arc",
      gap = gap and {x = gap[1], y = gap[2]} or nil}
  end
  if g then g.walled = true end
  local placed = {}
  for _, t in ipairs(tiles) do
    local ok, w = pcall(surface.create_entity, {name = "entity-ghost", inner_name = WALL,
      position = {x = t[1] + 0.5, y = t[2] + 0.5}, force = force})
    if ok and w then
      w.tags = {claude_plan = plan}
      placed[#placed + 1] = {x = t[1] + 0.5, y = t[2] + 0.5}
    end
  end
  if #placed > 0 then
    helper_do({type = "build_ghosts", x = cx, y = cy, radius = 16, plan = plan}, "walls:" .. plan)
    force.print(PREFIX .. "Walling the turrets at [gps=" .. math.floor(cx) .. "," .. math.floor(cy) .. "," ..
      surface.name .. "] (" .. #placed .. " stone walls).")
  end
  return {plan = plan, placed = placed, at = {x = cx, y = cy}}
end

---------------------------------------------------------------------------
-- status (the autopilot's observation)
---------------------------------------------------------------------------

-- every minute: the cloud's total (its trend), and how near the nests are to the base (cached for the status, which
-- the autopilot reads every few seconds)
local function sample_minute()
  local surface = util.get_surface({})
  local force = util.get_force({})
  if not (surface and force) then return end
  local s = state()
  local ok, total = pcall(function() return surface.get_total_pollution() end)
  if ok and type(total) == "number" then
    s.poll[#s.poll + 1] = {game.tick, total}
    while #s.poll > POLL_KEEP do table.remove(s.poll, 1) end
  end
  local c = force.get_spawn_position(surface)
  local nests = nests_near(surface, force, c, NEST_SCAN)
  table.sort(nests, function(a, b) return util.dist(a.position, c) < util.dist(b.position, c) end)
  local near, polluted = nil, 0
  for i = 1, math.min(#nests, 12) do
    local p = nests[i].position
    if surface.get_pollution(p) > 0 then polluted = polluted + 1 end
    for _, r in ipairs(NEAR_STEPS) do
      if near and r >= near then break end
      if building_within(surface, force, p, r) then near = r; break end
    end
  end
  s.nests = {tick = game.tick, dist = near, polluted = polluted, n = #nests}
  seed_groups(surface, force)
end

events.on_nth_tick(REARM_EVERY, function()
  local ok, err = pcall(sample_minute)
  if not ok then log("claude-bridge defense sample: " .. tostring(err)) end
end)

-- per-minute growth of the cloud over the samples kept (positive: the cloud spreads)
local function pollution_trend(s)
  local first, last = s.poll[1], s.poll[#s.poll]
  if not (first and last) or last[1] <= first[1] then return 0, last and last[2] or 0 end
  return (last[2] - first[2]) / ((last[1] - first[1]) / 3600), last[2]
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
  local walls = enabled(force, WALL)
  -- turret groups still without a wall arc (once walls are researched; a group whose arc had a gap is tried again
  -- after 30 game minutes: the helper may have cleared the rock meanwhile)
  local unwalled = {}
  if walls then
    for _, g in ipairs(s.groups) do
      local srf = game.get_surface(g.surface)
      if srf and not g.walled and (not g.wall_try or game.tick - g.wall_try > 60 * 60 * 30) and #unwalled < 4 and
          turrets_near(srf, force, g, 8) > 0 then
        unwalled[#unwalled + 1] = {x = g.x, y = g.y, toward_x = g.tx, toward_y = g.ty, plan = g.plan,
          surface = g.surface}
      end
    end
  end
  local trend, cloud = pollution_trend(s)
  local n = s.nests or {}
  return {attacks = attacks, lost = s.lost, rebuilt = s.rebuilt, turrets = total, low_ammo = low,
    peaceful = peaceful, researched = enabled(force, TURRET), walls_researched = walls, groups = #s.groups,
    unwalled = unwalled, pollution = math.floor(cloud), pollution_trend = math.floor(trend * 10) / 10,
    nest_dist = n.dist, polluted_nests = n.polluted or 0, nests = n.n or 0}
end

-- n gun-turret ghosts in a row across the line from (x, y) toward the threat, 6 tiles out; free spots only. walls (and
-- Stone wall researched): a wall arc in front of them (place_walls)
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
  local res = {plan = plan, placed = placed, at = {x = cx, y = cy}}
  if #placed > 0 then
    add_group(state(), surface, {x = cx, y = cy}, {x = tx, y = ty}, plan)
    helper_do({type = "build_ghosts", x = cx, y = cy, radius = 12}, "turrets:" .. plan)
    helper_do({type = "rearm", x = cx, y = cy, radius = 16, fill = 25, low = LOW_AMMO}, "rearm:" .. plan)
    force.print(PREFIX .. "Placing " .. #placed .. " gun turrets at [gps=" .. math.floor(cx) .. "," .. math.floor(cy) ..
      "," .. surface.name .. "].")
    if args.walls ~= false and enabled(force, WALL) then
      local w = defense.place_walls(surface, force, {x = cx, y = cy, toward_x = tx, toward_y = ty, radius = 10,
        plan = plan .. "-wall", group = plan})
      res.walls, res.walls_skipped = #w.placed, w.skipped
    end
  end
  return res
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

api.register("defense_place_walls", function(args)
  if not (args.x and args.y) then error("x, y required") end
  return defense.place_walls(util.get_surface(args), util.get_force(args), args)
end)

-- the autopilot's observation carries the defence status (defense.lua loads after autopilot.lua)
require("scripts.autopilot").defense_status = defense.status

return defense
