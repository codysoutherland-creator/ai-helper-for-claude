"""Router scenarios on the fixture maps, shared by test_router.py (offline checks) and live_router.py (in-engine).

build(seed) -> dict with the grid before routing, the routes ({name: {start, goal, ents, ...}}), pipe routes and a
pole cluster. Everything is deterministic for a fixture seed.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude.planner.grid import BLOCKED, FREE  # noqa: E402
from factorio_claude.planner.router import (EAST, NORTH, SOUTH, VEC, WEST, PE, Port, place_poles, rev,  # noqa: E402
                                            route_belt, route_pipe)
from factorio_claude.planner.testing import load_fixture  # noqa: E402

ORES = ("iron-ore", "copper-ore", "coal", "stone")


def dominant_dir(dx, dy):
    if abs(dx) >= abs(dy):
        return EAST if dx > 0 else WEST
    return SOUTH if dy > 0 else NORTH


def water_on_line(world, a, b):
    n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) or 1
    return sum(world.is_water(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1))


def pick_hub(world, grid, center, taken, rmin=60, rmax=120):
    """Free 3x3 spot 60-120 tiles from the patch center; prefers a straight line crossing water (so the route
    has to go around it), then a distance near 90."""
    best, bkey = None, None
    for r in range(rmin, rmax + 1, 10):
        for k in range(48):
            a = 2 * math.pi * k / 48
            hx, hy = int(round(center[0] + r * math.cos(a))), int(round(center[1] + r * math.sin(a)))
            if not all(grid.state_at(hx + i, hy + j) == FREE and not grid.resource(hx + i, hy + j)
                       for i in (-1, 0, 1) for j in (-1, 0, 1)):
                continue
            if any(abs(hx - tx) + abs(hy - ty) < 12 for tx, ty in taken):
                continue
            key = (min(water_on_line(world, center, (hx, hy)), 25), -abs(r - 90), -k)
            if bkey is None or key > bkey:
                best, bkey = (hx, hy), key
    return best


def pick_start(grid, patch, hub):
    """Patch tile nearest the hub with the source chest + inserter fitting behind it (all free)."""
    d = dominant_dir(hub[0] - patch.center[0], hub[1] - patch.center[1])
    vx, vy = VEC[d]
    for t in sorted(patch.tiles, key=lambda t: (abs(t[0] - hub[0]) + abs(t[1] - hub[1]), t)):
        tiles = [(t[0] - vx * i, t[1] - vy * i) for i in range(3)]
        if all(grid.state_at(*p) == FREE for p in tiles):
            return t, d
    raise ValueError("no start tile")


def build(seed, poles=True, pipes=True, lanes=True):
    world, gd = load_fixture(seed, cached=False)
    grid = world.grid()
    base = grid.copy()  # before any routing (for the checks)
    out = {"seed": seed, "world": world, "gd": gd, "grid": grid, "base": base, "routes": {}, "pipes": {},
           "fixtures": [], "poles": None}
    taken = []
    if pipes:  # first: without pipe-to-ground a pipe can't cross the belt routes
        _pipe_cases(out, world, grid, gd, taken)
    for i, ore in enumerate(ORES):
        patch = min((p for p in world.patches() if p.name == ore), key=lambda p: math.hypot(*p.center))
        hub = pick_hub(world, grid, patch.center, taken)
        taken.append(hub)
        S, d = pick_start(grid, patch, hub)
        vx, vy = VEC[d]
        chest, ins = (S[0] - 2 * vx, S[1] - 2 * vy), (S[0] - vx, S[1] - vy)
        grid.occupy([chest, ins], "test:source")
        start = Port("belt_out", ore, S[0], S[1], d)
        gdir = dominant_dir(hub[0] - S[0], hub[1] - S[1])
        goal = Port("belt_in", ore, hub[0], hub[1], gdir)
        ug = i >= 2  # coal + stone may use undergrounds; iron + copper use what a fresh map allows (none)
        ents = route_belt(grid, gd, start, goal, allow_underground=True if ug else None)
        out["routes"][ore] = {"start": start, "goal": goal, "ents": ents, "chest": chest, "inserter": ins,
                              "inserter_dir": rev(d), "patch_center": patch.center, "hub": hub, "underground": ug}
    if lanes:
        _lane_cases(out, world, grid, gd, taken)
    if poles:
        _pole_case(out, world, grid, gd)
    return out


def _source_for(grid, near, d):
    """Free spot for chest + inserter + start tile in a row along d, nearest to `near`."""
    vx, vy = VEC[d]
    w, h = (3, 1) if vx else (1, 3)
    p = grid.free_rect(w, h, near=near, avoid_resources=True, allow_clear=False)
    tiles = [(p[0] + i, p[1] + j) for i in range(w) for j in range(h)]
    tiles.sort(key=lambda t: t[0] * vx + t[1] * vy)  # chest first
    return tiles


def _lane_cases(out, world, grid, gd, taken):
    """Side-load coal onto an existing (planned) east belt's left lane, and copper onto a port's right lane."""
    hub = out["routes"]["coal"]["hub"]
    # a 12-tile main belt flowing east, 25 tiles from the coal hub
    p = grid.free_rect(14, 3, near=(hub[0], hub[1] + 25), avoid_resources=True, allow_clear=False)
    y = p[1] + 1
    main = [PE("transport-belt", p[0] + 1 + i + 0.5, y + 0.5, EAST) for i in range(12)]
    for e in main:
        grid.occupy([(math.floor(e.x), math.floor(e.y))], f"test:transport-belt@{EAST}")
    src = _source_for(grid, (p[0] + 6, y - 30), SOUTH)
    grid.occupy(src[:2], "test:source")
    mid = (p[0] + 7, y)
    ents = route_belt(grid, gd, Port("belt_out", "coal", src[2][0], src[2][1], SOUTH),
                      Port("belt_in", "coal", mid[0], mid[1], EAST, lane="left"), others=main)
    out["fixtures"].append({"kind": "lane_sideload", "main": main, "ents": ents, "src": src, "item": "coal",
                            "lane": "left", "check": (p[0] + 12, y)})
    # port goal with lane: copper from a source to a plain belt_in port, all items on the right lane
    q = grid.free_rect(3, 20, near=(hub[0] - 30, hub[1]), avoid_resources=True, allow_clear=False)
    src2 = [(q[0] + 1, q[1] + i) for i in range(3)]
    grid.occupy(src2[:2], "test:source")
    goal = Port("belt_in", "copper-plate", q[0] + 1, q[1] + 17, SOUTH, lane="right")
    ents2 = route_belt(grid, gd, Port("belt_out", "copper-plate", src2[2][0], src2[2][1], SOUTH), goal)
    out["fixtures"].append({"kind": "lane_port", "ents": ents2, "src": src2, "item": "copper-plate",
                            "lane": "right", "check": (q[0] + 1, q[1] + 17)})


def _pipe_cases(out, world, grid, gd, taken):
    body = world.nearest_water(0, 0)
    spots = [s for s in world.shoreline(body) if all(grid.state_at(s[0] + i, s[1] + j) == FREE
                                                    for i in (-1, 0, 1) for j in (-1, 0, 1)
                                                    if not world.is_water(s[0] + i, s[1] + j))]
    spots.sort(key=lambda s: (s[0] ** 2 + s[1] ** 2, s))
    for k, ug in enumerate((True, False)):
        sx, sy, sdir = spots[k * 7]
        conns = gd.fluid_connections("offshore-pump", sx + 0.5, sy + 0.5, sdir)
        tx, ty, odir = conns[0]
        pump = PE("offshore-pump", sx + 0.5, sy + 0.5, sdir)
        grid.occupy(gd.footprint("offshore-pump", sx + 0.5, sy + 0.5, sdir), "test:offshore-pump")
        far = (sx - 60 * VEC[sdir][0] + 8 * k, sy - 60 * VEC[sdir][1] + 8 * k)  # inland, away from the water
        g = grid.free_rect(1, 1, near=far, avoid_resources=True, allow_clear=False)
        ents = route_pipe(grid, gd, Port("pipe_out", "water", tx, ty, odir), Port("pipe_in", "water", g[0], g[1], odir),
                          allow_underground=ug, others=[pump])
        out["pipes"][f"water_{'ptg' if ug else 'plain'}"] = {"pump": pump, "ents": ents, "goal": g, "underground": ug}


def _pole_case(out, world, grid, gd):
    """Assemblers, inserters, a lab, a radar and electric drills on iron, powered from one source pole next to an
    electric-energy-interface; place_poles must cover and connect them all."""
    iron = min((p for p in world.patches() if p.name == "iron-ore"), key=lambda p: math.hypot(*p.center))
    cx, cy = iron.center
    ents = []
    # drills on ore near the patch center (3x3, only where all tiles free + ore)
    placed = 0
    for t in sorted(iron.tiles, key=lambda t: (abs(t[0] - cx) + abs(t[1] - cy), t)):
        x, y = t[0] + 0.5, t[1] + 0.5
        fp = gd.footprint("electric-mining-drill", x, y, NORTH)
        if all(grid.state_at(*f) == FREE and grid.resource(*f) == "iron-ore" for f in fp):
            ents.append(PE("electric-mining-drill", x, y, NORTH))
            grid.occupy(fp, "test:drill")
            placed += 1
            if placed >= 4:
                break
    # an assembler block: 2 rows of 5 assemblers with 1-tile gaps, inserters below each
    p = grid.free_rect(26, 12, near=(cx + 40, cy + 10), avoid_resources=True, allow_clear=False)
    for r in range(2):
        for c in range(5):
            ax, ay = p[0] + 1 + c * 5 + 1.5, p[1] + r * 6 + 1.5
            ents.append(PE("assembling-machine-1", ax, ay, NORTH, recipe="iron-gear-wheel"))
            ents.append(PE("inserter", ax, ay + 2, NORTH))
    ents.append(PE("lab", p[0] + 24.5, p[1] + 1.5, NORTH))
    ents.append(PE("radar", p[0] + 24.5, p[1] + 7.5, NORTH))
    for e in ents[placed:]:
        grid.occupy(gd.footprint(e.name, e.x, e.y, e.direction), "test:" + e.name)
    # power source 30 tiles away: EEI (2x2) + one pole
    q = grid.free_rect(4, 2, near=(p[0] - 25, p[1] + 5), avoid_resources=True, allow_clear=False)
    eei = PE("electric-energy-interface", q[0] + 1, q[1] + 1, NORTH)
    src = PE("small-electric-pole", q[0] + 2.5, q[1] + 0.5, NORTH)
    grid.occupy(gd.footprint(eei.name, eei.x, eei.y), "test:eei")
    grid.occupy([(q[0] + 2, q[1])], "test:pole")
    poles = place_poles(grid, gd, ents, "small-electric-pole", sources=[src])
    out["poles"] = {"consumers": ents, "eei": eei, "source": src, "poles": poles}
