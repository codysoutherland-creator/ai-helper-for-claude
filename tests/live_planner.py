"""In-engine starter-base check on a headless test slot (not collected by pytest: needs a server).

    .venv\\Scripts\\python.exe tests\\live_planner.py [--seed 1001] [--stage bootstrap|electric] [--variant compact]
                                                     [--minutes 12] [--slot 9] [--name planner]

Restarts the slot from testenv/write/seed_<n>.zip and then goes through the real pipeline over UDP: the server.py
planner tool (map export + game-data export + plan_variants), plan_preview, plan_apply (tagged ghosts). Then it
builds the ghosts (revive = cheat, test only), clears the marked trees, puts coal into what the plan marks as fuelled by
hand (and the boilers), runs the game fast and compares measured production (last 5 min) with the plan:
every product within 10 % of the plan, every furnace working, no drill out of fuel/power/ore (backed up is
fine: mines are rounded up to whole drills), boilers and engines working in the electric stage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def restart(name, slot, save=None, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", name, "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", save]
    with tempfile.TemporaryFile() as f:  # never pipe: the started factorio.exe would keep the pipe open
        subprocess.run(args, stdout=f, stderr=f, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1001)
    ap.add_argument("--stage", default="bootstrap")
    ap.add_argument("--variant", default="compact")
    ap.add_argument("--minutes", type=float, default=12)
    ap.add_argument("--slot", type=int, default=9)
    ap.add_argument("--name", default="planner")
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    a = ap.parse_args()

    # this process only ever talks to the test slot
    os.environ.update({"FACTORIO_UDP_PORT": str(34500 + a.slot), "FACTORIO_RCON_PORT": str(27016 + a.slot),
                       "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1",
                       "FACTORIO_PLAN_UNCHARTED": "1",
                       "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / a.name / "write" / "script-output")})
    sys.path.insert(0, str(ROOT))
    from factorio_claude import link, rcon, server
    from factorio_claude.planner.plan import Plan

    print(f"== seed {a.seed} {a.variant} {a.stage}: restarting slot {a.slot}")
    restart(a.name, a.slot, f"seed_{a.seed}.zip")
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")

    def lua(code):
        r = rcon._client.command("/silent-command " + code)
        if r and r.startswith("Cannot execute command"):
            raise RuntimeError(r)
        return r

    lua("local s=game.surfaces.nauvis; s.request_to_generate_chunks({0,0}, 8); s.force_generate_chunk_requests()")
    # biters would eat the polluting drills during the run: peaceful + no nests (the planner only warns about them)
    lua("local s=game.surfaces.nauvis; s.peaceful_mode=true; for _, e in pairs(s.find_entities_filtered{force='enemy'})"
        " do e.destroy() end")
    if a.stage == "electric":  # a force that has the electric starter tech (the plan must not need assume_research)
        lua("for _, n in pairs{'automation-science-pack','electronics','automation','steam-power',"
            "'electric-mining-drill','logistics'} do local t=game.forces.player.technologies[n]; "
            "if t then t.researched=true end end")
    server.PLANS = Path(tempfile.mkdtemp(prefix="plans_"))
    t0 = time.time()
    out = json.loads(server.plan_starter_base("", variants=1 if a.variant == "compact" else 3, stage=a.stage))
    if "plans" not in out:
        print("!! planning failed:", out)
        return 1
    print(f"   planned in {time.time() - t0:.1f} s (incl. exports): {[p['id'] for p in out['plans']]}")
    brief = next((p for p in out["plans"] if p["variant"] == a.variant), out["plans"][0])
    plan = Plan.load(server.PLANS / f"{brief['id']}.json")
    print("  ", plan.summary)
    for p in out["plans"]:
        if p.get("preview_error"):
            print("!! preview:", p["preview_error"])
            return 1
    applied = json.loads(server.plan_apply(plan.id))
    print("   plan_apply:", json.dumps(applied)[:300])
    r = applied.get("applied") or {}
    deadline = time.time() + 30
    while r.get("pending") and time.time() < deadline:
        time.sleep(0.5)
        r = link.call("plan_get", id=plan.id).get("result") or {}
    ok = r.get("placed", 0) + r.get("existing", 0) == len(plan.entities) and not r.get("blocked")
    print(f"   ghosts placed {r.get('placed')} / {len(plan.entities)}, blocked {r.get('blocked')}")
    tagged = json.loads(lua("""
local n, other = 0, 0
for _, g in pairs(game.surfaces.nauvis.find_entities_filtered{type='entity-ghost'}) do
  if g.tags and g.tags.claude_plan == %s then n = n + 1 else other = other + 1 end
end
rcon.print(helpers.table_to_json{n, other})""" % json.dumps(plan.id)))
    print(f"   ghosts tagged with the plan id: {tagged[0]} (untagged {tagged[1]})")
    ok &= tagged[0] == len(plan.entities)

    # build (test cheat): remove marked trees/rocks, revive ghosts, coal where the plan says "by hand" + boilers
    built = json.loads(lua("""
local s = game.surfaces.nauvis
local d = 0
for _, e in pairs(s.find_entities_filtered{to_be_deconstructed=true}) do e.destroy(); d = d + 1 end
local n, fail = 0, 0
for _, g in pairs(s.find_entities_filtered{type='entity-ghost'}) do
  local ok = g.revive()
  if ok then n = n + 1 else fail = fail + 1 end
end
rcon.print(helpers.table_to_json{d, n, fail})"""))
    print(f"   cleared {built[0]}, built {built[1]}, revive failed {built[2]}")
    ok &= built[2] == 0
    manual = [[e.x, e.y, e.name] for e in plan.entities if (e.extra or {}).get("fuel") == "manual"
              or e.name == "boiler"]
    lua("""
local s = game.surfaces.nauvis
for _, m in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entity(m[3], {m[1], m[2]})
  if e then e.insert{name='coal', count=(m[3] == 'boiler') and 20 or 30} end
end
game.speed = 30""" % json.dumps(manual))
    # the bus has no consumers yet: a loader into a deleting infinity chest at every bus end (test only)
    sinks = [[p[2] + 0.5, p[3] + 0.5, p[4]] for p in plan.stats["ports"]]
    lua("""
local s = game.surfaces.nauvis
local V = {[0] = {0, -1}, [4] = {1, 0}, [8] = {0, 1}, [12] = {-1, 0}}
for _, p in pairs(helpers.json_to_table([==[%s]==])) do
  local v = V[p[3]]
  for _, e in pairs(s.find_entities_filtered{area = {{p[1] - 0.4 + math.min(0, v[1]), p[2] - 0.4 + math.min(0, v[2])},
      {p[1] + 0.4 + math.max(0, v[1]), p[2] + 0.4 + math.max(0, v[2])}}, type = {'tree', 'simple-entity'}}) do e.destroy() end
  s.create_entity{name = 'loader-1x1', position = {p[1], p[2]}, direction = p[3], type = 'input', force = 'player'}
  local c = s.create_entity{name = 'infinity-chest', position = {p[1] + v[1], p[2] + v[2]}, force = 'player'}
  c.remove_unfiltered_items = true
end""" % json.dumps(sinks))

    items = ["iron-plate", "copper-plate", "stone-brick", "iron-ore", "copper-ore", "stone", "coal"]
    count_lua = """
local st = game.forces.player.get_item_production_statistics(game.surfaces.nauvis)
local out = {tick = game.tick}
for _, n in pairs(helpers.json_to_table('%s')) do out[n] = st.get_input_count(n) end
rcon.print(helpers.table_to_json(out))""" % json.dumps(items)

    def run_until(tick):
        while int(lua("rcon.print(game.tick)")) < tick:
            time.sleep(1)
    start = int(lua("rcon.print(game.tick)"))
    window = 5 * 3600
    run_until(start + int(a.minutes * 3600) - window)  # warm-up: belts fill, furnaces start
    c0 = json.loads(lua(count_lua))
    run_until(c0["tick"] + window)
    c1 = json.loads(lua(count_lua))
    lua("game.speed = 1")
    mins = (c1["tick"] - c0["tick"]) / 3600
    stats = json.loads(lua("""
local s, f = game.surfaces.nauvis, game.forces.player
local out = {status = {}}
for _, e in pairs(s.find_entities_filtered{type={'furnace','mining-drill','boiler','generator','inserter'}, force=f}) do
  local k = e.name .. ':' .. (({[defines.entity_status.working]='working'})[e.status] or tostring(e.status))
  out.status[k] = (out.status[k] or 0) + 1
end
local names = {}
for k, v in pairs(defines.entity_status) do names[tostring(v)] = k end
out.names = names
rcon.print(helpers.table_to_json(out))"""))
    stats["per_min"] = {n: (c1.get(n, 0) - c0.get(n, 0)) / mins for n in items}
    names = stats.pop("names")
    status = {k.split(":")[0] + ":" + (names.get(k.split(":")[1], k.split(":")[1])): v for k, v in stats["status"].items()}
    print(f"   measured per min ({mins:.1f} min window):", {k: round(v, 1) for k, v in stats["per_min"].items()})
    print("   planned per min:", plan.stats["per_min"])
    print("   entity status:", status)
    for item, want in plan.stats["per_min"].items():
        got = stats["per_min"].get(item, 0)
        good = abs(got - want) <= 0.1 * want
        ok &= good
        print(f"   {item}: plan {want:.1f}/min, measured {got:.1f}/min -> {'OK' if good else 'OFF'}")
    # furnaces must all work; drills may be backed up (mines are sized up to whole drills), nothing else
    bad = {k: v for k, v in status.items() if k.split(":")[0] in ("stone-furnace", "burner-mining-drill",
                                                                     "electric-mining-drill", "boiler", "steam-engine")
           and not k.endswith(":working")
           and not (k.endswith(":waiting_for_space_in_destination") and "drill" in k)}
    if bad:
        print("   not working:", bad)
        ok = False
    if a.stage == "electric":
        pw = link.call("power")
        print("   power:", json.dumps(pw)[:300])
    log = (ROOT / "testenv" / "inst" / a.name / "server.log").read_text(encoding="utf-8", errors="replace")
    errs = [line for line in log.splitlines() if "Error" in line or "error" in line.lower() and "claude" in line.lower()]
    if errs:
        print("   server.log errors:", errs[:5])
        ok = False
    if not a.keep:
        restart(a.name, a.slot, stop=True)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
