"""Live check of planner blocks on a headless test slot (not collected by pytest: needs a server).

    .venv\\Scripts\\python.exe tests\\live_blocks.py [--slot 6] [--seed 1001] [--seconds 120] [case ...]

Restarts slot <slot> on testenv/write/seed_<seed>.zip, places every block as ghosts (place_ghosts) at free spots
taken from the matching fixture, instant-builds them, adds test feeders (infinity chests + fuelled burner inserters
on belt_in ports, infinity pipes on pipe_in, an electric-energy-interface wired to pole ports as source or load),
fuels the burners marked fuel=manual, runs game.speed=10 for <seconds> game seconds and checks the machines work
and the output ports carry the right items.
"""
import argparse
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from factorio_claude import rcon  # noqa: E402
from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner.gamedata import GameData, DIR_VEC  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402
from blockcheck import check  # noqa: E402

LUA_PRELUDE = r"""
local s, f = game.surfaces.nauvis, game.forces.player
local V = {[0] = {0, -1}, [4] = {1, 0}, [8] = {0, 1}, [12] = {-1, 0}}
local function dirof(v) for d, w in pairs(V) do if w[1] == v[1] and w[2] == v[2] then return d end end end
local function mk(name, x, y, dir)
  local e = s.create_entity{name = name, position = {x, y}, direction = dir or 0, force = f}
  if not e then error("cannot create " .. name .. " at " .. x .. "," .. y) end
  return e
end
local function belt_at(x, y) return s.find_entities_filtered{type = "transport-belt", position = {x + .5, y + .5}}[1] end
"""


def lua(code):
    out = rcon.lua(LUA_PRELUDE + code)
    if "Error" in out or "error" in out[:200]:
        raise RuntimeError(out[:2000])
    return out


def lua_json(code):
    out = lua(code)
    return json.loads(out.strip().splitlines()[-1])


def restart(slot, seed, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", "blocks", "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", f"seed_{seed}.zip"]
    log = ROOT / "testenv" / "inst" / "blocks_restart.txt"
    with open(log, "w") as fh:  # not a pipe: the started server would keep it open
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=120)
    return log.read_text(errors="replace")


def all_enabled(gd):
    raw = copy.deepcopy(gd.raw)
    for r in raw["recipes"].values():
        r["enabled"] = True
    return GameData(raw)


def nearest(world, name, k=0):
    ps = sorted((p for p in world.patches() if p.name == name), key=lambda p: p.center[0] ** 2 + p.center[1] ** 2)
    return ps[k]


def column_spot(grid, gd, patch, block):
    """Origin where every drill of a burner column sits fully on the patch and everything is placeable."""
    drills = [e for e in block.entities if "drill" in e.name]
    x0, y0, x1, y1 = patch.bbox
    for oy in range(y0, y1):
        for ox in range(x0, x1):
            if all(gd.footprint(e.name, e.x + ox, e.y + oy, e.direction) <= patch.tiles for e in drills):
                tiles = set()
                for e in block.entities:
                    tiles |= gd.footprint(e.name, e.x + ox, e.y + oy, e.direction)
                if grid.can_place(tiles):
                    return ox, oy
    raise RuntimeError("no spot for the burner column")


def build_cases(world, gd0, gda, grid, only):
    cases = []

    def add(label, block, checks, margin=7):
        if only and label not in only:
            return
        if block.origin == (0, 0):
            w, h = block.size
            spot = grid.free_rect(w + 2 * margin, h + 2 * margin, near=(0, 0), avoid_resources=True)
            if spot is None:
                raise RuntimeError(f"no room for {label}")
            block = block.at(spot[0] + margin, spot[1] + margin)
        x, y = block.origin
        grid.occupy({(i, j) for i in range(x - margin, x + block.size[0] + margin)
                     for j in range(y - margin, y + block.size[1] + margin)}, label)
        for p in block.placed_ports():  # test belts after the output ports
            if p.kind == "belt_out":
                dx, dy = DIR_VEC[p.direction]
                grid.occupy({(p.x + k * dx + i, p.y + k * dy + j) for k in range(6) for i in (-1, 0, 1)
                             for j in (-1, 0, 1)}, label)
        cases.append({"label": label, "block": block, "checks": checks})

    add("smelter_burner_mixed", B.smelter_array(6, "stone-furnace", "mixed_lane", "north", gd=gd0),
        {"furnaces": "iron-plate"})
    add("smelter_burner_separate", B.smelter_array(4, "stone-furnace", "separate", "east", gd=gd0, ore="copper-ore"),
        {"furnaces": "copper-plate"})
    add("smelter_electric_steel", B.smelter_array(4, "steel-furnace", "mixed_lane", "west", "inserter", gd=gda),
        {"furnaces": "iron-plate", "power": "source"})
    add("smelter_electric_furnace", B.smelter_array(3, "electric-furnace", output_side="south", inserters="inserter",
                                                   gd=gda, ore="stone"), {"furnaces": "stone-brick", "power": "source"})
    add("steam_3x6", B.steam_power(3, gd=gda), {"steam": True, "power": "load"})
    add("steam_2x3_south", B.steam_power(2, 3, gd=gda, water_side="south", pole="small-electric-pole"),
        {"steam": True, "power": "load"})
    add("lane_merge", B.lane_merge("iron-ore", "coal", gd0, output_side="east"), {"lanes": ["iron-ore", "coal"]})
    add("bus_start", B.bus_start(["iron-plate", "copper-plate", "coal", "stone", "iron-gear-wheel"], 4, gd0,
                                 output_side="west"), {"bus": True, "inline_feed": True})
    add("inserter_tap", B.inserter_tap(gd0, output_side="south"), {"outs": True})
    add("coal_splitter", B.coal_splitter(gda, output_side="north"), {"outs": True})
    iron = nearest(world, "iron-ore")
    add("outpost_electric", B.mining_outpost(iron, "electric-mining-drill", "transport-belt", "small-electric-pole",
                                             "west", gd=gda, grid=grid, max_drills=12),
        {"drills": "iron-ore", "power": "source"}, margin=2)
    copper = nearest(world, "copper-ore")
    add("outpost_burner", B.mining_outpost(copper, output_side="south", gd=gd0, grid=grid, max_drills=6),
        {"drills": "copper-ore"}, margin=2)
    coal = nearest(world, "coal")
    col = B.burner_mining_column(4, gd=gd0, output_side="north", resource="coal")
    add("burner_column_coal", col.at(*column_spot(grid, gd0, coal, col)), {"drills": "coal"}, margin=2)
    return cases


def setup(case, gd):
    b = case["block"]
    x, y = b.origin
    w, h = b.size
    m = 7
    lua(f"""
for _, e in pairs(s.find_entities_filtered{{area = {{{{{x - m}, {y - m}}}, {{{x + w + m}, {y + h + m}}}}},
    type = {{"tree", "simple-entity", "cliff", "fish"}}}}) do e.destroy() end
rcon.print("ok")""")
    ghosts = [{"name": e.name, "x": e.x, "y": e.y, "direction": e.direction} for e in b.placed()]
    r = rcon.bridge("place_ghosts", ghosts=ghosts, plan="blocks-test")
    if r["failed"]:
        raise RuntimeError(f"{case['label']}: ghosts failed {r['failed'][:5]}")
    r2 = rcon.bridge("instant_build", area=[[x - 1, y - 1], [x + w + 1, y + h + 1]])
    if r2["built"] != len(ghosts):
        raise RuntimeError(f"{case['label']}: built {r2['built']}/{len(ghosts)}")
    feeds, outs, pipes, power = [], [], [], None
    for p in b.placed_ports():
        if p.kind == "belt_in":
            feeds.append({"x": p.x, "y": p.y, "dir": p.direction, "item": p.item, "lane": p.lane,
                          "inline": bool(case["checks"].get("inline_feed"))})
        elif p.kind == "belt_out":
            outs.append({"x": p.x, "y": p.y, "dir": p.direction})
        elif p.kind == "pipe_in":
            pipes.append({"x": p.x, "y": p.y, "item": p.item})
        elif p.kind == "pole" and "power" in case["checks"]:
            power = {"x": p.x, "y": p.y, "mode": case["checks"]["power"],
                     "kw": b.rates.get("power_kw", 0), "ex": x - 4, "ey": y - 4}
    outs = list({(o["x"], o["y"]): o for o in outs}.values())
    manual = [{"name": e.name, "x": e.x, "y": e.y} for e in b.placed() if (e.extra or {}).get("fuel") == "manual"]
    data = json.dumps({"feeds": feeds, "outs": outs, "pipes": pipes, "power": power, "manual": manual})
    lua(f"""
local D = helpers.json_to_table([==[{data}]==])
for _, fd in pairs(D.feeds) do
  local v = V[fd.dir]
  local L, R = {{v[2], -v[1]}}, {{-v[2], v[1]}}
  local sides = fd.lane == "left" and {{R}} or (fd.lane == "right" and {{L}} or {{L, R}})
  if fd.inline then  -- chest -> inserter -> port belt in line (neighbouring ports leave no room at the sides)
    mk("transport-belt", fd.x + .5, fd.y + .5, fd.dir)
    local ins = mk("burner-inserter", fd.x - v[1] + .5, fd.y - v[2] + .5, dirof({{-v[1], -v[2]}}))
    ins.insert{{name = "coal", count = 20}}
    local ch = mk("infinity-chest", fd.x - 2 * v[1] + .5, fd.y - 2 * v[2] + .5)
    ch.set_infinity_container_filter(1, {{name = fd.item, count = 50, index = 1}})
    sides = {{}}
  end
  for k = 0, (fd.inline and -1 or 3) do
    local bx, by = fd.x - k * v[1], fd.y - k * v[2]
    if not belt_at(bx, by) then mk("transport-belt", bx + .5, by + .5, fd.dir) end
    for _, S in pairs(sides) do
      local ins = mk("burner-inserter", bx + S[1] + .5, by + S[2] + .5, dirof(S))
      ins.insert{{name = "coal", count = 20}}
      local ch = mk("infinity-chest", bx + 2 * S[1] + .5, by + 2 * S[2] + .5)
      ch.set_infinity_container_filter(1, {{name = fd.item, count = 50, index = 1}})
    end
  end
end
for _, o in pairs(D.outs) do
  local v = V[o.dir]
  for k = 0, 4 do
    local bx, by = o.x + k * v[1], o.y + k * v[2]
    if not belt_at(bx, by) then mk("transport-belt", bx + .5, by + .5, o.dir) end
  end
end
for _, p in pairs(D.pipes) do
  local e = mk("infinity-pipe", p.x + .5, p.y + .5)
  e.set_infinity_pipe_filter{{name = p.item, percentage = 1, temperature = 15}}
end
for _, mm in pairs(D.manual) do
  local e = s.find_entities_filtered{{name = mm.name, position = {{mm.x, mm.y}}}}[1]
  if e then e.insert{{name = "coal", count = 10}} end
end
if D.power then
  local P = D.power
  local tx, ty = P.ex, P.ey
  for _, e in pairs(s.find_entities_filtered{{area = {{{{tx - 1, ty - 1}}, {{tx + 4, ty + 3}}}}}}) do
    if e.type ~= "resource" and e.type ~= "character" then e.destroy() end
  end
  s.set_tiles({{{{name = "grass-1", position = {{tx, ty}}}}, {{name = "grass-1", position = {{tx + 1, ty}}}},
    {{name = "grass-1", position = {{tx, ty + 1}}}}, {{name = "grass-1", position = {{tx + 1, ty + 1}}}},
    {{name = "grass-1", position = {{tx + 2, ty}}}}}})
  local eei = mk("electric-energy-interface", tx + 1, ty + 1)
  eei.electric_buffer_size = 1e9
  if P.mode == "source" then eei.power_production = 1e8 / 60; eei.power_usage = 0
  else eei.power_production = 0; eei.power_usage = P.kw * 1000 / 60 end
  local pole = mk("small-electric-pole", tx + 2.5, ty + .5)
  local target = s.find_entities_filtered{{type = "electric-pole", position = {{P.x + .5, P.y + .5}}}}[1]
  local a = pole.get_wire_connector(defines.wire_connector_id.pole_copper, true)
  a.connect_to(target.get_wire_connector(defines.wire_connector_id.pole_copper, true), false)
end
rcon.print("ok")""")
    case["outs"] = outs


def measure(case):
    b = case["block"]
    x, y = b.origin
    w, h = b.size
    data = json.dumps({"outs": case["outs"]})
    return lua_json(f"""
local D = helpers.json_to_table([==[{data}]==])
local st = {{}}
for k, v in pairs(defines.entity_status) do st[v] = k end
local r = {{furnaces = {{}}, drills = {{}}, engines = {{}}, boilers = {{}}, outs = {{}}, unpowered = 0, nofuel = {{}}}}
local area = {{{{{x}, {y}}}, {{{x + w}, {y + h}}}}}
for _, e in pairs(s.find_entities_filtered{{area = area, force = f}}) do
  if e.type == "furnace" then r.furnaces[#r.furnaces + 1] = {{e.products_finished, st[e.status] or ""}}
  elseif e.type == "mining-drill" then r.drills[#r.drills + 1] = st[e.status] or ""
  elseif e.type == "generator" then r.engines[#r.engines + 1] = e.energy_generated_last_tick * 60 / 1000
  elseif e.type == "boiler" then r.boilers[#r.boilers + 1] = st[e.status] or "" end
  if e.burner and not e.burner.currently_burning and e.burner.inventory.is_empty() then
    r.nofuel[#r.nofuel + 1] = e.name .. "@" .. e.position.x .. "," .. e.position.y
  end
end
for i, o in pairs(D.outs) do
  local lanes = {{{{}}, {{}}}}
  local v = V[o.dir]
  for k = 0, 4 do
    local bt = belt_at(o.x + k * v[1], o.y + k * v[2])
    if bt then for li = 1, 2 do
      for _, c in pairs(bt.get_transport_line(li).get_contents()) do
        lanes[li][c.name] = (lanes[li][c.name] or 0) + c.count
      end
    end end
  end
  r.outs[i] = lanes
end
rcon.print(helpers.table_to_json(r))""")


def judge(case, m):
    c, errs = case["checks"], []
    outs = m["outs"]

    def items(lane=None):
        tot = {}
        for o in outs:
            for li, lane_items in enumerate(o):
                if lane is None or li == lane:
                    for k, v in (lane_items or {}).items():
                        tot[k] = tot.get(k, 0) + v
        return tot
    if "furnaces" in c:
        idle = [f for f in m["furnaces"] if f[0] <= 0]
        if idle or not m["furnaces"]:
            errs.append(f"furnaces not producing: {idle}")
        b = case["block"]
        want = b.rates[c["furnaces"]] / b.info["furnaces"] * case["seconds"] / 60  # per furnace, planned
        slow = [f[0] for f in m["furnaces"] if f[0] < 0.85 * want]
        if slow:
            errs.append(f"furnaces below 85% of the planned {want:.1f} crafts: {slow}")
        if items().get(c["furnaces"], 0) <= 0:
            errs.append(f"no {c['furnaces']} at the output port: {outs}")
    if "drills" in c:
        bad = [d for d in m["drills"] if d not in ("working", "waiting_for_space_in_destination")]
        if bad or not m["drills"]:
            errs.append(f"drills not working: {bad}")
        if items().get(c["drills"], 0) <= 0:
            errs.append(f"no {c['drills']} at the output port")
    if "steam" in c:
        if not m["engines"] or min(m["engines"]) <= 0:
            errs.append(f"engines idle: {m['engines']}")
        if any(s != "working" for s in m["boilers"]):
            errs.append(f"boilers: {m['boilers']}")
    if "lanes" in c:
        left, right = items(0), items(1)
        if set(left) != {c["lanes"][0]} or set(right) != {c["lanes"][1]}:
            errs.append(f"lanes wrong: left {left} right {right}")
    if "bus" in c:
        if any(not (o[0] or o[1]) for o in outs):
            errs.append(f"empty bus lane: {outs}")
    if "outs" in c:
        if any(not (o[0] or o[1]) for o in outs):
            errs.append(f"an output is empty: {outs}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1001)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--keep", action="store_true", help="leave the server running")
    ap.add_argument("cases", nargs="*")
    a = ap.parse_args()
    world, gd0 = load_fixture(a.seed)
    gda = all_enabled(gd0)
    grid = world.grid(world.bbox)
    cases = build_cases(world, gd0, gda, grid, set(a.cases))
    for cs in cases:
        errs = check(cs["block"], gda, ore=None)
        assert not errs, (cs["label"], errs)
    print(restart(a.slot, a.seed).strip().splitlines()[-1])
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    lua("""f.research_all_technologies()
s.request_to_generate_chunks({0, 0}, 8)
s.force_generate_chunk_requests()
rcon.print("ok")""")
    for cs in cases:
        setup(cs, gda)
        print(f"placed {cs['label']}: {len(cs['block'].entities)} entities at {cs['block'].origin}")
    lua(f"game.speed = 10 rcon.print('ok')")
    t0 = lua_json("rcon.print(game.tick)")
    while lua_json("rcon.print(game.tick)") - t0 < a.seconds * 60:
        time.sleep(1)
    lua("game.speed = 1 rcon.print('ok')")
    failed = []
    for cs in cases:
        cs["seconds"] = a.seconds
        m = measure(cs)
        errs = judge(cs, m)
        summary = {k: m[k] for k in ("furnaces", "drills", "engines", "boilers", "nofuel") if m[k]}
        print(("PASS " if not errs else "FAIL ") + cs["label"], json.dumps(summary)[:400])
        print("   outs:", json.dumps(m["outs"])[:300])
        for e in errs:
            print("   ", e)
        if errs:
            failed.append(cs["label"])
    log = (ROOT / "testenv" / "inst" / "blocks" / "server.log").read_text(errors="replace")
    script_errs = [ln for ln in log.splitlines() if "Error" in ln and "claude" in ln.lower()]
    if script_errs:
        print("server.log errors:", script_errs[:5])
        failed.append("server.log")
    if not a.keep:
        restart(a.slot, a.seed, stop=True)
    print("RESULT:", "PASS" if not failed else f"FAIL {failed}", f"({len(cases)} cases, {a.seconds} s game time)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
