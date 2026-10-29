"""In-engine check of planner v2 rebuilds (PLANNER-v2.md A6) on a headless test slot (not collected by pytest).

    .venv\\Scripts\\python.exe tests\\live_rebuild.py [--save seed_4004.zip] [--goal "..."] [--slot 34] [--name rebuild]

Restarts the slot from testenv/write/<save>, plans with server.plan_starter_base (goal words that allow removals),
takes the "rebuild" variant and checks, with a census of the force's entities by unit_number:
  1. Approve (plan_apply op) marks exactly the listed entities for deconstruction, nothing else of the force;
  2. Reject (plan_drop) cancels every mark and touches nothing else;
  3. after a second Approve, the helper's dismantle task picks up exactly the listed entities (removed set == listed
     set minus "changed"), the salvage ends in the helper's inventory (item delta within 5 % of the plan's salvage),
     player ghosts are unchanged, and the ghosts that waited for the ground are placed.
Optional --pre-lua runs Lua first (e.g. clear_claude_ghosts for a real-save copy). Exit 0 when every check passes.
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
      y = e.position.y, m = (not g) and e.to_be_deconstructed() or nil,
      t = g and e.tags and e.tags.claude_plan or nil, p = g and e.last_user ~= nil or nil}
  end
end
rcon.print(helpers.table_to_json(out))
"""


def restart(name, slot, save=None, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", name, "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", save]
    with tempfile.TemporaryFile() as f:  # never pipe: the started factorio.exe would keep the pipe open
        subprocess.run(args, stdout=f, stderr=f, timeout=120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default="seed_4004.zip")
    ap.add_argument("--goal", default="starter base, tidy up my old stuff")
    ap.add_argument("--slot", type=int, default=34)
    ap.add_argument("--name", default="rebuild")
    ap.add_argument("--pre-op", action="append", default=[], help='op JSON run first, e.g. '
                    '\'{"op": "clear_claude_ghosts", "plan": "abc"}\'')
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    a = ap.parse_args()
    assert a.slot >= 1, "never slot 0"
    os.environ.update({"FACTORIO_UDP_PORT": str(34500 + a.slot), "FACTORIO_RCON_PORT": str(27016 + a.slot),
                       "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1",
                       "FACTORIO_PLAN_UNCHARTED": "1",
                       "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / a.name / "write" / "script-output")})
    sys.path.insert(0, str(ROOT))
    from factorio_claude import link, rcon, server
    from factorio_claude.planner.plan import Plan

    fails = []

    def check(name, ok, info=""):
        print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + str(info) if info else ''}", flush=True)
        if not ok:
            fails.append(name)

    print(f"== {a.save}: restarting slot {a.slot}", flush=True)
    restart(a.name, a.slot, a.save)
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    rcon._lua_armed = False

    def lua(code):
        return rcon.lua(" ".join(ln for ln in code.splitlines() if not ln.strip().startswith("--")))

    def census():
        return {(r.get("u") or (r["n"], r["x"], r["y"])): r for r in json.loads(lua(CENSUS).strip().splitlines()[-1])}
    lua("game.surfaces.nauvis.peaceful_mode = true rcon.print('ok')")
    for op in a.pre_op:
        d = json.loads(op)
        print("pre-op", d["op"], link.call(d.pop("op"), **d))
    server.PLANS = Path(tempfile.mkdtemp(prefix="plans_"))
    out = json.loads(server.plan_starter_base(a.goal, variants=4))
    if "plans" not in out:
        print("!! planning failed:", out)
        return 1
    reb = next((p for p in out["plans"] if p["variant"] == "rebuild"), None)
    check("a rebuild variant is offered", reb is not None, [p["variant"] for p in out["plans"]])
    if reb is None:
        return 1
    plan = Plan.load(server.PLANS / f"{reb['id']}.json")
    pid = plan.id
    listed = {r[3] for r in plan.removes}
    print(f"   {pid}: removes {len(plan.removes)} ({reb['removes']['count_by_name']}); card: {reb['card'][:300]}")
    check("every removal has a reason", all(c.get("reason") for c in reb["removes"]["clusters"]))
    before = census()
    player_ghosts = sorted((r["n"], r["x"], r["y"]) for r in before.values() if r.get("p"))

    def marked(c):
        return {k for k, r in c.items() if r.get("m")}

    # -- approve, then reject: marks exactly the listed entities, then cancels all of them ------------------------
    r = link.call("plan_apply", id=pid)
    for _ in range(50):
        if not link.call("plan_get", id=pid).get("drawing") and link.call("plan_get", id=pid)["status"] != "applying":
            break
        time.sleep(0.2)
    c1 = census()
    check("approve marks exactly the listed entities", marked(c1) == listed - set(),
          f"marked {len(marked(c1))}, listed {len(listed)}, extra {sorted(marked(c1) - listed)[:5]}, "
          f"missing {sorted(listed - marked(c1))[:5]}")
    check("approve reports the marks", (r.get("removes") or {}).get("marked") == len(listed), r.get("removes"))
    link.call("plan_drop", id=pid)
    c2 = census()
    check("reject cancels every mark", not marked(c2), sorted(marked(c2))[:5])
    nonghost_before = {k for k, v in before.items() if not v["n"].startswith("ghost:")}
    nonghost_after = {k for k, v in c2.items() if not v["n"].startswith("ghost:")}
    check("reject touches nothing else (non-ghost entities)", nonghost_before == nonghost_after)
    link.call("clear_claude_ghosts", plan=pid)

    # -- Dismiss while the helper is dismantling (the Plans tab's Dismiss and Reject both call plans.drop): the
    # remaining marks are cancelled, what was already picked up stays picked up, nothing else is touched -----------
    if len(listed) >= 3:
        server._send_preview(plan, show=False)
        link.call("plan_apply", id=pid)
        link.call("agent_spawn")
        link.call("agent_tasks", tasks=[{"type": "dismantle", "plan": pid}], replace=True)
        lua("game.speed = 4 rcon.print('ok')")
        t_end = time.time() + 120
        while time.time() < t_end:
            rc = (link.call("plan_get", id=pid).get("removes") or {})
            if rc.get("removed", 0) >= 1:
                break
            time.sleep(0.1)
        lua("game.speed = 1 rcon.print('ok')")
        link.call("agent_stop")
        link.call("plan_drop", id=pid)
        cd = census()
        gone_d = nonghost_before - {k for k, v in cd.items() if not v["n"].startswith("ghost:")}
        check("dismiss mid-dismantle: remaining marks cancelled", not marked(cd), sorted(marked(cd))[:5])
        check("dismiss mid-dismantle: only listed entities were removed", 0 < len(gone_d) < len(listed) + 1 and
              gone_d <= listed, f"removed {len(gone_d)} of {len(listed)}, extra {sorted(gone_d - listed)[:5]}")
        link.call("clear_claude_ghosts", plan=pid)
        # the dismantled ones are gone now: plan again on the changed map for the full run below
        nonghost_before = {k for k, v in cd.items() if not v["n"].startswith("ghost:")}
        server.PLANS = Path(tempfile.mkdtemp(prefix="plans_"))
        out = json.loads(server.plan_starter_base(a.goal, variants=4))
        reb = next((p for p in out.get("plans") or [] if p["variant"] == "rebuild"), None)
        check("after the partial dismantle a rebuild variant is still offered (or nothing is left to remove)",
              reb is not None or len(gone_d) == len(listed), [p["variant"] for p in out.get("plans") or []])
        if reb is None:
            if not a.keep:
                restart(a.name, a.slot, stop=True)
            print(f"== {'OK' if not fails else 'FAILED: ' + ', '.join(fails)}")
            return 1 if fails else 0
        plan = Plan.load(server.PLANS / f"{reb['id']}.json")
        pid = plan.id
        listed = {r[3] for r in plan.removes}
        before = cd

    # -- approve again; the helper dismantles exactly the listed entities --------------------------------------------
    server._send_preview(plan, show=False)
    r = link.call("plan_apply", id=pid)
    print("   apply:", {k: r.get(k) for k in ("placed", "deferred", "blocked", "removes")})
    link.call("agent_spawn")
    inv0 = link.call("agent_status").get("inventory") or {}
    link.call("agent_tasks", tasks=[{"type": "dismantle", "plan": pid}], replace=True)
    lua("game.speed = 10 rcon.print('ok')")
    t_end = time.time() + 240
    st = {}
    while time.time() < t_end:
        st = link.call("agent_status")
        if not st.get("current") and not st.get("queue"):
            break
        time.sleep(0.5)
    lua("game.speed = 1 rcon.print('ok')")
    print("   helper log:", [x["msg"] for x in (st.get("log") or [])[-4:]], "halted:", st.get("halted"))
    c3 = census()
    gone = nonghost_before - {k for k, v in c3.items() if not v["n"].startswith("ghost:")}
    info = link.call("plan_get", id=pid)
    rc = info.get("removes") or {}
    check("dismantled exactly the listed entities", gone == listed,
          f"gone {len(gone)}, listed {len(listed)}, extra {sorted(gone - listed)[:5]}, counts {rc}")
    pg = sorted((v["n"], v["x"], v["y"]) for v in c3.values() if v.get("p"))
    check("player ghosts untouched", pg == player_ghosts, f"{len(pg)} vs {len(player_ghosts)}")
    inv1 = st.get("inventory") or {}
    salvage = (reb.get("bill") or {}).get("salvage") or {}
    delta = {k: inv1.get(k, 0) - inv0.get(k, 0) for k in set(inv1) | set(inv0)}
    # the entities themselves come back exactly; their contents (fuel, ore in a running furnace) change while the
    # game runs, so the total is compared within 5 %
    names = {r[0] for r in plan.removes}
    exact = all(delta.get(k, 0) == v for k, v in salvage.items() if k in names)
    tot_s = sum(salvage.values())
    tot_d = sum(delta.get(k, 0) for k in salvage)
    check("salvage ends in the helper's inventory (entities exact, total within 5 %)",
          exact and abs(tot_d - tot_s) <= max(1, 0.05 * tot_s), f"delta {delta} vs salvage {salvage}")
    check("no ghost waits for the ground any more", rc.get("deferred", 0) == 0 and rc.get("pending", 0) == 0, rc)
    ghosts = sum(1 for v in c3.values() if v.get("t") == pid)
    check("the plan's ghosts are placed (incl. deferred ones)", ghosts + int((info.get("result") or {}).get("existing") or 0)
          >= len(plan.entities) - int((info.get("result") or {}).get("blocked") or 0), f"{ghosts} of {len(plan.entities)}")

    # -- a ghost on the ground of a listed entity: 2.1 accepts a manual_ghost placement over an entity marked for
    # deconstruction (verified), so it is placed at once; where the engine refuses, it waits (deferred) until the
    # helper's dismantle frees the ground. Either way it must exist once the entity is gone. ------------------------
    hp = link.call("agent_status")["pos"]
    x, y = round(hp["x"]) + 6, round(hp["y"]) + 6
    lua(f"local s, f = game.surfaces.nauvis, game.forces.player "
        f"for _, e in pairs(s.find_entities_filtered{{area = {{{{{x - 2}, {y - 2}}}, {{{x + 3}, {y + 3}}}}}}}) do "
        f"if e.type ~= 'character' and e.type ~= 'resource' then e.destroy() end end "
        f"s.create_entity{{name = 'stone-furnace', position = {{{x + 1}, {y + 1}}}, force = f}} rcon.print('ok')")
    furn = [k for k, v in census().items() if v["n"] == "stone-furnace" and v["x"] == x + 1 and v["y"] == y + 1]
    did = "deferred-test"
    link.call("plan_preview", id=did, title="deferred test", entities=[["wooden-chest", x + 0.5, y + 0.5, 0]],
              removes=[["stone-furnace", x + 1, y + 1, furn[0] if furn else None]], show=False)
    r = link.call("plan_apply", id=did)
    g0 = sum(1 for v in census().values() if v.get("t") == did)
    check("a ghost on a listed entity's ground is placed or deferred",
          (r.get("deferred") == 1 and g0 == 0) or (r.get("placed") == 1 and g0 == 1), r)
    check("the listed entity under it is marked, not removed yet", furn and census()[furn[0]].get("m"))
    link.call("agent_tasks", tasks=[{"type": "dismantle", "plan": did}], replace=True)
    t_end = time.time() + 60
    while time.time() < t_end:
        st = link.call("agent_status")
        if not st.get("current") and not st.get("queue"):
            break
        time.sleep(0.3)
    time.sleep(0.5)
    g1 = sum(1 for v in census().values() if v.get("t") == did)
    rc = (link.call("plan_get", id=did).get("removes") or {})
    check("it is placed once the entity is gone", g1 == 1 and rc.get("deferred") == 0, rc)
    if not a.keep:
        restart(a.name, a.slot, stop=True)
    print(f"== {'OK' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
