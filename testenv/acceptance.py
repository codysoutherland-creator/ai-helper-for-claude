"""M1 acceptance test (SPEC-v3 M1.10) on a headless test slot. Never talks to the real game.

    .venv\\Scripts\\python.exe testenv\\acceptance.py [--seeds 1001 2002 3003] [--stages bootstrap electric]
        [--variants all|0] [--minutes 12] [--boot-minutes 32] [--refuel-at 17] [--speed 10] [--slot 11]
        [--name accept] [--mcp-seed 1001]
        [--claude-seed 1001 | --claude-seed 0] [--out testenv/acceptance_results.json]

Per seed and stage: fresh map (restart.ps1 -Save seed_<n>.zip) -> planner (Python API; on --mcp-seed the bootstrap
stage goes through the real MCP tool path instead: run_server.py over stdio, tool plan_starter_base) -> every variant in
turn on a fresh copy of the map: validation ok (stored + independent re-validation) -> plan_preview + plan_apply
(0 blocked, all ghosts tagged) -> build (test cheat: revive ghosts, remove the trees/rocks plan_apply marked) ->
initial fuel -> game.speed for --minutes in-game minutes (bootstrap: --boot-minutes, longer than one load of hand fuel
lasts, with the real helper refuel - op plan_helper what=refuel, the Plans tab's "Helper: refuel" - at --refuel-at)
-> measured production (force item stats) vs the plan.

Test scaffolding (documented, not part of the plan):
  - peaceful mode + enemies removed (the planner only warns about nests; biters ate the drills in earlier runs);
  - electric stage: the starter techs are marked researched (the plan must not assume research);
  - initial fuel only where a human hand-feeds at start: entities the plan marks extra.fuel="manual" (burner drills on
    ore, output burner inserters - no burner leech in 2.1) get FUEL_MANUAL coal, boilers get FUEL_BOILER coal to start
    (their belt + burner inserters feed them after that). Furnaces and coal-mine drills get nothing;
  - the bus has no consumers yet: a loader into a deleting infinity chest at every bus end port;
  - bootstrap refuel: the helper gets 50 coal per hand-fed burner handed to it (agent_give: the coal the player would
    give it), then does the refuel itself (walking to every hand-fed burner of the plan).

--no-cheat (planner v2): a player-like server (-NoDebugOps); none of the build/fuel scaffolding above: after plan_apply
the real helper does Approve's work (plan_helper build: S0 coal takes, listed dismantling, gathering + crafting from
nothing, building only this plan's ghosts site by site, then its coal round). The first plate is measured against the
plan's eta.first_plates (within 50 %), then the usual measuring window. Planner v2 builds stage 1 here; its placed
stage 2 (power + labs, approved after stage 1 runs) is checked in-engine by tests/live_onsite.py --stage2-minutes, and
stage 3 is still an estimate, so --stages electric is skipped for v2.

PASS (all seeds/stages/variants): validation ok; 0 blocked ghosts; iron/copper/brick measured >= 90 % of plan in the
measuring window; no machine stalled for a design reason (furnace crafts < 90 % of its share, drill/boiler/engine/
inserter out of fuel or power at any sample; a drill on depleted ore is allowed); electric: one network, no consumer
low on power, generation >= demand. Then (only if all that passed) one real Ask-Claude round trip through the
companion with the cost gate: "@claude set me up with a starter base" -> Run -> reply; cost + time recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
ITEMS = ["iron-plate", "copper-plate", "stone-brick"]
FUEL_MANUAL = 50   # one stack of coal into each hand-fed burner: a burner drill burns ~22 coal in 10 min
FUEL_BOILER = 5    # a few coal so the boilers start; their inserters feed them from the coal belt afterwards
THRESHOLD = 0.90
ELECTRIC_TECHS = ["automation-science-pack", "electronics", "automation", "steam-power", "electric-mining-drill",
                  "logistics"]


def restart(name, slot, save=None, stop=False, no_debug=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", name, "-Slot", str(slot)]
    args += ["-Stop"] if stop else (["-Save", save] + (["-NoDebugOps"] if no_debug else []))
    with tempfile.TemporaryFile() as f:  # never pipe: the started factorio.exe would keep the pipe open
        subprocess.run(args, stdout=f, stderr=f, timeout=120)


class Slot:
    """One headless test instance (rcon for test-only Lua, link for the mod ops)."""

    def __init__(self, a):
        self.a = a
        self.inst = ROOT / "testenv" / "inst" / a.name
        from factorio_claude import link, rcon
        self.link, self.rcon = link, rcon

    def lua(self, code):
        r = self.rcon._client.command("/silent-command " + code)
        if r and r.startswith("Cannot execute command"):
            raise RuntimeError(r)
        return r

    def jlua(self, code):
        return json.loads(self.lua(code))

    def fresh(self, seed, stage):
        restart(self.a.name, self.a.slot, f"seed_{seed}.zip", no_debug=self.a.no_cheat)
        self.rcon._client = self.rcon.Rcon("127.0.0.1", 27016 + self.a.slot, "testpw")
        for _ in range(40):
            try:
                self.lua("rcon.print(1)")
                break
            except Exception:  # noqa: BLE001 - server still starting
                time.sleep(0.5)
        self.lua("local s=game.surfaces.nauvis; s.request_to_generate_chunks({0,0}, 8); s.force_generate_chunk_requests()")
        self.lua("local s=game.surfaces.nauvis; s.peaceful_mode=true; "
                 "for _, e in pairs(s.find_entities_filtered{force='enemy'}) do e.destroy() end")
        if stage == "electric":
            self.lua("for _, n in pairs(helpers.json_to_table('%s')) do local t=game.forces.player.technologies[n]; "
                     "if t then t.researched=true end end" % json.dumps(ELECTRIC_TECHS))

    def call(self, op, **args):
        return self.link.call(op, **args)

    def server_errors(self):
        log = (self.inst / "server.log").read_text(encoding="utf-8", errors="replace")
        return [ln for ln in log.splitlines() if "Error" in ln or ("error" in ln.lower() and "claude" in ln.lower())]


# ---------------------------------------------------------------- planning

def plan_python(slot, stage, planner="v2"):
    from factorio_claude.planner import placer, staged
    from factorio_claude.planner.gamedata import GameData
    from factorio_claude.planner.state import BaseState
    from factorio_claude.planner.world import World
    t0 = time.time()
    world = World.from_link(area=(-200, -200, 200, 200), link=slot.link.call, charted=False)
    gd = GameData.from_link(link=slot.link)
    t1 = time.time()
    if planner == "v2":  # every build-now variant (later stages like the long-term hub are not built here)
        st = BaseState.from_link(slot.link.call, 0, 0)
        plans = [p for p in staged.plan_options(world, gd, st, {"near": (0.0, 0.0)}, variants=4)
                 if p.stats.get("build_now", True)]
    else:
        plans = placer.plan_variants(world, gd, {"stage": stage, "near": (0.0, 0.0)})
    return plans, {"export_s": round(t1 - t0, 1), "plan_s": round(time.time() - t1, 1), "path": "python"}, gd, world


def plan_mcp(stage, plans_dir):
    """plan_starter_base through the real MCP server (run_server.py over stdio) -> saved Plans."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from factorio_claude.planner.plan import Plan

    async def go():
        env = {k: v for k, v in os.environ.items()}
        params = StdioServerParameters(command=str(PY), args=[str(ROOT / "run_server.py")], cwd=str(ROOT), env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                assert "plan_starter_base" in names, names
                res = await s.call_tool("plan_starter_base", {"goal_text": "set me up with a starter base",
                                                              "variants": 3, "stage": stage})
                return res.content[0].text
    t0 = time.time()
    text = asyncio.run(go())
    out = json.loads(text) if text.startswith("{") else {"error": text}
    if "plans" not in out:
        raise RuntimeError(f"MCP plan_starter_base failed: {text[:600]}")
    plans = [Plan.load(Path(plans_dir) / f"{b['id']}.json") for b in out["plans"] if b.get("build_now", True)]
    return plans, {"tool_s": round(time.time() - t0, 1), "path": "mcp", "tool_reply_chars": len(text),
                   "preview_errors": [b.get("preview_error") for b in out["plans"] if b.get("preview_error")]}


# ---------------------------------------------------------------- one variant in the engine

def run_variant(slot, plan, gd, a, res):
    from factorio_claude.planner.validate import validate
    ok = True

    def check(name, cond, info=None):
        nonlocal ok
        res.setdefault("checks", {})[name] = bool(cond) if info is None else {"ok": bool(cond), "info": info}
        if not cond:
            ok = False
            print(f"   FAIL {name}: {info}")

    check("validation_ok", (plan.validation or {}).get("ok"), (plan.validation or {}).get("errors"))
    rep = validate(plan.entities, gd, None, plan.expect()) if gd is not None else None
    if rep is not None:  # independent re-check of exactly these entities (no world: structure only)
        check("revalidation_ok", rep.ok, [str(i) for i in rep.errors[:5]])

    pv = slot.call("plan_preview", **plan.preview_args(show=True))
    check("preview", pv is not None and not (isinstance(pv, dict) and pv.get("error")), pv)
    r = slot.call("plan_apply", id=plan.id) or {}
    deadline = time.time() + 60
    while r.get("pending") and time.time() < deadline:
        time.sleep(0.5)
        r = slot.call("plan_get", id=plan.id) or {}
        r = r.get("result") or r
    placed = (r.get("placed") or 0) + (r.get("existing") or 0)
    res["apply"] = {k: r.get(k) for k in ("placed", "existing", "blocked", "deconstruct", "unknown", "blocked_list")}
    check("all_placed", placed == len(plan.entities), {"placed": placed, "planned": len(plan.entities)})
    check("zero_blocked", not r.get("blocked"), r.get("blocked"))
    tagged = slot.jlua("""
local n, other = 0, 0
for _, g in pairs(game.surfaces.nauvis.find_entities_filtered{type='entity-ghost'}) do
  if g.tags and g.tags.claude_plan == %s then n = n + 1 else other = other + 1 end
end
rcon.print(helpers.table_to_json{n, other})""" % json.dumps(plan.id))
    check("ghosts_tagged", tagged[0] == len(plan.entities), tagged)

    if a.no_cheat:  # the real helper builds and fuels it (no revive, no coal handed out)
        real_build(slot, plan, a, res, check)
        return ok

    # build: clear what plan_apply marked for deconstruction, revive every ghost (test cheat)
    built = slot.jlua("""
local s = game.surfaces.nauvis
local d = 0
for _, e in pairs(s.find_entities_filtered{to_be_deconstructed=true}) do e.destroy(); d = d + 1 end
local n, fail = 0, 0
for _, g in pairs(s.find_entities_filtered{type='entity-ghost'}) do
  if g.revive() then n = n + 1 else fail = fail + 1 end
end
rcon.print(helpers.table_to_json{d, n, fail})""")
    res["built"] = {"cleared": built[0], "revived": built[1], "revive_failed": built[2]}
    check("built", built[2] == 0 and built[1] == len(plan.entities), res["built"])

    # initial fuel: only the hand-fed burners (plan extra.fuel="manual") and boilers (a few coal to start)
    manual = [[e.x, e.y, e.name, FUEL_BOILER if e.name == "boiler" else FUEL_MANUAL] for e in plan.entities
              if (e.extra or {}).get("fuel") == "manual" or e.name == "boiler"]
    fed = slot.jlua("""
local s, n = game.surfaces.nauvis, 0
for _, m in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entity(m[3], {m[1], m[2]})
  if e and e.insert{name='coal', count=m[4]} > 0 then n = n + 1 end
end
rcon.print(n)""" % json.dumps(manual))
    res["fuel_seeded"] = {"entities": len(manual), "fed": fed, "coal_each_manual": FUEL_MANUAL,
                          "coal_each_boiler": FUEL_BOILER}
    check("fuel_seeded", fed == len(manual), res["fuel_seeded"])

    # bus sinks (scaffolding: nothing consumes plates yet)
    sinks = [[p[2] + 0.5, p[3] + 0.5, p[4]] for p in plan.stats.get("ports") or []]  # v2 on-site rows: chests
    nsink = slot.jlua("""
local s, n = game.surfaces.nauvis, 0
local V = {[0] = {0, -1}, [4] = {1, 0}, [8] = {0, 1}, [12] = {-1, 0}}
for _, p in pairs(helpers.json_to_table([==[%s]==])) do
  local v = V[p[3]]
  for _, e in pairs(s.find_entities_filtered{area = {{p[1] - 0.4 + math.min(0, v[1]), p[2] - 0.4 + math.min(0, v[2])},
      {p[1] + 0.4 + math.max(0, v[1]), p[2] + 0.4 + math.max(0, v[2])}}, type = {'tree', 'simple-entity'}}) do e.destroy() end
  local l = s.create_entity{name = 'loader-1x1', position = {p[1], p[2]}, direction = p[3], type = 'input', force = 'player'}
  local c = s.create_entity{name = 'infinity-chest', position = {p[1] + v[1], p[2] + v[2]}, force = 'player'}
  if l and c then c.remove_unfiltered_items = true; n = n + 1 end
end
rcon.print(n)""" % json.dumps(sinks))
    check("sinks", nsink == len(sinks), {"sinks": nsink, "ports": len(sinks)})

    measure(slot, plan, a, res, check, manual)  # its checks go through check() and clear ok
    return ok


MADE_LUA = """
local s, n, built = game.surfaces.nauvis, 0, 0
for _, r in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entity(r[1], {r[2], r[3]})
  if e then
    built = built + 1
    if e.type == 'furnace' then n = n + e.products_finished end
  end
end
rcon.print(helpers.table_to_json{tick = game.tick, made = n, built = built})"""


def real_build(slot, plan, a, res, check):
    """--no-cheat: Approve's real follow-up (plan_helper build: S0 coal takes, listed dismantling, gathering, crafting,
    building only this plan's ghosts, then the coal round) at game.speed; first plate measured against
    eta.first_plates (within 50 %), then the usual measuring window once the plan's ETA to full rate has passed."""
    eta = plan.stats.get("eta") or {}
    ents = [[e.name, e.x, e.y] for e in plan.entities]
    slot.lua(f"game.speed = {a.speed}")
    t0 = int(slot.lua("rcon.print(game.tick)"))
    r = slot.call("plan_helper", id=plan.id, what="build")
    res["helper"] = r.get("msg")
    first, last = None, None
    full = eta.get("target") or eta.get("complete") or 15
    until = t0 + int((min(60.0, max(full + 2, (eta.get("complete") or 0) + 2))) * 3600)
    log, seen = [], set()
    while True:
        last = slot.jlua(MADE_LUA % json.dumps(ents))
        if first is None and last["made"] > 0:
            first = last["tick"]
        for x in (slot.call("agent_status") or {}).get("log") or []:   # the helper keeps only 25 lines
            k = (x.get("tick"), x.get("msg"))
            if k not in seen:
                seen.add(k)
                log.append(f"{round((x.get('tick', t0) - t0) / 3600, 2)} {x.get('msg')}")
        if last["tick"] >= until and first is not None:
            break
        if last["tick"] >= t0 + 60 * 3600:   # an hour of game time without plates: give up
            break
        time.sleep(0.3)
    res["helper_history"] = [ln for ln in log if not ln.split(" ", 1)[1].startswith(("done: take", "took "))][:150]
    meas = round((first - t0) / 3600, 2) if first else None
    pred = eta.get("first_plates")
    res["first_plates"] = {"predicted": pred, "measured": meas, "built": last["built"], "planned": len(ents)}
    check("first_plates", meas is not None, res["first_plates"])
    if meas is not None and pred:
        check("first_plates_within_50pct", abs(meas - pred) <= 0.5 * pred + 0.25, res["first_plates"])
    st = slot.call("agent_status") or {}
    res["helper_log"] = [x["msg"] for x in st.get("log") or []]
    res["build_order"] = plan.stats.get("build_order_names")
    slot.lua("game.speed = 1")
    measure(slot, plan, a, res, check, ())
    end = slot.jlua(MADE_LUA % json.dumps(ents))   # by the end of the measuring window everything is built
    res["first_plates"]["built_at_end"] = end["built"]
    check("all_built", end["built"] == len(ents), res["first_plates"])


SAMPLE_LUA = """
local s, f = game.surfaces.nauvis, game.forces.player
local names = {}
for k, v in pairs(defines.entity_status) do names[v] = k end
local BAD = helpers.json_to_table('%s')
local st, fin, bad = {}, {}, {}
for _, e in pairs(s.find_entities_filtered{type={'furnace','mining-drill','boiler','generator','inserter',
    'offshore-pump'}, force=f}) do
  local k = e.name .. ':' .. (names[e.status] or tostring(e.status))
  st[k] = (st[k] or 0) + 1
  if BAD[names[e.status]] then bad[#bad + 1] = {e.name, names[e.status], e.position.x, e.position.y} end
  if e.type == 'furnace' then fin[#fin + 1] = {e.position.x, e.position.y, e.products_finished} end
end
local is = f.get_item_production_statistics(s)
local cnt = {}
for _, n in pairs(helpers.json_to_table('%s')) do cnt[n] = is.get_input_count(n) end
local nets, low = {}, 0
for _, p in pairs(s.find_entities_filtered{type='electric-pole', force=f}) do nets[p.electric_network_id or -1] = true end
local nn = 0
for _ in pairs(nets) do nn = nn + 1 end
rcon.print(helpers.table_to_json{tick = game.tick, status = st, furnaces = fin, counts = cnt, networks = nn, bad = bad})"""

# out of fuel / power / water = stalled by design; no_minable_resources (ore depletion) and backed-up drills are allowed
BAD = {"no_fuel", "no_power", "low_power", "no_input_fluid"}


FUEL_LUA = """
local s, ok, low = game.surfaces.nauvis, 0, {}
for _, m in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entity(m[3], {m[1], m[2]})
  local n = e and e.get_fuel_inventory() and e.get_fuel_inventory().get_item_count('coal') or 0
  -- a burner inserter's fill is 5 coal (it burns very little), drills and furnaces get 30-50
  local want = (e and e.type == 'inserter') and math.min(%d, 3) or %d
  if n >= want then ok = ok + 1 elseif #low < 5 then low[#low + 1] = {m[3], m[1], m[2], n} end
end
rcon.print(helpers.table_to_json{ok = ok, low = low})"""


def helper_refuel(slot, plan, manual, res, check):
    """The real helper refuel of the plan's hand-fed burners (Plans tab "Helper: refuel")."""
    hand = [m for m in manual if m[2] != "boiler"]
    slot.call("agent_spawn")
    slot.call("agent_give", item="coal", count=FUEL_MANUAL * len(hand))
    r = slot.call("plan_helper", id=plan.id, what="refuel")
    res["refuel"] = {"msg": r.get("msg"), "tick": int(slot.lua("rcon.print(game.tick)")), "entities": len(hand)}
    return hand


def measure(slot, plan, a, res, check, manual=()):
    sample_lua = SAMPLE_LUA % (json.dumps({k: True for k in BAD}), json.dumps(ITEMS))
    slot.lua(f"game.speed = {a.speed}")
    start = int(slot.lua("rcon.print(game.tick)"))
    minutes = a.boot_minutes if plan.stage == "bootstrap" else a.minutes
    refuel_at = start + int(a.refuel_at * 3600) if plan.stage == "bootstrap" and a.refuel_at and not a.no_cheat \
        else None
    hand = None
    total = int(minutes * 3600)
    warm = int(min(a.warmup, minutes / 2) * 3600)
    samples = []
    t0 = time.time()

    def until(tick):
        while int(slot.lua("rcon.print(game.tick)")) < tick:
            time.sleep(0.5)
    until(start + warm)
    first = slot.jlua(sample_lua)
    samples.append(first)
    step = 3600  # one status sample per in-game minute
    t = first["tick"]
    while t < start + total:
        until(min(t + step, start + total))
        samples.append(slot.jlua(sample_lua))
        t = samples[-1]["tick"]
        if refuel_at and hand is None and t >= refuel_at:
            hand = helper_refuel(slot, plan, manual, res, check)
        elif hand is not None and "after" not in res["refuel"] and t >= refuel_at + 4 * 3600:
            st = slot.call("agent_status") or {}
            res["refuel"]["after"] = slot.jlua(FUEL_LUA % (json.dumps(hand), 25, 25))
            res["refuel"]["log"] = [x["msg"] for x in st.get("log") or [] if "refuel" in x["msg"] or "Stopped" in x["msg"]][-3:]
            check("helper_refuel", res["refuel"]["after"]["ok"] == len(hand), res["refuel"])
    slot.lua("game.speed = 1")
    if refuel_at and manual and "after" not in (res.get("refuel") or {}):
        check("helper_refuel", False, "refuel never checked (run shorter than refuel-at + 4 min?)")
    last = samples[-1]
    mins = (last["tick"] - first["tick"]) / 3600
    res["run"] = {"game_minutes": round((last["tick"] - start) / 3600, 1), "window_minutes": round(mins, 1),
                  "speed": a.speed, "wall_s": round(time.time() - t0, 1)}
    per_min = {n: (last["counts"].get(n, 0) - first["counts"].get(n, 0)) / mins for n in ITEMS}
    want = plan.stats["per_min"]
    res["rates"] = {}
    for item in ITEMS:
        w = float(want.get(item) or 0)
        g = per_min[item]
        ratio = g / w if w else None
        res["rates"][item] = {"plan": w, "measured": round(g, 1), "ratio": round(ratio, 3) if ratio else None}
        if w:
            check(f"rate_{item}", ratio >= THRESHOLD, res["rates"][item])
    # per-furnace crafts over the window vs its share (a furnace below 90 % of the plan rate is stalled by design)
    # only the plan's own machines are judged (a map may hold idle old builds, e.g. the sprawl fixture)
    mine = {(float(e.x), float(e.y)) for e in plan.entities}
    f0 = {(x, y): n for x, y, n in first["furnaces"]}
    crafts = sorted(((n - f0.get((x, y), 0)), (x, y)) for x, y, n in last["furnaces"] if (x, y) in mine)
    speed = {"stone-furnace": 1.0, "steel-furnace": 2.0, "electric-furnace": 2.0}
    fname = next((e.name for e in plan.entities if e.name in speed), "stone-furnace")
    # stone brick takes 3.2 s too (2 stone -> 1 brick): every vanilla smelting recipe is 3.2 s
    expect = mins * 60 / (3.2 / speed[fname])
    if "units" in plan.stats:  # planner v2 burner pairs: one burner drill (15 ore/min) feeds each furnace
        expect = min(expect, mins * 15.0)
    slow = [(c, xy) for c, xy in crafts if c < THRESHOLD * expect]
    res["furnaces"] = {"count": len(crafts), "expected_crafts_each": round(expect, 1),
                       "min_crafts": crafts[0][0] if crafts else None, "slow": slow[:6]}
    check("furnaces_all_working", not slow, res["furnaces"])
    # status samples (one per in-game minute): an entity out of fuel / power in two samples in a row has stopped
    # (stalled); seen once only = a momentary gap (e.g. the coal lane still filling right after warm-up) - those are
    # recorded as blips, and the per-furnace craft count above still catches any furnace that lost output.
    seen = [{(b[0], b[2], b[3]): b[1] for b in (smp.get("bad") or []) if (b[2], b[3]) in mine} for smp in samples]
    stalled = sorted({(k[0], st, k[1], k[2]) for prev, cur in zip(seen, seen[1:]) for k, st in cur.items() if k in prev})
    blips = [[smp["tick"]] + b for smp in samples for b in (smp.get("bad") or [])]
    res["status_last"] = last["status"]
    res["status_bad"] = {f"{n}:{st}@{x},{y}": True for n, st, x, y in stalled}
    res["status_blips"] = blips[:20]
    check("no_design_stalls", not stalled, stalled[:10])
    if plan.stage == "electric":
        pw = slot.call("power") or {}
        nets = pw.get("networks") or []
        res["power"] = nets[:2]
        prod = sum(n.get("production_MW", 0) for n in nets)
        cons = sum(n.get("consumption_MW", 0) for n in nets)
        check("power_one_network", last["networks"] == 1, last["networks"])
        check("power_satisfied", prod >= cons * 0.99 and cons > 0, {"prod_MW": prod, "cons_MW": cons})
    errs = slot.server_errors()
    check("no_script_errors", not errs, errs[:5])


# ---------------------------------------------------------------- real Claude round trip

def claude_round_trip(slot, a, seed, out):
    """Companion (real claude -p, MCP pointed at this slot) answers '@claude set me up with a starter base' after
    the cost gate's Run. Records cost + time."""
    slot.fresh(seed, "bootstrap")
    work = ROOT / "testenv" / "inst" / f"companion_{a.name}"
    work.mkdir(parents=True, exist_ok=True)
    for f in ("companion.log", "session.json"):
        (work / f).unlink(missing_ok=True)
    plans_dir = Path(tempfile.mkdtemp(prefix="plans_claude_"))
    env = dict(os.environ, FACTORIO_COMPANION_LOCK_PORT=str(34700 + a.slot), FACTORIO_CHAT_DIR=str(work),
               FACTORIO_COMPANION_LOG=str(work / "companion.log"), FACTORIO_PLANS_DIR=str(plans_dir))
    env.pop("FACTORIO_FAKE_CLAUDE", None)
    proc = subprocess.Popen([str(PY), "-m", "factorio_claude.companion", "--no-exit-check"], cwd=ROOT, env=env)
    rec = {"seed": seed}
    try:
        def hist():
            return slot.call("claude_history", n=50)
        end = time.time() + 30
        while time.time() < end and not hist().get("online"):
            time.sleep(0.5)
        rec["online"] = bool(hist().get("online"))
        slot.call("claude_confirm_drop")
        n0 = len(hist()["history"])
        t0 = time.time()
        slot.rcon._client.command("@claude set me up with a starter base")
        h, end = None, time.time() + 30
        while time.time() < end:
            h = hist()
            if h.get("confirms"):
                break
            time.sleep(0.5)
        rec["gate_text"] = h["confirms"][0]["text"] if h and h.get("confirms") else None
        if not rec["gate_text"]:
            rec["error"] = "no cost-gate confirmation"
            return rec
        slot.call("confirm_simulate", request_id=h["confirms"][0]["id"], run=True)
        t1 = time.time()
        end = time.time() + 900
        while time.time() < end:
            hs = hist()["history"]
            if len(hs) > n0 and hs[-1]["who"] == "Claude" and hs[-1].get("cost"):
                rec.update(reply=hs[-1]["text"], cost_line=hs[-1]["cost"], seconds=round(time.time() - t1, 1),
                           seconds_incl_gate=round(time.time() - t0, 1))
                break
            time.sleep(1)
        else:
            rec["error"] = "no reply within 15 min"
        log = (work / "companion.log").read_text(encoding="utf-8", errors="replace")
        rec["log_tail"] = [ln for ln in log.splitlines() if "<<" in ln or "ERROR" in ln][-3:]
        rec["plans"] = [p.stem for p in plans_dir.glob("*.json")]
        rec["plan_list"] = slot.call("plan_list")
        rec["plan_list"] = [{k: r.get(k) for k in ("id", "status", "entities")} for r in rec["plan_list"] or []]
        rec["ok"] = bool(rec.get("reply")) and bool(rec["plans"]) and "error" not in rec
    finally:
        proc.terminate()
        proc.wait(15)
    return rec


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1001, 2002, 3003])
    ap.add_argument("--stages", nargs="+", default=["bootstrap", "electric"])
    ap.add_argument("--variants", default="all", help="all | comma list of indices (0 = first variant)")
    ap.add_argument("--planner", default="v2", choices=("v1", "v2"), help="v2 = staged.plan_options (default)")
    ap.add_argument("--minutes", type=float, default=12, help="in-game minutes run per electric variant (>= 10)")
    ap.add_argument("--boot-minutes", type=float, default=32,
                    help="in-game minutes per bootstrap variant (longer than one load of hand fuel lasts)")
    ap.add_argument("--refuel-at", type=float, default=17, help="bootstrap: helper refuel after this many minutes")
    ap.add_argument("--warmup", type=float, default=5, help="in-game minutes before the measuring window")
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--slot", type=int, default=11)
    ap.add_argument("--name", default="accept")
    ap.add_argument("--mcp-seed", type=int, default=1001, help="seed whose bootstrap plans come via the MCP tool")
    ap.add_argument("--claude-seed", type=int, default=1001, help="0 = skip the real Claude round trip")
    ap.add_argument("--claude-only", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "testenv" / "acceptance_results.json"))
    ap.add_argument("--no-cheat", action="store_true",
                    help="player-like server (-NoDebugOps): the real helper builds and fuels the plan (plan_helper "
                         "build), nothing is revived or handed out; first plates measured against the plan's ETA")
    a = ap.parse_args()
    if a.planner == "v2" and "electric" in a.stages:
        # planner v2: stage 1 is built here; stage 2 (placed) by tests/live_onsite.py --stage2-minutes; stage 3 is an
        # estimate on the cards
        print("note: planner v2 builds stage 1 here (stage 2: tests/live_onsite.py --stage2-minutes; stage 3 is an "
              "estimate): the 'electric' stage is skipped; use --planner v1 for the electric hub")
        a.stages = [s for s in a.stages if s != "electric"] or ["bootstrap"]

    plans_dir = Path(tempfile.mkdtemp(prefix="plans_accept_"))
    os.environ.update({"FACTORIO_UDP_PORT": str(34500 + a.slot), "FACTORIO_RCON_PORT": str(27016 + a.slot),
                       "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1",
                       "FACTORIO_PLAN_UNCHARTED": "1", "FACTORIO_PLANS_DIR": str(plans_dir),
                       "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / a.name / "write" / "script-output")})
    sys.path.insert(0, str(ROOT))
    from factorio_claude.planner.gamedata import GameData
    slot = Slot(a)
    results = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "args": vars(a), "runs": [], "seeds": {}}

    def save():
        Path(a.out).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")

    all_ok = True
    try:
        for seed in ([] if a.claude_only else a.seeds):
            for stage in a.stages:
                key = f"{seed}/{stage}"
                print(f"== seed {seed} {stage}: fresh map, planning")
                slot.fresh(seed, stage)
                gd = None
                try:
                    if seed == a.mcp_seed and stage == "bootstrap":
                        plans, info = plan_mcp(stage, plans_dir)
                        gd = GameData.from_link(link=slot.link)
                    else:
                        plans, info, gd, _ = plan_python(slot, stage, a.planner)
                except Exception as e:  # noqa: BLE001
                    print("!! planning failed:", e)
                    results["seeds"][key] = {"ok": False, "error": str(e)}
                    all_ok = False
                    save()
                    continue
                info["variants"] = [p.variant for p in plans]
                results["seeds"][key] = info
                print(f"   {info}")
                idx = range(len(plans)) if a.variants == "all" else [int(i) for i in a.variants.split(",")]
                for i in idx:
                    if i >= len(plans):
                        continue
                    plan = plans[i]
                    if i > 0 or info["path"] == "mcp":  # every variant gets its own fresh map
                        slot.fresh(seed, stage)
                    else:
                        slot.call("plan_clear_preview")
                    res = {"seed": seed, "stage": stage, "variant": plan.variant, "index": i, "plan": plan.id,
                           "entities": len(plan.entities), "planned": plan.stats.get("per_min"),
                           "manual_fuel": sum(1 for e in plan.entities if (e.extra or {}).get("fuel") == "manual"),
                           "via": info["path"]}
                    print(f"-- {seed} {stage} {plan.variant} ({plan.id}, {len(plan.entities)} entities)")
                    t0 = time.time()
                    try:
                        res["ok"] = bool(run_variant(slot, plan, gd, a, res))
                    except Exception as e:  # noqa: BLE001
                        res["ok"], res["error"] = False, repr(e)
                    res["wall_s"] = round(time.time() - t0, 1)
                    all_ok &= res["ok"]
                    print(f"   {'PASS' if res['ok'] else 'FAIL'} rates={res.get('rates')} "
                          f"furnaces={res.get('furnaces', {}).get('min_crafts')}/"
                          f"{res.get('furnaces', {}).get('expected_crafts_each')} bad={res.get('status_bad')} "
                          f"blips={len(res.get('status_blips') or [])}")
                    results["runs"].append(res)
                    save()
        results["engine_pass"] = all_ok
        if a.claude_seed and (all_ok or a.claude_only):
            print(f"== real Claude round trip on seed {a.claude_seed}")
            results["claude"] = claude_round_trip(slot, a, a.claude_seed, results)
            print("  ", json.dumps(results["claude"])[:800])
            all_ok &= bool(results["claude"].get("ok"))
        results["pass"] = all_ok
        save()
    finally:
        restart(a.name, a.slot, stop=True)
    print("RESULT", "PASS" if all_ok else "FAIL", "->", a.out)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
