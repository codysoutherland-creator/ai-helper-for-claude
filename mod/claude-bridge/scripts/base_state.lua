-- base_state {x?, y?, radius?, surface?, force?}: read-only snapshot for the starter-base planner v2
-- (docs/design/PLANNER-v2.md 4.1): the player's and the helper's inventories, chests and machines of the force in
-- range (status, fuel, input/output), belt/pole counts, production (10 min) and research. No player names.

local api = require("scripts.api")
local util = require("scripts.util")
local inv_contents, pos_out, get_surface, get_force, status_names, first_player =
  util.inv_contents, util.pos_out, util.get_surface, util.get_force, util.status_names, util.first_player

local MAX_RADIUS = 300
local MAX_MACHINES = 2000
local MAX_CHESTS = 300
local MACHINE_TYPES = {"furnace", "mining-drill", "boiler", "generator", "offshore-pump", "lab", "assembling-machine",
  "inserter", "burner-generator", "reactor"}

local function main_inv(entity)
  local ok, inv = pcall(function() return entity.get_main_inventory() end)
  return ok and inv and inv_contents(inv) or {}
end

local function inv_of(e, id)
  local ok, inv = pcall(e.get_inventory, id)
  if ok and inv then return inv_contents(inv) end
  return nil
end

local function player_of(force, surface)
  local best
  for _, p in pairs(force.players) do
    if p.surface == surface then
      if p.connected and p.character then return p end
      best = best or p
    end
  end
  if best then return best end
  local fp = first_player()
  return fp and fp.force == force and fp or nil
end

local function machine_row(e)
  local o = {unit = e.unit_number, name = e.name, type = e.type, x = e.position.x, y = e.position.y,
    dir = e.direction}
  local ok, st = pcall(function() return e.status end)
  if ok and st then o.status = status_names[st] end
  if e.burner then
    o.burner = true
    local fi = e.get_fuel_inventory()
    if fi then
      local c = inv_contents(fi)
      local n, item = 0, nil
      for k, v in pairs(c) do n = n + v; item = k end
      o.fuel, o.fuel_item = n, item
    end
  end
  if e.type == "furnace" or e.type == "assembling-machine" then
    o.input = inv_of(e, defines.inventory.crafter_input)
    o.output = inv_of(e, defines.inventory.crafter_output)
    local rok, r = pcall(e.get_recipe)
    if rok and r then o.recipe = r.name end
  elseif e.type == "lab" then
    o.input = inv_of(e, defines.inventory.lab_input)
  end
  if e.type ~= "inserter" and e.electric_network_id then o.network = e.electric_network_id end
  return o
end

api.register("base_state", function(args)
  local surface = get_surface(args)
  local force = get_force(args)
  local p = player_of(force, surface)
  local cx = tonumber(args.x) or (p and p.position.x) or 0
  local cy = tonumber(args.y) or (p and p.position.y) or 0
  local r = math.min(MAX_RADIUS, math.max(8, tonumber(args.radius) or 150))
  local area = {{cx - r, cy - r}, {cx + r, cy + r}}
  local out = {tick = game.tick, surface = surface.name, center = {x = cx, y = cy}, radius = r}
  local s = settings.global["claude-helper-use-player-items"]
  out.settings = {use_player_items = s == nil or s.value and true or false}
  if p then
    out.player = {pos = pos_out(p.position), main = p.character and main_inv(p.character) or main_inv(p),
      connected = p.connected}
  end
  local a = storage.agent
  if a and a.entity and a.entity.valid then
    out.helper = {pos = pos_out(a.entity.position), main = main_inv(a.entity), stats = a.stats}
  end
  local chests = {}
  for _, c in pairs(surface.find_entities_filtered{area = area, force = force, type = {"container", "logistic-container"}}) do
    if #chests >= MAX_CHESTS then break end
    chests[#chests + 1] = {unit = c.unit_number, name = c.name, x = c.position.x, y = c.position.y,
      contents = inv_of(c, defines.inventory.chest) or {}}
  end
  out.chests = chests
  local machines, n_more = {}, 0
  for _, e in pairs(surface.find_entities_filtered{area = area, force = force, type = MACHINE_TYPES}) do
    if #machines < MAX_MACHINES then
      local ok, row = pcall(machine_row, e)
      if ok then machines[#machines + 1] = row end
    else
      n_more = n_more + 1
    end
  end
  out.machines = machines
  if n_more > 0 then out.machines_truncated = n_more end
  out.belts = {count = surface.count_entities_filtered{area = area, force = force,
    type = {"transport-belt", "underground-belt", "splitter"}}}
  local nets, npoles = {}, 0
  for _, pole in pairs(surface.find_entities_filtered{area = area, force = force, type = "electric-pole"}) do
    npoles = npoles + 1
    local id = pole.electric_network_id
    if id then
      local k = tostring(id)
      nets[k] = nets[k] or {poles = 0}
      nets[k].poles = nets[k].poles + 1
    end
  end
  out.poles = {count = npoles, networks = nets}
  local pok, prod = pcall(api.ops.production, {surface = surface.name, force = force.name, window = "10m", limit = 40})
  out.production = {window = "10m", per_min = pok and prod.items_produced or {}}
  local done = {}
  for name, t in pairs(force.technologies) do
    if t.researched then done[#done + 1] = name end
  end
  table.sort(done)
  local queue = {}
  for _, t in pairs(force.research_queue or {}) do queue[#queue + 1] = t.name end
  out.research = {done = done, current = force.current_research and force.current_research.name or nil, queue = queue}
  return out
end)

return {}
