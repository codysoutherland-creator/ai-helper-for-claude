"""Router tests: synthetic grids (exact behaviour) + all fixture maps (ore patches -> hubs 60-120 tiles away, around
water). The same fixture scenarios are placed in-engine by tests/live_router.py."""
import copy
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from factorio_claude.planner.gamedata import GameData  # noqa: E402
from factorio_claude.planner.grid import BLOCKED, CLEARABLE, Grid  # noqa: E402
from factorio_claude.planner.router import (EAST, NORTH, SOUTH, VEC, WEST, PE, NoRoute, Port, place_poles,  # noqa: E402
                                            rev, route_belt, route_pipe, underground_for, underground_pair)
from factorio_claude.planner.testing import fixture_dir, fixture_seeds  # noqa: E402

SEEDS = fixture_seeds()


@pytest.fixture(scope="module")
def gd():
    return GameData.load(fixture_dir(SEEDS[0]) / "data.json")


@pytest.fixture(scope="module")
def gd_ug(gd):
    """Same data with underground belts and pipe-to-ground researched."""
    raw = copy.deepcopy(gd.raw)
    for r in ("underground-belt", "fast-underground-belt", "pipe-to-ground", "small-electric-pole"):
        raw["recipes"][r]["enabled"] = True
    return GameData(raw)


def tile(e):
    return math.floor(e.x), math.floor(e.y)


def chain(ents, gd, start, goal=None):
    """Follow the belt from the start tile; returns visited tiles in order. Asserts: every belt points to the next
    entity (or the end), no belt is fed head-on, underground pairs match, every route entity is on the chain."""
    m = {tile(e): e for e in ents}
    t = (math.floor(start.x), math.floor(start.y))
    move = start.direction
    seen = []
    while t in m:
        e = m[t]
        assert e.direction != rev(move), f"head-on belt at {t}"
        seen.append(t)
        d = e.direction
        if e.name.startswith(("underground", "fast-underground")) and e.extra["type"] == "input":
            assert d == move, "underground entered from the side"
            md = int(gd.entity(e.name)["max_underground_distance"])
            out = next(((t[0] + VEC[d][0] * k, t[1] + VEC[d][1] * k) for k in range(1, md + 1)
                        if (o := m.get((t[0] + VEC[d][0] * k, t[1] + VEC[d][1] * k))) and o.name == e.name
                        and o.extra and o.extra["type"] == "output"), None)
            assert out and m[out].direction == d, f"no matching underground exit for {t}"
            seen.append(out)
            t = out
        t = (t[0] + VEC[d][0], t[1] + VEC[d][1])
        move = d
        if len(seen) > len(ents) + 2:
            raise AssertionError("loop")
    if goal is not None and not getattr(goal, "lane", None):
        assert seen[-1] == (math.floor(goal.x), math.floor(goal.y)) and m[seen[-1]].direction == goal.direction
    plain = [tile(e) for e in ents if not (e.extra and e.extra.get("role"))]
    assert sorted(seen) == sorted(plain)
    return seen


def open_grid(w=40, h=30, x0=-5, y0=-15):
    return Grid(x0, y0, w, h)


# --- belts on synthetic grids ---------------------------------------------------------------------------------
def test_straight_and_turns(gd):
    g = open_grid()
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST))
    assert [tile(e) for e in ents] == [(x, 0) for x in range(11)] and all(e.direction == EAST for e in ents)
    assert all(e.x % 1 == 0.5 and e.y % 1 == 0.5 for e in ents)
    s, goal = Port("belt_out", "x", 0, 2, EAST), Port("belt_in", "x", 6, 9, SOUTH)
    ents = route_belt(g, gd, s, goal)
    seen = chain(ents, gd, s, goal)
    assert len(seen) == 6 + 7 + 1
    assert sum(1 for a, b in zip(ents, ents[1:]) if a.direction != b.direction) == 1  # one turn only
    # the grid is marked with the belt direction
    assert g.state_at(3, 0) == BLOCKED and g.tag_at(3, 0) == "route:transport-belt@4"
    # start facing the goal's opposite direction: must turn around via two turns, never head-on
    s2, g2 = Port("belt_out", "x", 20, -5, WEST), Port("belt_in", "x", 25, -5, EAST)
    chain(route_belt(g, gd, s2, g2), gd, s2, g2)


def test_avoids_blocked_and_water(gd):
    g = open_grid()
    g.occupy([(5, y) for y in range(-15, 10)], "existing:stone-wall")
    g.water[20 + 5, 10 + 5] = True
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST))
    assert all(tile(e)[0] != 5 or tile(e)[1] >= 10 for e in ents)
    assert len(ents) > 11


def test_undergrounds_only_when_enabled(gd, gd_ug):
    def walled(width):
        g = open_grid()
        g.occupy([(5 + i, y) for i in range(width) for y in range(-15, 15)], "existing:stone-wall")
        return g
    s, goal = Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 15, 0, EAST)
    with pytest.raises(NoRoute):  # fresh map: no undergrounds unlocked
        route_belt(walled(1), gd, s, goal)
    ents = route_belt(walled(4), gd_ug, s, goal)  # yellow: positions up to 5 apart = 4-tile gap
    chain(ents, gd_ug, s, goal)
    ug = [e for e in ents if e.name == "underground-belt"]
    assert [e.extra["type"] for e in ug] == ["input", "output"] and all(e.direction == EAST for e in ug)
    assert tile(ug[1])[0] - tile(ug[0])[0] == 5
    with pytest.raises(NoRoute):
        route_belt(walled(5), gd_ug, s, goal)
    ents = route_belt(walled(5), gd_ug, s, goal, belt="fast-transport-belt")  # fast: 7 apart
    assert {e.name for e in ents} == {"fast-transport-belt", "fast-underground-belt"}
    assert route_belt(walled(1), gd, s, goal, allow_underground=True)  # explicit override
    assert underground_for(gd, "express-transport-belt") == ("express-underground-belt", 9)


def test_underground_not_intercepted(gd_ug):
    """A foreign underground of the same kind inside the span would grab the belt: the router must not jump over it."""
    g = open_grid()
    g.occupy([(5, y) for y in range(-15, 15)], "existing:stone-wall")
    g.occupy([(6, y) for y in range(-15, 15)], "existing:underground-belt")
    s, goal = Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 15, 0, EAST)
    with pytest.raises(NoRoute):
        route_belt(g, gd_ug, s, goal)
    ents = route_belt(g, gd_ug, s, goal, belt="fast-transport-belt")  # a different tier passes under it
    assert any(e.name == "fast-underground-belt" for e in ents)


def test_trees_cost_and_clear(gd):
    g = open_grid()
    for y in range(-15, 15):  # a full-height band of trees: must go through, the tile is reported for clearing
        i, j = y - g.y0, 5 - g.x0
        g.terrain[i, j] = g.state[i, j] = CLEARABLE
        g.clearables[(5, y)] = [("tree-01", 5.4, y + 0.6)]
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST))
    assert len(ents) == 11
    assert g.to_clear([tile(e) for e in ents]) == [(5.4, 0.6, "tree-01")]
    # a short detour (5 tiles of trees with free space around) is preferred over clearing
    g = open_grid()
    for y in range(-2, 3):
        g.terrain[y - g.y0, 5 - g.x0] = g.state[y - g.y0, 5 - g.x0] = CLEARABLE
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST), clear_cost=20)
    assert all(g.terrain[tile(e)[1] - g.y0, tile(e)[0] - g.x0] == 0 for e in ents)


def test_never_fed_by_foreign_belt(gd):
    g = open_grid()
    foreign = [PE("transport-belt", 3.5, -0.5, SOUTH)]  # points into (3, 0)
    g.occupy([(3, -1)], "existing:transport-belt")
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST), others=foreign)
    assert (3, 0) not in {tile(e) for e in ents}
    # same thing learnt from the grid tags of an earlier route (no `others` needed)
    g = open_grid()
    first = route_belt(g, gd, Port("belt_out", "x", 3, -6, SOUTH), Port("belt_in", "x", 3, -1, SOUTH))
    assert tile(first[-1]) == (3, -1)
    ents = route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST))
    assert (3, 0) not in {tile(e) for e in ents}


@pytest.mark.parametrize("lane,side", [("left", (5, -1)), ("right", (5, 1)), (None, None)])
def test_sideload_existing_belt(gd, lane, side):
    g = open_grid()
    main = [PE("transport-belt", x + 0.5, 0.5, EAST) for x in range(0, 10)]
    g.occupy([tile(e) for e in main], "existing:transport-belt")
    s = Port("belt_out", "coal", 5, -8 if lane != "right" else 8, SOUTH if lane != "right" else NORTH)
    ents = route_belt(g, gd, s, Port("belt_in", "coal", 5, 0, EAST, lane=lane), others=main)
    last = ents[-1]
    nxt = (tile(last)[0] + VEC[last.direction][0], tile(last)[1] + VEC[last.direction][1])
    assert nxt == (5, 0) and last.direction in (NORTH, SOUTH)  # from the side, never head-on
    if side:
        assert tile(last) == side
    chain(ents, gd, s)


def test_lane_port_adds_blocker(gd):
    g = open_grid()
    goal = Port("belt_in", "copper-plate", 4, 8, SOUTH, lane="right")
    ents = route_belt(g, gd, Port("belt_out", "copper-plate", 4, -8, SOUTH), goal)
    roles = {e.extra["role"]: tile(e) for e in ents if e.extra and e.extra.get("role")}
    assert roles == {"lane_target": (4, 8), "lane_blocker": (4, 7)}
    last = [e for e in ents if not (e.extra and e.extra.get("role"))][-1]
    assert tile(last) == (3, 8) and last.direction == EAST  # right lane of a south belt = its west side
    # same lane on both ends: a plain route keeps the lanes, no blocker
    ents = route_belt(open_grid(), gd, Port("belt_out", "x", 4, -8, SOUTH, lane="left"),
                      Port("belt_in", "x", 4, 8, SOUTH, lane="left"))
    assert not any(e.extra for e in ents)


def test_no_route_raises(gd):
    g = open_grid()
    g.occupy([(x, y) for x in range(-5, 35) for y in (2, -2)], "wall")
    g.occupy([(8, y) for y in range(-2, 3)], "wall")
    with pytest.raises(NoRoute):
        route_belt(g, gd, Port("belt_out", "x", 0, 0, EAST), Port("belt_in", "x", 10, 0, EAST))
    with pytest.raises(NoRoute):  # start tile taken
        route_belt(g, gd, Port("belt_out", "x", 8, 0, EAST), Port("belt_in", "x", 10, 0, EAST))


# --- pipes ----------------------------------------------------------------------------------------------------
def test_pipe_avoids_foreign_fluids(gd):
    g = open_grid()
    foreign = [PE("pipe", x + 0.5, 1.5, 0) for x in range(2, 8)]
    g.occupy([tile(e) for e in foreign], "existing:pipe")
    ents = route_pipe(g, gd, Port("pipe_out", "water", 0, 0, EAST), Port("pipe_in", "water", 10, 0, EAST),
                      others=foreign)
    for e in ents:
        x, y = tile(e)
        assert all((x + vx, y + vy) not in {tile(f) for f in foreign} for vx, vy in VEC.values()), (x, y)
    assert tile(ents[0]) == (0, 0) and tile(ents[-1]) == (10, 0)
    # the foreign line is also known from grid tags alone
    ents2 = route_pipe(g, gd, Port("pipe_out", "water", 0, 4, EAST), Port("pipe_in", "water", 10, 4, EAST))
    assert all(tile(e)[1] != 2 for e in ents2)


def test_pipe_to_ground(gd, gd_ug):
    def walled(width):
        g = open_grid()
        g.occupy([(3 + i, y) for i in range(width) for y in range(-15, 15)], "existing:stone-wall")
        return g
    s, goal = Port("pipe_out", "water", 0, 0, EAST), Port("pipe_in", "water", 20, 0, EAST)
    with pytest.raises(NoRoute):
        route_pipe(walled(1), gd, s, goal)
    ents = route_pipe(walled(9), gd_ug, s, goal)  # positions up to 10 apart
    ptg = [e for e in ents if e.name == "pipe-to-ground"]
    assert len(ptg) == 2 and ptg[0].direction == WEST and ptg[1].direction == EAST  # entrance faces back
    assert tile(ptg[1])[0] - tile(ptg[0])[0] <= 10
    with pytest.raises(NoRoute):
        route_pipe(walled(10), gd_ug, s, goal)
    # pipe-to-ground connects only along the route: it may pass right next to a foreign pipe
    g = open_grid()
    g.occupy([(x, 1) for x in range(-5, 35)], "existing:pipe")
    ents = route_pipe(g, gd_ug, Port("pipe_out", "water", 0, 0, EAST), Port("pipe_in", "water", 25, 0, EAST))
    assert all(e.name == "pipe-to-ground" or tile(e)[1] < 0 or tile(e) in {(0, 0), (25, 0)} for e in ents)
    assert underground_pair(gd, "pipe-to-ground", 0, 0, EAST, 10)[0].direction == WEST


# --- poles ----------------------------------------------------------------------------------------------------
def powered(gd, e, poles):
    box = gd.box(e.name, e.x, e.y, e.direction)
    for p in poles:
        r = gd.pole(p.name)[0]
        if box[0] < p.x + r and box[2] > p.x - r and box[1] < p.y + r and box[3] > p.y - r:
            return True
    return False


def connected(gd, poles):
    seen, todo = {0}, [0]
    while todo:
        i = todo.pop()
        for j, q in enumerate(poles):
            if j not in seen and math.hypot(poles[i].x - q.x, poles[i].y - q.y) <= min(
                    gd.pole(poles[i].name)[1], gd.pole(q.name)[1]) + 1e-9:
                seen.add(j)
                todo.append(j)
    return len(seen) == len(poles)


@pytest.mark.parametrize("pole", ["small-electric-pole", "medium-electric-pole"])
def test_poles_cover_and_connect(gd, pole):
    g = Grid(-10, -10, 80, 40)
    ents = []
    for c in range(6):
        ents.append(PE("assembling-machine-1", c * 6 + 1.5, 1.5, 0, recipe="iron-gear-wheel"))
        ents.append(PE("inserter", c * 6 + 1.5, 3.5, 0))
    ents.append(PE("stone-furnace", 50, 1, 0))  # burner: no pole needed
    for e in ents:
        g.occupy(gd.footprint(e.name, e.x, e.y, e.direction), "plan")
    src = PE("small-electric-pole", 60.5, 20.5, 0)
    g.occupy([(60, 20)], "existing:small-electric-pole")
    poles = place_poles(g, gd, ents, pole, sources=[src])
    assert all(powered(gd, e, poles) for e in ents if e.name != "stone-furnace")
    assert connected(gd, poles + [src])
    taken = set()
    for e in ents:
        taken |= gd.footprint(e.name, e.x, e.y, e.direction)
    assert not taken & {tile(p) for p in poles}
    assert all(g.tag_at(*tile(p)) == f"pole:{pole}@0" for p in poles)
    assert len(poles) <= (10 if pole == "small-electric-pole" else 9)  # cover (6 resp. 3 + 2 links) + wire chain


def test_poles_unreachable(gd):
    g = Grid(0, 0, 20, 20)
    g.water[:, 10:] = True
    g.state[:, 10:] = BLOCKED
    e = PE("assembling-machine-1", 3.5, 3.5, 0)
    g.occupy(gd.footprint(e.name, e.x, e.y), "plan")
    with pytest.raises(NoRoute):  # source pole across 10 tiles of water: no wire chain possible
        place_poles(g, gd, [e], "small-electric-pole", sources=[PE("small-electric-pole", 40.5, 3.5)])


# --- fixture maps ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module", params=SEEDS)
def scenario(request):
    import router_scenarios as rs
    return rs.build(request.param)


def test_fixture_routes(scenario):
    sc, gd, base, world = scenario, scenario["gd"], scenario["base"], scenario["world"]
    crossing = 0
    for ore, r in sc["routes"].items():
        ents, s, goal = r["ents"], r["start"], r["goal"]
        chain(ents, gd, s, goal)
        d = math.dist(r["patch_center"], r["hub"])
        assert 60 - 1 <= d <= 120 + 1
        for e in ents:
            x, y = tile(e)
            assert base.state_at(x, y) != BLOCKED, (ore, x, y)
            assert not world.is_water(x, y)
        names = {e.name for e in ents}
        assert ("underground-belt" in names) <= r["underground"]  # no undergrounds unless allowed
        manhattan = abs(goal.x - s.x) + abs(goal.y - s.y)
        assert len(ents) <= 2 * manhattan + 30, (ore, len(ents), manhattan)
        crossing += __import__("router_scenarios").water_on_line(world, (s.x, s.y), (goal.x, goal.y)) > 0
    assert crossing >= 1  # at least one straight line is blocked by water: the router went around it
    # routes never share tiles
    all_tiles = [tile(e) for r in sc["routes"].values() for e in r["ents"]]
    assert len(all_tiles) == len(set(all_tiles))


def test_fixture_lanes_pipes_poles(scenario):
    sc, gd = scenario, scenario["gd"]
    side = next(f for f in sc["fixtures"] if f["kind"] == "lane_sideload")
    last = side["ents"][-1]
    assert last.direction == SOUTH  # left lane of an east belt = its north side
    port = next(f for f in sc["fixtures"] if f["kind"] == "lane_port")
    assert {e.extra["role"] for e in port["ents"] if e.extra} == {"lane_target", "lane_blocker"}
    for k, p in sc["pipes"].items():
        assert ("pipe-to-ground" in {e.name for e in p["ents"]}) <= p["underground"]
        assert tile(p["ents"][-1]) == tuple(p["goal"]) or p["ents"][-1].name == "pipe-to-ground"
    pc = sc["poles"]
    assert all(powered(gd, e, pc["poles"] + [pc["source"]]) for e in pc["consumers"])
    assert connected(gd, [pc["source"]] + pc["poles"])
