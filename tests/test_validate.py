"""Offline tests for planner/validate.py. The same scenarios are cross-checked in-engine by tests/live_validator.py
(22 + 7 cases: every verdict matched what the engine actually did)."""
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factorio_claude import blueprints as bp  # noqa: E402
from factorio_claude.planner.validate import Issue, Report, validate  # noqa: E402
from validator_cases import GEN, LIVE, E, N, S, W, gen_entities, ore, pe  # noqa: E402
from validator_util import gd_all, gd_fresh, make_grid  # noqa: E402

GD = gd_all()


def run(c, gd=GD):
    return validate(c["entities"], gd, make_grid(c["resources"], c["water"]), c["expect"])


def kinds(rep):
    return {i.kind for i in rep.errors}


def msgs(rep, kind=None):
    return [i.msg for i in rep.errors if kind is None or i.kind == kind]


@pytest.mark.parametrize("c", LIVE, ids=[c["name"] for c in LIVE])
def test_case_verdict(c):
    rep = run(c)
    if c["want"]:
        assert c["want"] <= kinds(rep), rep.summary(20)
    else:
        assert rep.ok and not rep.errors, rep.summary(20)
    for i in rep.errors + rep.warnings:
        assert isinstance(i.x, float) and isinstance(i.y, float) and "(" in i.msg


# ---- precise messages / positions for the adversarial cases ---------------------------------------------------
def by_name(name):
    return next(c for c in LIVE if c["name"] == name)


def test_broken_belt_points_at_gap():
    rep = run(by_name("broken_belt"))
    de = [i for i in rep.errors if i.kind == "belt_dead_end"]
    assert [(i.x, i.y) for i in de] == [(2.5, 1.5)] and "east into nothing at (3, 1)" in de[0].msg
    st = [i for i in rep.errors if i.kind == "inserter_starved"][0]
    assert (st.x, st.y) == (7.5, 1.5) and "line starting at transport-belt at (4.5, 1.5) is fed by nothing" in st.msg


def test_reversed_tile_is_headon():
    rep = run(by_name("reversed_belt_tile"))
    h = [i for i in rep.errors if i.kind == "belt_headon"]
    assert len(h) == 1 and (h[0].x, h[0].y) == (2.5, 1.5) and "transport-belt at (3.5, 1.5) facing west" in h[0].msg


def test_inserter_facing_nothing_tiles():
    rep = run(by_name("inserter_facing_nothing"))
    assert "nothing to pick up from at tile (7, 0)" in msgs(rep, "inserter_pickup")[0]
    assert "empty ground at tile (7, 2)" in msgs(rep, "inserter_drop")[0]


def test_pipe_gap_names_the_gap():
    m = msgs(run(by_name("steam_pipe_gap")), "fluid_source")
    assert m == ["boiler at (8.5, 1) gets no water: its pipe network has no water source; gap at tile (4, 1) "
                 "between pipe at (5.5, 1.5) and pipe at (3.5, 1.5)"]
    c = by_name("steam_ok")   # no pipes at all between pump and boiler: point at the boiler's open water inlet
    ents = [e for e in c["entities"] if not (e["name"] == "pipe" and e["x"] >= 2)]
    m = msgs(validate(ents, GD, make_grid(c["resources"], c["water"]), c["expect"]), "fluid_source")
    assert m[0].endswith("nothing is attached to its connection at tile (6, 1) (west side)")


def test_ptg_wrong_pairing():
    m = msgs(run(by_name("steam_ptg_wrong")), "pipe_to_ground")
    assert any("faces the same way" in x for x in m)


def test_power_messages():
    m = msgs(run(by_name("steam_unpowered")), "power_coverage")
    assert m == ["electric-mining-drill at (17.5, -2.5) isn't inside any electric pole's supply area (nearest pole "
                 "small-electric-pole at (11.5, -2.5), 6.0 tiles away, supply reach 2.5)"]
    m = msgs(run(by_name("steam_two_networks")), "power_network")
    assert len(m) == 1 and "10.0 tiles" in m[0] and "wire reach 7.5" in m[0]


def test_offshore_and_drill_messages():
    assert "is not on a shoreline" in msgs(run(by_name("steam_offshore_off_shore")), "offshore")[0]
    assert "has no ore in its mining area (0, 2)..(1, 3)" in msgs(run(by_name("drill_off_ore")), "drill_resource")[0]


def test_overload_lane_and_rate():
    rep = run(by_name("overloaded_belt"))
    ov = [i for i in rep.errors if i.kind == "belt_overload"]
    assert len(ov) == 1 and (ov[0].x, ov[0].y) == (6.5, 0.5) and "left lane must carry 15.00/s" in ov[0].msg
    assert rep.stats["delivered_per_min"]["iron-plate"] == pytest.approx(450)
    assert run(by_name("curve_full_belt")).stats["delivered_per_min"]["iron-plate"] == pytest.approx(900)


def test_fuel_paths():
    rep = run(by_name("smelter_line_ok"))
    assert rep.ok
    # input burner inserters refuel themselves from the coal they pick up; furnaces get coal via the mixed lane
    rep = run(by_name("smelter_output_inserters_unfuelled"))
    assert sorted((i.x, i.y) for i in rep.errors if i.kind == "fuel") == [(7.5, 4.5), (9.5, 4.5)]


# ---- known-good generator blocks ------------------------------------------------------------------------------
def test_generator_blocks():
    good = {c["name"]: run(c) for c in GEN if not c["want"]}
    assert all(r.ok for r in good.values()), {k: r.summary() for k, r in good.items()}
    assert good["gen_smelter_column_electric"].stats["produced_per_min"]["iron-plate"] == pytest.approx(75)  # speed 2
    gears = good["gen_assembler_line_gears"]
    # AM1 gears want 2 plates/s; a basic inserter moves ~0.83/s: a warning, an error only when strict
    assert any(i.kind == "inserter_throughput" for i in gears.warnings)
    c = next(c for c in GEN if c["name"] == "gen_assembler_line_gears")
    strict = validate(c["entities"], GD, None, dict(c["expect"], strict=True))
    assert "inserter_throughput" in kinds(strict)


def test_mining_outpost_generator_west_column_unpowered():
    """blueprints.mining_outpost: poles only reach the east drills (confirmed in-engine)."""
    c = next(c for c in GEN if c["name"] == "gen_mining_outpost")
    rep = run(c)
    assert sorted((i.x, i.y) for i in rep.errors if i.kind == "power_coverage") == \
        [(1.5, 1.5), (1.5, 4.5), (1.5, 7.5), (1.5, 10.5)] and kinds(rep) == {"power_coverage"}


def test_solar_block_alone():
    rep = validate(gen_entities(bp.solar_block(2, 4, 2)), GD, make_grid())
    assert rep.ok and not rep.warnings


# ---- research -------------------------------------------------------------------------------------------------
def test_unresearched_entities():
    fresh = gd_fresh()
    c = by_name("underground_gap5")
    rep = run(c, fresh)
    assert kinds(rep) == {"research"} and "underground-belt isn't buildable yet" in msgs(rep)[0]
    rep = validate(c["entities"], fresh, make_grid(c["resources"]), dict(c["expect"], assume_research=True))
    assert rep.ok and rep.stats["requires_research"] == ["underground-belt"]
    assert any(i.kind == "research" for i in rep.warnings)
    # burner bootstrap entities are buildable on a fresh map
    assert run(by_name("belt_line_ok"), fresh).ok


def test_recipe_checks():
    fresh = gd_fresh()
    ents = [pe("assembling-machine-1", 1.5, 1.5, recipe="iron-gear-wheel")]
    rep = validate(ents, fresh, None, {"power": "external"})
    assert {"research"} <= kinds(rep)
    rep = validate([pe("assembling-machine-1", 1.5, 1.5, recipe="iron-plate")], GD, None)
    assert any("can't craft iron-plate" in m for m in msgs(rep, "recipe"))
    rep = validate([pe("assembling-machine-1", 1.5, 1.5)], GD, None)
    assert "has no recipe set" in msgs(rep, "recipe")[0]
    rep = validate([pe("wooden-chest", 0.5, 0.5, recipe="iron-gear-wheel")], GD, None)
    assert "can't have a recipe" in msgs(rep, "recipe")[0]


# ---- placement ------------------------------------------------------------------------------------------------
def test_placement_errors():
    rep = validate([pe("stone-furnace", 0.5, 0.5), pe("transport-belt", 3, 0.5, 2), pe("no-such-thing", 0, 0)],
                   GD, None, {"manual_fuel": True})
    k = kinds(rep)
    assert {"position", "unknown_entity"} <= k
    assert any("off-grid" in m and "(1, 1)" in m for m in msgs(rep, "position"))
    assert any("not north/east/south/west" in m for m in msgs(rep, "position"))
    rep = validate([pe("wooden-chest", 0.5, 0.5), pe("burner-inserter", 0.5, 0.5)], GD, None)
    assert "overlaps" in msgs(rep, "overlap")[0]


def test_terrain_and_existing_entities():
    g = make_grid(water=[(5, 5)])
    g.occupy([(2, 2)], "existing:wooden-chest")
    rep = validate([pe("wooden-chest", 5.5, 5.5), pe("wooden-chest", 2.5, 2.5)], GD, g)
    assert kinds(rep) == {"terrain"} and "stands on water at tile (5, 5)" in msgs(rep)[0]
    rep = validate([pe("stone-furnace", 3, 3)], GD, g, {"manual_fuel": True})
    assert "overlaps existing wooden-chest at tile (2, 2)" in msgs(rep, "terrain")[0]
    # an inserter may drop into an existing chest it only knows from the grid
    rep = validate([pe("wooden-chest", 2.5, 4.5), pe("burner-inserter", 2.5, 3.5, S)], GD, g,
                   {"manual_fuel": True, "containers": {(2.5, 4.5): ["coal"]}})
    assert rep.ok, rep.summary()
    # trees are clearable, counted in stats
    g2 = make_grid()
    g2.terrain[g2._ij(0, 0)] = g2.state[g2._ij(0, 0)] = 1
    g2.clearables[(0, 0)] = [("tree-01", 0.4, 0.6)]
    assert validate([pe("wooden-chest", 0.5, 0.5)], GD, g2).stats["to_clear"] == 1


# ---- fluids / power -------------------------------------------------------------------------------------------
def test_fluid_mix():
    c = by_name("steam_ok")
    # pipe the boiler's steam output back into the water line: water + steam in one network
    ents = [e for e in c["entities"] if e["name"] != "steam-engine"]
    ents += [pe("pipe", 8.5, -0.5), pe("pipe", 7.5, -0.5), pe("pipe", 6.5, -0.5), pe("pipe", 6.5, 0.5)]
    rep = validate(ents, GD, make_grid(c["resources"], c["water"]), c["expect"])
    assert msgs(rep, "fluid_mix") == ["one pipe network joins steam (boiler at (8.5, 1)) and water (offshore-pump at "
                                      "(0.5, 1.5)); fluids would mix"]


def test_power_capacity():
    ents = [pe("solar-panel", 1.5, 1.5), pe("small-electric-pole", 3.5, 1.5), pe("electric-mining-drill", 5.5, 1.5, E),
            pe("wooden-chest", 7.5, 1.5)]
    g = make_grid(ore("iron-ore", 3, 0, 7, 3))
    rep = validate(ents, GD, g)
    assert rep.ok and any(i.kind == "power_capacity" for i in rep.warnings)
    assert rep.stats["power"] == {"demand_kw": 90.0, "supply_kw": 60.0, "networks": 1, "poles": 1}
    assert "power_capacity" in kinds(validate(ents, GD, g, {"strict": True}))


def test_generator_without_pole():
    c = by_name("steam_ok")
    ents = [e for e in c["entities"] if e["name"] != "small-electric-pole"]
    rep = validate(ents, GD, make_grid(c["resources"], c["water"]), c["expect"])
    assert {"power_generator", "power_coverage"} <= kinds(rep)


# ---- inputs / outputs / API -----------------------------------------------------------------------------------
@dataclass
class Port:
    kind: str
    item: str
    x: float
    y: float
    direction: int = 0
    lane: str = None


@dataclass
class PE:
    name: str
    x: float
    y: float
    direction: int = 0
    recipe: str = None
    extra: dict = None


def test_object_and_list_inputs():
    ents = [PE(**e) for e in gen_entities(bp.smelter_column(2, furnace="electric-furnace"))]
    ports = [Port("belt_in", "iron-ore", 0.5, 6.5, N), Port("belt_out", "iron-plate", 6.5, -0.5, N),
             Port("pole", None, 1.5, 2.5)]
    rep = validate(ents, GD, None, {"ports": ports})
    assert rep.ok, rep.summary()
    lst = [["wooden-chest", 0.5, 0.5], ("burner-inserter", 1.5, 0.5, W)]
    rep = validate(lst, GD, None, {"manual_fuel": True})
    assert kinds(rep) == {"inserter_drop"}
    d = rep.to_dict()
    json.dumps(d)
    assert d["ok"] is False and d["errors"][0]["kind"] == "inserter_drop"
    assert isinstance(rep, Report) and isinstance(rep.errors[0], Issue) and "FAILED" in rep.summary()


def test_machine_io():
    # furnace fed only coal: nothing to smelt; assembler output never taken
    ents = [pe("wooden-chest", 0.5, 0.5), pe("burner-inserter", 1.5, 0.5, W), pe("stone-furnace", 3, 1)]
    rep = validate(ents, GD, None, {"containers": {(0.5, 0.5): ["coal"]}})
    assert "never receives anything it can smelt (gets coal)" in msgs(rep, "machine_input")[0]
    ents = [pe("wooden-chest", 0.5, 0.5), pe("inserter", 1.5, 0.5, W),
            pe("assembling-machine-1", 3.5, 1.5, recipe="iron-gear-wheel")]
    rep = validate(ents, GD, None, {"containers": {(0.5, 0.5): ["iron-plate"]}, "power": "external"})
    assert "nothing takes its output (iron-gear-wheel)" in msgs(rep, "machine_output")[0]
    rep = validate(ents, GD, None, {"containers": {(0.5, 0.5): ["copper-plate"]}, "power": "external"})
    assert any("doesn't take it" in m for m in msgs(rep, "inserter_starved"))
    assert any("never receives iron-plate" in m for m in msgs(rep, "machine_input"))


def test_world_context_fixture():
    from factorio_claude.planner.testing import load_fixture
    world, _ = load_fixture(1001)
    patch = min(world.patches(name="iron-ore"), key=lambda p: abs(p.center[0]) + abs(p.center[1]))
    g = world.grid(patch.bbox)
    spot = next((x, y) for x, y in sorted(patch.tiles, key=lambda t: (abs(t[0] - patch.center[0]) + abs(t[1] - patch.center[1])))
                if all((x + dx, y + dy) in patch.tiles for dx in (0, 1) for dy in (-2, -1, 0, 1))
                and g.can_place([(x + dx, y + dy) for dx in (0, 1) for dy in (-2, -1, 0, 1)], allow_clear=False))
    x, y = spot
    ents = [pe("burner-mining-drill", x + 1, y + 1, N), pe("wooden-chest", x + 0.5, y - 0.5)]
    rep = validate(ents, GD, world, {"manual_fuel": True})
    assert rep.ok, rep.summary()
    (wx, wy), _ = world.nearest_water_tile(0, 0)
    rep = validate([pe("wooden-chest", wx + 0.5, wy + 0.5)], GD, world)
    assert kinds(rep) == {"terrain"}


def test_performance_large_layout():
    ents, ports = [], []
    for k in range(8):
        col = gen_entities(bp.smelter_column(24))
        for e in col:
            e["x"] += k * 8
        ents += col
        ports += [{"kind": "belt_in", "x": 0.5 + 8 * k, "y": 48.5, "direction": N, "items": ["iron-ore", "coal"],
                   "per_min": 900}, {"kind": "belt_out", "x": 5.5 + 8 * k, "y": -0.5}]
    manual = [(e["x"], e["y"]) for e in ents if e["name"] == "burner-inserter" and (e["x"] - 4.5) % 8 == 0]
    t = time.time()
    rep = validate(ents, GD, make_grid(rect=(-20, -20, 100, 80)), {"ports": ports, "manual_fuel": manual,
                                                                     "rates": {"iron-plate": 3600}})
    assert time.time() - t < 3.0
    assert rep.ok, rep.summary()
    assert rep.stats["produced_per_min"]["iron-plate"] == pytest.approx(3600)


def test_manual_fuel_from_pe_extra():
    ents, fuel, _ = __import__("validator_cases")._drill_furnace()
    ents = [dict(e, extra={"fuel": "manual"}) if e["name"] != "wooden-chest" else e for e in ents]
    assert run({"entities": ents, "resources": ore("iron-ore", 0, 2, 1, 3), "water": [], "expect": {}}).ok
