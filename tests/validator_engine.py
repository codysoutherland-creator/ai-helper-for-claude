"""Engine harness for cross-checking validate.py on a headless test slot (not a pytest module).

    eng = Engine(slot=8)                         # RCON 27016+slot, pw testpw
    eng.build(entities, origin=(1000, 1000), resources=[("iron-ore", x, y, amount)], water=[(x, y)],
              extras=[...], fuel=[(x, y)])       # local coords, same PE dicts the validator gets
    eng.run(ticks)                               # advance the game (game.speed raised while waiting)
    info = eng.inspect()                         # per entity: status, products, belt/fluid/power facts

Everything happens on the slot's own map in a cleared pad around `origin` (grass tiles, nothing else).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factorio_claude import rcon  # noqa: E402

LUA_LIB = r"""
V = V or {}
local S = game.surfaces[1]
function V.pad(x0, y0, x1, y1)
  S.request_to_generate_chunks({(x0 + x1) / 2, (y0 + y1) / 2}, math.ceil(math.max(x1 - x0, y1 - y0) / 64) + 1)
  S.force_generate_chunk_requests()
  for _, e in pairs(S.find_entities_filtered{area = {{x0 - 2, y0 - 2}, {x1 + 2, y1 + 2}}}) do
    if e.valid and e.type ~= "character" then e.destroy() end
  end
  local t = {}
  for x = x0, x1 - 1 do for y = y0, y1 - 1 do t[#t + 1] = {name = "grass-1", position = {x, y}} end end
  S.set_tiles(t)
  S.destroy_decoratives{area = {{x0, y0}, {x1, y1}}}
end
function V.build(spec)
  local ox, oy = spec.ox, spec.oy
  if spec.water and #spec.water > 0 then
    local t = {}
    for _, w in pairs(spec.water) do t[#t + 1] = {name = "water", position = {w[1] + ox, w[2] + oy}} end
    S.set_tiles(t)
  end
  for _, r in pairs(spec.resources or {}) do
    S.create_entity{name = r[1], position = {r[2] + ox + 0.5, r[3] + oy + 0.5}, amount = r[4]}
  end
  V.ents, V.ox, V.oy = {}, ox, oy
  V.sets = V.sets or {}
  V.sets[spec.key or "default"] = {ents = V.ents, ox = ox, oy = oy}
  local out = {}
  for i, e in pairs(spec.entities) do
    local pos = {e.x + ox, e.y + oy}
    local can = S.can_place_entity{name = e.name, position = pos, direction = e.direction or 0, force = "player",
      build_check_type = defines.build_check_type.manual}
    local p = {name = e.name, position = pos, direction = e.direction or 0, force = "player",
      create_build_effect_smoke = false}
    if e.recipe then p.recipe = e.recipe end
    if e.ug then p.type = e.ug end
    local ok, ent = pcall(S.create_entity, p)
    if ok and ent then V.ents[i] = ent end
    out[i] = {can = can, made = (ok and ent ~= nil) or false, err = (not ok) and tostring(ent) or nil}
  end
  for _, x in pairs(spec.extras or {}) do
    local p = {name = x.name, position = {x.x + ox, x.y + oy}, direction = x.direction or 0, force = "player"}
    if x.ug then p.type = x.ug end
    local ent = S.create_entity(p)
    if ent and x.infinity then
      local fs = {}
      for k, it in pairs(x.infinity) do fs[k] = {name = it, count = 50, mode = "exactly", index = k} end
      ent.infinity_container_filters = fs
    end
    if ent and x.chest_items then for _, it in pairs(x.chest_items) do ent.insert{name = it[1], count = it[2]} end end
    if ent and x.loader then ent.loader_type = x.loader end
    if ent and x.eei then ent.power_production = x.eei ent.electric_buffer_size = x.eei * 2 end
  end
  for _, f in pairs(spec.fuel or {}) do
    for _, ent in pairs(S.find_entities_filtered{position = {f[1] + ox, f[2] + oy}, force = "player"}) do
      if ent.get_fuel_inventory() then ent.get_fuel_inventory().insert{name = f[3] or "coal", count = f[4] or 50} end
    end
  end
  return out
end
local function rel(p) return p and {p.x - V.ox, p.y - V.oy} or nil end
function V.contents(a)
  local set = V.sets[a.key or "default"]
  local n = 0
  for _, e in pairs(S.find_entities_filtered{position = {a.x + set.ox, a.y + set.oy}}) do
    local inv = e.get_inventory(defines.inventory.chest)
    if inv then n = n + inv.get_item_count(a.item) end
  end
  return n
end
function V.inspect(key)
  local set = V.sets[key or "default"]
  V.ents, V.ox, V.oy = set.ents, set.ox, set.oy
  local out = {}
  for i, e in pairs(V.ents) do
    if e.valid then
      local r = {name = e.name, pos = rel(e.position), status = e.status, type = e.type}
      if e.type == "transport-belt" then r.shape = e.belt_shape end
      if e.type == "transport-belt" or e.type == "underground-belt" or e.type == "splitter" then
        local n = 0
        for li = 1, e.get_max_transport_line_index() do
          local l = e.get_transport_line(li)
          r["l" .. li] = l.get_contents()
          n = n + l.get_item_count()
        end
        r.items = n
        local nb = e.belt_neighbours
        r.outs = {}
        for _, o in pairs(nb.outputs) do r.outs[#r.outs + 1] = rel(o.position) end
      end
      if e.type == "underground-belt" then
        r.ug = e.belt_to_ground_type
        local nb = e.underground_belt_neighbour
        r.ug_partner = nb and rel(nb.position) or nil
      end
      if e.type == "inserter" then
        r.pickup = e.pickup_target and e.pickup_target.name or nil
        r.drop = e.drop_target and e.drop_target.name or nil
        r.pickup_pos, r.drop_pos = rel(e.pickup_position), rel(e.drop_position)
      end
      if e.type == "mining-drill" then
        r.drop = e.drop_target and e.drop_target.name or nil
        r.drop_pos = rel(e.drop_position)
        r.mining_target = e.mining_target and e.mining_target.name or nil
      end
      if e.type == "assembling-machine" or e.type == "furnace" then
        r.products_finished = e.products_finished
        local rc = e.get_recipe()
        r.recipe = rc and rc.name or nil
        local o = e.get_output_inventory()
        r.output = o and o.get_contents() or nil
      end
      if e.burner then
        local fi = e.get_fuel_inventory()
        r.fuel = fi and fi.get_item_count() or 0
        r.burning = e.burner.currently_burning and e.burner.currently_burning.name.name or nil
      end
      if e.type == "container" or e.type == "infinity-container" then
        r.contents = e.get_inventory(defines.inventory.chest).get_contents()
      end
      local okn, nid = pcall(function() return e.electric_network_id end)
      if okn and nid then r.net = nid end
      local okc, con = pcall(e.is_connected_to_electric_network)
      if okc then r.powered = con end
      if e.type == "generator" then r.gen = e.energy_generated_last_tick end
      local nfb = e.fluids_count
      if nfb > 0 then
        r.seg = {}
        r.fluid = {}
        for k = 1, nfb do
          local oks, sid = pcall(e.get_fluid_segment_id, k)
          r.seg[k] = oks and sid or -1
          local okf, f = pcall(e.get_fluid, k)
          r.fluid[k] = (okf and f) and (f.name .. ":" .. string.format("%.1f", f.amount)) or ""
        end
      end
      out[i] = r
    else
      out[i] = {gone = true}
    end
  end
  return out
end
"""


class Engine:
    def __init__(self, slot: int = 8):
        rcon._client = rcon.Rcon("127.0.0.1", 27016 + slot, "testpw")
        rcon._lua_armed = False
        self.lua("game.forces.player.enable_all_recipes() game.surfaces[1].peaceful_mode = true "
                 "game.map_settings.enemy_expansion.enabled = false game.forces.enemy.kill_all_units() "
                 "game.map_settings.pollution.enabled = false rcon.print('ok')")
        self.load_lib()

    def lua(self, code: str) -> str:
        return rcon.lua(code)

    def load_lib(self):
        out = self.lua(LUA_LIB.replace("\n", " ") + " rcon.print('lib')")
        assert out.strip().endswith("lib"), out

    def call(self, fn: str, arg=None):
        if arg is None:
            a = "nil"
        elif isinstance(arg, str):
            a = json.dumps(arg)
        else:
            a = f"helpers.json_to_table([==[{json.dumps(arg)}]==])"
        out = self.lua(f"rcon.print(helpers.table_to_json({{r = {fn}({a})}}))").strip()
        try:
            return json.loads(out)["r"] if out else None
        except json.JSONDecodeError:
            raise RuntimeError(out[:2000]) from None

    def pad(self, x0, y0, x1, y1):
        self.lua(f"V.pad({x0}, {y0}, {x1}, {y1}) rcon.print('ok')")

    def build(self, entities, origin, resources=(), water=(), fuel=(), extras=(), pad=24, key="default"):
        ents = []
        for e in entities:
            if isinstance(e, (list, tuple)):
                e = {"name": e[0], "x": e[1], "y": e[2], "direction": e[3] if len(e) > 3 else 0,
                     "recipe": e[4] if len(e) > 4 else None}
            d = {"name": e["name"], "x": e["x"], "y": e["y"], "direction": e.get("direction") or 0}
            if e.get("recipe"):
                d["recipe"] = e["recipe"]
            ug = (e.get("extra") or {}).get("type") or e.get("ug")
            if ug:
                d["ug"] = ug
            ents.append(d)
        xs = [e["x"] for e in ents] + [x[1] for x in resources] + [w[0] for w in water] + [x["x"] for x in extras]
        ys = [e["y"] for e in ents] + [x[2] for x in resources] + [w[1] for w in water] + [x["y"] for x in extras]
        ox, oy = origin
        self.pad(int(ox + min(xs)) - pad, int(oy + min(ys)) - pad, int(ox + max(xs)) + pad, int(oy + max(ys)) + pad)
        spec = {"key": key, "ox": ox, "oy": oy, "entities": ents, "resources": [list(r) for r in resources],
                "water": [list(w) for w in water], "fuel": [list(f) for f in fuel], "extras": list(extras)}
        return self.call("V.build", spec)

    def run(self, ticks: int, speed: float = 64.0, timeout: float = 120.0):
        t0 = int(self.lua("rcon.print(game.tick)").strip())
        self.lua(f"game.speed = {speed}")
        end = time.time() + timeout
        try:
            while time.time() < end:
                if int(self.lua("rcon.print(game.tick)").strip()) >= t0 + ticks:
                    return
                time.sleep(0.2)
        finally:
            self.lua("game.speed = 1")
        raise TimeoutError(f"game didn't advance {ticks} ticks")

    def contents(self, key: str, x: float, y: float, item: str | None = None) -> int:
        """Items in a chest at local (x, y) of build `key` (works for engine-only extras too)."""
        a = {"key": key, "x": x, "y": y}
        if item:
            a["item"] = item
        return int(self.call("V.contents", a) or 0)

    def inspect(self, key: str = "default") -> dict:
        r = self.call("V.inspect", key)
        if isinstance(r, list):
            return {i + 1: v for i, v in enumerate(r)}
        return {int(k): v for k, v in (r or {}).items()}
