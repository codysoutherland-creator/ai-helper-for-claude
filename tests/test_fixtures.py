"""The seed fixtures load through the planner and agree with the in-game counts taken when they were built."""
import json

import numpy as np
import pytest

from factorio_claude.planner.testing import FIXTURES, fixture_seeds, load_fixture

SEEDS = fixture_seeds()
ORES = ("iron-ore", "copper-ore", "coal", "stone")


def test_have_three_seeds():
    assert len(SEEDS) >= 3, SEEDS


@pytest.mark.parametrize("seed", SEEDS)
def test_fixture_matches_engine_counts(seed):
    world, gd = load_fixture(seed)
    sc = json.loads((FIXTURES / f"seed_{seed}" / "screen.json").read_text())
    sx, sy, r = int(sc["spawn"]["x"]), int(sc["spawn"]["y"]), sc["near_radius"]
    assert world.errors == 0 and world.surface == "nauvis"
    assert world.bbox[0] <= sx - 200 and world.bbox[2] >= sx + 200
    i0, j0 = sy - r - world.y0, sx - r - world.x0
    win = (slice(i0, i0 + 2 * r), slice(j0, j0 + 2 * r))  # tiles [s-r, s+r)
    for name, v in sc["resources"].items():
        k = world.res_names.index(name)
        mask = world.res[win] == k
        assert int(mask.sum()) == v["tiles"], name
        assert int(world.amount[win][mask].sum()) == v["amount"], name
    assert int(world.water[win].sum()) == sc["water_tiles"]


@pytest.mark.parametrize("seed", SEEDS)
def test_starter_resources_near_spawn(seed):
    world, _ = load_fixture(seed)
    near = lambda p: min(max(abs(x + 0.5), abs(y + 0.5)) for x, y in p.tiles) <= 150
    for ore in ORES:
        assert any(near(p) for p in world.patches(name=ore)), ore
    (tx, ty), body = world.nearest_water_tile(0, 0)
    assert max(abs(tx), abs(ty)) <= 150 and body.size >= 30
    assert world.shoreline(body), "no offshore pump spot"


@pytest.mark.parametrize("seed", SEEDS)
def test_gamedata_fresh_force(seed):
    _, gd = load_fixture(seed)
    assert gd.version_key
    for e in ("burner-mining-drill", "stone-furnace", "burner-inserter", "transport-belt", "wooden-chest"):
        assert gd.can_build(e), e
    assert not gd.enabled("underground-belt")  # logistics not researched on a fresh map
    assert gd.footprint("stone-furnace", 1, 1, 0) == {(0, 0), (1, 0), (0, 1), (1, 1)}


def test_cached_vs_private_copy():
    seed = SEEDS[0]
    a, _ = load_fixture(seed)
    assert load_fixture(seed)[0] is a
    b, _ = load_fixture(seed, cached=False)
    assert b is not a and np.array_equal(a.water, b.water)


def test_unknown_seed():
    with pytest.raises(FileNotFoundError):
        load_fixture(424242, cached=False)
