"""Planner v3 stage by stage with the REAL helper on a headless test slot (B2-B5 of docs/design/PLANNER-v3.md; not
collected by pytest). Player-like server (-NoDebugOps); the only test setup is peaceful mode.

    .venv\\Scripts\\python.exe tests\\live_stages.py --save seed_2002.zip [--slot 32] [--tag mytest] [--to 5]

Flow (the shipped tool path): plan_starter_base -> per stage: the mod's own "stage ready" offer is recorded ->
plan_next_stage -> plan_apply (MCP) -> plan_helper build -> wait until the stage's ghosts and removals are done (the
helper then runs its coal round for a few minutes, as in a game) -> measured minutes vs the offer's "ready in".
Research: Automation from 10 packs the HELPER crafts from plates it takes out of the buffer chests (as a player
would), queued with queue_research (the research screen); every later tech from packs the S3 assemblers make.
B3: after S2 and after S3 the helper is stopped for 30 game minutes; plate / pack rates, fuel and power at the
start, half and end. B4: entity census by unit_number before and after S4. B5: green packs/min after S5.
Results: --out (default testenv/inst/live_stages_<tag>.json); plans in testenv/inst/live_stages_<tag>_plans.
Saves of a real game belong in testenv/private (gitignored): pass --out there too.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--save", required=True, help="save file in testenv/write")
ap.add_argument("--slot", type=int, default=32)
ap.add_argument("--tag", default="stages")
ap.add_argument("--to", type=float, default=5, help="last stage built (3 = red science, 5 = green)")
ap.add_argument("--out", default=None)
ap.add_argument("--speed", type=float, default=10)
ap.add_argument("--variant", default=None, help="hub | onsite (default: the recommended master)")
ap.add_argument("--allow-removal", action="store_true",
                help="plan_starter_base(allow_removal=true): the player said yes to taking idle builds apart")
ap.add_argument("--idle-min", type=float, default=30, help="game minutes the helper stays idle after S2, S3, S5")
ap.add_argument("--player-lab", action="store_true",
                help="test setup on a fresh map: mark the red-science trigger tech done (in a game the PLAYER crafts "
                     "one lab; a headless server has no player)")
A = ap.parse_args()
assert 31 <= A.slot <= 39, "test slots 31-39 only"
assert A.speed <= 10, "game.speed <= 10"
SAVE, TAG, SLOT, TO, SPEED = A.save, A.tag, A.slot, A.to, A.speed
NAME = f"stages_{TAG}_{SLOT}"
PLANS = ROOT / "testenv" / "inst" / f"live_stages_{TAG}_plans"
PLANS.mkdir(parents=True, exist_ok=True)
os.environ.update({"FACTORIO_UDP_PORT": str(34500 + SLOT), "FACTORIO_RCON_PORT": str(27016 + SLOT),
                   "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1", "FACTORIO_UDP_TIMEOUT": "20",
                   "FACTORIO_PLANS_DIR": str(PLANS),
                   "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / NAME / "write" / "script-output")})
if SAVE.startswith("seed_"):     # fixture maps on a headless server: nothing is charted, export what is generated
    os.environ["FACTORIO_PLAN_UNCHARTED"] = "1"
sys.path.insert(0, str(ROOT))
from factorio_claude import link, rcon  # noqa: E402
from factorio_claude import server as S  # noqa: E402
from factorio_claude.planner import master as M  # noqa: E402

OUT = Path(A.out) if A.out else ROOT / "testenv" / "inst" / f"live_stages_{TAG}.json"
REP = {"save": SAVE, "slot": SLOT, "setup_cheats": ["peaceful_mode = true + enemies removed (test setup only)"],
       "stages": {}, "log": []}


class env:  # noqa: N801 - the slot helpers
    @staticmethod
    def restart(slot, name, save=None, stop=False):
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "testenv" / "restart.ps1"), "-Name", name, "-Slot", str(slot)]
        args += ["-Stop"] if stop else ["-Save", save, "-NoDebugOps"]
        lf = ROOT / "testenv" / "inst" / f"{name}_restart.txt"
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, "w") as fh:   # a file, not a pipe: the started server inherits the handle
            subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=300)
        return lf.read_text(errors="replace")

    @staticmethod
    def connect(slot, tries=90):
        rcon._client = rcon.Rcon("127.0.0.1", 27016 + slot, "testpw")
        for _ in range(tries):
            try:
                rcon.lua("rcon.print('ping')")
                rcon.lua("rcon.print('ping')")
                return
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise RuntimeError("server did not come up")


def lua(code: str) -> str:
    out = rcon.lua("local s, f = game.surfaces.nauvis, game.forces.player\n" + code)
    if "rror" in out[:300]:
        raise RuntimeError(out[:2000])
    return out.strip().splitlines()[-1] if out.strip() else ""




def save():
    OUT.write_text(json.dumps(REP, indent=1, default=str), encoding="utf-8")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(time.strftime("%H:%M:%S"), s, flush=True)
    REP["log"].append(s)
    save()


def jlua(code):
    return json.loads(lua(code))


def tick():
    return int(lua("rcon.print(game.tick)"))


def speed(v):
    lua(f"game.speed = {v} rcon.print(1)")


def wait_game(minutes, poll=2.0, cond=None):
    t1 = tick() + int(minutes * 3600)
    while tick() < t1:
        if cond and cond():
            return True
        time.sleep(poll)
    return False


PROGRESS = """
local id = %s
local g = 0
for _, e in pairs(s.find_entities_filtered{type = 'entity-ghost'}) do
  local ok, t = pcall(function() return e.tags end)
  if ok and t and t.claude_plan == id then g = g + 1 end
end
rcon.print(helpers.table_to_json{tick = game.tick, ghosts = g})"""

SAMPLE = """
local ST = {}
for k, v in pairs(defines.entity_status) do ST[v] = k end
local is = f.get_item_production_statistics(s)
local r = {tick = game.tick, made = {}, nofuel = {}, boilers_low = 0, status = {}, lowpower = 0, nopower = 0,
  research = f.current_research and f.current_research.name or "", progress = f.research_progress}
for _, n in pairs({'iron-plate', 'copper-plate', 'automation-science-pack', 'logistic-science-pack',
    'iron-gear-wheel', 'coal', 'electronic-circuit'}) do
  r.made[n] = is.get_input_count(n)
end
local B = helpers.json_to_table([==[%s]==])
for _, q in pairs(B) do
  local e = s.find_entities_filtered{name = q[1], position = {q[2], q[3]}, force = f}[1]
  if e then
    local st = ST[e.status] or '?'
    local k = e.name .. ':' .. st
    r.status[k] = (r.status[k] or 0) + 1
    if st == 'low_power' then r.lowpower = r.lowpower + 1 end
    if st == 'no_power' then r.nopower = r.nopower + 1 end
    if e.burner and not e.burner.currently_burning and e.burner.inventory.is_empty() then
      r.nofuel[#r.nofuel + 1] = e.name .. '@' .. e.position.x .. ',' .. e.position.y
    end
    if e.type == 'boiler' and e.get_fuel_inventory().get_item_count('coal') < 5 then r.boilers_low = r.boilers_low + 1 end
  end
end
local gen, gmax = 0, 0
for _, e in pairs(s.find_entities_filtered{type = 'generator', force = f}) do
  gen = gen + e.energy_generated_last_tick * 60 / 1000
  gmax = gmax + e.prototype.get_max_energy_production() * 60 / 1000
end
r.gen_kw, r.gen_max_kw = gen, gmax
rcon.print(helpers.table_to_json(r))"""


def sample(mp, k):
    rows = [[e.name, e.x, e.y] for e in mp.ents if M.alive(e, k) and e.name in (
        "stone-furnace", "assembling-machine-1", "lab", "boiler", "burner-mining-drill", "electric-mining-drill",
        "burner-inserter", "inserter", "steam-engine")]
    return jlua(SAMPLE % json.dumps(rows))


def agent_log(n=3):
    try:
        st = link.call("agent_status") or {}
        return [f"{x.get('tick')} {x.get('msg')}" for x in (st.get("log") or [])][-n:], st.get("desc")
    except Exception as e:
        return [repr(e)], None


def mod_status(plan_id):
    try:
        rows = link.call("plan_list") or []
    except Exception as e:
        return {"error": repr(e)}
    r = next((x for x in rows if x.get("id") == plan_id), None)
    return {"status": r.get("status"), "ready_tick": r.get("ready_tick"), "future": bool(r.get("future"))} if r else None


def build_stage(mp, plan_id, label, pred):
    t0 = tick()
    raw = S.plan_apply(plan_id)
    try:
        r = json.loads(raw)
    except Exception:
        r = {"raw": raw}
    refused = not (isinstance(r, dict) and r.get("applied"))
    if refused:
        log(label, "MCP plan_apply REFUSED", str(r)[:1500])
        REP.setdefault("mcp_apply_refused", {})[label] = r
        return {"refused": True}
    for _ in range(120):
        g = link.call("plan_get", id=plan_id) or {}
        rr = g.get("result") or {}
        if not rr.get("pending"):
            break
        time.sleep(1)
    log(label, "applied", {k: rr.get(k) for k in ("placed", "existing", "blocked", "deconstruct", "deferred")},
        "removes", g.get("removes"))
    speed(SPEED)
    h = link.call("plan_helper", id=plan_id, what="build")
    log(label, "helper:", (h or {}).get("msg"))
    limit = t0 + int(max(3 * (pred or 10), 30) * 3600)
    last = 0
    p = {}
    while True:
        p = jlua(PROGRESS % json.dumps(plan_id))
        g = link.call("plan_get", id=plan_id) or {}
        pend = ((g.get("removes") or {}).get("pending") or 0) + ((g.get("removes") or {}).get("deferred") or 0)
        if p["ghosts"] == 0 and pend == 0 and p["tick"] > t0 + 300:
            break
        if p["tick"] > limit:
            break
        if time.time() - last > 45:
            lg, desc = agent_log(2)
            log(label, "progress", round((p["tick"] - t0) / 3600, 1), "min, ghosts", p["ghosts"], "removals", pend,
                desc, lg)
            last = time.time()
        time.sleep(2)
    meas = round((p["tick"] - t0) / 3600, 2)
    done = p["ghosts"] == 0
    if not done:     # what is left and what the helper says (diagnosis of a stalled stage)
        try:
            REP.setdefault("stalled", {})[label] = {
                "ghosts": jlua("local id = %s local o = {} for _, e in pairs(s.find_entities_filtered{type = "
                               "'entity-ghost'}) do local ok, t = pcall(function() return e.tags end) if ok and t and "
                               "t.claude_plan == id then o[#o + 1] = {e.ghost_name, e.position.x, e.position.y} end "
                               "end rcon.print(helpers.table_to_json(o))" % json.dumps(plan_id)),
                "agent": link.call("agent_status")}
        except Exception as ex:  # noqa: BLE001
            REP.setdefault("stalled", {})[label] = {"error": repr(ex)}
    # the helper finishes the way it does in a game: its coal round runs (it primes the coal fields, tops up the
    # hand-fed burners); then the plates of the base so far are sampled for 2 minutes (each line must run)
    wait_game(ROUND_MIN)
    a = sample(mp, k_of(label))
    wait_game(2)
    b = sample(mp, k_of(label))
    rates = {n: round((b["made"][n] - a["made"][n]) / ((b["tick"] - a["tick"]) / 3600), 1)
             for n in ("iron-plate", "copper-plate", "automation-science-pack", "logistic-science-pack")}
    speed(1)
    res = {"plan": plan_id, "predicted_min": pred, "measured_min": meas, "complete": done,
           "rates_after_build": rates, "plan_rates": M.stage_rates(mp, MP_GD[0], k_of(label)) if MP_GD[0] else None,
           "nofuel_after_build": b["nofuel"],
           "ratio": round(meas / pred, 2) if pred else None,
           "within_50pct": bool(done and pred and abs(meas - pred) <= 0.5 * pred), "agent_log": agent_log(4)[0],
           "apply": {k: rr.get(k) for k in ("placed", "existing", "blocked", "deconstruct", "deferred")}}
    REP["stages"][label] = res
    log(label, "RESULT", json.dumps({k: res[k] for k in ("predicted_min", "measured_min", "complete", "ratio",
                                                         "within_50pct")}))
    return res


KEEP_RESEARCHING = """
-- what a player does with the research screen open: when nothing is being researched, queue an available tech that
-- only needs the packs the base makes (shortest unit time first; never completes anything by itself)
local packs = helpers.json_to_table([==[%s]==])
local ok = {}
for _, p in pairs(packs) do ok[p] = true end
if f.current_research then rcon.print(helpers.table_to_json{queued = ''}) return end
local cands = {}
for name, t in pairs(f.technologies) do
  -- unit techs only (a trigger tech, e.g. steel-axe in 2.x, cannot be queued for labs)
  if t.enabled and not t.researched and not t.prototype.research_trigger and t.research_unit_energy > 0 then
    local ing = #t.research_unit_ingredients > 0
    for _, q in pairs(t.prerequisites) do if not q.researched then ing = false break end end
    for _, i in pairs(t.research_unit_ingredients) do if not ok[i.name] then ing = false break end end
    if ing then cands[#cands + 1] = t end
  end
end
table.sort(cands, function(a, b)
  if a.research_unit_energy ~= b.research_unit_energy then return a.research_unit_energy < b.research_unit_energy end
  return a.name < b.name
end)
for _, t in ipairs(cands) do
  if f.add_research(t.name) then
    rcon.print(helpers.table_to_json{queued = t.name, unit_s = t.research_unit_energy / 60, count = t.research_unit_count})
    return
  end
end
rcon.print(helpers.table_to_json{queued = 'none'})"""


def keep_researching(k):
    packs = ["automation-science-pack"] + (["logistic-science-pack"] if k >= 5 else [])
    try:
        r = jlua(KEEP_RESEARCHING % json.dumps(packs))
    except Exception as ex:  # noqa: BLE001
        return {"error": repr(ex)}
    if r.get("queued"):
        REP.setdefault("research_queued", []).append(r)
    return r


def idle_run(mp, k, label, minutes=30):
    link.call("agent_stop")
    speed(SPEED)
    if k >= 3:
        keep_researching(k)
    s0 = sample(mp, k)
    windows, prev = [], s0
    step = 3.0                       # rate per 3-minute window: shows when research (the packs' consumer) stops
    t_end = minutes
    t = 0.0
    s1 = None
    while t < t_end - 1e-9:
        if k >= 3:                   # the research screen: keep the labs busy (the packs' only consumer)
            wait_game(min(step, t_end - t) / 3)
            keep_researching(k)
            wait_game(min(step, t_end - t) / 3)
            keep_researching(k)
            wait_game(min(step, t_end - t) / 3)
            keep_researching(k)
        else:
            wait_game(min(step, t_end - t))
        t += min(step, t_end - t)
        cur = sample(mp, k)
        windows.append({"to_min": round(t, 1), "research": cur["research"],
                        **{n: round((cur["made"][n] - prev["made"][n]) / ((cur["tick"] - prev["tick"]) / 3600), 1)
                           for n in ("iron-plate", "copper-plate", "automation-science-pack", "logistic-science-pack")}})
        prev = cur
        if s1 is None and t >= minutes / 2 - 1e-9:
            s1 = cur
    s2 = prev
    s1 = s1 or s2
    speed(1)

    def rate(a, b, n):
        return round((b["made"][n] - a["made"][n]) / ((b["tick"] - a["tick"]) / 3600), 1)
    out = {n: {"first_half": rate(s0, s1, n), "second_half": rate(s1, s2, n)}
           for n in ("iron-plate", "copper-plate", "automation-science-pack", "logistic-science-pack", "coal")}
    res = {"rates_per_min": out, "windows": windows, "plan_rates": M.stage_rates(mp, S_GD[0], k),
           "nofuel_start": s0["nofuel"],
           "nofuel_end": s2["nofuel"], "boilers_low_end": s2["boilers_low"],
           "lowpower": [s0["lowpower"], s1["lowpower"], s2["lowpower"]], "nopower_end": s2["nopower"],
           "gen_kw": [round(s0["gen_kw"]), round(s1["gen_kw"]), round(s2["gen_kw"])], "gen_max_kw": s2["gen_max_kw"],
           "status_end": s2["status"], "research": [s0["research"], s2["research"]]}
    # steady: the second half keeps 95 % of the first, or (round 3: the first minutes drain the ore the trunks hold,
    # e.g. 75/min for 15 min then the planned 60) every window of the second half makes >= 95 % of the plan rate
    plan = res["plan_rates"] or {}
    half = [w for w in windows if w["to_min"] > minutes / 2 + 1e-9]
    ok = all(out[n]["second_half"] >= 0.95 * out[n]["first_half"] or
             (plan.get(n) and half and min(w[n] for w in half) >= 0.95 * plan[n])
             for n in ("iron-plate", "copper-plate"))
    res["pass"] = bool(ok and not s2["nofuel"] and s2["boilers_low"] == 0 and s2["nopower"] == 0)
    REP[f"idle_after_{label}"] = res
    log("IDLE", label, json.dumps(res)[:1800])
    return res


S_GD = [None]
MP_GD = [None]
ROUND_MIN = 3
K_OF = {}


def k_of(label):
    return K_OF[label]


def research_state():
    return jlua("local o = {} for _, t in pairs({'automation', 'electric-mining-drill', 'logistics', "
                "'logistic-science-pack', 'automation-science-pack', 'steam-power', 'electronics'}) do "
                "o[t] = f.technologies[t].researched end rcon.print(helpers.table_to_json(o))")


def hand_packs(mp, k):
    """D1: the helper takes plates from the buffer chests, crafts gears + 10 red packs and puts them in a lab."""
    labs = [e for e in mp.ents if M.alive(e, k) and e.name == "lab"]
    bufs = [e for e in mp.ents if M.alive(e, k) and (e.extra or {}).get("role") == "buffer"]
    tasks = []
    for b in bufs:
        tasks.append({"type": "take", "x": b.x, "y": b.y, "item": "iron-plate", "count": 40, "name": "wooden-chest"})
        tasks.append({"type": "take", "x": b.x, "y": b.y, "item": "copper-plate", "count": 20, "name": "wooden-chest"})
    tasks += [{"type": "craft", "recipe": "iron-gear-wheel", "count": 10},
              {"type": "craft", "recipe": "automation-science-pack", "count": 10},
              {"type": "insert", "x": labs[0].x, "y": labs[0].y, "item": "automation-science-pack", "count": 10,
               "name": "lab"}]
    link.call("queue_research", tech="automation")
    r = link.call("agent_tasks", tasks=tasks, replace=True)
    log("D1 tasks", r)
    speed(SPEED)
    t0 = tick()
    ok = wait_game(40, cond=lambda: research_state()["automation"])
    speed(1)
    REP["automation"] = {"researched": ok, "game_min": round((tick() - t0) / 3600, 1), "agent": agent_log()}
    log("automation", REP["automation"])
    return ok


SWAP_CENSUS = """
local r = {}
for _, n in pairs({'burner-mining-drill', 'electric-mining-drill', 'small-electric-pole', 'burner-inserter',
    'inserter', 'transport-belt'}) do
  r[n] = {}
  for _, e in pairs(s.find_entities_filtered{name = n, force = f}) do
    r[n][#r[n] + 1] = {e.unit_number, e.position.x, e.position.y}
  end
end
rcon.print(helpers.table_to_json(r))"""

PACKS_BY_HAND = """
local n = 0
for _, p in pairs(game.players) do
  if p.character then n = n + p.character.get_item_count('automation-science-pack') end
end
rcon.print(helpers.table_to_json({craft = %s}))"""


def main():
    log("restart", env.restart(SLOT, NAME, SAVE)[-160:])
    env.connect(SLOT)
    lua("game.speed = 1 s.peaceful_mode = true for _, e in pairs(s.find_entities_filtered{force = 'enemy'}) do "
        "e.destroy() end rcon.print(1)")
    if A.player_lab:
        lua("f.technologies['automation-science-pack'].researched = true rcon.print(1)")
        REP["setup_cheats"].append("automation-science-pack trigger set researched (no player to craft a lab)")
    REP["research_start"] = research_state()
    t_ps = time.time()
    raw = S.plan_starter_base("", allow_removal=A.allow_removal)
    res = json.loads(raw) if raw.startswith("{") else {"error": raw}
    if "masters" not in res:
        log("plan_starter_base gave no master plans:", str(res)[:1500])
        return
    REP["plan_starter_base"] = {"seconds": round(time.time() - t_ps), "recommended": res.get("recommended"),
                                "not_offered": res.get("not_offered"),
                                "cards": {m["master"]: m.get("card") for m in res.get("masters") or []},
                                "self_running_from": {m["master"]: m.get("self_running_from")
                                                      for m in res.get("masters") or []}}
    mid = res["recommended"]
    if A.variant:
        mid = next(m["master"] for m in res["masters"] if m["variant"] == A.variant)
    REP["variant"] = {"chosen": mid, "recommended": res["recommended"], "allow_removal": A.allow_removal}
    br = next(m for m in res["masters"] if m["master"] == mid)
    REP["brief_roadmap"] = br.get("roadmap")
    mp = M.MasterPlan.load(S._master_path(mid))
    for st_ in mp.stages:
        K_OF[M.slabel(st_.index)] = st_.index
    MP_GD[0] = S._master_world(mp)[1]
    log("master", mid, mp.variant, "now", br.get("now_plan"), REP["plan_starter_base"]["seconds"], "s")
    log("card\n" + br.get("card", ""))
    order = [s.index for s in mp.stages]
    first = order[0]
    built = []
    nxt_offer = {"id": br["now_plan"], "stage": M.slabel(first),
                 "eta": (next(r for r in br["roadmap"] if r["index"] == first).get("eta") or {})}
    waits = 0
    while True:
        if nxt_offer is None:
            # the next stage is offered by the game itself once it can be built (M5): record that, then ask the tool
            expect = next((x for x in order if M.slabel(x) not in built), None)
            if expect is None:
                break
            pid_e = M.plan_id(mp, expect)
            speed(SPEED)
            t_w = tick()
            st_mod = None
            while tick() < t_w + 1800:
                st_mod = mod_status(pid_e)
                if st_mod and st_mod.get("status") in ("proposed", "applied"):
                    break
                time.sleep(1)
            speed(1)
            REP.setdefault("mod_offers", {})[M.slabel(expect)] = st_mod
            log(M.slabel(expect), "mod offer:", st_mod)
            off = json.loads(S.plan_next_stage(mid, show=False))
            REP.setdefault("plan_next_stage", []).append({"after": built[-1] if built else None,
                                                          "offered": off.get("stage"), "valid": off.get("valid"),
                                                          "waiting_for": off.get("waiting_for"),
                                                          "eta": off.get("eta")})
            if not off.get("stage"):
                log("nothing offered; waiting for", off.get("waiting_for"), off.get("status"))
                wf = off.get("waiting_for") or [None, []]
                gate = wf[1] if isinstance(wf, list) and len(wf) > 1 else []
                waits += 1
                if waits > 3:
                    break
                if "automation" in gate and not research_state()["automation"]:
                    hand_packs(mp, 2)
                    continue
                need = [t for t in gate if not research_state().get(t, True)]
                if need:
                    for t in need:
                        link.call("queue_research", tech=t)
                    speed(SPEED)
                    wait_game(40, cond=lambda: all(research_state().get(t) for t in need))
                    speed(1)
                    continue
                break
            if off["stage"] in built:
                log("plan_next_stage RE-OFFERED a built stage", off["stage"])
            nxt_offer = {"id": off["id"], "stage": off["stage"], "eta": off.get("eta") or {}}
        label = nxt_offer["stage"]
        k = next(x for x in order if M.slabel(x) == label)
        if k > TO:
            break
        # the ETA model's variants at the moment of the offer (calibration data)
        try:
            cx, cy = mp.hub or (0, 0)
            w_, gd_ = S._master_world(mp)
            st_ = S._state(cx, cy)
            status_ = M.stage_status(mp, gd_, w_)
            st_.save(PLANS / f"state_{label}.json")      # the state the offer's ETA was computed from
            alt = {}
            for nm, sc, cp in (("serial_1.0", 1.0, False), ("parallel_1.0", 1.0, True), ("serial_0.7", 0.7, False)):
                e_ = M.roadmap_eta(mp, gd_, st_, status=status_, scale=sc, craft_parallel=cp).get(k) or {}
                alt[nm] = (e_.get("eta") or {}).get("complete")
                alt["sim"] = e_.get("sim")
            REP.setdefault("eta_variants", {})[label] = alt
        except Exception as ex:   # noqa: BLE001
            REP.setdefault("eta_variants", {})[label] = {"error": repr(ex)}
        r = build_stage(mp, nxt_offer["id"], label, (nxt_offer["eta"] or {}).get("complete"))
        nxt_offer = None
        if r.get("refused") or not r.get("complete"):
            log("stage not finished; stopping", label)
            break
        if label not in built:
            built.append(label)
        rest = [x for x in order if M.slabel(x) not in built]
        if k == 2:
            S_GD[0] = S._master_world(mp)[1]
            idle_run(mp, 2, "S2", minutes=A.idle_min)
            if not research_state()["automation"]:
                hand_packs(mp, 2)
        if k == 3:
            S_GD[0] = S._master_world(mp)[1]
            # what a player queues once red science runs: the stage gates first, then red techs that keep the labs
            # busy (a lab without research takes no packs and the red assemblers stop with full outputs)
            for t in ("electric-mining-drill", "logistics", "logistic-science-pack", "steel-processing", "military",
                      "fast-inserter", "stone-wall"):
                try:
                    link.call("queue_research", tech=t)
                except Exception:   # noqa: BLE001
                    pass
            a0 = jlua("rcon.print(helpers.table_to_json{f.get_item_production_statistics(s)"
                      ".get_input_count('automation-science-pack')})")[0]
            REP["research_after_S3_start"] = research_state()
            idle_run(mp, 3, "S3", minutes=A.idle_min)
            REP["research_after_S3_idle"] = research_state()
            a1 = jlua("rcon.print(helpers.table_to_json{f.get_item_production_statistics(s)"
                      ".get_input_count('automation-science-pack')})")[0]
            REP["red_packs_made_during_S3_idle"] = a1 - a0
            log("research after S3 idle", REP["research_after_S3_idle"], "red packs made", a1 - a0)
        if rest and rest[0] == 4:
            REP["census_before_S4"] = jlua(SWAP_CENSUS)
        if k == 4:
            REP["census_after_S4"] = jlua(SWAP_CENSUS)
            REP["helper_inv_after_S4"] = (link.call("agent_status") or {}).get("inventory")
        if k == 5:
            S_GD[0] = S._master_world(mp)[1]
            # red + green techs with 15 s units: 10 labs then take 0.67 packs/s of each, more than the 0.5 the base
            # makes, so the measured rate is what the blocks make (not what slow research lets them make)
            for t in ("circuit-network", "engine", "automation-2", "military-2", "fluid-handling"):
                try:
                    link.call("queue_research", tech=t)
                except Exception:   # noqa: BLE001
                    pass
            idle_run(mp, 5, "S5", minutes=A.idle_min)
        if not rest:
            break
    REP["swap_counts_master"] = M.swap_counts(mp)
    REP["planned_S4"] = {
        "burner_out": [[e.x, e.y] for e in M.removed_in(mp, 4) if e.name == "burner-mining-drill"],
        "ins_out": [[e.x, e.y] for e in M.removed_in(mp, 4) if e.name == "burner-inserter"],
        "electric_in": [[e.x, e.y] for e in M.new_in(mp, 4) if e.name == "electric-mining-drill"],
        "poles_new": [[e.x, e.y] for e in M.new_in(mp, 4) if e.name == "small-electric-pole"],
        "removed_all": [[e.name, e.x, e.y] for e in M.removed_in(mp, 4)]}
    REP["research_end"] = research_state()
    save()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        REP["error"] = traceback.format_exc()
        save()
        print(REP["error"])
    finally:
        print(env.restart(SLOT, NAME, stop=True))
