"""rates.solve against hand-computed answers (data: tests/fixtures/data_fresh.json.gz, a fresh 2.1.23 Space Age export)."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factorio_claude.planner.gamedata import GameData  # noqa: E402
from factorio_claude.planner.rates import solve  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "data_fresh.json.gz"
SCIENCE = ["automation-science-pack", "logistic-science-pack", "inserter", "transport-belt", "electronic-circuit",
           "copper-cable", "iron-gear-wheel", "iron-plate", "copper-plate"]
RAW = ["iron-ore", "copper-ore", "coal", "water"]


@pytest.fixture(scope="module")
def gd():
    return GameData.load(FIXTURE)


def approx(v, rel=1e-6):
    return pytest.approx(v, rel=rel, abs=1e-6)


def science(gd, machine):
    return solve({"automation-science-pack": 60, "logistic-science-pack": 60}, gd,
                 {"allowed_recipes": SCIENCE, "raw_items": RAW, "mine": False,
                  "machines": {"crafting": machine, "smelting": "stone-furnace"}})


def test_red_green_science_am1(gd):
    r = science(gd, "assembling-machine-1")  # speed 0.5
    assert r.status == "ok", r.notes
    c = {k: v["count"] for k, v in r.recipes.items()}
    assert c["automation-science-pack"] == approx(10)    # 60/min * 5 s / 0.5 / 60
    assert c["logistic-science-pack"] == approx(12)      # 60 * 6 / 0.5 / 60
    assert c["inserter"] == approx(1)
    assert c["transport-belt"] == approx(0.5)            # 2 belts per craft
    assert c["electronic-circuit"] == approx(1)
    assert c["copper-cable"] == approx(1.5)              # circuit : cable = 2 : 3
    assert c["iron-gear-wheel"] == approx(2.5)           # 60 red + 60 inserter + 30 belt gears
    assert r.recipes["iron-gear-wheel"]["count_ceil"] == 3
    # plates: gears 150*2 + inserter 60 + belt 30 + circuit 60 = 450 iron, red 60 + cable 90 = 150 copper
    assert r.raw["iron-ore"] == approx(450)
    assert r.raw["copper-ore"] == approx(150)
    assert c["iron-plate"] == approx(450 * 3.2 / 60)    # stone furnaces, speed 1
    assert r.machines["assembling-machine-1"] == 10 + 12 + 1 + 1 + 1 + 2 + 3
    # power: 28 crafting machines' worth of (75 kW + 2.5 kW drain) by fractional count
    am_count = sum(v["count"] for v in r.recipes.values() if v["machine"] == "assembling-machine-1")
    assert r.power_kw == approx(am_count * 77.5)
    # furnace coal: 90 kW / 4 MJ per furnace
    assert r.fuel_per_min == approx((c["iron-plate"] + c["copper-plate"]) * 90e3 * 60 / 4e6)
    assert not r.surplus


def test_red_green_science_am2(gd):
    r = science(gd, "assembling-machine-2")  # speed 0.75
    c = {k: v["count"] for k, v in r.recipes.items()}
    assert c["automation-science-pack"] == approx(10 * 0.5 / 0.75)
    assert c["logistic-science-pack"] == approx(8)
    assert c["copper-cable"] == approx(1.0)
    assert c["electronic-circuit"] == approx(2 / 3)
    assert c["iron-gear-wheel"] == approx(2.5 * 0.5 / 0.75)


@pytest.mark.parametrize("furnace,count,coal", [("stone-furnace", 48, 64.8), ("steel-furnace", 24, 32.4)])
def test_yellow_belt_of_iron_plates(gd, furnace, count, coal):
    belt = gd.belt_throughput("transport-belt") * 60    # 900/min
    assert belt == approx(900)
    r = solve({"iron-plate": belt}, gd, {"allowed_recipes": ["iron-plate"], "raw_items": ["iron-ore", "coal"],
                                         "mine": False, "machines": {"smelting": furnace}})
    assert r.recipes["iron-plate"]["count"] == approx(count)   # 900 * 3.2 s / speed / 60
    assert r.fuel_per_min == approx(coal)                      # count * 90 kW / 4 MJ * 60
    assert r.raw == {"iron-ore": approx(900), "coal": approx(coal)}
    assert r.belts["iron-plate"]["transport-belt"] == approx(1)


@pytest.mark.parametrize("mw", [36, 7.2])
def test_steam_power_ratio(gd, mw):
    r = solve({"electricity": mw * 1000}, gd, {"allowed_recipes": [], "raw_items": ["water", "coal"], "mine": ["water"]})
    engines = r.recipes["generate:steam-engine"]["count"]
    boilers = r.recipes["boil:boiler"]["count"]
    pumps = r.recipes["pump:water"]["count"]
    assert engines == approx(mw * 1000 / 900)            # 900 kW each
    assert boilers == approx(engines / 2)                # 1 boiler : 2 engines
    assert pumps == approx(engines / 40)                 # 1 pump : 20 boilers : 40 engines
    assert r.fuel_per_min == approx(boilers * 1.8e6 * 60 / 4e6)
    assert r.raw["water"] == approx(boilers * 60 * 60)


def test_steam_power_includes_factory_demand(gd):
    r = solve({"iron-gear-wheel": 60}, gd, {"allowed_recipes": ["iron-gear-wheel"], "raw_items": ["iron-plate", "coal", "water"],
                                            "machines": {"crafting": "assembling-machine-1"}, "power": "steam"})
    am = r.recipes["iron-gear-wheel"]["count"]
    assert am == approx(1)
    assert r.power_kw == approx(77.5)
    assert r.recipes["generate:steam-engine"]["count"] == approx(77.5 / 900)


@pytest.mark.parametrize("drill,prod,per_drill", [("electric-mining-drill", 0.0, 30), ("electric-mining-drill", 0.1, 33),
                                                  ("burner-mining-drill", 0.0, 15), ("electric-mining-drill", 0.3, 39)])
def test_drills_per_belt(gd, drill, prod, per_drill):
    r = solve({"iron-ore": 900}, gd, {"allowed_recipes": [], "raw_items": ["iron-ore", "coal"],
                                      "machines": {"basic-solid": drill}, "mining_productivity": prod})
    m = r.recipes["mine:iron-ore"]
    assert m["machine"] == drill
    assert m["count"] == approx(900 / per_drill)         # mining_speed / mining_time * 60 * (1 + prod)
    assert r.raw["iron-ore"] == approx(900)
    if drill == "burner-mining-drill":
        # 60 burner drills * 150 kW / 4 MJ = 135 coal/min, itself mined by burner drills (coal costs coal)
        assert r.fuel_per_min > 135
        assert r.recipes["mine:coal"]["count"] > 0


def test_force_mining_bonus_is_default(gd):
    g = copy.deepcopy(gd)
    g.raw["bonuses"]["mining_drill_productivity_bonus"] = 0.2
    r = solve({"iron-ore": 360}, g, {"allowed_recipes": [], "raw_items": ["iron-ore"],
                                     "machines": {"basic-solid": "electric-mining-drill"}})
    assert r.recipes["mine:iron-ore"]["count"] == approx(10)


def unlocked(gd):
    g = copy.deepcopy(gd)
    for q in g.raw["qualities"].values():
        q["unlocked"] = True
    return g


def test_quality_roll_matches_engine_sample(gd):
    g = unlocked(gd)
    # LuaQualityPrototype.get_roll_chances(0.1) with every quality unlocked (recorded on the test server)
    engine = {"normal": 0.9, "uncommon": 0.09, "rare": 0.009, "epic": 0.0009, "legendary": 0.0001}
    got = g.quality_roll("normal", 0.1)
    assert got == {k: approx(v) for k, v in engine.items()}
    up = g.quality_roll("uncommon", 0.04)
    assert up == {"uncommon": approx(0.96), "rare": approx(0.036), "epic": approx(0.0036), "legendary": approx(0.0004)}
    assert gd.quality_roll("normal", 0.1) == {"normal": 1.0}   # nothing unlocked on a fresh map


def test_quality_modules_normal_target(gd):
    g = unlocked(gd)
    r = solve({"iron-gear-wheel": 98}, g, {"allowed_recipes": ["iron-gear-wheel"], "raw_items": ["iron-plate"],
                                           "machines": {"crafting": "assembling-machine-2"},
                                           "modules": {"iron-gear-wheel": ["quality-module", "quality-module"]}})
    e = r.recipes["iron-gear-wheel"]
    assert e["per_min"] == approx(100)                     # 2% of crafts roll up
    assert e["count"] == approx(100 * 0.5 / (0.75 * 0.9) / 60)   # quality module speed -5% each
    assert r.surplus["iron-gear-wheel@uncommon"] == approx(100 * 0.02 * 0.9)
    assert r.surplus["iron-gear-wheel@legendary"] == approx(100 * 0.02 * 0.001)
    assert r.raw["iron-plate"] == approx(200)


def test_quality_target_variant(gd):
    g = unlocked(gd)
    r = solve({"iron-gear-wheel@uncommon": 9}, g,
              {"allowed_recipes": ["iron-gear-wheel"], "raw_items": ["iron-plate"],
               "machines": {"crafting": "assembling-machine-2"},
               "modules": {"*": ["quality-module", "quality-module"]}})
    # only normal plates exist, so uncommon gears come from rolls: 9 / (0.02 * 0.9) = 500 crafts
    assert r.status == "ok", r.notes
    assert r.recipes["iron-gear-wheel"]["per_min"] == approx(500)
    assert r.surplus["iron-gear-wheel"] == approx(500 * 0.98)


def test_productivity_and_speed_modules(gd):
    r = solve({"iron-gear-wheel": 108}, gd, {"allowed_recipes": ["iron-gear-wheel"], "raw_items": ["iron-plate"],
                                             "machines": {"crafting": "assembling-machine-2"},
                                             "modules": {"crafting": ["productivity-module", "productivity-module"]}})
    e = r.recipes["iron-gear-wheel"]
    assert e["per_min"] == approx(100)                     # +8% productivity
    assert e["count"] == approx(100 * 0.5 / (0.75 * 0.9) / 60)
    assert e["power_kw"] == approx(e["count"] * (150 * 1.8 + 5))   # +40% consumption each
    # productivity modules are refused on recipes that don't allow productivity
    r2 = solve({"inserter": 60}, gd, {"allowed_recipes": ["inserter"], "raw_items": ["iron-plate", "iron-gear-wheel", "electronic-circuit"],
                                      "machines": {"crafting": "assembling-machine-2"},
                                      "modules": {"*": ["productivity-module"]}})
    assert any("not allowed" in n for n in r2.notes)
    assert r2.recipes["inserter"]["per_min"] == approx(60)


def test_beacons(gd):
    r = solve({"iron-gear-wheel": 120}, gd, {"allowed_recipes": ["iron-gear-wheel"], "raw_items": ["iron-plate"],
                                             "machines": {"crafting": "assembling-machine-2"},
                                             "beacons": {"*": {"beacon": "beacon", "count": 2, "modules": ["speed-module", "speed-module"]}}})
    # per beacon 2 * 0.2 speed * 1.5 effectivity; 2 beacons * profile[1] (0.7071)
    speed = 2 * 0.4 * 1.5 * 0.7071
    assert r.recipes["iron-gear-wheel"]["count"] == approx(120 * 0.5 / (0.75 * (1 + speed)) / 60)


def test_missing_input_reported(gd):
    r = solve({"electronic-circuit": 60}, gd, {"allowed_recipes": ["electronic-circuit"], "raw_items": ["iron-plate"],
                                              "machines": {"crafting": "assembling-machine-1"}})
    assert r.status == "missing inputs"
    assert r.missing == {"copper-cable": approx(180)}


def test_default_recipes_respect_research(gd):
    # fresh map: only enabled recipes; gears + belts are enabled, circuits are not
    r = solve({"transport-belt": 60}, gd, {"raw_items": RAW, "mine": False})
    assert r.status == "ok", r.notes
    assert set(r.recipes) == {"transport-belt", "iron-gear-wheel", "iron-plate"}
    assert r.recipes["iron-plate"]["machine"] == "stone-furnace"
    r2 = solve({"electronic-circuit": 10}, gd, {"raw_items": RAW, "mine": False})
    assert r2.status == "missing inputs"


def test_surface_conditions(gd):
    r = solve({"turbo-transport-belt": 10}, gd, {"assume_research": True, "surface": "nauvis", "raw_items": RAW})
    assert "turbo-transport-belt" not in r.recipes


def test_surface_limits_raw_items(gd):
    # with every recipe allowed, nauvis has no calcite/lava, so foundry casting routes need missing inputs and are avoided
    r = solve({"automation-science-pack": 60}, gd, {"assume_research": True, "surface": "nauvis"})
    assert r.status == "ok", r.notes
    assert set(r.raw) <= {"iron-ore", "copper-ore", "coal", "stone", "water", "crude-oil", "uranium-ore"}
    assert not any(k.startswith("casting") or "melting" in k for k in r.recipes)
