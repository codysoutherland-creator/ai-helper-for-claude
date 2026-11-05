"""Live autopilot run (AUTOPILOT.md 11, Q1-Q4): a player-like test server (-NoDebugOps), a test companion with its own
config under the run folder, the autopilot switched on by the server console (the host), then NO other input.
Not collected by pytest.

    .venv\\Scripts\\python.exe tests\\live_autopilot.py --save seed_1001.zip --slot 33 --hours 1 [--player-lab]
        [--fake-claude] [--plans-from DIR] [--tag NAME] [--speed 10] [--out DIR]

Setup cheats (listed in the report): peaceful mode + enemies removed; --player-lab marks the red-science trigger
researched (a headless server has no player to craft the lab, open decision D3). Nothing else is scripted: the
companion's autopilot thread plans, the mod self-approves, the helper builds.
Every 5 game minutes: research, production, power, census, helper hand-craft counters, the autopilot's state and
Claude calls; a map render every 30 game minutes; at the end the player's baseline entities are checked.
Results: <out>/report.json, report.md, renders/*.png, autopilot.jsonl, companion.log (default
testenv/private/autopilot/runs/<tag>: gitignored, a real-save run holds the player's name).
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--save", required=True, help="save file in testenv/write (seed_1001.zip, a private copy, ...)")
ap.add_argument("--slot", type=int, default=33)
ap.add_argument("--tag", default=None)
ap.add_argument("--hours", type=float, default=1.0, help="game hours to run")
ap.add_argument("--speed", type=float, default=10)
ap.add_argument("--player-lab", action="store_true", help="setup: mark the red-science trigger researched (D3)")
ap.add_argument("--fake-claude", action="store_true", help="no real claude -p calls (default: real calls)")
ap.add_argument("--plans-from", default=None, help="copy *.json plans (masters) from here into the run's plans dir")
ap.add_argument("--out", default=None)
ap.add_argument("--calls-per-hour", type=int, default=6)
ap.add_argument("--plateau-min", type=float, default=45, help="stop when no new goal or tech for this long")
ap.add_argument("--render-every", type=float, default=30)
ap.add_argument("--uncharted", action="store_true", help="plan on uncharted chunks (fixture maps; default for seed_*)")
A = ap.parse_args()
assert 31 <= A.slot <= 39, "test slots 31-39 only"
assert A.speed <= 10, "game.speed <= 10"
TAG = A.tag or f"{Path(A.save).stem}_{time.strftime('%m%d_%H%M')}"
NAME = f"ap_{TAG}_{A.slot}"
OUT = Path(A.out) if A.out else ROOT / "testenv" / "private" / "autopilot" / "runs" / TAG
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "renders").mkdir(exist_ok=True)
INST = ROOT / "testenv" / "inst" / NAME
PLANS = OUT / "plans"
PLANS.mkdir(exist_ok=True)
UDP, RCON = 34500 + A.slot, 27016 + A.slot
# the harness's own link goes to the slot only (never the default ports / the player's config)
os.environ.update({"FACTORIO_UDP_PORT": str(UDP), "FACTORIO_RCON_PORT": str(RCON), "FACTORIO_RCON_PASSWORD": "testpw",
                   "FACTORIO_RCON_HOST": "127.0.0.1", "FACTORIO_UDP_TIMEOUT": "20", "FACTORIO_PLANS_DIR": str(PLANS),
                   "FACTORIO_CONFIG": str(OUT / "harness_config.json"),
                   "FACTORIO_SCRIPT_OUTPUT": str(INST / "write" / "script-output")})
(OUT / "harness_config.json").write_text(json.dumps({"udp_port": UDP, "rcon": True, "password": "testpw",
                                                      "host": "127.0.0.1", "port": RCON}), encoding="utf-8")
UNCHARTED = A.uncharted or Path(A.save).name.startswith("seed_")
if UNCHARTED:
    os.environ["FACTORIO_PLAN_UNCHARTED"] = "1"
sys.path.insert(0, str(ROOT))
from factorio_claude import link, rcon  # noqa: E402

REP = {"save": A.save, "slot": A.slot, "tag": TAG, "speed": A.speed, "hours": A.hours, "fake_claude": A.fake_claude,
       "setup_cheats": ["peaceful_mode = true and enemies removed (setup only)"], "samples": [], "renders": [],
       "log": [], "toggled_on_by": "server console (RCON /claude autopilot_set: the host's own machine)"}


def save():
    (OUT / "report.json").write_text(json.dumps(REP, indent=1, default=str), encoding="utf-8")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(time.strftime("%H:%M:%S"), s, flush=True)
    REP["log"].append(s)
    save()


def restart(stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", NAME, "-Slot", str(A.slot)]
    args += ["-Stop"] if stop else ["-Save", A.save, "-NoDebugOps"]
    lf = OUT / ("stop.txt" if stop else "restart.txt")
    with open(lf, "w") as fh:          # a file, not a pipe: the started server inherits the handle
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=300)
    return lf.read_text(errors="replace")


def connect(tries=90):
    rcon._client = rcon.Rcon("127.0.0.1", RCON, "testpw")
    for _ in range(tries):
        try:
            rcon.lua("rcon.print('ping')")
            rcon.lua("rcon.print('ping')")
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("server did not come up")


def lua(code):
    out = rcon.lua("local s, f = game.surfaces.nauvis, game.forces.player\n" + code)
    if "rror" in out[:300]:
        raise RuntimeError(out[:1500])
    return out.strip().splitlines()[-1] if out.strip() else ""


def jlua(code):
    return json.loads(lua(code))


def tick():
    return int(lua("rcon.print(game.tick)"))


def bridge(op, **args):
    """An op as the server console: /claude <json> over RCON (the console counts as the host)."""
    return rcon.bridge(op, **args)


COMP = []


def stop_companion():
    for p in COMP:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()


def start_companion():
    work = OUT / "companion"
    work.mkdir(exist_ok=True)
    cfg = {"udp_port": UDP, "rcon": False, "password": "", "chat_model": "sonnet",
           "autopilot_calls_per_hour": A.calls_per_hour, "max_requests_per_session": 200}
    (work / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FACTORIO_")}
    env.update({"FACTORIO_CONFIG": str(work / "config.json"), "FACTORIO_UDP_PORT": str(UDP),
                "FACTORIO_UDP_TIMEOUT": "20",
                "FACTORIO_COMPANION_LOCK_PORT": str(34600 + A.slot), "FACTORIO_COMPANION_LOG": str(OUT / "companion.log"),
                "FACTORIO_CHAT_DIR": str(work / "chat"), "FACTORIO_PLANS_DIR": str(PLANS),
                "FACTORIO_SCRIPT_OUTPUT": str(INST / "write" / "script-output"),
                "FACTORIO_AUTOPILOT_JOURNAL": str(OUT / "autopilot.jsonl"),
                "FACTORIO_GAME_MATCH": f"testenv/inst/{NAME}/"})
    if UNCHARTED:
        env["FACTORIO_PLAN_UNCHARTED"] = "1"
    if A.fake_claude:
        env["FACTORIO_FAKE_CLAUDE"] = "1"
    py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fh = open(OUT / "companion.out", "w")
    p = subprocess.Popen([str(py), "-m", "factorio_claude.companion", "--no-exit-check"], cwd=ROOT, env=env,
                         stdout=fh, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    COMP.append(p)
    return p


SAMPLE = """
local is = f.get_item_production_statistics(s)
local r = {tick = game.tick, made = {}, research = f.current_research and f.current_research.name or "",
  done = {}, census = {}}
for _, n in pairs({'iron-plate', 'copper-plate', 'steel-plate', 'iron-gear-wheel', 'electronic-circuit',
    'automation-science-pack', 'logistic-science-pack', 'stone', 'coal', 'iron-ore', 'copper-ore'}) do
  r.made[n] = is.get_input_count(n)
end
for name, t in pairs(f.technologies) do if t.researched then r.done[#r.done + 1] = name end end
for _, e in pairs(s.find_entities_filtered{force = f}) do
  if e.type ~= 'entity-ghost' then r.census[e.name] = (r.census[e.name] or 0) + 1 end
end
local hand = 0
for _, p in pairs(game.players) do if p.character then hand = hand + 1 end end
r.players_with_character = hand
rcon.print(helpers.table_to_json(r))"""


def sample(prev):
    s = jlua(SAMPLE)
    try:
        apg = link.call("autopilot_get", stock=True)
    except link.LinkError as e:
        apg = {"error": str(e)}
    try:
        obs = link.call("autopilot_observe")
        power = obs.get("power")
        plans = [{k: p.get(k) for k in ("id", "status", "owner", "ghosts", "built", "queued", "blocked")}
                 for p in (obs.get("plans") or [])]
        helper = obs.get("helper")
        made_rate = obs.get("made")
    except link.LinkError as e:
        power, plans, helper, made_rate = {"error": str(e)}, [], {}, {}
    row = {"game_min": round(s["tick"] / 3600, 1), "research": s["research"], "techs_done": len(s["done"]),
           "made_total": s["made"], "rate_10m": made_rate, "power": power, "census": s["census"],
           "entities": sum(s["census"].values()), "plans": plans,
           "helper": {k: (helper or {}).get(k) for k in ("current", "queue", "halted", "counters")},
           "autopilot": {k: apg.get(k) for k in ("on", "phase", "goal", "step", "next_step", "calls", "halted",
                                                  "owned", "baseline", "waiting_for", "queue", "mall_chests", "mall_stocked",
                                                  "mall_stock", "held")}}
    if prev:
        dt = (s["tick"] - prev["tick"]) / 3600
        row["per_min"] = {n: round((s["made"][n] - prev["made"][n]) / dt, 1) for n in s["made"]} if dt > 0 else {}
        row["new_techs"] = sorted(set(s["done"]) - set(prev["done"]))
    return s, row


def render(label):
    try:
        from factorio_claude.planner.world import World
        spec = importlib.util.spec_from_file_location("render_plan", ROOT / "testenv" / "render_plan.py")
        rp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp)
        bb = jlua("local x0, y0, x1, y1 = 1e9, 1e9, -1e9, -1e9 for _, e in pairs(s.find_entities_filtered{force = f}) "
                  "do local p = e.position if e.type ~= 'character' then x0 = math.min(x0, p.x) y0 = math.min(y0, p.y) "
                  "x1 = math.max(x1, p.x) y1 = math.max(y1, p.y) end end "
                  "if x0 > x1 then x0, y0, x1, y1 = -40, -40, 40, 40 end "
                  "rcon.print(helpers.table_to_json({x0, y0, x1, y1}))")
        pad = 24
        area = (int(bb[0]) - pad, int(bb[1]) - pad, int(bb[2]) + pad, int(bb[3]) + pad)
        w = World.from_link(area=area, link=link.call, charted=False if UNCHARTED else None)
        out = OUT / "renders" / f"{label}.png"
        rp.render(w, str(out), scale=3 if (area[2] - area[0]) < 500 else 2, ghosts=True, mute_existing=False)
        REP["renders"].append({"label": label, "file": str(out.relative_to(OUT)), "area": area})
        log("render", out.name, area)
    except Exception as e:  # noqa: BLE001
        log("render failed", repr(e)[:300])


def journal_summary():
    from factorio_claude import autopilot as AP
    rep = AP.report(OUT / "autopilot.jsonl", hours=1000)
    rows = AP.read_journal(OUT / "autopilot.jsonl")
    calls = [r for r in rows if r.get("kind") == "decision"]
    return rep, rows, calls


def main():
    log("restart", restart()[-200:])
    connect()
    lua("game.speed = 1 s.peaceful_mode = true for _, e in pairs(s.find_entities_filtered{force = 'enemy'}) do "
        "e.destroy() end rcon.print(1)")
    if A.player_lab:
        lua("f.technologies['automation-science-pack'].researched = true rcon.print(1)")
        REP["setup_cheats"].append("automation-science-pack trigger set researched (D3: no player to craft a lab)")
    if A.plans_from:
        n = 0
        for p in Path(A.plans_from).glob("*.json"):
            shutil.copy2(p, PLANS / p.name)
            n += 1
        REP["plans_copied"] = n
    t0 = tick()
    s0, row = sample(None)
    REP["start"] = row
    REP["start_research"] = s0["done"]
    render("start")
    comp = start_companion()
    REP["companion_pid"] = comp.pid
    time.sleep(4)
    r = bridge("autopilot_set", on=True)
    log("autopilot on:", r)
    lua(f"game.speed = {A.speed} rcon.print(1)")
    end = t0 + int(A.hours * 216000)
    prev, last_sample, last_render = s0, t0, t0
    last_goal_change = t0
    last_state = None
    while True:
        time.sleep(3)
        try:
            now = tick()
        except Exception as e:  # noqa: BLE001
            log("tick failed", repr(e))
            connect()
            continue
        if comp.poll() is not None:
            log("companion exited", comp.returncode)
            break
        if now - last_sample >= 5 * 3600:
            prev, row = sample(prev)
            REP["samples"].append(row)
            last_sample = now
            # a plateau: no new goal, no new tech and no new entity for --plateau-min game minutes
            st = (row["autopilot"].get("goal"), row["techs_done"], row["entities"])
            if st != last_state:
                last_state, last_goal_change = st, now
            log(f"t={row['game_min']}min goal={row['autopilot'].get('goal')} step={row['autopilot'].get('next_step')}"
                f" techs={row['techs_done']} ents={row['entities']} calls={row['autopilot'].get('calls')}"
                f" helper={row['helper'].get('current')} per_min={ {k: v for k, v in (row.get('per_min') or {}).items() if v} }")
            if row["autopilot"].get("halted"):
                log("HALTED", row["autopilot"]["halted"])
                break
        if now - last_render >= A.render_every * 3600:
            lua("game.speed = 1 rcon.print(1)")
            render(f"t{int((now - t0) / 3600):04d}")
            lua(f"game.speed = {A.speed} rcon.print(1)")
            last_render = now
        if now >= end:
            log("time is up")
            break
        if now - last_goal_change >= A.plateau_min * 3600 and now - t0 > 60 * 3600:
            log("plateau: no new goal or tech for", A.plateau_min, "game minutes")
            REP["plateau_at_min"] = round((now - t0) / 3600, 1)
            break
    lua("game.speed = 1 rcon.print(1)")
    prev, row = sample(prev)
    REP["end"] = row
    render("end")
    REP["baseline_check"] = bridge("autopilot_baseline_check")
    r = bridge("autopilot_set", on=False)
    REP["off"] = {"result": r, "after": link.call("autopilot_get")}
    time.sleep(3)
    rep, rows, calls = journal_summary()
    REP["journal"] = rep
    REP["claude_calls"] = sum(1 for c in calls if c.get("source") == "claude")
    REP["decisions_by_source"] = {}
    for c in calls:
        REP["decisions_by_source"][c.get("source")] = REP["decisions_by_source"].get(c.get("source"), 0) + 1
    REP["game_minutes"] = round((tick() - t0) / 3600, 1)
    REP["new_techs"] = sorted(set(prev["done"]) - set(s0["done"]))
    stop_companion()
    save()
    write_md()


def write_md():
    lines = [f"# Autopilot run {TAG}", "", f"- save: {Path(A.save).name}, slot {A.slot}, speed {A.speed}",
             f"- game minutes: {REP.get('game_minutes')}", f"- setup cheats: {'; '.join(REP['setup_cheats'])}",
             f"- Claude calls: {REP.get('claude_calls')} ({REP.get('decisions_by_source')})",
             f"- new techs: {', '.join(REP.get('new_techs') or [])}",
             f"- baseline: {REP.get('baseline_check')}", "",
             "| game min | goal | next step | techs | entities | iron/min | copper/min | red/min | green/min | "
             "hand share | calls |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in REP["samples"]:
        pm = s.get("per_min") or {}
        a = s["autopilot"]
        h = (s["helper"] or {}).get("counters") or {}
        lines.append(f"| {s['game_min']} | {a.get('goal')} | {a.get('next_step')} | {s['techs_done']} | "
                     f"{s['entities']} | {pm.get('iron-plate', 0)} | {pm.get('copper-plate', 0)} | "
                     f"{pm.get('automation-science-pack', 0)} | {pm.get('logistic-science-pack', 0)} | "
                     f"{h.get('share_1h')} | {a.get('calls')} |")
    lines += ["", "Renders: " + ", ".join(r["file"] for r in REP["renders"])]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        REP["error"] = traceback.format_exc()
        save()
        print(REP["error"])
    finally:
        stop_companion()
        print(restart(stop=True))
