"""Offline test fixtures: real 2.1.23 exports of fresh Space Age maps (testenv/fixtures/seed_<n>, see its README)."""
from functools import lru_cache
from pathlib import Path

from .gamedata import GameData
from .world import World

FIXTURES = Path(__file__).resolve().parent.parent.parent / "testenv" / "fixtures"


def fixture_seeds() -> list[int]:
    """Seeds with a complete fixture folder (map/index.json + data.json)."""
    return sorted(int(p.name[5:]) for p in FIXTURES.glob("seed_*")
                  if (p / "map" / "index.json").exists() and (p / "data.json").exists())


def fixture_dir(seed: int) -> Path:
    d = FIXTURES / f"seed_{seed}"
    if not (d / "map" / "index.json").exists():
        raise FileNotFoundError(f"no fixture for seed {seed} (have {fixture_seeds()}); "
                                "build it with testenv/make_fixtures.py")
    return d


@lru_cache(maxsize=None)
def _cached(seed: int):
    return load_fixture(seed, cached=False)


def load_fixture(seed: int, cached: bool = True) -> tuple[World, GameData]:
    """(World, GameData) for a fixture seed. cached=True shares one loaded copy per seed (treat it as read-only;
    World.grid() returns a fresh Grid each call); cached=False loads a private copy."""
    if cached:
        return _cached(seed)
    d = fixture_dir(seed)
    return World.load(d / "map"), GameData.load(d / "data.json")
