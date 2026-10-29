-- export_map: dump charted chunks (water/void tiles, resources, obstacles, force entities) as JSON files under
-- script-output/claude/map/<surface>/<run_id>/ for the Python planner (planner/world.py). Big areas are spread
-- over ticks (job state in storage.claude_export); `export_status {run_id}` reports progress.
-- See SPEC-v3-contracts.md "export_map" for the file format.

local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")

local M = {}

local SYNC_CHUNKS = 6       -- exports this small finish inside the op call
-- Cost model (profiled in 2.1.23 on 1225 chunks): ~3.5 ms per chunk (write_file dominates, ~2 ms per call even for
-- tiny files) + ~0.005 ms per entity/tile object (scan + JSON). 2500-unit slices measured mean 25 / max 41 ms (disk jitter), so 2000.
local CHUNK_COST = 380       -- work units per chunk (1 unit ~ 0.01 ms)
local TICK_BUDGET = 2000     -- work units per tick (~20 ms mean)
local KEEP_RUNS = 2         -- finished runs kept on disk per surface (older dirs are removed)
local KEEP_TICKS = 36000    -- finished job state kept for export_status (10 min)
local ALL_INCLUDE = {tiles = true, resources = true, obstacles = true, entities = true}

local void_names, void_set  -- tile prototypes that are out-of-map / empty space
local obstacle_layer = {}   -- entity name -> collides with buildings (cached)

local function tile_sets()
  if not void_names then
    void_names, void_set = {}, {}
    for name, p in pairs(prototypes.tile) do
      local l = p.collision_mask.layers
      if l.out_of_map or l.empty_space then void_names[#void_names + 1] = name; void_set[name] = true end
    end
  end
  return void_names, void_set
end

local function blocks_building(e)
  local v = obstacle_layer[e.name]
  if v == nil then
    local l = e.prototype.collision_mask.layers
    v = (l.object or l.is_object or l.is_lower_object or l.player) and true or false
    obstacle_layer[e.name] = v
  end
  return v
end

local function st()
  local s = storage.claude_export
  if not s then s = {jobs = {}, seq = 0, runs = {}}; storage.claude_export = s end
  return s
end

-- Tile bounds {x0,y0,x1,y1} (x1/y1 exclusive) covered by an entity's collision box (rotated boxes -> AABB).
local function tile_box(e)
  local bb = e.bounding_box
  local lt, rb = bb.left_top, bb.right_bottom
  local x0, y0, x1, y1 = lt.x, lt.y, rb.x, rb.y
  if bb.orientation and bb.orientation ~= 0 then
    local cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    local hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    local a = bb.orientation * 2 * math.pi
    local c, s = math.abs(math.cos(a)), math.abs(math.sin(a))
    local ex, ey = hw * c + hh * s, hw * s + hh * c
    x0, y0, x1, y1 = cx - ex, cy - ey, cx + ex, cy + ey
  end
  return {math.floor(x0 + 0.01), math.floor(y0 + 0.01), math.ceil(x1 - 0.01), math.ceil(y1 - 0.01)}
end

-- For rotated boxes (corner cliffs): the tiles the oriented box really overlaps (separating-axis test), else nil.
local function oriented_tiles(e, box)
  local bb = e.bounding_box
  if not bb.orientation or bb.orientation == 0 then return nil end
  local lt, rb = bb.left_top, bb.right_bottom
  local cx, cy = (lt.x + rb.x) / 2, (lt.y + rb.y) / 2
  local hw, hh = (rb.x - lt.x) / 2 - 0.01, (rb.y - lt.y) / 2 - 0.01
  local a = bb.orientation * 2 * math.pi
  local ux, uy, vx, vy = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
  local out = {}
  for ty = box[2], box[4] - 1 do
    for tx = box[1], box[3] - 1 do
      local dx, dy = tx + 0.5 - cx, ty + 0.5 - cy
      -- tile axes are covered by the AABB loop; test the box's own two axes
      if math.abs(dx * ux + dy * uy) < hw + 0.5 * (math.abs(ux) + math.abs(uy))
        and math.abs(dx * vx + dy * vy) < hh + 0.5 * (math.abs(vx) + math.abs(vy)) then
        out[#out + 1] = {tx, ty}
      end
    end
  end
  return out
end

-- 32 row strings ('1' where tile is in the list), or nil.
local function rows_of(tiles, x0, y0, skip)
  local grid, any = {}, false
  for _, t in pairs(tiles) do
    if not (skip and skip[t.name]) then
      local p = t.position
      local r = grid[p.y - y0 + 1]
      if not r then r = {}; grid[p.y - y0 + 1] = r end
      r[p.x - x0 + 1] = true
      any = true
    end
  end
  if not any then return nil end
  local rows = {}
  for j = 1, 32 do
    local r, cs = grid[j], {}
    for i = 1, 32 do cs[i] = (r and r[i]) and "1" or "0" end
    rows[j] = table.concat(cs)
  end
  return rows
end

local FULL_ROWS
local function full_rows()
  if not FULL_ROWS then FULL_ROWS = {}; for j = 1, 32 do FULL_ROWS[j] = string.rep("1", 32) end end
  return FULL_ROWS
end

local KIND = {tree = "tree", cliff = "cliff", ["simple-entity"] = "rock"}
local SKIP_FORCE_TYPES = {character = true, ["tile-ghost"] = true, ["item-request-proxy"] = true,
  ["character-corpse"] = true, ["deconstructible-tile-proxy"] = true}
local CRAFTERS = {["assembling-machine"] = true, furnace = true, ["rocket-silo"] = true}

-- Export one chunk; returns (data table, work units).
local function export_chunk(surface, force, cx, cy, inc)
  local x0, y0 = cx * 32, cy * 32
  local area = {{x0, y0}, {x0 + 32, y0 + 32}}
  local out = {cx = cx, cy = cy}
  local work = CHUNK_COST
  if inc.tiles then
    local vnames, vset = tile_sets()
    local nvoid = (#vnames > 0) and surface.count_tiles_filtered{area = area, name = vnames} or 0
    local nwater = surface.count_tiles_filtered{area = area, collision_mask = "water_tile"} - nvoid
    if nvoid > 0 then
      local vt = surface.find_tiles_filtered{area = area, name = vnames}
      out.void = (nvoid == 1024) and full_rows() or rows_of(vt, x0, y0)
      work = work + #vt / 2
    end
    if nwater > 0 then
      if nwater == 1024 then out.water = full_rows()
      else
        local wt = surface.find_tiles_filtered{area = area, collision_mask = "water_tile"}
        out.water = rows_of(wt, x0, y0, vset)
        work = work + #wt / 2
      end
    end
  end
  local res, obs, ents = {}, {}, {}
  if inc.resources or inc.obstacles or inc.entities then
    local list = surface.find_entities_filtered{area = area}
    work = work + #list / 2
    for _, e in pairs(list) do
      local p = e.position
      if p.x >= x0 and p.x < x0 + 32 and p.y >= y0 and p.y < y0 + 32 then  -- owner chunk only (no duplicates)
        local t = e.type
        local fname = e.force.name
        if t == "resource" then
          if inc.resources then res[#res + 1] = {e.name, p.x, p.y, e.amount} end
        elseif fname == "neutral" or fname == "enemy" then
          if inc.obstacles and blocks_building(e) then
            local kind = KIND[t] or (fname == "enemy" and "enemy" or "other_neutral")
            local orient = (t == "cliff") and e.cliff_orientation or ""
            local box = tile_box(e)
            obs[#obs + 1] = {kind, e.name, p.x, p.y, orient, box, oriented_tiles(e, box)}
          end
        elseif inc.entities and fname == force and not SKIP_FORCE_TYPES[t] then
          local name, extra = e.name, {type = t}
          if t == "entity-ghost" then
            name = "ghost:" .. e.ghost_name
            extra.type = e.ghost_type
            if e.tags then extra.tags = e.tags end
          end
          local ct = extra.type
          if CRAFTERS[ct] then
            local ok, r = pcall(e.get_recipe)
            if ok and r then extra.recipe = r.name end
          end
          if ct == "underground-belt" then
            local ok, u = pcall(function() return e.belt_to_ground_type end)
            if ok then extra.ug = u end
          end
          extra.b = tile_box(e)
          if e.unit_number then extra.unit = e.unit_number end
          ents[#ents + 1] = {name, p.x, p.y, e.direction, extra}
        end
      end
    end
  end
  if inc.resources then out.resources = res end
  if inc.obstacles then out.obstacles = obs end
  if inc.entities then out.entities = ents end
  return out, work
end

local function write_index(job)
  local idx = {surface = job.surface_name, tick = job.tick, run_id = job.run_id, chunks = job.done_chunks,
    bbox = job.bbox, area = job.area, include = job.include_list, charted_only = job.charted,
    done_tick = game.tick, errors = job.errors, error = job.error}
  helpers.write_file(job.dir .. "/index.json", helpers.table_to_json(idx))
end

-- surface names become folder names under script-output: keep them to [A-Za-z0-9-_]
local function safe_dir(name)
  return (tostring(name):gsub("[^%w%-_]", "_"))
end

local function prune_runs(surface_name, run_id)
  local s = st()
  local runs = s.runs[surface_name] or {}
  s.runs[surface_name] = runs
  runs[#runs + 1] = run_id
  while #runs > KEEP_RUNS do
    local old = table.remove(runs, 1)
    pcall(helpers.remove_path, "claude/map/" .. safe_dir(surface_name) .. "/" .. old)
  end
end

local function finish(job)
  job.done = true
  job.finished_tick = game.tick
  write_index(job)
  prune_runs(job.surface_name, job.run_id)
end

-- Process chunks of one job until the work budget is used. Returns work spent.
local function step(job, budget)
  local surface = game.get_surface(job.surface_index)
  if not surface then job.error = "surface gone"; finish(job); return 0 end
  local force = job.force
  local spent = 0
  while job.i < #job.chunks and spent < budget do
    job.i = job.i + 1
    local c = job.chunks[job.i]
    local ok, data, work = pcall(export_chunk, surface, force, c[1], c[2], job.include)
    if ok then
      helpers.write_file(job.dir .. "/c_" .. c[1] .. "_" .. c[2] .. ".json", helpers.table_to_json(data))
      job.done_chunks[#job.done_chunks + 1] = c
      spent = spent + work
    else
      job.errors = job.errors + 1
      job.error = tostring(data)
      spent = spent + CHUNK_COST
    end
  end
  if job.i >= #job.chunks then finish(job) end
  return spent
end

local function area_of(a)
  if not a then return nil end
  if a.x0 then return a.x0, a.y0, a.x1, a.y1 end
  if a[1] and type(a[1]) == "number" then return a[1], a[2], a[3], a[4] end
  local lt, rb = util.pos_of(a.left_top or a[1]), util.pos_of(a.right_bottom or a[2])
  return lt.x, lt.y, rb.x, rb.y
end

-- args: surface?, area? {x0,y0,x1,y1} (tiles; default: whole surface), include? [...],
--       force? (default "player"), charted? (default true; false = every generated chunk, for headless tests)
function M.start(args)
  local surface = util.get_surface(args)
  local force = util.get_force(args)
  if not force then error("no force " .. tostring(args.force)) end
  local charted = args.charted ~= false
  local include, include_list = {}, {}
  for _, k in pairs(args.include or {"tiles", "resources", "obstacles", "entities"}) do
    if ALL_INCLUDE[k] then include[k] = true; include_list[#include_list + 1] = k end
  end
  local chunks = {}
  local x0, y0, x1, y1 = area_of(args.area)
  local function want(cx, cy)
    if not surface.is_chunk_generated({cx, cy}) then return false end
    return (not charted) or force.is_chunk_charted(surface, {cx, cy})
  end
  if x0 then
    for cy = math.floor(y0 / 32), math.ceil(y1 / 32) - 1 do
      for cx = math.floor(x0 / 32), math.ceil(x1 / 32) - 1 do
        if want(cx, cy) then chunks[#chunks + 1] = {cx, cy} end
      end
    end
  else
    for c in surface.get_chunks() do
      if want(c.x, c.y) then chunks[#chunks + 1] = {c.x, c.y} end
    end
  end
  local bx0, by0, bx1, by1
  for _, c in pairs(chunks) do
    bx0 = math.min(bx0 or c[1], c[1]); by0 = math.min(by0 or c[2], c[2])
    bx1 = math.max(bx1 or c[1], c[1]); by1 = math.max(by1 or c[2], c[2])
  end
  local s = st()
  if not s.runs[surface.name] then  -- first export in this save: drop leftovers from other saves/sessions
    pcall(helpers.remove_path, "claude/map/" .. safe_dir(surface.name))
    s.runs[surface.name] = {}
  end
  s.seq = s.seq + 1
  local run_id = game.tick .. "_" .. s.seq
  local job = {run_id = run_id, surface_index = surface.index, surface_name = surface.name, force = force.name,
    chunks = chunks, i = 0, include = include, include_list = include_list, charted = charted,
    dir = "claude/map/" .. safe_dir(surface.name) .. "/" .. run_id, done_chunks = {}, errors = 0, done = false, tick = game.tick,
    area = x0 and {x0 = x0, y0 = y0, x1 = x1, y1 = y1} or nil,
    bbox = bx0 and {x0 = bx0 * 32, y0 = by0 * 32, x1 = (bx1 + 1) * 32, y1 = (by1 + 1) * 32} or nil}
  s.jobs[run_id] = job
  if #chunks <= SYNC_CHUNKS then step(job, math.huge) end
  return {run_id = run_id, dir = job.dir, chunks = #chunks, tick = game.tick, done = job.done}
end

function M.status(args)
  local job = st().jobs[tostring(args.run_id)]
  if not job then error("unknown run_id " .. tostring(args.run_id)) end
  return {done = job.done, written = #job.done_chunks, total = #job.chunks, errors = job.errors, error = job.error,
    dir = job.dir}
end

local function on_tick()
  local s = storage.claude_export
  if not s or next(s.jobs) == nil then return end
  local budget = TICK_BUDGET
  for id, job in pairs(s.jobs) do
    if not job.done then
      local ok, err = pcall(step, job, budget)
      if not ok then job.error = tostring(err); pcall(finish, job) end
      return  -- one active job per tick
    elseif game.tick - (job.finished_tick or 0) > KEEP_TICKS then
      s.jobs[id] = nil
      return
    end
  end
end

M.export_chunk, M.step, M.TICK_BUDGET = export_chunk, step, TICK_BUDGET  -- exposed for profiling from the console
api.register("export_map", M.start)
api.register("export_status", M.status)
events.on(defines.events.on_tick, function() pcall(on_tick) end)

return M
