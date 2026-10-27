"""Build planner test fixtures from fresh vanilla Space Age maps (headless, own test slot only).

  .venv\\Scripts\\python.exe testenv\\make_fixtures.py create 1001 1002 ...   # testenv/write/seed_<n>.zip
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py screen 1001 1002 ...   # resources + water within 150 of spawn
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py build 1001 1002 ...    # export -> testenv/fixtures/seed_<n>/
  .venv\\Scripts\\python.exe testenv\\make_fixtures.py readme                 # rewrite testenv/fixtures/README.md
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
    for seed in fixture_seeds():
        world, gd = load_fixture(seed)
        sc = json.loads((FIXTURES / f"seed_{seed}" / "screen.json").read_text())
        sp = sc["spawn"]
        lines += [f"## seed {seed}", "",
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
    finally:
        if cmd in ("screen", "build"):
            restart(name, slot, stop=True)


if __name__ == "__main__":
    main(sys.argv[1:])
