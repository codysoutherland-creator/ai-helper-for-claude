-- Op registry + all observation/action/blueprint ops + the /claude <json> command (RCON path).
-- Every response is {ok=true, result=...} or {ok=false, error="..."}; transport.lua serves the same ops over UDP.

local util = require("scripts.util")
local access = require("scripts.access")
local round, pos_of, pos_out, dist, first_player, get_player, get_surface, get_force, parse_dir, dir_names, status_names, area_from, contents_to_map, inv_contents, entity_at, entity_brief =
  util.round, util.pos_of, util.pos_out, util.dist, util.first_player, util.get_player, util.get_surface, util.get_force, util.parse_dir, util.dir_names, util.status_names, util.area_from, util.contents_to_map, util.inv_contents, util.entity_at, util.entity_brief

local api = {}
local ops = {}
api.ops = ops
api.agent_tasks = {}  -- task handler table, filled by agent.lua (listed by `help`)

function api.register(name, fn) ops[name] = fn end

-- Test / cheat ops exist only with the "claude-debug-ops" startup setting (the headless test harness turns it on).
-- Anything on this machine can send UDP to the game, so in a normal game these must not be reachable.
function api.register_debug(name, fn)
  if access.debug_ops() then
    ops[name] = fn
  else
    ops[name] = function()
      error(name .. " is a test/cheat op: enable the mod's startup setting 'Debug: enable test and cheat ops' to use it")
    end
  end
end

-- Run one op; never throws.
function api.call(op, args)
  local fn = ops[op]
  if not fn then return {ok = false, error = "unknown op " .. tostring(op)} end
  local ok, res = pcall(fn, args or {})
  if ok then return {ok = true, result = res} end
  return {ok = false, error = tostring(res)}
end

---------------------------------------------------------------------------
-- observation ops
---------------------------------------------------------------------------

function ops.ping(args)
  local players = {}
  for _, p in pairs(game.players) do
    players[#players + 1] = {name = p.name, connected = p.connected, surface = p.surface.name,
      pos = pos_out(p.position), admin = p.admin, index = p.index, role = access.role(p)}
  end
  local surfaces = {}
  for _, s in pairs(game.surfaces) do surfaces[#surfaces + 1] = s.name end
  local a = storage.agent
  return {tick = game.tick, version = helpers.game_version, players = players, surfaces = surfaces,
    agent = (a and a.entity and a.entity.valid) and pos_out(a.entity.position) or nil,
    multiplayer = game.is_multiplayer(), who_can_ask = access.policy(), debug_ops = access.debug_ops()}
end

function ops.player(args)
  local p = get_player(args)
  local o = {name = p.name, surface = p.surface.name, pos = pos_out(p.position), controller = p.controller_type}
  local inv = p.get_main_inventory()
  if inv then o.inventory = inv_contents(inv) end
  if p.cursor_stack and p.cursor_stack.valid_for_read then o.cursor = p.cursor_stack.name end
  if p.selected then o.hovering = entity_brief(p.selected, true) end
  return o
end

local function top_flows(stats, category, precision, limit, filter)
  local counts = category == "input" and stats.input_counts or stats.output_counts
  local rows = {}
  for name, _ in pairs(counts) do
    if not filter or filter[name] then
      local r = stats.get_flow_count{name = name, category = category, precision_index = precision}
      if r > 0.001 then rows[#rows + 1] = {name, r} end
    end
  end
  table.sort(rows, function(a, b) return a[2] > b[2] end)
  local out = {}
  for i = 1, math.min(limit, #rows) do out[rows[i][1]] = round(rows[i][2], 1) end
  return out
end

local precisions = {
  ["5s"] = defines.flow_precision_index.five_seconds,
  ["1m"] = defines.flow_precision_index.one_minute,
  ["10m"] = defines.flow_precision_index.ten_minutes,
  ["1h"] = defines.flow_precision_index.one_hour,
}

-- Items/fluids per minute. args.items = optional list to restrict to.
function ops.production(args)
  local surface = get_surface(args)
  local force = get_force(args)
  local precision = precisions[args.window or "10m"] or precisions["10m"]
  local filter
  if args.items then filter = {}; for _, n in pairs(args.items) do filter[n] = true end end
  local limit = args.limit or 25
  local is = force.get_item_production_statistics(surface)
  local fs = force.get_fluid_production_statistics(surface)
  return {
    window = args.window or "10m", unit = "per_minute", surface = surface.name,
    items_produced = top_flows(is, "input", precision, limit, filter),
    items_consumed = top_flows(is, "output", precision, limit, filter),
    fluids_produced = top_flows(fs, "input", precision, limit, filter),
    fluids_consumed = top_flows(fs, "output", precision, limit, filter),
  }
end

function ops.power(args)
  local surface = get_surface(args)
  local force = get_force(args)
  local seen, nets = {}, {}
  for _, pole in pairs(surface.find_entities_filtered{type = "electric-pole", force = force}) do
    local id = pole.electric_network_id
    if id and not seen[id] then
      seen[id] = true
      local st = pole.electric_network_statistics
      local prod, cons = 0, 0
      local producers, consumers = {}, {}
      for name, _ in pairs(st.output_counts) do
        local v = st.get_flow_count{name = name, category = "output", precision_index = defines.flow_precision_index.five_seconds} * 60 / 1e6
        prod = prod + v
        if v > 0.01 then producers[name] = round(v, 2) end
      end
      for name, _ in pairs(st.input_counts) do
        local v = st.get_flow_count{name = name, category = "input", precision_index = defines.flow_precision_index.five_seconds} * 60 / 1e6
        cons = cons + v
        if v > 0.01 then consumers[name] = round(v, 2) end
      end
      nets[#nets + 1] = {id = id, sample_pole = pos_out(pole.position), production_MW = round(prod, 2),
        consumption_MW = round(cons, 2), producers = producers, top_consumers = consumers}
    end
  end
  table.sort(nets, function(a, b) return a.consumption_MW > b.consumption_MW end)
  return {networks = nets}
end

function ops.research(args)
  local force = get_force(args)
  local cur = force.current_research
  local o = {current = cur and cur.name or nil, progress = round(force.research_progress or 0, 3), queue = {}}
  for _, t in pairs(force.research_queue or {}) do
    o.queue[#o.queue + 1] = type(t) == "string" and t or t.name
  end
  if args.available then
    local avail = {}
    for name, t in pairs(force.technologies) do
      if t.enabled and not t.researched then
        local ok = true
        for _, pre in pairs(t.prerequisites) do if not pre.researched then ok = false break end end
        if ok then
          local packs = {}
          for _, ing in pairs(t.research_unit_ingredients) do packs[#packs + 1] = ing.name end
          avail[#avail + 1] = {name = name, units = t.research_unit_count, packs = packs}
        end
      end
    end
    o.available = avail
  end
  return o
end

function ops.queue_research(args)
  local force = get_force(args)
  local ok = force.add_research(args.tech)
  return {queued = ok}
end

-- Entity counts by name (whole surface or area).
function ops.census(args)
  local surface = get_surface(args)
  local f = {force = get_force(args)}
  if args.area or args.radius then f.area = area_from(args) end
  local counts = {}
  for _, e in pairs(surface.find_entities_filtered(f)) do
    counts[e.name] = (counts[e.name] or 0) + 1
  end
  return counts
end

local problem_types = {"assembling-machine", "furnace", "mining-drill", "lab", "boiler", "generator",
  "rocket-silo", "offshore-pump", "pump", "reactor", "agricultural-tower", "ammo-turret", "roboport", "beacon"}
local fine = {
  working = true, normal = true, fully_charged = true, charging = true, discharging = true,
  waiting_for_plants_to_grow = true, turned_off_during_daytime = true,
}

-- Non-working machines grouped by (name, status), with sample positions.
function ops.problems(args)
  local surface = get_surface(args)
  local f = {force = get_force(args), type = args.types or problem_types}
  if args.area or args.radius then f.area = area_from(args) end
  local groups, order = {}, {}
  local total = 0
  for _, e in pairs(surface.find_entities_filtered(f)) do
    local st = e.status
    local sn = st and status_names[st] or "unknown"
    if not fine[sn] then
      local key = e.name .. "|" .. sn
      local g = groups[key]
      if not g then
        g = {name = e.name, status = sn, count = 0, samples = {}}
        groups[key] = g
        order[#order + 1] = g
      end
      g.count = g.count + 1
      total = total + 1
      if #g.samples < (args.samples or 3) then
        local s = pos_out(e.position)
        local r = (e.type == "assembling-machine" or e.type == "furnace") and e.get_recipe()
        if r then s.recipe = r.name end
        g.samples[#g.samples + 1] = s
      end
    end
  end
  table.sort(order, function(a, b) return a.count > b.count end)
  return {total = total, groups = order}
end

function ops.inspect(args)
  local surface = get_surface(args)
  if (args.x or args.position) and not args.radius and not args.area then
    local e = entity_at(surface, args)
    if not e then return {found = false} end
    return entity_brief(e, true)
  end
  local f = {area = area_from(args, 4)}
  if args.name then f.name = args.name end
  if args.type then f.type = args.type end
  local out = {}
  local limit = args.limit or 60
  for _, e in pairs(surface.find_entities_filtered(f)) do
    if e.type ~= "resource" or args.include_resources then
      out[#out + 1] = entity_brief(e, args.detail ~= false)
      if #out >= limit then break end
    end
  end
  return {count = #out, entities = out}
end

function ops.find(args)
  local surface = get_surface(args)
  local limit = args.limit or 20
  local f = {name = args.name, type = args.type}
  if args.area or args.radius then f.area = area_from(args) end
  if args.force then f.force = args.force end
  local origin = pos_of(args.position) or (args.x and {x = args.x, y = args.y})
  if origin and not f.area then
    f.position = origin
    f.radius = args.max_distance or 200
  end
  if not origin then f.limit = limit end
  local found = surface.find_entities_filtered(f)
  if origin then
    table.sort(found, function(a, b) return dist(a.position, origin) < dist(b.position, origin) end)
  end
  local out = {}
  for i = 1, math.min(limit, #found) do out[i] = entity_brief(found[i], false) end
  return {total = #found, nearest = out}
end

-- Resource patches near a point: totals + bounding box per resource.
function ops.resources(args)
  local surface = get_surface(args)
  local found = surface.find_entities_filtered{type = "resource", area = area_from(args, 64)}
  local res = {}
  for _, e in pairs(found) do
    local r = res[e.name]
    if not r then
      r = {amount = 0, tiles = 0, min = {x = 1e9, y = 1e9}, max = {x = -1e9, y = -1e9}}
      res[e.name] = r
    end
    r.amount = r.amount + e.amount
    r.tiles = r.tiles + 1
    local p = e.position
    if p.x < r.min.x then r.min.x = p.x end
    if p.y < r.min.y then r.min.y = p.y end
    if p.x > r.max.x then r.max.x = p.x end
    if p.y > r.max.y then r.max.y = p.y end
  end
  for _, r in pairs(res) do
    r.center = pos_out{x = (r.min.x + r.max.x) / 2, y = (r.min.y + r.max.y) / 2}
    r.min = pos_out(r.min); r.max = pos_out(r.max)
  end
  return res
end

---------------------------------------------------------------------------
-- ASCII map
---------------------------------------------------------------------------

local type_glyph = {
  ["furnace"] = "F", ["assembling-machine"] = "A", ["mining-drill"] = "M", ["inserter"] = "i",
  ["electric-pole"] = "+", ["container"] = "C", ["logistic-container"] = "C", ["pipe"] = "=",
  ["pipe-to-ground"] = "u", ["underground-belt"] = "U", ["splitter"] = "S", ["boiler"] = "B",
  ["generator"] = "G", ["offshore-pump"] = "O", ["lab"] = "L", ["solar-panel"] = "P",
  ["accumulator"] = "Q", ["tree"] = "T", ["simple-entity"] = "R", ["cliff"] = "#",
  ["entity-ghost"] = "?", ["character"] = "@", ["wall"] = "W", ["gate"] = "W",
  ["ammo-turret"] = "X", ["electric-turret"] = "X", ["fluid-turret"] = "X", ["straight-rail"] = "r",
  ["curved-rail-a"] = "r", ["curved-rail-b"] = "r", ["half-diagonal-rail"] = "r", ["storage-tank"] = "K",
  ["pump"] = "p", ["roboport"] = "R", ["radar"] = "D", ["beacon"] = "b", ["chemical-plant"] = "H",
  ["oil-refinery"] = "Y", ["rocket-silo"] = "Z", ["train-stop"] = "N", ["rail-signal"] = "s",
  ["rail-chain-signal"] = "s", ["lamp"] = "l", ["mining-drill-ghost"] = "?",
}
local belt_glyph = {
  [defines.direction.north] = "^", [defines.direction.east] = ">",
  [defines.direction.south] = "v", [defines.direction.west] = "<",
}

-- args: x,y,radius (or area); max 100x100 tiles.
function ops.map(args)
  local surface = get_surface(args)
  local area = area_from(args, 16)
  local x0, y0 = math.floor(area.left_top.x), math.floor(area.left_top.y)
  local x1, y1 = math.ceil(area.right_bottom.x) - 1, math.ceil(area.right_bottom.y) - 1
  local w, h = x1 - x0 + 1, y1 - y0 + 1
  if w > 100 or h > 100 then error("map area too large (max 100x100)") end
  local grid = {}
  for y = 1, h do
    local row = {}
    for x = 1, w do row[x] = "." end
    grid[y] = row
  end
  local legend = {}
  local function put(tx, ty, g, prio)
    local gx, gy = tx - x0 + 1, ty - y0 + 1
    if gx >= 1 and gx <= w and gy >= 1 and gy <= h then grid[gy][gx] = g end
  end
  -- water
  local ok_w, water = pcall(surface.find_tiles_filtered, {area = area, collision_mask = "water_tile"})
  if ok_w then for _, t in pairs(water) do put(t.position.x, t.position.y, "~") end end
  -- resources first, then entities on top
  local ents = surface.find_entities_filtered{area = area}
  local layer = {resource = 0, tree = 1, ["simple-entity"] = 1, cliff = 1}
  table.sort(ents, function(a, b) return (layer[a.type] or 2) < (layer[b.type] or 2) end)
  for _, e in pairs(ents) do
    local g
    if e.type == "resource" then
      g = string.lower(string.sub(e.name, 1, 1))
      if e.name == "coal" then g = "k" end
      if e.name == "crude-oil" then g = "o" end
    elseif e.type == "transport-belt" then
      g = belt_glyph[e.direction] or "~"
      legend["^>v<"] = "transport belts (direction)"
    else
      g = type_glyph[e.type] or "*"
    end
    if e.type ~= "resource" and e.type ~= "transport-belt" then
      legend[g] = legend[g] and (string.find(legend[g], e.name, 1, true) and legend[g] or (legend[g] .. "," .. e.name)) or e.name
    elseif e.type == "resource" then
      legend[g] = e.name
    end
    local bb = e.bounding_box
    local bx0, by0 = math.floor(bb.left_top.x + 0.01), math.floor(bb.left_top.y + 0.01)
    local bx1, by1 = math.ceil(bb.right_bottom.x - 0.01) - 1, math.ceil(bb.right_bottom.y - 0.01) - 1
    for ty = by0, by1 do for tx = bx0, bx1 do put(tx, ty, g) end end
  end
  local lines = {}
  for y = 1, h do lines[y] = table.concat(grid[y]) end
  legend["~"] = legend["~"] or "water"
  legend["."] = "empty"
  return {origin = {x = x0, y = y0}, width = w, height = h, rows = lines, legend = legend,
    note = "row 1 = y " .. y0 .. ", col 1 = x " .. x0}
end

---------------------------------------------------------------------------
-- actions
---------------------------------------------------------------------------

function ops.say(args)
  local who = args.as or "Claude"
  game.print({"", "[color=0.85,0.47,0.34][" .. who .. "][/color] ", args.text}, {skip = defines.print_skip.never})
  return true
end

-- Every ghost Claude places carries tags.claude_plan (a plan id, or "manual") so clear_claude_ghosts is exact.
local function plan_id(args)
  local p = args.plan
  return (p ~= nil and p ~= "") and tostring(p) or "manual"
end

local function place_ghost(surface, force, g, plan)
  local p = pos_of(g.position) or {x = g.x, y = g.y}
  local params = {name = "entity-ghost", inner_name = g.name, position = p, direction = parse_dir(g.direction),
    force = force, tags = {claude_plan = plan}}
  if not surface.can_place_entity{name = g.name, position = p, direction = params.direction, force = force,
      build_check_type = defines.build_check_type.script_ghost} then
    return nil, "blocked"
  end
  -- the engine treats other ghosts as replaceable; don't silently stack/replace planned builds
  local proto = prototypes.entity[g.name]
  local cb = proto.collision_box
  local hw, hh = (cb.right_bottom.x - cb.left_top.x) / 2 - 0.1, (cb.right_bottom.y - cb.left_top.y) / 2 - 0.1
  if params.direction == defines.direction.east or params.direction == defines.direction.west then hw, hh = hh, hw end
  local existing = surface.find_entities_filtered{type = "entity-ghost", force = force,
    area = {{p.x - hw, p.y - hh}, {p.x + hw, p.y + hh}}, limit = 1}[1]
  if existing then return nil, "ghost of " .. existing.ghost_name .. " already there" end
  local e = surface.create_entity(params)
  if e and g.recipe then pcall(function() e.set_recipe(g.recipe) end) end
  return e
end

-- args.ghosts = [{name, x, y, direction, recipe}], args.plan = plan id for the tag (default "manual")
function ops.place_ghosts(args)
  local surface, force = get_surface(args), get_force(args)
  local plan = plan_id(args)
  local placed, failed = 0, {}
  for _, g in pairs(args.ghosts or {args}) do
    local e, err = place_ghost(surface, force, g, plan)
    if e then placed = placed + 1 else failed[#failed + 1] = {name = g.name, x = g.x, y = g.y, why = err} end
  end
  return {placed = placed, failed = failed}
end

function ops.set_recipe(args)
  local e = entity_at(get_surface(args), args)
  if not e then error("no entity there") end
  local overflow = e.set_recipe(args.recipe, args.quality)
  return {entity = e.name, recipe = args.recipe, returned_items = overflow}
end

local MAX_DECONSTRUCT_HALF = 64  -- tiles; one call can't mark a whole base (unless debug ops are on)

function ops.deconstruct(args)
  local surface, force = get_surface(args), get_force(args)
  local area = area_from(args, 1)
  if not access.debug_ops() then
    local w = area.right_bottom.x - area.left_top.x
    local h = area.right_bottom.y - area.left_top.y
    if not (w and h) or w > 2 * MAX_DECONSTRUCT_HALF or h > 2 * MAX_DECONSTRUCT_HALF then
      error("deconstruct area too large (max radius " .. MAX_DECONSTRUCT_HALF .. ")")
    end
  end
  local f = {area = area}
  if args.name then f.name = args.name end
  if args.type then f.type = args.type end
  local n = 0
  for _, e in pairs(surface.find_entities_filtered(f)) do
    if e.type ~= "character" and e.type ~= "resource" and e.order_deconstruction(force) then n = n + 1 end
  end
  return {marked = n}
end

-- {plan?, surface?, force?, dry_run?}: remove Claude's ghosts. A ghost is Claude's when no player is its last_user
-- (script-placed: v0.1/v0.2 ghosts and everything tagged since); with `plan`, only ghosts tagged with that plan.
-- Ghosts a player placed or touched (last_user = a player) are never removed.
function ops.clear_claude_ghosts(args)
  local force = get_force(args)
  local surfaces = args.surface and {get_surface(args)} or game.surfaces
  local plan = args.plan ~= nil and args.plan ~= "" and tostring(args.plan) or nil
  local r = {removed = 0, tagged = 0, untagged = 0, kept_player = 0, kept_other = 0, by_plan = {},
    dry_run = args.dry_run and true or false}
  for _, surface in pairs(surfaces) do
    for _, g in pairs(surface.find_entities_filtered{type = {"entity-ghost", "tile-ghost"}, force = force}) do
      local tag = g.type == "entity-ghost" and g.tags and g.tags.claude_plan or nil
      if g.last_user then
        r.kept_player = r.kept_player + 1
      elseif plan and tag ~= plan then
        r.kept_other = r.kept_other + 1
      else
        if tag then
          r.tagged = r.tagged + 1
          r.by_plan[tag] = (r.by_plan[tag] or 0) + 1
        else
          r.untagged = r.untagged + 1
        end
        r.removed = r.removed + 1
        if not args.dry_run then g.destroy() end
      end
    end
  end
  return r
end

-- Instantly build ghosts in an area (cheat; skips robots). Only when explicitly asked. Debug ops only.
local function instant_build(args)
  local surface = get_surface(args)
  local n = 0
  for _, g in pairs(surface.find_entities_filtered{area = area_from(args, 16), type = "entity-ghost", force = get_force(args)}) do
    local _, e = g.revive{raise_revive = true}
    if e then n = n + 1 end
  end
  return {built = n}
end
api.register_debug("instant_build", instant_build)

---------------------------------------------------------------------------
-- blueprints
---------------------------------------------------------------------------

local function with_blueprint(str, fn)
  local inv = game.create_inventory(1)
  local ok, res = pcall(function()
    local stack = inv[1]
    local code = stack.import_stack(str)
    if code == 1 then error("blueprint import failed") end
    if stack.is_blueprint_book then
      local book = stack.get_inventory(defines.inventory.item_main)
      local idx = stack.active_index or 1
      if not (book and book[idx] and book[idx].valid_for_read) then error("empty blueprint book") end
      stack = book[idx]
    end
    if not stack.is_blueprint then error("not a blueprint") end
    return fn(stack, code)
  end)
  inv.destroy()
  if not ok then error(res, 0) end
  return res
end

function ops.blueprint_build(args)
  local surface, force = get_surface(args), get_force(args)
  local p = pos_of(args.position) or {x = args.x, y = args.y}
  return with_blueprint(args.blueprint, function(bp, code)
    local ghosts = bp.build_blueprint{surface = surface, force = force, position = p,
      direction = parse_dir(args.direction), build_mode = defines.build_mode[args.mode or "forced"],
      skip_fog_of_war = false, raise_built = true}
    local plan = plan_id(args)
    for _, g in pairs(ghosts) do
      if g.valid and g.type == "entity-ghost" then
        local t = g.tags or {}
        t.claude_plan = plan
        g.tags = t
      end
    end
    return {ghosts = #ghosts, import_warnings = (code == -1)}
  end)
end

function ops.blueprint_capture(args)
  local surface, force = get_surface(args), get_force(args)
  local inv = game.create_inventory(1)
  local ok, res = pcall(function()
    local st = inv[1]
    st.set_stack{name = "blueprint"}
    st.create_blueprint{surface = surface, force = force, area = area_from(args, 8)}
    if args.label then st.label = args.label end
    local n = st.get_blueprint_entity_count()
    if n == 0 then error("no entities in that area") end
    return {entities = n, blueprint = st.export_stack()}
  end)
  inv.destroy()
  if not ok then error(res, 0) end
  return res
end

-- Decode a blueprint in-engine: entity list + tile footprint + overlap check.
function ops.blueprint_inspect(args)
  return with_blueprint(args.blueprint, function(bp)
    local ents = bp.get_blueprint_entities() or {}
    local out, occ, overlaps, unknown = {}, {}, {}, {}
    for _, e in pairs(ents) do
      local proto = prototypes.entity[e.name]
      out[#out + 1] = {n = e.entity_number, name = e.name, x = e.position.x, y = e.position.y,
        dir = e.direction and dir_names[e.direction] or nil, recipe = e.recipe}
      if not proto then
        unknown[#unknown + 1] = e.name
      else
        local cb = proto.collision_box
        local d = e.direction or 0
        local w, h = cb.right_bottom.x - cb.left_top.x, cb.right_bottom.y - cb.left_top.y
        if d == defines.direction.east or d == defines.direction.west then w, h = h, w end
        local x0 = math.floor(e.position.x - w / 2 + 0.05)
        local y0 = math.floor(e.position.y - h / 2 + 0.05)
        local x1 = math.ceil(e.position.x + w / 2 - 0.05) - 1
        local y1 = math.ceil(e.position.y + h / 2 - 0.05) - 1
        local layer = (e.name:find("underground") or e.name:find("pipe%-to%-ground")) and "u" or "g"
        for ty = y0, y1 do
          for tx = x0, x1 do
            local key = tx .. "," .. ty
            local prev = occ[key]
            if prev and prev ~= e.entity_number and #overlaps < 20 then
              overlaps[#overlaps + 1] = {tile = key, a = prev, b = e.entity_number}
            end
            occ[key] = e.entity_number
          end
        end
      end
    end
    return {label = bp.label, count = #out, entities = out, overlaps = overlaps, unknown_names = unknown}
  end)
end

function ops.help()
  local names = {}
  for k, _ in pairs(ops) do names[#names + 1] = k end
  table.sort(names)
  local tnames = {}
  for k, _ in pairs(api.agent_tasks) do tnames[#tnames + 1] = k end
  table.sort(tnames)
  return {ops = names, agent_task_types = tnames}
end

commands.add_command("claude", "Claude bridge API (JSON). Used over RCON.", function(cmd)
  if cmd.player_index then
    local p = game.get_player(cmd.player_index)
    if not p.admin then p.print("claude: admin only"); return end
  end
  local ok, req = pcall(helpers.json_to_table, cmd.parameter or "{}")
  local out
  if not ok or type(req) ~= "table" then
    out = {ok = false, error = "bad json"}
  else
    out = api.call(req.op, req.args)
  end
  local s = helpers.table_to_json(out)
  if cmd.player_index then
    game.get_player(cmd.player_index).print(s)
  else
    rcon.print(s)
  end
end)

return api
