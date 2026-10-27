-- Test-server only: place every buildable entity in 4 directions and record what the engine did
-- (position, direction, bounding box, pipe connections). Writes script-output/claude/test_geometry.json.
local s = game.surfaces.nauvis
local f = game.forces.player
f.enable_all_recipes()
local X0, Y0 = 512, 512
s.request_to_generate_chunks({X0, Y0}, 2)
s.force_generate_chunk_requests()
local area = {{X0 - 40, Y0 - 40}, {X0 + 40, Y0 + 40}}
for _, e in pairs(s.find_entities(area)) do e.destroy() end
local tiles = {}
for x = X0 - 40, X0 + 40 do for y = Y0 - 40, Y0 + 40 do tiles[#tiles + 1] = {name = "landfill", position = {x, y}} end end
s.set_tiles(tiles)
local water = {}
for x = X0 - 40, X0 + 40 do for y = Y0 + 3, Y0 + 40 do water[#water + 1] = {name = "water", position = {x, y}} end end
local out = {}
local function rec(name, d, px, py, e)
  local b = e.bounding_box
  local r = {name = name, dir = d, req = {px, py}, pos = {e.position.x, e.position.y}, edir = e.direction,
    box = {b.left_top.x, b.left_top.y, b.right_bottom.x, b.right_bottom.y}, conns = {}}
  for i = 1, e.fluids_count do
    local ok, cs = pcall(e.get_fluid_box_pipe_connections, i)
    if ok then
      for _, c in ipairs(cs or {}) do
        r.conns[#r.conns + 1] = {i, c.connection_type, c.flow_direction, c.position.x, c.position.y,
          c.target_position.x, c.target_position.y}
      end
    end
  end
  return r
end
local only = ONLY  -- optional set of names
for name, p in pairs(prototypes.entity) do
  local itp = p.items_to_place_this
  if itp and #itp > 0 and (not only or only[name]) then
    for _, d in ipairs({0, 4, 8, 12}) do
      local w, h = p.tile_width, p.tile_height
      if d == 4 or d == 12 then w, h = h, w end
      for k, off in ipairs({{0, 0}, {0.3, 0.2}}) do
        local px, py = X0 + w / 2 + off[1], Y0 + h / 2 + off[2]
        if p.type == "offshore-pump" then s.set_tiles(water) end
        local ok, e = pcall(s.create_entity, {name = name, position = {px, py}, direction = d, force = f,
          raise_built = false, create_build_effect_smoke = false})
        if ok and e and e.valid then
          local r = rec(name, d, px, py, e)
          r.k = k
          out[#out + 1] = r
          e.destroy()
        else
          out[#out + 1] = {name = name, dir = d, k = k, error = ok and "nil" or tostring(e)}
        end
        if p.type == "offshore-pump" then s.set_tiles(tiles) end
        for _, o in pairs(s.find_entities(area)) do if o.valid then o.destroy() end end
      end
    end
  end
end
helpers.write_file("claude/test_geometry.json", helpers.table_to_json(out), false)
rcon.print(#out)
