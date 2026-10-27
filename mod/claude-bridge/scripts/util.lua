-- Shared helpers (positions, players, entity summaries).

local util = {}
util.CLAUDE_COLOR = {r = 0.85, g = 0.47, b = 0.34}

local function round(v, p)
  local m = 10 ^ (p or 1)
  return math.floor(v * m + 0.5) / m
end

local function pos_of(t)
  if not t then return nil end
  if t.x then return {x = t.x, y = t.y} end
  if t[1] then return {x = t[1], y = t[2]} end
  return nil
end

local function pos_out(p)
  return {x = round(p.x, 1), y = round(p.y, 1)}
end

local function dist(a, b)
  local dx, dy = a.x - b.x, a.y - b.y
  return math.sqrt(dx * dx + dy * dy)
end

local function first_player()
  for _, p in pairs(game.connected_players) do return p end
  for _, p in pairs(game.players) do return p end
  return nil
end

local function get_player(args)
  if args.player then
    local p = game.get_player(args.player)
    if not p then error("no player " .. tostring(args.player)) end
    return p
  end
  return first_player()
end

local function get_surface(args)
  if args.surface then
    local s = game.get_surface(args.surface)
    if not s then error("no surface " .. tostring(args.surface)) end
    return s
  end
  local p = first_player()
  if p then return p.surface end
  return game.get_surface("nauvis")
end

local function get_force(args)
  return game.forces[args.force or "player"]
end

local function parse_dir(d)
  if d == nil then return defines.direction.north end
  if type(d) == "number" then return d end
  local v = defines.direction[d]
  if v == nil then error("bad direction " .. tostring(d)) end
  return v
end

local dir_names = {}
for k, v in pairs(defines.direction) do dir_names[v] = k end

local status_names = {}
for k, v in pairs(defines.entity_status) do status_names[v] = k end

local function area_from(args, default_radius)
  if args.area then
    local a = args.area
    local lt = pos_of(a.left_top or a[1])
    local rb = pos_of(a.right_bottom or a[2])
    return {left_top = lt, right_bottom = rb}
  end
  local c = pos_of(args.position) or (args.x and {x = args.x, y = args.y})
  if not c then
    local p = first_player()
    c = p and p.position or {x = 0, y = 0}
  end
  local r = args.radius or default_radius or 16
  return {left_top = {x = c.x - r, y = c.y - r}, right_bottom = {x = c.x + r, y = c.y + r}}
end

local function contents_to_map(list, limit)
  local out, n = {}, 0
  for _, it in pairs(list or {}) do
    local key = it.name
    if it.quality and it.quality ~= "normal" then key = key .. "@" .. it.quality end
    out[key] = (out[key] or 0) + it.count
    n = n + 1
    if limit and n >= limit then break end
  end
  return out
end

local function inv_contents(inv)
  if not (inv and inv.valid) then return nil end
  return contents_to_map(inv.get_contents())
end

-- Find the most relevant entity at/near a position.
local function entity_at(surface, args, extra_filter)
  local p = pos_of(args.position) or {x = args.x, y = args.y}
  if not p.x then error("position required") end
  local f = {position = p, radius = args.radius or 0.6}
  if args.name then f.name = args.name end
  if extra_filter then for k, v in pairs(extra_filter) do f[k] = v end end
  local found = surface.find_entities_filtered(f)
  local best, bestd
  for _, e in pairs(found) do
    if e.type ~= "character" or args.name then
      local rank = (e.type == "resource") and 100 or 0
      local d = dist(e.position, p) + rank
      if not best or d < bestd then best, bestd = e, d end
    end
  end
  return best
end

local function entity_brief(e, detail)
  local o = {name = e.name, type = e.type, pos = pos_out(e.position)}
  if e.type == "entity-ghost" then o.ghost_of = e.ghost_name end
  if e.supports_direction then o.dir = dir_names[e.direction] end
  local ok, st = pcall(function() return e.status end)
  if ok and st then o.status = status_names[st] end
  if e.type == "assembling-machine" or e.type == "furnace" or e.type == "rocket-silo" then
    local r = e.get_recipe()
    if r then o.recipe = r.name end
  end
  if e.type == "resource" then o.amount = e.amount end
  if detail then
    if e.type == "assembling-machine" or e.type == "furnace" then
      o.progress = round(e.crafting_progress, 2)
      o.input = inv_contents(e.get_inventory(defines.inventory.crafter_input))
      o.output = inv_contents(e.get_inventory(defines.inventory.crafter_output))
    elseif e.type == "container" or e.type == "logistic-container" then
      o.contents = inv_contents(e.get_inventory(defines.inventory.chest))
    elseif e.type == "inserter" then
      o.pickup = pos_out(e.pickup_position)
      o.drop = pos_out(e.drop_position)
      if e.held_stack and e.held_stack.valid_for_read then o.holding = e.held_stack.name end
    elseif e.type == "transport-belt" or e.type == "underground-belt" or e.type == "splitter" then
      local lanes = {}
      for i = 1, e.get_max_transport_line_index() do
        lanes[i] = contents_to_map(e.get_transport_line(i).get_contents())
      end
      o.lanes = lanes
    elseif e.type == "mining-drill" then
      local t = e.mining_target
      if t then o.mining = t.name; o.remaining = t.amount end
    elseif e.type == "lab" then
      o.packs = inv_contents(e.get_inventory(defines.inventory.lab_input))
    end
    local fuel = e.get_fuel_inventory and e.get_fuel_inventory()
    if fuel then o.fuel = inv_contents(fuel) end
    if e.prototype.electric_energy_source_prototype then
      o.powered = e.is_connected_to_electric_network()
    end
  end
  return o
end

util.round = round
util.pos_of = pos_of
util.pos_out = pos_out
util.dist = dist
util.first_player = first_player
util.get_player = get_player
util.get_surface = get_surface
util.get_force = get_force
util.parse_dir = parse_dir
util.dir_names = dir_names
util.status_names = status_names
util.area_from = area_from
util.contents_to_map = contents_to_map
util.inv_contents = inv_contents
util.entity_at = entity_at
util.entity_brief = entity_brief

---------------------------------------------------------------------------
-- locations players can actually find: [gps] chat pins + world/map markers
---------------------------------------------------------------------------

local NUM = "%-?%d+%.?%d*"

-- Append a clickable [gps=x,y,surface] pin after coordinates in text ("x=-40, y=20", "(-40, 20)",
-- "at -40,20" / "near -40, 20"). Text that already has gps tags is left alone.
function util.linkify(text, surface)
  if type(text) ~= "string" or text:find("[gps=", 1, true) then return text end
  local s = surface and ("," .. surface) or ""
  local function pin(x, y) return "[gps=" .. x .. "," .. y .. s .. "]" end
  local function tag(whole, x, y) return whole .. " " .. pin(x, y) end
  text = text:gsub("([xX]%s*=%s*(" .. NUM .. ")%s*,%s*[yY]%s*=%s*(" .. NUM .. "))", tag)
  if text:find("[gps=", 1, true) then return text end
  text = text:gsub("(%((" .. NUM .. "),%s*(" .. NUM .. ")%))", tag)
  if text:find("[gps=", 1, true) then return text end
  text = text:gsub("(%f[%a][Aa][tr]o?u?n?d?%s+(" .. NUM .. "),%s*(" .. NUM .. "))", tag)
  text = text:gsub("(%f[%a][Nn]ear%s+(" .. NUM .. "),%s*(" .. NUM .. "))", tag)
  return text
end

-- Markdown -> plain chat sentences: headings/bullets/numbered items become sentences, **bold**/*italic*/`code`/
-- [text](url) lose their syntax. Rich-text tags like [gps=..] / [item=..] are kept.
local function plain_line(line)
  line = line:gsub("^%s+", ""):gsub("%s+$", "")
  if line:match("^[-*_][-*_%s]*$") and #line:gsub("%s", "") >= 3 then return "" end  -- horizontal rule
  line = line:gsub("^#+%s*", "")              -- heading
  line = line:gsub("^>%s*", "")               -- quote
  line = line:gsub("^[-*+\226\128\162]%s+", "") -- bullet (incl. U+2022)
  line = line:gsub("^%d+[.)]%s+", "")         -- numbered item
  line = line:gsub("%*%*(.-)%*%*", "%1"):gsub("__(.-)__", "%1")
  line = line:gsub("%*([^%*%s][^%*]-)%*", "%1")
  line = (" " .. line .. " "):gsub("([^%w_])_([^_%s][^_]-)_([^%w_])", "%1%2%3")  -- _italic_, not iron_ore
  line = line:gsub("`+", "")
  line = line:gsub("%[([^%]=]-)%]%(([^)]-)%)", "%1")
  line = line:gsub("%s+", " "):gsub("^%s+", ""):gsub("%s+$", "")
  return line
end

function util.strip_markdown(text)
  if type(text) ~= "string" then return text end
  local out = {}
  for raw in (text .. "\n"):gmatch("([^\n]*)\n") do
    local line = plain_line(raw)
    if line ~= "" then
      line = line:gsub("^%l", string.upper)
      if #out > 0 and not out[#out]:match("[%.!?:;,]$") then
        out[#out] = out[#out] .. "."
      end
      out[#out + 1] = line
    end
  end
  return table.concat(out, " ")
end

-- Draw a pulsing ring + label at a spot, visible in the world and on the map, for `seconds`.
function util.mark(surface, pos, text, seconds, color)
  local ttl = math.floor((seconds or 60) * 60)
  color = color or util.CLAUDE_COLOR
  for _, mode in ipairs({"game", "chart"}) do
    rendering.draw_circle{surface = surface, target = pos, radius = mode == "game" and 1.5 or 3, width = 4,
      color = color, filled = false, time_to_live = ttl, render_mode = mode, blink_interval = mode == "game" and 30 or 0}
    if text and text ~= "" then
      rendering.draw_text{surface = surface, target = {pos.x, pos.y - (mode == "game" and 2.2 or 4)}, text = text,
        color = color, scale = mode == "game" and 1.4 or 2, alignment = "center", time_to_live = ttl,
        render_mode = mode, use_rich_text = true}
    end
  end
end

return util
