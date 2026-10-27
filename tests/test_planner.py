"""Starter-base planner (placer.py / plan.py) on the fixture maps: every plan passes the validator, is buildable with
the research the stage claims, keeps ore and coal on separate lanes, and the variants differ."""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude.planner import placer  # noqa: E402
from factorio_claude.planner.blocks import PE  # noqa: E402
from factorio_claude.planner.gamedata import rotate  # noqa: E402
from factorio_claude.planner.plan import (Plan, build_minutes, raw_materials, sentences,  # noqa: E402
                                          world_without_plan)
from factorio_claude.planner.testing import fixture_seeds, load_fixture  # noqa: E402
from factorio_claude.planner.validate import validate  # noqa: E402

SEEDS = fixture_seeds()
FAST = dict(keep=2, want_sites=1)  # tests: first good site only (the default searches a few more)
_cache: dict = {}


def plans_for(seed, stage):
    key = (seed, stage)
    if key not in _cache:
        world, gd = load_fixture(seed)
        _cache[key] = placer.plan_variants(world, gd, {"stage": stage}, **FAST)
    return _cache[key]


def footprint_overlaps(gd, ents):
    seen, bad = {}, []
    for e in ents:
        for t in gd.footprint(e.name, e.x, e.y, e.direction):
            if t in seen:
                bad.append((t, seen[t], e.name))
            seen[t] = e.name
    return bad


@pytest.mark.parametrize("seed", SEEDS)
def test_bootstrap_variants(seed):
    world, gd = load_fixture(seed)
    plans = plans_for(seed, "bootstrap")
    assert [p.variant for p in plans] == list(placer.DEFAULT_VARIANTS)
    for p in plans:
        assert p.stage == "bootstrap" and p.validation["ok"], p.validation["errors"][:3]
        # independent re-check with the plan's own expectations
        rep = validate(p.entities, gd, world, p.expect())
        assert rep.ok, rep.summary()
        assert not footprint_overlaps(gd, p.entities)
        # buildable from nothing: everything unlocked on a fresh map, nothing electric
        assert not p.stats["needs_research"]
        assert all(gd.can_build(e.name) for e in p.entities)
        assert not any((gd.entity(e.name).get("energy_source") or {}).get("type") == "electric" for e in p.entities)
        # mines on every ore + coal, a hub with all three products
        assert set(p.stats["per_min"]) == {"iron-plate", "copper-plate", "stone-brick"}
        assert all(v > 0 for v in p.stats["per_min"].values())
        assert set(p.stats["mines"]) == {"ore:iron-ore", "fuel:iron-ore", "ore:copper-ore", "fuel:copper-ore",
                                         "ore:stone", "fuel:stone"}
        assert len(p.stats["ports"]) == 3  # one bus lane per product
    # genuinely different: output grows compact -> balanced -> expandable
    iron = [p.stats["per_min"]["iron-plate"] for p in plans]
    assert iron[0] < iron[1] and iron[0] < iron[2]
    assert len({p.stats["footprint"][0] * p.stats["footprint"][1] for p in plans}) == 3


@pytest.mark.parametrize("seed", SEEDS)
def test_electric_stage(seed):
    world, gd = load_fixture(seed)
    p = placer.plan_starter_base(world, gd, "electric starter base", "compact", **FAST)
    assert p.stage == "electric" and p.validation["ok"]
    c = p.stats["counts"]
    assert c.get("offshore-pump") == 1 and c.get("boiler", 0) >= 1 and c.get("steam-engine", 0) >= 2
    assert c.get("electric-mining-drill", 0) >= 6 and c.get("inserter", 0) > 0
    assert p.stats["generation_kw"] >= p.stats["power_kw"]
    # fresh map: the electric entities need research -> listed, validated with assume_research
    assert "electric-mining-drill" in p.stats["needs_research"] and p.stats["assume_research"]
    assert any("research" in w for w in p.warnings)
    assert validate(p.entities, gd, world, p.expect()).ok
    assert not footprint_overlaps(gd, p.entities)
    # the pump stands on a shoreline spot of the world
    pump = next(e for e in p.entities if e.name == "offshore-pump")
    spots = {(x, y, d) for b in world.water_bodies(30) for x, y, d in world.shoreline(b)}
    assert (math.floor(pump.x), math.floor(pump.y), pump.direction) in spots


def test_electric_with_research_needs_nothing():
    world, gd = load_fixture(SEEDS[0])
    from copy import deepcopy
    from factorio_claude.planner.gamedata import GameData
    raw = deepcopy(gd.raw)
    for r in raw["recipes"].values():
        r["enabled"] = True
    gd2 = GameData(raw)
    assert placer.default_stage(gd2) == "electric" and placer.default_stage(gd) == "bootstrap"
    p = placer.plan_starter_base(world, gd2, None, "compact", **FAST)
    assert p.stage == "electric" and not p.stats["needs_research"] and p.validation["ok"]


def _lane_of_drop(drill, belt):
    """Lane (relative to the belt's flow) a drill drops onto: the half of the belt tile nearest the drill
    (measured in 2.1.23)."""
    vx, vy = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}[belt.direction]
    dx, dy = drill.x - belt.x, drill.y - belt.y
    cross = vx * dy - vy * dx  # > 0: drill on the right of the flow
    return "right" if cross > 0 else "left"


@pytest.mark.parametrize("seed", SEEDS)
def test_ore_and_coal_lanes(seed):
    """Ore mines drop on one lane only; the coal belt side-loads onto the other lane of that ore belt."""
    world, gd = load_fixture(seed)
    p = plans_for(seed, "bootstrap")[0]
    belts = {(math.floor(e.x), math.floor(e.y)): e for e in p.entities if e.name == "transport-belt"}
    for ore in ("iron-ore", "copper-ore", "stone"):
        mx, my, n = p.stats["mines"][f"ore:{ore}"]
        drills = [e for e in p.entities if e.name == "burner-mining-drill" and abs(e.x - mx) < 3 * n + 4
                  and abs(e.y - my) < 3 * n + 4 and world.resource_at(e.x, e.y) and
                  world.resource_at(e.x, e.y)[0] == ore]
        assert drills
        lanes = set()
        for d in drills:
            vx, vy = rotate(*gd.entity(d.name)["vector_to_place_result"], d.direction)
            lanes.add(_lane_of_drop(d, belts[(math.floor(d.x + vx), math.floor(d.y + vy))]))
        assert len(lanes) == 1, (ore, lanes)
        # the coal belt's last tile points sideways into a tile of a straight belt from the other side
        cx, cy, _n = p.stats["mines"][f"fuel:{ore}"]
        # follow the coal belt from its mine port
        t, seen = (cx, cy), set()
        while t in belts and t not in seen:
            seen.add(t)
            b = belts[t]
            vx, vy = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}[b.direction]
            nxt = (t[0] + vx, t[1] + vy)
            if nxt in belts and belts[nxt].direction not in (b.direction, (b.direction + 8) % 16):
                target = belts[nxt]
                tvx, tvy = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}[target.direction]
                behind = (nxt[0] - tvx, nxt[1] - tvy)
                if behind in belts and belts[behind].direction == target.direction:  # side-load, not a curve
                    coal_lane = _lane_of_drop(b, target)  # nearest half = the lane it lands on
                    assert coal_lane not in lanes or len(seen) < 2, (ore, coal_lane, lanes)
                    break
            t = nxt


def test_plan_roundtrip_and_preview(tmp_path):
    p = plans_for(SEEDS[0], "bootstrap")[0]
    f = p.save(tmp_path)
    q = Plan.load(f)
    assert q.id == p.id and q.entities == p.entities and q.deconstruct == p.deconstruct and q.stats == p.stats
    args = p.preview_args(group="g1", show=False)
    assert args["id"] == p.id and args["group"] == "g1" and args["show"] is False
    assert len(args["entities"]) == len(p.entities)
    assert all(4 <= len(r) <= 6 for r in args["entities"])
    assert args["validation"] == {"ok": True, "errors": 0, "warnings": len(p.validation["warnings"])}
    assert args["summary"] and all(isinstance(s, str) for s in args["summary"])
    brief = p.brief()
    assert "entities" in brief and isinstance(brief["entities"], int) and len(str(brief)) < 3000


def test_summary_plain_text():
    for p in plans_for(SEEDS[0], "bootstrap"):
        s = p.summary
        assert not re.search(r"\*\*|^#|\n|`|^\s*[-*] ", s, re.M), s
        assert "[gps=" in s and "per minute" in s and "belts" in s and "Helper build time" in s
        assert p.stats["build_min"]["scratch"] >= p.stats["build_min"]["stocked"] > 0


def test_world_without_plan():
    world, gd = load_fixture(SEEDS[0])
    from factorio_claude.planner.world import MapEntity
    w = world_without_plan(world, Plan("p1", "t", "compact", "bootstrap", "", [PE("stone-furnace", 3.0, 3.0)]))
    w.entities = list(w.entities) + [
        MapEntity("ghost:transport-belt", 0.5, 0.5, 0, {"tags": {"claude_plan": "p1"}}, (0, 0, 1, 1)),
        MapEntity("ghost:transport-belt", 1.5, 0.5, 0, {"tags": {"claude_plan": "other"}}, (1, 0, 2, 1)),
        MapEntity("stone-furnace", 3.0, 3.0, 0, {}, (2, 2, 4, 4))]
    w._chunk_ent = dict(w._chunk_ent)
    w._chunk_ent[(0, 0)] = list(w._chunk_ent.get((0, 0), [])) + [len(w.entities) - 3, len(w.entities) - 2,
                                                                   len(w.entities) - 1]
    out = world_without_plan(w, Plan("p1", "t", "compact", "bootstrap", "", [PE("stone-furnace", 3.0, 3.0)]))
    names = [(e.name, e.x) for e in out.entities_in((0, 0, 5, 5))]
    assert ("ghost:transport-belt", 1.5) in names  # someone else's ghost stays
    assert ("ghost:transport-belt", 0.5) not in names and ("stone-furnace", 3.0) not in names


def test_parse_goal_and_sizes():
    g = placer.parse_goal("set me up with a bigger electric starter base")
    assert g["stage"] == "electric" and g["scale"] == 1.5
    assert placer.parse_goal("small burner start")["stage"] == "bootstrap"
    assert placer.parse_goal(None) == {}
    world, gd = load_fixture(SEEDS[0])
    pl = placer.Planner(world, gd, {"furnaces": {"iron": 10}, "stage": "bootstrap"}, "compact")
    fl = {f.key: f.drills for f in pl.flows()}
    assert pl.furnaces["iron"] == 10
    # stone furnace: 0.3125 ore/s each, burner drill 0.25/s -> 13 drills; coal 10 x 90 kW / 4 MJ = 0.225/s
    # + the burner input inserters swinging ~44 % of the time at 144 kW (+10 %) -> 0.43/s -> 2 drills
    assert fl[("ore", "iron-ore")] == 13 and fl[("fuel", "iron-ore")] == 2


def test_drill_column_layouts():
    _w, gd = load_fixture(SEEDS[0])
    for layout, lane, d in (("east", "right", 12), ("west", "left", 4)):
        b = placer.drill_column(gd, 3, "electric-mining-drill", resource="iron-ore", layout=layout)
        drills = [e for e in b.entities if e.name == "electric-mining-drill"]
        assert len(drills) == 3 and {e.direction for e in drills} == {d}
        assert b.ports[0].lane == lane and b.ports[0].kind == "belt_out"
    b = placer.drill_column(gd, 5, "burner-mining-drill", resource="coal")
    assert all(e.extra == {"fuel": "manual"} for e in b.entities if e.name == "burner-mining-drill")


def test_hub_ports_and_bus():
    _w, gd = load_fixture(SEEDS[0])
    hub = placer.build_hub(gd, [("iron-ore", 4), ("copper-ore", 2), ("stone", 2)], [2, 0, 1], bus_len=5, spare=2)
    ins = hub.inputs()
    assert [p.item for p in ins] == ["stone", "iron-ore", "copper-ore"] == hub.ores
    outs = [p for p in hub.block.ports if p.kind == "belt_out"]
    assert len(outs) == 3 and len({p.y for p in outs}) == 1  # adjacent lanes ending on one row
    assert sorted(p.x for p in outs) == list(range(min(p.x for p in outs), min(p.x for p in outs) + 3))
    assert len(hub.reserved) == 2 * (5 + 3 + 1)
    r = hub.rotated(1)
    assert r.block.size == hub.block.size[::-1] and len(r.reserved) == len(hub.reserved)


def test_materials_and_build_time():
    _w, gd = load_fixture(SEEDS[0])
    base, secs, crafted = raw_materials(gd, {"transport-belt": 10, "burner-mining-drill": 1})
    # 10 belts = 5 crafts of (1 plate + 1 gear), gear = 2 plates -> 15; drill: 3 gears + 3 plates -> 9, + a stone
    # furnace (5 stone)
    assert base["iron-plate"] == 15 + 9 and base["stone"] == 5
    assert secs > 0 and crafted["iron-gear-wheel"] == 8
    t = build_minutes(base, secs, 11, 10)
    assert t["scratch"] >= t["stocked"] >= 1


def test_sentences():
    assert sentences("Makes 7.5 plates. Hub at [gps=1,2]. Done!") == ["Makes 7.5 plates.", "Hub at [gps=1,2].",
                                                                      "Done!"]


def test_plan_error_without_patches():
    world, gd = load_fixture(SEEDS[0])
    w = world_without_plan(world, Plan("x", "t", "c", "b", "", []))
    w.res = w.res * 0  # no ore anywhere
    w._water_cache = None
    with pytest.raises(placer.PlanError):
        placer.plan_starter_base(w, gd, None, "compact", **FAST)
