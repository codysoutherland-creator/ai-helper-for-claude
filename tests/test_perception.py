"""Tests for planner.paths / world / grid (perception). Offline: synthetic exports + a real export captured from a
2.1.23 test server (tests/data/lake_export) with engine ground truth (tests/data/lake_truth.json)."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factorio_claude.planner import paths  # noqa: E402
from factorio_claude.planner.grid import BLOCKED, CLEARABLE, FREE, Grid  # noqa: E402
from factorio_claude.planner.world import World  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


# --- helpers --------------------------------------------------------------------------------------------------
def rows_from(tiles, cx, cy):
    g = [["0"] * 32 for _ in range(32)]
    for x, y in tiles:
        g[y - cy * 32][x - cx * 32] = "1"
    return ["".join(r) for r in g]


def write_export(d: Path, chunks: dict, run_id="1_1"):
    """chunks: {(cx,cy): {"water": set|None, "void": set|None, "resources": [...], "obstacles": [...], "entities": [...]}}"""
    d.mkdir(parents=True, exist_ok=True)
    for (cx, cy), c in chunks.items():
        out = {"cx": cx, "cy": cy, "resources": c.get("resources", []), "obstacles": c.get("obstacles", []),
               "entities": c.get("entities", {})}  # {} = how Lua encodes an empty table sometimes
        for k in ("water", "void"):
            if c.get(k):
                out[k] = rows_from(c[k], cx, cy)
        (d / f"c_{cx}_{cy}.json").write_text(json.dumps(out))
    (d / "index.json").write_text(json.dumps({"surface": "nauvis", "tick": 5, "run_id": run_id,
                                              "chunks": [list(k) for k in chunks], "bbox": None}))
    return d


def brute_shoreline(water: set, land_ok):
    """Reference implementation of the verified offshore-pump rule."""
    fw = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
    out = set()
    xs = [x for x, _ in water]
    ys = [y for _, y in water]
    for y in range(min(ys) - 3, max(ys) + 4):
        for x in range(min(xs) - 3, max(xs) + 4):
            if (x, y) in water or not land_ok(x, y):
                continue
            for d, (fx, fy) in fw.items():
                px, py = -fy, fx
                if all((x + j * fx + k * px, y + j * fy + k * py) in water for j in (1, 2) for k in (-1, 0, 1)):
                    out.add((x, y, d))
    return out


@pytest.fixture
def synth(tmp_path):
    # chunk (0,0) and (1,0): 64x32 tiles
    lake = {(x, y) for x in range(40, 50) for y in range(10, 18)} | {(39, 12), (50, 15)}
    puddle = {(5, 30), (6, 30)}  # tiny body
    void = {(x, 31) for x in range(40, 64)}
    # iron: two blobs separated by a 2-tile gap (one patch) + a far blob 4 tiles away (separate)
    iron = [(x, y) for x in range(2, 8) for y in range(2, 8)] + [(x, y) for x in range(10, 14) for y in range(2, 8)]
    iron_far = [(x, y) for x in range(18, 23) for y in range(2, 7)]
    copper = [(x, y) for x in range(2, 6) for y in range(20, 23)]  # 12 tiles: below min_tiles=20
    res0 = [["iron-ore", x + 0.5, y + 0.5, 100] for x, y in iron + iron_far] + \
           [["copper-ore", x + 0.5, y + 0.5, 50] for x, y in copper] + \
           [["crude-oil", 25.5, 25.5, 300000], ["crude-oil", 30.5, 27.5, 300000]]
    obs0 = [["tree", "tree-01", 20.3, 20.7, "", [19, 20, 21, 21]],
            ["rock", "big-rock", 24, 12, "", [23, 11, 25, 13]],
            ["cliff", "cliff", 30, 12, "west-to-east", [28, 11, 32, 13]],
            ["cliff", "cliff", 30, 18, "north-to-east", [28, 17, 31, 20], [[28, 17], [29, 18], [30, 19]]],
            ["enemy", "small-worm-turret", 60, 5, "", [59, 4, 61, 6]]]
    ent1 = [["assembling-machine-1", 55.5, 22.5, 0, {"type": "assembling-machine", "recipe": "iron-gear-wheel",
                                                       "b": [54, 21, 57, 24]}],
            ["ghost:transport-belt", 58.5, 22.5, 4, {"type": "transport-belt", "tags": {"claude_plan": "p1"},
                                                      "b": [58, 22, 59, 23]}]]
    d = write_export(tmp_path / "claude" / "map" / "nauvis" / "1_1", {
        (0, 0): {"water": puddle, "resources": res0, "obstacles": obs0},
        (1, 0): {"water": lake | void, "void": void, "entities": ent1},
    })
    return d, dict(lake=lake, puddle=puddle, void=void, iron=iron, iron_far=iron_far, copper=copper)


# --- paths ------------------------------------------------------------------------------------------------------
def test_script_output_env_config_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORIO_SCRIPT_OUTPUT", str(tmp_path / "so"))
    assert paths.script_output() == tmp_path / "so"
    monkeypatch.delenv("FACTORIO_SCRIPT_OUTPUT")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"script_output": str(tmp_path / "cfgso")}))
    monkeypatch.setattr(paths, "CONFIG_PATH", cfg)
    assert paths.script_output() == tmp_path / "cfgso"
    cfg.write_text("{}")
    monkeypatch.setenv("FACTORIO_USER_DIR", str(tmp_path / "ud"))
    assert paths.script_output() == tmp_path / "ud" / "script-output"
    cfg.write_text(json.dumps({"factorio_user_dir": str(tmp_path / "cfgud")}))
    monkeypatch.delenv("FACTORIO_USER_DIR")
    assert paths.script_output() == tmp_path / "cfgud" / "script-output"


# --- world ------------------------------------------------------------------------------------------------------
def test_load_basics(synth):
    d, t = synth
    w = World.load(d)
    assert w.bbox == (0, 0, 64, 32) and w.surface == "nauvis" and w.run_id == "1_1"
    assert w.is_water(45, 12) and not w.is_water(45, 31) and w.is_void(45, 31) and not w.is_void(45, 12)
    assert not w.is_water(-5, 0) and not w.is_known(-5, 0) and w.is_known(63, 31)
    assert w.resource_at(3, 3) == ("iron-ore", 100) and w.resource_at(9, 3) is None
    assert int(w.water.sum()) == len(t["lake"]) + len(t["puddle"])


def test_patches(synth):
    w = World.load(synth[0])
    ps = w.patches()
    iron = [p for p in ps if p.name == "iron-ore"]
    assert len(iron) == 2  # 2-tile gap merged, 4-tile gap separate
    main = max(iron, key=lambda p: p.amount)
    assert main.size == 36 + 24 and main.amount == 6000 and main.bbox == (2, 2, 14, 8)
    assert main.tiles == set(synth[1]["iron"])
    cx = np.mean([x + 0.5 for x, _ in synth[1]["iron"]])
    assert abs(main.center[0] - cx) < 1e-9 and abs(main.center[1] - 5.0) < 1e-9
    assert not any(p.name == "copper-ore" for p in ps)  # 12 tiles < 20
    assert any(p.name == "copper-ore" and p.size == 12 for p in w.patches(min_tiles=10))
    oil = [p for p in ps if p.name == "crude-oil"]
    assert len(oil) == 1 and oil[0].size == 2 and oil[0].amount == 600000  # wells grouped, no min size
    assert ps[0].amount >= ps[-1].amount
    assert main.outline() < main.tiles and (5, 5) not in main.outline()
    assert not main.edge  # fully inside the export
    edge = World.load(synth[0]).patches(min_tiles=1, name="crude-oil")
    assert edge[0].edge is False


def test_patch_gap_parameter(tmp_path):
    tiles = [(x, 5) for x in range(0, 10)] + [(x, 5) for x in range(12, 22)]  # gap of 2 (x=10,11)
    res = [["stone", x + 0.5, y + 0.5, 10] for x, y in tiles]
    w = World.load(write_export(tmp_path / "e", {(0, 0): {"resources": res}}))
    assert len(w.patches(min_tiles=5)) == 1
    assert len(w.patches(min_tiles=5, gap=1)) == 2
    assert len(w.patches(min_tiles=5, gap=0)) == 2


def test_water_bodies_and_nearest(synth):
    w = World.load(synth[0])
    bodies = w.water_bodies()
    assert len(bodies) == 1 and bodies[0].tiles == synth[1]["lake"]
    assert bodies[0].bbox == (39, 10, 51, 18)
    assert len(w.water_bodies(min_tiles=1)) == 2
    (tx, ty), b = w.nearest_water_tile(0, 0)
    assert b is bodies[0] and (tx, ty) in b.tiles
    assert w.nearest_water(0, 0, min_tiles=1).size == 2  # the puddle is closer
    assert w.nearest_water(60, 14) is bodies[0]


def test_shoreline_matches_rule(synth):
    w = World.load(synth[0])
    b = w.water_bodies()[0]
    got = set(w.shoreline(b, terrain_only=True))
    ref = brute_shoreline(b.tiles, lambda x, y: w.is_known(x, y) and not w.is_void(x, y))
    assert got == ref and got
    assert (45, 9, 8) in got and (45, 18, 0) in got and (39, 14, 4) in got and (50, 14, 12) in got
    assert (38, 12, 4) not in got and (39, 12, 4) not in got  # (39,12) is water; 3x2 block not complete


def test_shoreline_skips_hard_blocked(tmp_path):
    lake = {(x, y) for x in range(10, 20) for y in range(10, 20)}
    obs = [["cliff", "cliff", 15, 8.5, "west-to-east", [13, 8, 17, 10]], ["tree", "t", 12.5, 9.5, "", [12, 9, 13, 10]]]
    ent = [["stone-furnace", 6, 15, 0, {"type": "furnace", "b": [5, 14, 7, 16]}]]
    w = World.load(write_export(tmp_path / "e", {(0, 0): {"water": lake, "obstacles": obs, "entities": ent}}))
    b = w.water_bodies()[0]
    free = set(w.shoreline(b))
    allv = set(w.shoreline(b, terrain_only=True))
    assert (15, 9, 8) in allv and (15, 9, 8) not in free  # cliff
    assert (12, 9, 8) in free                                # tree: clearable, still offered
    assert (6, 15, 4) not in free and (9, 15, 4) in free     # furnace box 5..6 x 14..15 is off the pump tile 9


def test_spatial_queries_and_grid(synth):
    w = World.load(synth[0])
    assert {o.kind for o in w.obstacles_in((0, 0, 64, 32))} == {"tree", "rock", "cliff", "enemy"}
    assert [o.name for o in w.obstacles_in((19, 20, 21, 21))] == ["tree-01"]
    assert w.obstacles_in((0, 0, 5, 5)) == []
    ents = w.entities_in((50, 20, 64, 30))
    assert {e.name for e in ents} == {"assembling-machine-1", "ghost:transport-belt"}
    gb = [e for e in ents if e.name.startswith("ghost:")][0]
    assert gb.extra["tags"] == {"claude_plan": "p1"} and gb.dir == 4 and gb.box == (58, 22, 59, 23)
    assert w.entities_in((0, 0, 50, 20)) == []

    g = w.grid()
    assert (g.x0, g.y0, g.w, g.h) == (0, 0, 64, 32)
    assert g.state_at(45, 12) == BLOCKED and g.is_water(45, 12)
    assert g.state_at(45, 31) == BLOCKED and g.void[31, 45]            # void
    assert g.state_at(20, 20) == CLEARABLE and g.state_at(19, 20) == CLEARABLE  # tree box
    assert g.state_at(24, 12) == CLEARABLE                              # rock
    assert g.state_at(30, 12) == BLOCKED                                # cliff (whole box)
    assert g.state_at(29, 18) == BLOCKED and g.state_at(29, 17) == FREE  # rotated cliff: exact tiles only
    assert g.state_at(60, 5) == BLOCKED                                 # enemy
    assert g.state_at(55, 22) == BLOCKED and g.tag_at(55, 22) == "existing:assembling-machine-1@0"
    assert g.tag_at(58, 22) == "existing:ghost:transport-belt@4"
    assert g.free(3, 3) and g.resource(3, 3) == "iron-ore" and g.resource(9, 3) is None
    assert g.to_clear([(20, 20), (24, 12), (3, 3)]) == [(20.3, 20.7, "tree-01"), (24, 12, "big-rock")]

    m = w.grid((-4, -4, 10, 10))
    assert m.state_at(-1, -1) == BLOCKED and m.void[0, 0]  # outside the export = unknown = blocked
    assert m.resource(3, 3) == "iron-ore"
    m2 = w.grid((10, 10, 20, 20), margin=3)
    assert m2.rect == (7, 7, 23, 23)


# --- grid -------------------------------------------------------------------------------------------------------
def test_grid_occupy_release_copy():
    g = Grid(-5, -5, 20, 10)
    g.terrain[0, 0] = g.state[0, 0] = CLEARABLE
    assert g.free(0, 0) and not g.free(-5, -5) and not g.free(100, 0)
    assert g.can_place([(-5, -5)]) and not g.can_place([(-5, -5)], allow_clear=False)
    assert not g.can_place([(15, 0)])  # out of bounds
    c = g.copy()
    g.occupy([(0, 0), (1, 0), (99, 99)], "smelter")
    assert g.state_at(0, 0) == BLOCKED and g.tag_at(1, 0) == "smelter" and not g.can_place([(0, 0)])
    assert c.free(0, 0) and c.tag_at(0, 0) is None  # copy unaffected
    g.release([(0, 0), (1, 0)])
    assert g.free(0, 0) and g.tag_at(0, 0) is None


def test_free_rect():
    g = Grid(0, 0, 30, 20)
    g.state[:, 10] = BLOCKED                       # wall at x=10
    g.res[0:5, 20:30] = g._res_id("iron-ore")
    assert g.free_rect(4, 4, near=(2, 2)) == (0, 0)
    r = g.free_rect(12, 3, near=(0, 0))            # wider than the left area -> right side
    assert r[0] >= 11 and g.can_place([(x, y) for x in range(r[0], r[0] + 12) for y in range(r[1], r[1] + 3)])
    r = g.free_rect(5, 5, near=(25, 0), avoid_resources=True)
    assert r[1] >= 5
    assert g.free_rect(5, 5, near=(25.5, 0)) == (23, 0)
    assert g.free_rect(5, 5, within=(0, 0, 10, 20), near=(29, 19)) == (5, 15)
    assert g.free_rect(31, 2) is None and g.free_rect(11, 21) is None
    g.state[:] = BLOCKED
    assert g.free_rect(1, 1) is None


def test_to_ascii(synth):
    w = World.load(synth[0])
    g = w.grid()
    g.occupy([(0, 0)], "plan")
    a = g.to_ascii((0, 0, 64, 32)).splitlines()
    assert len(a) == 32 and all(len(r) == 64 for r in a)
    assert a[0][0] == "@" and a[3][3] == "i" and a[12][45] == "~" and a[31][45] == " " and a[20][20] == "t"
    assert a[22][55] == "E" and a[12][30] == "#" and a[0][63] == "."
    assert g.to_ascii((0, 0, 1, 1), tag_chars={"plan": "P"}) == "P"


# --- from_link (fake link) --------------------------------------------------------------------------------------
def test_from_link_polls_and_loads(synth, monkeypatch):
    d, _ = synth
    so = d.parent.parent.parent.parent  # .../claude/map/nauvis/1_1 -> script-output
    monkeypatch.setenv("FACTORIO_SCRIPT_OUTPUT", str(so))
    calls = []

    def fake(op, **args):
        calls.append((op, args))
        if op == "export_map":
            return {"run_id": "1_1", "dir": "claude/map/nauvis/1_1", "chunks": 2, "tick": 5, "done": False}
        return {"done": len(calls) >= 3, "written": 2, "total": 2}

    w = World.from_link(area=(0, 0, 64, 32.5), link=fake)
    assert w.bbox == (0, 0, 64, 32)
    assert calls[0] == ("export_map", {"area": {"x0": 0, "y0": 0, "x1": 64, "y1": 33}})
    assert [c[0] for c in calls[1:]] == ["export_status", "export_status"]


# --- real export captured from the engine ----------------------------------------------------------------------
def test_real_export_matches_engine():
    truth = json.loads((DATA / "lake_truth.json").read_text())
    w = World.load(DATA / "lake_export")
    assert int(w.water.sum()) == truth["water_tiles"]
    b = w.nearest_water(0, 0)
    shore = set(w.shoreline(b, terrain_only=True))
    eng = {tuple(t) for t in truth["pump_manual_ok"]}
    g = w.grid(b.bbox, margin=4)
    free = {t for t in shore if g.state_at(t[0], t[1]) == FREE}
    assert eng <= shore            # never miss a spot the engine allows
    assert free == eng             # on obstacle-free tiles we agree exactly with can_place_entity(manual)
    assert set(w.shoreline(b)) >= eng
