-- Lua port of factorio_claude/blueprints.py generators (same layouts, wires, icons, version) so standard
-- blueprints are free: generated in-game and dropped into the player's cursor.

local api = require("scripts.api")

local blueprint_gen = {}

-- Factorio 2.1.23 map version used in blueprint JSON (exact as a double: < 2^53)
local VERSION = 2 * 2 ^ 48 + 2 ^ 32 + 23 * 2 ^ 16
local DIR = {north = 0, east = 4, south = 8, west = 12}
-- inserter direction = side it picks up FROM, so to drop toward X face the opposite way
local INSERTER_DIR_FOR_DROP = {north = DIR.south, south = DIR.north, east = DIR.west, west = DIR.east}
local POLE_REACH = {["small-electric-pole"] = 7.5, ["medium-electric-pole"] = 9, ["big-electric-pole"] = 30, substation = 18}
local POLE_COPPER = 5 -- defines.wire_connector_id.pole_copper

local RECIPES = {"iron-gear-wheel", "copper-cable", "electronic-circuit", "automation-science-pack",
  "logistic-science-pack", "inserter", "transport-belt", "pipe", "iron-stick", "firearm-magazine",
  "advanced-circuit", "engine-unit"}

-- ordered {id, title, params = {{name, title, type, default, min, max, choices}}}
blueprint_gen.KINDS = {
  {id = "smelter_column", title = "Smelter column", params = {
    {name = "count", title = "Furnaces", type = "int", default = 8, min = 1, max = 50},
    {name = "furnace", title = "Furnace", type = "choice", default = "stone-furnace",
      choices = {"stone-furnace", "steel-furnace", "electric-furnace"}}}},
  {id = "assembler_line", title = "Assembler line", params = {
    {name = "count", title = "Assemblers", type = "int", default = 6, min = 1, max = 50},
    {name = "recipe", title = "Recipe", type = "choice", default = "iron-gear-wheel", choices = RECIPES},
    {name = "machine", title = "Machine", type = "choice", default = "assembling-machine-1",
      choices = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}}}},
  {id = "mining_outpost", title = "Mining outpost", params = {
    {name = "rows", title = "Drill rows", type = "int", default = 4, min = 1, max = 50},
    {name = "drill", title = "Drill", type = "choice", default = "electric-mining-drill",
      choices = {"electric-mining-drill"}}}},
  {id = "solar_block", title = "Solar block", params = {
    {name = "pairs", title = "Strips", type = "int", default = 2, min = 1, max = 20},
    {name = "panel_rows", title = "Panel rows", type = "int", default = 4, min = 1, max = 30},
    {name = "accumulator_rows", title = "Accumulator rows", type = "int", default = 0, min = 0, max = 30}}},
}
local by_id = {}
for _, k in ipairs(blueprint_gen.KINDS) do by_id[k.id] = k end

-- hidden optional params (Python generator kwargs) -> required prototype type
local EXTRA = {belt = "transport-belt", inserter = "inserter", pole = "electric-pole"}

---------------------------------------------------------------------------
-- generators (1:1 with blueprints.py; entity order matters for numbering/wires)
---------------------------------------------------------------------------

local function inserter(name, x, y, drop)
  return {name = name, x = x, y = y, direction = INSERTER_DIR_FOR_DROP[drop]}
end

local gen = {}

function gen.smelter_column(p)
  local ents, furnace, belt, pole = {}, p.furnace, p.belt or "transport-belt", p.pole or "small-electric-pole"
  local size = furnace == "electric-furnace" and 3 or 2
  local ins = p.inserter or (furnace == "electric-furnace" and "inserter" or "burner-inserter")
  local electric = ins ~= "burner-inserter"
  local out_x = 2 + size + 1
  for i = 0, p.count - 1 do
    local y0 = i * size
    for dy = 0, size - 1 do
      ents[#ents + 1] = {name = belt, x = 0.5, y = y0 + dy + 0.5, direction = "north"}
      ents[#ents + 1] = {name = belt, x = out_x + 0.5, y = y0 + dy + 0.5, direction = "north"}
    end
    ents[#ents + 1] = inserter(ins, 1.5, y0 + 0.5, "east")
    ents[#ents + 1] = {name = furnace, x = 2 + size / 2, y = y0 + size / 2}
    ents[#ents + 1] = inserter(ins, 2 + size + 0.5, y0 + 0.5, "east")
    if electric then
      ents[#ents + 1] = {name = pole, x = 1.5, y = y0 + size - 0.5}
      ents[#ents + 1] = {name = pole, x = 2 + size + 0.5, y = y0 + size - 0.5}
    end
  end
  return ents
end

function gen.assembler_line(p)
  local ents, belt = {}, p.belt or "transport-belt"
  local ins, pole = p.inserter or "inserter", p.pole or "small-electric-pole"
  local second = p.second_input == true
  local off = second and 1 or 0
  for i = 0, p.count - 1 do
    local y0 = i * 3
    for dy = 0, 2 do
      ents[#ents + 1] = {name = belt, x = off + 0.5, y = y0 + dy + 0.5, direction = "north"}
      ents[#ents + 1] = {name = belt, x = off + 6.5, y = y0 + dy + 0.5, direction = "north"}
      if second then ents[#ents + 1] = {name = belt, x = -0.5, y = y0 + dy + 0.5, direction = "north"} end
    end
    ents[#ents + 1] = inserter(ins, off + 1.5, y0 + 1.5, "east")
    if second then ents[#ents + 1] = inserter("long-handed-inserter", off + 1.5, y0 + 2.5, "east") end
    ents[#ents + 1] = {name = p.machine, x = off + 3.5, y = y0 + 1.5, recipe = p.recipe}
    ents[#ents + 1] = inserter(ins, off + 5.5, y0 + 1.5, "east")
    ents[#ents + 1] = {name = pole, x = off + 1.5, y = y0 + 0.5}
    ents[#ents + 1] = {name = pole, x = off + 5.5, y = y0 + 0.5}
  end
  return ents
end

function gen.mining_outpost(p)
  local ents, belt, pole = {}, p.belt or "transport-belt", p.pole or "small-electric-pole"
  for i = 0, p.rows - 1 do
    local y = i * 3 + 1.5
    ents[#ents + 1] = {name = p.drill, x = 1.5, y = y, direction = "east"}
    ents[#ents + 1] = {name = p.drill, x = 5.5, y = y, direction = "west"}
    for dy = 0, 2 do
      ents[#ents + 1] = {name = belt, x = 3.5, y = i * 3 + dy + 0.5, direction = "north"}
    end
    if i % 2 == 0 then ents[#ents + 1] = {name = pole, x = 7.5, y = y} end
  end
  return ents
end

function gen.solar_block(p)
  local ents, pole = {}, p.pole or "medium-electric-pole"
  local height = p.panel_rows * 3 + p.accumulator_rows * 2
  for s = 0, p.pairs - 1 do
    local bx = s * 7
    for r = 0, p.panel_rows - 1 do
      ents[#ents + 1] = {name = "solar-panel", x = bx + 1.5, y = r * 3 + 1.5}
      ents[#ents + 1] = {name = "solar-panel", x = bx + 5.5, y = r * 3 + 1.5}
    end
    for r = 0, p.accumulator_rows - 1 do
      local y = p.panel_rows * 3 + r * 2 + 1
      ents[#ents + 1] = {name = "accumulator", x = bx + 1, y = y}
      ents[#ents + 1] = {name = "accumulator", x = bx + 6, y = y}
    end
    local y = 3.5
    while y - 3.5 < height do
      ents[#ents + 1] = {name = pole, x = bx + 3.5, y = math.min(y, height - 0.5)}
      y = y + 6
    end
  end
  return ents
end

---------------------------------------------------------------------------
-- make_blueprint (numbering, icons, auto pole wires)
---------------------------------------------------------------------------

local function pole_wires(out)
  local poles, wires = {}, {}
  for _, e in ipairs(out) do
    if POLE_REACH[e.name] then poles[#poles + 1] = e end
  end
  for i, a in ipairs(poles) do
    local cands = {}
    for j = 1, i - 1 do
      local b = poles[j]
      local dx, dy = b.position.x - a.position.x, b.position.y - a.position.y
      local d = (dx * dx + dy * dy) ^ 0.5
      if d <= math.min(POLE_REACH[a.name], POLE_REACH[b.name]) then cands[#cands + 1] = {d, b.entity_number} end
    end
    table.sort(cands, function(u, v) if u[1] ~= v[1] then return u[1] < v[1] end return u[2] < v[2] end)
    for k = 1, math.min(5, #cands) do
      wires[#wires + 1] = {cands[k][2], POLE_COPPER, a.entity_number, POLE_COPPER}
    end
  end
  return wires
end

local function make_blueprint(entities, label)
  local out, counts, order = {}, {}, {}
  for i, e in ipairs(entities) do
    local ent = {entity_number = i, name = e.name, position = {x = e.x, y = e.y}}
    local d = e.direction
    if type(d) == "string" then d = DIR[d] end
    if d and d ~= 0 then ent.direction = d end
    if e.recipe then ent.recipe = e.recipe end
    out[i] = ent
    if not counts[e.name] then counts[e.name] = 0; order[#order + 1] = e.name end
    counts[e.name] = counts[e.name] + 1
  end
  -- top 2 by count, ties in first-seen order (Python's stable sort)
  local rank = {}
  for i, n in ipairs(order) do rank[n] = i end
  table.sort(order, function(a, b)
    if counts[a] ~= counts[b] then return counts[a] > counts[b] end
    return rank[a] < rank[b]
  end)
  local icons = {}
  for i = 1, math.min(2, #order) do icons[i] = {signal = {type = "item", name = order[i]}, index = i} end
  local bp = {item = "blueprint", label = label, icons = icons, entities = out, version = VERSION}
  local wires = pole_wires(out)
  if #wires > 0 then bp.wires = wires end
  return {blueprint = bp}
end

---------------------------------------------------------------------------
-- params
---------------------------------------------------------------------------

local function kind_names()
  local t = {}
  for _, k in ipairs(blueprint_gen.KINDS) do t[#t + 1] = k.id end
  return table.concat(t, ", ")
end

local function proto_type(name)
  local p = type(name) == "string" and prototypes.entity[name]
  return p and p.type
end

-- -> clean params table, or nil, err. Ints are clamped; bad choices are errors.
function blueprint_gen.validate(kind, params)
  local k = by_id[kind or ""]
  if not k then return nil, "unknown blueprint kind '" .. tostring(kind) .. "'; options: " .. kind_names() end
  params = type(params) == "table" and params or {}
  local p = {}
  for _, d in ipairs(k.params) do
    local v = params[d.name]
    if d.type == "int" then
      v = math.floor(tonumber(v) or d.default)
      p[d.name] = math.max(d.min, math.min(d.max, v))
    else
      if v == nil or v == "" then v = d.default end
      local ok = false
      for _, c in ipairs(d.choices) do if c == v then ok = true end end
      if not ok then return nil, d.name .. " must be one of: " .. table.concat(d.choices, ", ") end
      p[d.name] = v
    end
  end
  for name, ptype in pairs(EXTRA) do
    local v = params[name]
    if v ~= nil and v ~= "" then
      if proto_type(v) ~= ptype then return nil, name .. ": '" .. tostring(v) .. "' isn't a valid " .. ptype end
      if name == "pole" and not POLE_REACH[v] then return nil, "pole: unsupported pole " .. v end
      p[name] = v
    end
  end
  p.second_input = params.second_input == true
  if kind == "assembler_line" then
    local r, m = prototypes.recipe[p.recipe], prototypes.entity[p.machine]
    if not (r and m) then return nil, "recipe or machine missing in this game" end
    local cats, fits = m.crafting_categories or {}, false
    for _, c in ipairs(r.categories) do if cats[c] then fits = true end end
    if not fits then return nil, p.machine .. " can't craft " .. p.recipe end
  end
  for _, n in pairs({p.furnace, p.machine, p.drill}) do
    if not prototypes.entity[n] then return nil, n .. " doesn't exist in this game" end
  end
  return p
end

-- -> blueprint_string, entity_count | nil, err
function blueprint_gen.build(kind, params)
  local p, err = blueprint_gen.validate(kind, params)
  if not p then return nil, err end
  local ents = gen[kind](p)
  local label = (type(params) == "table" and type(params.label) == "string" and params.label ~= "" and params.label)
    or kind:gsub("_", " ")
  local s = helpers.encode_string(helpers.table_to_json(make_blueprint(ents, label)))
  if not s then return nil, "blueprint encode failed" end
  return "0" .. s, #ents
end

-- -> ok, err
function blueprint_gen.to_cursor(player, kind, params)
  if not (player and player.valid) then return false, "no player" end
  local s, err = blueprint_gen.build(kind, params)
  if not s then return false, err end
  if not player.clear_cursor() then return false, "couldn't clear your cursor (inventory full?)" end
  local stack = player.cursor_stack
  if not stack then return false, "no cursor in this view" end
  if stack.import_stack(s) == 1 then return false, "blueprint import failed" end
  player.cursor_stack_temporary = true  -- vanishes when the cursor is cleared (no inventory clutter)
  return true
end

api.register("blueprint_generate", function(args)
  local s, n = blueprint_gen.build(args.kind, args.params or {})
  if not s then error(n, 0) end
  return {blueprint = s, entities = n}
end)

api.register("blueprint_kinds", function()
  return blueprint_gen.KINDS
end)

return blueprint_gen
