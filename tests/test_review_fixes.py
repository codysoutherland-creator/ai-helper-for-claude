"""Regression tests for the v3 review findings (engine behaviour measured on test slots in 2.1.23):
side-loading onto undergrounds, burner inserter self-refuel, underground / pipe-to-ground pairing, existing belts and
inserters next to a plan, burner inserters' own coal draw, coal mines never cut below demand, cost-gate wording."""
from __future__ import annotations

import math
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude.planner import placer  # noqa: E402
from factorio_claude.planner.blocks import Port  # noqa: E402
from factorio_claude.planner.router import route_belt  # noqa: E402
from factorio_claude.planner.testing import fixture_seeds, load_fixture  # noqa: E402
from factorio_claude.planner.validate import validate  # noqa: E402
from validator_cases import E, N, S, W, pe  # noqa: E402
from validator_util import gd_all, make_grid  # noqa: E402

GD = gd_all()
SRC = [{"kind": "belt_in", "item": "iron-ore", "x": -0.5, "y": 0.5, "direction": E, "lane": "left", "per_min": 20},
       {"kind": "belt_in", "item": "coal", "x": -0.5, "y": 0.5, "direction": E, "lane": "right", "per_min": 20}]
MANUAL = {"manual_fuel": ["burner-inserter"]}


def kinds(rep):
    return {i.kind for i in rep.errors}


def _smelt_tail(x, y):
    """belt north at (x, y) -> burner inserter -> stone furnace -> burner inserter -> chest"""
    return [pe("transport-belt", x, y, N), pe("burner-inserter", x, y - 1, S), pe("stone-furnace", x + 0.5, y - 2.5),
            pe("burner-inserter", x, y - 4, S), pe("wooden-chest", x, y - 5)]


def _sideload(kind):
    ents = [pe("transport-belt", 0.5, 0.5, E), pe("transport-belt", 1.5, 0.5, E), pe("transport-belt", 2.5, 0.5, E)]
    if kind == "belt":
        ents += [pe("transport-belt", 3.5, 0.5, N), pe("transport-belt", 3.5, 1.5, N), pe("transport-belt", 3.5, -0.5, N)]
        return ents + _smelt_tail(3.5, -1.5)
    if kind == "ug_in":  # entrance facing north, exit 3 tiles on
        ents += [pe("underground-belt", 3.5, 0.5, N, type="input"), pe("underground-belt", 3.5, -2.5, N, type="output")]
        return ents + _smelt_tail(3.5, -3.5)
    ents += [pe("underground-belt", 3.5, 3.5, N, type="input"), pe("underground-belt", 3.5, 0.5, N, type="output")]
    return ents + _smelt_tail(3.5, -0.5)


def test_sideload_onto_underground_passes_one_lane():
    ex = dict(MANUAL, sources=SRC)
    assert validate(_sideload("belt"), GD, make_grid(), ex).ok
    # entrance: only the incoming lane on its open back half (the right lane here = coal) gets through
    rep = validate(_sideload("ug_in"), GD, make_grid(), ex)
    assert not rep.ok and "machine_input" in kinds(rep)
    assert any(i.kind == "ug_sideload" and "right lane" in i.msg for i in rep.warnings)
    # exit: only the lane on its open front half (left = iron) gets through: the furnace has no coal
    rep = validate(_sideload("ug_out"), GD, make_grid(), ex)
    assert any(i.kind == "fuel" and "stone-furnace" in i.msg for i in rep.errors)


def test_burner_inserter_self_refuel_needs_a_fuel_taking_target():
    src = [{"kind": "belt_in", "item": "iron-plate", "x": -0.5, "y": 0.5, "direction": E, "lane": "left"},
           {"kind": "belt_in", "item": "coal", "x": -0.5, "y": 0.5, "direction": E, "lane": "right"}]
    belt = [pe("transport-belt", x + 0.5, 0.5, E) for x in range(4)] + [pe("wooden-chest", 4.5, 0.5)]
    ents = belt + [pe("burner-inserter", 1.5, 1.5, N), pe("assembling-machine-1", 1.5, 3.5, recipe="iron-gear-wheel"),
                   pe("small-electric-pole", 3.5, 2.5)]
    rep = validate(ents, GD, make_grid(), {"sources": src, "power": "external"})
    assert any(i.kind == "fuel" and "burner-inserter at (1.5, 1.5)" in i.msg for i in rep.errors), rep.summary()
    # into a furnace (takes coal as fuel) it keeps itself going
    ents = belt + [pe("burner-inserter", 1.5, 1.5, N), pe("stone-furnace", 2, 3)]
    rep = validate(ents, GD, make_grid(), {"sources": [dict(src[0], item="iron-ore"), src[1]]})
    assert not any(i.kind == "fuel" and "burner-inserter" in i.msg for i in rep.errors), rep.summary()


def test_underground_pairs_with_opposite_facing_one():
    ents = [pe("underground-belt", 0.5, 0.5, E, type="input"), pe("underground-belt", 4.5, 0.5, E, type="output"),
            pe("underground-belt", 2.5, 0.5, W, type="input"), pe("underground-belt", -1.5, 0.5, W, type="output")]
    rep = validate(ents, GD, make_grid(), {})
    assert any(i.kind == "underground" and "opposite-facing" in i.msg for i in rep.errors), rep.summary()
    # perpendicular ones in between are ignored (engine)
    ents = [pe("underground-belt", 0.5, 0.5, E, type="input"), pe("underground-belt", 4.5, 0.5, E, type="output"),
            pe("underground-belt", 2.5, 0.5, N, type="input"), pe("underground-belt", 2.5, -1.5, N, type="output")]
    rep = validate(ents, GD, make_grid(), {})
    assert not any(i.kind == "underground" for i in rep.errors), rep.summary()


def test_pipe_to_ground_skips_perpendicular():
    ents = [pe("pipe-to-ground", 0.5, 0.5, W), pe("pipe-to-ground", 2.5, 0.5, N), pe("pipe-to-ground", 4.5, 0.5, E)]
    rep = validate(ents, GD, make_grid(), {})
    bad = [i for i in rep.errors if i.kind == "pipe_to_ground"]
    assert not any("faces the same way" in i.msg for i in bad)
    assert {(i.x, i.y) for i in bad} <= {(2.5, 0.5)}, bad


MapEntity = namedtuple("MapEntity", "name x y dir extra box")


class FakeWorld:
    """World stand-in: a blank grid plus existing force entities (tagged like World.grid does)."""

    def __init__(self, ents):
        self.ents = ents

    def entities_in(self, rect):
        return list(self.ents)

    def grid(self, rect, margin=0):
        g = make_grid(rect=tuple(int(v) for v in rect))
        for e in self.ents:
            g.occupy([(math.floor(e.x), math.floor(e.y))], f"existing:{e.name}@{e.dir}")
        return g


def test_existing_belt_and_inserter_next_to_a_plan():
    line = [pe("transport-belt", x + 0.5, 0.5, E) for x in range(5)]
    ex = {"sources": [{"kind": "belt_in", "item": "iron-ore", "x": -0.5, "y": 0.5, "direction": E}],
          "sinks": [{"kind": "belt_out", "x": 5.5, "y": 0.5, "direction": E}]}
    assert validate(line, GD, FakeWorld([]), ex).ok
    # an existing belt pointing at the plan's belt would side-load foreign items onto it
    w = FakeWorld([MapEntity("transport-belt", 2.5, 1.5, N, {}, (2, 1, 3, 2))])
    rep = validate(line, GD, w, ex)
    assert "existing_belt" in kinds(rep), rep.summary()
    # an existing inserter dropping onto it, too
    w = FakeWorld([MapEntity("burner-inserter", 2.5, 1.5, S, {}, (2, 1, 3, 2))])
    rep = validate(line, GD, w, ex)
    assert "existing_link" in kinds(rep), rep.summary()


def test_router_avoids_existing_belt_outputs_and_inserter_reach():
    g = make_grid(rect=(-5, -5, 15, 10))
    g.occupy([(2, 1)], "existing:transport-belt@0")      # outputs north into (2, 0)
    g.occupy([(5, -1)], "existing:burner-inserter@0")    # picks up from (5, -2), drops onto (5, 0)
    g.terrain[g._ij(2, 1)] = g.terrain[g._ij(5, -1)] = 2
    ents = route_belt(g, GD, Port("belt_out", "iron-ore", 0, 0, E), Port("belt_in", "iron-ore", 8, 0, E))
    tiles = {(math.floor(e.x), math.floor(e.y)) for e in ents}
    assert (2, 0) not in tiles and (5, 0) not in tiles, sorted(tiles)


def test_burner_inserters_own_coal_counts():
    """1 furnace: coal 1.35/min for the furnace; its burner input inserter burns about as much again."""
    src = [{"kind": "belt_in", "item": "iron-ore", "x": -0.5, "y": 0.5, "direction": E, "lane": "left", "per_min": 40},
           {"kind": "belt_in", "item": "coal", "x": -0.5, "y": 0.5, "direction": E, "lane": "right", "per_min": 1.6}]
    ents = [pe("transport-belt", x + 0.5, 0.5, E) for x in range(3)] + [
        pe("burner-inserter", 1.5, 1.5, N), pe("stone-furnace", 2, 3)]
    ok = validate(ents, GD, make_grid(), {"sources": src, "manual_fuel": ["burner-inserter"], "strict_fuel": True})
    assert not [i for i in ok.errors if i.kind == "belt_supply"], ok.summary()
    rep = validate(ents, GD, make_grid(), {"sources": src, "strict_fuel": True})
    assert any(i.kind == "belt_supply" and "coal" in i.msg for i in rep.errors), rep.summary()
    rep = validate(ents, GD, make_grid(), {"sources": src})  # not strict: only a warning
    assert not any(i.kind == "belt_supply" for i in rep.errors)


def test_coal_mine_never_cut_below_demand(monkeypatch):
    """A cramped coal patch (2+ drills don't fit) must not give a coal-starved plan that claims full output."""
    seed = fixture_seeds()[0]
    world, gd = load_fixture(seed)
    real = placer.place_mine

    def cramped(grid, gd_, patch, n, *a, **k):
        if patch.name == "coal" and n >= 2:
            return None
        return real(grid, gd_, patch, n, *a, **k)
    monkeypatch.setattr(placer, "place_mine", cramped)
    pl = placer.Planner(world, gd, {"stage": "bootstrap"}, "balanced", keep=2, want_sites=1, time_budget=20)
    flows = {f.key: f for f in pl.flows()}
    need = {k: f.min_drills for k, f in flows.items() if k[0] == "fuel"}
    try:
        plan = pl.run()
    except placer.PlanError:
        assert max(need.values()) >= 2
        return  # refusing is fine; a starved plan is not
    for k, n in need.items():
        assert plan.stats["mines"][f"fuel:{k[1]}"][2] >= n


def test_bootstrap_summary_states_hand_fuel_time():
    seed = fixture_seeds()[0]
    world, gd = load_fixture(seed)
    plan = placer.plan_starter_base(world, gd, {"stage": "bootstrap"}, "compact", keep=2, want_sites=1)
    assert plan.stats["hand_fuel_min"] == 22
    assert "lasts about 22 minutes" in plan.summary and "Helper: refuel" in plan.summary


def test_preview_rows_carry_manual_fuel_marker():
    from factorio_claude.planner.blocks import PE
    from factorio_claude.planner.plan import Plan
    p = Plan("x", "t", "compact", "bootstrap", "s", [
        PE("burner-mining-drill", 1, 1, 4, extra={"fuel": "manual"}), PE("transport-belt", 2.5, 0.5, 0),
        PE("underground-belt", 3.5, 0.5, 0, extra={"type": "input"}), PE("stone-furnace", 5, 5)])
    rows = p.rows()
    assert rows[0] == ["burner-mining-drill", 1, 1, 4, None, {"fuel": "manual"}]
    assert rows[1] == ["transport-belt", 2.5, 0.5, 0]
    assert rows[2] == ["underground-belt", 3.5, 0.5, 0, None, "input"]
