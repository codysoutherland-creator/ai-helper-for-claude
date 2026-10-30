-- Plan previews + approval (M1). Ops: plan_preview / plan_clear_preview / plan_apply / plan_list / plan_drop / plan_get.
-- storage.plans[id] = {id, title, variant, stage, group, lines (summary), warnings, validation, color, force, surface,
--   entities = {{name, x, y, dir, recipe?, extra?}...}, deconstruct = {{x, y, name}...}, bbox, tick, seq,
--   status = "proposed"|"applying"|"applied", result (last apply), shown (overlay drawn), objs (LuaRenderObjects),
--   draw / apply (job state while a big plan is drawn / applied over several ticks)}
-- The overlay is plain rendering objects (no per-tick work): merged same-kind tile rectangles (game + map view),
-- outlines/icons for big entities, flow arrows for belts/inserters, red boxes for obstacles to clear.
-- Every ghost plan_apply places carries tags.claude_plan = id and has no last_user, so clear_claude_ghosts finds it.
-- Planner v2 rebuilds: p.removes = {{name, x, y, unit?}...} existing entities of the force the plan dismantles. They are
-- drawn red ("To dismantle (yours)"); approve orders deconstruction of exactly those (p.removing[unit] = row), places
-- the ghosts that don't overlap them and defers the rest (p.deferred) until the helper (task "dismantle") or the
-- player has removed them. Reject / Dismiss / plan_drop / clear_claude_ghosts {plan} cancel every remaining mark.

local api = require("scripts.api")
local util = require("scripts.util")
local agent = require("scripts.agent")

local plans = {}
plans.on_change = function() end  -- gui.lua overrides it (refresh the Plans tab)

local MAX_BLOCKED = 50    -- blocked entries listed in an apply result
local ARROW_LIMIT = 20000 -- skip flow arrows beyond this
local SLICE = 1500        -- entities drawn / applied per tick (~25 ms); smaller plans are done at once
local CLEARABLE = {"tree", "simple-entity"}  -- what plan_apply may order deconstructed
local MAX_REMOVES = 2000  -- existing entities one plan may dismantle

-- legend: kind -> {caption, r, g, b}; fills are drawn translucent
plans.LEGEND = {
  {"belt", "Belts", 1, 0.82, 0.15},
  {"inserter", "Inserters", 0.35, 0.95, 0.35},
  {"mining", "Drills", 0.8, 0.5, 0.25},
  {"smelting", "Furnaces", 1, 0.45, 0.1},
  {"crafting", "Assemblers / labs", 0.3, 0.6, 1},
  {"power", "Power", 0.8, 0.4, 1},
  {"fluid", "Pipes / pumps", 0.2, 0.9, 0.95},
  {"storage", "Chests", 0.75, 0.75, 0.75},
  {"other", "Other", 1, 1, 1},
  {"clear", "To clear (trees/rocks)", 1, 0.2, 0.2},
  {"remove", "To dismantle (yours)", 1, 0.05, 0.05},
}
local LEG = {}
for _, l in ipairs(plans.LEGEND) do LEG[l[1]] = l end

local KIND_OF = {
  ["transport-belt"] = "belt", ["underground-belt"] = "belt", splitter = "belt", loader = "belt",
  ["loader-1x1"] = "belt", ["linked-belt"] = "belt", ["lane-splitter"] = "belt",
  inserter = "inserter",
  ["mining-drill"] = "mining",
  furnace = "smelting",
  ["assembling-machine"] = "crafting", lab = "crafting", ["rocket-silo"] = "crafting",
  ["electric-pole"] = "power", boiler = "power", generator = "power", ["solar-panel"] = "power",
  accumulator = "power", reactor = "power", ["burner-generator"] = "power", ["fusion-reactor"] = "power",
  ["fusion-generator"] = "power", ["heat-pipe"] = "power", ["electric-energy-interface"] = "power",
  ["lightning-attractor"] = "power", beacon = "power",
  pipe = "fluid", ["pipe-to-ground"] = "fluid", pump = "fluid", ["offshore-pump"] = "fluid",
  ["storage-tank"] = "fluid",
  container = "storage", ["logistic-container"] = "storage", ["infinity-container"] = "storage",
}
-- directional kinds that get a flow arrow; inserters point at their drop side (direction = pickup side)
local ARROW = {["transport-belt"] = 0, ["underground-belt"] = 0, splitter = 0, loader = 0, ["loader-1x1"] = 0,
  inserter = 8, ["offshore-pump"] = 0, pump = 0}

local function init()
  storage.plans = storage.plans or {}
  storage.plan_seq = storage.plan_seq or 0
end

local function changed()
  local ok, err = pcall(plans.on_change)
  if not ok then log("claude-bridge plans on_change: " .. tostring(err)) end
end

local function rgba(l, a) return {l[3] * a, l[4] * a, l[5] * a, a} end  -- premultiplied

local function strip(s)
  s = tostring(s or "")
  return util.strip_markdown and util.strip_markdown(s) or s
end

-- summary: string (lines), list of strings, or {key = value}
local function lines_of(v)
  local out = {}
  if type(v) == "string" then
    for line in strip(v):gmatch("[^\n]+") do
      if line:match("%S") then out[#out + 1] = line end
    end
  elseif type(v) == "table" then
    if #v > 0 then
      for _, s in ipairs(v) do out[#out + 1] = strip(s) end
    else
      local keys = {}
      for k in pairs(v) do keys[#keys + 1] = k end
      table.sort(keys)
      for _, k in ipairs(keys) do
        local x = v[k]
        if type(x) == "table" then x = helpers.table_to_json(x) end
        out[#out + 1] = tostring(k) .. ": " .. strip(x)
      end
    end
  end
  return out
end

-- entity row: [name, x, y, dir, recipe?, extra?] or {name, x, y, direction|dir, recipe, extra}
-- extra may be a string (underground/loader type "input"/"output") or a table ({type=..., fuel=...}).
local function parse_entity(r)
  local name, x, y, dir, recipe, extra
  if r.name then
    name, x, y, dir, recipe, extra = r.name, r.x, r.y, r.direction or r.dir, r.recipe, r.extra
  else
    name, x, y, dir, recipe, extra = r[1], r[2], r[3], r[4], r[5], r[6]
  end
  if type(name) ~= "string" or type(x) ~= "number" or type(y) ~= "number" then return nil end
  dir = util.parse_dir(dir)
  if recipe == "" or type(recipe) ~= "string" then recipe = nil end
  if type(extra) == "string" then extra = {type = extra} elseif type(extra) ~= "table" then extra = nil end
  return {name, x, y, dir, recipe, extra}
end

-- tile box {x0, y0, x1, y1} (x1/y1 exclusive) of a planned entity; nil prototype -> 1x1
local function box_of(e)
  local proto = prototypes.entity[e[1]]
  local w, h = 1, 1
  if proto then w, h = proto.tile_width, proto.tile_height end
  if e[4] == defines.direction.east or e[4] == defines.direction.west then w, h = h, w end
  local x0 = math.floor(e[2] - w / 2 + 0.5)
  local y0 = math.floor(e[3] - h / 2 + 0.5)
  return {x0, y0, x0 + w, y0 + h}, proto
end

local function kind_of(proto)
  if not proto then return "other" end
  return KIND_OF[proto.type] or "other"
end

local function get(id)
  init()
  local p = storage.plans[tostring(id)]
  if not p then error("no plan " .. tostring(id)) end
  return p
end
plans.get = function(id) init(); return storage.plans[tostring(id)] end

---------------------------------------------------------------------------
-- overlay
---------------------------------------------------------------------------

-- greedy merge of a tile set {[y] = {[x] = true}} into rectangles {x0, y0, x1, y1}
local function merge_tiles(rows)
  local ys = {}
  for y in pairs(rows) do ys[#ys + 1] = y end
  table.sort(ys)
  local rects, open, prev = {}, {}, nil
  for _, y in ipairs(ys) do
    local xs = {}
    for x in pairs(rows[y]) do xs[#xs + 1] = x end
    table.sort(xs)
    local nxt, i = {}, 1
    while i <= #xs do
      local j = i
      while j < #xs and xs[j + 1] == xs[j] + 1 do j = j + 1 end
      local key = xs[i] .. ":" .. xs[j]
      local r = prev == y - 1 and open[key]
      if r then r[4] = y + 1 else
        r = {xs[i], y, xs[j] + 1, y + 1}
        rects[#rects + 1] = r
      end
      nxt[key] = r
      i = j + 1
    end
    open, prev = nxt, y
  end
  return rects
end
plans.merge_tiles = merge_tiles

-- Big plans are drawn/applied in slices over several ticks (storage.plan_jobs[id] = true while work is pending);
-- with no jobs the tick handler returns at once.
local function queue_job(p)
  storage.plan_jobs = storage.plan_jobs or {}
  storage.plan_jobs[p.id] = true
end

local function hide(p)
  for _, o in pairs(p.objs or {}) do
    if o.valid then o.destroy() end
  end
  p.objs = nil
  p.draw = nil
  p.shown = false
end

local WHITE = {0.9, 0.9, 0.9, 0.9}
-- planner v3 reserved zones by kind
local ZONE_COLOR = {mine = {1, 0.65, 0.25}, coal = {0.6, 0.6, 0.6}, smelt = {1, 1, 1}, science = {0.8, 0.5, 1},
  power = {1, 0.3, 0.3}, mall = {0.5, 0.8, 1}}

-- draw entities [job.i, job.i + n): fills are accumulated per kind and drawn merged at the end
local function draw_step(p, n)
  local job = p.draw
  local surface = game.get_surface(p.surface)
  if not surface then hide(p); return true end
  local objs, forces, by_kind = p.objs, {p.force}, job.by_kind
  local function mark(kind, b)
    local rows = by_kind[kind]
    if not rows then rows = {}; by_kind[kind] = rows end
    for y = b[2], b[4] - 1 do
      local r = rows[y]
      if not r then r = {}; rows[y] = r end
      for x = b[1], b[3] - 1 do r[x] = true end
    end
  end
  local last = math.min(#p.entities, job.i + n - 1)
  for k = job.i, last do
    local e = p.entities[k]
    local b, proto = box_of(e)
    local kind = kind_of(proto)
    mark(kind, b)
    local w, h = b[3] - b[1], b[4] - b[2]
    if w > 1 or h > 1 then
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, color = rgba(LEG[kind], 0.9),
        width = 2, filled = false, left_top = {b[1] + 0.06, b[2] + 0.06}, right_bottom = {b[3] - 0.06, b[4] - 0.06}}
      local sprite = "entity/" .. e[1]
      if helpers.is_valid_sprite_path(sprite) then
        local sc = math.min(w, h) * 0.32
        objs[#objs + 1] = rendering.draw_sprite{surface = surface, forces = forces, sprite = sprite,
          target = {e[2], e[3]}, x_scale = sc, y_scale = sc, tint = {0.85, 0.85, 0.85, 0.85}}
      end
    end
    local turn = proto and ARROW[proto.type]
    if turn and job.arrows < ARROW_LIMIT then
      job.arrows = job.arrows + 1
      -- triangle pointing along the flow; orientation 0 = north, 0.25 = east
      local sc = math.min(w, h)
      objs[#objs + 1] = rendering.draw_polygon{surface = surface, forces = forces, color = WHITE, target = {e[2], e[3]},
        orientation = ((e[4] + turn) % 16) / 16, vertices = {{0, -0.32 * sc}, {-0.24 * sc, 0.18 * sc}, {0.24 * sc, 0.18 * sc}}}
    end
  end
  job.i = last + 1
  if job.i <= #p.entities then return false end
  for _, d in ipairs(p.deconstruct) do
    local x, y = math.floor(d[1]), math.floor(d[2])
    mark("clear", {x, y, x + 1, y + 1})
  end
  local red = rgba(LEG.remove, 0.95)
  for _, rm in ipairs(p.removes or {}) do
    local ent = plans.find_listed(surface, p, rm)
    local b
    if ent then
      local bb = ent.bounding_box
      b = {bb.left_top.x, bb.left_top.y, bb.right_bottom.x, bb.right_bottom.y}
    else
      local tb = box_of({rm[1], rm[2], rm[3], 0})
      b = {tb[1], tb[2], tb[3], tb[4]}
    end
    for _, mode in ipairs({"game", "chart"}) do
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, color = red, width = 3,
        filled = false, render_mode = mode, left_top = {b[1], b[2]}, right_bottom = {b[3], b[4]}}
      objs[#objs + 1] = rendering.draw_line{surface = surface, forces = forces, color = red, width = 2,
        render_mode = mode, from = {b[1], b[2]}, to = {b[3], b[4]}}
      objs[#objs + 1] = rendering.draw_line{surface = surface, forces = forces, color = red, width = 2,
        render_mode = mode, from = {b[1], b[4]}, to = {b[3], b[2]}}
    end
  end
  for kind, rows in pairs(by_kind) do
    local l = LEG[kind]
    local fill, chart = rgba(l, kind == "clear" and 0.25 or 0.3), rgba(l, 0.85)
    for _, r in ipairs(merge_tiles(rows)) do  -- above trees/entities so the plan stays visible in forests
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, color = fill, filled = true,
        left_top = {r[1], r[2]}, right_bottom = {r[3], r[4]}}
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, color = chart, filled = true,
        render_mode = "chart", left_top = {r[1], r[2]}, right_bottom = {r[3], r[4]}}
    end
  end
  for _, z in ipairs(p.zones or {}) do   -- reserved for later stages: faint fill, thin outline, label
    local zc = ZONE_COLOR[z[6]] or {0.8, 0.8, 0.8}
    for _, mode in ipairs({"game", "chart"}) do
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, filled = true,
        color = {zc[1] * 0.08, zc[2] * 0.08, zc[3] * 0.08, 0.08}, render_mode = mode,
        left_top = {z[1], z[2]}, right_bottom = {z[3], z[4]}}
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, filled = false, width = 1,
        color = {zc[1] * 0.6, zc[2] * 0.6, zc[3] * 0.6, 0.6}, render_mode = mode,
        left_top = {z[1], z[2]}, right_bottom = {z[3], z[4]}}
    end
    if z[5] ~= "" then
      objs[#objs + 1] = rendering.draw_text{surface = surface, forces = forces, text = z[5], scale = 1.2,
        color = {zc[1], zc[2], zc[3], 0.8}, target = {z[1] + 0.3, z[2] + 0.2}}
    end
  end
  local bb = p.bbox
  if bb then
    local c = p.color or {0.85, 0.47, 0.34, 1}
    local title = (p.title ~= "" and p.title or p.id) .. "  [" .. p.id .. "]"
    for _, mode in ipairs({"game", "chart"}) do
      objs[#objs + 1] = rendering.draw_rectangle{surface = surface, forces = forces, color = c, filled = false,
        width = mode == "game" and 3 or 2, render_mode = mode, left_top = {bb[1] - 1, bb[2] - 1},
        right_bottom = {bb[3] + 1, bb[4] + 1}}
      objs[#objs + 1] = rendering.draw_text{surface = surface, forces = forces, color = c, text = title,
        render_mode = mode, target = {bb[1] - 1, bb[2] - (mode == "game" and 2.6 or 4)}, scale = mode == "game" and 2 or 1.4}
    end
  end
  p.draw = nil
  return true
end

-- (re)draw a plan: small plans at once, big ones over the next ticks. -> object count (nil while pending)
local function draw(p)
  hide(p)
  if not game.get_surface(p.surface) then error("surface " .. tostring(p.surface) .. " is gone") end
  p.objs, p.shown = {}, true
  p.draw = {i = 1, by_kind = {}, arrows = 0}
  if #p.entities <= SLICE then
    draw_step(p, SLICE)
    return #p.objs
  end
  queue_job(p)
  return nil
end

-- show one plan; others of the same force are hidden unless keep_others (variants usually overlap)
function plans.show(id, keep_others)
  local p = get(id)
  if not keep_others then
    for _, q in pairs(storage.plans) do
      if q ~= p and q.shown and q.force == p.force then hide(q) end
    end
  end
  local n = draw(p)
  changed()
  return n
end

function plans.hide(id)
  init()
  for _, p in pairs(storage.plans) do
    if id == nil or p.id == tostring(id) then hide(p) end
  end
  changed()
end

---------------------------------------------------------------------------
-- ops
---------------------------------------------------------------------------

local function compute_bbox(p)
  local bb
  local function grow(x0, y0, x1, y1)
    if not bb then bb = {x0, y0, x1, y1} return end
    bb[1], bb[2] = math.min(bb[1], x0), math.min(bb[2], y0)
    bb[3], bb[4] = math.max(bb[3], x1), math.max(bb[4], y1)
  end
  for _, e in ipairs(p.entities) do
    local b = box_of(e)
    grow(b[1], b[2], b[3], b[4])
  end
  for _, d in ipairs(p.deconstruct) do
    grow(math.floor(d[1]), math.floor(d[2]), math.floor(d[1]) + 1, math.floor(d[2]) + 1)
  end
  p.bbox = bb
end

-- prototypes that can never be a ghost (rows with these are dropped as bad_rows)
local NOT_BUILDABLE = {resource = true, tree = true, ["simple-entity"] = true, unit = true, character = true,
  cliff = true, fish = true, ["item-entity"] = true, ["entity-ghost"] = true, ["tile-ghost"] = true, corpse = true}

local function validation_of(v)
  if type(v) ~= "table" then return nil end
  local function n(x) return type(x) == "table" and #x or tonumber(x) or 0 end
  return {ok = v.ok and true or false, errors = n(v.errors), warnings = n(v.warnings)}
end

-- {id, title, entities, deconstruct?, summary?, color?, surface?, force?, variant?, stage?, group?, warnings?,
--  validation?, append?, show? (default true), keep_others?}
-- append = true adds entities/deconstruct to an existing plan (for very large plans sent in several ops).
-- Returns {id, entities, deconstruct, unknown, objects, ms?}.
api.register("plan_preview", function(args)
  init()
  local id = args.id ~= nil and tostring(args.id) or ""
  if id == "" then error("id required") end
  local p = storage.plans[id]
  if not (args.append and p) then
    if p then hide(p) end
    local surface = util.get_surface(args)
    storage.plan_seq = storage.plan_seq + 1
    p = {id = id, entities = {}, deconstruct = {}, status = "proposed", seq = storage.plan_seq,
      surface = surface.name, force = util.get_force(args).name, tick = game.tick}
    storage.plans[id] = p
  end
  if args.title ~= nil then p.title = strip(args.title) end
  for _, k in ipairs({"variant", "stage", "group", "master"}) do
    if args[k] ~= nil then p[k] = tostring(args[k]) end
  end
  p.title = p.title or id
  if args.summary ~= nil then p.lines = lines_of(args.summary) end
  if args.warnings ~= nil then p.warnings = lines_of(args.warnings) end
  if args.validation ~= nil then p.validation = validation_of(args.validation) end
  if type(args.color) == "table" then
    local c = args.color
    p.color = {c.r or c[1] or 1, c.g or c[2] or 1, c.b or c[3] or 1, c.a or c[4] or 1}
  end
  local bad, unknown = 0, {}
  for _, r in pairs(args.entities or {}) do
    local e = type(r) == "table" and parse_entity(r)
    local proto = e and prototypes.entity[e[1]]
    if e and proto and NOT_BUILDABLE[proto.type] then
      bad = bad + 1
    elseif e then
      p.entities[#p.entities + 1] = e
      if not proto then unknown[e[1]] = true end
    else
      bad = bad + 1
    end
  end
  for _, d in pairs(args.deconstruct or {}) do
    local x, y, name
    if type(d) == "table" then x, y, name = d.x or d[1], d.y or d[2], d.name or d[3] end
    if type(x) == "number" and type(y) == "number" and type(name) == "string" then
      p.deconstruct[#p.deconstruct + 1] = {x, y, name}
    else
      bad = bad + 1
    end
  end
  if args.removes ~= nil and not (args.append and p.removes) then p.removes = {} end
  for _, d in pairs(args.removes or {}) do
    local name, x, y, unit
    if type(d) == "table" then name, x, y, unit = d.name or d[1], d.x or d[2], d.y or d[3], d.unit or d[4] end
    if type(name) == "string" and type(x) == "number" and type(y) == "number" and #p.removes < MAX_REMOVES then
      p.removes[#p.removes + 1] = {name, x, y, tonumber(unit)}
    else
      bad = bad + 1
    end
  end
  if type(args.facts) == "table" then p.facts = args.facts end
  -- S0 fuel takes: {{name, x, y, unit?, item, count}} coal out of listed idle machines of the base (planner v2)
  if args.takes ~= nil and not (args.append and p.takes) then p.takes = {} end
  for _, d in pairs(args.takes or {}) do
    local name, x, y, unit, item, count
    if type(d) == "table" then name, x, y, unit, item, count = d[1], d[2], d[3], d[4], d[5], d[6] end
    if type(name) == "string" and type(x) == "number" and type(y) == "number" and type(item) == "string" and
        type(count) == "number" and #p.takes < MAX_REMOVES then
      p.takes[#p.takes + 1] = {name, x, y, tonumber(unit), item, math.floor(count)}
    else
      bad = bad + 1
    end
  end
  -- S0 repairs: existing burners of the base the helper refuels {{name, x, y, unit?, n}} (planner v2)
  if args.refuel_existing ~= nil then
    p.refuel_existing = {}
    for _, d in pairs(args.refuel_existing) do
      if type(d) == "table" and type(d[1]) == "string" and type(d[2]) == "number" and type(d[3]) == "number" and
          #p.refuel_existing < 500 then
        p.refuel_existing[#p.refuel_existing + 1] = {d[1], d[2], d[3], tonumber(d[4]), tonumber(d[5]) or 10}
      else
        bad = bad + 1
      end
    end
  end
  -- the plan's sites in build order {{x0, y0, x1, y1}}: the helper gathers for and builds one after the other
  if args.build_order ~= nil then
    p.build_order = {}
    for _, d in pairs(args.build_order) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and type(d[3]) == "number" and
          type(d[4]) == "number" and #p.build_order < 32 then
        p.build_order[#p.build_order + 1] = {d[1], d[2], d[3], d[4]}
      end
    end
  end
  -- a later stage: the previous stage's hand-fed burners {{x, y, n}} stay in this plan's coal round (a new round
  -- replaces the old one), and its output chests {{x, y}} are where the helper takes materials from first
  if args.keep_fuelled ~= nil then
    p.keep_fuelled = {}
    for _, d in pairs(args.keep_fuelled) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and #p.keep_fuelled < 500 then
        p.keep_fuelled[#p.keep_fuelled + 1] = {d[1], d[2], tonumber(d[3]) or 10, d[4] == 1 or nil}
      end
    end
  end
  if args.reserve ~= nil then   -- {{x0, y0, x1, y1}}: the next stage's ground (the helper builds no smelter there)
    p.reserve = {}
    for _, d in pairs(args.reserve) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and type(d[3]) == "number" and
          type(d[4]) == "number" and #p.reserve < 500 then
        p.reserve[#p.reserve + 1] = {d[1], d[2], d[3], d[4]}
      end
    end
  end
  -- planner v3: ground later stages of the master plan build on {{x0, y0, x1, y1, label, kind}} (drawn faint, no
  -- ghosts; at most 64)
  if args.zones ~= nil then
    p.zones = {}
    for _, d in pairs(args.zones) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and type(d[3]) == "number" and
          type(d[4]) == "number" and #p.zones < 64 then
        p.zones[#p.zones + 1] = {d[1], d[2], d[3], d[4], strip(tostring(d[5] or "")), tostring(d[6] or "")}
      end
    end
  end
  -- planner v3: a later stage of a master plan {master, label, gate = {tech...}, after = plan id}. It waits (status
  -- "future", not drawn, no ghosts) until every gate tech is researched and the `after` stage is built; then it is
  -- offered (status "proposed", a print + a companion event "stage_ready <id>")
  if type(args.future) == "table" then
    local fu = args.future
    p.future = {master = tostring(fu.master or ""), label = strip(tostring(fu.label or "")), gate = {},
      after = fu.after ~= nil and tostring(fu.after) or nil, order = tonumber(fu.order) or 0}
    for _, t in pairs(fu.gate or {}) do
      if type(t) == "string" and #p.future.gate < 8 then p.future.gate[#p.future.gate + 1] = t end
    end
    if fu.waiting ~= false and p.status == "proposed" then p.status = "future" end
  end
  -- planner v3: poles of earlier stages {{x, y}} this stage's poles are wired to explicitly (see wire_poles)
  if args.wire_with ~= nil then
    p.wire_with = {}
    for _, d in pairs(args.wire_with) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and #p.wire_with < 1000 then
        p.wire_with[#p.wire_with + 1] = {d[1], d[2]}
      end
    end
  end
  if args.take_from ~= nil then
    p.take_from = {}
    for _, d in pairs(args.take_from) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and #p.take_from < 64 then
        p.take_from[#p.take_from + 1] = {d[1], d[2]}
      end
    end
  end
  -- the plan's coal chests {{x, y}}: where its logistics round takes coal from first
  if args.coal_from ~= nil then
    p.coal_from = {}
    for _, d in pairs(args.coal_from) do
      if type(d) == "table" and type(d[1]) == "number" and type(d[2]) == "number" and #p.coal_from < 64 then
        p.coal_from[#p.coal_from + 1] = {d[1], d[2]}
      end
    end
  end
  compute_bbox(p)
  local out = {id = id, entities = #p.entities, deconstruct = #p.deconstruct, bad_rows = bad, bbox = p.bbox,
    removes = p.removes and #p.removes or 0}
  local u = {}
  for n in pairs(unknown) do u[#u + 1] = n end
  if #u > 0 then out.unknown = u end
  if args.show == false or p.status == "future" then
    if p.shown then hide(p) end
    changed()
  else
    local prof = helpers.create_profiler()
    out.objects = plans.show(id, args.keep_others)
    prof.stop()
    out.drawn = out.objects ~= nil
    out.pending = out.objects == nil  -- big plan: finishes drawing over the next ticks
    log({"", "claude-bridge plan_preview ", id, " ", out.objects or "pending", " objects: ", prof})
  end
  return out
end)

-- {id?}: hide one plan's overlay (or all); the plan stays in the list
api.register("plan_clear_preview", function(args)
  plans.hide(args.id)
  return {cleared = args.id or "all"}
end)

local function entity_brief(p)
  local counts = {}
  for _, e in ipairs(p.entities) do counts[e[1]] = (counts[e[1]] or 0) + 1 end
  return counts
end

local function plan_info(p, full)
  local o = {id = p.id, title = p.title, variant = p.variant, stage = p.stage, group = p.group, status = p.status,
    entities = #p.entities, deconstruct = #p.deconstruct, surface = p.surface, force = p.force, shown = p.shown or false,
    drawing = p.draw ~= nil, superseded = p.superseded,
    summary = p.lines, warnings = p.warnings, validation = p.validation, bbox = p.bbox, tick = p.tick, result = p.result,
    facts = p.facts, future = p.future, ready_tick = p.ready_tick}
  if p.removes and #p.removes > 0 then o.removes = plans.removal_counts(p) end
  if p.takes and #p.takes > 0 then o.takes = {listed = #p.takes, done = p.takes_done or false} end
  if full then o.counts = entity_brief(p) end
  return o
end
plans.info = plan_info

-- plans of a force, newest first
function plans.list(force)
  init()
  local out = {}
  for _, p in pairs(storage.plans) do
    if not force or p.force == force then out[#out + 1] = p end
  end
  table.sort(out, function(a, b) return (a.seq or 0) > (b.seq or 0) end)
  return out
end

-- {force?, all?}: compact list (all = every force)
api.register("plan_list", function(args)
  local out = {}
  for _, p in ipairs(plans.list(not args.all and util.get_force(args).name or nil)) do out[#out + 1] = plan_info(p) end
  return out
end)

-- {id, entities?}: one plan (entities = true adds the rows)
api.register("plan_get", function(args)
  local p = get(args.id)
  local o = plan_info(p, true)
  if args.entities then o.rows = p.entities; o.deconstruct_rows = p.deconstruct; o.removes_rows = p.removes end
  return o
end)

function plans.drop(id)
  local p = get(id)
  plans.cancel_removal(p)
  hide(p)
  storage.plans[p.id] = nil
  changed()
end

-- {id}: forget a plan and its overlay (ghosts it placed stay; use clear_claude_ghosts {plan = id} for those)
api.register("plan_drop", function(args)
  plans.drop(args.id)
  return {dropped = tostring(args.id)}
end)

---------------------------------------------------------------------------
-- removals (planner v2 rebuilds)
---------------------------------------------------------------------------

-- the listed existing entity (same name, force, exact position, unit when given; never a ghost) or nil
function plans.find_listed(surface, p, rm)
  local force = game.forces[p.force]
  if not (surface and force) then return nil end
  for _, e in pairs(surface.find_entities_filtered{name = rm[1], position = {rm[2], rm[3]}, force = force}) do
    if e.valid and e.type ~= "entity-ghost" and e.position.x == rm[2] and e.position.y == rm[3] and
        (rm[4] == nil or e.unit_number == rm[4]) then
      return e
    end
  end
  return nil
end

function plans.removal_counts(p)
  local rs = p.rm or {}
  local pending = 0
  for _ in pairs(p.removing or {}) do pending = pending + 1 end
  return {listed = #(p.removes or {}), marked = rs.marked or 0, removed = rs.removed or 0, changed = rs.changed or 0,
    cancelled = rs.cancelled or 0, kept = rs.kept or 0, pending = pending, deferred = #(p.deferred or {})}
end

-- order deconstruction of exactly the listed entities
local function mark_removals(p, surface, force)
  p.removing, p.deferred = p.removing or {}, p.deferred or {}
  p.rm = p.rm or {marked = 0, removed = 0, changed = 0, cancelled = 0, kept = 0}
  for _, rm in ipairs(p.removes or {}) do
    local e = plans.find_listed(surface, p, rm)
    if e and e.unit_number and (e.to_be_deconstructed() or e.order_deconstruction(force)) then
      p.removing[e.unit_number] = {name = rm[1], x = rm[2], y = rm[3], entity = e}
      p.rm.marked = p.rm.marked + 1
    else
      p.rm.changed = p.rm.changed + 1
    end
  end
end

-- a listed entity of this plan is still standing inside tile box b
local function removing_in(p, surface, b)
  if not p.removing or next(p.removing) == nil then return false end
  for _, f in pairs(surface.find_entities_filtered{area = {{b[1] + 0.1, b[2] + 0.1}, {b[3] - 0.1, b[4] - 0.1}}}) do
    if f.valid and f.unit_number and p.removing[f.unit_number] then return true end
  end
  return false
end

-- cancel every mark this plan still has; deferred ghosts are dropped (nothing else is touched). -> marks cancelled
function plans.cancel_removal(p)
  local n = 0
  local rows = p.removing or {}
  p.removing = {}  -- first: the on_cancelled_deconstruction events below must not count these as "kept by you"
  for _, row in pairs(rows) do
    local e = row.entity
    if e and e.valid and e.to_be_deconstructed() then
      e.cancel_deconstruction(game.forces[p.force] or e.force)
      n = n + 1
    end
  end
  if p.rm then p.rm.cancelled = (p.rm.cancelled or 0) + n end
  p.deferred = {}
  return n
end

-- The plan's own poles (ghosts or built) are wired to each other explicitly: a ghost pole auto-connects to whatever is
-- nearest, e.g. a ghost pole of the player's that the helper never builds, and the game skips the direct link when the
-- two are already joined through it - the plan's line would then stay cut (verified on a real save). Every pair of
-- plan poles within wire reach gets a copper wire. -> number of wires added.
local function wire_poles(p, surface, force)
  local poles = {}
  for _, e in ipairs(p.entities) do
    local proto = prototypes.entity[e[1]]
    if proto and proto.type == "electric-pole" then
      local f = surface.find_entities_filtered{position = {e[2], e[3]}, radius = 0.3, force = force, limit = 1,
        name = e[1]}[1] or surface.find_entities_filtered{position = {e[2], e[3]}, radius = 0.3, force = force,
        limit = 1, ghost_name = e[1]}[1]
      if f then
        local reach = 7.5
        local okr, v = pcall(function() return proto.get_max_wire_distance() end)
        if okr and type(v) == "number" then reach = v end
        poles[#poles + 1] = {e = f, reach = reach}
      end
    end
  end
  local own = #poles
  for _, d in ipairs(p.wire_with or {}) do   -- an earlier stage's poles (built by now, or still ghosts)
    local f = surface.find_entities_filtered{position = {d[1], d[2]}, radius = 0.3, force = force, limit = 1,
      type = "electric-pole"}[1] or surface.find_entities_filtered{position = {d[1], d[2]}, radius = 0.3,
      force = force, limit = 1, ghost_type = "electric-pole"}[1]
    if f then
      local reach = 7.5
      local okr, v = pcall(function() return (f.type == "entity-ghost" and f.ghost_prototype or f.prototype)
        .get_max_wire_distance() end)
      if okr and type(v) == "number" then reach = v end
      poles[#poles + 1] = {e = f, reach = reach}
    end
  end
  local n = 0
  for i = 1, own do
    local a = poles[i]
    for j = i + 1, #poles do
      local b = poles[j]
      local dx, dy = a.e.position.x - b.e.position.x, a.e.position.y - b.e.position.y
      if dx * dx + dy * dy <= math.min(a.reach, b.reach) ^ 2 + 1e-6 then
        local ca = a.e.get_wire_connector(defines.wire_connector_id.pole_copper, true)
        local cb = b.e.get_wire_connector(defines.wire_connector_id.pole_copper, true)
        if ca and cb and not ca.is_connected_to(cb) and ca.connect_to(cb, false) then n = n + 1 end
      end
    end
  end
  return n
end


-- Place the plan's ghosts (tags.claude_plan = id, no last_user) and order deconstruction of listed obstacles plus
-- trees/rocks under the ghosts placed. Blocked entities (other entities, water, cliffs, other ghosts) are skipped and
-- reported; the same entity/ghost already at the same spot counts as existing (re-applying is harmless).
-- One area search per entity; check = manual_ghost + forced (trees/rocks don't block, they get deconstructed).
local function apply_step(p, n)
  local job = p.apply
  local r = job.r
  local surface, force = game.get_surface(p.surface), game.forces[p.force]
  if not (surface and force) then
    p.apply, r.pending, p.status = nil, nil, r.placed > 0 and "applied" or "proposed"
    return true
  end
  local function blocked(e, why)
    r.blocked = r.blocked + 1
    if #r.blocked_list < MAX_BLOCKED then r.blocked_list[#r.blocked_list + 1] = {name = e[1], x = e[2], y = e[3], why = why} end
  end
  local tags = {claude_plan = p.id}
  local last = math.min(#p.entities, job.i + n - 1)
  -- one entity; errors (e.g. "iron-ore can not be part a entity ghost") count as blocked, never stall the plan
  local function place(e)
    local name, dir = e[1], e[4]
    local pos = {x = e[2], y = e[3]}
    if not prototypes.entity[name] then
      r.unknown = r.unknown + 1
      blocked(e, "unknown entity")
    else
      local b = box_of(e)
      local existing, ghost, cliff, clear = false, nil, false, {}
      for _, f in pairs(surface.find_entities_filtered{area = {{b[1] + 0.1, b[2] + 0.1}, {b[3] - 0.1, b[4] - 0.1}}}) do
        local t = f.type
        if t == "entity-ghost" then
          if f.ghost_name == name and f.position.x == pos.x and f.position.y == pos.y and f.force == force then
            existing = true
          else
            ghost = f.ghost_name
          end
        elseif f.name == name and f.force == force and f.position.x == pos.x and f.position.y == pos.y then
          -- the same entity there counts as built, unless this plan takes it apart (a planner v3 belt crossing the
          -- player's idle belt line: their tile goes, ours comes); then the ghost is placed over the marked one
          if not (f.unit_number and p.removing and p.removing[f.unit_number]) then existing = true end
        elseif t == "tree" or t == "simple-entity" then
          clear[#clear + 1] = f
        elseif t == "cliff" then
          cliff = true
        end
      end
      if existing then
        r.existing = r.existing + 1
      elseif removing_in(p, surface, b) and not surface.can_place_entity{name = name, position = pos, direction = dir,
          force = force, forced = true, build_check_type = defines.build_check_type.manual_ghost} then
        p.deferred[#p.deferred + 1] = e   -- placed once the listed entity under it is gone
        r.deferred = (r.deferred or 0) + 1
      elseif ghost then
        blocked(e, "ghost of " .. ghost .. " already there")
      elseif cliff then
        blocked(e, "cliff")
      elseif not surface.can_place_entity{name = name, position = pos, direction = dir, force = force, forced = true,
          build_check_type = defines.build_check_type.manual_ghost} then
        blocked(e, "blocked")
      else
        local params = {name = "entity-ghost", inner_name = name, position = pos, direction = dir, force = force, tags = tags}
        local ex = e[6]
        if ex and (ex.type == "input" or ex.type == "output") then params.type = ex.type end
        local g = surface.create_entity(params)
        if g then
          r.placed = r.placed + 1
          if e[5] and not pcall(g.set_recipe, e[5]) then r.recipe_failed = (r.recipe_failed or 0) + 1 end
          for _, t in ipairs(clear) do
            local key = t.valid and (t.unit_number or (t.name .. "@" .. t.position.x .. "," .. t.position.y))
            if key and not job.marked[key] then
              job.marked[key] = true
              if t.to_be_deconstructed() or t.order_deconstruction(force) then r.deconstruct = r.deconstruct + 1 end
            end
          end
        else
          blocked(e, "create failed")
        end
      end
    end
  end
  for k = job.i, last do
    local e = p.entities[k]
    local ok, err = pcall(place, e)
    if not ok then blocked(e, "error: " .. tostring(err):sub(1, 120)) end
  end
  job.i = last + 1
  if job.i <= #p.entities then return false end
  -- done
  local okw, nw = pcall(wire_poles, p, surface, force)
  if okw then r.wired = nw else r.wire_error = tostring(nw):sub(1, 120) end
  p.apply = nil
  r.pending = nil
  r.tick = game.tick
  p.status = "applied"
  for _, bl in ipairs(r.blocked_list) do  -- mark what couldn't be placed for 5 minutes
    local bx = box_of({bl.name, bl.x, bl.y, 0})
    rendering.draw_rectangle{surface = surface, forces = {force}, color = {0.9, 0.1, 0.1, 0.9}, width = 3,
      filled = false, left_top = {bx[1], bx[2]}, right_bottom = {bx[3], bx[4]}, time_to_live = 60 * 60 * 5}
  end
  return true
end

-- a draw/apply job failed: never leave the plan stuck in "applying"/"drawing" (the GUI would show no buttons)
function plans.abort(p, err)
  if p.apply then
    local r = p.apply.r
    r.pending, r.error, r.tick = nil, tostring(err):sub(1, 200), game.tick
    p.apply = nil
    p.status = r.placed > 0 and "applied" or "proposed"
    if r.placed == 0 then
      for _, q in pairs(storage.plans) do
        if q.superseded == p.id then q.superseded = nil end
      end
    end
  end
  if p.draw then hide(p) end
  changed()
end

-- -> result {placed, existing, blocked, blocked_list, deconstruct, unknown, total}; pending = true for a big plan
-- (it finishes over the next ticks; plan_list / plan_get show the final result, status "applying" -> "applied")
function plans.apply(id)
  local p = get(id)
  local surface = game.get_surface(p.surface)
  if not surface then error("surface " .. tostring(p.surface) .. " is gone") end
  local force = game.forces[p.force]
  if not force then error("force " .. tostring(p.force) .. " is gone") end
  if p.apply then return p.result end  -- already running
  local prof = helpers.create_profiler()
  local r = {placed = 0, existing = 0, blocked = 0, unknown = 0, deconstruct = 0, blocked_list = {}, total = #p.entities,
    deferred = 0}
  -- the listed existing builds first (exactly those; anything that changed since planning is skipped and counted)
  if p.removes and #p.removes > 0 then
    mark_removals(p, surface, force)
    r.removes = plans.removal_counts(p)
  end
  -- listed obstacles (trees/rocks/... the planner wants gone); cliffs, resources and characters are never ordered
  -- Only neutral trees/rocks: never the player's buildings or ghosts, even for a row without a name.
  for _, d in ipairs(p.deconstruct) do
    local x, y = math.floor(d[1]), math.floor(d[2])
    if type(d[3]) == "string" then
      for _, ent in pairs(surface.find_entities_filtered{area = {{x + 0.05, y + 0.05}, {x + 0.95, y + 0.95}},
          name = d[3], type = CLEARABLE}) do
        if ent.force.name == "neutral" and not ent.to_be_deconstructed() and ent.order_deconstruction(force) then
          r.deconstruct = r.deconstruct + 1
        end
      end
    end
  end
  hide(p)
  p.result, p.status, p.superseded = r, "applying", nil
  -- other proposed plans on the same ground (sibling variants) can't be built on top of this one any more
  local bb = p.bbox
  for _, q in pairs(storage.plans) do
    local qb = q.bbox
    if q ~= p and q.force == p.force and q.surface == p.surface and q.status == "proposed" and bb and qb and
        qb[1] < bb[3] and bb[1] < qb[3] and qb[2] < bb[4] and bb[2] < qb[4] then
      q.superseded = p.id
      if q.shown then hide(q) end
    end
  end
  -- planner v3: approving a stage of one master plan drops the waiting stages of the other masters (variants)
  if p.master then
    local drop = {}
    for qid, q in pairs(storage.plans) do
      if q ~= p and q.status == "future" and q.force == p.force and q.future and q.future.master ~= p.master then
        drop[#drop + 1] = qid
      end
    end
    for _, qid in ipairs(drop) do pcall(plans.drop, qid) end
  end
  p.apply = {i = 1, r = r, marked = {}}
  local ok, done = pcall(apply_step, p, SLICE)
  if not ok then
    plans.abort(p, done)
    return r
  end
  prof.stop()
  log({"", "claude-bridge plan_apply ", p.id, " first slice: ", prof})
  if not done then
    r.pending = true
    queue_job(p)
  end
  changed()
  return r
end

-- {id}: place the plan's tagged ghosts + order deconstruction.
-- -> {placed, existing, blocked, blocked_list, deconstruct, unknown, total, pending?}
api.register("plan_apply", function(args)
  return plans.apply(args.id)
end)

-- pending draw/apply work: one slice per tick in total; no jobs -> return at once
local events = require("scripts.events")
events.on(defines.events.on_tick, function()
  local dq = storage.plan_deferred
  if dq then
    storage.plan_deferred = nil
    for id in pairs(dq) do
      local p = storage.plans and storage.plans[id]
      if p then
        local ok, err = pcall(plans.place_deferred, p)
        if not ok then log("claude-bridge place_deferred: " .. tostring(err)) end
        changed()
      end
    end
  end
  local jobs = storage.plan_jobs
  if not jobs then return end
  local id = next(jobs)
  if id == nil then storage.plan_jobs = nil; return end
  local ok, err = pcall(function()
    local p = storage.plans and storage.plans[id]
    local done = true
    if p and p.apply then
      local prof = helpers.create_profiler()
      done = apply_step(p, SLICE)
      prof.stop()
      log({"", "claude-bridge plan_apply ", id, " slice: ", prof})
    elseif p and p.draw then
      local prof = helpers.create_profiler()
      done = draw_step(p, SLICE)
      prof.stop()
      log({"", "claude-bridge plan_preview ", id, " slice: ", prof})
    end
    if done then
      jobs[id] = nil
      if p then changed() end
    end
  end)
  if not ok then
    jobs[id] = nil
    log("claude-bridge plan job " .. tostring(id) .. ": " .. tostring(err))
    pcall(function()
      local p = storage.plans and storage.plans[id]
      if p then plans.abort(p, err) end
    end)
  end
end)

-- ghosts deferred behind listed entities: place the ones whose ground is free now. -> placed count
function plans.place_deferred(p)
  if not p.deferred or #p.deferred == 0 then return 0 end
  local surface, force = game.get_surface(p.surface), game.forces[p.force]
  if not (surface and force) then return 0 end
  local keep, n = {}, 0
  for _, e in ipairs(p.deferred) do
    local b = box_of(e)
    if removing_in(p, surface, b) then
      keep[#keep + 1] = e
    else
      local pos = {x = e[2], y = e[3]}
      local ok = surface.can_place_entity{name = e[1], position = pos, direction = e[4], force = force, forced = true,
        build_check_type = defines.build_check_type.manual_ghost}
      local ug = e[6] and (e[6].type == "input" or e[6].type == "output") and e[6].type or nil
      local g = ok and surface.create_entity{name = "entity-ghost", inner_name = e[1], position = pos, direction = e[4],
        force = force, tags = {claude_plan = p.id}, type = ug}
      if g then
        n = n + 1
        if e[5] then pcall(g.set_recipe, e[5]) end
      elseif p.result then
        p.result.blocked = (p.result.blocked or 0) + 1
      end
    end
  end
  p.deferred = keep
  if p.result then p.result.placed = (p.result.placed or 0) + n end
  return n
end

-- a listed entity is gone (helper, player, robots, destroyed) or its mark was cancelled ("kept"): count it and place
-- the ghosts that waited for it (kept: the ghosts overlapping it are dropped)
function plans.removed(unit, how)
  init()
  for _, p in pairs(storage.plans) do
    local row = p.removing and p.removing[unit]
    if row then
      p.removing[unit] = nil
      p.rm = p.rm or {}
      local e = row.entity
      if how == "kept" then
        p.rm.kept = (p.rm.kept or 0) + 1
        if e and e.valid and p.deferred and #p.deferred > 0 then
          local bb = e.bounding_box
          local keep = {}
          for _, d in ipairs(p.deferred) do
            local b = box_of(d)
            if not (b[1] < bb.right_bottom.x and bb.left_top.x < b[3] and b[2] < bb.right_bottom.y and
                bb.left_top.y < b[4]) then
              keep[#keep + 1] = d
            end
          end
          p.deferred = keep
        end
      else
        p.rm.removed = (p.rm.removed or 0) + 1
        if e and e.valid then row.entity = nil end
      end
      if how ~= "kept" then
        -- the entity is still valid while its event runs: place deferred ghosts on the next tick
        storage.plan_deferred = storage.plan_deferred or {}
        storage.plan_deferred[p.id] = true
      end
      changed()
    end
  end
end
api.plans_removed = plans.removed

local function on_gone(ev)
  local e = ev.entity
  if e and e.valid and e.unit_number and storage.plans then
    local ok, err = pcall(plans.removed, e.unit_number, "removed")
    if not ok then log("claude-bridge plans removed: " .. tostring(err)) end
  end
end

local ev_mod = require("scripts.events")
for _, id in ipairs({defines.events.on_player_mined_entity, defines.events.on_robot_mined_entity,
    defines.events.on_entity_died, defines.events.script_raised_destroy}) do
  ev_mod.on(id, on_gone)
end
ev_mod.on(defines.events.on_cancelled_deconstruction, function(ev)
  local e = ev.entity
  if e and e.valid and e.unit_number and storage.plans then
    local ok, err = pcall(plans.removed, e.unit_number, "kept")
    if not ok then log("claude-bridge plans kept: " .. tostring(err)) end
  end
end)

-- clear_claude_ghosts {plan}: its pending removals are cancelled too
api.on_clear_plan = function(id)
  init()
  local p = storage.plans[tostring(id)]
  if p then return plans.cancel_removal(p) end
  return 0
end

-- this plan's ghosts are gone again: plans it replaced may be approved instead
function plans.unsupersede(id)
  init()
  for _, q in pairs(storage.plans) do
    if q.superseded == tostring(id) then q.superseded = nil end
  end
  changed()
end

-- coal a hand-fed burner gets per fill (about 22 min for a burner drill or a stone furnace; the round tops a burner up
-- once it is below half, so far sites are visited about every 10 minutes)
local FILL = {["mining-drill"] = 50, furnace = 30, inserter = 5, boiler = 50}

---------------------------------------------------------------------------
-- planner v3 roadmap: later stages wait as "future" plans and are offered when they can be built
---------------------------------------------------------------------------

-- every ghost of an applied plan is built and every entity it listed for removal is gone
function plans.stage_built(p)
  if not p or p.status ~= "applied" then return false end
  if p.removing and next(p.removing) ~= nil then return false end
  if p.deferred and #p.deferred > 0 then return false end
  local surface = game.get_surface(p.surface)
  local bb = p.bbox
  if not (surface and bb) then return false end
  for _, g in pairs(surface.find_entities_filtered{area = {{bb[1] - 1, bb[2] - 1}, {bb[3] + 1, bb[4] + 1}},
      type = "entity-ghost", force = p.force}) do
    local ok, tags = pcall(function() return g.tags end)
    if ok and type(tags) == "table" and tags.claude_plan == p.id then return false end
  end
  return true
end

local function gate_open(p)
  local force = game.forces[p.force]
  if not force then return false end
  for _, t in ipairs(p.future and p.future.gate or {}) do
    local tech = force.technologies[t]
    if not (tech and tech.researched) then return false end
  end
  return true
end

-- what a waiting stage waits for (Plans tab roadmap line): missing research (names), previous stage built?
function plans.waiting_for(p)
  local fu = p.future
  if not fu then return {}, true end
  local force = game.forces[p.force]
  local missing = {}
  for _, t in ipairs(fu.gate or {}) do
    local tech = force and force.technologies[t]
    if not (tech and tech.researched) then missing[#missing + 1] = t end
  end
  local after = fu.after and storage.plans[fu.after]
  return missing, (not after) or plans.stage_built(after)
end

-- future stages whose research is done and whose previous stage is built become proposed (offered). -> n offered
function plans.check_ready()
  init()
  local n = 0
  for _, p in pairs(storage.plans) do
    if p.status == "future" and p.future and gate_open(p) then
      local after = p.future.after and storage.plans[p.future.after]
      -- a previous stage that was dismissed (no longer listed) does not block its successor
      if not after or plans.stage_built(after) then
        storage.plan_seq = storage.plan_seq + 1
        p.status, p.seq, p.ready_tick = "proposed", storage.plan_seq, game.tick
        n = n + 1
        local force = game.forces[p.force]
        local msg = "Stage ready: " .. (p.future.label ~= "" and p.future.label or tostring(p.title)) ..
          ". Preview and approve it in the Plans tab."
        if force then
          for _, pl in pairs(force.connected_players) do
            pl.print({"", "[color=0.85,0.47,0.34][Claude helper][/color] ", msg})
          end
        end
        local okt, transport = pcall(require, "scripts.transport")
        if okt and type(transport) == "table" and transport.post_event then
          pcall(transport.post_event, "stage_ready " .. p.id)
        end
      end
    end
  end
  if n > 0 then changed() end
  return n
end

api.register("plan_check_ready", function() return {offered = plans.check_ready()} end)

local function any_future()
  for _, p in pairs(storage.plans or {}) do
    if p.status == "future" then return true end
  end
  return false
end

local ev_ready = require("scripts.events")
ev_ready.on(defines.events.on_research_finished, function()
  local ok, err = pcall(plans.check_ready)
  if not ok then log("claude-bridge check_ready: " .. tostring(err)) end
end)
ev_ready.on_nth_tick(600, function()     -- a stage counts as built once its last ghost is: checked every 10 s
  local ok, err = pcall(function()
    if any_future() then plans.check_ready() end
  end)
  if not ok then log("claude-bridge check_ready: " .. tostring(err)) end
end)

-- {{x, y, n, primed?}, ...} of the entities a player (or the helper) fuels by hand: extra.fuel = "manual", boilers
-- (planner v1/v2 only: a planner v3 stage's boilers are fed by a coal belt); n = coal per fill (extra.fuel_n, else by
-- type). primed = a planner v3 self-feeding coal-field burner: the helper only starts it and restarts it should it
-- ever stop (not hand-fed). In plan order (planner v2 lists its coal drills first, then each pair).
function plans.fuel_at(p, existing_only)
  local out = {}
  for _, r in ipairs(p.refuel_existing or {}) do out[#out + 1] = {r[2], r[3], r[5]} end  -- S0 repairs first
  if existing_only then return out end
  for _, e in ipairs(p.entities) do
    local proto = prototypes.entity[e[1]]
    if (e[6] and e[6].fuel == "manual") or (proto and proto.type == "boiler" and not p.master) then
      local n = e[6] and tonumber(e[6].fuel_n) or (proto and FILL[proto.type]) or 10
      out[#out + 1] = {e[2], e[3], n}
    elseif e[6] and tonumber(e[6].prime) then
      -- planner v3: a self-feeding burner (coal field) runs on what it mines once it has a few coal; the helper
      -- primes it and its round restarts it should it ever stop
      out[#out + 1] = {e[2], e[3], tonumber(e[6].prime), true}
    end
  end
  for _, r in ipairs(p.keep_fuelled or {}) do out[#out + 1] = {r[1], r[2], r[3], r[4]} end  -- earlier stages' burners
  return out
end

-- how many of fuel_at's entries are hand-fed (not just primed coal-field burners)
function plans.hand_fed(fuel_at)
  local n = 0
  for _, r in ipairs(fuel_at or {}) do if not r[4] then n = n + 1 end end
  return n
end

-- {id, what = "build"|"refuel"|"round", player?}: the helper builds the plan's ghosts (S0 fuel takes and listed
-- removals first, then only this plan's ghosts, then its logistics round), only refuels the hand-fed burners once, or
-- starts the round; spawns the helper if needed (Plans tab buttons; tests). -> {msg}
api.register("plan_helper", function(args)
  local p = get(args.id)
  local what = (args.what == "refuel" or args.what == "round") and args.what or "build"
  if not p.bbox then error("plan has no entities") end
  local player = args.player and util.get_player(args) or nil
  return {msg = agent.work_area(player, p.bbox, what, p.title, plans.fuel_at(p), p.id)}
end)

-- one-line description of an apply result (GUI / chat)
function plans.result_text(r)
  if not r then return "" end
  local s = "Placed " .. r.placed .. " ghost(s)"
  if r.existing > 0 then s = s .. ", " .. r.existing .. " already there" end
  if r.blocked > 0 then s = s .. ", " .. r.blocked .. " blocked (marked red)" end
  if r.deconstruct > 0 then s = s .. ", " .. r.deconstruct .. " tree(s)/rock(s) marked for removal" end
  if r.removes then
    s = s .. ", " .. r.removes.marked .. " of your entities marked to dismantle"
    if r.removes.changed > 0 then s = s .. " (" .. r.removes.changed .. " had changed and stay)" end
  end
  if (r.deferred or 0) > 0 then s = s .. ", " .. r.deferred .. " ghost(s) wait for the dismantling" end
  if r.error then s = s .. ". Stopped early: " .. r.error end
  return s .. "."
end

events.on_init(init)
events.on_configuration_changed(function()
  init()
  -- a draw/apply job of an older version may have been dropped: queue unfinished ones again
  for _, p in pairs(storage.plans) do
    if p.apply or p.draw then queue_job(p) end
  end
end)

return plans
