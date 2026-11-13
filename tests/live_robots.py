"""Engine checks for the robot network (roadmap G11: planner/blocks.py robot_block, planner/expand.py roboports,
mod/claude-bridge/scripts/agent.lua build_ghosts, autopilot.lua observe) on a headless test server. Not collected by
pytest: it needs Factorio.

    .venv\\Scripts\\python.exe tests\\live_robots.py --factorio <factorio.exe> [--udp-port 34536] [--rcon-port 27036]
        [--game-port 34436] [--keep] [case ...]

Starts its own server like tests/live_defense.py (temp write-data, a copy of testenv/write/seed_1001.zip, the mod from
mod/claude-bridge with its test ops on), researches what the cases need (test setup), then:

  RB1 robot_block   blocks.robot_block built through place_ghosts + instant_build on cleared ground, plates on
                    reservoir belts at its four ports, water and crude oil from infinity pipes, power from an
                    electric-energy-interface; at game.speed 10: the refinery runs advanced oil processing (heavy oil
                    to the lubricant plant, petroleum to the sulfur plant, light oil into the tank), every machine
                    finishes crafts, each of the four belts carries one item per lane, and the product chests get
                    construction robots, logistic robots, roboports, storage and passive provider chests
  RB2 prototype     2.1's roboport: logistic radius 25 (50x50), construction radius 55 (110x110), what
                    planner/expand.py assumes when the data export has no radius fields; the techs and recipes the
                    roadmap names exist (robotics, construction-robotics, logistic-robotics, battery, lubricant, ...)
  RB3 robots_build  a roboport with construction robots and a storage chest holding the items: the helper's
                    build_ghosts leaves the plan's ghosts inside the construction area to the robots (they build
                    them) and builds the one outside it itself; autopilot_observe reports the network (roboports,
                    robots, ghosts the robots cover)
"""
import argparse
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("live_defense", ROOT / "tests" / "live_defense.py")
LD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LD)
_spec = importlib.util.spec_from_file_location("v3b", ROOT / "tests" / "live_v3_blocks.py")
V3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V3)
lua, lua_json, clear, build, source, reservoir, run, wire_all = (V3.lua, V3.lua_json, V3.clear, V3.build, V3.source,
                                                                 V3.reservoir, V3.run, V3.wire_all)
call = LD.call

from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner import expand as X  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402

RESEARCH = ("automation", "logistics", "steam-power", "electronics", "electric-mining-drill", "steel-processing",
            "fast-inserter", "fluid-handling", "oil-gathering", "oil-processing", "plastics", "automation-2", "engine",
            "advanced-circuit", "sulfur-processing", "chemical-science-pack", "battery", "advanced-oil-processing",
            "lubricant", "electric-engine", "robotics", "construction-robotics", "logistic-robotics",
            "logistic-science-pack", "automation-science-pack")


def setup():
    lua("for _, t in ipairs({" + ",".join(f'"{t}"' for t in RESEARCH) + "}) do f.technologies[t].researched = true "
        "end rcon.print('ok')")


def chest_contents(x0, y0, x1, y1, types=("container",)):
    return lua_json(f"""
local r = {{}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{x0}, {y0}}}, {{{x1}, {y1}}}}}, type = {{"{'", "'.join(types)}"}}}}) do
  local c = {{}}
  for _, it in pairs(e.get_inventory(defines.inventory.chest).get_contents()) do c[it.name] = it.count end
  r[#r + 1] = {{e.position.x, e.position.y, c}}
end
rcon.print(helpers.table_to_json(r))""")


def lane_items(x0, x1, y):
    return lua_json(f"""
local tot = {{{{}}, {{}}}}
for x = {x0}, {x1} do
  local l = lanes(x, {y})
  for li = 1, 2 do for n, c in pairs(l[li]) do tot[li][n] = (tot[li][n] or 0) + c end end
end
rcon.print(helpers.table_to_json(tot))""")


# ---- cases ---------------------------------------------------------------------------------------------------------
def case_robot_block(gd, errs, m):
    """RB1"""
    X0, Y0 = 200, 200
    blk = B.robot_block(gd)
    w, h = blk.size
    clear(X0 - 30, Y0 - 6, X0 + w + 8, Y0 + h + 6)
    build(blk.entities, X0, Y0, "robots")
    wire_all(X0 - 1, Y0 - 1, X0 + w + 1, Y0 + h + 1)
    for e in blk.entities:          # the inventory bars the mod sets when the helper builds a mall chest
        bar = (e.extra or {}).get("bar")
        if bar and e.extra.get("role") == "mall_chest":
            lua(f"""local c = s.find_entities_filtered{{position = {{{X0 + e.x}, {Y0 + e.y}}}, radius = 0.3,
type = "container"}}[1] c.get_inventory(defines.inventory.chest).set_bar({int(bar) + 1}) rcon.print("ok")""")
    ports = {p.item: p for p in blk.ports}
    for item in ("iron-plate", "steel-plate", "copper-plate", "plastic-bar"):
        p = ports[item]
        reservoir(X0 + p.x, Y0 + p.y, p.direction, item, 24)
    for fluid in ("water", "crude-oil"):
        p = ports[fluid]
        lua(f"""
local e = mk("infinity-pipe", {X0 + p.x} + .5, {Y0 + p.y} + .5)
e.set_infinity_pipe_filter{{name = "{fluid}", percentage = 1}}
rcon.print("ok")""")
    pole = next(e for e in blk.entities if (e.extra or {}).get("role") == "robot_pole")
    source(X0 - 4, Y0 + 12, X0 + pole.x, Y0 + pole.y)
    probe = f"""
local r = {{machines = {{}}, fluids = {{}}}}
for _, e in pairs(s.find_entities_filtered{{area = {{{{{X0}, {Y0}}}, {{{X0 + w}, {Y0 + h}}}}}, force = f}}) do
  if e.type == "assembling-machine" then
    local rc = e.get_recipe()
    r.machines[#r.machines + 1] = {{e.position.x - {X0}, e.position.y - {Y0}, rc and rc.name or "", ST[e.status] or "",
      e.products_finished}}
  elseif e.type == "storage-tank" then
    local ok, fl = pcall(function() return e.get_fluid_contents() end)
    r.fluids[#r.fluids + 1] = ok and fl or {{}}
  end
end
rcon.print(helpers.table_to_json(r))"""
    run(300)                          # warm-up: circuits, engines, the refinery's first cycles, acid, batteries
    m0 = lua_json(probe)
    run(420)
    m1 = lua_json(probe)
    m["machines"] = [x for x in m1["machines"]]
    m["tank"] = m1["fluids"]
    idle = [x for x in m1["machines"] if x[4] == 0]
    # (logistic robots come last: the construction robot machine is first on the frames lane and the roboport machine
    # before it on the advanced circuit lane - checked below once their chests are full)
    if [x for x in idle if x[2] != "logistic-robot"]:
        errs.append(f"RB1: machines that never finished a craft: {idle}")
    by = {}
    for x in m1["machines"]:
        by[x[2]] = by.get(x[2], 0) + x[4]
    m["crafts"] = by
    if not by.get("advanced-oil-processing") or not by.get("lubricant") or not by.get("electric-engine-unit"):
        errs.append(f"RB1: oil chain did not run: {by}")
    frames = by.get("flying-robot-frame", 0) - sum(x[4] for x in m0["machines"] if x[2] == "flying-robot-frame")
    m["frames_per_min"] = round(frames / 7.0, 2)
    if not any((f or {}).get("light-oil") for f in m1["fluids"]):
        errs.append(f"RB1: no light oil in the tank: {m1['fluids']}")
    got = {}
    for _x, _y, c in chest_contents(X0, Y0, X0 + w, Y0 + h):
        for k, v in (c or {}).items():
            got[k] = got.get(k, 0) + v
    m["chests"] = got
    for item in ("construction-robot", "storage-chest", "passive-provider-chest", "roboport"):
        if not got.get(item):
            errs.append(f"RB1: no {item} in the product chests")
    # construction first: with the construction robot, roboport and chest machines saturated (test setup: their chests
    # and their own output slots - a machine goes on crafting until that holds a full stack - filled by script) the
    # frames and advanced circuits go on to the logistic robot machine
    first = ("construction-robot", "roboport", "storage-chest", "passive-provider-chest")
    for e in blk.entities:
        role, unit = (e.extra or {}).get("role"), (e.extra or {}).get("unit")
        if role == "mall_chest" and unit in first:
            lua(f"""local c = s.find_entities_filtered{{position = {{{X0 + e.x}, {Y0 + e.y}}}, radius = 0.3,
type = "container"}}[1] c.insert{{name = "{unit}", count = 100}} rcon.print("ok")""")
        elif e.recipe in first:
            lua(f"""local a = s.find_entities_filtered{{position = {{{X0 + e.x}, {Y0 + e.y}}}, radius = 0.3,
type = "assembling-machine"}}[1] a.get_output_inventory().insert{{name = "{e.recipe}", count = 100}} rcon.print("ok")""")
    run(300)
    got2 = {}
    for _x, _y, c in chest_contents(X0, Y0, X0 + w, Y0 + h):
        for k, v in (c or {}).items():
            got2[k] = got2.get(k, 0) + v
    m["chests_after"] = got2
    if not got2.get("logistic-robot"):
        errs.append(f"RB1: no logistic robots once the construction robot and roboport chests were full: {got2}")
    # lanes: each belt (east-flowing: lane 1 = left = north, lane 2 = right = south) one item per lane
    want = {16: ("flying-robot-frame", "steel-plate"), 17: ("advanced-circuit", "iron-plate"),
            23: ("copper-plate", "electronic-circuit"), 24: ("plastic-bar", "engine-unit")}
    m["lanes"] = {}
    for row, (north, south) in want.items():
        ln = lane_items(X0 + 4, X0 + 78, Y0 + row)
        m["lanes"][row] = ln
        n, s_ = ln[0] or {}, ln[1] or {}
        if set(n) - {north} or set(s_) - {south}:
            errs.append(f"RB1: belt row {row} lanes mixed (north {n}, south {s_})")


def case_prototype(gd, errs, m):
    """RB2"""
    p = lua_json("""
local e = prototypes.entity.roboport
local function get(k) local ok, v = pcall(function() return e[k] end) return ok and v or nil end
local function q(k) local ok, v = pcall(function() return e[k](e, "normal") end) if ok then return v end
  ok, v = pcall(function() return e[k]("normal") end) return ok and v or nil end
local techs = {}
for _, t in ipairs({"robotics", "construction-robotics", "logistic-robotics", "battery", "lubricant",
    "electric-engine", "advanced-oil-processing", "logistic-system"}) do techs[t] = f.technologies[t] ~= nil end
rcon.print(helpers.table_to_json({logistic = get("logistic_radius") or q("get_logistic_radius"),
  construction = get("construction_radius") or q("get_construction_radius"), techs = techs,
  robot_speed = prototypes.entity["construction-robot"].speed}))""")
    m["prototype"] = p
    if (p.get("logistic"), p.get("construction")) != (X.ROBOPORT_LOGISTIC_R, X.ROBOPORT_CONSTRUCTION_R):
        errs.append(f"RB2: roboport radii differ from planner/expand.py's defaults: {p}")
    if not all(p["techs"].values()):
        errs.append(f"RB2: techs missing: {p['techs']}")
    # the data export carries the radii now (planner/expand.py roboport_radii reads them from there)
    from factorio_claude.planner.gamedata import GameData
    res = call("export_data")
    gd2 = GameData.load(m["_write"] / "script-output" / res.get("path", "claude/data.json"))
    m["export"] = {k: gd2.entity("roboport").get(k) for k in ("logistic_radius", "construction_radius")}
    if X.roboport_radii(gd2) != (p.get("logistic"), p.get("construction")) or None in m["export"].values():
        errs.append(f"RB2: the data export's roboport radii: {m['export']}")


def mod_lua(code):
    """Lua in the mod's own state (its storage): test setup and read-back."""
    from factorio_claude import rcon
    out = rcon.client().command("/silent-command __claude-bridge__ " + code)
    if "Error" in out[:400] or "error" in out[:200]:
        raise RuntimeError(out[:2000])
    return out


def robots_build(gd, errs, m):
    """RB3"""
    X0, Y0 = -300, 200
    clear(X0 - 12, Y0 - 70, X0 + 130, Y0 + 70)
    lua(f"""
local eei = mk("electric-energy-interface", {X0} - 6, {Y0} + 2)
eei.electric_buffer_size = 1e10 eei.power_production = 1e9 / 60 eei.power_usage = 0
mk("medium-electric-pole", {X0} - 3.5, {Y0} + 2.5)
local rp = mk("roboport", {X0} + 2, {Y0} + 2)
rp.insert{{name = "construction-robot", count = 20}}
local st = mk("storage-chest", {X0} + 6.5, {Y0} + .5)
st.insert{{name = "transport-belt", count = 50}} st.insert{{name = "inserter", count = 10}}
st.insert{{name = "passive-provider-chest", count = 5}}
rcon.print("ok")""")
    wire_all(X0 - 10, Y0 - 5, X0 + 10, Y0 + 10)
    call("agent_spawn")
    lua(f"""
for _, c in pairs(s.find_entities_filtered{{type = "character"}}) do
  c.teleport({{{X0} + 12, {Y0} + 14}}) c.insert{{name = "wooden-chest", count = 2}}
end
rcon.print("ok")""")
    inside = [{"name": "transport-belt", "x": X0 + 10.5 + i, "y": Y0 + 10.5, "direction": 4} for i in range(10)]
    inside.append({"name": "inserter", "x": X0 + 12.5, "y": Y0 + 11.5, "direction": 0})
    outside = [{"name": "wooden-chest", "x": X0 + 100.5, "y": Y0 + 10.5, "direction": 0}]
    from factorio_claude import rcon
    rcon.bridge("place_ghosts", ghosts=inside + outside, plan="rb3")
    obs = call("autopilot_observe")
    m["robots_before"] = obs.get("robots")
    call("agent_tasks", tasks=[{"type": "build_ghosts", "x": X0 + 50, "y": Y0 + 10, "radius": 70, "plan": "rb3"}],
         replace=True)
    run(90)
    st = lua_json(f"""
local g = s.find_entities_filtered{{area = {{{{{X0}, {Y0}}}, {{{X0 + 110}, {Y0 + 20}}}}}, type = "entity-ghost"}}
local names = {{}}
for _, x in pairs(g) do names[#names + 1] = x.ghost_name end
local ch = s.find_entities_filtered{{name = "wooden-chest", position = {{{X0} + 100.5, {Y0} + 10.5}}}}[1]
local sc = s.find_entities_filtered{{name = "storage-chest", position = {{{X0} + 6.5, {Y0} + .5}}}}[1]
rcon.print(helpers.table_to_json({{ghosts = names, chest = ch ~= nil,
  belts_stored = sc.get_inventory(defines.inventory.chest).get_item_count("transport-belt"),
  belts_built = #s.find_entities_filtered{{area = {{{{{X0}, {Y0} + 10}}, {{{X0} + 22, {Y0} + 11}}}}, name = "transport-belt"}}}}))""")
    m["rb3"] = st
    log = call("agent_status").get("log") or []
    m["helper_log"] = [x.get("msg") if isinstance(x, dict) else x for x in log][-8:]
    if st["ghosts"]:
        errs.append(f"RB3: ghosts left: {st['ghosts']}")
    if st["belts_built"] != 10 or st["belts_stored"] != 40:
        errs.append(f"RB3: the robots did not build the belts from the storage chest: {st}")
    if not st["chest"]:
        errs.append("RB3: the helper did not build the ghost outside the construction area")
    if not any("to the construction robots" in str(x) for x in m["helper_log"]):
        errs.append(f"RB3: the helper's log does not say it left ghosts to the robots: {m['helper_log']}")
    rb = (m["robots_before"] or {})
    if rb.get("roboports") != 1 or rb.get("construction") != 20 or rb.get("networks") != 1 or rb.get("storage") != 1:
        errs.append(f"RB3: observe robots {rb}")
    # logistic chests: a mall chest inside the logistic area (registered by script: test setup) is marked for upgrade
    # and the robots swap it for a passive provider from the storage chest, keeping its contents; it is registered
    # again as a mall chest with its inventory bar
    lua(f"""local c = mk("iron-chest", {X0} + 9.5, {Y0} + 4.5) c.insert{{name = "iron-plate", count = 30}}
c.get_inventory(defines.inventory.chest).set_bar(6) rcon.print("ok")""")
    mod_lua(f"""local c = game.surfaces.nauvis.find_entities_filtered{{name = "iron-chest", position = {{{X0} + 9.5,
{Y0} + 4.5}}}}[1] storage.autopilot.mall_chests[c.unit_number] = {{c.position.x, c.position.y, "nauvis"}}
storage.autopilot.owned[c.unit_number] = "rb3mall"
rcon.print("ok")""")
    m["upgradable"] = (call("autopilot_observe").get("robots") or {}).get("upgradable")
    m["logistic_chests"] = call("autopilot_logistic_chests")
    run(60)
    up = lua_json(f"""
local c = s.find_entities_filtered{{position = {{{X0} + 9.5, {Y0} + 4.5}}, type = {{"container", "logistic-container"}}}}[1]
local inv = c.get_inventory(defines.inventory.chest)
rcon.print(helpers.table_to_json({{name = c.name, unit = c.unit_number, plates = inv.get_item_count("iron-plate"),
  bar = inv.get_bar()}}))""")
    reg = mod_lua(f"""local n, mine = 0, false
for u, at in pairs(storage.autopilot.mall_chests) do n = n + 1 if u == {up['unit']} then mine = true end end
local owned = 0
for u, pid in pairs(storage.autopilot.owned) do if pid == "rb3mall" then owned = owned + (u == {up['unit']} and 1 or 100) end end
rcon.print(helpers.table_to_json({{n = n, mine = mine, owned = owned}}))""")
    up["registered"] = json.loads(reg.strip().splitlines()[-1])
    m["upgrade"] = up
    if m["upgradable"] != 1 or (m["logistic_chests"] or {}).get("ordered") != 1:
        errs.append(f"RB3: upgrade not ordered: upgradable {m['upgradable']}, {m['logistic_chests']}")
    if up["name"] != "passive-provider-chest" or up["plates"] != 30 or up["bar"] != 6:
        errs.append(f"RB3: the mall chest was not swapped for a passive provider keeping its contents: {up}")
    if not up["registered"]["mine"] or up["registered"]["n"] != 1:
        errs.append(f"RB3: the passive provider is not the registered mall chest: {up['registered']}")
    if up["registered"].get("owned") != 1:      # (the plan's ownership moved to the new unit, none left on the old)
        errs.append(f"RB3: the passive provider did not take over the plan ownership: {up['registered']}")
    # observe's plan rows count the ghosts the robots will build (R6 leaves those to them): a plan with 3 inserters
    # inside the construction area (the storage chest has inserters; ~50 tiles out, so the robots are still on the way
    # when observe runs) and a wooden chest outside it
    ents = [["inserter", X0 + 50.5, Y0 + 30.5 + i, 0] for i in range(3)] + [["wooden-chest", X0 + 100.5, Y0 + 14.5, 0]]
    call("plan_preview", id="rb3plan", entities=ents, owner="player", title="rb3")
    call("plan_apply", id="rb3plan")
    row = None
    for _ in range(40):
        row = next((p for p in call("autopilot_observe").get("plans") or [] if p.get("id") == "rb3plan"), None)
        if row and row.get("status") == "applied" and row.get("ghosts"):
            break
        time.sleep(0.25)
    m["plan_row"] = row
    if not row or row.get("ghosts") != 4 or row.get("robot_ghosts") != 3:
        errs.append(f"RB3: observe's plan row does not count the robots' ghosts: {row}")


def seed(gd, errs, m):
    """RB4: R12's seed action as the executor queues it (autopilot.py do_seed_robots: acquire + insert): the helper
    takes construction robots out of a mall chest (registered by script: test setup) and puts them into a roboport."""
    X0, Y0 = -300, -200
    clear(X0 - 12, Y0 - 20, X0 + 40, Y0 + 20)
    call("agent_spawn")
    lua(f"""
local eei = mk("electric-energy-interface", {X0} - 6, {Y0} + 2)
eei.electric_buffer_size = 1e10 eei.power_production = 1e9 / 60 eei.power_usage = 0
mk("medium-electric-pole", {X0} - 3.5, {Y0} + 2.5)
mk("roboport", {X0} + 2, {Y0} + 2)
local c = mk("iron-chest", {X0} + 20.5, {Y0} + 8.5) c.insert{{name = "construction-robot", count = 30}}
for _, ch in pairs(s.find_entities_filtered{{type = "character"}}) do ch.teleport({{{X0} + 10, {Y0} + 10}}) end
rcon.print("ok")""")
    wire_all(X0 - 10, Y0 - 5, X0 + 10, Y0 + 10)
    mod_lua(f"""local c = game.surfaces.nauvis.find_entities_filtered{{name = "iron-chest", position = {{{X0} + 20.5,
{Y0} + 8.5}}}}[1] storage.autopilot.mall_chests[c.unit_number] = {{c.position.x, c.position.y, "nauvis"}}
rcon.print("ok")""")
    from factorio_claude import autopilot as AP
    from factorio_claude import autopilot_rules as R
    ex = AP.Executor(call, None)
    a = R.Action("seed_robots", "R12", None, "", {"item": "construction-robot", "count": 30, "x": X0 + 2, "y": Y0 + 2,
                                                  "into": "roboport"})
    count = f"""
local r = s.find_entities_filtered{{name = "roboport", position = {{{X0} + 2, {Y0} + 2}}}}[1]
local c = s.find_entities_filtered{{name = "iron-chest", position = {{{X0} + 20.5, {Y0} + 8.5}}}}[1]
rcon.print(helpers.table_to_json({{robots = r.logistic_network.all_construction_robots,
  in_chest = c.get_inventory(defines.inventory.chest).get_item_count("construction-robot")}}))"""
    before = lua_json(count)
    m["seed"] = ex.run(a, R.Snapshot(helper={"alive": True}))
    run(40)
    got = lua_json(count)
    got["robots"] -= before["robots"]          # (the ones it brought)
    m["seed_after"] = got
    if got["robots"] < 30 or got["in_chest"] != 0:      # (RB3's robots on the same map may join it)
        errs.append(f"RB4: the helper did not bring the mall chest's 30 robots into the roboport: {got}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factorio", required=True, type=Path)
    ap.add_argument("--udp-port", type=int, default=34536)
    ap.add_argument("--rcon-port", type=int, default=27036)
    ap.add_argument("--game-port", type=int, default=34436)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    assert a.udp_port not in (34500,) and a.rcon_port not in (27015,), "never the player's game"
    _w, gd = load_fixture(1001, cached=False)
    for r in gd.raw["recipes"].values():
        r["enabled"] = True
    proc, work, log = LD.start(a.factorio, a.udp_port, a.rcon_port, a.game_port, debug_ops=True)
    m = {"_write": work / "write"}
    errs = []
    cases = {"prototype": case_prototype, "robots_build": robots_build, "seed": seed, "robot_block": case_robot_block}
    try:
        setup()
        for name in a.cases or list(cases):
            before = len(errs)
            try:
                cases[name](gd, errs, m)
            except Exception as ex:  # noqa: BLE001 - report and go on
                errs.append(f"{name}: {ex}")
            print(f"{'PASS' if len(errs) == before else 'FAIL'} {name}: {errs[before:] or ''}")
    finally:
        if not a.keep:
            proc.kill()
            time.sleep(1)
            shutil.rmtree(work, ignore_errors=True)
    m.pop("_write", None)
    print(json.dumps(m, default=str)[:6000])
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
