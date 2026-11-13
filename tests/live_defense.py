"""Engine checks for defence v2 (mod/claude-bridge/scripts/defense.lua) on a headless test server. Not collected by
pytest: it needs Factorio.

    .venv\\Scripts\\python.exe tests\\live_defense.py --factorio <factorio.exe> [--udp-port 34533] [--rcon-port 27033]
        [--keep]

Starts its own server (temp write-data, a copy of testenv/write/seed_1001.zip, the mod from mod/claude-bridge; RCON
and the game port on 127.0.0.1) on the given test ports, then on cleared ground with a biter spawner placed by the
test (test setup: Lua over RCON, peaceful mode so nothing attacks meanwhile):

  D1 turrets_then_walls   defense_place_turrets before Stone wall is researched: 4 turret ghosts, no walls, the group is
                          recorded; once Stone wall is researched the status lists it as unwalled and
                          defense_place_walls puts a wall arc in front of it: every wall between the turrets and the
                          nest, one 4-connected piece, no tile shared with a turret; the ghosts revive into real
                          entities (instant build stands in for the helper)
  D2 walls_with_turrets   defense_place_turrets once walls are researched: turrets and their wall arc in one call
  D3 gap_free             a belt across the arc's first line moves the arc one tile out; belts across all three
                          candidate lines: no walls at all (a wall with a hole is never placed); water in the line
                          counts as closed (no wall ghost on it, the arc still placed; the tile north of the water
                          gets its wall although 2.1 refuses a wall GHOST there by can_place_entity)
  D4 cloud_and_status     defense_threats reports `cloud` (chunks to the polluted chunk next to the base); the status
                          after a game minute has nest_dist, pollution and its trend
  D5 seeded_groups        turrets built without a recorded group (0.7.x saves) are clustered on the next minute's
                          sample into one group per cluster, each with its own plan name; both get their wall arc and
                          leave the unwalled list
"""
import argparse
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

PRELUDE = r"""
local s, f = game.surfaces.nauvis, game.forces.player
local function clear(x0, y0, x1, y1)
  for _, e in pairs(s.find_entities_filtered{area = {{x0, y0}, {x1, y1}}}) do
    if e.valid and e.type ~= "character" then e.destroy() end
  end
  local t = {}
  for x = x0, x1 - 1 do for y = y0, y1 - 1 do t[#t + 1] = {name = "grass-1", position = {x, y}} end end
  s.set_tiles(t)
end
"""


def lua(code):
    from factorio_claude import rcon
    out = rcon.lua(PRELUDE + code)
    if "Error" in out[:400] or "error" in out[:200]:
        raise RuntimeError(out[:3000])
    return out


def lua_json(code):
    return json.loads(lua(code).strip().splitlines()[-1])


def call(op, **kw):
    from factorio_claude import link
    return link.call(op, **kw)


def start(factorio: Path, udp: int, rcon_port: int, game_port: int, debug_ops=False):
    """The test server (debug_ops: the mod's test/cheat ops on, e.g. instant_build for tests/live_expand_blocks.py's
    cases). -> (process, temp dir, server log)"""
    work = Path(tempfile.mkdtemp(prefix="fc-defense-"))
    mods, write = work / "mods", work / "write"
    (write / "saves").mkdir(parents=True)
    mods.mkdir()
    shutil.copytree(ROOT / "mod" / "claude-bridge", mods / "claude-bridge")
    shutil.copy2(ROOT / "testenv" / "mods" / "mod-list.json", mods / "mod-list.json")
    if debug_ops:
        subprocess.run([sys.executable, str(ROOT / "testenv" / "mod_settings_dat.py"), str(mods / "mod-settings.dat"),
                        "claude-debug-ops=true"], check=True, timeout=60)
    save = write / "saves" / "seed_1001.zip"
    shutil.copy2(ROOT / "testenv" / "write" / "seed_1001.zip", save)
    (work / "config.ini").write_text(f"[path]\nread-data=__PATH__executable__/../../data\n"
                                     f"write-data={write.as_posix()}\n", encoding="utf-8")
    (work / "server-settings.json").write_text(json.dumps({
        "name": "defense-test", "description": "test", "visibility": {"public": False, "lan": False},
        "require_user_verification": False, "auto_pause": False, "autosave_interval": 0}), encoding="utf-8")
    log = work / "server.log"
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen([str(factorio), "--config", str(work / "config.ini"), "--mod-directory", str(mods),
                             "--start-server", str(save), "--server-settings", str(work / "server-settings.json"),
                             "--bind", f"127.0.0.1:{game_port}", "--rcon-bind", f"127.0.0.1:{rcon_port}",
                             "--rcon-password", "testpw", "--enable-lua-udp", str(udp)],
                            stdout=fh, stderr=subprocess.STDOUT)
    end = time.monotonic() + 300
    while time.monotonic() < end:
        text = log.read_text(encoding="utf-8", errors="replace")
        if "Starting RCON" in text:
            break
        if proc.poll() is not None or "Error" in text:
            raise RuntimeError("server did not start:\n" + text[-3000:])
        time.sleep(0.5)
    os.environ.update(FACTORIO_UDP_PORT=str(udp), FACTORIO_RCON_PORT=str(rcon_port), FACTORIO_RCON_PASSWORD="testpw",
                      FACTORIO_UDP_TIMEOUT="8")
    from factorio_claude import rcon
    rcon._client = rcon.Rcon("127.0.0.1", rcon_port, "testpw")
    for _ in range(40):
        try:
            rcon.lua("rcon.print('ping')")
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    rcon.lua("rcon.print('warm')")
    return proc, work, log


def research(*techs):
    lua("for _, t in ipairs({" + ",".join(f'"{t}"' for t in techs) + "}) do f.technologies[t].researched = true end "
        "rcon.print('ok')")


def site(x0, y0, nest_dx=60):
    """Cleared ground around (x0, y0), a chest standing for the base there and a spawner nest_dx tiles east."""
    lua(f"""
clear({x0 - 30}, {y0 - 30}, {x0 + nest_dx + 20}, {y0 + 30})
s.create_entity{{name = "wooden-chest", position = {{{x0} + 0.5, {y0} + 0.5}}, force = f}}
s.create_entity{{name = "biter-spawner", position = {{{x0 + nest_dx}, {y0}}}, force = "enemy"}}
rcon.print("ok")""")


def ghosts(x0, y0, x1, y1, inner=None):
    return lua_json(f"""
local r = {{}}
for _, g in pairs(s.find_entities_filtered{{area = {{{{{x0}, {y0}}}, {{{x1}, {y1}}}}}, type = "entity-ghost"}}) do
  if {('g.ghost_name == "' + inner + '"') if inner else 'true'} then
    local b = g.bounding_box
    r[#r + 1] = {{g.ghost_name, g.position.x, g.position.y, b.left_top.x, b.left_top.y, b.right_bottom.x,
      b.right_bottom.y}}
  end
end
rcon.print(helpers.table_to_json(r))""")


def connected(tiles) -> bool:
    tiles = {(round(t[0] - 0.5), round(t[1] - 0.5)) for t in tiles}
    if not tiles:
        return False
    seen, todo = set(), [next(iter(tiles))]
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        todo += [(t[0] + dx, t[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)) if (t[0] + dx, t[1] + dy) in tiles]
    return seen == tiles


def check_arc(errs, tag, walls, turrets, nest_x):
    if not walls:
        errs.append(f"{tag}: no walls")
        return
    if not connected([(w[1], w[2]) for w in walls]):
        errs.append(f"{tag}: the wall arc is not one connected piece")
    front = max(t[5] for t in turrets)
    line = [w for w in walls if w[1] > front]
    if not line or max(w[1] for w in walls) >= nest_x - 2:
        errs.append(f"{tag}: the wall is not between the turrets and the nest")
    ys = [t[2] for t in turrets]
    span = [w[2] for w in walls if w[1] == max(x[1] for x in walls)]
    if not (min(span) < min(ys) - 1 and max(span) > max(ys) + 1):
        errs.append(f"{tag}: the front line does not cover the turrets ({min(span)}..{max(span)} vs {ys})")
    tt = {(x, y) for t in turrets for x in range(int(t[3] + 0.01) // 1, int(t[5] + 0.99)) for y in
          range(int(t[4] + 0.01) // 1, int(t[6] + 0.99))}
    if any((round(w[1] - 0.5), round(w[2] - 0.5)) in tt for w in walls):
        errs.append(f"{tag}: a wall stands on a turret tile")


def d1(errs, m):
    X, Y = 100, -250
    site(X, Y)
    r = call("defense_place_turrets", x=X + 0.5, y=Y + 0.5, toward_x=X + 60, toward_y=Y, n=4, plan="d1")
    m["d1_turrets"] = len(r.get("placed") or [])
    if m["d1_turrets"] != 4:
        errs.append(f"D1: {m['d1_turrets']} turrets placed")
    if r.get("walls"):
        errs.append("D1: walls placed before Stone wall is researched")
    research("stone-wall")
    st = call("defense_status")
    groups = [g for g in st.get("unwalled") or [] if abs(g["x"] - X) < 12 and abs(g["y"] - Y) < 12]
    m["d1_unwalled"] = len(groups)
    if not groups:
        errs.append(f"D1: the group is not listed as unwalled: {st.get('unwalled')}")
        return
    g = groups[0]
    w = call("defense_place_walls", x=g["x"], y=g["y"], toward_x=g["toward_x"], toward_y=g["toward_y"],
             group=g.get("plan"), plan="d1-wall")
    m["d1_walls"] = len(w.get("placed") or [])
    walls = ghosts(X - 25, Y - 25, X + 40, Y + 25, "stone-wall")
    turrets = ghosts(X - 25, Y - 25, X + 40, Y + 25, "gun-turret")
    check_arc(errs, "D1", walls, turrets, X + 60)
    st = call("defense_status")
    if any(abs(u["x"] - X) < 12 and abs(u["y"] - Y) < 12 for u in st.get("unwalled") or []):
        errs.append("D1: still listed as unwalled after its walls")
    built = lua_json(f"""
local n = 0
for _, g in pairs(s.find_entities_filtered{{area = {{{{{X - 25}, {Y - 25}}}, {{{X + 40}, {Y + 25}}}}}, type = "entity-ghost"}}) do
  local ok = g.revive()
  if ok then n = n + 1 end
end
rcon.print(helpers.table_to_json({{revived = n,
  walls = s.count_entities_filtered{{area = {{{{{X - 25}, {Y - 25}}}, {{{X + 40}, {Y + 25}}}}}, name = "stone-wall"}},
  turrets = s.count_entities_filtered{{area = {{{{{X - 25}, {Y - 25}}}, {{{X + 40}, {Y + 25}}}}}, name = "gun-turret"}}}}))""")
    m["d1_built"] = built
    if built["walls"] != m["d1_walls"] or built["turrets"] != 4:
        errs.append(f"D1: built {built} vs {m['d1_walls']} walls, 4 turrets")


def d2(errs, m):
    X, Y = 100, -130
    site(X, Y)
    r = call("defense_place_turrets", x=X + 0.5, y=Y + 0.5, toward_x=X + 60, toward_y=Y, n=4, plan="d2")
    m["d2"] = {"turrets": len(r.get("placed") or []), "walls": r.get("walls"), "skipped": r.get("walls_skipped")}
    check_arc(errs, "D2", ghosts(X - 25, Y - 25, X + 40, Y + 25, "stone-wall"),
              ghosts(X - 25, Y - 25, X + 40, Y + 25, "gun-turret"), X + 60)


def d3(errs, m):
    # a belt across the first candidate line: the arc moves out by one
    X, Y = 100, -10
    site(X, Y)
    r = call("defense_place_turrets", x=X + 0.5, y=Y + 0.5, toward_x=X + 60, toward_y=Y, n=4, plan="d3",
             walls=False)
    turrets = ghosts(X - 25, Y - 25, X + 40, Y + 25, "gun-turret")
    front = int(max(t[5] for t in turrets) + 0.99)          # first free tile column past the turrets
    first = front + 2                                       # WALL_GAP
    lua(f"""s.create_entity{{name = "transport-belt", position = {{{first} + 0.5, {Y} + 0.5}}, direction = 0, force = f}}
rcon.print("ok")""")
    w = call("defense_place_walls", x=r["at"]["x"], y=r["at"]["y"], toward_x=X + 60, toward_y=Y, plan="d3-wall")
    walls = ghosts(X - 25, Y - 25, X + 40, Y + 25, "stone-wall")
    m["d3_shift"] = {"placed": len(w.get("placed") or []), "front": max((x[1] for x in walls), default=None),
                     "first_line": first + 0.5}
    if not walls or max(x[1] for x in walls) != first + 1.5:
        errs.append(f"D3: the arc did not move one tile out past the belt: {m['d3_shift']}")
    check_arc(errs, "D3", walls, turrets, X + 60)
    # belts across all three lines: nothing placed
    X, Y = 100, 110
    site(X, Y)
    r = call("defense_place_turrets", x=X + 0.5, y=Y + 0.5, toward_x=X + 60, toward_y=Y, n=4, plan="d3b",
             walls=False)
    turrets = ghosts(X - 25, Y - 25, X + 40, Y + 25, "gun-turret")
    first = int(max(t[5] for t in turrets) + 0.99) + 2
    lua(f"""for dx = 0, 2 do s.create_entity{{name = "transport-belt", position = {{{first} + dx + 0.5, {Y} + 0.5}},
  direction = 0, force = f}} end
rcon.print("ok")""")
    w = call("defense_place_walls", x=r["at"]["x"], y=r["at"]["y"], toward_x=X + 60, toward_y=Y, plan="d3b-wall")
    m["d3_blocked"] = {"placed": len(w.get("placed") or []), "skipped": w.get("skipped"), "gap": w.get("gap")}
    if w.get("placed") or w.get("skipped") != "no gap-free wall arc":
        errs.append(f"D3: walls placed with a gap in every line: {m['d3_blocked']}")
    # water in the line: closed, no wall on it
    X, Y = 100, 230
    site(X, Y)
    r = call("defense_place_turrets", x=X + 0.5, y=Y + 0.5, toward_x=X + 60, toward_y=Y, n=4, plan="d3c",
             walls=False)
    turrets = ghosts(X - 25, Y - 25, X + 40, Y + 25, "gun-turret")
    first = int(max(t[5] for t in turrets) + 0.99) + 2
    lua(f"""s.set_tiles({{{{name = "water", position = {{{first}, {Y}}}}}, {{name = "water", position = {{{first}, {Y + 1}}}}}}})
rcon.print("ok")""")
    w = call("defense_place_walls", x=r["at"]["x"], y=r["at"]["y"], toward_x=X + 60, toward_y=Y, plan="d3c-wall")
    walls = ghosts(X - 25, Y - 25, X + 40, Y + 25, "stone-wall")
    on_water = [x for x in walls if x[1] == first + 0.5 and x[2] in (Y + 0.5, Y + 1.5)]
    m["d3_water"] = {"placed": len(w.get("placed") or []), "skipped": w.get("skipped"), "on_water": len(on_water),
                     "front": max((x[1] for x in walls), default=None)}
    # (the same arc as D2's minus the two water tiles: the tile north of the water gets its wall too)
    if not walls or on_water or max(x[1] for x in walls) != first + 0.5 or \
            len(w.get("placed") or []) != (m.get("d2") or {}).get("walls", 0) - 2:
        errs.append(f"D3: water in the line: {m['d3_water']}")


def d4(errs, m):
    X, Y = -150, 0
    site(X, Y, nest_dx=100)
    lua(f"""s.pollute({{{X}, {Y}}}, 2000) rcon.print("ok")""")
    th = [t for t in call("defense_threats", x=X, y=Y, radius=200) if abs(t["x"] - X - 100) < 3 and
          abs(t["y"] - Y) < 3]
    m["d4_threat"] = th[:1]
    if not th or th[0].get("cloud") is None or not 1 <= th[0]["cloud"] <= 4 or not th[0].get("base"):
        errs.append(f"D4: no cloud distance for the nest: {th[:1]}")
    lua("game.speed = 60 rcon.print('ok')")
    t0 = call("defense_status").get("pollution")
    time.sleep(6)
    st = call("defense_status")
    lua("game.speed = 1 rcon.print('ok')")
    m["d4_status"] = {k: st.get(k) for k in ("nest_dist", "polluted_nests", "nests", "pollution", "pollution_trend",
                                              "walls_researched", "researched", "groups")}
    if st.get("pollution") in (None, 0) or "pollution_trend" not in st or st.get("nests") in (None, 0):
        errs.append(f"D4: status without the per-minute sample: {m['d4_status']} (before: {t0})")


def mod_lua(code):
    """Lua in the mod's own state (its storage), not the level's."""
    from factorio_claude import rcon
    rcon.lua("rcon.print('armed')")
    out = rcon.client().command("/silent-command __claude-bridge__ " + code)
    if "rror" in out[:400]:
        raise RuntimeError(out[:3000])
    return out


def d5(errs, m):
    # turrets placed before defence v2 (no group recorded): two clusters 20 tiles apart, seeded once on the next
    # per-minute sample; each gets its own wall arc (review fix: all seeded groups shared the plan name "seeded",
    # so place_walls marked the first one walled for every call and the other stayed listed for ever)
    X, Y = 100, -370
    site(X, Y)
    lua(f"""for _, dy in ipairs({{-1, 1, 19, 21}}) do
  s.create_entity{{name = "gun-turret", position = {{{X + 6}, {Y} + dy}}, force = f}}
end
rcon.print("ok")""")
    mod_lua("storage.claude_defense.groups_seeded = nil rcon.print('ok')")
    lua("game.speed = 60 rcon.print('ok')")
    time.sleep(8)
    lua("game.speed = 1 rcon.print('ok')")
    st = call("defense_status")
    mine = [g for g in st.get("unwalled") or [] if abs(g["x"] - X - 6) < 4 and Y - 4 < g["y"] < Y + 24]
    m["d5_seeded"] = mine
    if len(mine) != 2 or len({g.get("plan") for g in mine}) != 2:
        errs.append(f"D5: expected 2 seeded groups with their own plan names: {mine}")
        return
    for i, g in enumerate(mine):
        w = call("defense_place_walls", x=g["x"], y=g["y"], toward_x=g["toward_x"], toward_y=g["toward_y"],
                 group=g.get("plan"), plan=f"d5-wall-{i}")
        if not w.get("placed"):
            errs.append(f"D5: no walls for seeded group {g.get('plan')}: {w.get('skipped')}")
    st = call("defense_status")
    left = [g for g in st.get("unwalled") or [] if abs(g["x"] - X - 6) < 4 and Y - 4 < g["y"] < Y + 24]
    m["d5_left"] = left
    if left:
        errs.append(f"D5: seeded groups still listed as unwalled after their walls: {left}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factorio", type=Path, default=Path(os.environ.get("FACTORIO_EXE") or "factorio"))
    ap.add_argument("--udp-port", type=int, default=34533)
    ap.add_argument("--rcon-port", type=int, default=27033)
    ap.add_argument("--game-port", type=int, default=34433)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    assert a.udp_port != 34500 and a.rcon_port != 27015, "never the player's game ports"
    proc, work, log = start(a.factorio, a.udp_port, a.rcon_port, a.game_port)
    errs, m = [], {}
    try:
        lua("s.peaceful_mode = true game.map_settings.enemy_expansion.enabled = false rcon.print('ok')")
        research("automation-science-pack", "gun-turret")
        for case in (d1, d2, d3, d4, d5):
            n = len(errs)
            try:
                case(errs, m)
            except Exception as ex:  # noqa: BLE001 - report and go on
                errs.append(f"{case.__name__}: {ex}")
            print(f"{'PASS' if len(errs) == n else 'FAIL'} {case.__name__}: {errs[n:] or ''}")
        print(json.dumps(m, indent=1)[:4000])
        bad = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
               if "claude-bridge" in ln and ("rror" in ln)]
        if bad:
            errs.append("server log: " + "; ".join(bad[:3]))
    finally:
        if not a.keep:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(work, ignore_errors=True)
    print("PASS" if not errs else f"FAIL: {errs}")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
