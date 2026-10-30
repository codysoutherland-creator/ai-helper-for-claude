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

local LEG = 28          -- a long walk whose path request fails goes in legs of about this many tiles
local MAX_LEGS = 40

-- a reachable spot about LEG tiles from `from` toward `to` (straight on, else turning up to ~70 degrees), or nil
local function leg_toward(c, from, to)
  local d = dist(from, to)
  local ux, uy = (to.x - from.x) / d, (to.y - from.y) / d
  for _, ang in ipairs({0, 0.6, -0.6, 1.2, -1.2}) do
    local vx, vy = ux * math.cos(ang) - uy * math.sin(ang), ux * math.sin(ang) + uy * math.cos(ang)
    local p = {x = from.x + vx * LEG, y = from.y + vy * LEG}
    local spot = c.surface.find_non_colliding_position("character", p, 8, 0.5)
    if spot and dist(spot, from) > LEG / 2 then return spot end
  end
  return nil
end

-- returns "done", "running" or nil,err
local function step_walk(a, t)
  local c = a.entity
  local final = {x = t.x, y = t.y}
  local radius = t.radius or 0.6
  if dist(c.position, final) <= radius then
    c.walking_state = {walking = false}
    a.path_state = nil
    return "done"
  end
  -- on the way to an intermediate leg point (the direct path request failed)
  if t.via and dist(c.position, t.via) <= 2 then
    t.via, t.last_pos, a.path_state = nil, nil, nil
    return "running"
  end
  local goal = t.via or final
  if t.via then radius = 2 end
  if not a.path_state or not t.last_pos then  -- new walk (also guards a stale path from a replaced task)
    t.start_pos = t.start_pos or {x = c.position.x, y = c.position.y}
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
  if a.path_state == "failed" and dist(c.position, final) > LEG * 1.5 and (t.legs or 0) < MAX_LEGS then
    -- no path found for a long walk (the pathfinder gives up on long detours, e.g. around a lake): go in legs
    local spot = leg_toward(c, c.position, final)
    if spot then
      t.legs = (t.legs or 0) + 1
      t.via, t.last_pos, a.path_state = spot, nil, nil
      return "running"
    end
  end
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
      if t.stuck_checks >= 2 and not t.unstuck and t.start_pos and dist(c.position, t.start_pos) < 1 then
        -- hasn't moved at all and no path leads out: boxed in by entities it built (or standing inside one). A
        -- player would pick something up; the helper is moved to a free spot a few tiles toward its goal (once per
        -- walk, logged)
        t.unstuck = true
        pcall(function()
          local d = math.max(dist(c.position, goal), 0.01)
          local ux, uy = (goal.x - c.position.x) / d, (goal.y - c.position.y) / d
          for _, k in ipairs({4, 7, 10}) do
            local spot = c.surface.find_non_colliding_position("character",
              {x = c.position.x + ux * k, y = c.position.y + uy * k}, 3, 0.5)
            if spot and dist(spot, c.position) >= 2 then
              c.teleport(spot)
              alog(a, "got out of a dead end at " .. round(t.start_pos.x, 0) .. "," .. round(t.start_pos.y, 0))
              t.stuck_checks = 0
              t.last_pos = c.position
              request_path(a, goal, radius)
              break
            end
          end
        end)
      end
      if t.stuck_checks >= 4 then
        c.walking_state = {walking = false}
        a.path_state = nil
        if t.via and (t.legs or 0) < MAX_LEGS then  -- that leg point can't be reached: try another one
          t.via, t.last_pos, t.stuck_checks = nil, nil, 0
          t.legs = (t.legs or 0) + 1
          return "running"
        end
        return nil, "stuck walking to " .. round(final.x) .. "," .. round(final.y)
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
      local function unreachable(p)  -- within 8 tiles of a tile the helper couldn't walk to
        for _, s in ipairs(t.skip_at or {}) do
          if math.abs(s.x - p.x) <= 8 and math.abs(s.y - p.y) <= 8 then return true end
        end
        return false
      end
      for _, e in pairs(c.surface.find_entities_filtered(f)) do
        if e.minable and e.prototype.mineable_properties.minable and e.type ~= "character"
            and not (t.skip and t.skip[e.position.x .. "," .. e.position.y]) and not unreachable(e.position) then
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
          local st = a.stats
          if st and st.mined then st.mined[prod.name] = (st.mined[prod.name] or 0) + amt end
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
  if t.started and c.crafting_queue_size > 0 and c.get_main_inventory().count_empty_stacks() == 0 then
    -- a hand craft whose product does not fit waits for ever: give up (the caller stashes, then plans again)
    t.full_since = t.full_since or game.tick
    if game.tick - t.full_since > 600 then
      c.cancel_crafting{index = 1, count = c.crafting_queue[1] and c.crafting_queue[1].count or 1}
      return nil, "inventory full while crafting " .. t.recipe
    end
  else
    t.full_since = nil
  end
  if not t.started then
    local n = c.begin_crafting{recipe = t.recipe, count = t.count or 1, silent = true}
    if n == 0 then return nil, "can't craft " .. t.recipe .. " (missing ingredients or not unlocked)" end
    t.started = n
    return "running"
  end
  if c.crafting_queue_size == 0 then return "done" end
  return "running"
end

-- a spot just outside box bb (a tile box) where the character fits, nearest first; nil if none
function agent.free_spot_outside(c, bb)
  local p = c.position
  local cands = {}
  for _, d in ipairs({1.2, 2.2, 3.2}) do
    cands[#cands + 1] = {x = bb.left_top.x - d, y = p.y}
    cands[#cands + 1] = {x = bb.right_bottom.x + d, y = p.y}
    cands[#cands + 1] = {x = p.x, y = bb.left_top.y - d}
    cands[#cands + 1] = {x = p.x, y = bb.right_bottom.y + d}
  end
  for _, q in ipairs(cands) do
    if c.surface.can_place_entity{name = "character", position = q} and
        c.surface.count_entities_filtered{type = "entity-ghost", position = q, radius = 0.45} == 0 then
      return q
    end
  end
  return nil
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
    -- standing on the ghost itself? step off first (a revive there would build the entity around the character
    -- and leave it stuck inside)
    local bb, p = ghost.bounding_box, c.position
    if not t.stepped and p.x > bb.left_top.x - 0.45 and p.x < bb.right_bottom.x + 0.45 and
        p.y > bb.left_top.y - 0.45 and p.y < bb.right_bottom.y + 0.45 then
      t.stepped = true
      local spot = agent.free_spot_outside(c, bb)
      if spot then
        table.insert(a.queue, 1, t)
        a.cur = {type = "walk", x = spot.x, y = spot.y, radius = 0.3, auto = true, soft = true}
        a.path_state = nil
        return "running"
      end
    end
    c.remove_item{name = item, count = 1}
    local _, e = ghost.revive{raise_revive = true}
    if not e then
      c.insert{name = item, count = 1}
      return nil, "ghost revive failed (blocked?)"
    end
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
  -- start fuel (planner v2 plans: a few coal into each new burner when the helper carries some, so the first
  -- machines run while it builds the rest; the plan's round tops them up afterwards)
  -- (coal only: a slot holding wood would refuse the round's coal until the wood has burnt)
  if t.fuel and t.fuel > 0 and built.valid and built.burner and c.get_item_count("coal") > 0 then
    pcall(function()
      local fi = built.get_fuel_inventory()
      if fi and fi.can_insert{name = "coal", count = 1} then
        local n = fi.insert{name = "coal", count = math.min(t.fuel, c.get_item_count("coal"))}
        if n > 0 then c.remove_item{name = "coal", count = n} end
      end
    end)
  end
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
agent.pick_fuel = pick_fuel

-- ---------- build ghosts, fetching/crafting the materials ----------

local ROUND_GHOSTS = 40    -- ghosts handled per round (bounds how much the helper carries at once)
local SOURCE_RADIUS = 150  -- how far from the build site to look for chests / furnace output
local WOOD_PER_TREE = 4
local MINEABLE = {stone = true, coal = true, ["iron-ore"] = true, ["copper-ore"] = true}
local MINE_RADIUS = 400   -- how far from the build site the material planner looks for ore to hand-mine (planner
                          -- v2 plans span far-apart patches; agenda.MINE_RADIUS)
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

-- a plan's next stage keeps its ground (planner v2 `reserve` boxes): a 2x2 furnace centred at p (+1 tile) touches one?
local function plan_reserved(p)
  for _, pl in pairs(storage.plans or {}) do
    for _, b in ipairs(pl.reserve or {}) do
      if p.x + 2 > b[1] and p.x - 2 < b[3] and p.y + 2 > b[2] and p.y - 2 < b[4] then return true end
    end
  end
  return false
end

-- a tile any plan (proposed, waiting or applied) puts an entity on, with a 1-tile margin
local function plan_tile(x, y)
  for _, pl in pairs(storage.plans or {}) do
    local bb = pl.bbox
    if bb and x >= bb[1] - 1 and x <= bb[3] + 1 and y >= bb[2] - 1 and y <= bb[4] + 1 then
      for _, e in ipairs(pl.entities or {}) do
        local proto = prototypes.entity[e[1]]
        local w = proto and proto.tile_width or 1
        local h = proto and proto.tile_height or 1
        if math.abs(e[2] - x) < w / 2 + 1.5 and math.abs(e[3] - y) < h / 2 + 1.5 then return true end
      end
    end
  end
  return false
end

-- A storage chest for the salvage the helper carries when its inventory is too full to fetch materials: placed near
-- it on free ground that no plan uses (no ore, no ghost, nothing planned or reserved within a tile), then everything
-- it carries that the remaining ghosts don't need (salvaged machines, ore, ...) goes in. -> task list or nil
local KEEP = {coal = true, wood = true, stone = true, ["iron-plate"] = true, ["copper-plate"] = true,
  ["iron-gear-wheel"] = true, ["electronic-circuit"] = true, ["copper-cable"] = true, ["steel-plate"] = true}
-- what of a KEEP item stays in the helper's inventory when it stashes (round 3 live: it took apart 17 plate chests
-- full of plates at S5 and stopped, its inventory full of plates, crafting the last 12 belts forever)
local KEEP_MAX = {["iron-plate"] = 600, ["copper-plate"] = 400, coal = 200, stone = 200, wood = 100}
local STASH_CHESTS = 4
local function stash_plan(a, need)
  local c = a.entity
  local chest
  for _, n in ipairs({"iron-chest", "wooden-chest"}) do
    if c.get_item_count(n) > 0 then chest = n; break end
  end
  local craft
  if not chest then
    for _, n in ipairs({"iron-chest", "wooden-chest"}) do
      local r = c.force.recipes[n]
      if r and r.enabled then
        local ok = true
        for _, ing in pairs(r.ingredients) do
          if c.get_item_count(ing.name) < ing.amount then ok = false end
        end
        if ok then chest, craft = n, n; break end
      end
    end
  end
  if not chest then return nil end
  local s = c.surface
  local cx, cy = math.floor(c.position.x), math.floor(c.position.y)
  local spot
  for r = 2, 30 do
    for i = -r, r do
      for _, d in ipairs{{i, -r}, {i, r}, {-r, i}, {r, i}} do
        local p = {x = cx + d[1] + 0.5, y = cy + d[2] + 0.5}
        if s.can_place_entity{name = chest, position = p, force = c.force,
              build_check_type = defines.build_check_type.manual}
            and s.count_entities_filtered{area = {{p.x - 1.5, p.y - 1.5}, {p.x + 1.5, p.y + 1.5}},
              type = {"entity-ghost", "resource", "character"}, limit = 1} == 0
            and not plan_reserved(p) and not plan_tile(p.x, p.y) then
          spot = p
          break
        end
      end
      if spot then break end
    end
    if spot then break end
  end
  if not spot then return nil end
  local list = {}
  local inv = c.get_main_inventory()
  -- what goes in: everything the remaining ghosts don't need, and of the materials it keeps the part above KEEP_MAX
  local store, stacks = {}, 0
  for _, it in pairs(inv.get_contents()) do
    local n = it.name
    if not store[n] and not need[n] and n ~= chest then
      local have = c.get_item_count(n)
      local keep = KEEP[n] and (KEEP_MAX[n] or 200) or 0
      if have > keep then
        store[n] = keep
        stacks = stacks + math.ceil((have - keep) / prototypes.item[n].stack_size)
      end
    end
  end
  if stacks == 0 then return nil end
  local size = (prototypes.entity[chest].get_inventory_size(defines.inventory.chest)) or 16
  local n_chests = math.min(STASH_CHESTS, math.ceil(stacks / size))
  if not craft then n_chests = math.min(n_chests, c.get_item_count(chest)) end
  local spots = {spot}
  for _ = 2, n_chests do      -- the next chests in a row next to the first (free, unplanned tiles only)
    local last = spots[#spots]
    local q = {x = last.x + 1, y = last.y}
    if not (s.can_place_entity{name = chest, position = q, force = c.force,
              build_check_type = defines.build_check_type.manual} and not plan_reserved(q) and
            not plan_tile(q.x, q.y)) then break end
    spots[#spots + 1] = q
  end
  for _, sp in ipairs(spots) do
    if craft then list[#list + 1] = {type = "craft", recipe = craft, count = 1} end
    list[#list + 1] = {type = "place", name = chest, x = sp.x, y = sp.y}
    for n, keep in pairs(store) do
      list[#list + 1] = {type = "insert", x = sp.x, y = sp.y, name = chest, item = n, keep = keep, soft = true}
    end
  end
  alog(a, "inventory full: storing salvage in " .. #spots .. " " .. chest .. " at " .. spot.x .. "," .. spot.y)
  return list
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
        if ok and plan_reserved(p) then ok = false end
        -- nor on anything a plan (also a later, waiting stage) will build there: the helper's own furnace once stood
        -- on the next stage's coal belt and that stage could not be placed
        if ok and plan_tile(p.x, p.y) then ok = false end
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
-- extra: {{x, y}, ...} chests that come first whatever their distance (a plan's coal chests for its fuel round).
local function new_planner(c, center, player, extra)
  local P = {tasks = {}, missing = {}, undo = {}, summary = {}, miss_n = 0, smelters = {}, spots = {}, used_f = {}}
  local avail, srcs, first, seen = {}, {}, {}, {}
  for _, p in ipairs(extra or {}) do
    local e = c.surface.find_entities_filtered{position = {x = p[1], y = p[2]}, radius = 0.6, force = c.force,
      type = {"container", "logistic-container"}, limit = 1}[1]
    local inv = e and target_inventory(e, true)
    if inv and e.unit_number and not seen[e.unit_number] then
      seen[e.unit_number] = true
      first[#first + 1] = {e = e, inv = inv, left = {}}
    end
  end
  for _, e in pairs(c.surface.find_entities_filtered{position = center, radius = SOURCE_RADIUS,
      type = {"container", "logistic-container", "furnace"}, force = c.force}) do
    local inv = target_inventory(e, true)
    if inv and not inv.is_empty() and not (e.unit_number and seen[e.unit_number]) then
      srcs[#srcs + 1] = {e = e, inv = inv, left = {}}
    end
  end
  table.sort(srcs, function(s1, s2) return dist(s1.e.position, c.position) < dist(s2.e.position, c.position) end)
  if #first > 0 then
    table.sort(first, function(s1, s2) return dist(s1.e.position, c.position) < dist(s2.e.position, c.position) end)
    for _, s in ipairs(srcs) do first[#first + 1] = s end
    srcs = first
  end
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
    if MINEABLE[item] and not P.no_mine and P.resource_near(item) then
      add({type = "mine", name = item, count = n, near = center, search_radius = MINE_RADIUS}, "mine " .. n .. " " .. item)
      return true
    end
    P.missing[item] = (P.missing[item] or 0) + n
    P.miss_n = P.miss_n + 1
    return false
  end

  local res_near = {}
  function P.resource_near(name)
    if res_near[name] == nil then
      res_near[name] = c.surface.count_entities_filtered{position = center, radius = MINE_RADIUS, name = name,
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

  -- roughly the nearest tile of resource `name` to the build site (first hit of growing search radii), or nil
  local function ore_near(name)
    for _, r in ipairs({16, 32, 64, 128, 256, MINE_RADIUS}) do
      local hit = c.surface.find_entities_filtered{position = center, radius = r, name = name, type = "resource",
        limit = 1}[1]
      if hit then return hit.position end
    end
    return nil
  end

  -- a smelter for `ore`: an existing burner furnace of the force next to the ore it mines (idle or already on this
  -- recipe), else a stone furnace the helper crafts and places on free ground there
  local function new_smelter(r, ore, item, depth)
    local cat
    for _, k in ipairs(recipe_cats(r)) do if prototypes.entity[SMELTER].crafting_categories[k] then cat = k end end
    local S = {item = item, joules = 0, have_j = 0, fuel = pick_fuel_item()}
    local best, bestd
    -- smelt next to the ore it mines (a plan's sites can be 200 tiles apart: no walking the ore to the build centre)
    local site = (MINEABLE[ore] and ore_near(ore)) or center
    local look = site ~= center and 24 or SOURCE_RADIUS
    for _, e in pairs(c.surface.find_entities_filtered{position = site, radius = look, type = "furnace",
        force = c.force}) do
      local pr = e.prototype
      if pr.burner_prototype and pr.crafting_categories[cat] and not P.used_f[e.unit_number] then
        local src, out = e.get_inventory(FURNACE_IN), e.get_inventory(FURNACE_OUT)
        if src and out and src.get_item_count(ore) == src.get_item_count() and not e.is_crafting()  -- not a running pair
            and out.get_item_count(item) == out.get_item_count() then
          local d = dist(e.position, site)
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
      local p = free_furnace_spot(c, site, P.spots) or (site ~= center and free_furnace_spot(c, center, P.spots))
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

-- start fuel the helper puts into a new burner of an approved plan (when it carries fuel)
local START_FUEL = {["mining-drill"] = 8, furnace = 5, inserter = 1, boiler = 5}  -- ~3.5 min of work

-- a ghost of plan `plan` (tags.claude_plan); no plan = any ghost
local function ghost_of_plan(g, plan)
  if not plan then return true end
  local ok, tags = pcall(function() return g.tags end)
  return ok and type(tags) == "table" and tags.claude_plan == plan
end
agent.ghost_of_plan = ghost_of_plan

-- {type="build_ghosts", x?, y?, radius=32, player?, plan?, fuel?}: build every ghost in range (plan = only the ghosts
-- tagged with that plan id: an approved plan never builds the player's own ghosts or another plan's). Each round
-- builds what it can carry (mining trees/rocks in the way first), otherwise plans + queues fetching/crafting/mining
-- the materials, then re-queues itself. fuel = put a few coal into each new burner when the helper carries some.
-- Stops when no ghosts are left, after 3 rounds without progress (then reports what's missing), or when 2 build
-- rounds in a row leave every ghost standing (something blocks them).
function tasks.build_ghosts(a, t)
  local c = a.entity
  local center = t.x and {x = t.x, y = t.y} or c.position
  local radius = t.radius or 32
  local ghosts = {}
  for _, g in pairs(c.surface.find_entities_filtered{position = center, radius = radius, type = "entity-ghost", force = c.force}) do
    if g.quality.name == "normal" and place_item(g.ghost_name) and ghost_of_plan(g, t.plan) then
      ghosts[#ghosts + 1] = g
    end
  end
  -- order = {{x0, y0, x1, y1}, ...}: an approved plan's sites in its build order; this round only handles the first
  -- site that still has ghosts (gather for it, build it, then the next one: the first machines run sooner)
  local box_i, more_boxes = nil, false
  if t.order and #ghosts > 0 then
    local all = ghosts
    for i, b in ipairs(t.order) do
      if not (t.skip_boxes and t.skip_boxes[i]) then
        local inside = {}
        for _, g in ipairs(all) do
          local p = g.position
          if p.x >= b[1] and p.x <= b[3] and p.y >= b[2] and p.y <= b[4] then inside[#inside + 1] = g end
        end
        if #inside > 0 then
          if box_i then more_boxes = true; break end
          ghosts, box_i = inside, i
        end
      end
    end
  end
  table.sort(ghosts, function(g1, g2) return dist(g1.position, c.position) < dist(g2.position, c.position) end)
  local built = t.built or 0
  if #ghosts == 0 then
    alog(a, built > 0 and ("built " .. built .. " ghosts, none left") or "no ghosts to build")
    return "done"
  end

  -- the last round tried to build some ghosts but none went away: they are blocked
  -- moving on to the next site: first top up the burners already built (they ran on their start fuel meanwhile)
  if box_i and t.cur_box and box_i ~= t.cur_box and t.fuel_at then
    t.cur_box = box_i
    t.tried, t.left, t.blocked = nil, nil, 0
    table.insert(a.queue, 1, t)
    table.insert(a.queue, 1, {type = "refuel", at = t.fuel_at, ordered = true, low_frac = 1 / 3,
      coal_from = t.coal_from, player = t.player, quiet = true, soft = true})
    t.requeued = true
    return "done"
  end
  local blocked = (t.tried and #ghosts >= (t.left or 0)) and (t.blocked or 0) + 1 or 0
  if blocked >= 2 and box_i and more_boxes then
    -- this site's last ghosts can't be placed: go on with the next site (they stay as ghosts)
    local g = ghosts[1]
    alog(a, "skipping " .. #ghosts .. " blocked ghost(s) near " .. round(g.position.x, 0) .. "," .. round(g.position.y, 0))
    t.skip_boxes = t.skip_boxes or {}
    t.skip_boxes[box_i] = true
    t.tried, t.left, t.blocked = nil, nil, 0
    table.insert(a.queue, 1, t)
    t.requeued = true
    return "done"
  end
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
    stall = t.stall or 0, left = #ghosts, blocked = blocked, plan = t.plan, fuel = t.fuel, coal_from = t.coal_from,
    order = t.order, skip_boxes = t.skip_boxes, fuel_try = t.fuel_try, fuel_at = t.fuel_at, cur_box = box_i,
    take_from = t.take_from, stashes = t.stashes}
  local site_key = box_i or 0
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
      -- trees/rocks in its footprint block the build (nothing else removes them on a fresh map); a tree standing
      -- just outside the tiles can still reach in with its collision box, so look a bit wider and test the boxes
      local bb = g.bounding_box   -- exact collision boxes: a tree can reach in by a few hundredths of a tile
      local x0, y0, x1, y1 = bb.left_top.x, bb.left_top.y, bb.right_bottom.x, bb.right_bottom.y
      for _, o in pairs(c.surface.find_entities_filtered{area = {{x0 - 1, y0 - 1}, {x1 + 1, y1 + 1}},
          type = {"tree", "simple-entity"}}) do
        local key = o.name .. "@" .. o.position.x .. "," .. o.position.y
        local ob = o.valid and o.bounding_box
        local touches = ob and ob.left_top.x < x1 and ob.right_bottom.x > x0 and ob.left_top.y < y1 and
          ob.right_bottom.y > y0
        if touches and o.minable and not cleared[key] then
          cleared[key] = true
          sub[#sub + 1] = {type = "mine", x = o.position.x, y = o.position.y, name = o.name, soft = true}
        end
      end
      sub[#sub + 1] = {type = "place", name = g.ghost_name, x = g.position.x, y = g.position.y,
        direction = g.direction, recipe = recipe, soft = true, ghost_only = true,
        fuel = t.fuel and START_FUEL[g.ghost_type] or nil}
    else
      need[item] = (need[item] or 0) + n
      rank[item] = math.min(rank[item] or 9, BUILD_RANK[g.ghost_type] or 1)
    end
  end

  if #sub > 0 then
    -- start fuel: right before placing a site's burners, get the coal they start on (the plan's coal chests first,
    -- else mining); fetched now and not earlier, so the helper's own hand-smelting can't burn it first. Checked for
    -- every batch (not once per site): a site's first batch may hold no burner, and a coal field placed without its
    -- start coal never starts (round-2 live finding)
    local fkey = tostring(site_key) .. "#" .. built
    if t.fuel and t.fuel_try ~= fkey then
      nxt.fuel_try = fkey
      local fneed = 0
      for _, st in ipairs(sub) do
        local proto = st.type == "place" and prototypes.entity[st.name]
        if proto and proto.burner_prototype then fneed = fneed + (st.fuel or 0) end
      end
      local short = fneed - c.get_item_count("coal")
      if short > 0 then
        local pl = t.player and game.get_player(t.player)
        if not (settings.global["claude-helper-use-player-items"] and settings.global["claude-helper-use-player-items"].value) then
          pl = nil
        end
        local P = new_planner(c, center, pl, t.coal_from)
        if P.acquire("coal", short, 0) and #P.tasks > 0 then
          local plan = P.ordered()
          alog(a, "start fuel: " .. table.concat(P.summary, ", "))
          nxt.left, nxt.tried, nxt.blocked = nil, nil, 0
          plan[#plan + 1] = nxt
          for i = #plan, 1, -1 do table.insert(a.queue, 1, plan[i]) end
          return "done"
        end
      end
    end
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
  if c.get_main_inventory().count_empty_stacks() < 6 and (t.stashes or 0) < 3 then
    -- nearly full (e.g. the plates of the plate chests a stage took apart): store the surplus first, then plan
    local list = stash_plan(a, need)
    if list then
      nxt.stashes = (t.stashes or 0) + 1
      list[#list + 1] = nxt
      for i = #list, 1, -1 do table.insert(a.queue, 1, list[i]) end
      return "done"
    end
  end
  local P = new_planner(c, center, player, t.take_from)  -- a later stage: the previous stage's chests first
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

  if #P.tasks == 0 and full and (t.stashes or 0) < 3 then
    -- too full to fetch: store the salvage it carries in a chest of its own first (round-2 live finding: after the
    -- electric-drill swap the burner drills it took apart filled its inventory and the build stopped)
    local list = stash_plan(a, need)
    if list then
      nxt.stashes = (t.stashes or 0) + 1
      list[#list + 1] = nxt
      for i = #list, 1, -1 do table.insert(a.queue, 1, list[i]) end
      return "done"
    end
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
  if t.keep then want = math.min(want, have - t.keep) end   -- a stash: only what is above what the helper keeps
  if want <= 0 then
    if t.soft then return "done" end
    return nil, "no " .. t.item
  end
  local n = e.insert{name = t.item, count = want}
  if n > 0 then c.remove_item{name = t.item, count = n} end
  alog(a, "inserted " .. n .. " " .. t.item .. " into " .. e.name)
  return "done"
end

-- {type="take", x, y, item?, count?, name?, inv?, unit?}: inv = "fuel" takes from the fuel slots (a plan's S0 step:
-- coal out of idle machines of the base, listed in the plan); unit = only that entity (unit_number)
function tasks.take(a, t)
  local c = a.entity
  local e = entity_at(c.surface, {x = t.x, y = t.y, name = t.name})
  if not e then return nil, "nothing at " .. t.x .. "," .. t.y end
  if t.unit and e.unit_number ~= t.unit then return nil, "not the listed " .. tostring(t.name) end
  if t.inv == "fuel" and e.force ~= c.force then return nil, "not ours" end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then return nil, "can't reach" end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local inv
  if t.inv == "fuel" then
    inv = e.burner and e.get_fuel_inventory()
  else
    inv = target_inventory(e, true)
  end
  if not inv then return nil, e.name .. " has no " .. (t.inv == "fuel" and "fuel" or "output") .. " inventory" end
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

local MAX_FETCH_FUEL = 400  -- coal the helper gathers for one refuel run when it runs out
local MAX_FETCHES = 3       -- coal fetch trips per refuel run

local function fuel_count(e)
  local fi = e.valid and e.burner and e.get_fuel_inventory()
  return fi and fi.get_item_count() or 0
end

-- {type="refuel", x?, y?, radius=32, per=10, types?, at?, ordered?, low?, low_frac?, coal_from?, player?, quiet?}: top up
-- burner entities' fuel from the helper's inventory. at = {{x, y, n?}, ...}: only the entities at these positions, n
-- coal each (else per); otherwise every burner within radius. low = only entities holding fewer than low coal
-- (default n); low_frac = the round's rule: go only when one of them is below that share of a fill, then top up all.
-- ordered (an approved plan's list: coal drills first, then each pair):
--   1. fetch: when the helper carries less than the starter amounts (a third of a fill each) it gets that much coal - the
--      chests at coal_from (the plan's coal chests) first, then any chest or furnace output nearby, the player's
--      inventory when the setting allows, or hand-mining (up to MAX_FETCHES trips); then, once, more coal for the full
--      top-up, only from what is already mined (chests, the player);
--   2. allocate what it carries: every burner its starter amount first (in list order), the rest tops them up in list
--      order;
--   3. deliver nearest first (one walk per site, not back and forth).
-- Not ordered: nearest first, each up to n, fetching coal (mining allowed) when it runs out.
local function refuel_fetch(a, t, need, mine, note)
  local c = a.entity
  local player = t.player and game.get_player(t.player)
  if not (settings.global["claude-helper-use-player-items"] and settings.global["claude-helper-use-player-items"].value) then
    player = nil
  end
  local P = new_planner(c, c.position, player, t.coal_from)
  P.no_mine = not mine or nil
  if not P.acquire("coal", math.max(1, math.min(math.ceil(need), MAX_FETCH_FUEL)), 0) or #P.tasks == 0 then
    return false
  end
  t.fetches = t.fetches + 1
  t.approach = nil
  local plan = P.ordered()
  if not t.quiet then tell(a, "Getting coal to refuel: " .. table.concat(P.summary, ", ")) end
  alog(a, "refuel: getting coal for " .. note .. " (" .. table.concat(P.summary, ", ") .. ")")
  table.insert(a.queue, 1, t)
  for i = #plan, 1, -1 do table.insert(a.queue, 1, plan[i]) end
  t.requeued = true
  return true
end

-- a drill of a plan (e.g. the player's own drill converted into a pair) whose drop target is still a ghost: fuelling it
-- now would spill ore on the ground where the furnace goes; it is fuelled once its furnace/chest is built
local function waits_for_ghost(e)
  if e.type ~= "mining-drill" then return false end
  local tgt = e.drop_target   -- a belt still there but marked for removal: the furnace replaces it, wait for that
  if tgt and tgt.valid then return tgt.to_be_deconstructed() end
  local ok, dp = pcall(function() return e.drop_position end)
  if not ok or not dp then return false end
  return e.surface.find_entities_filtered{area = {{dp.x - 0.1, dp.y - 0.1}, {dp.x + 0.1, dp.y + 0.1}},
    type = "entity-ghost", force = e.force, limit = 1}[1] ~= nil
end

function tasks.refuel(a, t)
  local c = a.entity
  local per = t.per or 10
  t.requeued = nil
  if not t.targets then
    local center = t.x and {x = t.x, y = t.y} or c.position
    t.targets = {}
    local found = {}
    if t.at then
      for _, p in ipairs(t.at) do
        local e = c.surface.find_entities_filtered{position = {x = p[1], y = p[2]}, force = c.force,
          type = burner_types, limit = 1}[1]
        if e and e.burner and not waits_for_ghost(e) then found[#found + 1] = {e = e, n = tonumber(p[3]) or per} end
      end
    else
      for _, e in pairs(c.surface.find_entities_filtered{position = center, radius = t.radius or 32,
          type = t.types or burner_types, force = c.force}) do
        if e.burner then found[#found + 1] = {e = e, n = per} end
      end
    end
    local any_low = false
    for _, x in ipairs(found) do
      local low = t.low or (t.low_frac and math.max(1, math.floor(x.n * t.low_frac))) or x.n
      if fuel_count(x.e) < low then any_low = true end
    end
    for _, x in ipairs(found) do
      local low = t.low or (t.low_frac and math.max(1, math.floor(x.n * t.low_frac))) or x.n
      local k = fuel_count(x.e)
      local held = x.e.get_fuel_inventory().get_contents()[1]
      -- a slot holding another fuel (e.g. wood) can't take coal: leave it until that burns out
      if (not held or held.name == "coal") and (k < low or (t.low_frac and any_low and k < x.n)) then
        t.targets[#t.targets + 1] = x
      end
    end
    t.fueled, t.fetches = 0, t.fetches or 0
    if #t.targets == 0 then
      if not t.quiet then alog(a, "refuel: nothing needs fuel") end
      return "done"
    end
  end
  local function starter(r) return math.max(1, math.floor(r.n / 3)) end   -- covers a round period plus the trip

  if t.ordered and not t.alloc then
    -- 1. fetch
    local carry = c.get_item_count("coal")
    local s_need, f_need = 0, 0
    for _, r in ipairs(t.targets) do
      if r.e.valid then
        local k = fuel_count(r.e)
        s_need = s_need + math.max(0, starter(r) - k)
        f_need = f_need + math.max(0, r.n - k)
      end
    end
    if carry < s_need and t.fetches < MAX_FETCHES and refuel_fetch(a, t, s_need - carry, true, "the starter amounts") then
      return "done"
    end
    if carry < f_need and not t.topped then
      t.topped = true
      if refuel_fetch(a, t, f_need - carry, false, "topping up") then return "done" end
    end
    -- 2. allocate
    local left = carry
    t.alloc = {}
    for i, r in ipairs(t.targets) do
      local give = r.e.valid and math.min(left, math.max(0, starter(r) - fuel_count(r.e))) or 0
      t.alloc[i] = give
      left = left - give
    end
    for i, r in ipairs(t.targets) do
      if left <= 0 then break end
      local give = r.e.valid and math.min(left, math.max(0, r.n - fuel_count(r.e) - t.alloc[i])) or 0
      t.alloc[i] = t.alloc[i] + give
      left = left - give
    end
  end

  -- 3. deliver (ordered: the allocation, nearest first) / plain: nearest first up to n
  local x, bi, bd
  for i, r in ipairs(t.targets) do
    local want = t.alloc and t.alloc[i] or (r.e.valid and (r.n - fuel_count(r.e)) or 0)
    if r.e.valid and want > 0 then
      local d = dist(r.e.position, c.position)
      if not bd or d < bd then x, bi, bd = r, i, d end
    end
  end
  if not x then
    if not t.quiet or t.fueled > 0 then
      alog(a, "refuel: fueled " .. t.fueled .. (t.fetches > 0 and (" (" .. t.fetches .. " coal trips)") or ""))
    end
    return "done"
  end
  local e = x.e
  local fi = e.get_fuel_inventory()
  -- a plan's round only ever puts coal in (wood would block the next coal delivery until it has burnt)
  local fuel
  if t.ordered then
    fuel = c.get_item_count("coal") > 0 and fi.can_insert{name = "coal", count = 1} and "coal" or nil
  else
    fuel = pick_fuel(c, fi)
  end
  if not fuel then
    if t.alloc then t.alloc[bi] = 0; return "running" end   -- allocated coal is gone (used elsewhere): skip it
    if t.fetches >= MAX_FETCHES then
      alog(a, "refuel: out of fuel after " .. t.fueled .. " (" .. t.fetches .. " coal trips)")
      if t.fueled > 0 or t.quiet then return "done" end
      return nil, "no fuel in inventory"
    end
    local need = 0
    for _, r in ipairs(t.targets) do
      if r.e.valid then need = need + math.max(0, r.n - fuel_count(r.e)) end
    end
    if refuel_fetch(a, t, need, true, "refuelling") then return "done" end
    if t.fueled > 0 or t.quiet then
      alog(a, "refuel: out of fuel after " .. t.fueled .. " (no coal to fetch or mine nearby)")
      return "done"
    end
    return nil, "no fuel in inventory (and no coal to fetch or mine nearby)"
  end
  local near = ensure_near(a, t, e.position, c.reach_distance)
  if near == nil then
    if t.alloc then t.alloc[bi] = 0 else table.remove(t.targets, bi) end
    t.approach = nil
    return "running"
  end
  if not near then return "running" end
  c.walking_state = {walking = false}
  local want = t.alloc and t.alloc[bi] or (x.n - fi.get_item_count())
  if t.alloc then  -- it burnt some since the allocation: at least its starter amount now, if the helper has it
    want = math.max(want, starter(x) - fi.get_item_count())
  end
  want = math.min(want, c.get_item_count(fuel))
  local n = want > 0 and fi.insert{name = fuel, count = want} or 0
  if n > 0 then c.remove_item{name = fuel, count = n}; t.fueled = t.fueled + 1 end
  if t.alloc then
    t.alloc[bi] = 0
  elseif n <= 0 then
    table.remove(t.targets, bi)   -- full of another fuel: skip it
  end
  t.approach = nil
  return "running"
end

local ROUND_PERIOD = 9000   -- ticks between two logistics rounds (2.5 min)

-- {type="round", at, coal_from?, period?, player?, plan?}: the helper's logistics round for an on-site plan
-- (PLANNER-v2.md 10.1): tops up the plan's hand-fed burners (at = {{x, y, n}}, in order; coal from the plan's coal
-- chests first, else any chest nearby or mining), then waits `period` ticks and repeats. Any other order queued
-- meanwhile ends it (it never blocks other work); Stop or a new quick action ends it too.
function tasks.round(a, t)
  if t.next_tick and game.tick < t.next_tick then
    if #a.queue > 0 then alog(a, "round: stopped for new orders"); return "done" end
    return "running"
  end
  t.cycles = (t.cycles or 0) + 1
  if t.cycles == 1 and agent.round_started then agent.round_started(a) end
  local nxt = {type = "round", at = t.at, coal_from = t.coal_from, period = t.period, player = t.player,
    plan = t.plan, cycles = t.cycles, quiet = true, next_tick = game.tick + (t.period or ROUND_PERIOD)}
  table.insert(a.queue, 1, nxt)
  table.insert(a.queue, 1, {type = "refuel", at = t.at, ordered = true, coal_from = t.coal_from, player = t.player,
    low_frac = 1 / 3, quiet = true, soft = true})
  return "done"
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

-- {type="dismantle", plan}: pick up exactly the entities an approved plan listed for removal (planner v2 rebuild):
-- nearest first, only while they are still valid and marked for deconstruction. Items (and their contents) go into
-- the helper's inventory; when it is full the rest spills next to it (reported). Ghosts that waited for the ground
-- are placed as soon as each entity is gone.
function tasks.dismantle(a, t)
  local c = a.entity
  local p = storage.plans and storage.plans[t.plan]
  if not (p and p.removing) then return "done" end
  local best, bd, bu
  for unit, row in pairs(p.removing) do
    local e = row.entity
    if e and e.valid and e.to_be_deconstructed() and not row.skip then
      local d = dist(e.position, c.position)
      if not bd or d < bd then best, bd, bu = e, d, unit end
    elseif not (e and e.valid) and api.plans_removed then
      api.plans_removed(unit, "removed")
    end
  end
  if not best then
    alog(a, "dismantle: done (" .. (t.n or 0) .. " picked up)")
    return "done"
  end
  if t.target ~= bu then t.target, t.timer, t.approach = bu, nil, nil end
  local near = ensure_near(a, t, best.position, c.reach_distance - 0.5)
  if near == nil then
    -- unreachable: leave it marked (the player or robots may take it) and go on with the others
    t.skip = (t.skip or 0) + 1
    if t.skip > 20 then return nil, "can't reach the entities to dismantle" end
    p.removing[bu].skip = true
    return "running"
  end
  if not near then return "running" end
  c.walking_state = {walking = false}
  if not t.timer then t.timer = game.tick + mining_ticks(c, best) end
  if game.tick < t.timer then return "running" end
  t.timer = nil
  local inv = c.get_main_inventory()
  local name = best.name
  local ok = best.valid and best.mine{inventory = inv, force = false}
  if not ok and best.valid then
    ok = best.mine{inventory = inv, force = true}  -- inventory full: what doesn't fit spills next to the helper
    if ok and not t.spilled then t.spilled = true; alog(a, "dismantle: inventory full, items spilled") end
  end
  if not ok then return nil, "couldn't pick up " .. name end
  t.n = (t.n or 0) + 1
  if api.plans_removed then api.plans_removed(bu, "removed") end
  return "running"
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
-- a plan's logistics round starts after its build: report the build as done (the round itself never ends)
agent.round_started = function(a)
  if a.notify_what then
    notify_done(a)
    notify(a, "I keep the plan's burners fuelled every few minutes (any new order stops that).")
  end
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
  -- helper work counters (planner v2 measures its gather rate from these): ticks busy / walking / mining
  local stats = a.stats
  if not stats then stats = {mined = {}, active_ticks = 0, mining_ticks = 0, walking_ticks = 0}; a.stats = stats end
  stats.active_ticks = stats.active_ticks + 1
  if t.type == "walk" then stats.walking_ticks = stats.walking_ticks + 1
  elseif t.type == "mine" and t.timer then stats.mining_ticks = stats.mining_ticks + 1 end
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
      if not t.auto and not t.quiet and not t.requeued then
        alog(a, "done: " .. t.type .. (t.name and (" " .. t.name) or ""))
      end
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
    local parent = a.queue[1]
    if t.auto and t.type == "walk" and parent and parent.type == "mine" and not parent.x and
        (parent.skipped or 0) < 5 then
      -- couldn't walk to that ore tile (e.g. nests or water in the way): mine another tile of the same resource
      local tg = parent.target
      parent.skip = parent.skip or {}
      parent.skip_at = parent.skip_at or {}
      if tg and tg.valid then
        parent.skip[tg.position.x .. "," .. tg.position.y] = true
        parent.skip_at[#parent.skip_at + 1] = {x = tg.position.x, y = tg.position.y}  -- and its neighbourhood
      end
      parent.target, parent.timer, parent.approach = nil, nil, nil
      parent.skipped = (parent.skipped or 0) + 1
      return
    end
    if t.auto and parent and parent.soft then table.remove(a.queue, 1) end
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
function agent.work_area(player, bb, what, label, fuel_at, plan_id)
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
  local pl = plan_id and storage.plans and storage.plans[plan_id]
  local coal_from = pl and pl.coal_from or nil
  local dismantle = what == "build" and pl and pl.removing and next(pl.removing) ~= nil
  -- S0 (planner v2): fuel out of the listed idle machines of the base first (exactly those, nothing else)
  local took = 0
  if what == "build" and pl and pl.takes and not pl.takes_done then
    for _, tk in ipairs(pl.takes) do
      list[#list + 1] = {type = "take", x = tk[2], y = tk[3], name = tk[1], unit = tk[4], item = tk[5], count = tk[6],
        inv = "fuel", soft = true}
      took = took + 1
    end
    pl.takes_done = true
  end
  if dismantle then
    list[#list + 1] = {type = "dismantle", plan = plan_id}
  end
  -- S0 repairs: the listed existing burners get fuel before anything new is built
  if what == "build" and pl and pl.refuel_existing and #pl.refuel_existing > 0 then
    local at = {}
    for _, r in ipairs(pl.refuel_existing) do at[#at + 1] = {r[2], r[3], r[5]} end
    list[#list + 1] = {type = "refuel", at = at, ordered = true, coal_from = coal_from, player = pi, soft = true}
  end
  if what == "build" then
    -- only this plan's ghosts (never the player's own or another plan's); a few coal into each new burner
    list[#list + 1] = {type = "build_ghosts", x = cx, y = cy, radius = radius, player = pi, plan = plan_id,
      fuel = plan_id and true or nil, coal_from = coal_from, order = pl and pl.build_order or nil,
      fuel_at = (pl and pl.build_order and fuel_at and #fuel_at > 0) and fuel_at or nil,
      take_from = pl and pl.take_from or nil}
  end
  local hand = fuel_at and #fuel_at > 0
  -- primed entries (r[4]) are planner v3 coal-field burners: started and restarted, not hand-fed
  local hand_n, primed_n = 0, 0
  for _, r in ipairs(fuel_at or {}) do
    if r[4] then primed_n = primed_n + 1 else hand_n = hand_n + 1 end
  end
  local round_txt = hand_n > 0 and (", then keeps the " .. hand_n .. " hand-fed burners fuelled with coal every few " ..
      "minutes" .. (primed_n > 0 and " and the coal fields running." or ".")) or
    (primed_n > 0 and (", then checks every few minutes that the coal fields keep running (everything else gets " ..
      "its coal by belt).") or ".")
  if hand and plan_id and (what == "build" or what == "round") then
    -- the plan's logistics round: keeps its hand-fed burners fuelled (coal from its coal chests) until a new order
    list[#list + 1] = {type = "round", at = fuel_at, coal_from = coal_from, player = pi, plan = plan_id}
  elseif hand then
    list[#list + 1] = {type = "refuel", x = cx, y = cy, radius = radius, per = 50, player = pi, at = fuel_at,
      ordered = plan_id and true or nil, coal_from = coal_from}
  elseif what == "refuel" or what == "round" then
    return pre .. "Nothing in this plan is fuelled by hand."
  end
  -- an endless follow or logistics round never finishes: drop it; a halted queue is stale: replace it
  local endless = {follow = true, round = true}
  local busy = a.cur and not endless[a.cur.type] and not (a.cur.quiet and a.cur.type == "refuel") and not a.halted
  local queue = {}
  for _, q in ipairs(a.queue) do
    if not endless[q.type] and not (q.quiet and q.type == "refuel") then queue[#queue + 1] = q end
  end
  if not a.halted and (busy or #queue > 0) then
    if a.cur and not busy then
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
    return pre .. "The helper is " .. (took > 0 and "taking the coal out of the listed idle machines, " or "") ..
      (dismantle and "taking apart the listed old builds, then " or "") ..
      "building " .. (label or "it") .. ", fetching or crafting what it needs" ..
      (busy and " after its current task" or "") .. round_txt
  end
  if what == "round" then
    if hand_n == 0 then
      return pre .. "The helper checks every few minutes that the plan's coal fields keep running (any new order " ..
        "stops it); everything else gets its coal by belt."
    end
    return pre .. "The helper keeps the plan's " .. hand_n .. " hand-fed burners fuelled every few minutes (any new " ..
      "order stops it)."
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
