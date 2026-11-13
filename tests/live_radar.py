"""Engine checks for the radar network (R11; planner/expand.py radar, autopilot.lua chart count + survey, defense.lua
nests around radars) on a headless test server. Not collected by pytest: it needs Factorio.

    .venv\\Scripts\\python.exe tests\\live_radar.py --factorio <factorio.exe> [--udp-port 34534] [--rcon-port 27034]
        [--game-port 34434] [--keep]

Starts its own server like tests/live_defense.py (temp write-data, a copy of testenv/write/seed_1001.zip, the mod from
mod/claude-bridge; peaceful mode so nothing attacks), then:

  R1 prototype      2.1's radar: the recipe is locked behind the Radar tech (prerequisite red science), 300 kW, keeps
                    3 chunks around its own charted (7x7), scans 14 out (quality getters: the plain prototype keys
                    throw in 2.1): what planner/expand.py assumes when the data export has no radar fields
  R2 survey         autopilot_chart_survey: 8 octants whose totals cover the ring; autopilot_observe has "chart"
                    (chunks, radars)
  R3 radar_works    a radar 600 tiles out (test setup: pole + electric-energy-interface, created by script) works and
                    the survey and observe list it
  R4 nests          a spawner 70 tiles beyond the radar (more than 400 tiles from spawn): defense_threats did not list
                    it before the radar, lists it after (with "charted", the radar as the base point it would attack)
  R5 rechart        force.chart twice over the same area: the charted count grows once (on_chunk_charted fires on
                    re-charts too); when the server charts nothing, the mod's on_chunk_charted handler is called
                    in its Lua state with 2 new chunks and 1 re-chart instead
A headless server charts nothing (no player: force.chart and radars leave every chunk uncharted - why the planner
tests export with FACTORIO_PLAN_UNCHARTED), so the charted COUNT growing (on_chunk_charted) cannot be checked here; it
is unit-tested on the Python side only (tests/test_autopilot.py recharted).
"""
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("live_defense", ROOT / "tests" / "live_defense.py")
LD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LD)
lua, lua_json, call = LD.lua, LD.lua_json, LD.call

RX, RY = 608.5, 16.5           # the radar (far outside seed 1001's start area)
NEST_DX = 70


def run_ticks(n):
    """Let the game run n ticks (the server runs at speed 1: wait for game.tick to pass)."""
    t0 = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
    end = time.monotonic() + n / 60 * 3 + 30
    while time.monotonic() < end:
        if int(lua("rcon.print(game.tick)").strip().splitlines()[-1]) >= t0 + n:
            return
        time.sleep(0.5)
    raise RuntimeError(f"the game did not run {n} ticks")


def r1(errs, m):
    p = lua_json("""
local e, r, t = prototypes.entity.radar, f.recipes.radar, f.technologies.radar
local pre = {}
for name in pairs(t.prerequisites) do pre[#pre + 1] = name end
local unlocks = false
for _, x in pairs(t.prototype.effects) do if x.type == "unlock-recipe" and x.recipe == "radar" then unlocks = true end end
rcon.print(helpers.table_to_json({near = e.get_max_distance_of_nearby_sector_revealed("normal"),
  scan = e.get_max_distance_of_sector_revealed("normal"), kw = e.get_max_energy_usage() * 60 / 1000,
  enabled = r.enabled, researched = t.researched, pre = pre, unlocks = unlocks}))""")
    m["prototype"] = p
    from factorio_claude.planner import expand as E
    if p["enabled"] or p["researched"] or not p["unlocks"] or p["pre"] != ["automation-science-pack"]:
        errs.append(f"r1: radar recipe/tech not as assumed: {p}")
    if (p["near"], p["scan"]) != (E.RADAR_NEARBY_CHUNKS, E.RADAR_SCAN_CHUNKS) or abs(p["kw"] - E.RADAR_KW) > 1:
        errs.append(f"r1: radar reach/power differ from planner/expand.py's defaults: {p}")
    # the data export carries them now (planner/expand.py radar_reach reads them from there)
    from factorio_claude.planner.gamedata import GameData
    res = call("export_data")
    gd = GameData.load(m["_write"] / "script-output" / res.get("path", "claude/data.json"))
    m["export"] = {k: gd.entity("radar").get(k) for k in ("max_distance_of_sector_revealed",
                                                          "max_distance_of_nearby_sector_revealed")}
    if E.radar_reach(gd) != (p["near"], p["scan"]) or None in m["export"].values():
        errs.append(f"r1: the data export's radar fields: {m['export']}")


def r2(errs, m):
    sv = call("autopilot_chart_survey", x=0, y=0, r0=0, r1=4)
    tot = sum(o["total"] for o in sv["octants"])
    m["survey0"] = sv["octants"]
    if len(sv["octants"]) != 8 or tot != 9 * 9 - 1 or sorted(o["dir"] for o in sv["octants"]) != list(range(8)):
        errs.append(f"r2: survey covers {tot} chunks in {len(sv['octants'])} octants, want 80 in 8")
    # the game's octants are the planner's (expand.octant: 0 east, clockwise): the same chunks in each
    from factorio_claude.planner import expand as E
    want = [0] * 8
    for dx in range(-4, 5):
        for dy in range(-4, 5):
            if dx or dy:
                want[E.octant(dx, dy)] += 1
    got = [o["total"] for o in sorted(sv["octants"], key=lambda o: o["dir"])]
    if got != want:
        errs.append(f"r2: octant totals {got}, the planner's octant() gives {want}")
    ob = call("autopilot_observe").get("chart")
    m["observe_chart"] = ob
    if not isinstance(ob, dict) or "chunks" not in ob or "radars" not in ob:
        errs.append(f"r2: autopilot_observe has no chart summary: {ob}")


def threats_far():
    return [t for t in call("defense_threats", radius=400) if isinstance(t, dict) and t.get("x", 0) > RX]


def r3_r4(errs, m):
    lua(f"""
s.request_to_generate_chunks({{{RX}, {RY}}}, 5)
s.force_generate_chunk_requests()
clear({int(RX) - 20}, {int(RY) - 20}, {int(RX) + NEST_DX + 20}, {int(RY) + 20})
s.create_entity{{name = "biter-spawner", position = {{{RX + NEST_DX}, {RY}}}, force = "enemy"}}
rcon.print("ok")""")
    before = threats_far()
    m["threats_before"] = before[:3]
    if before:
        errs.append(f"r4: nests 470+ tiles out were listed before any radar: {before[:3]}")
    lua(f"""
s.create_entity{{name = "radar", position = {{{RX}, {RY}}}, force = f}}
s.create_entity{{name = "small-electric-pole", position = {{{RX} - 2, {RY}}}, force = f}}
local eei = s.create_entity{{name = "electric-energy-interface", position = {{{RX} - 4.5, {RY} - 0.5}}, force = f}}
eei.power_production = 1e6
eei.electric_buffer_size = 1e7
rcon.print("ok")""")
    run_ticks(120)
    st = lua_json(f"""
local r = s.find_entities_filtered{{name = "radar", position = {{{RX}, {RY}}}, radius = 1}}[1]
local name
for k, v in pairs(defines.entity_status) do if r and v == r.status then name = k end end
rcon.print(helpers.table_to_json({{status = name or "none", working = r and r.status == defines.entity_status.working}}))""")
    sv = call("autopilot_chart_survey", x=0, y=0, r0=10, r1=24)
    m["radar"] = {"status": st, "survey_radars": sv.get("radars"), "observe": call("autopilot_observe")["chart"]}
    if not st.get("working"):
        errs.append(f"r3: the radar is not working: {st}")
    if not any(abs(r["x"] - RX) < 1 and abs(r["y"] - RY) < 1 and r.get("working") for r in sv.get("radars") or []):
        errs.append(f"r3: the survey does not list the working radar: {sv.get('radars')}")
    if m["radar"]["observe"].get("radars") != 1:
        errs.append(f"r3: observe counts {m['radar']['observe'].get('radars')} radars, want 1")
    after = threats_far()
    m["threats_after"] = after[:3]
    ours = [t for t in after if abs(t["x"] - (RX + NEST_DX)) < 3 and abs(t["y"] - RY) < 3]
    if len(ours) != 1 or "charted" not in ours[0] or not ours[0].get("base") or \
            abs(ours[0]["base"]["x"] - RX) > 4:
        errs.append(f"r4: the nest beyond the radar is not listed with the radar as its base point: {after[:3]}")


def r5(errs, m):
    """Review: on_chunk_charted also fires on a RE-chart; the count must grow only for chunks not charted before.
    force.chart twice over the same (generated) area: the count grows once. Skipped (noted) when the server charts
    nothing at all."""
    area = f"{{{{{RX - 80}, {RY - 80}}}, {{{RX + 80}, {RY + 80}}}}}"
    n0 = call("autopilot_observe")["chart"]["chunks"]
    lua(f"f.chart(s, {area}) rcon.print('ok')")
    run_ticks(120)
    n1 = call("autopilot_observe")["chart"]["chunks"]
    real = int(lua_json(f"""
local n = 0
for cx = {int((RX - 80) // 32)}, {int((RX + 80) // 32)} do
  for cy = {int((RY - 80) // 32)}, {int((RY + 80) // 32)} do if f.is_chunk_charted(s, {{cx, cy}}) then n = n + 1 end end
end
rcon.print(n)"""))
    lua(f"f.chart(s, {area}) rcon.print('ok')")
    run_ticks(120)
    n2 = call("autopilot_observe")["chart"]["chunks"]
    m["rechart"] = {"before": n0, "after_chart": n1, "after_rechart": n2, "charted_in_area": real}
    if real == 0:
        # the server charts nothing: call the mod's own on_chunk_charted handler (in its Lua state) with a chunk
        # twice and another once, as the engine would on a chart and a re-chart
        from factorio_claude import rcon
        rcon.client().command("/silent-command __claude-bridge__ "
                              "local h = script.get_event_handler(defines.events.on_chunk_charted) "
                              "local f = game.forces.player "
                              "for _, x in ipairs({900, 900, 901}) do h({name = defines.events.on_chunk_charted, "
                              "tick = game.tick, force = f, surface_index = game.surfaces.nauvis.index, "
                              "position = {x = x, y = 900}, area = {{0, 0}, {0, 0}}}) end rcon.print('ok')")
        n3 = call("autopilot_observe")["chart"]["chunks"]
        m["rechart"]["handler_called"] = n3
        if n3 != n2 + 2:
            errs.append(f"r5: the handler counted {n3 - n2} chunks for 2 new + 1 re-charted: {m['rechart']}")
        return
    if n1 <= n0 or n2 != n1:
        errs.append(f"r5: charted count {n0} -> {n1} -> {n2} (re-chart must not count): {m['rechart']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factorio", type=Path, default=Path("factorio"))
    ap.add_argument("--udp-port", type=int, default=34534)
    ap.add_argument("--rcon-port", type=int, default=27034)
    ap.add_argument("--game-port", type=int, default=34434)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    assert a.udp_port != 34500 and a.rcon_port != 27015, "never the player's game ports"
    proc, work, log = LD.start(a.factorio, a.udp_port, a.rcon_port, a.game_port)
    errs, m = [], {"_write": work / "write"}
    try:
        lua("s.peaceful_mode = true game.map_settings.enemy_expansion.enabled = false rcon.print('ok')")
        for case in (r1, r2, r3_r4, r5):
            n = len(errs)
            try:
                case(errs, m)
            except Exception as ex:  # noqa: BLE001 - report and go on
                errs.append(f"{case.__name__}: {ex}")
            print(f"{'PASS' if len(errs) == n else 'FAIL'} {case.__name__}: {errs[n:] or ''}")
        print(json.dumps({k: v for k, v in m.items() if k != "_write"}, indent=1)[:4000])
        bad = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if "claude-bridge" in ln and ("rror" in ln)]
        if bad:
            errs.append("server log: " + "; ".join(bad[:3]))
    finally:
        if not a.keep:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(work, ignore_errors=True)
    print("PASS" if not errs else f"FAIL: {errs}")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
