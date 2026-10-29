"""No-cheat in-engine run of a planner v2 on-site plan (PLANNER-v2.md A1/A2; not collected by pytest).

    .venv\\Scripts\\python.exe tests\\live_onsite.py --save seed_4004.zip --variant quick [--slot 32] [--goal "..."]
        [--minutes 25] [--speed 10] [--pre-op '{"op": "clear_claude_ghosts", "plan": "abc"}'] [--out result.json]
        [--debug-ops]

The server starts like a player's game (restart.ps1 -NoDebugOps: no instant build, no given items; --debug-ops turns
the test ops back on). Scaffolding, all documented: peaceful mode and the helper's old queue stopped (agent_stop).
Then: plan_starter_base (the MCP tool function) -> the chosen variant -> plan_apply (MCP tool: re-validation + the
plan_apply op, as the Plans tab's Approve) -> plan_helper build (the helper takes the listed coal, dismantles, gathers,
crafts, builds only this plan's ghosts, then runs its coal round) -> game.speed for --minutes in-game minutes. Every
in-game minute it samples the plan's furnaces (plates made), the burners' fuel and the helper.
Checks: first plate measured vs the plan's eta.first_plates (within 50 %, and within ~10 min for quick), plate rate
over the last 10 minutes vs the plan, every burner of the plan fuelled at the end, the player's ghosts unchanged,
nothing of the force removed except the plan's listed removals, and the S0 repairs (if any) working.

--stage2-minutes M (round 3): then the variant's placed stage 2 (stages[1].id) is approved the same way (plan_apply
re-validates it against the map as it is now) and the helper builds it (plan_helper build); after M more in-game
minutes: every stage-2 entity built, the labs powered (scaffolding, documented: once it is built, 10 red packs put
into each lab and Automation queued by script, as the player would by hand - and, when red science is still locked,
its trigger tech marked researched, standing for the lab the player crafts by hand: the helper's crafting does not
trigger it; then research progress > 0 and no lab without power), the boiler fuelled, stage 1's burners still fuelled by stage 2's coal round and stage 1's plate rate
still >= 90 % of the plan, the player's ghosts unchanged, nothing removed.
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

CENSUS = r"""
local out = {}
for _, e in pairs(game.surfaces.nauvis.find_entities_filtered{force = "player"}) do
  if e.type ~= "character" then
    local g = e.type == "entity-ghost"
    out[#out + 1] = {u = e.unit_number, n = g and ("ghost:" .. e.ghost_name) or e.name, x = e.position.x,
      y = e.position.y, t = g and e.tags and e.tags.claude_plan or nil}
  end
end
rcon.print(helpers.table_to_json(out))
"""

SAMPLE = r"""
local s = game.surfaces.nauvis
local P = helpers.json_to_table([==[%s]==])
local made, fuel, nofuel, built = 0, 0, 0, 0
local names = {}
for k, v in pairs(defines.entity_status) do names[v] = k end
local st = {}
for _, r in pairs(P.ents) do
  local e = s.find_entity(r[1], {r[2], r[3]})
  if e then
    built = built + 1
    if e.type == "furnace" then made = made + e.products_finished end
    if e.burner then
      local n = e.get_fuel_inventory().get_item_count()
      fuel = fuel + n
      if n == 0 and not (e.burner.remaining_burning_fuel > 0) then nofuel = nofuel + 1 end
    end
    local k = e.name .. ":" .. tostring(names[e.status] or e.status)
    st[k] = (st[k] or 0) + 1
  end
end
local rep = {}
for _, r in pairs(P.repairs) do
  local e = s.find_entity(r[1], {r[2], r[3]})
  rep[#rep + 1] = e and tostring(names[e.status] or e.status) or "gone"
end
rcon.print(helpers.table_to_json{tick = game.tick, made = made, fuel = fuel, nofuel = nofuel, built = built,
  status = st, repairs = rep})
"""


def restart(name, slot, save=None, stop=False, no_debug=True):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", name, "-Slot", str(slot)]
    args += ["-Stop"] if stop else (["-Save", save] + (["-NoDebugOps"] if no_debug else []))
    with tempfile.TemporaryFile() as f:  # never pipe: the started factorio.exe would keep the pipe open
        subprocess.run(args, stdout=f, stderr=f, timeout=120)


LABS = r"""
local s = game.surfaces.nauvis
local P = helpers.json_to_table([==[%s]==])
local names = {}
for k, v in pairs(defines.entity_status) do names[v] = k end
local out = {labs = {}, engines = {}, boiler_fuel = 0, built = 0, research = nil, progress = 0}
for _, r in pairs(P.ents) do
  local e = s.find_entity(r[1], {r[2], r[3]})
  if e then out.built = out.built + 1 end
end
for _, r in pairs(P.labs) do
  local e = s.find_entity("lab", {r[1], r[2]})
  out.labs[#out.labs + 1] = e and tostring(names[e.status] or e.status) or "gone"
  if e and P.feed then
    e.get_inventory(defines.inventory.lab_input).insert{name = "automation-science-pack", count = 10}
  end
end
for _, e in pairs(s.find_entities_filtered{name = "steam-engine", force = "player"}) do
  out.engines[#out.engines + 1] = {x = e.position.x, y = e.position.y, status = tostring(names[e.status] or e.status),
    kw = math.floor((e.energy_generated_last_tick or 0) * 60 / 1000)}
end
for _, e in pairs(s.find_entities_filtered{name = "boiler", force = "player"}) do
  out.boiler_fuel = out.boiler_fuel + e.get_fuel_inventory().get_item_count()
end
local f = game.forces.player
if P.feed and not f.technologies["automation-science-pack"].researched then
  -- scaffolding: the player crafts a lab by hand (the helper's crafting does not trigger red science, see the card)
  f.technologies["automation-science-pack"].researched = true
  out.lab_crafted_by_script = true
end
if P.feed and not f.current_research then out.added = f.add_research("automation") end
out.research = f.current_research and f.current_research.name or nil
out.progress = f.research_progress or 0
out.automation = f.technologies["automation"].researched
rcon.print(helpers.table_to_json(out))
"""

LABS_AT = ("local o = {} for _, e in pairs(game.surfaces.nauvis.find_entities_filtered{name='lab', force='player'}) "
           "do o[#o+1] = {x = e.position.x, y = e.position.y} end rcon.print(helpers.table_to_json(o))")


def stage2(a, plan, server, link, lua, census, check, res, payload1, player_ghosts):
    """Approve the variant's placed stage 2 and let the helper build it (see the module doc)."""
    from factorio_claude.planner.plan import Plan
    row = next((s for s in plan.stats.get("stages") or [] if s.get("placed")), None)
    check("stage 2 was placed by the planner", row is not None and row.get("id"), row and row.get("id"))
    if not row:
        return
    s2 = Plan.load(server.PLANS / f"{row['id']}.json")
    res["stage2"] = {"id": s2.id, "card": s2.summary, "eta": s2.stats.get("eta"), "counts": s2.stats.get("counts")}
    print(s2.summary, flush=True)
    before = census()
    ap_ = json.loads(server.plan_apply(s2.id))
    res["stage2"]["apply"] = ap_
    check("stage 2: plan_apply re-validated and placed it", (ap_.get("applied") or {}).get("placed", 0) > 0,
          ap_.get("applied") or ap_.get("why"))
    if not ap_.get("applied"):
        return
    lua(f"game.speed = {a.speed} rcon.print('ok')")
    t0 = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
    res["stage2"]["helper_msg"] = json.loads(server.plan_helper(s2.id, "build")).get("msg")
    ents2 = [[e.name, e.x, e.y] for e in s2.entities if not (e.extra or {}).get("existing")]
    labs = [[e.x, e.y] for e in s2.entities if e.name == "lab"]
    if (s2.stats.get("labs") or {}).get("reused"):
        labs = [[r["x"], r["y"]] for r in json.loads(lua(LABS_AT).strip().splitlines()[-1])]
    end = t0 + int(a.stage2_minutes * 3600)
    fed, samples = False, []
    while True:
        tick = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
        smp = json.loads(lua(LABS % json.dumps({"ents": ents2, "labs": labs, "feed": False})).strip().splitlines()[-1])
        s1 = json.loads(lua(SAMPLE % payload1).strip().splitlines()[-1])
        smp["min"] = round((tick - t0) / 3600, 2)
        smp["s1_made"], smp["s1_nofuel"] = s1["made"], s1["nofuel"]
        st = link.call("agent_status")
        smp["helper"] = (st.get("current") or {}).get("type")
        samples.append(smp)
        print(f"  s2 t={smp['min']:5.1f} built={smp['built']}/{len(ents2)} labs={smp['labs']} "
              f"engines={smp['engines']} boiler_fuel={smp['boiler_fuel']} s1_made={s1['made']} "
              f"s1_nofuel={s1['nofuel']} helper={smp['helper']} {[x['msg'] for x in (st.get('log') or [])[-1:]]}",
              flush=True)
        if not fed and smp["built"] >= len(ents2):
            # scaffolding: the player puts red packs into the labs and picks a research (the plan says to)
            lua(LABS % json.dumps({"ents": ents2, "labs": labs, "feed": True}))
            fed = True
            res["stage2"]["built_min"] = smp["min"]
        if tick >= end:
            break
        time.sleep(2.0)
    lua("game.speed = 1 rcon.print('ok')")
    res["stage2"]["samples"] = samples
    last = samples[-1]
    check("stage 2: every entity built", last["built"] == len(ents2), f"{last['built']}/{len(ents2)}")
    check("stage 2: no lab without power", bool(labs) and all(x not in ("no_power", "gone") for x in last["labs"]),
          last["labs"])
    check("stage 2: research progresses on its power (Automation researched or in progress)",
          last.get("automation") or (last.get("progress") or 0) > 0,
          {"research": last.get("research"), "progress": last.get("progress"), "automation": last.get("automation")})
    check("stage 2: the boiler holds fuel (the round includes it)", last["boiler_fuel"] > 0, last["boiler_fuel"])
    check("stage 2: stage 1's burners still fuelled", last["s1_nofuel"] == 0, last["s1_nofuel"])
    want = sum(v for k, v in (plan.stats.get("per_min") or {}).items() if k.endswith("-plate"))
    tail = [x for x in samples if x["min"] >= a.stage2_minutes - 10.01]
    if len(tail) >= 2 and want and a.stage2_minutes >= 12:
        rate = (tail[-1]["s1_made"] - tail[0]["s1_made"]) / max(1e-9, tail[-1]["min"] - tail[0]["min"])
        res["stage2"]["s1_rate"] = round(rate, 1)
        check("stage 2: stage 1 still makes >= 90 % of its plates", rate >= 0.9 * want, f"{round(rate, 1)} / {want}")
    e2 = s2.stats.get("eta") or {}
    if res["stage2"].get("built_min") is not None and e2.get("power_on") is not None and e2.get("start") is not None:
        res["stage2"]["build_min_measured_vs_model"] = [res["stage2"]["built_min"], round(e2["power_on"] - e2["start"], 1)]
    after = census()
    pg = sorted((r["n"], r["x"], r["y"]) for r in after.values() if r["n"].startswith("ghost:") and not r.get("t"))
    check("stage 2: player ghosts unchanged", pg == player_ghosts, f"{len(pg)} vs {len(player_ghosts)}")
    gone = {k for k, v in before.items() if not v["n"].startswith("ghost:")} - set(after)
    check("stage 2: nothing of the force removed", not gone, sorted(gone)[:5])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default="seed_4004.zip")
    ap.add_argument("--variant", default="quick")
    ap.add_argument("--goal", default="set me up with a starter base")
    ap.add_argument("--slot", type=int, default=32)
    ap.add_argument("--name", default="onsite")
    ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--pre-op", action="append", default=[])
    ap.add_argument("--debug-ops", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--stage2-minutes", type=float, default=0)
    a = ap.parse_args()
    assert 1 <= a.slot and 27016 + a.slot != 27015 and 34500 + a.slot != 34500, "never the real game's ports"
    os.environ.update({"FACTORIO_UDP_PORT": str(34500 + a.slot), "FACTORIO_RCON_PORT": str(27016 + a.slot),
                       "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1",
                       "FACTORIO_PLAN_UNCHARTED": "1",
                       "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / a.name / "write" / "script-output")})
    sys.path.insert(0, str(ROOT))
    from factorio_claude import link, rcon, server
    from factorio_claude.planner.plan import Plan

    res = {"args": vars(a), "checks": {}}
    out = Path(a.out) if a.out else ROOT / "testenv" / "inst" / a.name / "live_onsite.json"

    def check(name, ok, info=""):
        res["checks"][name] = {"ok": bool(ok), "info": info}
        print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + str(info) if info != '' else ''}", flush=True)

    print(f"== {a.save}: restarting slot {a.slot} ({'debug ops' if a.debug_ops else 'no debug ops'})", flush=True)
    restart(a.name, a.slot, a.save, no_debug=not a.debug_ops)
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    rcon._lua_armed = False

    def lua(code):
        return rcon.lua(" ".join(ln for ln in code.splitlines() if not ln.strip().startswith("--")))

    def census():
        return {(r.get("u") or (r["n"], r["x"], r["y"])): r for r in json.loads(lua(CENSUS).strip().splitlines()[-1])}
    try:
        lua("game.surfaces.nauvis.peaceful_mode = true rcon.print('ok')")
        try:
            link.call("agent_stop")
        except Exception:  # noqa: BLE001 - no helper yet
            pass
        for op in a.pre_op:
            d = json.loads(op)
            print("pre-op", d["op"], link.call(d.pop("op"), **d), flush=True)
        server.PLANS = Path(tempfile.mkdtemp(prefix="plans_"))
        t0 = time.time()
        o = json.loads(server.plan_starter_base(a.goal, variants=4))
        if "plans" not in o:
            print("!! planning failed:", o)
            return 1
        res["plan_s"] = round(time.time() - t0, 1)
        res["offered"] = [{k: b.get(k) for k in ("id", "variant", "eta", "build_now", "score", "recommended")}
                          for b in o["plans"]]
        b = next((p for p in o["plans"] if p["variant"] == a.variant), None)
        if b is None:
            print("!! no variant", a.variant, [p["variant"] for p in o["plans"]])
            return 1
        plan = Plan.load(server.PLANS / f"{b['id']}.json")
        pid = plan.id
        res["plan"] = {"id": pid, "card": plan.summary, "eta": plan.stats.get("eta"), "bill": plan.stats.get("bill"),
                       "per_min": plan.stats.get("per_min"), "timeline": plan.stats.get("timeline"),
                       "takes": len(plan.stats.get("takes") or []), "removes": len(plan.removes),
                       "repairs": plan.stats.get("repairs")}
        print(plan.summary, flush=True)
        before = census()
        player_ghosts = sorted((r["n"], r["x"], r["y"]) for r in before.values()
                               if r["n"].startswith("ghost:") and not r.get("t"))
        res["player_ghosts"] = len(player_ghosts)
        ap_ = json.loads(server.plan_apply(pid))
        res["apply"] = ap_
        check("plan_apply placed the plan", (ap_.get("applied") or {}).get("placed", 0) > 0, ap_.get("applied"))
        lua(f"game.speed = {a.speed} rcon.print('ok')")
        t_start = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
        hm = json.loads(server.plan_helper(pid, "build"))
        res["helper_msg"] = hm.get("msg")
        ents = [[e.name, e.x, e.y] for e in plan.entities if e.name in ("stone-furnace", "burner-mining-drill",
                                                                       "burner-inserter", "boiler")]
        reps = [[r[0], r[1], r[2]] for r in plan.stats.get("refuel_existing") or []]
        payload = json.dumps({"ents": ents, "repairs": reps})
        samples, first_tick = [], None
        end = t_start + int(a.minutes * 3600)
        nxt = t_start
        while True:
            tick = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
            if tick >= nxt:
                smp = json.loads(lua(SAMPLE % payload).strip().splitlines()[-1])
                smp["min"] = round((smp["tick"] - t_start) / 3600, 2)
                st = link.call("agent_status")
                smp["helper"] = {"cur": (st.get("current") or {}).get("type"), "q": len(st.get("queue") or []),
                                 "halted": st.get("halted"), "log": [x["msg"] for x in (st.get("log") or [])[-3:]]}
                samples.append(smp)
                print(f"  t={smp['min']:5.1f} built={smp['built']}/{len(ents)} made={smp['made']} fuel={smp['fuel']} "
                      f"nofuel={smp['nofuel']} helper={smp['helper']['cur']} {smp['helper']['log'][-1:]}", flush=True)
                if first_tick is None and smp["made"] > 0:
                    first_tick = smp["tick"]
                nxt = tick + (600 if first_tick is None else 3600)   # 10 s samples until the first plate
            if tick >= end:
                break
            time.sleep(0.25)
        lua("game.speed = 1 rcon.print('ok')")
        res["samples"] = samples
        st = link.call("agent_status")
        res["helper_log"] = [x["msg"] for x in st.get("log") or []]
        eta = plan.stats.get("eta") or {}
        pred = eta.get("first_plates")
        meas = round((first_tick - t_start) / 3600, 2) if first_tick is not None else None
        res["first_plates"] = {"predicted": pred, "measured": meas}
        check("first plates measured", meas is not None, meas)
        if meas is not None and pred:
            check("first plates within 50 % of the plan's ETA", abs(meas - pred) <= 0.5 * pred + 1e-9,
                  f"measured {meas} vs predicted {pred} min")
        if a.variant == "quick":
            check("quick start: first plates within ~10 in-game minutes", meas is not None and meas <= 10.5, meas)
        want = sum(v for k, v in (plan.stats.get("per_min") or {}).items() if k.endswith("-plate"))
        last = [s for s in samples if s["min"] >= a.minutes - 10.01]
        if len(last) >= 2 and want:
            rate = (last[-1]["made"] - last[0]["made"]) / max(1e-9, last[-1]["min"] - last[0]["min"])
            res["rate"] = {"plan": want, "measured": round(rate, 1), "window": [last[0]["min"], last[-1]["min"]]}
            check("plate rate over the last 10 min >= 90 % of the plan", rate >= 0.9 * want, res["rate"])
        check("every plan burner is fuelled at the end", samples and samples[-1]["nofuel"] == 0,
              samples[-1]["nofuel"] if samples else None)
        if reps:
            check("S0 repairs: the refuelled machines work", all(s in ("working", "normal") for s in
                                                                 samples[-1]["repairs"]), samples[-1]["repairs"])
        after = census()
        pg = sorted((r["n"], r["x"], r["y"]) for r in after.values() if r["n"].startswith("ghost:") and not r.get("t"))
        check("player ghosts unchanged", pg == player_ghosts, f"{len(pg)} vs {len(player_ghosts)}")
        listed = {r[3] for r in plan.removes}
        gone = {k for k, v in before.items() if not v["n"].startswith("ghost:")} - set(after)
        check("nothing of the force removed except the listed entities", gone <= listed,
              f"removed {len(gone)}, listed {len(listed)}, extra {sorted(gone - listed)[:5]}")
        if a.stage2_minutes > 0:
            stage2(a, plan, server, link, lua, census, check, res, payload, player_ghosts)
    finally:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        if not a.keep:
            restart(a.name, a.slot, stop=True)
    bad = [k for k, v in res["checks"].items() if not v["ok"]]
    print(f"== {'OK' if not bad else 'FAILED: ' + ', '.join(bad)} -> {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
