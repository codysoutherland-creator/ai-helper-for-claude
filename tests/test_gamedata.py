"""GameData: offline checks on the fixture + live checks against a headless TEST server.

Live tests run only when FACTORIO_TEST_SLOT and FACTORIO_SCRIPT_OUTPUT point at a running test instance, e.g.
  powershell -File testenv/restart.ps1 -Name gamedata -Slot 3
  FACTORIO_TEST_SLOT=3 FACTORIO_SCRIPT_OUTPUT=testenv/inst/gamedata/write/script-output pytest tests/test_gamedata.py
(never point them at the real game: the geometry test enables all recipes and rewrites tiles).
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factorio_claude.planner.gamedata import UNSUPPORTED_GEOMETRY, GameData, tiles_of_box  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "data_fresh.json.gz"
FLUID_ENTITIES = ["offshore-pump", "boiler", "steam-engine", "pipe", "pipe-to-ground", "chemical-plant"]


@pytest.fixture(scope="module")
def gd():
    return GameData.load(FIXTURE)


def test_lookups(gd):
    assert gd.entity("assembling-machine-1")["crafting_speed"] == 0.5
    assert gd.recipe("iron-gear-wheel")["energy"] == 0.5
    assert gd.item("coal")["fuel_value"] == 4e6
    assert gd.is_fluid("water") and not gd.is_fluid("iron-plate")
    with pytest.raises(KeyError):
        gd.entity("nope")
    assert gd.quality("normal")["next"] == "uncommon"
    assert gd.qualities_unlocked() == ["normal"]
    assert gd.version_key.startswith("2.1.23;")


def test_research_state(gd):
    assert gd.enabled("iron-gear-wheel") and gd.enabled("stone-furnace")
    assert not gd.enabled("electronic-circuit") and not gd.enabled("underground-belt")
    assert gd.can_build("burner-mining-drill") and gd.can_build("transport-belt") and gd.can_build("burner-inserter")
    # 2.x fresh map: steam power (offshore pump, boiler, pipe) and automation need research
    for e in ("offshore-pump", "boiler", "pipe", "assembling-machine-1", "underground-belt", "inserter"):
        assert not gd.can_build(e), e
    assert gd.machines_for("smelting", buildable_only=True) == ["stone-furnace"]
    assert gd.drills_for("basic-solid", buildable_only=True) == ["burner-mining-drill"]


def test_size_and_footprint(gd):
    assert gd.size("boiler", 0) == (3, 2) and gd.size("boiler", 4) == (2, 3)
    assert gd.size("steam-engine", 12) == (5, 3)
    assert gd.footprint("assembling-machine-1", 0.5, 0.5, 0) == {(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1)}
    assert gd.footprint("stone-furnace", 0, 0, 4) == {(-1, -1), (0, -1), (-1, 0), (0, 0)}
    assert gd.footprint("boiler", 0.5, 0, 0) == {(x, y) for x in (-1, 0, 1) for y in (-1, 0)}
    assert gd.footprint("boiler", 0, 0.5, 4) == {(x, y) for x in (-1, 0) for y in (-1, 0, 1)}
    assert gd.footprint("transport-belt", 3.5, -2.5, 8) == {(3, -3)}
    # not-rotatable entities ignore direction
    assert gd.footprint("thruster", 0, 0.5, 4) == gd.footprint("thruster", 0, 0.5, 0)
    with pytest.raises(ValueError):
        gd.footprint("curved-rail-a", 0, 0, 0)
    with pytest.raises(ValueError):
        gd.footprint("pipe", 0.5, 0.5, 2)


def test_snap(gd):
    assert gd.snap("assembling-machine-1", 3.2, 4.9) == (3.5, 4.5)
    assert gd.snap("stone-furnace", 3.2, 4.6) == (3.0, 5.0)
    assert gd.snap("boiler", 0.2, 0.2, 4) == (0.0, 0.5)
    assert gd.snap("car", 1.3, 2.2) == (1.296875, 2.19921875)


def test_fluid_connections(gd):
    # pipe-to-ground facing north: normal connection north, underground one south
    full = gd.fluid_connections_full("pipe-to-ground", 0.5, 0.5, 0)
    assert sorted((c["type"], c["dir"], c["target_tile"]) for c in full) == [("normal", 0, (0, -1)), ("underground", 8, (0, 1))]
    assert gd.fluid_connections("pipe-to-ground", 0.5, 0.5, 4) == [(1, 0, 4)]
    assert sorted(gd.fluid_connections("pipe", 0.5, 0.5, 0)) == [(-1, 0, 12), (0, -1, 0), (0, 1, 8), (1, 0, 4)]
    # boiler north at (0.5, 1): water in/out west+east on the lower row, steam out north
    assert sorted(gd.fluid_connections("boiler", 0.5, 1, 0)) == [(-2, 1, 12), (0, -1, 0), (2, 1, 4)]
    assert sorted(gd.fluid_connections("steam-engine", 0.5, 0.5, 4)) == [(-3, 0, 12), (3, 0, 4)]
    steam = [c for c in gd.fluid_connections_full("boiler", 0.5, 1, 0) if c["filter"] == "steam"]
    assert steam[0]["flow"] == "output"


def test_throughput(gd):
    assert gd.belt_throughput("transport-belt") == 15
    assert gd.belt_throughput("fast-transport-belt") == 30
    assert gd.inserter_throughput("inserter") == pytest.approx(60 / 72)      # 0.014 rev/tick -> 36 ticks per half turn
    assert gd.inserter_throughput("fast-inserter") == pytest.approx(60 / 26)
    assert gd.inserter_throughput("burner-inserter") < gd.inserter_throughput("inserter")
    assert gd.pole("small-electric-pole") == (2.5, 7.5)
    assert gd.pole("medium-electric-pole") == (3.5, 9)


# ---------------------------------------------------------------------------------------------- live
SLOT = os.environ.get("FACTORIO_TEST_SLOT")
SO = os.environ.get("FACTORIO_SCRIPT_OUTPUT")
live = pytest.mark.skipif(not (SLOT and SO), reason="needs a running test server (FACTORIO_TEST_SLOT, FACTORIO_SCRIPT_OUTPUT)")


@pytest.fixture(scope="module")
def server():
    from factorio_claude import rcon
    assert int(SLOT) > 0 or "testenv" in SO, "refusing to run against a non-test instance"
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + int(SLOT), "testpw")
    return rcon


@live
def test_live_export_and_geometry(server):
    res = server.bridge("export_data")
    gd = GameData.load(Path(SO) / res["path"])
    assert res["version_key"] == gd.version_key
    out = server.lua(Path(ROOT / "tests" / "engine_geometry.lua").read_text())
    assert out.strip().isdigit(), out
    geo = json.loads((Path(SO) / "claude" / "test_geometry.json").read_text())
    checked, fluid_checked, bad = 0, set(), []
    for r in geo:
        if "error" in r or gd.entity(r["name"])["type"] in UNSUPPORTED_GEOMETRY:
            continue
        n, d, (x, y) = r["name"], r["dir"], r["pos"]
        if gd.footprint(n, x, y, d) != tiles_of_box(*r["box"]):
            bad.append(("footprint", n, d))
        if gd.snap(n, *r["req"], d) != (x, y):
            bad.append(("snap", n, d, r["req"], (x, y)))
        checked += 1

        def q(v):
            return round(v * 8) / 8
        eng = sorted((c[1], q(c[3]), q(c[4]), q(c[5]), q(c[6])) for c in r["conns"])
        mine = sorted((c["type"], q(c["pos"][0]), q(c["pos"][1]), q(c["target"][0]), q(c["target"][1]))
                      for c in gd.fluid_connections_full(n, x, y, d))
        if not eng and mine and all(c["conditional"] for c in gd.fluid_connections_full(n, x, y, d)):
            continue  # fluidbox inactive without a fluid recipe / fluid resource
        if eng != mine:
            bad.append(("fluid", n, d, eng, mine))
        elif eng:
            fluid_checked.add((n, d))
    assert not bad, bad[:10]
    assert checked > 900
    for n in FLUID_ENTITIES:
        for d in (0, 4, 8, 12):
            assert (n, d) in fluid_checked, (n, d)
