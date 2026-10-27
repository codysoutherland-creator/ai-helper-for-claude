"""Offline tests for planner.blocks: geometry, ports, rates, research flags, static checks in every orientation."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner.gamedata import GameData  # noqa: E402
from factorio_claude.planner.testing import load_fixture, fixture_seeds  # noqa: E402
from blockcheck import check  # noqa: E402

SIDES = ["north", "east", "south", "west"]


@pytest.fixture(scope="module")
def fresh():
    return load_fixture(1001)


@pytest.fixture(scope="module")
def gd_all(fresh):
    """Fixture game data with every recipe enabled (as after research)."""
    raw = copy.deepcopy(fresh[1].raw)
    for r in raw["recipes"].values():
        r["enabled"] = True
    return GameData(raw)


def ok(b, gd, ore=None):
    errs = check(b, gd, ore)
    assert not errs, "\n".join([b.name] + errs[:20])


def test_vanilla_geometry_matches_fixture(fresh):
    gd = fresh[1]
    v = B.vanilla()
    for n in v.raw["entities"]:
        for d in (0, 4, 8, 12):
            assert v.footprint(n, 0.5, 0.5, d) == gd.footprint(n, 0.5, 0.5, d), n
    assert v.fluid_connections("boiler", 1.5, 1, 4) == gd.fluid_connections("boiler", 1.5, 1, 4)


def test_options_fresh_vs_researched(fresh, gd_all):
    o = B.options(fresh[1])
    assert o["drill"] == "burner-mining-drill" and o["inserter"] == "burner-inserter"
    assert o["pole"] is None and not o["underground"] and not o["splitter"] and not o["steam"]
    o = B.options(gd_all)
    assert o["drill"] == "electric-mining-drill" and o["underground"] and o["steam"]
    assert B.options(None)["drill"] == "electric-mining-drill"  # no research info -> everything allowed


@pytest.mark.parametrize("side", SIDES)
@pytest.mark.parametrize("mode", ["mixed_lane", "separate"])
def test_smelter_burner(side, mode, fresh):
    gd = fresh[1]
    b = B.smelter_array(6, "stone-furnace", mode, side, gd=gd)
    ok(b, gd)
    assert b.requires == []  # buildable on a fresh map
    assert b.counts()["stone-furnace"] == 6 and b.counts()["burner-inserter"] == 12
    assert b.rates["iron-ore"] == pytest.approx(-6 * 60 / 3.2) and b.rates["iron-plate"] == pytest.approx(6 * 60 / 3.2)
    assert b.rates["coal"] == pytest.approx(-6 * 90 * 60 / 4000)
    out = b.port("belt_out")
    assert out.item == "iron-plate" and out.direction == {"north": 0, "east": 4, "south": 8, "west": 12}[side]
    ins = [p for p in b.ports if p.kind == "belt_in"]
    assert {p.item for p in ins} == {"iron-ore", "coal"}
    if mode == "mixed_lane":
        assert {(p.x, p.y) for p in ins} == {(ins[0].x, ins[0].y)} and {p.lane for p in ins} == {"left", "right"}
    manual = [e for e in b.entities if (e.extra or {}).get("fuel") == "manual"]
    assert len(manual) == 6  # output burner inserters


def test_smelter_electric_and_limits(gd_all):
    b = B.smelter_array(10, "steel-furnace", "mixed_lane", "north", "inserter", gd=gd_all, ore="copper-ore")
    ok(b, gd_all)
    assert b.port("pole") and b.rates["power_kw"] > 0 and b.rates["copper-plate"] == pytest.approx(10 * 2 * 60 / 3.2)
    b = B.smelter_array(4, "electric-furnace", gd=gd_all, inserters="inserter", output_side="east")
    ok(b, gd_all)
    assert b.info["fuel_mode"] == "none" and "coal" not in b.rates
    b = B.smelter_array(30, "stone-furnace", gd=gd_all)
    assert any("exceeds one lane" in n for n in b.notes)
    b = B.smelter_array(4, "stone-furnace", gd=gd_all, ore="stone")
    assert b.rates["stone-brick"] == pytest.approx(4 * 60 / 3.2) and b.rates["stone"] == pytest.approx(-8 * 60 / 3.2)


@pytest.mark.parametrize("boilers,engines", [(1, 2), (1, 1), (2, 4), (3, 5), (4, 8), (5, 10)])
@pytest.mark.parametrize("side", SIDES)
def test_steam_power(boilers, engines, side, fresh, gd_all):
    b = B.steam_power(boilers, engines, gd=gd_all, water_side=side)
    ok(b, gd_all)
    c = b.counts()
    assert c["boiler"] == boilers and c["steam-engine"] == engines and c["burner-inserter"] == boilers
    assert b.rates["power_kw"] == pytest.approx(min(engines * 900, boilers * 1800))
    assert b.rates["coal"] == pytest.approx(-boilers * 1800 * 60 / 4000)
    assert b.info["offshore_pumps"] == 1
    assert b.port("pipe_in").item == "water" and b.port("belt_in").item == "coal"
    assert set(B.steam_power(1, gd=fresh[1]).requires) >= {"boiler", "steam-engine"}  # research needed


def test_steam_ratio_notes(gd_all):
    b = B.steam_power(20, gd=gd_all)
    assert b.info["offshore_pumps"] == 1 and b.rates["power_kw"] == 36000
    assert B.steam_power(21, gd=gd_all).info["offshore_pumps"] == 2


def _nearest_patch(world, name):
    ps = [p for p in world.patches() if p.name == name]
    return min(ps, key=lambda p: p.center[0] ** 2 + p.center[1] ** 2)


@pytest.mark.parametrize("seed", fixture_seeds())
@pytest.mark.parametrize("side", SIDES)
def test_mining_outpost_on_fixture(seed, side, gd_all):
    world, _ = load_fixture(seed)
    patch = _nearest_patch(world, "iron-ore")
    grid = world.grid(world.bbox)
    b = B.mining_outpost(patch, "electric-mining-drill", "transport-belt", "small-electric-pole", side, gd=gd_all,
                         grid=grid)
    ok(b, gd_all, ore=patch.tiles)
    n = b.counts()["electric-mining-drill"]
    assert n == b.info["drills"] and b.rates["iron-ore"] == pytest.approx(n * 30)
    assert b.info["covered"] >= 0.85 * len(patch.tiles)
    assert b.port("belt_out").direction == {"north": 0, "east": 4, "south": 8, "west": 12}[side]
    # every entity is on placeable ground in map coords
    tiles = set()
    for e in b.placed():
        tiles |= gd_all.footprint(e.name, e.x, e.y, e.direction)
    assert grid.can_place(tiles)
    # map coords: drills sit where the ore is
    for e in b.placed():
        if e.name == "electric-mining-drill":
            area = {(x, y) for x in range(int(e.x - 2.5), int(e.x + 2.5)) for y in range(int(e.y - 2.5), int(e.y + 2.5))}
            assert area & patch.tiles
            assert all(grid.resource(*t) in (None, "iron-ore") for t in area)  # no mixed ore


def test_mining_outpost_limits(gd_all, fresh):
    world, gd = fresh
    patch = _nearest_patch(world, "copper-ore")
    b = B.mining_outpost(patch, gd=gd_all, max_drills=8, output_side="east")
    ok(b, gd_all, ore=patch.tiles)
    assert b.counts()["electric-mining-drill"] == 8
    b = B.mining_outpost(patch, gd=gd, max_drills=6)  # fresh map -> burner drills, no poles
    ok(b, gd, ore=patch.tiles)
    assert b.counts().get("burner-mining-drill") == 6 and "small-electric-pole" not in b.counts()
    assert b.requires == [] and b.rates["coal"] < 0
    sq = {(x, y) for x in range(10) for y in range(10)}
    b = B.mining_outpost(sq, gd=gd_all, resource="iron-ore", pole="medium-electric-pole")
    ok(b, gd_all, ore=sq)
    assert b.origin[0] <= 0 and b.origin[1] <= 0


@pytest.mark.parametrize("side", SIDES)
def test_burner_column(side, fresh):
    gd = fresh[1]
    b = B.burner_mining_column(5, output_side=side, gd=gd, resource="iron-ore")
    ok(b, gd)
    assert b.counts()["burner-mining-drill"] == 5 and b.rates["iron-ore"] == pytest.approx(5 * 15)
    assert b.requires == []
    b1 = B.burner_mining_column(3, gd=gd, sides=1)
    ok(b1, gd)
    assert b1.size == (3, 6)


@pytest.mark.parametrize("side", SIDES)
def test_helpers(side, gd_all, fresh):
    gd = fresh[1]
    for b in (B.bus_start(["iron-plate", "copper-plate", "coal", "stone", "iron-gear-wheel"], 5, gd, output_side=side),
              B.lane_merge("iron-ore", "coal", gd, output_side=side),
              B.inserter_tap(gd, output_side=side)):
        ok(b, gd)
        assert b.requires == []
    b = B.bus_start(["a", "b", "c", "d", "e"], 5, gd)
    assert b.size == (6, 5)
    b = B.coal_splitter(gd_all, output_side=side)
    ok(b, gd_all)
    assert B.coal_splitter(gd).requires == ["splitter"]


def test_rotation_roundtrip(gd_all):
    b = B.steam_power(3, gd=gd_all)
    r = b.rotated(4)
    assert [(e.x, e.y, e.direction) for e in r.entities] == [(e.x, e.y, e.direction) for e in b.entities]
    assert r.ports == b.ports
    m = b.at(100, -50)
    assert m.placed()[0].x == b.entities[0].x + 100 and m.placed_ports()[0].y == b.ports[0].y - 50


def _validate_cases(world, gd, gda):
    iron = _nearest_patch(world, "iron-ore")
    grid = world.grid(world.bbox)
    return {
        "smelter_mixed": (B.smelter_array(6, gd=gd), gd, None),
        "smelter_separate": (B.smelter_array(4, gd=gd, fuel_mode="separate", output_side="east"), gd, None),
        "smelter_electric": (B.smelter_array(4, "steel-furnace", inserters="inserter", gd=gda), gda, None),
        "smelter_efurnace": (B.smelter_array(3, "electric-furnace", inserters="inserter", input_inserter="fast-inserter",
                                             gd=gda, ore="stone"), gda, None),
        "steam_3": (B.steam_power(3, gd=gda), gda, None),
        "steam_2x3": (B.steam_power(2, 3, gd=gda, water_side="south"), gda, None),
        "outpost": (B.mining_outpost(iron, gd=gda, grid=grid, max_drills=12, output_side="west"), gda, world),
        "outpost_burner": (B.mining_outpost(iron, gd=gd, grid=grid, max_drills=6), gd, world),
        "burner_column": (B.burner_mining_column(4, gd=gd, resource="iron-ore"), gd, None),
        "bus": (B.bus_start(["iron-plate", "coal"], 4, gd), gd, None),
        "lane_merge": (B.lane_merge("iron-ore", "coal", gd), gd, None),
        "inserter_tap": (B.inserter_tap(gd), gd, None),
        "coal_splitter": (B.coal_splitter(gda), gda, None),
    }


def test_planner_validator(fresh, gd_all):
    validate = pytest.importorskip("factorio_claude.planner.validate").validate
    world, gd = fresh
    for label, (b, g, w) in _validate_cases(world, gd, gd_all).items():
        man = [(e.x, e.y) for e in b.placed() if (e.extra or {}).get("fuel") == "manual"]
        rep = validate(b.placed(), g, w, {"ports": b.placed_ports(), "manual_fuel": man})
        assert rep.ok, label + "\n" + rep.summary()
        # the straightener belt behind a two-sided merge carries nothing by design
        other = [i for i in rep.warnings if not (i.kind == "belt_unused" and label in ("lane_merge", "smelter_separate"))]
        assert not other, label + "\n" + rep.summary()
