"""Live check of a planner v3 master plan, stage by stage, on a headless test slot (not collected by pytest).

    .venv\\Scripts\\python.exe tests\\live_master.py [--slot 32] [--seed 2002] [--variant hub] [--to 4] [--minutes 10]

TEST SETUP WITH CHEATS (this is not B2): every stage's ghosts are placed through the mod's place_ghosts and built
with the debug op instant_build; the entities a stage swaps out are destroyed first; research a stage's gate needs
is set researched by script; each new burner gets the planner's start fuel (5 coal, 20 per boiler) the way the
helper would put it in. Then the game runs `minutes` at game.speed 10 with nobody touching it (the helper is never
spawned: this measures whether the layout runs by itself, B3 offline) and the report compares what the machines did
with the master plan's stage_rates: plates/min per ore, red packs/min, power satisfaction, burners out of fuel.
Results: testenv/inst/v3master_<seed>_<variant>.json.
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
from factorio_claude.planner import master as M  # noqa: E402
from factorio_claude.planner.state import BaseState  # noqa: E402
from factorio_claude.planner.testing import load_fixture, load_state  # noqa: E402

PRE = r"""
local s, f = game.surfaces.nauvis, game.forces.player
local ST = {}
for k, v in pairs(defines.entity_status) do ST[v] = k end
"""


def lua(code):
    out = rcon.lua(PRE + code)
    if "Error" in out[:400] or "error" in out[:200]:
        raise RuntimeError(out[:3000])
    return out


def lua_json(code):
    return json.loads(lua(code).strip().splitlines()[-1])


def restart(slot, save, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", f"v3master{slot}", "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", save]
    log = ROOT / "testenv" / "inst" / f"v3master{slot}_restart.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=180)
    return log.read_text(errors="replace")


def research(techs):
    if techs:
        lua("for _, t in pairs({" + ",".join(f'"{t}"' for t in techs) + "}) do f.technologies[t].researched = true end "
            "rcon.print('ok')")


def remove(ents):
    if not ents:
        return
    rows = json.dumps([[e.name, e.x, e.y] for e in ents])
    lua(f"""
local n = 0
for _, r in pairs(helpers.json_to_table([==[{rows}]==])) do
  local e = s.find_entities_filtered{{name = r[1], position = {{r[2], r[3]}}, force = f}}[1]
  if e then e.destroy(); n = n + 1 end
end
rcon.print(n)""")


def build(ents, label):
    if not ents:
        return 0
    gh = []
    for e in ents:
        g = {"name": e.name, "x": e.x, "y": e.y, "direction": int(e.direction or 0)}
        if e.recipe:
            g["recipe"] = e.recipe
        if isinstance(e.extra, dict) and e.extra.get("type"):
            g["type"] = e.extra["type"]
        gh.append(g)
    failed = []
    for i in range(0, len(gh), 300):
        r = rcon.bridge("place_ghosts", ghosts=gh[i:i + 300], plan=f"v3m-{label}")
        failed += r.get("failed") or []
    xs = [g["x"] for g in gh]
    ys = [g["y"] for g in gh]
    built = 0
    x0, y0, x1, y1 = min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3
    for bx in range(int(x0), int(x1) + 1, 64):     # instant_build in tiles of the area (big plans)
        for by in range(int(y0), int(y1) + 1, 64):
            r2 = rcon.bridge("instant_build", area=[[bx, by], [min(bx + 64, x1), min(by + 64, y1)]])
            built += r2.get("built") or 0
    return {"ghosts": len(gh), "failed": failed[:8], "n_failed": len(failed), "built": built}


def start_fuel(ents):
    rows = json.dumps([[e.name, e.x, e.y] for e in ents])
    lua(f"""
local n = 0
for _, r in pairs(helpers.json_to_table([==[{rows}]==])) do
  local e = s.find_entities_filtered{{name = r[1], position = {{r[2], r[3]}}, force = f}}[1]
  if e and e.burner then e.insert{{name = "coal", count = (e.type == "boiler") and 20 or 5}}; n = n + 1 end
end
rcon.print(n)""")


def clear_obstacles(mp):
    """Trees / rocks under the plan (the helper would deconstruct them)."""
    rows =json.dumps([[e.name, e.x, e.y] for e in mp.ents])
    lua(f"""
local n = 0
for _, r in pairs(helpers.json_to_table([==[{rows}]==])) do
  for _, o in pairs(s.find_entities_filtered{{area = {{{{r[2] - 2, r[3] - 2}}, {{r[2] + 2, r[3] + 2}}}},
      type = {{"tree", "simple-entity", "fish"}}}}) do o.destroy(); n = n + 1 end
end
rcon.print(n)""")


def census(mp, k):
    """Counters of the machines of the base after stage k: furnace/AM products_finished, labs, burners without fuel,
    power."""
    rows = json.dumps([[e.name, e.x, e.y, (e.extra or {}).get("ore") or "", (e.extra or {}).get("role")]
                       for e in mp.ents if M.alive(e, k) and e.name in (
                           "stone-furnace", "assembling-machine-1", "lab", "boiler", "steam-engine",
                           "burner-mining-drill", "electric-mining-drill", "burner-inserter")])
    return lua_json(f"""
local r = {{iron = 0, copper = 0, red = 0, green = 0, gears = 0, nofuel = {{}}, engines_kw = 0, labs = 0,
           labs_working = 0,
           drills = {{}}, missing = 0, red_status = {{}}}}
for _, q in pairs(helpers.json_to_table([==[{rows}]==])) do
  local e = s.find_entities_filtered{{name = q[1], position = {{q[2], q[3]}}, force = f}}[1]
  if e then
    if e.type == "furnace" then
      if q[4] == "iron-ore" then r.iron = r.iron + e.products_finished else r.copper = r.copper + e.products_finished end
    elseif e.name == "assembling-machine-1" then
      local rc = e.get_recipe()
      if rc and rc.name == "automation-science-pack" then
        r.red = r.red + e.products_finished
        local k = ST[e.status] or "?"
        r.red_status[k] = (r.red_status[k] or 0) + 1
      elseif rc and rc.name == "logistic-science-pack" then r.green = r.green + e.products_finished
      elseif rc then r.gears = r.gears + e.products_finished end
    elseif e.type == "lab" then
      r.labs = r.labs + 1
      if e.status == defines.entity_status.working then r.labs_working = r.labs_working + 1 end
    elseif e.type == "generator" then r.engines_kw = r.engines_kw + e.energy_generated_last_tick * 60 / 1000
    elseif e.type == "mining-drill" then
      local k = e.name .. ":" .. (ST[e.status] or "?")
      r.drills[k] = (r.drills[k] or 0) + 1
    end
    if e.burner and not e.burner.currently_burning and e.burner.inventory.is_empty() and #r.nofuel < 30 then
      r.nofuel[#r.nofuel + 1] = e.name .. "@" .. e.position.x .. "," .. e.position.y
    end
  else
    r.missing = r.missing + 1
  end
end
r.tick = game.tick
rcon.print(helpers.table_to_json(r))""")


TICK_SINKS = r"""
-- the player's consumption, simulated: empty the plan's plate buffer chests; keep the labs researching red-only techs
for _, r in pairs(BUF) do
  local e = s.find_entities_filtered{name = "wooden-chest", position = {r[1], r[2]}, force = f}[1]
  if e then e.clear_items_inside() end
end
if not f.current_research then
  local q = {}
  for name, t in pairs(f.technologies) do
    if not t.researched and t.enabled and #q < 5 then
      local ok = true
      for _, p in pairs(t.prerequisites) do if not p.researched then ok = false end end
      local n, green = 0, false
      for _, ing in pairs(t.research_unit_ingredients) do
        n = n + 1
        if ing.name == "logistic-science-pack" then green = true end
        if ing.name ~= "automation-science-pack" and not (GREEN and ing.name == "logistic-science-pack") then
          ok = false
        end
      end
      if n == 0 then ok = false end   -- trigger techs
      -- measuring green: only red + green techs, so the labs take both packs alike (a red-only tech in between
      -- leaves the green packs waiting and the green assemblers stop with full outputs)
      if GREEN and not green then ok = false end
      if ok then q[#q + 1] = name end
    end
  end
  f.research_queue = q
end
rcon.print(game.tick)
"""


def run(minutes, buffers=(), speed=10, green=False):
    lua(f"game.speed = {speed} rcon.print('ok')")
    tick0 = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
    target = tick0 + int(minutes * 3600)
    t_end = time.monotonic() + minutes * 60 / speed * 3 + 30
    buf = ("local BUF = helpers.json_to_table([==[" + json.dumps([list(b) for b in buffers] or [[0, 0]]) + "]==])\n"
           + f"local GREEN = {'true' if green else 'false'}\n")
    while time.monotonic() < t_end:
        time.sleep(2.0)
        if int(lua(buf + TICK_SINKS).strip().splitlines()[-1]) >= target:
            break
    lua("game.speed = 1 rcon.print('ok')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=32)
    ap.add_argument("--seed", type=int, default=2002)
    ap.add_argument("--variant", default="hub")
    ap.add_argument("--to", type=float, default=4)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--private", help="PRIVATE real-save copy: capture folder under testenv/private (map/, data.json, "
                                      "state.json) and the save name in testenv/write (results stay gitignored)")
    ap.add_argument("--save", default="priv_real_1029.zip")
    a = ap.parse_args()
    assert 31 <= a.slot <= 39, "test slots 31-39 only"
    if a.private:
        from factorio_claude.planner.gamedata import GameData
        from factorio_claude.planner.world import World
        cap = ROOT / "testenv" / "private" / a.private
        w, gd, st = World.load(cap / "map"), GameData.load(cap / "data.json"), BaseState.load(cap / "state.json")
        save, a.seed = a.save, "real"
    else:
        w, gd = load_fixture(a.seed)
        st = load_state(a.seed) or BaseState.fresh_start()
        save = f"seed_{a.seed}.zip"
    mp = M.design_master(w, gd, st, {"near": st.pos or (0, 0)}, a.variant)
    print("master", mp.id, "stages", [M.slabel(s.index) for s in mp.stages])
    print(restart(a.slot, save)[-300:])
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    for _ in range(40):
        try:
            rcon.lua("rcon.print('ping')")
            break
        except Exception:
            time.sleep(1)
    rcon.lua("rcon.print('warm')")
    report = {"master": mp.id, "seed": a.seed, "variant": a.variant, "cheats": "test setup: instant_build, research "
              "set by script, start fuel inserted; nobody touches the base while it runs", "stages": {}}
    try:
        clear_obstacles(mp)
        for s in mp.stages:
            k = s.index
            if k > a.to:
                break
            research(s.gate)
            remove(M.removed_in(mp, k))
            new = M.new_in(mp, k)
            b = build(new, M.slabel(k))
            start_fuel(new)
            report["stages"][M.slabel(k)] = {"build": b}
            print(M.slabel(k), b)
        # something for the labs to research with the assembler-made packs
        lua("""
local q = {}
for _, t in pairs({"electric-mining-drill", "logistics", "logistic-science-pack", "turrets", "military",
                   "stone-wall", "radar", "lamp", "repair-pack", "steel-processing", "fast-inserter",
                   "weapon-shooting-speed-1", "physical-projectile-damage-1"}) do
  local tt = f.technologies[t]
  if tt and not tt.researched and #q < 7 then q[#q + 1] = t end
end
f.research_queue = q
rcon.print(#f.research_queue)""")
        # run with nobody touching it: two halves, to see whether the rates hold
        last = max(s.index for s in mp.stages if s.index <= a.to)
        bufs = [(e.x, e.y) for e in mp.ents if M.alive(e, last) and (e.extra or {}).get("role") == "buffer"]
        run(1, bufs)                          # warm-up: belts fill, assemblers start
        c0 = census(mp, last)
        run(a.minutes / 2, bufs)
        c1 = census(mp, last)
        run(a.minutes / 2, bufs)
        c2 = census(mp, last)
        want = M.stage_rates(mp, gd, last)
        dt1 = (c1["tick"] - c0["tick"]) / 3600
        dt2 = (c2["tick"] - c1["tick"]) / 3600
        got = {}
        for key, item in (("iron", "iron-plate"), ("copper", "copper-plate"), ("red", "automation-science-pack")):
            got[item] = [round((c1[key] - c0[key]) / max(dt1, 1e-9), 1), round((c2[key] - c1[key]) / max(dt2, 1e-9), 1)]
        report["after"] = M.slabel(last)
        report["planned_per_min"] = want
        report["measured_per_min"] = got
        report["census_end"] = c2
        report["power"] = M.power_budget(mp, gd, last)
        print(json.dumps({k: report[k] for k in ("after", "planned_per_min", "measured_per_min")}))
        print("end:", json.dumps({k: c2[k] for k in ("nofuel", "engines_kw", "labs", "labs_working", "drills",
                                                     "missing", "red_status")}))
    finally:
        out = ROOT / "testenv" / "inst" / f"v3master_{a.seed}_{a.variant}.json"
        out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        if not a.keep:
            restart(a.slot, "", stop=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
