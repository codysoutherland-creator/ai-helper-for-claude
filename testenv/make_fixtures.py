"""Build planner test fixtures from fresh vanilla Space Age maps (headless, own test slot only).

  .venv\\Scripts\\python.exe testenv\\make_fixtures.py create 1001 1002 ...   # testenv/write/seed_<n>.zip
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py screen 1001 1002 ...   # resources + water within 150 of spawn
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py build 1001 1002 ...    # export -> testenv/fixtures/seed_<n>/
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py readme                 # rewrite testenv/fixtures/README.md
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py sprawl 4004            # synthetic planner-v2 map (SPRAWL)
Options: --slot N (default 5), --name NAME (default fixtures). The instance is stopped at the end.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factorio_claude import platforms, rcon  # noqa: E402

EXE = str(platforms.find_factorio()[0] or "factorio")  # env FACTORIO_EXE / FACTORIO_BIN, else Steam discovery
TESTENV = ROOT / "testenv"
FIXTURES = TESTENV / "fixtures"
HALF = 200          # exported area = spawn +- HALF tiles (400x400)
NEAR = 150          # "near spawn" radius for the screening criteria
ORES = ("iron-ore", "copper-ore", "coal", "stone")


def create(seed):
    out = TESTENV / "write" / f"seed_{seed}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    cfg = TESTENV / "config.ini"   # local, generated (gitignored): keeps Factorio's write-data inside testenv/write
    if not cfg.exists():
        cfg.write_text("[path]\nread-data=__PATH__executable__/../../data\n"
                       f"write-data={(TESTENV / 'write').as_posix()}\n", encoding="ascii")
    subprocess.run([EXE, "--config", str(TESTENV / "config.ini"), "--mod-directory", str(TESTENV / "mods"),
                    "--create", str(out), "--map-gen-seed", str(seed)], check=True, capture_output=True)
    print("created", out)


def restart(name, slot, save=None, stop=False):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TESTENV / "restart.ps1"),
           "-Name", name, "-Slot", str(slot)]
    cmd += ["-Stop"] if stop else ["-Save", save]
    # not a pipe: the started factorio.exe inherits stdout and would keep a pipe open forever
    with tempfile.TemporaryFile("w+") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        f.seek(0)
        out = f.read().strip()
    print(out.splitlines()[-1] if out else "(no output)", flush=True)


def connect(slot):
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + slot, "testpw")
    rcon._lua_armed = False
    for _ in range(40):
        try:
            rcon.bridge("ping")
            return
        except Exception:
            time.sleep(0.5)
    rcon.bridge("ping")


def lua_json(code):
    out = rcon.lua(code).strip()
    return json.loads(out.splitlines()[-1])


# Generate chunks around spawn (headless servers don't chart; export_map uses charted=false) and measure.
SCREEN_LUA = """
local s, f = game.surfaces.nauvis, game.forces.player
local sp = f.get_spawn_position(s)
s.request_to_generate_chunks(sp, %d) s.force_generate_chunk_requests()
f.chart(s, {{sp.x-%d, sp.y-%d}, {sp.x+%d, sp.y+%d}})
local R = %d
local area = {{sp.x-R, sp.y-R}, {sp.x+R, sp.y+R}}
local res = {}
for _, e in pairs(s.find_entities_filtered{area=area, type="resource"}) do
  local dx, dy = e.position.x - sp.x, e.position.y - sp.y
  local d = math.sqrt(dx*dx + dy*dy)
  local r = res[e.name] or {tiles=0, amount=0, nearest=1e9}
  res[e.name] = r
  r.tiles = r.tiles + 1  r.amount = r.amount + e.amount
  if d < r.nearest then r.nearest = math.floor(d) end
end
local wn = {}
for n, t in pairs(prototypes.tile) do if t.fluid and t.fluid.name == "water" then wn[#wn+1] = n end end
local nw = 1e9
local wt = s.find_tiles_filtered{area=area, name=wn}
for _, t in pairs(wt) do
  local dx, dy = t.position.x + 0.5 - sp.x, t.position.y + 0.5 - sp.y
  local d = math.sqrt(dx*dx + dy*dy) if d < nw then nw = math.floor(d) end
end
rcon.print(helpers.table_to_json({spawn=sp, resources=res, water_tiles=#wt, water_nearest=nw}))
"""


def screen(seed, name, slot):
    restart(name, slot, f"seed_{seed}.zip")
    connect(slot)
    radius = (HALF + 31) // 32 + 1
    r = lua_json(SCREEN_LUA % (radius, HALF, HALF, HALF, HALF, NEAR))
    ok = all(o in r["resources"] for o in ORES) and r["water_tiles"] > 0
    print(f"seed {seed}: {'OK ' if ok else 'BAD'} water {r['water_tiles']} tiles (nearest {r['water_nearest']}) "
          + ", ".join(f"{n} {v['tiles']}t/{v['nearest']}" for n, v in sorted(r["resources"].items())))
    return r, ok


def build(seed, name, slot):
    r, ok = screen(seed, name, slot)
    out = FIXTURES / f"seed_{seed}"
    so = TESTENV / "inst" / name / "write" / "script-output"
    os.environ["FACTORIO_SCRIPT_OUTPUT"] = str(so)
    from factorio_claude.planner.world import World
    sp = r["spawn"]
    area = (int(sp["x"]) - HALF, int(sp["y"]) - HALF, int(sp["x"]) + HALF, int(sp["y"]) + HALF)
    world = World.from_link(area=area, link=rcon.bridge, charted=False)
    dres = rcon.bridge("export_data")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(world.dir, out / "map")
    shutil.copy2(so / dres["path"], out / "data.json")
    (out / "screen.json").write_text(json.dumps({"seed": seed, "area": area, "near_radius": NEAR, **r}, indent=1))
    print(f"seed {seed}: {len(list((out / 'map').glob('c_*.json')))} chunks -> {out}")
    return ok


# ---- seed 4004 "sprawl": synthetic map for planner v2 (ore far apart, lake off-centre, old builds in the middle) ----
SPRAWL = {  # resource: (center x, center y, radius, amount per tile) - distances from spawn ~180/150/90/200
    "iron-ore": (128, 126, 25, 1100), "copper-ore": (-150, 8, 19, 900), "coal": (6, -90, 16, 900),
    "stone": (-140, -142, 13, 800)}
LAKE = (49, 49, 15)        # off-centre lake, ~70 tiles from spawn (south-east)
SPRAWL_STATE = {           # synthetic player state (no helper yet)
    "player_pos": [0.5, 0.5], "player_inv": {"iron-plate": 100, "stone": 60, "coal": 20, "burner-mining-drill": 1,
                                             "wood": 10},
    "production": {}, "research_done": ["automation-science-pack", "electronics", "steam-power"], "source": "fixture"}

SPRAWL_LUA = r"""
local s, f = game.surfaces.nauvis, game.forces.player
s.request_to_generate_chunks({0, 0}, 9) s.force_generate_chunk_requests()
local A = {{-232, -232}, {232, 232}}
for _, e in pairs(s.find_entities_filtered{area = A, type = "resource"}) do e.destroy() end
-- no natural water near the build: land everywhere, then one lake
local land = {}
for _, t in pairs(s.find_tiles_filtered{area = A, collision_mask = "water_tile"}) do
  land[#land + 1] = {name = "grass-1", position = t.position}
end
s.set_tiles(land, true, true, true, true)
local P = %s
local function disc(cx, cy, r, fn)
  for y = cy - r - 2, cy + r + 2 do for x = cx - r - 2, cx + r + 2 do
    local dx, dy = x - cx, y - cy
    local wob = 1 + 0.12 * math.sin(x * 0.37) + 0.1 * math.cos(y * 0.29)
    if dx * dx + dy * dy <= (r * wob) ^ 2 then fn(x, y) end
  end end
end
for name, v in pairs(P) do
  disc(v[1], v[2], v[3], function(x, y)
    s.create_entity{name = name, position = {x + 0.5, y + 0.5}, amount = v[4]}
  end)
end
local L = %s
local water = {}
disc(L[1], L[2], L[3], function(x, y) water[#water + 1] = {name = "water", position = {x, y}} end)
s.set_tiles(water, true, true, true, true)
-- research like a player who crafted a lab (trigger techs only)
for _, t in pairs({"steam-power", "electronics", "automation-science-pack"}) do f.technologies[t].researched = true end
-- old builds in the middle (clutter): an idle furnace row fed by a long belt from nowhere, burner drills off any ore,
-- two unpowered labs, stray belts and a pole line
local function mk(name, x, y, d) return s.create_entity{name = name, position = {x, y}, direction = d or 0, force = f} end
local function clear(x0, y0, x1, y1)
  for _, e in pairs(s.find_entities_filtered{area = {{x0, y0}, {x1, y1}}, type = {"tree", "simple-entity", "cliff"}}) do
    e.destroy()
  end
end
clear(-80, -20, 40, 40)
for x = -75, 7 do mk("transport-belt", x + 0.5, -12.5, defines.direction.east) end
for i = 0, 13 do
  local x = -20 + 2 * i
  mk("burner-inserter", x + 0.5, -11.5, defines.direction.north)
  mk("stone-furnace", x + 1, -10)
end
for i = 0, 7 do mk("burner-mining-drill", 20 + 2 * i + 1, 11, defines.direction.north) end
mk("lab", -29.5, 20.5) mk("lab", -25.5, 20.5)
for x = -10, 3 do mk("transport-belt", x + 0.5, 24.5, defines.direction.west) end
for y = -6, 4 do mk("transport-belt", 30.5, y + 0.5, defines.direction.south) end
for i = 0, 8 do mk("small-electric-pole", -40.5 + 7 * i, 30.5) end
rcon.print("ok")
"""


def _lua_table(d):
    return "{" + ", ".join(f'["{k}"] = {{{", ".join(str(x) for x in v)}}}' for k, v in d.items()) + "}"


def sprawl(seed, name, slot):
    """Create seed_<seed>.zip, rewrite it into the sprawl map on a test slot, save, then build the fixture."""
    create(seed)
    restart(name, slot, f"seed_{seed}.zip")
    connect(slot)
    code = SPRAWL_LUA % (_lua_table(SPRAWL), "{" + ", ".join(str(v) for v in LAKE) + "}")
    code = " ".join(ln for ln in code.splitlines() if not ln.strip().startswith("--"))  # one line, no comments
    out = rcon.lua(code)
    if "ok" not in out:
        raise RuntimeError(out[:2000])
    rcon.lua('game.server_save("sprawl_tmp")')
    saved = TESTENV / "inst" / name / "write" / "saves" / "sprawl_tmp.zip"
    for _ in range(60):
        if saved.exists() and saved.stat().st_size > 0:
            time.sleep(2)
            break
        time.sleep(0.5)
    shutil.copy2(saved, TESTENV / "write" / f"seed_{seed}.zip")
    build(seed, name, slot)
    out_dir = FIXTURES / f"seed_{seed}"
    sc = json.loads((out_dir / "screen.json").read_text())
    sc["kind"] = "sprawl"
    sc["layout"] = {"patches": SPRAWL, "lake": LAKE}
    (out_dir / "screen.json").write_text(json.dumps(sc, indent=1))
    # machines / chests as the game reports them (status, fuel); the inventories are the synthetic ones above
    from factorio_claude.planner.state import BaseState
    st = BaseState.from_dict(dict(rcon.bridge("base_state", x=0, y=0, radius=220), source="fixture"))
    d = dict(SPRAWL_STATE, machines=st.machines, chests=st.chests, tick=st.tick)
    (out_dir / "state.json").write_text(json.dumps(d, indent=1))
    print(f"seed {seed}: sprawl fixture -> {out_dir}")


def readme():
    """Summaries come from the fixtures themselves (loaded through the planner), so README matches the data."""
    from factorio_claude.planner.testing import load_fixture, fixture_seeds
    lines = ["# Planner test fixtures", "",
             "Fresh vanilla Space Age maps (Factorio 2.1.23, default map settings, no player), one folder per seed:",
             "`seed_<n>/map/` = export_map of spawn +-200 tiles (400x400, `charted=false`, headless servers don't chart),",
             "`seed_<n>/data.json` = export_data of the fresh force, `seed_<n>/screen.json` = in-game counts within",
             f"{NEAR} tiles of spawn (resource tiles / amount / nearest distance, water). Saves: `testenv/write/seed_<n>.zip`.",
             "", "Load in tests: `from factorio_claude.planner.testing import load_fixture, fixture_seeds`;",
             "`world, gd = load_fixture(1001)` (cached, read-only; `cached=False` for a private copy).",
             "Start a live instance from the same map: `testenv/restart.ps1 -Name <name> -Slot <slot> -Save seed_<n>.zip`.",
             "Rebuild: `.venv\\Scripts\\python.exe testenv\\make_fixtures.py create|build <seeds>` then `readme`.", ""]
    for seed in fixture_seeds(None):
        world, gd = load_fixture(seed)
        sc = json.loads((FIXTURES / f"seed_{seed}" / "screen.json").read_text())
        sp = sc["spawn"]
        lines += [f"## seed {seed}" + (" (sprawl, synthetic)" if sc.get("kind") == "sprawl" else ""), ""]
        if sc.get("kind") == "sprawl":
            lines += ["Planner v2 regression map (`make_fixtures.py sprawl 4004`): natural ore and water removed around "
                      "spawn, then iron ~180 tiles, copper ~150, coal ~90 and stone ~200 tiles from spawn in different "
                      "directions, one lake ~70 tiles south-east, and old builds in the middle (an idle row of 14 stone "
                      "furnaces fed by an 83-tile belt from nowhere, 8 burner drills off any ore, 2 unpowered labs, "
                      "stray belts, a pole line). `state.json`: synthetic inventory (100 iron plates, 60 stone, 20 "
                      "coal, 10 wood, 1 burner drill), machine status from the game, steam power researched. Not in "
                      "`fixture_seeds()` (default kind \"fresh\"); use `fixture_seeds(\"sprawl\")` / `load_state(4004)`.",
                      ""]
        lines += [
                  f"Spawn ({sp['x']:g}, {sp['y']:g}); exported bbox {world.bbox}, tick {world.tick}, "
                  f"{len(world.obstacles)} obstacles, {len(world.entities)} force entities.", "",
                  "| resource | patches | total amount | nearest patch: tiles / amount / center / closest tile to spawn |",
                  "|---|---|---|---|"]
        totals = world.resource_totals()
        for name in sorted(totals):
            ps = world.patches(name=name, min_tiles=20)  # fluid wells: no minimum (World handles that)

            def dist(p):
                return min(((x + 0.5 - sp["x"]) ** 2 + (y + 0.5 - sp["y"]) ** 2) ** 0.5 for x, y in p.tiles)
            near = "-"
            if ps:
                p = min(ps, key=dist)
                near = f"{p.size} / {p.amount:,} / ({p.center[0]:.0f}, {p.center[1]:.0f}) / {dist(p):.0f}"
            lines.append(f"| {name} | {len(ps)} | {totals[name][1]:,} | {near} |")
        bodies = sorted(world.water_bodies(), key=lambda b: -b.size)
        nw = world.nearest_water(sp["x"], sp["y"])
        lines += ["", f"Water: {int(world.water.sum())} tiles in {len(bodies)} bodies (>=30 tiles); "
                  f"largest {bodies[0].size if bodies else 0} tiles; nearest body to spawn: "
                  + (f"{nw.size} tiles centered ({nw.center[0]:.0f}, {nw.center[1]:.0f}), nearest tile "
                     f"{sc['water_nearest']} from spawn." if nw else "none."), ""]
    (FIXTURES / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print("wrote", FIXTURES / "README.md")


def main(argv):
    slot, name, seeds, cmd = 5, "fixtures", [], argv[0]
    it = iter(argv[1:])
    for a in it:
        if a == "--slot":
            slot = int(next(it))
        elif a == "--name":
            name = next(it)
        else:
            seeds.append(int(a))
    if cmd == "readme":
        return readme()
    try:
        for seed in seeds:
            if cmd == "create":
                create(seed)
            elif cmd == "screen":
                screen(seed, name, slot)
            elif cmd == "build":
                build(seed, name, slot)
            elif cmd == "sprawl":
                sprawl(seed, name, slot)
    finally:
        if cmd in ("screen", "build", "sprawl"):
            restart(name, slot, stop=True)


if __name__ == "__main__":
    main(sys.argv[1:])
