"""Live test of the plan ops (plan_preview / plan_clear_preview / plan_apply / plan_list / plan_drop) and the Plans GUI.
Not collected by pytest (needs a running server). Run:
    .venv\\Scripts\\python.exe tests\\live_planui.py [--slot 10] [--no-restart] [--client] [--keep]
--client starts a graphical client (own config dir testenv\\client_planui) to test the Plans tab with a real player and
take screenshots (testenv\\client_planui\\write\\script-output\\planui_*.png); it is closed again unless --keep.
Synthetic plans come from planner.blocks on fixture seed 1001 (the slot is restarted from seed_1001.zip)."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factorio_claude import platforms  # noqa: E402
FACTORIO = str(platforms.find_factorio()[0] or "")  # env FACTORIO_EXE / FACTORIO_BIN, else Steam discovery
if not FACTORIO:
    sys.exit("Factorio not found: set FACTORIO_EXE")

ap = argparse.ArgumentParser()
ap.add_argument("--slot", type=int, default=10)
ap.add_argument("--name", default="planui")
ap.add_argument("--no-restart", action="store_true")
ap.add_argument("--client", action="store_true")
ap.add_argument("--keep", action="store_true")
ARGS = ap.parse_args()
SLOT, NAME = ARGS.slot, ARGS.name
INST = ROOT / "testenv" / "inst" / NAME
CLIENT_DIR = ROOT / "testenv" / "client_planui"
os.environ.update(FACTORIO_UDP_PORT=str(34500 + SLOT), FACTORIO_RCON_PORT=str(27016 + SLOT),
                  FACTORIO_RCON_PASSWORD="testpw", FACTORIO_SCRIPT_OUTPUT=str(INST / "write" / "script-output"),
                  FACTORIO_UDP_TIMEOUT="20")

from factorio_claude import link, rcon  # noqa: E402
from factorio_claude.planner import blocks  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402

rcon._client = rcon.Rcon("127.0.0.1", 27016 + SLOT, "testpw")
RESULTS = []


def check(name, cond, info=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
    return cond


def lua(code):
    return rcon.lua(code).strip()


def lua_json(code):
    out = lua(code)
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError(f"lua: {out[:500]}")


def call(op, **args):
    return link.call(op, **args)


def restart(save="seed_1001.zip"):
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "testenv" / "restart.ps1"), "-Name", NAME, "-Slot", str(SLOT), "-Save", save],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + SLOT, "testpw")


def rows(entities):
    """PE list -> plan_preview rows; underground/loader type goes in the 6th field."""
    out = []
    for e in entities:
        r = [e.name, e.x, e.y, e.direction, e.recipe]
        t = (e.extra or {}).get("type")
        if t:
            r.append(t)
        out.append(r)
    return out


def ghost_stats(plan):
    return lua_json(f"""
local n, ug, rec, untagged_users = 0, {{}}, {{}}, 0
for _, g in pairs(game.surfaces.nauvis.find_entities_filtered{{type="entity-ghost"}}) do
  if g.tags and g.tags.claude_plan == "{plan}" then
    n = n + 1
    if g.last_user then untagged_users = untagged_users + 1 end
    if g.ghost_name == "underground-belt" then ug[#ug+1] = g.belt_to_ground_type end
    if g.ghost_type == "assembling-machine" then local r = g.get_recipe(); rec[#rec+1] = r and r.name or "none" end
  end
end
table.sort(ug)
rcon.print(helpers.table_to_json({{n=n, ug=ug, rec=rec, with_user=untagged_users}}))""")


def render_count():
    return int(lua('rcon.print(#rendering.get_all_objects("claude-bridge"))'))


def log_ms(pattern):
    """Max 'Duration: X ms' logged for a plan op (server.log): the op itself and every per-tick slice."""
    txt = (INST / "server.log").read_text(encoding="utf8", errors="replace")
    m = re.findall(pattern + r"[^\n]*?Duration: ([\d.]+)ms", txt)
    return (max(float(x) for x in m), len(m)) if m else (None, 0)


def wait_plan(pid, cond, timeout=20):
    for _ in range(int(timeout * 10)):
        p = next((x for x in call("plan_list") if x["id"] == pid), None)
        if p and cond(p):
            return p
        time.sleep(0.1)
    return None


# --------------------------------------------------------------------------------------------- plans from blocks
def make_plans():
    world, gd = load_fixture(1001)
    grid = world.grid((-120, -120, 120, 120))
    P = {}
    # small mixed plan: burner smelter + steam power + gear assembler + an underground pair, on ground with trees
    sm = blocks.smelter_array(6, "stone-furnace", gd=gd)
    st = blocks.steam_power(2, gd=gd)
    w = max(sm.size[0], 0) + st.size[0] + 8
    h = max(sm.size[1], st.size[1]) + 4
    # densest tree cluster near spawn, so apply has trees to clear under the footprints
    cells = {}
    for o in world.obstacles_in((-90, -90, 90, 90)):
        if o.kind == "tree":
            k = (int(o.x // 12), int(o.y // 12))
            cells[k] = cells.get(k, 0) + 1
    cx, cy = max(cells, key=cells.get)
    near = (cx * 12 + 6, cy * 12 + 6)
    x0, y0 = grid.free_rect(w, h, near=near, allow_clear=True, avoid_resources=True)[:2]
    ents = sm.at(x0, y0).placed() + st.at(x0 + sm.size[0] + 3, y0).placed()
    ax, ay = x0 + sm.size[0] + 3, y0 + st.size[1] + 1
    ents.append(blocks.PE("assembling-machine-1", ax + 1.5, ay + 1.5, 0, "iron-gear-wheel"))
    ents.append(blocks.PE("underground-belt", ax + 4.5, ay + 0.5, 4, extra={"type": "input"}))
    ents.append(blocks.PE("underground-belt", ax + 8.5, ay + 0.5, 4, extra={"type": "output"}))
    trees = [o for o in world.obstacles_in((x0 - 6, y0 - 6, x0 + w + 6, y0 + h + 6)) if o[0] in ("tree", "rock")]
    occupied = set()
    for e in ents:
        occupied |= set(gd.footprint(e.name, e.x, e.y, e.direction))
    loose = [t for t in trees if (int(t[2] // 1), int(t[3] // 1)) not in occupied][:5]
    P["small"] = dict(entities=ents, deconstruct=[[t[2], t[3], t[1]] for t in loose],
                      area=(x0, y0, x0 + w, y0 + h))
    # big plan: 2000+ entities (rows of smelter arrays), validity irrelevant
    big, bx, by = [], 0, 0
    blk = blocks.smelter_array(30, "stone-furnace", gd=gd)
    bw, bh = blk.size
    cols = 14
    gx, gy = grid.free_rect(cols * (bw + 1), 2 * (bh + 1), near=(0, 0), allow_clear=True)[:2] or (0, 0)
    for i in range(cols * 2):
        bx, by = gx + (i % cols) * (bw + 1), gy + (i // cols) * (bh + 1)
        big += blk.at(bx, by).placed()
    P["big"] = dict(entities=big, deconstruct=[], area=(gx, gy, gx + cols * (bw + 1), gy + 2 * (bh + 1)))
    return P, gd


# --------------------------------------------------------------------------------------------- tests
def test_ops(P):
    small = P["small"]
    sx0, sy0, sx1, sy1 = small["area"]
    lua(f"game.surfaces.nauvis.request_to_generate_chunks({{0,0}}, 8); game.surfaces.nauvis.force_generate_chunk_requests()")
    base = render_count()

    r = call("plan_preview", id="t_small", title="Small test", variant="A", stage="bootstrap",
             summary="Burner smelting: 6 stone furnaces\n**Power**: 2 boilers, 4 engines\n- 1 gear assembler",
             entities=rows(small["entities"]), deconstruct=small["deconstruct"],
             validation={"ok": True, "errors": [], "warnings": [{"kind": "x"}]}, warnings=["fuel by hand"])
    n_small = len(small["entities"])
    check("preview small: entities stored", r["entities"] == n_small and r["deconstruct"] == len(small["deconstruct"]),
          f"{r['entities']}/{n_small}, objects {r.get('objects')}")
    check("preview small: no bad rows / unknown", r["bad_rows"] == 0 and not r.get("unknown"))
    objs = render_count() - base
    check("preview small: overlay drawn (game + chart)", objs == r["objects"] and objs > 10, f"{objs} objects")
    modes = lua_json('local m={} for _,o in pairs(rendering.get_all_objects("claude-bridge")) do m[o.render_mode]=(m[o.render_mode] or 0)+1 end rcon.print(helpers.table_to_json(m))')
    check("overlay has chart-mode objects", modes.get("chart", 0) > 0 and modes.get("game", 0) > 0, str(modes))

    lst = call("plan_list")
    p = next((x for x in lst if x["id"] == "t_small"), None)
    check("plan_list: small listed", p and p["status"] == "proposed" and p["shown"] and p["variant"] == "A",
          json.dumps(p)[:200] if p else "missing")
    check("summary markdown stripped", p and not any("**" in s or s.startswith("- ") for s in p["summary"]),
          str(p and p["summary"]))
    check("validation summarized", p and p["validation"] == {"ok": True, "errors": 0, "warnings": 1}, str(p and p["validation"]))

    # variant B hides A (same force); keep_others keeps both
    r2 = call("plan_preview", id="t_small_b", title="Small test B", variant="B",
              entities=rows(small["entities"][: len(small["entities"]) // 2]))
    lst = {x["id"]: x for x in call("plan_list")}
    check("second preview hides the first", lst["t_small_b"]["shown"] and not lst["t_small"]["shown"])
    check("render objects = only B", render_count() - base == r2["objects"])
    call("plan_preview", id="t_small", title="Small test", variant="A", entities=[], append=True, keep_others=True)
    lst = {x["id"]: x for x in call("plan_list")}
    check("keep_others shows both", lst["t_small_b"]["shown"] and lst["t_small"]["shown"])
    check("append with no rows keeps entities", lst["t_small"]["entities"] == n_small)
    call("plan_clear_preview", id="t_small_b")
    lst = {x["id"]: x for x in call("plan_list")}
    check("plan_clear_preview {id}", not lst["t_small_b"]["shown"] and lst["t_small"]["shown"])
    call("plan_clear_preview")
    check("plan_clear_preview {} hides all", render_count() == base, f"{render_count() - base} left")
    call("plan_drop", id="t_small_b")
    check("plan_drop", all(x["id"] != "t_small_b" for x in call("plan_list")))

    # bad input
    bad = call("plan_preview", id="t_bad", entities=[["no-such-entity", 1.5, 1.5, 0], ["x"], {"name": "pipe", "x": 3.5, "y": 1.5}],
               show=False)
    check("bad rows counted, unknown names reported", bad["bad_rows"] == 1 and bad.get("unknown") == ["no-such-entity"]
          and bad["entities"] == 2, json.dumps(bad))
    try:
        call("plan_apply", id="nope")
        check("plan_apply unknown id errors", False)
    except link.LinkError as e:
        check("plan_apply unknown id errors", "no plan" in str(e), str(e)[:80])
    call("plan_drop", id="t_bad")

    # apply small
    call("plan_preview", id="t_small", title="Small test", entities=rows(small["entities"]), deconstruct=small["deconstruct"])
    t0 = time.time()
    a = call("plan_apply", id="t_small")
    dt = time.time() - t0
    blocked_detail = [(b["name"], b["why"]) for b in a["blocked_list"]][:6]
    check("apply small: all placed", a["placed"] == n_small and a["blocked"] == 0,
          f"placed {a['placed']}/{n_small} blocked {a['blocked']} {blocked_detail} decon {a['deconstruct']} {dt:.2f}s")
    gs = ghost_stats("t_small")
    check("ghosts tagged claude_plan=t_small, no last_user", gs["n"] == n_small and gs["with_user"] == 0, json.dumps(gs))
    check("underground ghost types input+output", gs["ug"] == ["input", "output"], str(gs["ug"]))
    check("assembler ghost recipe set", gs["rec"] == ["iron-gear-wheel"], str(gs["rec"]))
    # every tree/rock overlapping a planned ghost is marked; listed obstacles are marked
    tr = lua_json(f"""
local s, f = game.surfaces.nauvis, game.forces.player
local under, marked = 0, 0
for _, g in pairs(s.find_entities_filtered{{type="entity-ghost"}}) do
  if g.tags and g.tags.claude_plan == "t_small" then
    local b = g.bounding_box
    for _, t in pairs(s.find_entities_filtered{{area={{{{b.left_top.x+0.05,b.left_top.y+0.05}},{{b.right_bottom.x-0.05,b.right_bottom.y-0.05}}}}, type={{"tree","simple-entity"}}}}) do
      under = under + 1
      if t.to_be_deconstructed() then marked = marked + 1 end
    end
  end
end
local listed, lmarked = 0, 0
for _, d in pairs(helpers.json_to_table('{json.dumps(small["deconstruct"])}')) do
  local x, y = math.floor(d[1]), math.floor(d[2])
  for _, t in pairs(s.find_entities_filtered{{area={{{{x+0.05,y+0.05}},{{x+0.95,y+0.95}}}}, name=d[3]}}) do
    listed = listed + 1
    if t.to_be_deconstructed() then lmarked = lmarked + 1 end
  end
end
rcon.print(helpers.table_to_json({{under=under, marked=marked, listed=listed, lmarked=lmarked}}))""")
    check("trees/rocks under footprints marked for deconstruction", tr["under"] == tr["marked"], json.dumps(tr))
    check("listed obstacles marked for deconstruction", tr["listed"] == tr["lmarked"] and tr["listed"] >= 1, json.dumps(tr))
    p = {x["id"]: x for x in call("plan_list")}["t_small"]
    check("status applied + overlay hidden", p["status"] == "applied" and not p["shown"] and p["result"]["placed"] == n_small)

    a2 = call("plan_apply", id="t_small")
    check("re-apply is idempotent (existing counted)", a2["placed"] == 0 and a2["existing"] == n_small and a2["blocked"] == 0,
          f"placed {a2['placed']} existing {a2['existing']} blocked {a2['blocked']}")
    # blocked: put a chest in the way of one furnace, apply a copy plan
    fur = next(e for e in small["entities"] if e.name == "stone-furnace")
    lua(f'game.surfaces.nauvis.create_entity{{name="wooden-chest", position={{{fur.x + 0.5},{fur.y + 40.5}}}, force="player"}}')
    moved = [blocks.PE(e.name, e.x, e.y + 40, e.direction, e.recipe, e.extra) for e in small["entities"]]
    call("plan_preview", id="t_moved", title="Moved", entities=rows(moved), show=False)
    a3 = call("plan_apply", id="t_moved")
    check("blocked entity skipped + reported", a3["blocked"] >= 1 and a3["placed"] + a3["blocked"] + a3["existing"] == n_small
          and any(b["name"] == "stone-furnace" for b in a3["blocked_list"]),
          f"placed {a3['placed']} blocked {a3['blocked']} {[(b['name'], b['why']) for b in a3['blocked_list']][:4]}")

    c = call("clear_claude_ghosts", plan="t_small")
    check("clear_claude_ghosts {plan} removes exactly that plan", c["removed"] == n_small and ghost_stats("t_small")["n"] == 0
          and ghost_stats("t_moved")["n"] == a3["placed"], f"removed {c['removed']}")
    call("clear_claude_ghosts", plan="t_moved")
    for pid in ("t_small", "t_moved"):
        call("plan_drop", id=pid)
    # left: only the red 5-minute markers of blocked entities
    check("all overlays gone after drops (red blocked markers stay)", render_count() - base == a3["blocked"],
          f"{render_count() - base} left")


def test_big(P):
    big = P["big"]
    n = len(big["entities"])
    payload = len(json.dumps(rows(big["entities"])))
    base = render_count()
    t0 = time.time()
    r = call("plan_preview", id="t_big", title="Big test", entities=rows(big["entities"]))
    dt = time.time() - t0
    check(f"big preview over UDP ({n} entities, {payload // 1024} KB JSON) drawn over several ticks",
          r["entities"] == n and r.get("pending"), f"{dt:.2f}s round trip, pending={r.get('pending')}")
    t1 = int(lua("rcon.print(game.tick)"))
    p = wait_plan("t_big", lambda p: p["shown"] and not p["drawing"])
    t2 = int(lua("rcon.print(game.tick)"))
    ms, nlog = log_ms(r"plan_preview t_big ")
    objs = render_count() - base
    check("big preview finished", p is not None, f"{objs} objects, {nlog} slices, max {ms} ms per tick, ~{t2 - t1} ticks")
    check("big preview: every tick < 50 ms", ms is not None and ms < 50, f"max {ms} ms")
    # chunked delivery via append (3 parts)
    ents = rows(big["entities"])
    k = len(ents) // 3
    call("plan_preview", id="t_big2", title="Big appended", entities=ents[:k], show=False)
    call("plan_preview", id="t_big2", entities=ents[k:2 * k], append=True, show=False)
    r2 = call("plan_preview", id="t_big2", entities=ents[2 * k:], append=True)
    check("append over 3 ops", r2["entities"] == n, f"{r2['entities']}")
    wait_plan("t_big2", lambda p: p["shown"] and not p["drawing"])
    hid = {x["id"]: x["shown"] for x in call("plan_list")}
    check("t_big hidden by t_big2", hid["t_big"] is False and hid["t_big2"] is True)
    check("hidden plan's objects destroyed", render_count() - base == objs, f"{render_count() - base} vs {objs}")
    # idle: no jobs left -> the tick handler returns at once
    jobs = lua("__claude-bridge__ rcon.print(tostring(storage.plan_jobs and next(storage.plan_jobs)))")
    check("no plan jobs left when idle (tick handler returns at once)", jobs == "nil", jobs)
    t0 = time.time()
    a = call("plan_apply", id="t_big2")
    dt = time.time() - t0
    p = wait_plan("t_big2", lambda p: p["status"] == "applied")
    res = p["result"] if p else {}
    ams, nlog = log_ms(r"plan_apply t_big2 ")
    check("big apply finished over several ticks", a.get("pending") and p is not None and
          res["placed"] + res["blocked"] + res["existing"] == n,
          f"placed {res.get('placed')} blocked {res.get('blocked')} decon {res.get('deconstruct')}, {nlog} slices, "
          f"max {ams} ms per tick, {dt:.2f}s")
    check("big apply: every tick < 50 ms", ams is not None and ams < 50, f"max {ams} ms")
    gs = ghost_stats("t_big2")
    check("big apply: ghosts tagged", gs["n"] == res["placed"], f"{gs['n']}")
    call("clear_claude_ghosts", plan="t_big2")
    call("plan_drop", id="t_big2")
    call("plan_drop", id="t_big")
    check("overlays gone after drops", render_count() == base)
    return ms, ams


# --------------------------------------------------------------------------------------------- GUI (needs a player)
def start_client():
    cfg = CLIENT_DIR / "config.ini"
    env = dict(os.environ, SteamAppId="427520")
    for k in ("FACTORIO_UDP_PORT", "FACTORIO_RCON_PORT", "FACTORIO_RCON_PASSWORD", "FACTORIO_SCRIPT_OUTPUT"):
        env.pop(k, None)
    log = open(CLIENT_DIR / "client-stdout.log", "w")
    pr = subprocess.Popen([FACTORIO, "--config", str(cfg).replace("\\", "/"),
                           "--mod-directory", str(INST / "mods").replace("\\", "/"),
                           "--mp-connect", f"127.0.0.1:{34199 + SLOT}", "--window-size", "1600x900"],
                          env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(1)
        if lua("rcon.print(#game.connected_players)") == "1":
            return pr
    return pr


def shot(name, **kw):
    kw.setdefault("resolution", {"x": 1600, "y": 900})
    kw.setdefault("show_gui", True)
    a = json.dumps(dict(player=1, path=f"planui_{name}.png", **kw))
    lua(f"game.take_screenshot(helpers.json_to_table('{a}'))")
    time.sleep(2.5)
    return CLIENT_DIR / "write" / "script-output" / f"planui_{name}.png"


def test_gui(P):
    small = P["small"]
    x0, y0, x1, y1 = small["area"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    lua("local p=game.get_player(1) if p.controller_type == defines.controllers.cutscene then p.exit_cutscene() end")
    time.sleep(1)
    lua(f'local p=game.get_player(1) p.teleport({{{cx}, {y1 + 6}}}) game.forces.player.chart(p.surface, {{{{-200,-200}},{{200,200}}}})')
    for vid, title, ok in (("v_a", "Compact hub", True), ("v_b", "Expandable bus", True), ("v_c", "Electric", False)):
        n = len(small["entities"]) if vid != "v_b" else len(small["entities"]) // 2
        call("plan_preview", id=vid, title=title, variant=vid[-1].upper(), stage="bootstrap", group="g1",
             summary=f"{title}: 6 stone furnaces (45 plates/min)\nPower 1.8 MW from 2 boilers\nBuild time about 4 min",
             entities=rows(small["entities"][:n]), deconstruct=small["deconstruct"],
             validation={"ok": ok, "errors": [] if ok else [{"kind": "power"}, {"kind": "belt"}], "warnings": []},
             warnings=[] if ok else ["2 furnaces have no fuel path"], show=False)
    B = lambda name, value=None: call("gui_click", player=1, name=name, **({} if value is None else {"value": value}))
    B("claude_toggle")
    d = call("gui_dump", player=1)
    badge = json.dumps(d["panel"]).find('"badge": "3"') >= 0
    check("Plans tab badge shows 3 new plans", badge)
    B("claude_tab_plans")
    d = json.dumps(call("gui_dump", player=1)["panel"])
    check("M0 tabs intact (helper/foreman/blueprints/ask widgets)", all(
        n in d for n in ("claude_clear_ghosts", "claude_foreman_scan", "claude_bp_cursor", "claude_ask_send",
                         "claude_confirm_box")) and "failed to load" not in d)
    check("Plans tab lists 3 variants with buttons",
          all(f"claude_plan_approve|{v}" in d for v in ("v_a", "v_b", "v_c")) and "Compact hub" in d and "validator: 2 error(s)" in d)
    check("legend shown", "To clear (trees/rocks)" in d and "Belts" in d)
    B("claude_plan_preview|v_a")
    lst = {x["id"]: x for x in call("plan_list")}
    check("GUI Preview shows v_a", lst["v_a"]["shown"] and not lst["v_b"]["shown"])
    d = json.dumps(call("gui_dump", player=1)["panel"])
    check("button flips to Hide", '"Hide"' in d)
    shots = [shot("panel_world", position={"x": cx, "y": cy}, zoom=0.7)]
    B("claude_plan_preview|v_b")
    lst = {x["id"]: x for x in call("plan_list")}
    check("Preview v_b hides v_a", lst["v_b"]["shown"] and not lst["v_a"]["shown"])
    B("claude_plan_preview|v_b")
    B("claude_plan_preview|v_a")
    shots.append(shot("world_close", position={"x": cx, "y": cy}, zoom=1.2, show_gui=False))
    B("claude_plan_show|v_a")
    ctrl = lua("rcon.print(game.get_player(1).controller_type)")
    check("Show opens remote view", ctrl == str(lua("rcon.print(defines.controllers.remote)")), ctrl)
    lua(f"local p=game.get_player(1) p.zoom=0.08")
    time.sleep(1)
    shots.append(shot("map_view", show_gui=True))
    lua("local p=game.get_player(1) p.exit_remote_view()")
    # Adjust pre-fills the Ask box
    B("claude_plan_adjust|v_b")
    d = call("gui_dump", player=1)
    txt = json.dumps(d["panel"])
    check("Adjust pre-fills Ask Claude", "Adjust plan v_b (Expandable bus): " in txt and d["state"]["tab"] == 4)
    B("claude_ask_text", "")  # sends nothing (empty)
    B("claude_tab_plans")
    # Reject v_b, Approve v_a
    B("claude_plan_reject|v_b")
    check("Reject drops the plan", "v_b" not in {x["id"] for x in call("plan_list")})
    call("inbox_pop")  # clear
    B("claude_act_follow")  # the helper follows the player (endless task): approving must still start the build
    B("claude_tab_plans")
    B("claude_plan_approve|v_a")
    p = {x["id"]: x for x in call("plan_list")}["v_a"]
    check("Approve applies (ghosts placed)", p["status"] == "applied" and p["result"]["placed"] > 0)
    inbox = call("inbox_pop")
    msgs = inbox if isinstance(inbox, list) else inbox.get("messages", [])
    check("Approve tells the companion (inbox source=plan)", any(m["source"] == "plan" and m["text"] == "approved v_a"
                                                                   for m in msgs), json.dumps(msgs)[:200])
    gs = ghost_stats("v_a")
    check("approved ghosts have no last_user (clearable)", gs["n"] == p["result"]["placed"] and gs["with_user"] == 0, json.dumps(gs))
    d = json.dumps(call("gui_dump", player=1)["panel"])
    check("applied card shows result + Remove its ghosts", "Approved. Placed" in d and "claude_plan_unghost|v_a" in d)
    ag = call("agent_status")
    cur = (ag.get("current") or {}).get("type")
    check("Approve starts the helper on the plan (follow dropped, build_ghosts running)",
          ag.get("alive") and cur != "follow" and "follow" not in (ag.get("queue") or []) and (cur == "build_ghosts" or "build_ghosts" in (ag.get("queue") or []) or
                               any("build" in x["msg"] or "gathering" in x["msg"] for x in ag.get("log") or [])),
          f"{cur} {ag.get('queue')}")
    check("applied card has Helper: build", "claude_plan_build|v_a" in d)
    check("overlapping proposal v_c is superseded (no Approve)", "Replaced by Compact hub" in d and
          "claude_plan_approve|v_c" not in d)
    call("agent_stop")
    shots.append(shot("approved", position={"x": cx, "y": cy}, zoom=0.7))
    B("claude_plan_unghost|v_a")
    check("Remove its ghosts", ghost_stats("v_a")["n"] == 0)
    d = json.dumps(call("gui_dump", player=1)["panel"])
    check("removing its ghosts frees v_c again", "claude_plan_approve|v_c" in d)
    B("claude_plan_dismiss|v_a")
    check("Dismiss drops applied plan", "v_a" not in {x["id"] for x in call("plan_list")})
    shots.append(shot("after", position={"x": cx, "y": cy}, zoom=0.7))
    errs = [line for line in (INST / "server.log").read_text(encoding="utf8", errors="replace").splitlines()
            if "claude-bridge gui" in line or "plan card" in line]
    check("no GUI errors logged", not errs, "; ".join(errs[:3]))
    return shots


def main():
    if not ARGS.no_restart:
        restart()
    lua("rcon.print(1)")
    P, gd = make_plans()
    print(f"small plan: {len(P['small']['entities'])} entities at {P['small']['area']}; big: {len(P['big']['entities'])}")
    test_ops(P)
    test_big(P)
    shots = []
    pr = None
    if ARGS.client:
        pr = start_client()
        try:
            if check("graphical client joined", lua("rcon.print(#game.connected_players)") == "1"):
                shots = test_gui(P)
        except BaseException:
            if not ARGS.keep:
                pr.kill()
            raise
    err = [line for line in (INST / "server.log").read_text(encoding="utf8", errors="replace").splitlines()
           if "Error" in line or "error" in line.lower() and "claude" in line.lower()]
    check("no script errors in server.log", not err, "; ".join(err[:3]))
    if pr and not ARGS.keep:
        pr.kill()
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed" + (f"; FAILED: {fails}" if fails else ""))
    for s in shots:
        print("screenshot:", s)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
