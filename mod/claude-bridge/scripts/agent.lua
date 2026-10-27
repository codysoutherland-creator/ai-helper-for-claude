-- The co-op helper character: a task queue run on_tick (auto-walk, halt on failure) + free quick actions.
-- storage.agent = {entity, label, queue = {tasks}, cur = task|nil, log = {}, halted = reason|nil, notify = player_index|nil}

local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")
local round, pos_out, dist, get_player, get_surface, get_force, parse_dir, inv_contents, entity_at =
  util.round, util.pos_out, util.dist, util.get_player, util.get_surface, util.get_force, util.parse_dir,
  util.inv_contents, util.entity_at
local ops = api.ops

local agent = {}

local function get()
  local a = storage.agent
  if a and a.entity and a.entity.valid then return a end
  return nil
end
agent.get = get

local function alog(a, msg)
  a.log = a.log or {}
  a.log_n = (a.log_n or 0) + 1  -- total ever logged (for "log lines since X")
  table.insert(a.log, {tick = game.tick, msg = msg})
  while #a.log > 25 do table.remove(a.log, 1) end
end

-- Create the character near `near` (no items unless kit). Returns state or nil, err.
local function create(surface, near, force, kit)
  local pos = surface.find_non_colliding_position("character", {x = near.x + 3, y = near.y}, 20, 0.5)
  if not pos then return nil, "no room to spawn" end
  local c = surface.create_entity{name = "character", position = pos, force = force}
  if not c then return nil, "spawn failed" end
  c.color = util.CLAUDE_COLOR
  local label = rendering.draw_text{text = "Claude", surface = surface, target = {entity = c, offset = {0, -2.6}},
    color = util.CLAUDE_COLOR, scale = 1.3, alignment = "center", use_rich_text = false}
  storage.agent = {entity = c, label = label, queue = {}, log = {}}
  if kit then
    c.insert{name = "iron-plate", count = 50}
    c.insert{name = "wood", count = 20}
    c.insert{name = "stone-furnace", count = 5}
    c.insert{name = "burner-mining-drill", count = 5}
    c.insert{name = "coal", count = 50}
  end
  alog(storage.agent, "spawned")
  return storage.agent
end

-- Inventory into a wooden chest at its feet (spill the rest), then remove.
local function remove(drop_items)
  local a = storage.agent
  if a then
    if a.label and a.label.valid then a.label.destroy() end
    if a.entity and a.entity.valid then
      if drop_items ~= false then
        local inv = a.entity.get_main_inventory()
        if inv and not inv.is_empty() then
          local chest = a.entity.surface.create_entity{name = "wooden-chest", position = a.entity.position,
            force = a.entity.force}
          for i = 1, #inv do
            if inv[i].valid_for_read then
              local n = chest and chest.insert(inv[i]) or 0
              if n < inv[i].count then
                a.entity.surface.spill_item_stack{position = a.entity.position, stack = {name = inv[i].name, count = inv[i].count - n}}
              end
            end
          end
        end
      end
      a.entity.destroy()
    end
  end
  storage.agent = nil
end

-- near player (or at `pos` when there's no player, e.g. headless tests)
function agent.spawn(player, pos)
  local a = get()
  if a then return true, "Helper is already here." end
  local surface = player and player.surface or game.get_surface("nauvis")
  local near = player and player.position or pos or {x = 0, y = 0}
  local force = player and player.force or game.forces.player
  local na, err = create(surface, near, force, false)
  if not na then return false, "Couldn't spawn the helper: " .. err end
  return true, "Helper spawned."
end

function agent.dismiss()
  if not storage.agent then return "No helper to dismiss." end
  remove(true)
  return "Helper dismissed (its items are in a wooden chest where it stood)."
end

-- forget any in-flight path so the next walk starts its own (late path results are ignored via path_id)
local function reset_path(a)
  a.path_state = nil; a.path = nil; a.path_id = nil
end

function agent.queue(tasks, replace)
  local a = get()
  if not a then error("agent not spawned (use agent_spawn)") end
  if replace then a.queue = {}; a.cur = nil; reset_path(a); a.entity.walking_state = {walking = false} end
  for _, t in pairs(tasks or {}) do
    if not t.type then error("task missing type") end
    a.queue[#a.queue + 1] = t
  end
  a.halted = nil
  return #a.queue
end

function agent.stop()
  local a = get()
  if not a then return false end
  a.queue = {}; a.cur = nil; a.halted = "stopped"; a.notify_what = nil
  reset_path(a)
  a.entity.walking_state = {walking = false}
  return true
end

-- copy of a task without LuaObjects / internal state (for JSON)
local function plain(t)
  if not t then return nil end
  local o = {}
  for k, v in pairs(t) do
    if type(v) ~= "userdata" and type(v) ~= "table" and k ~= "approach" and k ~= "last_check" and k ~= "stuck_checks" then
      o[k] = v
    end
  end
  return o
end

function agent.status()
  local a = get()
  if not a then return {alive = false} end
  local c = a.entity
  local q = {}
  for i, t in ipairs(a.queue) do
    q[#q + 1] = t.type .. (t.name and (":" .. t.name) or "") .. (t.recipe and (":" .. t.recipe) or "")
    if i >= 15 then q[#q + 1] = "...+" .. (#a.queue - 15); break end
  end
  return {alive = true, pos = pos_out(c.position), surface = c.surface.name, health = c.health,
    inventory = inv_contents(c.get_main_inventory()),
    crafting = c.crafting_queue_size > 0 and c.crafting_queue or nil,
    current = plain(a.cur), queue = q, halted = a.halted, log = a.log}
end

---------------------------------------------------------------------------
-- ops (names/args/outputs unchanged from v0.1)
---------------------------------------------------------------------------

function ops.agent_spawn(args)
  local a = get()
  if a then return {already = true, pos = pos_out(a.entity.position)} end
  local p = get_player(args)
  local surface = p and p.surface or get_surface(args)
  local na, err = create(surface, p and p.position or {x = 0, y = 0}, get_force(args), args.kit)
  if not na then error(err) end
  return {pos = pos_out(na.entity.position)}
end

function ops.agent_remove(args)
  remove(args.drop_items)
  return true
end

function ops.agent_status(args) return agent.status() end

-- args.tasks = [...], args.replace = bool
function ops.agent_tasks(args)
  return {queued = agent.queue(args.tasks, args.replace)}
end

function ops.agent_stop(args) return agent.stop() end

-- Debug/cheat: give the agent items directly (debug ops only).
api.register_debug("agent_give", function(args)
  local a = get()
  if not a then error("no agent") end
  local n = a.entity.insert{name = args.item, count = args.count or 1}
  return {inserted = n}
end)

-- Free quick action (same as the GUI buttons / chat keywords): {action, player?, count?, x?, y?}
-- Without a player (headless), "the player" is x,y or the helper's own position.
local function quick_op(args)
  if not args.action then error("action required") end
  local p = get_player(args)
  local m = agent.quick(p, args.action, {count = args.count, position = args.x and {x = args.x, y = args.y} or nil})
  return {msg = m, status = agent.describe()}
end
api.register("quick_action", quick_op)
api.register("agent_quick", quick_op)  -- earlier name

-- ---------- movement ----------

local SQRT2 = math.sqrt(2)
local function dir_toward(from, to)
  local dx, dy = to.x - from.x, to.y - from.y
  local ang = math.atan2(dy, dx) -- 0 = east, pi/2 = south
  local oct = math.floor((ang / (math.pi / 4)) + 0.5) % 8
  local map = {[0] = "east", "southeast", "south", "southwest", "west", "northwest", "north", "northeast"}
  return defines.direction[map[oct]]
end

local function request_path(a, goal, radius)
  local c = a.entity
  local proto = prototypes.entity["character"]
  a.path_id = c.surface.request_path{
    bounding_box = {{-0.25, -0.25}, {0.25, 0.25}},
    collision_mask = proto.collision_mask,
    start = c.position, goal = goal, force = c.force,
    radius = math.max(radius or 0.5, 0.5),
    pathfind_flags = {cache = false, prefer_straight_paths = false, allow_paths_through_own_entities = false},
    path_resolution_modifier = 0,
    entity_to_ignore = c,
  }
  a.path = nil
  a.path_state = "pending"
end

events.on(defines.events.on_script_path_request_finished, function(e)
  local a = get()
  if not a or a.path_id ~= e.id then return end
  if e.path then
    a.path = e.path
    a.path_i = 1
    a.path_state = "follow"
  elseif e.try_again_later then
    a.path_state = "retry"
    a.retry_at = game.tick + 30
  else
    a.path_state = "failed"
  end
end)

-- returns "done", "running" or nil,err
local function step_walk(a, t)
  local c = a.entity
  local goal = {x = t.x, y = t.y}
  local radius = t.radius or 0.6
  if dist(c.position, goal) <= radius then
    c.walking_state = {walking = false}
    a.path_state = nil
    return "done"
  end
  if not a.path_state or not t.last_pos then  -- new walk (also guards a stale path from a replaced task)
    request_path(a, goal, radius)
    t.stuck_checks = 0
    t.last_pos = c.position
    t.last_check = game.tick
    return "running"
  end
  if a.path_state == "pending" then return "running" end
  if a.path_state == "retry" then
    if game.tick >= a.retry_at then request_path(a, goal, radius) end
    return "running"
  end
  local target
  if a.path_state == "failed" then
    -- no path found: try walking straight, the stuck check will give up eventually
    target = goal
  else
    local wp = a.path[a.path_i]
    while wp and dist(c.position, wp.position) < 0.35 do
      a.path_i = a.path_i + 1
      wp = a.path[a.path_i]
    end
    target = wp and wp.position or goal
  end
  c.walking_state = {walking = true, direction = dir_toward(c.position, target)}
  if game.tick - (t.last_check or 0) >= 60 then
    if dist(c.position, t.last_pos) < 0.2 then
      t.stuck_checks = (t.stuck_checks or 0) + 1
      if t.stuck_checks >= 4 then
        c.walking_state = {walking = false}
        a.path_state = nil
        return nil, "stuck walking to " .. round(goal.x) .. "," .. round(goal.y)
      end
      request_path(a, goal, radius)
    end
    t.last_pos = c.position
    t.last_check = game.tick
  end
  return "running"
end

-- If target is out of range, push a walk subtask. Returns true if already in range.
local function ensure_near(a, t, pos, range)
  if dist(a.entity.position, pos) <= range then return true end
  t.approach = (t.approach or 0) + 1
  if t.approach > 3 then return nil end
  table.insert(a.queue, 1, t)
  a.cur = {type = "walk", x = pos.x, y = pos.y, radius = math.max(range - 1, 0.6), auto = true, soft = t.soft}
  a.path_state = nil
  return false
end

-- ---------- task handlers ----------

local tasks = {}

tasks.walk = step_walk

function tasks.wait(a, t)
  t.until_tick = t.until_tick or (game.tick + (t.ticks or 60))
  if game.tick >= t.until_tick then return "done" end
  return "running"
end

function tasks.say(a, t)
  api.ops.say{text = t.text}
  return "done"
end

local function mining_ticks(c, target)
  local mp = target.prototype.mineable_properties
  local base = prototypes.entity["character"].mining_speed or 0.5
  local speed = base * (1 + c.character_mining_speed_modifier + c.force.manual_mining_speed_modifier)
  return math.max(1, math.ceil((mp.mining_time or 1) / speed * 60))
end

local function find_mine_target(a, t)
  local c = a.entity
  if t.x then
    local e = entity_at(c.surface, {x = t.x, y = t.y, name = t.name, radius = t.radius or 0.8})
    return e
  end
  -- nearest by name/type to `near` (first target) or the helper; small radii first (big forests are slow to scan)
  if not t.name and not t.entity_type then error("mine needs x,y or name/entity_type") end
  local center = t.near or c.position
  local max_r = t.search_radius or 100
  local f = {position = center}
  if t.name then f.name = t.name end
  if t.entity_type then f.type = t.entity_type end
  for _, r in ipairs{16, 48, max_r} do
    if r <= max_r then
      f.radius = r
      local best, bestd
      for _, e in pairs(c.surface.find_entities_filtered(f)) do
        if e.minable and e.prototype.mineable_properties.minable and e.type ~= "character"
            and not (t.skip and t.skip[e.position.x .. "," .. e.position.y]) then
          local d = dist(e.position, center)
          if not best or d < bestd then best, bestd = e, d end
        end
      end
      if best then t.near = nil; return best end  -- later targets: nearest to the helper
      if r == max_r then break end
    end
  end
  return nil
end

-- {type="mine", x?,y?, name?, entity_type?, count=1}
function tasks.mine(a, t)
  local c = a.entity
  t.count = t.count or 1
  t.done_n = t.done_n or 0
  if t.done_n >= t.count then return "done" end
  local target = t.target
  if not (target and target.valid) then
    target = find_mine_target(a, t)
    t.target = target
    t.timer = nil
    t.approach = nil  -- walk attempts count per target
    if not target then
      if t.done_n > 0 then return "done" end
      return nil, "nothing to mine"
    end
  end
  local range = (target.type == "resource") and c.resource_reach_distance or c.reach_distance
  local near = ensure_near(a, t, target.position, range)
  if near == nil then
    -- searching by name: skip an unreachable one (a few times) instead of giving up
    if t.x or (t.skipped or 0) >= 5 then return nil, "can't reach " .. target.name end
    t.skip = t.skip or {}
    t.skip[target.position.x .. "," .. target.position.y] = true
    t.skipped = (t.skipped or 0) + 1
    t.target = nil
    return "running"
  end
  if not near then return "running" end
  c.walking_state = {walking = false}
  if not t.timer then t.timer = game.tick + mining_ticks(c, target) end
  if game.tick < t.timer then return "running" end
  t.timer = nil
  local inv = c.get_main_inventory()
  if target.type == "resource" then
    local mp = target.prototype.mineable_properties
    for _, prod in pairs(mp.products or {}) do
      if prod.type == "item" then
        local amt = prod.amount or prod.amount_min or 1
        if (prod.probability or 1) >= math.random() then
          if c.insert{name = prod.name, count = amt} < amt then return nil, "inventory full" end
        end
      end
    end
    if target.amount > 1 then target.amount = target.amount - 1 else target.destroy() end
  else
    local name = target.name
    if not target.mine{inventory = inv, force = false} then return nil, "inventory full mining " .. name end
  end
  t.done_n = t.done_n + 1
  if t.done_n >= t.count then return "done" end
  if t.x and not (t.target and t.target.valid) then
    -- fixed-position target is gone; continue with nearest of same name if given
    t.x, t.y = nil, nil
    if not t.name then return "done" end
  end
  return "running"
end

-- {type="craft", recipe, count}
function tasks.craft(a, t)
  local c = a.entity
  if not t.started then
    local n = c.begin_crafting{recipe = t.recipe, count = t.count or 1, silent = true}
    if n == 0 then return nil, "can't craft " .. t.recipe .. " (missing ingredients or not unlocked)" end
    t.started = n
    return "running"
  end
  if c.crafting_queue_size == 0 then return "done" end
  return "running"
end

local function item_for(name)
  local proto = prototypes.entity[name]
  if not proto then error("unknown entity " .. tostring(name)) end
  local items = proto.items_to_place_this
  if not items or not items[1] then error("nothing places " .. name) end
  return items[1].name
end

-- {type="place", name, x, y, direction?, recipe?}
function tasks.place(a, t)
  local c = a.entity
  local pos = {x = t.x, y = t.y}
  local item = item_for(t.name)
  if c.get_item_count(item) < 1 then return nil, "no " .. item .. " in inventory" end
  local near = ensure_near(a, t, pos, c.build_distance - 0.5)
  if near == nil then return nil, "can't get in build range" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local dir = parse_dir(t.direction)
  local ghost = c.surface.find_entities_filtered{position = pos, radius = 0.5, ghost_name = t.name, force = c.force, limit = 1}[1]
  -- building for a ghost that has since been removed/built by someone else: don't place anything
  if t.ghost_only and not ghost then return "done" end
  local built
  if ghost then
    c.remove_item{name = item, count = 1}
    local _, e = ghost.revive{raise_revive = true}
    if not e then c.insert{name = item, count = 1}; return nil, "ghost revive failed (blocked?)" end
    built = e
  else
    if not c.surface.can_place_entity{name = t.name, position = pos, direction = dir, force = c.force,
        build_check_type = defines.build_check_type.manual} then
      return nil, "can't place " .. t.name .. " at " .. pos.x .. "," .. pos.y .. " (blocked)"
    end
    built = c.surface.create_entity{name = t.name, position = pos, direction = dir, force = c.force,
      raise_built = true, create_build_effect_smoke = true}
    if not built then return nil, "create_entity failed" end
    c.remove_item{name = item, count = 1}
  end
  if t.recipe then pcall(function() built.set_recipe(t.recipe) end) end
  return "done"
end

local function target_inventory(e, want_output)
  if e.type == "container" or e.type == "logistic-container" then return e.get_inventory(defines.inventory.chest) end
  if want_output then return e.get_output_inventory() end
  return nil
end

-- best fuel the helper carries for a fuel inventory (highest fuel value that fits)
local function pick_fuel(c, fuel_inv)
  local best, bestv
  for _, it in pairs(c.get_main_inventory().get_contents()) do
    local proto = prototypes.item[it.name]
    if proto and proto.fuel_value > 0 and (not best or proto.fuel_value > bestv)
        and fuel_inv.can_insert{name = it.name, count = 1} then
      best, bestv = it.name, proto.fuel_value
    end
  end
  return best
end

-- ---------- build ghosts, fetching/crafting the materials ----------

local ROUND_GHOSTS = 40    -- ghosts handled per round (bounds how much the helper carries at once)
local SOURCE_RADIUS = 150  -- how far from the build site to look for chests / furnace output
local WOOD_PER_TREE = 4
local MINEABLE = {stone = true, coal = true, ["iron-ore"] = true, ["copper-ore"] = true}
-- gather order when materials are scarce: things that produce (drills, furnaces) before logistics
local BUILD_RANK = {["mining-drill"] = 0, furnace = 0}

local function place_item(ghost_name)
  local proto = prototypes.entity[ghost_name]
  local it = proto and proto.items_to_place_this and proto.items_to_place_this[1]
  if not it then return nil end
  return it.name, it.count or 1
end

local char_cats
local function handcraftable(recipe)
  if not (recipe and recipe.enabled and not recipe.hidden) then return false end
  if recipe.prototype.hide_from_player_crafting then return false end
  char_cats = char_cats or prototypes.entity["character"].crafting_categories
  for k, v in pairs(recipe.categories) do
    local name = type(k) == "string" and k or (type(v) == "string" and v or (v and v.name))
    if name and char_cats[name] then return true end
  end
  return false
end

local function product_amount(recipe, item)
  for _, p in pairs(recipe.products) do
    if p.name == item then return p.amount or p.amount_min or 1 end
  end
  return 0
end

local function recipe_cats(recipe)
  local out = {}
  for k, v in pairs(recipe.categories or {}) do
    local name = type(k) == "string" and k or (type(v) == "string" and v or (v and v.name))
    if name then out[#out + 1] = name end
  end
  return out
end

-- enabled furnace recipe (one item in -> `item` out) the helper can run in a stone furnace, or nil
local SMELTER = "stone-furnace"
local function smelt_recipe(force, item)
  local fp = prototypes.entity[SMELTER]
  if not fp then return nil end
  local cats = fp.crafting_categories
  for _, r in pairs(prototypes.get_recipe_filtered{{filter = "has-product-item", elem_filters = {{filter = "name", name = item}}}}) do
    local fr = force.recipes[r.name]
    if fr and fr.enabled and #fr.ingredients == 1 and fr.ingredients[1].type == "item" and product_amount(fr, item) > 0 then
      for _, cat in ipairs(recipe_cats(fr)) do
        if cats[cat] then return fr, cat end
      end
    end
  end
  return nil
end

local FURNACE_IN, FURNACE_OUT = defines.inventory.crafter_input, defines.inventory.crafter_output

-- joules a furnace burns per second while working
local function furnace_watts(proto)
  return (proto.get_max_energy_usage() or proto.energy_usage or 0) * 60
end

-- Free ground for a new 2x2 furnace near `center`: not on ore, ghosts or anything else (1-tile margin
-- so it never blocks a planned layout or its own access), away from spots already reserved.
local function free_furnace_spot(c, center, reserved)
  local s = c.surface
  local cx, cy = math.floor(center.x), math.floor(center.y)
  for r = 4, 40, 2 do
    for i = -r, r, 2 do
      for _, d in ipairs{{i, -r}, {i, r}, {-r, i}, {r, i}} do
        local p = {x = cx + d[1], y = cy + d[2]}
        local ok = math.abs(c.position.x - p.x) > 2.5 or math.abs(c.position.y - p.y) > 2.5  -- not under the helper
        for _, q in ipairs(reserved) do
          if math.abs(q.x - p.x) < 4 and math.abs(q.y - p.y) < 4 then ok = false; break end
        end
        if ok and s.can_place_entity{name = SMELTER, position = p, force = c.force,
            build_check_type = defines.build_check_type.manual}
            and s.count_entities_filtered{area = {{p.x - 2, p.y - 2}, {p.x + 2, p.y + 2}}, type = "character",
              invert = true, limit = 1} == 0 then
          return p
        end
      end
    end
  end
  return nil
end

-- order planned tasks run in: 1 smelter setup (its ore/fuel/furnace), 2 load the smelter, 3 other
-- fetching/mining (while it smelts), 4 collect the plates, 5 crafting
local PHASE = {take = 3, take_player = 3, mine = 3, craft = 5, place = 1}
local FIRST_LOAD = 8  -- ore mined before a new smelter is started

-- Plans how to get `item` x n into the helper: its own stock, chests/furnace output, the player's
-- inventory, hand-crafting (recursively), smelting (in a nearby burner furnace or one it builds), or
-- mining raw stone/coal/ore/wood. Records an undo log so a failed branch leaves nothing half-planned.
local function new_planner(c, center, player)
  local P = {tasks = {}, missing = {}, undo = {}, summary = {}, miss_n = 0, smelters = {}, spots = {}, used_f = {}}
  local avail, srcs = {}, {}
  for _, e in pairs(c.surface.find_entities_filtered{position = center, radius = SOURCE_RADIUS,
      type = {"container", "logistic-container", "furnace"}, force = c.force}) do
    local inv = target_inventory(e, true)
    if inv and not inv.is_empty() then srcs[#srcs + 1] = {e = e, inv = inv, left = {}} end
  end
  table.sort(srcs, function(s1, s2) return dist(s1.e.position, c.position) < dist(s2.e.position, c.position) end)
  local pinv = player and player.connected and player.character and player.get_main_inventory()
  local pleft = {}

  local function set(tbl, k, v)
    P.undo[#P.undo + 1] = {tbl, k, tbl[k]}
    tbl[k] = v
  end
  local function mark() return {#P.tasks, #P.undo} end
  local function rollback(m)
    for i = #P.undo, m[2] + 1, -1 do
      local u = P.undo[i]
      u[1][u[2]] = u[3]
      P.undo[i] = nil
    end
    for i = #P.tasks, m[1] + 1, -1 do P.tasks[i] = nil end
  end
  local function add(task, note, phase)
    task.soft = true
    task.phase = phase or P.phase or PHASE[task.type] or 3
    P.tasks[#P.tasks + 1] = task
    if note then P.summary[#P.summary + 1] = note end
  end

  function P.acquire(item, n, depth)
    if avail[item] == nil then avail[item] = c.get_item_count(item) end
    local use = math.min(avail[item], n)
    if use > 0 then set(avail, item, avail[item] - use); n = n - use end
    if n <= 0 then return true end
    for _, s in ipairs(srcs) do
      if s.e.valid then
        if s.left[item] == nil then s.left[item] = s.inv.get_item_count(item) end
        local k = math.min(s.left[item], n)
        if k > 0 then
          set(s.left, item, s.left[item] - k)
          add({type = "take", x = s.e.position.x, y = s.e.position.y, name = s.e.name, item = item, count = k},
            k .. " " .. item .. " from " .. s.e.name)
          n = n - k
          if n <= 0 then return true end
        end
      end
    end
    if pinv then
      if pleft[item] == nil then pleft[item] = pinv.get_item_count(item) end
      local k = math.min(pleft[item], n)
      if k > 0 then
        set(pleft, item, pleft[item] - k)
        add({type = "take_player", player = player.index, item = item, count = k}, k .. " " .. item .. " from you")
        n = n - k
        if n <= 0 then return true end
      end
    end
    local r = c.force.recipes[item]
    if (depth or 0) < 5 and handcraftable(r) then
      local per = product_amount(r, item)
      if per > 0 then
        local crafts = math.ceil(n / per)
        local m, miss_before = mark(), P.miss_n
        local ok = true
        for _, ing in pairs(r.ingredients) do
          if ing.type ~= "item" or not P.acquire(ing.name, ing.amount * crafts, (depth or 0) + 1) then ok = false; break end
        end
        if ok then
          add({type = "craft", recipe = r.name, count = crafts}, "craft " .. crafts * per .. " " .. item)
          set(avail, item, avail[item] + crafts * per - n)
          return true
        end
        rollback(m)
        -- an ingredient is the real blocker (already reported) - don't also list this item
        if P.miss_n > miss_before then return false end
      end
    end
    -- furnace product (plates, bricks): smelt it; one level only (no smelting the input of a smelt)
    local sr = not P.smelting and smelt_recipe(c.force, item)
    if sr then
      local per = product_amount(sr, item)
      local runs = math.ceil(n / per)
      local m, miss_before = mark(), P.miss_n
      if P.smelt(sr, item, runs, (depth or 0) + 1) then
        set(avail, item, avail[item] + runs * per - n)
        return true
      end
      rollback(m)
      if P.miss_n > miss_before then return false end
    end
    if item == "wood" then
      local trees = math.ceil(n / WOOD_PER_TREE)
      add({type = "mine", entity_type = "tree", count = trees, near = center, search_radius = 150},
        "chop " .. trees .. " trees")
      set(avail, item, avail[item] + trees * WOOD_PER_TREE - n)
      return true
    end
    if MINEABLE[item] and P.resource_near(item) then
      add({type = "mine", name = item, count = n, near = center, search_radius = 200}, "mine " .. n .. " " .. item)
      return true
    end
    P.missing[item] = (P.missing[item] or 0) + n
    P.miss_n = P.miss_n + 1
    return false
  end

  local res_near = {}
  function P.resource_near(name)
    if res_near[name] == nil then
      res_near[name] = c.surface.count_entities_filtered{position = center, radius = 200, name = name,
        type = "resource", limit = 1} > 0
    end
    return res_near[name]
  end

  local fuel_item
  local function pick_fuel_item()
    if fuel_item then return fuel_item end
    fuel_item = "wood"
    if c.get_item_count("coal") > 0 or (pinv and pinv.get_item_count("coal") > 0) or P.resource_near("coal") then
      fuel_item = "coal"
    else
      for _, s in ipairs(srcs) do
        if s.e.valid and s.inv.get_item_count("coal") > 0 then fuel_item = "coal"; break end
      end
    end
    return fuel_item
  end

  -- a smelter for `ore`: an existing burner furnace of the force near the build site (idle or already on
  -- this recipe), else a stone furnace the helper crafts and places on free ground
  local function new_smelter(r, ore, item, depth)
    local cat
    for _, k in ipairs(recipe_cats(r)) do if prototypes.entity[SMELTER].crafting_categories[k] then cat = k end end
    local S = {item = item, joules = 0, have_j = 0, fuel = pick_fuel_item()}
    local best, bestd
    for _, e in pairs(c.surface.find_entities_filtered{position = center, radius = SOURCE_RADIUS, type = "furnace",
        force = c.force}) do
      local pr = e.prototype
      if pr.burner_prototype and pr.crafting_categories[cat] and not P.used_f[e.unit_number] then
        local src, out = e.get_inventory(FURNACE_IN), e.get_inventory(FURNACE_OUT)
        if src and out and src.get_item_count(ore) == src.get_item_count()
            and out.get_item_count(item) == out.get_item_count() then
          local d = dist(e.position, center)
          if not best or d < bestd then best, bestd = e, d end
        end
      end
    end
    local proto
    if best then
      proto = best.prototype
      S.x, S.y, S.name = best.position.x, best.position.y, best.name
      local fi = best.get_fuel_inventory()
      for _, it in pairs(fi and fi.get_contents() or {}) do
        local ip = prototypes.item[it.name]
        S.have_j = S.have_j + it.count * (ip and ip.fuel_value or 0)
      end
      S.have_j = S.have_j + (best.burner and best.burner.remaining_burning_fuel or 0)
      set(P.used_f, best.unit_number, true)
    else
      local p = free_furnace_spot(c, center, P.spots)
      if not p then
        P.missing["free ground for a furnace"] = 1
        P.miss_n = P.miss_n + 1
        return nil
      end
      if not P.acquire(SMELTER, 1, depth) then return nil end
      set(P.spots, #P.spots + 1, p)
      add({type = "place", name = SMELTER, x = p.x, y = p.y}, "build a furnace at " .. p.x .. "," .. p.y)
      proto = prototypes.entity[SMELTER]
      S.x, S.y, S.name = p.x, p.y, SMELTER
    end
    S.speed = proto.get_crafting_speed() or 1
    S.watts = furnace_watts(proto)
    S.load = {type = "smelt", load_only = true, x = S.x, y = S.y, name = S.name, ore = ore, item = item,
      ore_count = 0, fuel = S.fuel, fuel_count = 0}
    S.run = {type = "smelt", x = S.x, y = S.y, name = S.name, ore = ore, item = item, count = 0,
      in_per = r.ingredients[1].amount, out_per = product_amount(r, item), fuel = S.fuel}
    add(S.load, nil, 2)
    add(S.run, nil, 4)
    set(P.smelters, ore, S)
    return S
  end

  -- smelt `runs` crafts of furnace recipe r (-> item): smelter, its fuel and the ore, all planned
  function P.smelt(r, item, runs, depth)
    local ore = r.ingredients[1].name
    local prev_phase = P.phase
    P.smelting, P.phase = true, 1
    local S = P.smelters[ore]
    if S and S.item ~= item then S = nil
    elseif not S then S = new_smelter(r, ore, item, depth) end
    local ok = S ~= nil
    if ok then
      local fv = prototypes.item[S.fuel].fuel_value
      local j = S.joules + runs * r.energy / S.speed * S.watts
      local fuel_n = math.max(0, math.ceil((j * 1.1 - S.have_j) / fv))
      ok = P.acquire(S.fuel, fuel_n - S.load.fuel_count, depth) and P.acquire(ore, r.ingredients[1].amount * runs, depth)
      if ok then
        set(S, "joules", j)
        set(S.load, "fuel_count", math.max(fuel_n, S.load.fuel_count))
        set(S.load, "ore_count", S.load.ore_count + r.ingredients[1].amount * runs)
        set(S.run, "count", S.run.count + runs * S.run.out_per)
      end
    end
    P.smelting, P.phase = false, prev_phase
    return ok
  end

  -- rough inventory slots the gathered items take (raw + smelted; crafting roughly nets out). Ore/fuel
  -- for a smelter is fed in chunks, so it holds at most about a stack of it at a time.
  function P.slots()
    local n, fed = {}, {}
    for _, S in pairs(P.smelters) do
      fed[S.load.ore] = (fed[S.load.ore] or 0) + S.load.ore_count
      fed[S.fuel] = (fed[S.fuel] or 0) + S.load.fuel_count
    end
    for _, t in ipairs(P.tasks) do
      local item, k
      if t.type == "mine" then item, k = t.name or "wood", t.name and t.count or t.count * WOOD_PER_TREE
      elseif t.type == "take" or t.type == "take_player" then item, k = t.item, t.count
      elseif t.type == "smelt" and not t.load_only then item, k = t.item, t.count end
      if item then n[item] = (n[item] or 0) + k end
    end
    local s = 0
    for item, k in pairs(n) do
      local ip = prototypes.item[item]
      local stack = ip and ip.stack_size or 50
      local f = math.min(fed[item] or 0, k)
      s = s + math.ceil((k - f + math.min(f, stack)) / stack)
    end
    return s
  end

  -- the plan in run order (see PHASE), one trip per resource/source within a phase, plus a summary line
  -- per smelter
  function P.ordered()
    local out = {}
    for ph = 1, 5 do
      local seen = {}
      for _, t in ipairs(P.tasks) do
        if t.phase == ph then
          local key
          if t.type == "mine" and not t.x then key = "m:" .. (t.name or t.entity_type)
          elseif t.type == "take" then key = "t:" .. t.x .. "," .. t.y .. ":" .. t.item
          elseif t.type == "take_player" then key = "p:" .. t.item end
          if key and seen[key] then
            seen[key].count = seen[key].count + t.count
          else
            if key then seen[key] = t end
            out[#out + 1] = t
          end
        end
      end
    end
    -- start each smelter after the first few ore, then mine the rest and top it up (it works meanwhile)
    for _, S in pairs(P.smelters) do
      local mi, li
      for i, t in ipairs(out) do
        if t.type == "mine" and t.phase == 1 and t.name == S.load.ore and not mi then mi = i end
        if t == S.load then li = i end
      end
      if mi and li and mi < li and out[mi].count > FIRST_LOAD + 4 then
        -- chunks grow 1.5x: hand-mining one ore (~2 s) is faster than smelting it (3.2 s)
        local m = out[mi]
        local left, chunk, at = m.count - FIRST_LOAD, FIRST_LOAD, li
        m.count = FIRST_LOAD
        while left > 0 do
          chunk = math.min(math.floor(chunk * 1.5), 40)
          local k = (left - chunk < 4) and left or math.min(chunk, left)
          left = left - k
          table.insert(out, at + 1, {type = "mine", name = m.name, count = k, near = m.near,
            search_radius = m.search_radius, soft = true, phase = 2})
          table.insert(out, at + 2, {type = "smelt", load_only = true, x = S.x, y = S.y, name = S.name,
            ore = S.load.ore, item = S.item, ore_count = k, fuel = S.fuel, fuel_count = 0, soft = true, phase = 2})
          at = at + 2
        end
      end
    end
    local notes, crafted, mined = {}, {}, {}
    for _, t in ipairs(out) do
      local s
      if t.type == "mine" then
        local key = t.name or "trees"
        if mined[key] then
          mined[key].n = mined[key].n + t.count
          notes[mined[key].i] = t.name and ("mine " .. mined[key].n .. " " .. t.name) or ("chop " .. mined[key].n .. " trees")
        else
          mined[key] = {n = t.count, i = #notes + 1}
          s = t.name and ("mine " .. t.count .. " " .. t.name) or ("chop " .. t.count .. " trees")
        end
      elseif t.type == "take" then s = t.count .. " " .. t.item .. " from " .. t.name
      elseif t.type == "take_player" then s = t.count .. " " .. t.item .. " from you"
      elseif t.type == "place" then s = "build a furnace at " .. t.x .. "," .. t.y
      elseif t.type == "smelt" and not t.load_only then
        s = "smelt " .. t.count .. " " .. t.item .. " in the " .. t.name .. " at " .. round(t.x, 0) .. "," .. round(t.y, 0)
      elseif t.type == "craft" and not crafted[t.recipe] then crafted[t.recipe] = true; s = "craft " .. t.recipe end
      if s then notes[#notes + 1] = s end
    end
    P.summary = notes
    return out
  end

  P.mark, P.rollback = mark, rollback
  return P
end

local function missing_text(missing)
  local parts = {}
  for item, n in pairs(missing) do parts[#parts + 1] = n .. " " .. item end
  table.sort(parts)
  return table.concat(parts, ", ")
end

local function tell(a, msg)
  local p = a.notify and game.get_player(a.notify)
  if p and p.connected then
    p.print({"", "[color=0.85,0.47,0.34][Claude helper][/color] ", util.linkify(msg, a.entity.surface.name)})
  end
end

-- {type="build_ghosts", x?, y?, radius=32, player?}: build every ghost in range. Each round builds what it
-- can carry (mining trees/rocks in the way first), otherwise plans + queues fetching/crafting/mining the
-- materials, then re-queues itself. Stops when no ghosts are left, after 3 rounds without progress (then reports
-- what's missing), or when 2 build rounds in a row leave every ghost standing (something blocks them).
function tasks.build_ghosts(a, t)
  local c = a.entity
  local center = t.x and {x = t.x, y = t.y} or c.position
  local radius = t.radius or 32
  local ghosts = {}
  for _, g in pairs(c.surface.find_entities_filtered{position = center, radius = radius, type = "entity-ghost", force = c.force}) do
    if g.quality.name == "normal" and place_item(g.ghost_name) then ghosts[#ghosts + 1] = g end
  end
  table.sort(ghosts, function(g1, g2) return dist(g1.position, c.position) < dist(g2.position, c.position) end)
  local built = t.built or 0
  if #ghosts == 0 then
    alog(a, built > 0 and ("built " .. built .. " ghosts, none left") or "no ghosts to build")
    return "done"
  end

  -- the last round tried to build some ghosts but none went away: they are blocked
  local blocked = (t.tried and #ghosts >= (t.left or 0)) and (t.blocked or 0) + 1 or 0
  if blocked >= 2 then
    local g = ghosts[1]
    local msg = "Built " .. built .. ". " .. #ghosts .. " ghost" .. (#ghosts == 1 and "" or "s") ..
      " left that I can't place: something is in the way, e.g. the " .. g.ghost_name .. " at [gps=" ..
      round(g.position.x, 0) .. "," .. round(g.position.y, 0) .. "," .. c.surface.name .. "]."
    alog(a, msg)
    tell(a, msg)
    return "done"
  end
  local nxt = {type = "build_ghosts", x = center.x, y = center.y, radius = radius, player = t.player, built = built,
    stall = t.stall or 0, left = #ghosts, blocked = blocked}
  local sub, used, need, rank, cleared = {}, {}, {}, {}, {}
  for i = 1, math.min(#ghosts, ROUND_GHOSTS) do
    local g = ghosts[i]
    local item, n = place_item(g.ghost_name)
    if c.get_item_count(item) - (used[item] or 0) >= n then
      used[item] = (used[item] or 0) + n
      local recipe
      if g.ghost_type == "assembling-machine" then
        local r = g.get_recipe()
        recipe = r and r.name
      end
      -- trees/rocks in its footprint block the build (nothing else removes them on a fresh map)
      local bb = g.bounding_box
      for _, o in pairs(c.surface.find_entities_filtered{area = {{bb.left_top.x + 0.05, bb.left_top.y + 0.05},
          {bb.right_bottom.x - 0.05, bb.right_bottom.y - 0.05}}, type = {"tree", "simple-entity"}}) do
        local key = o.name .. "@" .. o.position.x .. "," .. o.position.y
        if o.valid and o.minable and not cleared[key] then
          cleared[key] = true
          sub[#sub + 1] = {type = "mine", x = o.position.x, y = o.position.y, name = o.name, soft = true}
        end
      end
      sub[#sub + 1] = {type = "place", name = g.ghost_name, x = g.position.x, y = g.position.y,
        direction = g.direction, recipe = recipe, soft = true, ghost_only = true}
    else
      need[item] = (need[item] or 0) + n
      rank[item] = math.min(rank[item] or 9, BUILD_RANK[g.ghost_type] or 1)
    end
  end

  if #sub > 0 then
    local placing = 0
    for _, st in ipairs(sub) do if st.type == "place" then placing = placing + 1 end end
    nxt.built, nxt.stall, nxt.tried = built + placing, 0, placing
    sub[#sub + 1] = nxt
    for i = #sub, 1, -1 do table.insert(a.queue, 1, sub[i]) end
    return "done"
  end

  -- nothing buildable from the inventory: go get materials for this batch
  local player = t.player and game.get_player(t.player)
  if not (settings.global["claude-helper-use-player-items"] and settings.global["claude-helper-use-player-items"].value) then
    player = nil
  end
  local P = new_planner(c, center, player)
  local items = {}
  for item in pairs(need) do items[#items + 1] = item end
  -- scarce materials: production first (drills/furnaces), then the rest; stop when the haul won't fit
  table.sort(items, function(i1, i2)
    if rank[i1] ~= rank[i2] then return rank[i1] < rank[i2] end
    return i1 < i2
  end)
  local free, full = c.get_main_inventory().count_empty_stacks() - 2, false
  for _, item in ipairs(items) do
    local n = need[item]
    while n > 0 do
      local m = P.mark()
      local ok = P.acquire(item, n, 0)
      if not ok or P.slots() <= free or #P.tasks == m[1] then break end
      P.rollback(m)
      n = math.floor(n / 2)
    end
    if n < need[item] then full = n == 0; break end  -- inventory is the limit; the next round takes the rest
  end

  if #P.tasks == 0 and full then
    local msg = "Built " .. built .. ". " .. #ghosts .. " ghosts left, but my inventory is too full to fetch materials."
      .. " Take my items (Give all) and press Build ghosts again."
    alog(a, msg)
    tell(a, msg)
    return "done"
  end

  if #P.tasks == 0 or nxt.stall >= 3 then
    local msg = "Built " .. built .. " ghost" .. (built == 1 and "" or "s") .. ". " .. #ghosts .. " left; can't get "
      .. (next(P.missing) and missing_text(P.missing) or "the materials")
      .. " (not in chests or your inventory, and can't craft, smelt or mine it). Put them in a chest or your inventory and press"
      .. " Build ghosts again."
    alog(a, msg)
    tell(a, msg)
    return "done"
  end
  nxt.stall = nxt.stall + 1
  local plan = P.ordered()
  local note = table.concat(P.summary, ", ")
  if #note > 220 then note = note:sub(1, 217) .. "..." end
  tell(a, "Getting materials: " .. note .. (next(P.missing) and (" (can't get " .. missing_text(P.missing) .. ")") or ""))
  alog(a, "gathering: " .. note)
  plan[#plan + 1] = nxt
  for i = #plan, 1, -1 do table.insert(a.queue, 1, plan[i]) end
  return "done"
end

-- {type="smelt", x, y, name, ore, item, fuel, load_only?, ore_count?, fuel_count?, count?, in_per=1, out_per=1}
-- load_only: put fuel_count fuel + ore_count ore into the furnace and leave (it smelts while the helper
-- does other things). Otherwise: stay, keep it fed from the helper's inventory and take `item` until
-- `count` taken; gives up (keeping what it got) when nothing moves for 10 s.
function tasks.smelt(a, t)
  local c = a.entity
  local e = t.ent
  if not (e and e.valid) then
    e = entity_at(c.surface, {x = t.x, y = t.y, name = t.name})
    if not e then return nil, "no " .. t.name .. " at " .. t.x .. "," .. t.y end
    t.ent = e
  end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then return nil, "can't reach the furnace" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local src, out, fi = e.get_inventory(FURNACE_IN), e.get_inventory(FURNACE_OUT), e.get_fuel_inventory()
  if not (src and out) then return nil, e.name .. " isn't a furnace" end
  local function put(inv, item, n)
    n = math.min(n, c.get_item_count(item))
    if n <= 0 or not inv then return 0 end
    local k = inv.insert{name = item, count = n}
    if k > 0 then c.remove_item{name = item, count = k} end
    return k
  end
  if t.load_only then
    local f = put(fi, t.fuel, t.fuel_count or 0)
    local o = put(src, t.ore, t.ore_count or 0)
    alog(a, "loaded " .. e.name .. ": " .. o .. " " .. t.ore .. ", " .. f .. " " .. t.fuel)
    return "done"
  end
  t.taken = t.taken or 0
  local k = out.get_item_count(t.item)
  if k > 0 then
    local got = c.insert{name = t.item, count = k}
    if got > 0 then out.remove{name = t.item, count = got}; t.taken = t.taken + got end
    if got < k then return nil, "inventory full" end
  end
  if t.taken >= (t.count or 0) then
    alog(a, "smelted " .. t.taken .. " " .. t.item)
    return "done"
  end
  -- keep it fed: ore for what's still missing, fuel when it's about to run dry
  local in_per, out_per = t.in_per or 1, t.out_per or 1
  local want_in = math.ceil((t.count - t.taken) / out_per) * in_per - src.get_item_count(t.ore)
    - (e.is_crafting() and in_per or 0)
  if want_in > 0 then put(src, t.ore, want_in) end
  if fi and fi.is_empty() and (src.get_item_count() > 0 or e.is_crafting()) then
    local fuel = c.get_item_count(t.fuel) > 0 and t.fuel or pick_fuel(c, fi)
    if fuel then put(fi, fuel, 5) end
  end
  local sig = t.taken .. ":" .. src.get_item_count() .. ":" .. round(e.crafting_progress, 3)
  if sig ~= t.sig then t.sig, t.since = sig, game.tick end
  if game.tick - t.since > 600 then
    if t.taken > 0 then alog(a, "smelting stalled, took " .. t.taken .. " " .. t.item); return "done" end
    return nil, "smelting stalled (no ore or fuel?)"
  end
  return "running"
end

-- {type="take_player", player, item, count}: take items out of a player's inventory (to build their ghosts)
function tasks.take_player(a, t)
  local c = a.entity
  local p = get_player(t)
  if not (p and p.connected and p.character) then return nil, "player not available" end
  local near = ensure_near(a, t, p.position, c.reach_distance - 1)
  if near == nil then return nil, "can't reach player" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local inv = p.get_main_inventory()
  local n = math.min(t.count or 0, inv.get_item_count(t.item))
  if n > 0 then
    local got = c.insert{name = t.item, count = n}
    if got > 0 then inv.remove{name = t.item, count = got} end
    alog(a, "took " .. got .. " " .. t.item .. " from " .. p.name)
  end
  return "done"
end

-- {type="insert", x, y, item, count?, name?}
function tasks.insert(a, t)
  local c = a.entity
  local e = entity_at(c.surface, {x = t.x, y = t.y, name = t.name})
  if not e then return nil, "nothing at " .. t.x .. "," .. t.y end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then return nil, "can't reach" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local have = c.get_item_count(t.item)
  local want = math.min(t.count or have, have)
  if want <= 0 then return nil, "no " .. t.item end
  local n = e.insert{name = t.item, count = want}
  if n > 0 then c.remove_item{name = t.item, count = n} end
  alog(a, "inserted " .. n .. " " .. t.item .. " into " .. e.name)
  return "done"
end

-- {type="take", x, y, item?, count?, name?}
function tasks.take(a, t)
  local c = a.entity
  local e = entity_at(c.surface, {x = t.x, y = t.y, name = t.name})
  if not e then return nil, "nothing at " .. t.x .. "," .. t.y end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then return nil, "can't reach" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local inv = target_inventory(e, true)
  if not inv then return nil, e.name .. " has no output inventory" end
  local moved, limit = 0, t.count or math.huge
  for i = 1, #inv do
    local st = inv[i]
    if st.valid_for_read and (not t.item or st.name == t.item) and moved < limit then
      local n = math.min(st.count, limit - moved)
      local got = c.insert{name = st.name, count = n, quality = st.quality}
      if got > 0 then st.count = st.count - got; moved = moved + got end
    end
  end
  alog(a, "took " .. moved .. " " .. (t.item or "items") .. " from " .. e.name)
  return "done"
end

-- {type="give_player", player?, item, count?}
function tasks.give_player(a, t)
  local c = a.entity
  local p = get_player(t)
  if not (p and p.character) then return nil, "player has no character" end
  local near = ensure_near(a, t, p.position, c.reach_distance - 1)
  if near == nil then return nil, "can't reach player" end
  if not near then return "running" end
  local have = c.get_item_count(t.item)
  local n = p.insert{name = t.item, count = math.min(t.count or have, have)}
  if n > 0 then c.remove_item{name = t.item, count = n} end
  alog(a, "gave " .. n .. " " .. t.item .. " to " .. p.name)
  return "done"
end

-- {type="recipe", x, y, recipe}
function tasks.recipe(a, t)
  local c = a.entity
  local e = entity_at(c.surface, {x = t.x, y = t.y})
  if not e then return nil, "nothing there" end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then return nil, "can't reach" end
  if not near then return "running" end
  local overflow = e.set_recipe(t.recipe)
  for _, it in pairs(overflow or {}) do c.insert(it) end
  return "done"
end

-- {type="follow", player?, distance=4, ticks?}: tag along behind a player (ticks=nil = until stopped)
function tasks.follow(a, t)
  local c = a.entity
  local p = get_player(t)
  if not p then return nil, "no player" end
  if t.ticks then
    t.end_tick = t.end_tick or (game.tick + t.ticks)
    if game.tick >= t.end_tick then c.walking_state = {walking = false}; return "done" end
  end
  if p.surface ~= c.surface then c.walking_state = {walking = false}; return "running" end
  local d = dist(c.position, p.position)
  if d > (t.distance or 4) then
    c.walking_state = {walking = true, direction = dir_toward(c.position, p.position)}
  else
    c.walking_state = {walking = false}
  end
  return "running"
end

local burner_types = {"furnace", "mining-drill", "inserter", "boiler", "assembling-machine", "burner-generator",
  "reactor", "lab", "car", "locomotive"}

local MAX_FETCH_FUEL = 300  -- coal the helper gathers for one refuel run when it has none

-- {type="refuel", x?, y?, radius=32, per=10, types?, at?, player?}: top up burner entities' fuel from the helper's
-- inventory, nearest first (at = {{x, y}, ...}: only the entities at these positions). With no fuel at all it first gathers coal (chests, the player's inventory when the
-- setting allows, or mining) and then runs again.
function tasks.refuel(a, t)
  local c = a.entity
  local per = t.per or 10
  if not t.targets then
    local center = t.x and {x = t.x, y = t.y} or c.position
    t.targets = {}
    local found = {}
    if t.at then
      for _, p in ipairs(t.at) do
        local e = c.surface.find_entities_filtered{position = {x = p[1], y = p[2]}, force = c.force,
          type = burner_types, limit = 1}[1]
        if e then found[#found + 1] = e end
      end
    else
      found = c.surface.find_entities_filtered{position = center, radius = t.radius or 32,
        type = t.types or burner_types, force = c.force}
    end
    for _, e in pairs(found) do
      local fi = e.burner and e.get_fuel_inventory()
      if fi and fi.get_item_count() < per then t.targets[#t.targets + 1] = e end
    end
    t.fueled = 0
    if #t.targets == 0 then alog(a, "refuel: nothing needs fuel"); return "done" end
  end
  -- nearest remaining target
  local e, bi, bd
  for i, x in ipairs(t.targets) do
    if x.valid then
      local d = dist(x.position, c.position)
      if not bd or d < bd then e, bi, bd = x, i, d end
    end
  end
  if not e then alog(a, "refuel: fueled " .. t.fueled); return "done" end
  local fi = e.get_fuel_inventory()
  local fuel = pick_fuel(c, fi)
  if not fuel then
    if t.fueled > 0 then alog(a, "refuel: out of fuel after " .. t.fueled); return "done" end
    if t.fetched then return nil, "no fuel in inventory" end
    -- gather coal for the rest, then refuel again
    local need = 0
    for _, x in ipairs(t.targets) do
      if x.valid then need = need + math.max(0, per - x.get_fuel_inventory().get_item_count()) end
    end
    local player = t.player and game.get_player(t.player)
    if not (settings.global["claude-helper-use-player-items"] and settings.global["claude-helper-use-player-items"].value) then
      player = nil
    end
    local P = new_planner(c, c.position, player)
    if not P.acquire("coal", math.min(need, MAX_FETCH_FUEL), 0) or #P.tasks == 0 then
      return nil, "no fuel in inventory (and no coal to fetch or mine nearby)"
    end
    local plan = P.ordered()
    tell(a, "Getting coal to refuel: " .. table.concat(P.summary, ", "))
    plan[#plan + 1] = {type = "refuel", x = t.x, y = t.y, radius = t.radius, per = per, types = t.types,
      at = t.at, player = t.player, fetched = true}
    for i = #plan, 1, -1 do table.insert(a.queue, 1, plan[i]) end
    return "done"
  end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then table.remove(t.targets, bi); t.approach = nil; return "running" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local want = math.min(per - fi.get_item_count(), c.get_item_count(fuel))
  local n = want > 0 and fi.insert{name = fuel, count = want} or 0
  if n > 0 then c.remove_item{name = fuel, count = n}; t.fueled = t.fueled + 1 end
  table.remove(t.targets, bi)
  t.approach = nil
  return "running"
end

-- {type="give_all", player?}: walk to the player and hand over everything
function tasks.give_all(a, t)
  local c = a.entity
  local p = get_player(t)
  if not (p and p.character) then return nil, "player has no character" end
  local near = ensure_near(a, t, p.position, c.reach_distance - 1)
  if near == nil then return nil, "can't reach player" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local inv = c.get_main_inventory()
  local moved = 0
  for i = 1, #inv do
    local st = inv[i]
    if st.valid_for_read then
      local n = p.insert(st)
      if n > 0 then moved = moved + n; if n >= st.count then st.clear() else st.count = st.count - n end end
    end
  end
  alog(a, "gave " .. moved .. " items to " .. p.name)
  if not inv.is_empty() then return nil, "player inventory full" end
  return "done"
end

api.agent_tasks = tasks

-- one-line feedback to whoever issued the last quick action (free)
local function notify(a, msg)
  local p = a.notify and game.get_player(a.notify)
  if p and p.connected then
    p.print({"", "[color=0.85,0.47,0.34][Claude helper][/color] ", util.linkify(msg, a.entity.valid and a.entity.surface.name)})
  end
end

-- queue finished: "Done: <what>" plus the last interesting log line since the quick action
local function notify_done(a)
  local what = a.notify_what
  a.notify_what = nil
  if not what then return end
  local extra
  local since = (a.log_n or 0) - (a.notify_from or 0)
  for i = #a.log, math.max(1, #a.log - since + 1), -1 do
    if not a.log[i].msg:find("^done: ") then extra = a.log[i].msg; break end
  end
  notify(a, "Done: " .. what .. (extra and (" (" .. extra .. ")") or "") .. ".")
end

local function tick()
  local a = storage.agent
  if not a then return end
  if not (a.entity and a.entity.valid) then
    if not a.dead_reported then game.print("[Claude] my character died or was removed."); a.dead_reported = true end
    return
  end
  if a.halted then return end
  if not a.cur then
    if #a.queue == 0 then return end
    a.cur = table.remove(a.queue, 1)
  end
  local t = a.cur
  local handler = tasks[t.type]
  local ok, res, err
  if not handler then
    ok, res, err = true, nil, "unknown task type " .. tostring(t.type)
  else
    ok, res, err = pcall(handler, a, t)
    if not ok then err = res; res = nil end
  end
  if res == "running" then return end
  if res == "done" then
    if a.cur == t then
      if not t.auto then alog(a, "done: " .. t.type .. (t.name and (" " .. t.name) or "")) end
      a.cur = nil
      if #a.queue == 0 then notify_done(a) end
    end
    return
  end
  a.entity.walking_state = {walking = false}
  a.path_state = nil
  -- soft steps (ghost building / material gathering) just get skipped; the next build round re-plans
  if t.soft then
    alog(a, "skipped " .. t.type .. ": " .. tostring(err))
    if a.cur == t then a.cur = nil end
    if t.auto and a.queue[1] and a.queue[1].soft then table.remove(a.queue, 1) end
    return
  end
  -- failure: halt the queue so Claude can re-plan
  a.halted = "failed " .. t.type .. ": " .. tostring(err)
  alog(a, a.halted)
  a.cur = nil
  if t.auto and a.queue[1] then table.remove(a.queue, 1) end
  a.notify_what = nil
  notify(a, "Stopped: " .. tostring(err))
end

events.on(defines.events.on_tick, tick)

---------------------------------------------------------------------------
-- quick actions (free; GUI buttons / chat keywords)
---------------------------------------------------------------------------

local ores = {stone = "stone", iron = "iron-ore", copper = "copper-ore", coal = "coal"}

-- One-line description of what the helper is doing (for GUIs/chat).
function agent.describe()
  local a = get()
  if not a then return "No helper." end
  local p = a.entity.position
  local s = "Helper at " .. round(p.x, 0) .. "," .. round(p.y, 0)
  if a.halted then return s .. " - " .. a.halted end
  if a.cur then return s .. " - " .. a.cur.type .. (#a.queue > 0 and (" (+" .. #a.queue .. " queued)") or "") end
  return s .. " - idle"
end

local needs_player = {give_all = true, take_hand = true}

-- Helper builds everything in bbox {x0, y0, x1, y1} (spawning it if needed), then fills the hand-fed burners
-- at positions `fuel_at` ({{x, y}, ...}: drills / inserters / boilers) with 50 coal. An endless follow (or an
-- idle/halted queue) is replaced; other work stays first. what = "build" | "refuel". -> one-line message.
function agent.work_area(player, bb, what, label, fuel_at)
  local cx, cy = (bb[1] + bb[3]) / 2, (bb[2] + bb[4]) / 2
  local radius = math.ceil(math.sqrt((bb[3] - bb[1]) ^ 2 + (bb[4] - bb[2]) ^ 2) / 2) + 4
  local pre = ""
  if not get() then
    local ok, msg = agent.spawn(player, {x = cx, y = cy})
    if not ok then return msg end
    pre = "Helper spawned. "
  end
  local a = get()
  local pi = player and player.index
  if pi then a.notify = pi end
  local list = {}
  if what == "build" then
    list[#list + 1] = {type = "build_ghosts", x = cx, y = cy, radius = radius, player = pi}
  end
  if fuel_at and #fuel_at > 0 then
    list[#list + 1] = {type = "refuel", x = cx, y = cy, radius = radius, per = 50, player = pi, at = fuel_at}
  elseif what == "refuel" then
    return pre .. "Nothing in this plan is fuelled by hand."
  end
  -- an endless follow never finishes: drop it; a halted queue is stale: replace it
  local busy = a.cur and a.cur.type ~= "follow" and not a.halted
  local queue = {}
  for _, q in ipairs(a.queue) do if q.type ~= "follow" then queue[#queue + 1] = q end end
  if not a.halted and (busy or #queue > 0) then
    if a.cur and a.cur.type == "follow" then
      a.cur = nil
      a.entity.walking_state = {walking = false}
    end
    a.queue = queue
    agent.queue(list, false)
  else
    agent.queue(list, true)
  end
  a.notify_what, a.notify_from = what == "build" and ("building " .. (label or "the plan")) or "refueling",
    a.log_n or 0
  if what == "build" then
    return pre .. "The helper is building " .. (label or "it") .. ", fetching or crafting what it needs" ..
      (busy and " after its current task" or "") ..
      ((fuel_at and #fuel_at > 0) and ", then fills the hand-fed burners with coal." or ".")
  end
  return pre .. "The helper is topping up the hand-fed drills and inserters with coal" ..
    (busy and " after its current task" or "") .. "."
end

-- Free quick action (GUI buttons, chat keywords, quick_action op) -> one-line reply.
-- "The player" falls back to opts.position, then the helper's own position, when player is nil (headless).
function agent.quick(player, action, opts)
  opts = opts or {}
  if action == "dismiss" then return agent.dismiss() end
  if action == "stop" then
    if not get() then return "No helper." end
    agent.stop()
    return "Helper stopped."
  end
  local known = action == "spawn" or action == "follow" or action == "come" or action == "wood" or ores[action]
    or action == "build_ghosts" or action == "refuel" or needs_player[action]
  if not known then return "Unknown helper action: " .. tostring(action) end
  if needs_player[action] and not (player and player.character) then return "That needs your character in the game." end
  local spawned = false
  if not get() then
    local ok, msg = agent.spawn(player, opts.position)
    if not ok then return msg end
    spawned = true
    if action == "spawn" then return msg end
  end
  local a = get()
  if player then a.notify = player.index end
  local pos = player and player.position or opts.position or a.entity.position
  pos = {x = pos.x, y = pos.y}
  local pre = spawned and "Helper spawned. " or ""
  local function q(tasks, msg, what)
    agent.queue(tasks, true)
    a.notify_what, a.notify_from = what, a.log_n or 0
    return pre .. msg
  end
  if action == "spawn" then
    return "Helper is already here."
  elseif action == "follow" then
    if player then return q({{type = "follow", player = player.index}}, "Helper following you.") end
    if not opts.position then return pre .. "No player to follow." end
    return q({{type = "walk", x = pos.x, y = pos.y, radius = 3}}, "No player to follow; helper walking to " ..
      round(pos.x, 0) .. "," .. round(pos.y, 0) .. ".", "walking there")
  elseif action == "come" then
    return q({{type = "walk", x = pos.x, y = pos.y, radius = 3}}, "Helper coming to you.", "arrived")
  elseif action == "wood" then
    local n = opts.count or 10
    return q({{type = "mine", entity_type = "tree", count = n, near = pos, search_radius = 150}},
      "Helper chopping " .. n .. " trees.", "chopping trees")
  elseif ores[action] then
    local n = opts.count or 50
    return q({{type = "mine", name = ores[action], count = n, near = pos, search_radius = 200}},
      "Helper mining " .. n .. " " .. ores[action] .. ".", "mining " .. ores[action])
  elseif action == "build_ghosts" then
    return q({{type = "build_ghosts", x = pos.x, y = pos.y, radius = 32, player = player and player.index}},
      "Helper building the ghosts near you (it fetches/crafts what it needs).", "building ghosts")
  elseif action == "refuel" then
    return q({{type = "refuel", x = pos.x, y = pos.y, radius = 32}}, "Helper refueling burners near you.", "refueling")
  elseif action == "give_all" then
    return q({{type = "give_all", player = player.index}}, "Helper bringing you its items.", "handed over")
  elseif action == "take_hand" then
    local cs = player.cursor_stack
    if not (cs and cs.valid_for_read) then return pre .. "Nothing in your hand." end
    local name, n = cs.name, a.entity.insert(cs)
    if n == 0 then return pre .. "Helper's inventory is full." end
    if n >= cs.count then cs.clear() else cs.count = cs.count - n end
    return pre .. "Gave the helper " .. n .. " " .. name .. "."
  end
end

return agent
