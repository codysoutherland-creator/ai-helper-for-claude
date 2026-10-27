"""Helpers for validator tests: an all-recipes GameData from the fixture and a synthetic Grid."""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

from factorio_claude.planner.gamedata import GameData
from factorio_claude.planner.grid import BLOCKED, Grid

ROOT = Path(__file__).resolve().parent.parent
FRESH = ROOT / "testenv" / "fixtures" / "seed_1001" / "data.json"


@lru_cache(maxsize=None)
def _fresh_raw():
    return GameData.load(FRESH).raw


def gd_fresh() -> GameData:
    return GameData(copy.deepcopy(_fresh_raw()))


def gd_all() -> GameData:
    """Fixture game data with every recipe enabled (a force that has researched everything)."""
    raw = copy.deepcopy(_fresh_raw())
    for r in raw["recipes"].values():
        r["enabled"] = True
    return GameData(raw)


def make_grid(resources=(), water=(), rect=(-40, -40, 60, 60)) -> Grid:
    x0, y0, x1, y1 = rect
    g = Grid(x0, y0, x1 - x0, y1 - y0)
    for x, y in water:
        i, j = g._ij(x, y)
        g.water[i, j] = True
        g.terrain[i, j] = g.state[i, j] = BLOCKED
    for name, x, y, *_ in resources:
        i, j = g._ij(x, y)
        g.res[i, j] = g._res_id(name)
    return g
