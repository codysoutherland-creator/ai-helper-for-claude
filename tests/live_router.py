"""In-engine router check on a headless test slot (not collected by pytest: needs a server).

    .venv\\Scripts\\python.exe tests\\live_router.py [seed ...] [--slot 7] [--name router]

Per fixture seed: restarts the slot from testenv/write/seed_<n>.zip, builds router_scenarios.build(seed) offline,
clears the trees/rocks the routes need (grid.to_clear), places everything for real, feeds each route from a chest +
burner inserter, runs game.speed 10 and checks: items reach the end of every belt route, side-loaded items sit on the
requested lane only, water reaches the end of every pipe route, every electric entity is powered by one network that
includes the source pole.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import router_scenarios as rs  # noqa: E402
from factorio_claude import rcon  # noqa: E402
from factorio_claude.planner.router import VEC  # noqa: E402


def restart(name, slot, save=None, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", name, "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", save]
    with tempfile.TemporaryFile() as f:  # never pipe: the started factorio.exe would keep the pipe open
        subprocess.run(args, stdout=f, stderr=f, timeout=120)


def lua(code):
    r = rcon._client.command("/silent-command " + code)
    if r and r.startswith("Cannot execute command"):
        raise RuntimeError(r)
    return r


def place(ents, chunk=60):
    """Create entities (list of dicts) for real; returns failures."""
    fails = []
    for i in range(0, len(ents), chunk):
        js = json.dumps(ents[i:i + chunk])
        r = lua(r"""
local s = game.surfaces.nauvis
local fails = {}
for _, e in pairs(helpers.json_to_table([==[%s]==])) do
  local p = {name=e.name, position={e.x, e.y}, direction=e.dir, force="player", raise_built=false}
  if e.type then p.type = e.type end
  if e.recipe then p.recipe = e.recipe end
  local ok, ent = pcall(s.create_entity, p)
  if not ok or not ent then fails[#fails+1] = e.name.."@"..e.x..","..e.y..(ok and "" or (" "..tostring(ent))) end
end
rcon.print(helpers.table_to_json(fails))""" % js)
        fails += json.loads(r or "[]")
    return fails


def as_dict(e):
    d = {"name": e.name, "x": e.x, "y": e.y, "dir": int(e.direction or 0)}
    if isinstance(e.extra, dict) and e.extra.get("type"):
        d["type"] = e.extra["type"]
    if e.recipe:
        d["recipe"] = e.recipe
    return d


def tile_center(t):
    return t[0] + 0.5, t[1] + 0.5


def run_seed(seed, name, slot):
    print(f"== seed {seed}")
    restart(name, slot, f"seed_{seed}.zip")
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + slot, "testpw")
    lua("local s=game.surfaces.nauvis; s.request_to_generate_chunks({0,0}, 8); s.force_generate_chunk_requests()")
    t0 = time.time()
    sc = rs.build(seed)
    gd, base = sc["gd"], sc["base"]
    print(f"   planned in {time.time() - t0:.2f} s")

    all_ents = []
    for r in sc["routes"].values():
        all_ents += r["ents"]
    for f in sc["fixtures"]:
        all_ents += f["ents"] + f.get("main", [])
    for p in sc["pipes"].values():
        all_ents += p["ents"] + [p["pump"]]
    pc = sc["poles"]
    all_ents += pc["consumers"] + pc["poles"] + [pc["eei"], pc["source"]]
    tiles = set()
    for e in all_ents:
        tiles |= gd.footprint(e.name, e.x, e.y, e.direction or 0)
    sources = []  # (chest tile, inserter tile, inserter dir, item)
    for ore, r in sc["routes"].items():
        sources.append((r["chest"], r["inserter"], r["inserter_dir"], ore))
    for f in sc["fixtures"]:
        d = rs.dominant_dir(f["src"][2][0] - f["src"][0][0], f["src"][2][1] - f["src"][0][1])
        sources.append((f["src"][0], f["src"][1], (d + 8) % 16, f["item"]))
    for c, i, _d, _it in sources:
        tiles |= {c, i}
    clear = base.to_clear(tiles)
    print(f"   {len(all_ents)} entities, clearing {len(clear)} trees/rocks")
    lua(r"""
local s = game.surfaces.nauvis
local n = 0
for _, o in pairs(helpers.json_to_table([==[%s]==])) do
  for _, e in pairs(s.find_entities_filtered{name=o[3], position={o[1], o[2]}, radius=0.05}) do e.destroy(); n = n + 1 end
end
rcon.print(n)""" % json.dumps(clear))

    fails = place([as_dict(e) for e in all_ents])
    src = [{"name": "iron-chest", "x": tile_center(c)[0], "y": tile_center(c)[1], "dir": 0} for c, *_ in sources]
    src += [{"name": "burner-inserter", "x": tile_center(i)[0], "y": tile_center(i)[1], "dir": d}
            for _c, i, d, _it in sources]
    fails += place(src)
    lua(r"""
local s = game.surfaces.nauvis
for _, o in pairs(helpers.json_to_table([==[%s]==])) do
  local c = s.find_entity("iron-chest", {o[1], o[2]}); c.insert{name=o[5], count=200}
  local i = s.find_entity("burner-inserter", {o[3], o[4]}); i.insert{name="coal", count=20}
end
local eei = s.find_entities_filtered{name="electric-energy-interface"}[1]
eei.power_production = 1e9; eei.electric_buffer_size = 1e10
game.speed = 10""" % json.dumps([[*tile_center(c), *tile_center(i), it] for c, i, _d, it in sources]))
    ok = not fails
    if fails:
        print("   !! placement failed:", fails[:10])

    # wait for the belts: longest route at yellow belt speed (1.875 tiles/s) + slack, in game ticks
    longest = max(len(r["ents"]) for r in sc["routes"].values())
    deadline_ticks = int((longest / 1.875 + 40) * 60)
    start_tick = int(lua("rcon.print(game.tick)"))
    checks = {}
    for ore, r in sc["routes"].items():
        g = r["goal"]
        checks[ore] = (g.x + 0.5, g.y + 0.5, None)
    for f in sc["fixtures"]:
        checks[f["kind"]] = (f["check"][0] + 0.5, f["check"][1] + 0.5, f["lane"])
    status = {}
    while True:
        res = json.loads(lua(r"""
local s = game.surfaces.nauvis
local out = {}
for k, c in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entities_filtered{position={c[1], c[2]}, radius=0.1, type={"transport-belt","underground-belt"}}[1]
  if e then
    local l, r = e.get_transport_line(defines.transport_line.left_line), e.get_transport_line(defines.transport_line.right_line)
    local items = {}
    for _, it in pairs(l.get_contents()) do items[it.name] = true end
    for _, it in pairs(r.get_contents()) do items[it.name] = true end
    out[k] = {l.get_item_count(), r.get_item_count(), items}
  else out[k] = "missing" end
end
out.tick = game.tick
rcon.print(helpers.table_to_json(out))""" % json.dumps(checks)))
        tick = res.pop("tick")
        status = res
        arrived = {k: isinstance(v, list) and v[0] + v[1] > 0 for k, v in res.items()}
        if all(arrived.values()) or tick - start_tick > deadline_ticks:
            break
        time.sleep(1.0)
    print(f"   belts after {(tick - start_tick) / 60:.0f} s game time:")
    for k, v in status.items():
        lane = checks[k][2]
        want = rs.ORES[0] if False else None
        item = sc["routes"][k]["start"].item if k in sc["routes"] else next(f["item"] for f in sc["fixtures"] if f["kind"] == k)
        good = isinstance(v, list) and v[0] + v[1] > 0 and set(v[2]) == {item}
        if lane == "left":
            good = good and v[0] > 0 and v[1] == 0
        if lane == "right":
            good = good and v[1] > 0 and v[0] == 0
        n = len(sc["routes"][k]["ents"]) if k in sc["routes"] else 0
        print(f"   {'ok ' if good else 'BAD'} {k}: left/right lane {v[:2] if isinstance(v, list) else v} items "
              f"{sorted(v[2]) if isinstance(v, list) else ''} {f'({n} entities)' if n else ''}")
        ok &= good

    # pipes: water in the last pipe
    pres = json.loads(lua(r"""
local s = game.surfaces.nauvis
local out = {}
for k, c in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entities_filtered{position={c[1], c[2]}, radius=0.1, type={"pipe","pipe-to-ground"}}[1]
  local f, seg = nil, -1
  if e and e.has_fluid_segment(1) then f = e.get_fluid_segment_fluid(1); seg = e.get_fluid_segment_id(1)
  elseif e then f = e.get_fluid(1) end
  out[k] = f and {f.name, f.amount, seg} or "empty"
end
rcon.print(helpers.table_to_json(out))""" % json.dumps({k: [*tile_center(p["goal"]), p["pump"].x, p["pump"].y]
                                                        for k, p in sc["pipes"].items()})))
    for k, v in pres.items():
        segs = [w[2] for w in pres.values() if isinstance(w, list) and w[2] != -1]
        good = isinstance(v, list) and v[0] == "water" and v[1] > 0 and len(set(segs)) == len(segs)  # separate nets
        print(f"   {'ok ' if good else 'BAD'} {k}: {v} ({len(sc['pipes'][k]['ents'])} entities)")
        ok &= good

    # poles: every consumer powered, all in the source pole's network
    cons = [[e.name, e.x, e.y] for e in pc["consumers"]]
    r = json.loads(lua(r"""
local s = game.surfaces.nauvis
local src = s.find_entity("small-electric-pole", {%f, %f})
local net = src.electric_network_id
local bad, n = {}, 0
for _, c in pairs(helpers.json_to_table([==[%s]==])) do
  local e = s.find_entity(c[1], {c[2], c[3]})
  n = n + 1
  if not (e and e.is_connected_to_electric_network() and e.electric_network_id == net) then bad[#bad+1] = c[1].."@"..c[2]..","..c[3] end
end
local poles = s.find_entities_filtered{name="small-electric-pole"}
local other = 0
for _, p in pairs(poles) do if p.electric_network_id ~= net then other = other + 1 end end
rcon.print(helpers.table_to_json({n=n, bad=bad, poles=#poles, other_net=other}))""" % (pc["source"].x, pc["source"].y,
                                                                                    json.dumps(cons))))
    good = not r["bad"] and r["other_net"] == 0
    print(f"   {'ok ' if good else 'BAD'} poles: {len(pc['poles'])} placed, {r['n']} consumers, unpowered {r['bad']}, "
          f"poles outside the source network {r['other_net']}")
    ok &= good
    errs = [ln for ln in (ROOT / "testenv" / "inst" / name / "server.log").read_text(errors="replace").splitlines()
            if "Error" in ln or "error" in ln]
    if errs:
        print("   server.log errors:", errs[:5])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds", nargs="*", type=int, default=[1001, 2002, 3003])
    ap.add_argument("--slot", type=int, default=7)
    ap.add_argument("--name", default="router")
    a = ap.parse_args()
    results = {}
    try:
        for seed in a.seeds:
            results[seed] = run_seed(seed, a.name, a.slot)
    finally:
        restart(a.name, a.slot, stop=True)
    print("RESULT", "PASS" if all(results.values()) else "FAIL", results)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
