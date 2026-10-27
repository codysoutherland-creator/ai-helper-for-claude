"""Live checks for the v3 review fixes on a headless test slot (not collected by pytest):
    .venv\\Scripts\\python.exe tests\\live_review_fixes.py [--slot 11] [--name fix] [--no-restart]
plan_apply survives entities that throw, never orders the player's own entities/ghosts deconstructed, approving
supersedes overlapping proposals, a failed draw/apply job resets the plan; the helper mines trees under ghosts, stops
with a message on ghosts it can't place, and refuels hand-fed burners (gathering coal first when it has none)."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ap = argparse.ArgumentParser()
ap.add_argument("--slot", type=int, default=11)
ap.add_argument("--name", default="fix")
ap.add_argument("--no-restart", action="store_true")
A = ap.parse_args()
os.environ.update(FACTORIO_UDP_PORT=str(34500 + A.slot), FACTORIO_RCON_PORT=str(27016 + A.slot),
                  FACTORIO_RCON_PASSWORD="testpw", FACTORIO_UDP_TIMEOUT="20")
from factorio_claude import link, rcon  # noqa: E402

rcon._client = rcon.Rcon("127.0.0.1", 27016 + A.slot, "testpw")
RESULTS = []


def check(name, cond, info=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)


def lua(code):
    r = rcon._client.command("/silent-command " + code)
    return (r or "").strip()


def mod(code):
    """run in the mod's Lua state (its storage)"""
    return lua("__claude-bridge__ " + code)


def jl(code):
    out = lua(code)
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError(f"lua: {out[:400]}")


def call(op, **args):
    return link.call(op, **args)


def run_ticks(n, speed=10):
    lua(f"game.speed = {speed}")
    t = int(lua("rcon.print(game.tick)"))
    while int(lua("rcon.print(game.tick)")) < t + n:
        time.sleep(0.3)
    lua("game.speed = 1")


def restart():
    with tempfile.TemporaryFile() as f:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "testenv" / "restart.ps1"), "-Name", A.name, "-Slot", str(A.slot),
                        "-Save", "seed_1001.zip"], stdout=f, stderr=f, timeout=120)
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + A.slot, "testpw")
    for _ in range(40):
        try:
            lua("rcon.print(1)")
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)


def free_spot(x, y, w=6, h=6):
    """clear a w x h area at (x, y) of trees/rocks/decoratives (test cheat)"""
    lua(f"local s=game.surfaces.nauvis for _, e in pairs(s.find_entities_filtered{{area={{{{{x},{y}}},{{{x + w},{y + h}}}}},"
        f"type={{'tree','simple-entity','item-entity'}}}}) do e.destroy() end")


def test_plan_apply():
    lua("game.surfaces.nauvis.peaceful_mode = true")
    free_spot(40, 40, 12, 8)
    # entity rows that can never be ghosts are dropped at preview
    r = call("plan_preview", id="rv1", title="rv1", entities=[["stone-furnace", 41, 41, 0], ["iron-ore", 45.5, 41.5, 0]],
             show=False)
    check("preview drops a resource row", r["entities"] == 1 and r["bad_rows"] == 1, r)
    # a row that throws inside create_entity (injected past the preview filter) only counts as blocked
    mod("storage.plans['rv1'].entities[2] = {'iron-ore', 45.5, 41.5, 0}")
    r = call("plan_apply", id="rv1")
    g = call("plan_get", id="rv1")
    check("apply survives a throwing entity", g["status"] == "applied" and r["placed"] == 1 and r["blocked"] >= 1,
          json.dumps(r)[:300])
    # a failed tick job resets the plan instead of leaving it stuck
    big = [["transport-belt", 60.5 + i % 40, 60.5 + i // 40, 4] for i in range(1600)]
    call("plan_preview", id="rv2", title="rv2", entities=big, show=False)
    mod("storage.plans['rv2'].entities[1600] = {'tree-01', 99.5, 99.5, 0}")
    r = call("plan_apply", id="rv2")
    time.sleep(2)
    g = call("plan_get", id="rv2")
    check("big apply with a bad row finishes", g["status"] == "applied" and not (g.get("result") or {}).get("pending"),
          json.dumps(g.get("result"))[:200])
    mod("local p = storage.plans['rv2'] p.draw = {i = 1} p.objs = {} p.shown = true storage.plan_jobs = {rv2 = true}")
    time.sleep(1)
    g = call("plan_get", id="rv2")
    check("failed draw job resets drawing/shown", not g["drawing"] and not g["shown"], json.dumps(g)[:200])
    call("clear_claude_ghosts", plan="rv2")


def test_deconstruct_filter():
    free_spot(80, 40, 10, 6)
    lua("local s=game.surfaces.nauvis s.create_entity{name='wooden-chest', position={81.5,41.5}, force='player'} "
        "s.create_entity{name='entity-ghost', inner_name='wooden-chest', position={83.5,41.5}, force='player'} "
        "s.create_entity{name='tree-01', position={85.5,41.5}}")
    call("plan_preview", id="rvd", title="rvd", entities=[],
         deconstruct=[[81.5, 41.5], [83.5, 41.5], [81.5, 41.5, "wooden-chest"], [85.5, 41.5, "tree-01"]], show=False)
    r = call("plan_apply", id="rvd")
    st = jl("local s=game.surfaces.nauvis local c=s.find_entity('wooden-chest',{81.5,41.5}) "
            "local g=s.find_entities_filtered{type='entity-ghost', position={83.5,41.5}, radius=0.5}[1] "
            "local t=s.find_entities_filtered{type='tree', position={85.5,41.5}, radius=0.6}[1] "
            "rcon.print(helpers.table_to_json{chest=c and c.to_be_deconstructed() or false, ghost=g ~= nil, "
            "tree=t and t.to_be_deconstructed() or false})")
    check("deconstruct never touches player entities/ghosts", st == {"chest": False, "ghost": True, "tree": True},
          f"{st} apply={r.get('deconstruct')}")


def test_supersede():
    free_spot(100, 40, 10, 10)
    ents = [["wooden-chest", 101.5 + i, 41.5, 0] for i in range(4)]
    call("plan_preview", id="va", title="Variant A", group="g", entities=ents, show=False)
    call("plan_preview", id="vb", title="Variant B", group="g", entities=ents[:2], show=False)
    call("plan_preview", id="vc", title="Elsewhere", group="g", entities=[["wooden-chest", 150.5, 150.5, 0]], show=False)
    call("plan_apply", id="va")
    lst = {p["id"]: p for p in call("plan_list")}
    check("approving supersedes the overlapping proposal only",
          lst["vb"].get("superseded") == "va" and not lst["vc"].get("superseded"), {k: v.get("superseded") for k, v in lst.items()})


def helper_log():
    return [x["msg"] for x in (call("agent_status").get("log") or [])]


def test_helper_trees_and_blocked():
    call("agent_remove", drop_items=False)
    # a ghost on a tree: the helper chops the tree, then builds
    free_spot(120, 40, 12, 12)
    lua("local s=game.surfaces.nauvis s.create_entity{name='tree-01', position={121.5,41.5}} "
        "s.create_entity{name='entity-ghost', inner_name='wooden-chest', position={121.5,41.5}, force='player'}")
    call("agent_spawn")
    mod("storage.agent.entity.teleport({126, 46}) storage.agent.entity.insert{name='wooden-chest', count=5}")
    call("agent_tasks", tasks=[{"type": "build_ghosts", "x": 121.5, "y": 41.5, "radius": 3}], replace=True)
    run_ticks(1500)
    st = jl("local s=game.surfaces.nauvis rcon.print(helpers.table_to_json{chest=s.find_entity('wooden-chest',{121.5,41.5}) ~= nil, "
            "ghost=#s.find_entities_filtered{type='entity-ghost', position={121.5,41.5}, radius=0.5}, "
            "tree=#s.find_entities_filtered{type='tree', position={121.5,41.5}, radius=0.9}})")
    check("helper chops the tree under a ghost and builds it", st == {"chest": True, "ghost": 0, "tree": 0},
          f"{st} log={helper_log()[-4:]}")
    # a ghost blocked by something it can't remove: stops with a message instead of looping
    lua("local s=game.surfaces.nauvis s.create_entity{name='wooden-chest', position={126.5,42.5}, force='neutral'} "
        "s.create_entity{name='entity-ghost', inner_name='stone-furnace', position={126,42}, force='player'}")
    n = int(lua("rcon.print(#game.surfaces.nauvis.find_entities_filtered{type='entity-ghost', position={126,42}, radius=0.5})"))
    check("blocked test ghost exists", n == 1, n)
    mod("storage.agent.entity.insert{name='stone-furnace', count=2}")
    call("agent_tasks", tasks=[{"type": "build_ghosts", "x": 126, "y": 42, "radius": 2}], replace=True)
    run_ticks(1800)
    s = call("agent_status")
    log = helper_log()
    check("blocked ghost: helper stops with a message", not s.get("current") and not s.get("queue")
          and any("can't place" in m for m in log), f"cur={s.get('current')} q={s.get('queue')} log={log[-3:]}")


def test_refuel_fetch():
    # a burner drill on iron with no fuel; the helper has no coal: it mines some, then refuels to 50
    pos = jl("local s=game.surfaces.nauvis local o=s.find_entities_filtered{name='iron-ore', position={0,0}, radius=150, limit=400} "
             "for _, r in pairs(o) do local p={math.floor(r.position.x)+1, math.floor(r.position.y)+1} "
             "if s.can_place_entity{name='burner-mining-drill', position=p, force='player'} then "
             "s.create_entity{name='burner-mining-drill', position=p, force='player'} rcon.print(helpers.table_to_json(p)) return end end "
             "rcon.print('null')")
    check("test drill placed", pos is not None, pos)
    if pos is None:
        return
    call("agent_remove", drop_items=False)
    call("agent_spawn")
    mod(f"storage.agent.entity.teleport({{{pos[0] + 4}, {pos[1]}}})")
    call("agent_tasks", tasks=[{"type": "refuel", "x": pos[0], "y": pos[1], "radius": 5, "per": 20,
                                "at": [[pos[0], pos[1]]]}], replace=True)
    for _ in range(12):
        run_ticks(3600, speed=20)
        n = int(lua(f"local d=game.surfaces.nauvis.find_entity('burner-mining-drill',{{{pos[0]},{pos[1]}}}) "
                    "rcon.print(d.get_fuel_inventory().get_item_count('coal') + (d.burner.currently_burning and 1 or 0))"))
        if n >= 15:
            break
    check("refuel gathers coal first, then fills the drill", n >= 15, f"coal={n} log={helper_log()[-4:]}")


def test_plan_helper_op():
    call("agent_remove", drop_items=False)
    free_spot(140, 40, 6, 6)
    call("plan_preview", id="ph", title="Helper plan", entities=[["burner-mining-drill", 141, 41, 0, None, {"fuel": "manual"}]],
         show=False)
    r = call("plan_helper", id="ph", what="refuel")
    time.sleep(0.5)
    s = call("agent_status")
    check("plan_helper spawns the helper and runs a refuel of the plan's hand-fed burners",
          s.get("alive") and any("refuel" in m for m in helper_log()), f"{r} {helper_log()[-3:]}")


def test_work_area_replaces_stale_queue():
    # a halted helper with leftovers: plan_helper build replaces the stale queue
    call("agent_remove", drop_items=False)
    call("agent_spawn")
    mod("storage.agent.halted = 'failed mine: test' storage.agent.queue = {{type = 'wait', ticks = 99999}}")
    call("plan_helper", id="ph", what="build")
    s = call("agent_status")
    q = [(s.get("current") or {}).get("type")] + list(s.get("queue") or [])
    time.sleep(0.5)
    log = helper_log()
    check("plan_helper on a halted helper replaces its stale queue", "wait" not in q and not s.get("halted")
          and ("build_ghosts" in q or any("build_ghosts" in m or "no ghosts" in m for m in log)), f"{q} {log[-3:]}")


def main():
    if not A.no_restart:
        restart()
    lua("rcon.print(1)")
    for t in (test_plan_apply, test_deconstruct_filter, test_supersede, test_helper_trees_and_blocked, test_refuel_fetch,
              test_plan_helper_op, test_work_area_replaces_stale_queue):
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, repr(e))
    log = (ROOT / "testenv" / "inst" / A.name / "server.log").read_text(encoding="utf-8", errors="replace")
    errs = [ln for ln in log.splitlines() if "Error" in ln and "claude" in ln.lower()]
    check("no script errors in server.log", not errs, errs[:3])
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed" + (f"; FAILED: {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


main()
