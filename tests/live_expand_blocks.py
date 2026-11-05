"""Engine checks for the autopilot's expansion blocks (docs/design/AUTOPILOT.md 8, planner/expand.py) on a headless
test slot. Not collected by pytest: it needs a server.

    .venv\\Scripts\\python.exe tests\\live_expand_blocks.py [--slot 31] [--keep] [case ...]

Restarts test slot <slot> (instance "expblocks", seed_1001.zip, debug ops on), researches what the blocks need (test
setup), builds each case through place_ghosts + instant_build on cleared ground with infinity-fed belts and an
electric-energy-interface, runs at game.speed 10 and reads the entities back:

  X1 mall          blocks.mall_block fed with iron and copper plates: every output chest gets its item (gears,
                   circuits, belts, inserters, splitters, undergrounds, pipes, steam engines, electric drills,
                   assemblers), belt A carries iron on its north lane and gears on its south lane only and BACKS UP
                   (no depot drains it since round 3: the gear machine stops when its lane is full instead of turning
                   the mall's iron into gears), the belt machine makes >= 40 belts/min, nothing jams
  X2 cell_column   the merged electric smelting column (expand._column_part): ore + coal at the merge, every furnace
                   smelts, the single output belt carries plates on BOTH lanes
  X3 bus_taps      the plate bus with a 2-inserter iron tap and a long-handed copper tap reaching over the iron
                   belt: each tap's belt carries only its own plate
  X4 oil_plant     blocks.oil_plant_block with crude from an infinity pipe at its crude port and coal on its belt:
                   the refinery runs basic oil processing (crude in its 2nd input, petroleum out of its 3rd output),
                   the chemical plant makes plastic into the depot chest
  X5 oil_trigger   a powered pumpjack on a crude-oil well (Oil gathering researched) finishes the Oil processing
                   trigger tech
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factorio_claude import rcon  # noqa: E402
from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner import expand as E  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402

_spec = importlib.util.spec_from_file_location("v3b", ROOT / "tests" / "live_v3_blocks.py")
V3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V3)
lua, lua_json, clear, build, source, reservoir, sink, run, wire_all = (V3.lua, V3.lua_json, V3.clear, V3.build,
                                                                      V3.source, V3.reservoir, V3.sink, V3.run,
                                                                      V3.wire_all)
NAME = "expblocks"


def restart(slot, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", NAME, "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", "seed_1001.zip"]
    log = ROOT / "testenv" / "inst" / f"{NAME}_restart.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=180)
    return log.read_text(errors="replace")


RESEARCH = ("automation", "logistics", "steam-power", "electronics", "electric-mining-drill", "steel-processing",
            "fast-inserter", "fluid-handling", "oil-gathering", "oil-processing", "plastics")


def setup():
    lua("for _, t in ipairs({" + ",".join(f'"{t}"' for t in RESEARCH) + "}) do f.technologies[t].researched = true "
        "end rcon.print('ok')")


def chest_contents(x0, y0, x1, y1):
    return lua_json(f"""
local r = {{}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{x0}, {y0}}}, {{{x1}, {y1}}}}}, type = "container"}}) do
  local c = {{}}
  for _, it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) do c[it.name] = it.count end
  r[#r + 1] = {{e.position.x, e.position.y, c}}
end
rcon.print(helpers.table_to_json(r))""")


def lane_totals(x0, x1, y):
    return lua_json(f"""
local tot = {{{{}}, {{}}}}
for x = {x0}, {x1} do
  local l = lanes(x, {y})
  for li = 1, 2 do for n, c in pairs(l[li]) do tot[li][n] = (tot[li][n] or 0) + c end end
end
rcon.print(helpers.table_to_json(tot))""")


# ---- cases ---------------------------------------------------------------------------------------------------------
def case_mall(gd, X, Y):
    blk = B.mall_block(gd, None)
    w, h = blk.size
    clear(X - 6, Y - 6, X + w + 8, Y + h + 10)
    build(blk.entities, X, Y, "mall")
    wire_all(X - 1, Y - 1, X + w + 1, Y + h + 1)
    for e in blk.entities:          # the inventory bars the mod sets when the helper builds a mall chest
        bar = (e.extra or {}).get("bar")
        if bar:
            lua(f"""local c = s.find_entities_filtered{{position = {{{X + e.x}, {Y + e.y}}}, radius = 0.3,
type = "container"}}[1] c.get_inventory(defines.inventory.chest).set_bar({int(bar) + 1}) rcon.print("ok")""")
    iron, cop = blk.ports
    reservoir(X + iron.x, Y + iron.y, iron.direction, "iron-plate", 20)
    reservoir(X + cop.x, Y + cop.y, cop.direction, "copper-plate", 20)
    pole = next(e for e in blk.entities if (e.extra or {}).get("role") == "mall_pole")
    source(X + 2, Y + h + 4, X + pole.x, Y + pole.y)
    run(1800)      # the belt-first slots fill their chests before the drill/assembler slots downstream get circuits
    chests = chest_contents(X, Y - 1, X + w, Y + h)
    lanes_a = lane_totals(X + 12, X + blk.info["x_end"], Y + 6)
    st = lua_json(f"""
local r = {{}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, type = "assembling-machine"}}) do
  local rc = e.get_recipe()
  r[#r + 1] = {{rc and rc.name or "", ST[e.status] or "", e.products_finished}}
end
rcon.print(helpers.table_to_json(r))""")
    got = {}
    for _x, _y, c in chests:
        for k, v in (c or {}).items():
            got[k] = got.get(k, 0) + v
    errs = []
    for item in blk.info["items"] + ["iron-gear-wheel", "electronic-circuit"]:
        if not got.get(item):
            errs.append(f"X1: no {item} in the mall chests")
    north, south = lanes_a[0] or {}, lanes_a[1] or {}
    if north.get("iron-gear-wheel"):
        errs.append(f"X1: gears on belt A's north (iron) lane: {north}")
    if south.get("iron-plate"):
        errs.append(f"X1: iron on belt A's south (gear) lane: {south}")
    if not north.get("iron-plate") or not south.get("iron-gear-wheel"):
        errs.append(f"X1: belt A is not backed up with iron (north) and gears (south): {lanes_a}")
    belts_made = 2 * sum(m[2] for m in st if m[0] == "transport-belt")
    if got.get("transport-belt", 0) < 400 or belts_made < 600:   # its 4-stack chest full (and the neighbours fed)
        errs.append(f"X1: the belt machine made {belts_made} belts, {got.get('transport-belt')} in its chest")
    idle = [m for m in st if m[2] == 0]
    if idle:
        errs.append(f"X1: machines that never finished a craft: {idle}")
    return errs, {"chests": got, "lanes_a": lanes_a, "machines": st}


def case_cell_column(gd, X, Y):
    col = E._column_part(gd, "iron-ore", 3)
    w, h = col.size
    clear(X - 6, Y - 6, X + w + 6, Y + h + 34)
    build(col.ents, X, Y, "cellcol")
    wire_all(X - 1, Y - 1, X + w + 1, Y + h + 1)
    ore, coal, plate = col.ports["ore"], col.ports["coal"], col.ports["plate"]
    reservoir(X + ore.x, Y + ore.y, ore.direction, "iron-ore", 30)
    reservoir(X + coal.x, Y + coal.y, coal.direction, "coal", 30)
    sink(X + plate.x, Y + plate.y, plate.direction, 4)
    pole = next(e for e in col.ents if (e.extra or {}).get("role") == "col_pole")
    source(X + w + 3, Y + 4, X + pole.x, Y + pole.y)
    run(300)
    out = lua_json(f"""
local tot = {{{{}}, {{}}}}
for k = 0, 3 do
  local l = lanes({X + plate.x}, {Y + plate.y} - k)
  for li = 1, 2 do for n, c in pairs(l[li]) do tot[li][n] = (tot[li][n] or 0) + c end end
end
local fz = {{}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, type = "furnace"}}) do
  fz[#fz + 1] = {{e.products_finished, ST[e.status] or ""}}
end
rcon.print(helpers.table_to_json({{lanes = tot, furnaces = fz}}))""")
    errs = []
    if len(out["furnaces"]) != 6 or any(f[0] < 30 for f in out["furnaces"]):
        errs.append(f"X2: furnaces {out['furnaces']}")
    lanes_ = out["lanes"]
    if not (lanes_[0] or {}).get("iron-plate") or not (lanes_[1] or {}).get("iron-plate"):
        errs.append(f"X2: plates not on both lanes of the merged output: {lanes_}")
    if any(k != "iron-plate" for ln in lanes_ for k in (ln or {})):
        errs.append(f"X2: something else on the plate belt: {lanes_}")
    return errs, out


def case_bus_taps(gd, X, Y):
    """A 20-tile bus flowing east (iron row Y, copper row Y+1) fed by reservoirs; a 2-inserter iron tap and a
    long-handed copper tap from the north side, each dropping onto a short belt flowing north to a sink."""
    from factorio_claude.planner.blocks import PE
    ents = [PE("transport-belt", X + x + 0.5, Y + 0.5, 4) for x in range(20)] + \
        [PE("transport-belt", X + x + 0.5, Y + 1.5, 4) for x in range(20)]
    # iron tap at x 5, 6: inserters on row -1 picking south, drops on row -2
    ents += [PE("inserter", X + 5.5, Y - 0.5, 8), PE("inserter", X + 6.5, Y - 0.5, 8)]
    ents += [PE("transport-belt", X + 5.5, Y - 1.5, 4), PE("transport-belt", X + 6.5, Y - 1.5, 0)]
    ents += [PE("transport-belt", X + 6.5, Y - 2.5 - k, 0) for k in range(4)]
    # copper tap at x 12: a long-handed inserter on row -1 picks row 1 (over the iron belt), drops on row -3
    ents += [PE("long-handed-inserter", X + 12.5, Y - 0.5, 8)]
    ents += [PE("transport-belt", X + 12.5, Y - 2.5 - k, 0) for k in range(5)]
    ents += [PE("small-electric-pole", X + 4.5, Y - 0.5, 0), PE("small-electric-pole", X + 11.5, Y - 0.5, 0)]
    clear(X - 26, Y - 12, X + 30, Y + 8)
    build(ents, 0, 0, "bus")
    wire_all(X - 1, Y - 2, X + 21, Y + 2)
    reservoir(X - 1, Y, 4, "iron-plate", 20)
    reservoir(X - 1, Y + 1, 4, "copper-plate", 20)
    lua(f"mk('infinity-chest', {X} + 20.5, {Y} + 0.5) mk('infinity-chest', {X} + 20.5, {Y} + 1.5) rcon.print('ok')")
    source(X + 2, Y - 6, X + 4.5, Y - 0.5)
    run(120)
    m = lua_json(f"""
local function belt_items(x, y0, n)
  local tot = {{}}
  for k = 0, n - 1 do
    local l = lanes(x, y0 - k)
    for li = 1, 2 do for nm, c in pairs(l[li]) do tot[nm] = (tot[nm] or 0) + c end end
  end
  return tot
end
rcon.print(helpers.table_to_json({{iron = belt_items({X} + 6, {Y} - 2, 5), copper = belt_items({X} + 12, {Y} - 3, 5)}}))
""")
    errs = []
    if not (m["iron"] or {}).get("iron-plate") or set(m["iron"] or {}) - {"iron-plate"}:
        errs.append(f"X3: iron tap belt carries {m['iron']}")
    if not (m["copper"] or {}).get("copper-plate") or set(m["copper"] or {}) - {"copper-plate"}:
        errs.append(f"X3: copper tap belt carries {m['copper']}")
    return errs, m


def case_oil_plant(gd, X, Y):
    blk = B.oil_plant_block(gd)
    w, h = blk.size
    clear(X - 6, Y - 6, X + w + 8, Y + h + 8)
    build(blk.entities, X, Y, "oilplant")
    wire_all(X - 1, Y - 1, X + w + 1, Y + h + 1)
    coal, crude = blk.ports
    reservoir(X + coal.x, Y + coal.y, coal.direction, "coal", 20)
    lua(f"""
local e = mk("infinity-pipe", {X + crude.x} + .5, {Y + crude.y} + .5)
e.set_infinity_pipe_filter{{name = "crude-oil", percentage = 1}}
rcon.print("ok")""")
    pole = next(e for e in blk.entities if (e.extra or {}).get("role") == "oil_pole")
    source(X + w + 3, Y + 1, X + pole.x, Y + pole.y)
    run(300)
    m = lua_json(f"""
local r = {{machines = {{}}, chest = {{}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X}, {Y}}}, {{{X + w}, {Y + h}}}}}, force = f}}) do
  if e.type == "assembling-machine" then
    local rc = e.get_recipe()
    r.machines[#r.machines + 1] = {{e.name, rc and rc.name or "", ST[e.status] or "", e.products_finished}}
  elseif e.type == "container" then
    for _, it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) do r.chest[it.name] = it.count end
  end
end
rcon.print(helpers.table_to_json(r))""")
    errs = []
    ref = [x for x in m["machines"] if x[0] == "oil-refinery"]
    chem = [x for x in m["machines"] if x[0] == "chemical-plant"]
    if not ref or ref[0][3] < 10:
        errs.append(f"X4: refinery {ref}")
    if not chem or chem[0][3] < 10:
        errs.append(f"X4: chemical plant {chem}")
    if (m["chest"] or {}).get("plastic-bar", 0) < 20:
        errs.append(f"X4: plastic in the depot: {m['chest']}")
    return errs, m


def case_oil_trigger(gd, X, Y):
    clear(X - 6, Y - 6, X + 10, Y + 10)
    lua(f"""
f.technologies["oil-processing"].researched = false
s.create_entity{{name = "crude-oil", position = {{{X} + .5, {Y} + .5}}, amount = 300000}}
local pj = mk("pumpjack", {X} + .5, {Y} + .5, 0)
rcon.print("ok")""")
    lua(f"""
local eei = mk("electric-energy-interface", {X} + 4, {Y} + 2)
eei.electric_buffer_size = 1e9 eei.power_production = 1e8 / 60 eei.power_usage = 0
mk("small-electric-pole", {X} + 2.5, {Y} + 2.5)
rcon.print("ok")""")
    run(60)
    m = lua_json(f"""
local pj = s.find_entities_filtered{{name = "pumpjack", position = {{{X} + .5, {Y} + .5}}}}[1]
rcon.print(helpers.table_to_json({{researched = f.technologies["oil-processing"].researched,
  status = pj and ST[pj.status] or "", crude = pj and pj.get_fluid_count("crude-oil") or 0}}))""")
    errs = []
    if not m["researched"]:
        errs.append(f"X5: Oil processing not researched after the pumpjack ran: {m}")
    return errs, m


CASES = {"mall": case_mall, "cell_column": case_cell_column, "bus_taps": case_bus_taps, "oil_plant": case_oil_plant,
         "oil_trigger": case_oil_trigger}
SPOTS = {"mall": (40, 60), "cell_column": (120, 60), "bus_taps": (60, 110), "oil_plant": (140, 110),
         "oil_trigger": (110, 140)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=31)
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    assert 31 <= a.slot <= 39, "test slots 31-39 only"
    _w, gd = load_fixture(1001)
    for t in RESEARCH:                 # the blocks are made for a force that has these (validator research checks)
        gd.raw["technologies"][t]["researched"] = True
    if not a.no_restart:
        print(restart(a.slot)[-400:])
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    for _ in range(40):
        try:
            rcon.lua("rcon.print('ping')")
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    rcon.lua("rcon.print('warm')")
    setup()
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
            print("   ", json.dumps(m)[:900])
    finally:
        if not a.keep:
            restart(a.slot, stop=True)
    (ROOT / "testenv" / "inst" / "expblocks_result.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0 if all(v["ok"] for v in out.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
