"""Engine checks E1-E7 for the planner v3 blocks (docs/design/PLANNER-v3.md section 14) on a headless test slot.
Not collected by pytest: it needs a server.

    .venv\\Scripts\\python.exe tests\\live_v3_blocks.py [--slot 31] [--keep] [case ...]

Restarts test slot <slot> (instance "v3blocks", seed_1001.zip, debug ops on) and builds every case through the
mod's place_ghosts + instant_build ops on cleared ground (ore is created where drills need it: test setup only).
Feeds are infinity chests; power comes from an electric-energy-interface. Runs at game.speed 10 and reads the
entities back:

  E1 field_lanes      a burner drill in the belt-side corner of an east 3x3 slot drops on the same belt lane as an
                      electric drill in an east slot (the right lane; the left lane stays empty)
  E2 field_selffeed   burner drills that start EMPTY run on coal their burner inserter (also started empty) takes
                      from the corridor belt
  E3 column_merge     ore side-loaded from the west and coal from the east of the column's merge: middle belt left
                      lane = ore only, right lane = coal only; every furnace smelts; the burner input inserters
                      started empty
  E4 out_lifetime     plates a burner output inserter moves per coal (pre-fuel lifetime)
  E5 swap_ghosts      an electric-drill ghost fits over a burner drill + burner inserter marked for deconstruction
                      (manual_ghost), an inserter ghost over a marked burner inserter, a belt ghost over a marked chest
  E6 field_poles      gap-row poles (pitch 7) power every electric drill of a 2-period field from one source
  E7 boiler_blackstart  2 boiler units, burner inserters and boilers started empty, coal only on the belt: the
                      engines generate
  E8 green            the green block fed with plates: 0.5 logistic science packs/s on one lane of the spine
  E9 column_electric  the smelting column's electric phase (electric input + output inserters, three pole columns)
  E10 coal_selffeed   a self-feeding coal field primed with the helper's start fuel keeps itself running
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factorio_claude import rcon  # noqa: E402
from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402

PRELUDE = r"""
local s, f = game.surfaces.nauvis, game.forces.player
local V = {[0] = {0, -1}, [4] = {1, 0}, [8] = {0, 1}, [12] = {-1, 0}}
local function mk(name, x, y, dir)
  local e = s.create_entity{name = name, position = {x, y}, direction = dir or 0, force = f}
  if not e then error("cannot create " .. name .. " at " .. x .. "," .. y) end
  return e
end
local function belt_at(x, y) return s.find_entities_filtered{type = "transport-belt", position = {x + .5, y + .5}}[1] end
local function lanes(x, y)
  local bt = belt_at(x, y)
  local out = {{}, {}}
  if bt then for li = 1, 2 do
    for _, c in pairs(bt.get_transport_line(li).get_contents()) do out[li][c.name] = (out[li][c.name] or 0) + c.count end
  end end
  return out
end
local ST = {}
for k, v in pairs(defines.entity_status) do ST[v] = k end
"""


def lua(code):
    out = rcon.lua(PRELUDE + code)
    if "Error" in out[:400] or "error" in out[:200]:
        raise RuntimeError(out[:3000])
    return out


def lua_json(code):
    return json.loads(lua(code).strip().splitlines()[-1])


def restart(slot, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", "v3blocks", "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", "seed_1001.zip"]
    log = ROOT / "testenv" / "inst" / "v3blocks_restart.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=180)
    return log.read_text(errors="replace")


def clear(x0, y0, x1, y1, ore=None, ore_rect=None):
    """Destroy everything in the area, lay grass, optionally create ore (test setup)."""
    orecode = ""
    if ore:
        ox0, oy0, ox1, oy1 = ore_rect
        orecode = f"""
for x = {ox0}, {ox1 - 1} do for y = {oy0}, {oy1 - 1} do
  s.create_entity{{name = "{ore}", position = {{x + .5, y + .5}}, amount = 50000}}
end end"""
    lua(f"""
for _, e in pairs(s.find_entities_filtered{{area = {{{{{x0}, {y0}}}, {{{x1}, {y1}}}}}}}) do
  if e.type ~= "character" then e.destroy() end
end
local t = {{}}
for x = {x0}, {x1 - 1} do for y = {y0}, {y1 - 1} do t[#t + 1] = {{name = "grass-1", position = {{x, y}}}} end end
s.set_tiles(t)
{orecode}
rcon.print("ok")""")


def build(ents, ox, oy, label):
    gh = []
    for e in ents:
        g = {"name": e.name, "x": e.x + ox, "y": e.y + oy, "direction": int(e.direction or 0)}
        if e.recipe:
            g["recipe"] = e.recipe
        if isinstance(e.extra, dict) and e.extra.get("type") in ("input", "output"):
            g["type"] = e.extra["type"]       # underground belt half
        gh.append(g)
    r = rcon.bridge("place_ghosts", ghosts=gh, plan=f"v3-{label}")
    if r.get("failed"):
        raise RuntimeError(f"{label}: ghosts failed {r['failed'][:6]}")
    xs = [g["x"] for g in gh]
    ys = [g["y"] for g in gh]
    r2 = rcon.bridge("instant_build", area=[[min(xs) - 3, min(ys) - 3], [max(xs) + 3, max(ys) + 3]])
    if r2.get("built") != len(gh):
        raise RuntimeError(f"{label}: built {r2.get('built')}/{len(gh)}")


def source(x, y, pole_x, pole_y, mode="source", kw=0):
    """Electric-energy-interface + pole at (x, y), wired to the pole at (pole_x, pole_y)."""
    lua(f"""
local eei = mk("electric-energy-interface", {x} + 1, {y} + 1)
eei.electric_buffer_size = 1e9
if "{mode}" == "source" then eei.power_production = 1e8 / 60; eei.power_usage = 0
else eei.power_production = 0; eei.power_usage = {kw} * 1000 / 60 end
local p = mk("small-electric-pole", {x} + 2.5, {y} + .5)
local t = s.find_entities_filtered{{type = "electric-pole", position = {{{pole_x}, {pole_y}}}}}[1]
p.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(
  t.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)
rcon.print("ok")""")


def feed(x, y, d, item):
    """Belt at port (x, y) flowing d, fed from an infinity chest by a (fuelled) burner inserter behind it."""
    lua(f"""
local v = V[{d}]
if not belt_at({x}, {y}) then mk("transport-belt", {x} + .5, {y} + .5, {d}) end
local ins = mk("burner-inserter", {x} - v[1] + .5, {y} - v[2] + .5, ({d} + 8) % 16)
ins.insert{{name = "coal", count = 50}}
local ch = mk("infinity-chest", {x} - 2 * v[1] + .5, {y} - 2 * v[2] + .5)
ch.set_infinity_container_filter(1, {{name = "{item}", count = 50, index = 1}})
rcon.print("ok")""")


RESERVOIRS = []      # (x, y, d, item, n): belts run() keeps topped up (test feed faster than an inserter)


def reservoir(x, y, d, item, n=60):
    """Feed for blocks that need more than an inserter moves: a straight belt of n tiles ending at port (x, y)
    flowing d, both lanes filled with `item` and topped up by run() every second (test setup)."""
    lua(f"""
local v = V[{d}]
for k = 0, {n - 1} do
  local bx, by = {x} - k * v[1], {y} - k * v[2]
  if not belt_at(bx, by) then mk("transport-belt", bx + .5, by + .5, {d}) end
end
rcon.print("ok")""")
    RESERVOIRS.append((x, y, d, item, n))
    refill()


DRAINS = []          # (x, y, d, n): belts run() empties every second (a sink that never backs up)


def refill():
    for x, y, d, n in DRAINS:
        lua(f"""
local v = V[{d}]
for k = 0, {n - 1} do
  local bt = belt_at({x} + k * v[1], {y} + k * v[2])
  if bt then for li = 1, 2 do bt.get_transport_line(li).clear() end end
end
rcon.print("ok")""")
    for x, y, d, item, n in RESERVOIRS:
        lua(f"""
local v = V[{d}]
for k = 1, {n - 1} do
  local bt = belt_at({x} - k * v[1], {y} - k * v[2])
  if bt then for li = 1, 2 do
    local tl = bt.get_transport_line(li)
    for _ = 1, 4 do if not tl.insert_at_back({{name = "{item}", count = 1}}) then break end end
  end end
end
rcon.print("ok")""")


def sink(x, y, d, n=5):
    lua(f"""
local v = V[{d}]
for k = 0, {n - 1} do
  local bx, by = {x} + k * v[1], {y} + k * v[2]
  if not belt_at(bx, by) then mk("transport-belt", bx + .5, by + .5, {d}) end
end
rcon.print("ok")""")


def run(seconds, speed=10):
    lua(f"game.speed = {speed} rcon.print('ok')")
    t_end = time.monotonic() + seconds / speed + 2
    tick0 = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
    while time.monotonic() < t_end + 30:
        time.sleep(1.0)
        refill()
        tick = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
        if tick - tick0 >= seconds * 60:
            break
    lua("game.speed = 1 rcon.print('ok')")


# ---- cases ---------------------------------------------------------------------------------------------------------
def case_field(gd, X, Y):
    """E1 + E2 + E6 on one 8-slot field: east slots 1 and 3 burner (empty), east slot 5 electric, west slots 0, 2,
    4, 6 electric, gap-row poles; ore under the whole lattice; coal into the corridor's tail port."""
    blk = B.mining_field(8, gd, resource="iron-ore")
    keep = []
    for e in blk.entities:
        x = e.extra or {}
        role, u = x.get("role"), x.get("unit")
        if role in ("drill_b", "ins_b") and u in (1, 3):
            keep.append(e)
        elif role == "drill_e" and u in (0, 2, 4, 5, 6):
            keep.append(e)
        elif role in ("ore_belt", "corr", "field_pole"):
            keep.append(e)
    w, h = blk.size
    clear(X - 8, Y - 10, X + w + 12, Y + h + 8, "iron-ore", (X, Y, X + w - 1, Y + h))
    build(keep, X, Y, "field")
    ports = blk.ports
    ore_out, coal_in, coal_out = ports[0], ports[1], ports[2]
    feed(X + coal_in.x, Y + coal_in.y, coal_in.direction, "coal")
    sink(X + ore_out.x, Y + ore_out.y, ore_out.direction, 6)
    sink(X + coal_out.x, Y + coal_out.y, coal_out.direction, 6)
    pole0 = next(e for e in keep if (e.extra or {}).get("role") == "field_pole")
    source(X + w + 3, Y + 2, X + pole0.x, Y + pole0.y)
    run(240)
    m = lua_json(f"""
local r = {{drills = {{}}, ins = {{}}, ore_lanes = lanes({X + ore_out.x}, {Y + ore_out.y}),
           head = lanes({X + 4}, {Y})}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "mining-drill" then
    r.drills[#r.drills + 1] = {{e.name, e.position.x - {X}, e.position.y - {Y}, ST[e.status] or "",
      e.burner and (e.burner.currently_burning ~= nil or not e.burner.inventory.is_empty()) or false}}
  elseif e.name == "burner-inserter" then
    r.ins[#r.ins + 1] = {{e.position.x - {X}, e.position.y - {Y}, ST[e.status] or "",
      e.burner.currently_burning ~= nil or not e.burner.inventory.is_empty()}}
  end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    left = {k: v for k, v in (m["ore_lanes"][0] or {}).items()} if m["ore_lanes"] else {}
    right = {k: v for k, v in (m["ore_lanes"][1] or {}).items()} if len(m["ore_lanes"]) > 1 else {}
    burners = [d for d in m["drills"] if d[0] == "burner-mining-drill"]
    elec = [d for d in m["drills"] if d[0] == "electric-mining-drill"]
    if len(burners) != 2 or any(d[3] not in ("working", "waiting_for_space_in_destination") or not d[4]
                                for d in burners):
        errs.append(f"E2: burner drills not running on corridor coal: {burners}")
    if any(not i[3] for i in m["ins"]):
        errs.append(f"E2: a burner inserter has no fuel: {m['ins']}")
    if len(elec) != 5 or any(d[3] not in ("working", "waiting_for_space_in_destination") for d in elec):
        errs.append(f"E6: electric drills not all powered/working: {elec}")
    if not right.get("iron-ore") or not left.get("iron-ore"):
        errs.append(f"E1: expected ore on both lanes (west electric left, east burner + electric right): "
                    f"left {left} right {right}")
    return errs, m


def case_field_lane(gd, X, Y):
    """E1 strict: only east slots (one burner, one electric): the output's left lane stays empty."""
    blk = B.mining_field(4, gd, resource="iron-ore")
    keep = [e for e in blk.entities if (e.extra or {}).get("role") in ("ore_belt", "corr", "field_pole") or
            ((e.extra or {}).get("role") in ("drill_b", "ins_b") and (e.extra or {}).get("unit") == 1) or
            ((e.extra or {}).get("role") == "drill_e" and (e.extra or {}).get("unit") == 3)]
    w, h = blk.size
    clear(X - 8, Y - 10, X + w + 12, Y + h + 8, "iron-ore", (X, Y, X + w - 1, Y + h))
    build(keep, X, Y, "lane")
    ore_out, coal_in, coal_out = blk.ports
    feed(X + coal_in.x, Y + coal_in.y, coal_in.direction, "coal")
    sink(X + ore_out.x, Y + ore_out.y, ore_out.direction, 6)
    sink(X + coal_out.x, Y + coal_out.y, coal_out.direction, 6)
    pole0 = next(e for e in keep if (e.extra or {}).get("role") == "field_pole")
    source(X + w + 3, Y + 2, X + pole0.x, Y + pole0.y)
    run(120)
    m = lua_json(f"""
local tot = {{{{}}, {{}}}}
for k = 0, 5 do
  local l = lanes({X + ore_out.x}, {Y + ore_out.y} - k)
  for li = 1, 2 do for n, c in pairs(l[li]) do tot[li][n] = (tot[li][n] or 0) + c end end
end
local st = {{}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, type = "mining-drill"}}) do
  st[#st + 1] = {{e.name, ST[e.status] or ""}}
end
rcon.print(helpers.table_to_json({{lanes = tot, drills = st}}))""")
    errs = []
    left, right = m["lanes"][0] or {}, m["lanes"][1] or {}
    if left.get("iron-ore"):
        errs.append(f"E1: ore on the LEFT lane from east slots: left {left} right {right}")
    if not right.get("iron-ore"):
        errs.append(f"E1: no ore on the right lane: {m}")
    return errs, m


def case_column(gd, X, Y, out_coal=1):
    """E3 + E4: a 2-pair column, burner in/out inserters (input ones empty, output ones with `out_coal` coal),
    buffer chests, ore and coal fed at the merge ports."""
    blk = B.smelter_column(2, gd, ore="iron-ore")
    keep = [e for e in blk.entities if (e.extra or {}).get("role") in ("furnace", "in_b", "out_b", "buffer", "mid",
                                                                         "merge")]
    w, h = blk.size
    clear(X - 6, Y - 6, X + w + 6, Y + h + 8)
    build(keep, X, Y, "column")
    ore, coal = blk.ports[2], blk.ports[3]
    feed(X + ore.x, Y + ore.y, ore.direction, "iron-ore")
    feed(X + coal.x, Y + coal.y, coal.direction, "coal")
    lua(f"""
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, name = "burner-inserter"}}) do
  local x = e.position.x - {X}
  if x < 2 or x > 9 then e.insert{{name = "coal", count = {out_coal}}} end
end
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, type = "furnace"}}) do
  e.insert{{name = "coal", count = 2}}
end
rcon.print("ok")""")
    run(600)
    m = lua_json(f"""
local r = {{furnaces = {{}}, chests = {{}}, mid = {{{{}}, {{}}}}, ins = {{}}}}
for y = 0, {h - 2} do
  local l = lanes({X + 5}, {Y} + y)
  for li = 1, 2 do for n, c in pairs(l[li]) do r.mid[li][n] = (r.mid[li][n] or 0) + c end end
end
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "furnace" then r.furnaces[#r.furnaces + 1] = {{e.products_finished, ST[e.status] or ""}}
  elseif e.type == "container" then r.chests[#r.chests + 1] = e.get_inventory(defines.inventory.chest).get_item_count("iron-plate")
  elseif e.name == "burner-inserter" then
    r.ins[#r.ins + 1] = {{e.position.x - {X}, e.position.y - {Y}, ST[e.status] or "",
      e.burner.currently_burning ~= nil or not e.burner.inventory.is_empty()}}
  end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    left, right = m["mid"][0] or {}, m["mid"][1] or {}
    if set(left) - {"iron-ore"} or set(right) - {"coal"} or not left or not right:
        errs.append(f"E3: middle belt lanes: left {left} right {right}")
    if len(m["furnaces"]) != 4 or any(fz[0] <= 0 for fz in m["furnaces"]):
        errs.append(f"E3: furnaces {m['furnaces']}")
    ins_in = [i for i in m["ins"] if 2 <= i[0] <= 9]
    if any(not i[3] for i in ins_in):
        errs.append(f"E3: an input burner inserter ran dry: {ins_in}")
    per_coal = (sum(m["chests"]) / max(1, len(m["chests"]))) / max(out_coal, 1)
    m["plates_per_coal"] = per_coal
    return errs, m


def case_swap_ghosts(gd, X, Y):
    """E5: ghosts over marked entities (manual_ghost)."""
    blk = B.mining_field(2, gd, resource="iron-ore")
    w, h = blk.size
    clear(X - 4, Y - 4, X + w + 4, Y + h + 4, "iron-ore", (X, Y, X + w, Y + h))
    db = next(e for e in blk.entities if (e.extra or {}).get("role") == "drill_b")
    ib = next(e for e in blk.entities if (e.extra or {}).get("role") == "ins_b")
    de = next(e for e in blk.entities if (e.extra or {}).get("role") == "drill_e" and (e.extra or {}).get("unit") == 1)
    m = lua_json(f"""
local d = mk("burner-mining-drill", {X + db.x}, {Y + db.y}, {db.direction})
local i = mk("burner-inserter", {X + ib.x}, {Y + ib.y}, {ib.direction})
local c = mk("wooden-chest", {X + 10.5}, {Y + 1.5})
local o = mk("burner-inserter", {X + 12.5}, {Y + 1.5}, 4)
local r = {{}}
r.before = s.can_place_entity{{name = "electric-mining-drill", position = {{{X + de.x}, {Y + de.y}}}, direction = {de.direction},
  force = f, build_check_type = defines.build_check_type.manual_ghost}}
d.order_deconstruction(f); i.order_deconstruction(f); c.order_deconstruction(f); o.order_deconstruction(f)
r.drill = s.can_place_entity{{name = "electric-mining-drill", position = {{{X + de.x}, {Y + de.y}}}, direction = {de.direction},
  force = f, build_check_type = defines.build_check_type.manual_ghost}}
r.belt = s.can_place_entity{{name = "transport-belt", position = {{{X + 10.5}, {Y + 1.5}}}, direction = 0, force = f,
  build_check_type = defines.build_check_type.manual_ghost}}
r.inserter = s.can_place_entity{{name = "inserter", position = {{{X + 12.5}, {Y + 1.5}}}, direction = 4, force = f,
  build_check_type = defines.build_check_type.manual_ghost}}
rcon.print(helpers.table_to_json(r))""")
    errs = []
    if m.get("before"):
        errs.append("E5: the electric drill fits even before the burner pair is marked (test geometry wrong?)")
    for k in ("drill", "belt", "inserter"):
        if not m.get(k):
            errs.append(f"E5: {k} ghost refused over marked entities")
    return errs, m


def case_power(gd, X, Y):
    """E7: two boiler units of power_block, everything empty except coal on the belt; water from an infinity pipe;
    a load on the pole."""
    blk = B.power_block(2, gd)
    w, h = blk.size
    clear(X - 8, Y - 6, X + w + 8, Y + h + 8)
    build(blk.entities, X, Y, "power")
    water, coal = blk.ports
    feed(X + coal.x, Y + coal.y, coal.direction, "coal")
    lua(f"""
local e = mk("infinity-pipe", {X + water.x} + .5, {Y + water.y} + .5)
e.set_infinity_pipe_filter{{name = "water", percentage = 1, temperature = 15}}
rcon.print("ok")""")
    pole = next(e for e in blk.entities if (e.extra or {}).get("role") == "power_pole")
    source(X + w + 3, Y - 4, X + pole.x, Y + pole.y, mode="load", kw=2000)
    run(180)
    m = lua_json(f"""
local r = {{engines = {{}}, boilers = {{}}, ins = {{}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "generator" then r.engines[#r.engines + 1] = e.energy_generated_last_tick * 60 / 1000
  elseif e.type == "boiler" then r.boilers[#r.boilers + 1] = ST[e.status] or ""
  elseif e.name == "burner-inserter" then r.ins[#r.ins + 1] = ST[e.status] or "" end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    if len(m["engines"]) != 4 or min(m["engines"]) <= 0:
        errs.append(f"E7: engines {m['engines']}")
    if any(b != "working" for b in m["boilers"]):
        errs.append(f"E7: boilers {m['boilers']}")
    return errs, m


def wire_all(x0, y0, x1, y1):
    """Connect every pair of small poles in the area within reach (test build: instant_build keeps no wires)."""
    lua(f"""
local ps = s.find_entities_filtered{{area = {{{{{x0}, {y0}}}, {{{x1}, {y1}}}}}, type = "electric-pole"}}
for i = 1, #ps do for j = i + 1, #ps do
  local a, b = ps[i], ps[j]
  local d = math.sqrt((a.position.x - b.position.x) ^ 2 + (a.position.y - b.position.y) ^ 2)
  if d <= 7.5 then
    a.get_wire_connector(defines.wire_connector_id.pole_copper, true).connect_to(
      b.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)
  end
end end
rcon.print("ok")""")


def case_column_electric(gd, X, Y):
    """E9: a 2-pair column in its electric phase (S2): electric input + output inserters on the burner ones' tiles,
    the three pole columns (out W, in C, out E) from one source; furnaces fed with ore + coal from the middle belt
    by the electric input inserters, plates into the buffer chests."""
    blk = B.smelter_column(2, gd, ore="iron-ore")
    keep = [e for e in blk.entities if (e.extra or {}).get("role") in ("furnace", "in_e", "out_e", "buffer", "mid",
                                                                         "merge", "col_pole")]
    w, h = blk.size
    clear(X - 6, Y - 6, X + w + 6, Y + h + 34)
    build(keep, X, Y, "colE")
    wire_all(X - 1, Y - 1, X + w + 1, Y + h + 1)
    ore, coal = blk.ports[2], blk.ports[3]
    reservoir(X + ore.x, Y + ore.y, ore.direction, "iron-ore", 30)
    feed(X + coal.x, Y + coal.y, coal.direction, "coal")
    pole = next(e for e in keep if (e.extra or {}).get("role") == "col_pole")
    source(X + w + 3, Y, X + pole.x, Y + pole.y)
    run(300)
    m = lua_json(f"""
local r = {{furnaces = {{}}, chests = {{}}, ins = {{}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "furnace" then r.furnaces[#r.furnaces + 1] = {{e.products_finished, ST[e.status] or "",
      e.get_fuel_inventory().get_item_count("coal")}}
  elseif e.type == "container" then r.chests[#r.chests + 1] = e.get_inventory(defines.inventory.chest).get_item_count("iron-plate")
  elseif e.name == "inserter" then r.ins[#r.ins + 1] = {{e.position.x - {X}, e.position.y - {Y}, ST[e.status] or ""}}
  end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    if len(m["furnaces"]) != 4 or any(fz[0] < 70 or fz[2] < 1 for fz in m["furnaces"]):
        errs.append(f"E9: furnaces (finished, status, coal) {m['furnaces']}")
    if len(m["ins"]) != 8 or any(i[2] in ("no_power", "low_power") for i in m["ins"]):
        errs.append(f"E9: electric inserters {m['ins']}")
    return errs, m


def case_green(gd, X, Y):
    """E8: the green block (science_block's green part) fed with iron and copper plates at its two ports, powered by
    the planner's own pole cover: 6 logistic-science-pack AM1 make 0.5 packs/s onto the spine's green lane, the
    red lane stays empty. Recipes enabled by a test cheat (the research is not what is tested here)."""
    sci = B.science_block(gd, n_red_res=8, labs_per_side=5)
    info = sci.info
    S, y0, Yg = info["S"], info["y0"], info["Yg"]
    roles = ("green_am", "green_out", "green_in", "prod_am", "prod_link", "prod_in", "prod_out", "mix_load", "mix",
             "mix_block", "feed", "feed_block", "feed_in")
    keep = [e for e in sci.entities if (e.extra or {}).get("role") in roles]
    keep += [e for e in sci.entities if (e.extra or {}).get("role") == "spine" and (e.extra or {}).get("row") >= y0 - 1]
    used = set()
    for e in keep:
        used |= gd.footprint(e.name, e.x, e.y, e.direction or 0)
    cands = [(x, y) for x in range(S + 1, S + 17) for y in range(y0, Yg + 5) if (x, y) not in used]
    needs = [e for e in keep if (gd.entity(e.name).get("energy_source") or {}).get("type") == "electric"]
    poles = B._cover(gd, "small-electric-pole", needs, cands)
    keep += poles
    lua("f.enable_all_recipes() rcon.print('ok')")
    w, h = sci.size
    clear(X - 6, Y - 40, X + w + 8, Y + h + 45)
    build(keep, X, Y, "green")
    wire_all(X, Y, X + w, Y + h)
    gfe, gcu = sci.ports[2], sci.ports[3]
    reservoir(X + gfe.x, Y + gfe.y, gfe.direction, "iron-plate", 40)
    reservoir(X + gcu.x, Y + gcu.y, gcu.direction, "copper-plate", 40)
    sink(X + S, Y + y0 - 2, 0, 30)
    DRAINS.append((X + S, Y + y0 - 12, 0, 20))     # the far part of the sink is emptied: labs never back up
    source(X + w + 3, Y + y0, X + poles[0].x, Y + poles[0].y)
    run(240)
    m0 = lua_json(f"""
local n = 0
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, name = "assembling-machine-1"}}) do
  local r = e.get_recipe()
  if r and r.name == "logistic-science-pack" then n = n + e.products_finished end
end
rcon.print(helpers.table_to_json({{n = n, tick = game.tick}}))""")
    run(600)
    m = lua_json(f"""
local r = {{ams = {{}}, n = 0, tick = game.tick, spine = {{{{}}, {{}}}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, name = "assembling-machine-1"}}) do
  local rc = e.get_recipe()
  r.ams[#r.ams + 1] = {{rc and rc.name or "", e.products_finished, ST[e.status] or ""}}
  if rc and rc.name == "logistic-science-pack" then r.n = r.n + e.products_finished end
end
for y = {Y + y0 - 11}, {Y + Yg - 2} do
  local l = lanes({X + S}, y)
  for li = 1, 2 do for n, c in pairs(l[li]) do r.spine[li][n] = (r.spine[li][n] or 0) + c end end
end
rcon.print(helpers.table_to_json(r))""")
    rate = (m["n"] - m0["n"]) / ((m["tick"] - m0["tick"]) / 60)
    m["green_per_s"] = round(rate, 3)
    errs = []
    if rate < 0.5 * 0.95:
        errs.append(f"E8: {rate:.3f} green/s (planned 0.5): {m['ams']}")
    lanes_ = [set(l) for l in m["spine"]]
    if not any(lanes_) or all(lanes_):
        errs.append(f"E8: green packs should sit on ONE spine lane: {m['spine']}")
    return errs, m


def case_coal_selffeed(gd, X, Y):
    """E10: a self-feeding coal field (2 burner slots) started the way the helper primes it (8 coal per drill, 1 per
    burner inserter), its output drained: it keeps itself running (20 game minutes)."""
    blk = B.mining_field(4, gd, resource="coal", self_feed=True)
    keep = [e for e in blk.entities if (e.extra or {}).get("role") in ("ore_belt", "feed", "corr") or
            ((e.extra or {}).get("role") in ("drill_b", "ins_b") and (e.extra or {}).get("unit") in (1, 3))]
    w, h = blk.size
    clear(X - 8, Y - 6, X + w + 12, Y + h + 30, "coal", (X, Y, X + w - 1, Y + h))
    build(keep, X, Y, "coalfield")
    tail = blk.ports[0]
    sink(X + tail.x, Y + tail.y, tail.direction, 20)
    DRAINS.append((X + tail.x, Y + tail.y + 10, tail.direction, 10))
    lua(f"""
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "mining-drill" then e.insert{{name = "coal", count = 8}}
  elseif e.name == "burner-inserter" then e.insert{{name = "coal", count = 1}} end
end
rcon.print("ok")""")
    run(1200)
    m = lua_json(f"""
local r = {{d = {{}}, i = {{}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "mining-drill" then r.d[#r.d + 1] = {{ST[e.status] or "?", e.burner.inventory.get_item_count()}}
  elseif e.name == "burner-inserter" then r.i[#r.i + 1] = {{ST[e.status] or "?", e.burner.inventory.get_item_count()}} end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    if len(m["d"]) != 2 or any(d[0] != "working" or d[1] < 1 for d in m["d"]):
        errs.append(f"E10: coal drills after 20 min {m['d']}")
    return errs, m


def case_mixed_chain(gd, X, Y):
    """E11 (round 3, the hub's chain): a mixed field (east slots 1 and 3 burner, started empty) gets coal at the
    tail of its corridor; its head merges the ore (left lane) and the coal that passed the corridor (right lane)
    onto one belt, which feeds a mixed-input column straight (2 pairs, burner input inserters started empty, burner
    output inserters with 5 coal, buffer chests). Expect: the trunk carries ore on one lane and coal on the other,
    the burner drills run on corridor coal, every furnace smelts with coal it got from the trunk."""
    fld = B.mining_field(4, gd, resource="iron-ore", mixed=True)
    col = B.smelter_column(2, gd, ore="iron-ore", mixed_in=True)
    fk = [e for e in fld.entities if (e.extra or {}).get("role") in ("ore_belt", "corr", "fmerge") or
          ((e.extra or {}).get("role") in ("drill_b", "ins_b") and (e.extra or {}).get("unit") in (1, 3))]
    ck = [e for e in col.entities if (e.extra or {}).get("role") in ("furnace", "in_b", "out_b", "buffer", "mid")]
    out, coal_in = fld.ports
    cin = col.ports[2]
    FY = Y + 12                                      # field origin; the column sits right above its out port
    gap = 2                                          # trunk tiles between the field's port and the column's port
    cx, cy = X + out.x - cin.x, FY + out.y - gap - cin.y
    fw, fh = fld.size
    clear(X - 8, cy - 6, X + fw + 12, FY + fh + 8, "iron-ore", (X, FY + 2, X + fw - 1, FY + fh))
    build(fk, X, FY, "mixfield")
    build(ck, cx, cy, "mixcol")
    trunk = [B.PE("transport-belt", X + out.x + 0.5, FY + out.y - i + 0.5, 0) for i in range(0, gap + 1)]
    build(trunk, 0, 0, "mixtrunk")
    feed(X + coal_in.x, FY + coal_in.y, coal_in.direction, "coal")
    lua(f"""
local cw, ch = {col.size[0]}, {col.size[1]}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{cx}, {cy}}}, {{{cx} + cw, {cy} + ch}}}}, force = f}}) do
  local x = e.position.x - {cx}
  if e.name == "burner-inserter" and (x < 2 or x > 9) then e.insert{{name = "coal", count = 5}} end
end
rcon.print("ok")""")
    run(600)
    m = lua_json(f"""
local r = {{trunk = {{{{}}, {{}}}}, furnaces = {{}}, drills = {{}}, ins = {{}}}}
for k = 0, {gap} do
  local l = lanes({X + out.x}, {FY + out.y} - k)
  for li = 1, 2 do for n, c in pairs(l[li]) do r.trunk[li][n] = (r.trunk[li][n] or 0) + c end end
end
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X - 2}, {cy - 2}}}, {{{X + fw + 2}, {FY + fh + 2}}}}}, force = f}}) do
  if e.type == "furnace" then r.furnaces[#r.furnaces + 1] = {{e.products_finished, ST[e.status] or "",
      e.get_fuel_inventory().get_item_count("coal")}}
  elseif e.type == "mining-drill" then r.drills[#r.drills + 1] = {{ST[e.status] or "",
      e.burner.currently_burning ~= nil or not e.burner.inventory.is_empty()}}
  elseif e.name == "burner-inserter" then r.ins[#r.ins + 1] = {{e.position.x, e.position.y, ST[e.status] or "",
      e.burner.currently_burning ~= nil or not e.burner.inventory.is_empty()}}
  end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    left, right = m["trunk"][0] or {}, m["trunk"][1] or {}
    if set(left) != {"iron-ore"} or set(right) != {"coal"}:
        errs.append(f"E11: trunk lanes should be ore | coal: left {left} right {right}")
    # 2 burner drills mine 0.5 ore/s (4 furnaces could smelt 1.25/s): the plates follow the ore, and every furnace
    # holds coal that came on the trunk
    made = sum(fz[0] for fz in m["furnaces"])
    m["plates_vs_ore"] = round(made / (0.5 * 600), 2)
    if len(m["furnaces"]) != 4 or made < 0.8 * 0.5 * 600 or any(fz[2] < 1 for fz in m["furnaces"]):
        errs.append(f"E11: furnaces (finished, status, coal) {m['furnaces']}")
    if len(m["drills"]) != 2 or any(not d[1] for d in m["drills"]):
        errs.append(f"E11: burner drills without corridor coal {m['drills']}")
    return errs, m


def case_split_branch(gd, X, Y):
    """E12 (round 3, on-site S5): a splitter placed the way _branch_green places it (one belt tile of the plate line
    becomes the splitter's left half, the lateral tile its right half) passes plates on along the line AND into the
    branch that starts in front of its other half. Recipes enabled by a test cheat (Logistics is not tested here)."""
    lua("f.enable_all_recipes() rcon.print('ok')")
    clear(X - 4, Y - 30, X + 10, Y + 40)
    tx, ty = X, Y                                     # the replaced tile; the line flows north
    line = [B.PE("transport-belt", tx + 0.5, ty + k + 0.5, 0) for k in range(1, 4)]
    line += [B.PE("transport-belt", tx + 0.5, ty - k + 0.5, 0) for k in range(1, 4)]
    lx = 1                                            # lateral to the east, as in _branch_green ((-vy, vx) for north)
    spl = B.PE("splitter", tx + 0.5 + lx * 0.5, ty + 0.5, 0)
    branch = [B.PE("transport-belt", tx + lx + 0.5, ty - k + 0.5, 0) for k in range(1, 4)]
    build(line + [spl] + branch, 0, 0, "split")
    reservoir(tx, ty + 3, 0, "iron-plate", 20)
    sink(tx, ty - 4, 0, 10)
    sink(tx + lx, ty - 4, 0, 10)
    DRAINS.append((tx, ty - 8, 0, 6))
    DRAINS.append((tx + lx, ty - 8, 0, 6))
    run(120)
    m = lua_json(f"""
local function cnt(x, y0, y1)
  local n = 0
  for y = y0, y1 do local l = lanes(x, y) for li = 1, 2 do for _, c in pairs(l[li]) do n = n + c end end end
  return n
end
rcon.print(helpers.table_to_json({{main = cnt({tx}, {ty - 7}, {ty - 1}), branch = cnt({tx + lx}, {ty - 7}, {ty - 1}),
  splitter = #s.find_entities_filtered{{name = "splitter", position = {{{spl.x}, {spl.y}}}}}}}))""")
    errs = []
    if m["splitter"] != 1 or m["main"] < 5 or m["branch"] < 5:
        errs.append(f"E12: splitter / plates on the line and the branch: {m}")
    return errs, m


CASES = {"field": case_field, "field_lane": case_field_lane, "column": case_column, "swap_ghosts": case_swap_ghosts,
         "power": case_power, "column_electric": case_column_electric, "green": case_green,
         "coal_selffeed": case_coal_selffeed, "mixed_chain": case_mixed_chain, "split_branch": case_split_branch}
SPOTS = {"field": (0, 140), "field_lane": (40, 140), "column": (80, 140), "swap_ghosts": (120, 140),
         "power": (0, 200), "column_electric": (80, 200), "green": (140, 220), "coal_selffeed": (200, 140),
         "mixed_chain": (240, 140), "split_branch": (280, 140)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=31)
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    assert 31 <= a.slot <= 39, "test slots 31-39 only"
    _w, gd = load_fixture(1001)
    if not a.no_restart:
        print(restart(a.slot)[-400:])
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    for _ in range(40):
        try:
            rcon.lua("rcon.print('ping')")
            break
        except Exception:
            time.sleep(1)
    rcon.lua("rcon.print('warm')")
    out = {}
    try:
        for name in a.cases or list(CASES):
            X, Y = SPOTS[name]
            try:
                errs, m = CASES[name](gd, X, Y)
            except Exception as ex:  # noqa: BLE001 - report and go on
                errs, m = [f"{name}: {ex}"], {}
            out[name] = {"ok": not errs, "errors": errs, "measured": m}
            print(f"{'PASS' if not errs else 'FAIL'} {name}: {errs or ''}")
            print("   ", json.dumps(m)[:600])
    finally:
        if not a.keep:
            restart(a.slot, stop=True)
    (ROOT / "testenv" / "inst" / "v3blocks_result.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0 if all(v["ok"] for v in out.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
