r"""Live test (headless slot 4): helper builds burner ghosts from an EMPTY inventory on a fresh map,
smelting its own plates. Run after: testenv/restart.ps1 -Name helper -Slot 4
  .venv\Scripts\python.exe tests\live_helper_bootstrap.py [bootstrap|chest]
"""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SLOT = 4
exec(open(ROOT / "testenv" / "t.py").read())
L = rcon.lua

def lj(code):
    return json.loads(L("rcon.print(helpers.table_to_json((function() " + code + " end)()))"))

LOG = []

def setup():
    L('game.forces.player.chart(game.surfaces.nauvis,{{-200,-200},{200,200}}) rcon.print("ok")')
    B("agent_remove", drop_items=False)

# first n 2x2 spots fully on `ore` near (x,y), and n free (no ore) spots of size w x h east/west of it
FIND = '''
local s=game.surfaces.nauvis
local function ore4(x,y,ore) for _,d in pairs{{-0.5,-0.5},{0.5,-0.5},{-0.5,0.5},{0.5,0.5}} do
  if s.count_entities_filtered{position={x+d[1],y+d[2]},radius=0.3,name=ore}==0 then return false end end return true end
local out={drills={}}
for r=0,20 do for dx=-r,r do for dy=-r,r do if (math.abs(dx)==r or math.abs(dy)==r) and #out.drills<%d then
  local x,y=%d+dx*2,%d+dy*2 local ok=ore4(x,y,"%s")
  for _,q in pairs(out.drills) do if math.abs(q[1]-x)<3 and math.abs(q[2]-y)<3 then ok=false end end
  if ok and s.can_place_entity{name="burner-mining-drill",position={x,y},force="player"} then out.drills[#out.drills+1]={x,y} end
end end end end
return out'''

def free_row(x0, y0, w, h):
    """top-left of a w x h free (no ore/entity) rectangle scanning west of x0"""
    return lj(f'''local s=game.surfaces.nauvis
for dx=0,60 do for dy=-10,10 do local x,y={x0}-dx,{y0}+dy
  if s.count_entities_filtered{{area={{{{x-1,y-1}},{{x+{w}+1,y+{h}+1}}}},limit=1}}==0 then
    local ok=true for i=0,{w}-1 do for j=0,{h}-1 do local t=s.get_tile(x+i,y+j) if t.collides_with("player") then ok=false end end end
    if ok then return {{x,y}} end end end end return {{}}''')

def run_until_done(limit_s=600, center=None):
    t0_tick = int(L("rcon.print(game.tick)")); t0 = time.time(); last = None; seen = {}
    while time.time() - t0 < limit_s:
        st = B("agent_status")
        for l in st.get("log") or []:
            k = (l["tick"], l["msg"])
            if k not in seen and not l["msg"].startswith("done: place"):
                seen[k] = 1; LOG.append(l["msg"]); print(f"    [{(l['tick'] - t0_tick) / 60:6.1f}s] {l['msg']}")
        cur = st.get("current"); q = st.get("queue")
        line = (cur and cur.get("type"), len(q or []), st.get("halted"))
        if not cur and not q:
            break
        time.sleep(0.5)
    ticks = int(L("rcon.print(game.tick)")) - t0_tick
    return ticks, time.time() - t0, B("agent_status")

def ghosts_left(x, y, r=32):
    return int(L(f'rcon.print(game.surfaces.nauvis.count_entities_filtered{{position={{{x},{y}}},radius={r},type="entity-ghost"}})'))

def built(x, y, names, r=32):
    return lj(f'''local o={{}} for _,n in pairs({{{",".join('"%s"' % n for n in names)}}}) do
      o[n]=game.surfaces.nauvis.count_entities_filtered{{position={{{x},{y}}},radius={r},name=n}} end return o''')

def bootstrap():
    setup()
    found = lj(FIND % (2, 63, 18, "iron-ore"))
    drills = found["drills"]; assert len(drills) == 2, found
    fx, fy = free_row(int(min(d[0] for d in drills)) - 4, int(drills[0][1]), 12, 6)
    g = []
    for d in drills: g.append(dict(name="burner-mining-drill", x=d[0], y=d[1], direction=8))
    g += [dict(name="stone-furnace", x=fx + 1, y=fy + 1), dict(name="stone-furnace", x=fx + 4, y=fy + 1)]
    g += [dict(name="burner-inserter", x=fx + 0.5, y=fy + 2.5, direction=8), dict(name="burner-inserter", x=fx + 3.5, y=fy + 2.5, direction=8)]
    g += [dict(name="transport-belt", x=fx + i + 0.5, y=fy + 3.5, direction=4) for i in range(10)]
    r = B("place_ghosts", ghosts=g, plan="test"); print("ghosts", r)
    assert r["placed"] == 16, r
    cx, cy = fx + 4, fy + 2
    print("site", cx, cy, "drills", drills)
    L("game.speed=20 rcon.print(1)")
    print(B("quick_action", action="build_ghosts", x=cx, y=cy))
    st0 = B("agent_status"); assert not st0["inventory"], st0["inventory"]
    ticks, wall, st = run_until_done(900)
    L("game.speed=1 rcon.print(1)")
    left = ghosts_left(cx, cy)
    b = built(cx, cy, ["burner-mining-drill", "stone-furnace", "burner-inserter", "transport-belt"], 40)
    print(f"bootstrap: {ticks} ticks = {ticks/60:.0f} s game, {wall:.0f} s wall; ghosts left {left}; built {b}")
    print("inventory", st["inventory"])
    assert left == 0, "ghosts left"
    assert b["burner-mining-drill"] >= 2 and b["stone-furnace"] >= 3 and b["burner-inserter"] >= 2 and b["transport-belt"] >= 10

def chest():
    """regression: materials (plates) in a chest -> fetch + craft path, no smelting"""
    setup()
    fx, fy = free_row(-20, 0, 12, 8)
    L(f'local c=game.surfaces.nauvis.create_entity{{name="wooden-chest",position={{{fx}+0.5,{fy}+6.5}},force="player"}} c.insert{{name="iron-plate",count=100}} c.insert{{name="stone",count=20}} rcon.print(1)')
    g = [dict(name="stone-furnace", x=fx + 1, y=fy + 1), dict(name="burner-inserter", x=fx + 0.5, y=fy + 2.5, direction=8)]
    g += [dict(name="transport-belt", x=fx + i + 0.5, y=fy + 3.5, direction=4) for i in range(10)]
    r = B("place_ghosts", ghosts=g, plan="test"); assert r["placed"] == 12, r
    L("game.speed=20 rcon.print(1)")
    print(B("quick_action", action="build_ghosts", x=fx + 4, y=fy + 2))
    ticks, wall, st = run_until_done(300)
    L("game.speed=1 rcon.print(1)")
    left = ghosts_left(fx + 4, fy + 2)
    nf = int(L('rcon.print(game.surfaces.nauvis.count_entities_filtered{name="stone-furnace"})'))
    print(f"chest: {ticks} ticks, ghosts left {left}, furnaces on map {nf}; log", [l["msg"] for l in st["log"]])
    assert left == 0 and nf == 1, "chest path should not build its own smelter"

def count(name):
    return int(L(f'rcon.print(game.surfaces.nauvis.count_entities_filtered{{name="{name}"}})'))

def existing():
    """an existing fueled stone furnace near the site is used; the helper builds no furnace of its own"""
    setup()
    fx, fy = free_row(40, 30, 12, 8)
    L(f'local f=game.surfaces.nauvis.create_entity{{name="stone-furnace",position={{{fx}+1,{fy}+6}},force="player"}} f.get_fuel_inventory().insert{{name="coal",count=5}} rcon.print(1)')
    nf0 = count("stone-furnace")
    g = [dict(name="transport-belt", x=fx + i + 0.5, y=fy + 1.5, direction=4) for i in range(6)]
    assert B("place_ghosts", ghosts=g, plan="test")["placed"] == 6
    L("game.speed=20 rcon.print(1)")
    B("quick_action", action="build_ghosts", x=fx + 4, y=fy + 2)
    ticks, wall, st = run_until_done(300)
    L("game.speed=1 rcon.print(1)")
    left, nf = ghosts_left(fx + 4, fy + 2), count("stone-furnace")
    print(f"existing: {ticks / 60:.0f} s game, ghosts left {left}, furnaces {nf0}->{nf}")
    assert left == 0 and nf == nf0

def capacity():
    """nearly full inventory: the haul is split over rounds instead of overfilling"""
    setup()
    fx, fy = free_row(40, -20, 28, 4)
    g = [dict(name="iron-chest", x=fx + i + 0.5, y=fy + 1.5) for i in range(26)]
    assert B("place_ghosts", ghosts=g, plan="test")["placed"] == 26
    B("quick_action", action="come", x=fx + 4, y=fy + 2)  # spawn
    B("agent_give", item="stone-brick", count=7300)
    free = 80 - 73  # 7300 bricks = 73 of 80 slots
    print("inventory", B("agent_status")["inventory"])
    L("game.speed=20 rcon.print(1)")
    B("quick_action", action="build_ghosts", x=fx + 4, y=fy + 2)
    ticks, wall, st = run_until_done(900)
    L("game.speed=1 rcon.print(1)")
    left = ghosts_left(fx + 4, fy + 2)
    rounds = sum(1 for l in LOG if l.startswith("gathering"))
    print(f"capacity: {free} free slots, {ticks / 60:.0f} s game, gathering rounds {rounds}, ghosts left {left}")
    assert rounds >= 2, "expected the haul to be split"
    assert left == 0

def fresh():
    """restart this test slot (fresh map, current mod code) and reconnect"""
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "testenv" / "restart.ps1"), "-Name", "helper", "-Slot", str(SLOT)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    exec(open(ROOT / "testenv" / "t.py").read(), globals())
    globals()["L"] = rcon.lua

if __name__ == "__main__":
    # each scenario on a fresh map (--no-restart reuses the running slot)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    which = args or ["chest", "bootstrap", "existing", "capacity"]
    for w in which:
        if "--no-restart" not in sys.argv: fresh()
        LOG.clear(); globals()[w]()
    print("PASS", which)
