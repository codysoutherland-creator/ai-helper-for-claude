"""In-engine check of a planner v2 conversion (R7 'reuse in place', round 3; not collected by pytest).

    .venv\\Scripts\\python.exe tests\\live_convert.py --save <save.zip> [--slot 36] [--minutes 15] [--speed 10]
        [--pre-op '{"op": "clear_claude_ghosts", "plan": "abc"}'] [--out result.json]

The planner's conversions() on the live map (the player's idle burner-drill lines on ore that drop onto a belt) are
built into a variant (balanced) exactly as the Rebuild card would: only the belt is listed for removal, the drills
stay and get a stone furnace + burner inserter + chest in front. The plan is approved through the MCP tool (plan_apply:
re-validation + the plan_apply op) and built by the helper (plan_helper build: dismantle the listed belts, build the
ghosts, coal round), on a server started like a player's game (restart.ps1 -NoDebugOps). Checks: exactly the listed
belts removed (census by unit_number), every converted drill still there and dropping into its new furnace, the
converted pairs make plates (>= 90 % of 15/min each from 1 minute after they are built), the player's ghosts unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from live_onsite import CENSUS, SAMPLE, restart  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", required=True)
    ap.add_argument("--slot", type=int, default=36)
    ap.add_argument("--name", default="convert")
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--goal", default="starter base, tidy up my old stuff")
    ap.add_argument("--pre-op", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    assert 31 <= a.slot <= 39, "test slots 31-39 only"
    os.environ.update({"FACTORIO_UDP_PORT": str(34500 + a.slot), "FACTORIO_RCON_PORT": str(27016 + a.slot),
                       "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1",
                       "FACTORIO_PLAN_UNCHARTED": "1",
                       "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / a.name / "write" / "script-output")})
    sys.path.insert(0, str(ROOT))
    from factorio_claude import link, rcon, server
    from factorio_claude.planner import placer, staged

    res = {"args": vars(a), "checks": {}}
    out = Path(a.out) if a.out else ROOT / "testenv" / "inst" / a.name / "live_convert.json"

    def check(name, ok, info=""):
        res["checks"][name] = {"ok": bool(ok), "info": info}
        print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + str(info) if info != '' else ''}", flush=True)

    restart(a.name, a.slot, a.save, no_debug=True)
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
        except Exception:  # noqa: BLE001
            pass
        for op in a.pre_op:
            d = json.loads(op)
            print("pre-op", d["op"], link.call(d.pop("op"), **d), flush=True)
        server.PLANS = Path(tempfile.mkdtemp(prefix="plans_"))
        cx, cy, surface = server._player_spot()
        world, gd = server._world(cx, cy, surface)
        st = server._state(cx, cy)
        goal = placer.parse_goal(a.goal)
        goal["near"] = (cx, cy)
        pl = staged.PlannerV2(world, gd, st, goal)
        convs = list(pl.conversions().values())
        check("the planner finds a line to convert", bool(convs), [(c.res, c.n, len(c.belts)) for c in convs])
        if not convs:
            return 1
        cv = max(convs, key=lambda c: c.n)
        cv.cluster.action, cv.cluster.reason = "reuse", "engine check of a conversion"
        plan = pl.build(pl.spec("balanced"), converting=[cv])
        pl.finish(plan)
        plan.save(server.PLANS)
        res["plan"] = {"id": plan.id, "card": plan.summary, "removes": plan.removes, "eta": plan.stats["eta"]}
        print(plan.summary, flush=True)
        before = census()
        player_ghosts = sorted((r["n"], r["x"], r["y"]) for r in before.values()
                               if r["n"].startswith("ghost:") and not r.get("t"))
        ap_ = json.loads(server.plan_apply(plan.id))
        res["apply"] = ap_
        check("plan_apply placed it", (ap_.get("applied") or {}).get("placed", 0) > 0, ap_.get("applied") or ap_)
        lua(f"game.speed = {a.speed} rcon.print('ok')")
        t0 = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
        res["helper_msg"] = json.loads(server.plan_helper(plan.id, "build")).get("msg")
        conv_ents = [[e.name, e.x, e.y] for e in cv.block.placed() if e.name in ("stone-furnace", "burner-mining-drill",
                                                                                "burner-inserter")]
        payload = json.dumps({"ents": conv_ents, "repairs": []})
        samples, end = [], t0 + int(a.minutes * 3600)
        while True:
            tick = int(lua("rcon.print(game.tick)").strip().splitlines()[-1])
            smp = json.loads(lua(SAMPLE % payload).strip().splitlines()[-1])
            smp["min"] = round((tick - t0) / 3600, 2)
            stt = link.call("agent_status")
            samples.append(smp)
            print(f"  t={smp['min']:5.1f} built={smp['built']}/{len(conv_ents)} made={smp['made']} "
                  f"nofuel={smp['nofuel']} status={smp['status']} {[x['msg'] for x in (stt.get('log') or [])[-1:]]}",
                  flush=True)
            if tick >= end:
                break
            time.sleep(2.0)
        lua("game.speed = 1 rcon.print('ok')")
        res["samples"] = samples
        after = census()
        listed = {r[3] for r in plan.removes}
        gone = {k for k, v in before.items() if not v["n"].startswith("ghost:")} - set(after)
        check("exactly the listed belts removed", gone == listed, f"removed {len(gone)}, listed {len(listed)}, "
              f"extra {sorted(gone - listed)[:5]}, kept {sorted(listed - gone)[:5]}")
        drills = {d.unit for d in cv.drills}
        check("the converted drills are still there", drills <= set(after), len(drills & set(after)))
        done = next((s["min"] for s in samples if s["built"] >= len(conv_ents)), None)
        res["built_min"] = done
        tail = [s for s in samples if done is not None and s["min"] >= done + 1.0]   # running, past the ramp-up
        want = 15.0 * cv.n
        check("the converted pairs are built", done is not None, done)
        if len(tail) >= 2 and tail[-1]["min"] - tail[0]["min"] >= 2.5:
            rate = (tail[-1]["made"] - tail[0]["made"]) / max(1e-9, tail[-1]["min"] - tail[0]["min"])
            res["rate"] = {"plan": want, "measured": round(rate, 1)}
            check("the converted pairs make >= 90 % of 15 plates/min each", rate >= 0.9 * want, res["rate"])
        pg = sorted((r["n"], r["x"], r["y"]) for r in after.values() if r["n"].startswith("ghost:") and not r.get("t"))
        check("player ghosts unchanged", pg == player_ghosts, f"{len(pg)} vs {len(player_ghosts)}")
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
