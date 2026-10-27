-- export_data: dump everything the Python planner needs (prototypes + force state) to
-- script-output/claude/data.json. Python side: factorio_claude/planner/gamedata.py.
-- Energy is exported in kW (prototype values are J/tick), fluid rates per second.

local api = require("scripts.api")
local util = require("scripts.util")

local export_data = {}

local function try(f, ...)
  local ok, v = pcall(f, ...)
  if ok then return v end
  return nil
end

-- Prototype attribute that may not exist for this type (2.x throws on wrong-type reads).
local function attr(p, k)
  return try(function() return p[k] end)
end

local function num(v)
  if type(v) ~= "number" or v ~= v or v == math.huge or v == -math.huge then return nil end
  return v
end

local function kw(v) v = num(v); return v and num(v * 60 / 1000) or nil end

local function keys(t)
  if not t then return nil end
  local r = {}
  for k in pairs(t) do r[#r + 1] = k end
  table.sort(r)
  return r
end

local function box(b)
  if not b then return nil end
  return {{b.left_top.x, b.left_top.y}, {b.right_bottom.x, b.right_bottom.y}}
end

local function vec(v)
  if not v then return nil end
  return {v.x or v[1], v.y or v[2]}
end

local function effect(e)
  if not e then return nil end
  local r = {}
  for k, v in pairs(e) do r[k] = num(v) end
  return r
end

local function energy_source(p)
  local b = attr(p, "burner_prototype")
  if b then
    return {type = "burner", fuel_categories = keys(b.fuel_categories), effectivity = b.effectivity,
      fuel_slots = b.fuel_inventory_size, emissions_per_joule = b.emissions_per_joule}
  end
  local e = attr(p, "electric_energy_source_prototype")
  if e then
    return {type = "electric", drain_kw = kw(e.drain), usage_priority = e.usage_priority,
      buffer_capacity = num(e.buffer_capacity), emissions_per_joule = e.emissions_per_joule}
  end
  if attr(p, "heat_energy_source_prototype") then return {type = "heat"} end
  if attr(p, "fluid_energy_source_prototype") then return {type = "fluid"} end
  if attr(p, "void_energy_source_prototype") then return {type = "void"} end
  return nil
end

local function fluidboxes(p)
  local fbs = attr(p, "fluidbox_prototypes")
  if not fbs or #fbs == 0 then return nil end
  -- conditional: drill input boxes only connect when mining a fluid-requiring resource; some machines
  -- only connect with a fluid recipe set
  local off = attr(p, "fluid_boxes_off_when_no_fluid_recipe")
  local r = {}
  for i, fb in ipairs(fbs) do
    local conns = {}
    for _, c in ipairs(fb.pipe_connections or {}) do
      local pos = {}
      for k, v in ipairs(c.positions or {}) do pos[k] = vec(v) end
      conns[#conns + 1] = {type = c.connection_type, flow = c.flow_direction, direction = c.direction,
        positions = pos, max_underground_distance = c.max_underground_distance,
        category = c.connection_category}
    end
    r[#r + 1] = {index = fb.index or i, production_type = fb.production_type,
      filter = fb.filter and fb.filter.name or nil, volume = try(fb.get_volume), connections = conns,
      conditional = off or (p.type == "mining-drill" and fb.production_type ~= "output") or nil}
  end
  return r
end

local function entity_data(p)
  local d = {type = p.type, collision_box = box(p.collision_box), tile_width = p.tile_width,
    tile_height = p.tile_height, hidden = p.hidden}
  local flags = attr(p, "flags") or {}
  d.flags = keys(flags)
  d.energy_source = energy_source(p)
  d.energy_usage_kw = kw(attr(p, "energy_usage"))
  d.max_energy_usage_kw = kw(try(p.get_max_energy_usage))
  d.max_energy_production_kw = kw(try(p.get_max_energy_production))
  if d.max_energy_usage_kw == 0 then d.max_energy_usage_kw = nil end
  if d.max_energy_production_kw == 0 then d.max_energy_production_kw = nil end
  local cats = attr(p, "crafting_categories")
  if cats then
    d.crafting_categories = keys(cats)
    d.crafting_speed = try(p.get_crafting_speed)
  end
  local rc = attr(p, "resource_categories")
  if rc then
    d.resource_categories = keys(rc)
    d.mining_speed = attr(p, "mining_speed")
    d.mining_drill_radius = try(p.get_mining_drill_radius)
    d.resource_drain_rate_percent = attr(p, "resource_drain_rate_percent")
    d.uses_force_mining_productivity_bonus = attr(p, "uses_force_mining_productivity_bonus")
    d.vector_to_place_result = vec(attr(p, "vector_to_place_result"))
  end
  d.module_slots = attr(p, "module_inventory_size")
  if d.module_slots == 0 then d.module_slots = nil end
  d.allowed_effects = attr(p, "allowed_effects")
  d.allowed_module_categories = keys(attr(p, "allowed_module_categories"))
  local er = attr(p, "effect_receiver")
  if er then
    d.effect_receiver = {base_effect = effect(er.base_effect), uses_module_effects = er.uses_module_effects,
      uses_beacon_effects = er.uses_beacon_effects, uses_surface_effects = er.uses_surface_effects}
  end
  d.fluidboxes = fluidboxes(p)
  local bs = attr(p, "belt_speed")
  if bs then
    d.belt_speed = bs
    d.lane_items_per_s = bs * 60 * 4  -- 4 items per lane per tile
    d.belt_items_per_s = bs * 60 * 8
  end
  d.max_underground_distance = attr(p, "max_underground_distance")
  if p.type == "inserter" then
    d.inserter = {rotation_speed = try(p.get_inserter_rotation_speed),
      extension_speed = try(p.get_inserter_extension_speed),
      pickup = vec(attr(p, "inserter_pickup_position")), drop = vec(attr(p, "inserter_drop_position")),
      stack_size_bonus = attr(p, "inserter_stack_size_bonus"), bulk = attr(p, "bulk"),
      chases_belt_items = attr(p, "inserter_chases_belt_items")}
  end
  if p.type == "electric-pole" then
    d.supply_area_distance = try(p.get_supply_area_distance)
    d.max_wire_distance = try(p.get_max_wire_distance)
  end
  if p.type == "beacon" then
    d.supply_area_distance = try(p.get_supply_area_distance)
    d.distribution_effectivity = attr(p, "distribution_effectivity")
    d.distribution_effectivity_bonus_per_quality_level = attr(p, "distribution_effectivity_bonus_per_quality_level")
    d.profile = attr(p, "profile")
    d.beacon_counter = attr(p, "beacon_counter")
  end
  if p.type == "offshore-pump" or p.type == "pump" then
    local s = try(p.get_pumping_speed)
    d.pumping_speed_per_s = s and s * 60 or nil
  end
  if p.type == "boiler" then d.target_temperature = attr(p, "target_temperature") end
  if p.type == "generator" or p.type == "fusion-generator" then
    local u = try(p.get_fluid_usage_per_tick)
    d.fluid_usage_per_s = u and u * 60 or nil
    d.maximum_temperature = num(attr(p, "maximum_temperature"))
    d.effectivity = attr(p, "effectivity")
  end
  if p.type == "lab" then
    d.researching_speed = try(p.get_researching_speed)
    d.lab_inputs = attr(p, "lab_inputs")
  end
  local em = attr(p, "emissions_per_second")
  if em and next(em) then
    d.emissions_per_minute = {}
    for k, v in pairs(em) do d.emissions_per_minute[k] = v * 60 end
  end
  local itp = attr(p, "items_to_place_this")
  if itp and #itp > 0 then
    d.items_to_place_this = {}
    for _, it in ipairs(itp) do d.items_to_place_this[#d.items_to_place_this + 1] = {it.name, it.count} end
  end
  local sc = attr(p, "surface_conditions")
  if sc and #sc > 0 then d.surface_conditions = sc end
  local nu = attr(p, "next_upgrade")
  d.next_upgrade = nu and nu.name or nil
  return d
end

local function resource_data(p)
  local m = p.mineable_properties or {}
  return {category = attr(p, "resource_category"), mining_time = m.mining_time, products = m.products,
    required_fluid = m.required_fluid, fluid_amount = m.fluid_amount,
    infinite = attr(p, "infinite_resource"), normal_amount = attr(p, "normal_resource_amount"),
    minimum_amount = attr(p, "minimum_resource_amount")}
end

local function recipe_data(p, fr)
  local d = {categories = p.categories, energy = p.energy, ingredients = p.ingredients, products = p.products,
    main_product = p.main_product and p.main_product.name or nil, hidden = p.hidden,
    hidden_from_player_crafting = p.hidden_from_player_crafting, allowed_effects = p.allowed_effects,
    allowed_module_categories = keys(p.allowed_module_categories), maximum_productivity = p.maximum_productivity,
    surface_conditions = p.surface_conditions, can_set_quality = attr(p, "can_set_quality"),
    emissions_multiplier = p.emissions_multiplier}
  if fr then
    d.enabled = fr.enabled
    local pb = num(fr.productivity_bonus)
    if pb and pb ~= 0 then d.productivity_bonus = pb end
  else
    d.enabled = p.enabled
  end
  return d
end

local function item_data(p)
  local d = {type = p.type, stack_size = p.stack_size, weight = num(attr(p, "weight")), hidden = p.hidden,
    subgroup = p.subgroup and p.subgroup.name or nil}
  local fv = attr(p, "fuel_value")
  if fv and fv > 0 then
    d.fuel_value = fv
    d.fuel_categories = attr(p, "fuel_categories")
    local br = attr(p, "burnt_result")
    d.burnt_result = br and br.name or nil
  end
  local pr = attr(p, "place_result")
  d.place_result = pr and pr.name or nil
  local pt = attr(p, "place_as_tile_result")
  d.place_as_tile = pt and pt.result and pt.result.name or nil
  if p.type == "module" then
    d.module_effects = effect(attr(p, "module_effects"))
    d.module_category = attr(p, "category")
    d.tier = attr(p, "tier")
  end
  local sr = attr(p, "spoil_result")
  if sr then d.spoil_result = sr.name; d.spoil_ticks = try(p.get_spoil_ticks) end
  return d
end

local QUALITY_FIELDS = {"default_multiplier", "crafting_machine_speed_multiplier", "crafting_machine_energy_usage_multiplier",
  "crafting_machine_module_slots_bonus", "mining_drill_module_slots_bonus", "mining_drill_mining_radius_bonus",
  "beacon_module_slots_bonus", "beacon_power_usage_multiplier", "beacon_supply_area_distance_bonus",
  "electric_pole_supply_area_distance_bonus", "electric_pole_wire_reach_bonus", "inserter_speed_multiplier",
  "lab_research_speed_multiplier", "module_speed_multiplier", "module_productivity_multiplier",
  "module_quality_multiplier", "module_consumption_multiplier", "module_pollution_multiplier",
  "inventory_size_multiplier"}

local function quality_data(q, force)
  local d = {level = q.level, next = q.next and q.next.name or nil, next_probability = q.next_probability,
    chain_probability = attr(q, "chain_probability"), hidden = q.hidden,
    unlocked = try(force.is_quality_unlocked, q.name) or false}
  for _, k in ipairs(QUALITY_FIELDS) do d[k] = num(attr(q, k)) end
  return d
end

local function tech_data(t)
  local effects = {}
  for _, e in ipairs(t.prototype.effects or {}) do
    local c = {}
    for k, v in pairs(e) do if type(v) ~= "table" and type(v) ~= "userdata" then c[k] = v end end
    effects[#effects + 1] = c
  end
  local ings = {}
  for _, i in ipairs(t.research_unit_ingredients or {}) do ings[#ings + 1] = {i.name, i.amount} end
  return {researched = t.researched, enabled = t.enabled, level = t.level, upgrade = t.upgrade,
    prerequisites = keys(t.prerequisites), unit_count = try(function() return t.research_unit_count end),
    unit_energy = t.research_unit_energy, ingredients = ings, effects = effects,
    research_trigger = try(function() return t.prototype.research_trigger end)}
end

local BONUSES = {"mining_drill_productivity_bonus", "inserter_stack_size_bonus", "bulk_inserter_capacity_bonus",
  "belt_stack_size_bonus", "laboratory_speed_modifier", "laboratory_productivity_bonus",
  "worker_robots_speed_modifier", "worker_robots_storage_bonus", "worker_robots_battery_modifier",
  "manual_crafting_speed_modifier", "manual_mining_speed_modifier", "beacon_distribution_modifier",
  "character_inventory_slots_bonus"}

function export_data.build(force)
  local data = {game_version = helpers.game_version, mods = script.active_mods, tick = game.tick,
    force = force.name, entities = {}, resources = {}, recipes = {}, items = {}, fluids = {},
    qualities = {}, technologies = {}, bonuses = {}, surfaces = {}}
  for name, p in pairs(prototypes.entity) do
    local itp = attr(p, "items_to_place_this")
    if (itp and #itp > 0) or p.type == "character" then
      data.entities[name] = entity_data(p)
    elseif p.type == "resource" then
      data.resources[name] = resource_data(p)
    end
  end
  for name, p in pairs(prototypes.recipe) do
    if not p.parameter then data.recipes[name] = recipe_data(p, force.recipes[name]) end
  end
  for name, p in pairs(prototypes.item) do
    if not p.parameter then data.items[name] = item_data(p) end
  end
  for name, p in pairs(prototypes.fluid) do
    if not p.parameter then
      data.fluids[name] = {default_temperature = num(p.default_temperature), max_temperature = num(p.max_temperature),
        heat_capacity = num(p.heat_capacity), fuel_value = num(p.fuel_value), hidden = p.hidden}
    end
  end
  for name, q in pairs(prototypes.quality) do
    if name ~= "quality-unknown" then data.qualities[name] = quality_data(q, force) end
  end
  -- engine reference rolls (lets tests check the Python quality model)
  local nq = prototypes.quality["normal"]
  if nq then data.quality_roll_samples = {["0.1"] = try(nq.get_roll_chances, 0.1, force)} end
  for name, t in pairs(force.technologies) do data.technologies[name] = tech_data(t) end
  local queue = {}
  for _, t in ipairs(force.research_queue or {}) do queue[#queue + 1] = t.name end
  data.research = {current = force.current_research and force.current_research.name or nil, queue = queue}
  for _, k in ipairs(BONUSES) do data.bonuses[k] = num(attr(force, k)) end
  local props = {}
  for name in pairs(prototypes.surface_property) do props[#props + 1] = name end
  for _, s in pairs(game.surfaces) do
    local sp = {}
    for _, n in ipairs(props) do sp[n] = num(try(s.get_property, n)) end
    local res = {}
    local mg = try(function() return s.map_gen_settings end)
    local ents = mg and mg.autoplace_settings and mg.autoplace_settings.entity and mg.autoplace_settings.entity.settings
    for k in pairs(ents or {}) do
      local ep = prototypes.entity[k]
      if ep and ep.type == "resource" then res[#res + 1] = k end
    end
    table.sort(res)
    data.surfaces[s.name] = {planet = s.planet and s.planet.name or nil,
      platform = s.platform and s.platform.name or nil, properties = sp, resources = res}
  end
  data.utility = {rocket_lift_weight = num(try(function() return prototypes.utility_constants.default_rocket_lift_weight end)),
    max_underground_belt_distance = prototypes.max_underground_belt_distance,
    max_pipe_to_ground_distance = prototypes.max_pipe_to_ground_distance}
  local mods = {}
  for k, v in pairs(script.active_mods) do mods[#mods + 1] = k .. "=" .. v end
  table.sort(mods)
  data.version_key = helpers.game_version .. ";" .. table.concat(mods, ",")
  return data
end

function export_data.write(args)
  local force = util.get_force(args or {})
  if not force then error("no force " .. tostring(args and args.force)) end
  local data = export_data.build(force)
  local path = "claude/data.json"
  helpers.write_file(path, helpers.table_to_json(data), false)
  return {path = path, version_key = data.version_key, tick = game.tick,
    counts = {entities = table_size(data.entities), recipes = table_size(data.recipes), items = table_size(data.items),
      technologies = table_size(data.technologies)}}
end

api.register("export_data", export_data.write)

return export_data
