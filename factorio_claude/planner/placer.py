"""Starter-base planner (SPEC-v3 M1): hub site selection, module placement, routing, poles, validation.

    plans = plan_variants(world, gd, goal)                      # 2-3 validated Plans with different trade-offs
    plan = plan_starter_base(world, gd, goal, "compact")        # one variant

Layout (no splitters or undergrounds needed, so it works on a fresh map):
  - ore mines: a column of drills on ONE side of a belt (burner drills in the "bootstrap" stage, electric drills in
    the "electric" stage), so the ore travels on one lane only (drills drop onto the near lane);
  - coal: a small coal mine per smelter array whose belt side-loads coal onto the free lane of that array's ore
    belt somewhere along the way (each array then has one mixed-lane input: ore on one lane, coal on the other);
  - hub: one smelter_array (mixed_lane) per product side by side; outputs gathered into adjacent bus lanes heading
    out of the hub (+ reserved spare lanes);
  - electric stage: steam power (blocks.steam_power) at the shore nearest the hub, offshore pump + pipe, its own coal
    mine, and poles covering every electric entity in one network.
The hub has one input per array, so its inputs are ordered by the direction their ore comes from (no crossings).
Every plan is checked with validate(); a configuration that fails (no route, no room, validation errors) is dropped
and the next candidate (hub site / array order / route order) is tried. PlanError if nothing passes.

goal: None | str (free text: "electric", "burner"/"bootstrap", "bigger", "small") | dict with optional keys
  stage ("bootstrap"|"electric"), near (x, y) (default (0, 0): spawn / the player), furnaces {iron, copper, stone},
  scale (multiplies the variant's furnace counts), bus_len, spare_lanes.
"""
from __future__ import annotations

import itertools
import math
import re
import time
import uuid
from dataclasses import dataclass, field, replace

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from . import blocks as B
from .blocks import PE, Block, Port
from .gamedata import DIR_VEC, rotate, tiles_of_box
from .grid import BLOCKED, CLEARABLE, Grid
from .plan import Plan, build_minutes, entity_counts, fmt_n, nice, raw_materials
from .router import NoRoute, _is_electric, left, place_poles, right, route_belt, route_pipe
from .validate import validate

NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12
ORES = {"iron": "iron-ore", "copper": "copper-ore", "stone": "stone"}
COAL = "coal"
CORRIDOR = 3        # tiles kept free straight out from each hub input until its belt is routed
MAX_NODES = 60_000  # A* expansion cap per route (a failing search is otherwise very slow)
COAL_NODES = 40_000
NEST_RANGE = 120     # biter nests this close to the build get a warning
MIN_AMOUNT = 8000    # ore under each drill (a burner drill mines 15/min: 8000 lasts ~9 h; electric ~4.5 h)  # ... per coal side-load attempt (several spots are tried)
# lane the ore travels on after a drill_column layout (relative to the flow at its port)
DROP_LANE = {"east": "right", "west": "left", "pairs_e": "right", "pairs_w": "left"}
# ore mine layouts by preference: one-sided with both sides kept open (coal may join from either side), one-sided,
# then both-sided with a lane-merging end (shorter, fits patches a long one-sided column doesn't)
ORE_LAYOUTS = (("east", "west"), ("east",), ("west",), ("pairs_e",), ("pairs_w",))
SIDE_OF = {EAST: "west", WEST: "east", NORTH: "south", SOUTH: "north"}  # pump output dir -> block's water side


class PlanError(RuntimeError):
    """No configuration produced a plan that passes validation. .reasons = what went wrong per attempt."""

    def __init__(self, msg, reasons=None):
        super().__init__(msg)
        self.reasons = reasons or []


@dataclass(frozen=True)
class Variant:
    key: str
    title: str
    furnaces: tuple            # (iron, copper, stone)
    bus_len: int
    spare_lanes: int
    margin: int                # free ring kept around the hub
    water_weight: float        # electric stage: hub-to-water distance weight in site selection
    headroom: float            # electric stage: generation / planned demand
    pitch: str                 # one-line trade-off for the summary


VARIANTS = {
    "compact": Variant("compact", "Compact hub", (4, 4, 2), 6, 0, 1, 0.5, 1.5,
                       "smallest and quickest to build, modest output"),
    "balanced": Variant("balanced", "Balanced hub", (8, 6, 2), 10, 0, 2, 1.5, 1.5,
                        "twice the iron, hub placed for the shortest belts overall and close to water for power"),
    "expandable": Variant("expandable", "Expandable bus", (12, 8, 4), 24, 2, 3, 0.5, 2.5,
                          "most output and a long bus with spare lanes and room to grow, but the most to build"),
}
DEFAULT_VARIANTS = ("compact", "balanced", "expandable")


# ---- goal ---------------------------------------------------------------------------------------------------
def parse_goal(goal) -> dict:
    if goal is None:
        return {}
    if isinstance(goal, dict):
        return dict(goal)
    text = str(goal).lower()
    out = {"text": str(goal)}
    if re.search(r"\belectric|\bsteam|\bpower", text):
        out["stage"] = "electric"
    elif re.search(r"\bburner|\bbootstrap|\bfrom (scratch|nothing)", text):
        out["stage"] = "bootstrap"
    if re.search(r"\b(big|bigger|large|more)\b", text):
        out["scale"] = 1.5
    elif re.search(r"\b(small|smaller|tiny|minimal)\b", text):
        out["scale"] = 0.5
    return out


def default_stage(gd) -> str:
    need = ("electric-mining-drill", "inserter", "offshore-pump", "boiler", "steam-engine", "small-electric-pole")
    return "electric" if all(B.buildable(gd, n) for n in need) else "bootstrap"


# ---- small geometry helpers -----------------------------------------------------------------------------------
def _rot_vec(dx, dy, k):
    """Vector after k clockwise turns (negative k: counter-clockwise)."""
    for _ in range(k % 4):
        dx, dy = -dy, dx
    return dx, dy


def _rot_tile(t, size, k):
    """Tile (x, y) inside a block of `size` after k clockwise turns (as Block.rotated does for ports)."""
    x, y = t
    w, h = size
    for _ in range(k % 4):
        x, y = h - 1 - y, x
        w, h = h, w
    return x, y


def _tiles(gd, e) -> set:
    return gd.footprint(e.name, e.x, e.y, e.direction or 0)


def _tag(prefix, e):
    io = ""
    if isinstance(e.extra, dict) and e.extra.get("type") in ("input", "output"):
        io = "in" if e.extra["type"] == "input" else "out"
    return f"{prefix}:{e.name}@{int(e.direction or 0)}{io}"


def _occupy(grid, gd, ents, prefix):
    """Mark entities in the grid; the tag keeps the direction (the router reads belt directions from it)."""
    for e in ents:
        grid.occupy(_tiles(gd, e), _tag(prefix, e))


def _tile(o):
    return (math.floor(o.x), math.floor(o.y))


def _taxi(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _rect_of(tiles):
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def _amounts(world, grid: Grid) -> np.ndarray:
    """Ore amount per tile over the grid (0 outside the export)."""
    out = np.zeros((grid.h, grid.w), np.int64)
    i0, i1 = max(grid.y0, world.y0), min(grid.y0 + grid.h, world.y0 + world.h)
    j0, j1 = max(grid.x0, world.x0), min(grid.x0 + grid.w, world.x0 + world.w)
    if i0 < i1 and j0 < j1:
        out[i0 - grid.y0:i1 - grid.y0, j0 - grid.x0:j1 - grid.x0] =             world.amount[i0 - world.y0:i1 - world.y0, j0 - world.x0:j1 - world.x0]
    return out


class _Sum:
    """Integral image over a grid-aligned boolean mask: counts in tile rects, vectorised over many origins."""

    def __init__(self, grid: Grid, mask):
        self.x0, self.y0, self.h, self.w = grid.x0, grid.y0, mask.shape[0], mask.shape[1]
        s = np.zeros((self.h + 1, self.w + 1), np.int64)
        s[1:, 1:] = np.asarray(mask, np.int64).cumsum(0).cumsum(1)
        self.s = s

    def count(self, X, Y, rect):
        """Cells of the mask in [X+x0, X+x1) x [Y+y0, Y+y1) for arrays X, Y of origins (clipped to the grid)."""
        x0, y0, x1, y1 = rect
        j0 = np.clip(X + x0 - self.x0, 0, self.w)
        j1 = np.clip(X + x1 - self.x0, 0, self.w)
        i0 = np.clip(Y + y0 - self.y0, 0, self.h)
        i1 = np.clip(Y + y1 - self.y0, 0, self.h)
        return self.s[i1, j1] - self.s[i0, j1] - self.s[i1, j0] + self.s[i0, j0]


def walk_distance(grid: Grid, sources, max_steps=600) -> np.ndarray:
    """4-connected walking distance (tiles) over non-BLOCKED tiles from the source tiles (BFS by array shifts).
    Unreached tiles get inf. Array is indexed [y - grid.y0, x - grid.x0]."""
    free = grid.state != BLOCKED
    dist = np.full(free.shape, np.inf)
    front = np.zeros(free.shape, bool)
    for x, y in sources:
        if grid.in_bounds(x, y) and free[y - grid.y0, x - grid.x0]:
            front[y - grid.y0, x - grid.x0] = True
    seen = front.copy()
    d = 0
    while front.any() and d <= max_steps:
        dist[front] = d
        nxt = np.zeros_like(front)
        nxt[1:, :] |= front[:-1, :]
        nxt[:-1, :] |= front[1:, :]
        nxt[:, 1:] |= front[:, :-1]
        nxt[:, :-1] |= front[:, 1:]
        front = nxt & free & ~seen
        seen |= front
        d += 1
    return dist


# ---- mines ----------------------------------------------------------------------------------------------------
def drill_column(gd, n: int, drill: str, belt: str = "transport-belt", resource: str | None = None,
                 layout: str = "pairs") -> Block:
    """n drills along a belt flowing north (canonical, belt in column x = drill size), belt_out port at the north
    end. layout: "pairs" = both sides; "east" = all drills east of the belt facing west; "west" = all west of it
    facing east; "pairs_e"/"pairs_w" = both sides, the column then side-loads onto a short belt running east/west
    (port at its end), which puts all items on one lane of it. Drills drop onto the belt lane NEAREST to them (measured
    in 2.1.23 for burner and electric drills, unlike inserters, which use the far lane). Item lane: DROP_LANE.
    Burner drills are marked fuel=manual."""
    s = gd.size(drill)[0]
    extra = {"fuel": "manual"} if B._burner(gd, drill) else None
    pairs = layout in ("pairs", "pairs_e", "pairs_w")
    ents = []
    for i in range(n):
        if pairs:
            side, r = i % 2, i // 2
        else:
            side, r = (1 if layout == "east" else 0), i
        ents.append(B._put(gd, drill, 0 if side == 0 else s + 1, r * s, EAST if side == 0 else WEST, extra=extra))
    drops = []
    for e in ents:
        vx, vy = rotate(*gd.entity(drill)["vector_to_place_result"], e.direction)
        if math.floor(e.x + vx) != s:
            raise ValueError(f"{drill} output does not land on the belt column")
        drops.append(math.floor(e.y + vy))
    ents += [B._put(gd, belt, s, y, NORTH) for y in range(max(drops) + 1)]
    rows = math.ceil(n / 2) if pairs else n
    lane = DROP_LANE.get(layout)
    port = Port("belt_out", None, s, -1, NORTH, lane)
    if layout in ("pairs_e", "pairs_w"):
        # the column side-loads onto a belt running east (west) with a blocker belt behind it: all ore on one lane
        d, step = (EAST, 1) if layout == "pairs_e" else (WEST, -1)
        ents += [B._put(gd, belt, s, -1, d), B._put(gd, belt, s - step, -1, d, extra={"role": "lane_blocker"})]
        port = Port("belt_out", None, s + step, -1, d, lane)
    b = Block("drill_column", ents, (2 * s + 1, rows * s), [port])
    if resource:
        item, rate = B.mining_rate(gd, drill, resource)
        b.ports[0].item = item
        b.rates[item] = round(rate * n * 60, 3)
    b.info = {"drills": n, "drill": drill, "resource": resource, "layout": layout}
    return b


def _drill_rects(gd, b: Block):
    """[(footprint rect, mining rect or None)] in local coords for every entity of a (rotated) block."""
    out = []
    for e in b.entities:
        box = gd.box(e.name, e.x, e.y, e.direction)
        fr = (math.floor(box[0] + 1e-6), math.floor(box[1] + 1e-6), math.ceil(box[2] - 1e-6), math.ceil(box[3] - 1e-6))
        mr = None
        r = gd.entity(e.name).get("mining_drill_radius")
        if r:
            mr = _rect_of(tiles_of_box(e.x - r, e.y - r, e.x + r, e.y + r))
        out.append((fr, mr))
    return out


def place_mine(grid: Grid, gd, patch, n: int, drill: str, belt: str, target, *, layouts=("pairs",),
               min_frac=0.5, amount=None, min_amount=0) -> dict | None:
    """Best placement of an n-drill column on `patch` (world.Patch) whose output port is closest to `target`
    (x, y), valid for every layout in `layouts` at once (same belt, drills on either side). Every drill sits on
    placeable ground with >= min_frac of its mining area on this ore and no other resource in it; belt and port tile
    placeable too. amount: ore amount per grid tile (array like grid.res); every drill's mining area must then hold
    >= min_amount ore (so edge drills don't run dry within the hour). -> {layout: placed Block} or None."""
    res = patch.name
    canon = {lay: drill_column(gd, n, drill, belt, res, lay) for lay in layouts}
    ok = _Sum(grid, grid.placeable_mask(allow_clear=True))
    rid = grid.res_names.index(res) if res in grid.res_names else -1
    mine = _Sum(grid, grid.res == rid)
    other = _Sum(grid, (grid.res != 0) & (grid.res != rid))
    rich = _Sum(grid, np.where(grid.res == rid, amount, 0)) if amount is not None and min_amount else None
    bx0, by0, bx1, by1 = patch.bbox
    best = None
    for k in range(4):
        rot = {lay: b.rotated(k) for lay, b in canon.items()}
        b0 = rot[layouts[0]]
        w, h = b0.size
        port = b0.ports[0]
        X, Y = np.meshgrid(np.arange(bx0 - w, bx1 + 1), np.arange(by0 - h, by1 + 1))
        X, Y = X.ravel(), Y.ravel()
        good = ok.count(X, Y, (port.x, port.y, port.x + 1, port.y + 1)) == 1
        rects = {r for b in rot.values() for r in _drill_rects(gd, b)}
        for fr, mr in rects:
            good &= ok.count(X, Y, fr) == (fr[2] - fr[0]) * (fr[3] - fr[1])
            if mr is not None:
                area = (mr[2] - mr[0]) * (mr[3] - mr[1])
                good &= mine.count(X, Y, mr) >= max(1, math.ceil(area * min_frac))
                good &= other.count(X, Y, mr) == 0
                if rich is not None:
                    good &= rich.count(X, Y, mr) >= min_amount
            if not good.any():
                break
        if not good.any():
            continue
        X, Y = X[good], Y[good]
        px, py = X + port.x, Y + port.y
        score = (np.abs(px - target[0]) + np.abs(py - target[1])).astype(float)
        vx, vy = DIR_VEC[port.direction]
        score += ((vx * (target[0] - px) + vy * (target[1] - py)) < 0) * 8.0  # port facing away from the target
        i = int(np.argmin(score))
        if best is None or score[i] < best[0]:
            best = (float(score[i]), {lay: b.at(int(X[i]), int(Y[i])) for lay, b in rot.items()})
    return best[1] if best else None


# ---- hub ------------------------------------------------------------------------------------------------------
@dataclass
class Hub:
    block: Block          # ports: one belt_in per array (in `ores` order), then the bus ends (belt_out)
    ores: list            # ore of each input port
    reserved: list        # local tiles kept free (spare bus lanes)
    arrays: list          # [(ore, furnaces, smelter Block)] in left-to-right order

    def rotated(self, k) -> "Hub":
        size = self.block.size
        return Hub(self.block.rotated(k), self.ores, [_rot_tile(t, size, k) for t in self.reserved], self.arrays)

    def inputs(self) -> list:
        return [p for p in self.block.ports if p.kind == "belt_in"]


def build_hub(gd, arrays, order, *, bus_len=8, spare=0, inserter="burner-inserter", pole=None,
              belt="transport-belt", furnace="stone-furnace") -> Hub:
    """arrays: [(ore, n furnaces)]; order: array indices left to right. Canonical frame: mixed-lane inputs at the
    bottom (south), outputs gathered into adjacent bus lanes leaving at the top (north)."""
    ents, ins, outs, used, ores = [], [], [], [], []
    for pos, ai in enumerate(order):
        ore, n = arrays[ai]
        a = B.smelter_array(n, furnace, "mixed_lane", "north", inserter, gd, belt=belt, pole=pole, ore=ore)
        X = 7 * pos
        ents += [e.moved(X, 0) for e in a.entities]
        p_in = next(p for p in a.ports if p.kind == "belt_in" and p.item == ore)
        ins.append(Port("belt_in", ore, p_in.x + X, p_in.y, p_in.direction))  # no lane: coal joins upstream
        ores.append(ore)
        p_out = next(p for p in a.ports if p.kind == "belt_out")
        outs.append((p_out.x + X, p_out.item))
        used.append((ore, n, a))
    o0 = outs[0][0]
    m = len(outs)
    y_end = -(m + bus_len)
    bus_ports = []
    for j, (ox, item) in enumerate(outs):
        lane_x = o0 + j
        if j == 0:
            ents += [PE(belt, ox + 0.5, y + 0.5, NORTH) for y in range(y_end, 0)]
        else:
            ents += [PE(belt, ox + 0.5, y + 0.5, NORTH) for y in range(-j, 0)]
            ents += [PE(belt, x + 0.5, -1 - j + 0.5, WEST) for x in range(lane_x + 1, ox + 1)]
            ents += [PE(belt, lane_x + 0.5, y + 0.5, NORTH) for y in range(y_end, -j)]
        bus_ports.append(Port("belt_out", item, lane_x, y_end - 1, NORTH, "right"))
    reserved = [(o0 + m + i, y) for i in range(spare) for y in range(y_end - 1, 0)]
    ports = ins + bus_ports
    tiles = set(reserved) | {(p.x, p.y) for p in ports}
    for e in ents:
        tiles |= _tiles(gd, e)
    x0, y0, x1, y1 = _rect_of(tiles)
    blk = Block("hub", [e.moved(-x0, -y0) for e in ents], (x1 - x0, y1 - y0), [p.moved(-x0, -y0) for p in ports])
    return Hub(blk, ores, [(x - x0, y - y0) for x, y in reserved], used)


def _ccw_from_bus(dx, dy) -> float:
    """Angle of a direction in the hub's canonical frame, counter-clockwise from north (the bus side): 0 north,
    pi/2 west, pi south, 3pi/2 east. Inputs left -> right along the south side want increasing angles."""
    th = math.atan2(dx, -dy)  # clockwise from north
    return -th if th <= 0 else 2 * math.pi - th


def array_orders(ores, angle, limit=2):
    """Array orders (left to right) with the fewest crossings between input order and source angles."""
    scored = []
    for order in itertools.permutations(range(len(ores))):
        seq = [angle[ores[i]] for i in order]
        inv = sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq)) if seq[i] > seq[j])
        scored.append((inv, order))
    scored.sort()
    return [o for _i, o in scored[:limit]]


def sideload_spots(route, gd, grid, side="right"):
    """Tiles of a belt route where another belt can side-load onto its `side` lane: plain straight belts fed from
    behind by the previous route belt, with the neighbour on that side placeable. -> [(tile, belt dir)]"""
    out = []
    turn = right if side == "right" else left
    for i in range(1, len(route) - 1):
        a, b = route[i - 1], route[i]
        if gd.entity(b.name).get("type") != "transport-belt" or gd.entity(a.name).get("type") != "transport-belt":
            continue
        if a.direction != b.direction:
            continue
        ta, tb = _tile(a), _tile(b)
        vx, vy = DIR_VEC[b.direction]
        if (ta[0] + vx, ta[1] + vy) != tb:
            continue
        rx, ry = DIR_VEC[turn(b.direction)]
        if grid.state_at(tb[0] + rx, tb[1] + ry) == BLOCKED:
            continue
        out.append((tb, b.direction))
    return out


# ---- the planner ------------------------------------------------------------------------------------------------
@dataclass
class Flow:
    key: tuple                  # ("ore", ore) | ("fuel", ore) | ("fuel", "boiler")
    resource: str
    drills: int
    mine: Block | None = None
    goal: Port | None = None
    alts: dict = field(default_factory=dict)   # ore mines: {layout: placed Block} while the drill side is open
    want: int = 0                              # drills the flow needs (drills = what fits)
    min_drills: int = 0                        # fewest drills _mine may settle for (0: half of `drills`)

    def __post_init__(self):
        self.want = self.want or self.drills
    route: list = field(default_factory=list)


class _Attempt(Exception):
    pass


class Planner:
    def __init__(self, world, gd, goal=None, variant="compact", *, max_sites=8, time_budget=45.0, keep=4,
                 want_sites=3):
        self.world, self.gd = world, gd
        self.goal = parse_goal(goal)
        self.v = VARIANTS[variant] if isinstance(variant, str) else variant
        self.stage = self.goal.get("stage") or default_stage(gd)
        self.near = tuple(self.goal.get("near") or (0, 0))
        self.max_sites, self.deadline = max_sites, time.monotonic() + time_budget
        self.keep, self.want_sites = keep, want_sites  # routing results kept per site / sites that must succeed
        electric = self.stage == "electric"
        self.belt = "transport-belt"
        self.furnace = "stone-furnace"
        self.drill = "electric-mining-drill" if electric else "burner-mining-drill"
        self.inserter = "inserter" if electric else "burner-inserter"
        self.pole = B.choose(gd, "medium-electric-pole", "small-electric-pole") if electric else None
        scale = float(self.goal.get("scale") or 1.0)
        fur = dict(zip(("iron", "copper", "stone"), self.v.furnaces))
        fur.update({k: int(v) for k, v in (self.goal.get("furnaces") or {}).items() if k in fur})
        self.furnaces = {k: max(1, round(n * scale)) for k, n in fur.items() if n}
        self.bus_len = int(self.goal.get("bus_len") or self.v.bus_len)
        self.spare = int(self.goal.get("spare_lanes", self.v.spare_lanes))
        self.reasons: list[str] = []

    # -- sizing ----------------------------------------------------------------------------------------------
    def _drills_for(self, resource, per_s):
        _item, rate = B.mining_rate(self.gd, self.drill, resource)
        return max(1, math.ceil(per_s / rate - 1e-9))

    def flows(self) -> list[Flow]:
        gd, out = self.gd, []
        speed = gd.entity(self.furnace).get("crafting_speed") or 1
        for key, n in self.furnaces.items():
            ore = ORES[key]
            rec = B.smelt_recipe(gd, ore, self.furnace)
            ore_s = n * speed / rec["energy"] * rec["in"]
            out.append(Flow(("ore", ore), ore, self._drills_for(ore, ore_s)))
            coal = B.fuel_per_min(gd, self.furnace, COAL, n) / 60
            if B._burner(gd, self.inserter):  # burner input inserters burn while they swing (~144 kW)
                ins = gd.entity(self.inserter)
                swings = ore_s + coal  # one item per swing
                busy = min(n, swings / gd.inserter_throughput(self.inserter, stack=1))
                coal += busy * (ins.get("max_energy_usage_kw") or 0) / (gd.fuel_value(COAL) / 1000)
            # coal mines are never cut below the real demand (a short coal supply starves furnaces + inserters)
            out.append(Flow(("fuel", ore), COAL, self._drills_for(COAL, coal * 1.1),
                            min_drills=self._drills_for(COAL, coal)))
        return out

    def steam_size(self, flows):
        """(boilers, engines, boiler coal Flow) for the planned machines times the variant's headroom."""
        gd = self.gd
        e_kw = gd.entity("steam-engine").get("max_energy_production_kw") or 900
        d_kw = gd.entity(self.drill).get("energy_usage_kw") or 90
        need = sum(f.drills for f in flows) * d_kw
        need += 2 * sum(self.furnaces.values()) * (gd.entity(self.inserter).get("max_energy_usage_kw") or 15)
        f = Flow(("fuel", "boiler"), COAL, 1)
        for _ in range(3):  # the boiler coal mine's electric drills add demand
            engines = max(2, math.ceil((need + f.drills * d_kw) * self.v.headroom / e_kw))
            boilers = math.ceil(engines / 2)
            f.drills = self._drills_for(COAL, B.fuel_per_min(gd, "boiler", COAL, boilers) / 60)
        f.want = f.min_drills = f.drills
        return boilers, engines, f

    # -- world picks -------------------------------------------------------------------------------------------
    def pick_patches(self, flows):
        out = {}
        s = self.gd.size(self.drill)[0]
        for res in sorted({f.resource for f in flows}):
            drills = sum(f.drills for f in flows if f.resource == res)
            cands = self.world.patches(min_tiles=20, name=res)
            if not cands:
                raise PlanError(f"no {nice(res)} patch in the exported area")

            def key(p):
                pts = np.array(list(p.tiles))
                d = float(np.min(np.abs(pts[:, 0] + 0.5 - self.near[0]) + np.abs(pts[:, 1] + 0.5 - self.near[1])))
                return d + (60 if p.size < drills * s * s * 1.5 else 0) + (20 if p.edge else 0)
            out[res] = min(cands, key=key)
        return out

    def area(self, patches, water_tile):
        pts = [self.near]
        for p in patches.values():
            pts += [(p.bbox[0], p.bbox[1]), (p.bbox[2], p.bbox[3])]
        if water_tile:
            pts.append(water_tile)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        m = 40
        wx0, wy0, wx1, wy1 = self.world.bbox
        return (max(wx0, int(min(xs)) - m), max(wy0, int(min(ys)) - m),
                min(wx1, int(max(xs)) + m), min(wy1, int(max(ys)) + m))

    # -- hub sites ---------------------------------------------------------------------------------------------
    def hub_sites(self, grid, hub: Hub, patches, flows, water_tile):
        """Candidate (cost, k, X, Y) for the hub's (rotated) block plus margin, best first and spread apart.
        Cost = flow-weighted taxicab distance from the input side to each patch (+ water distance, + trees)."""
        good = grid.placeable_mask(allow_clear=True, avoid_resources=True)
        S = _Sum(grid, good)
        C = _Sum(grid, grid.state == CLEARABLE)

        def dist_to(tiles):
            mask = np.zeros_like(good)
            for x, y in tiles:
                if grid.in_bounds(x, y):
                    mask[y - grid.y0, x - grid.x0] = True
            if not mask.any():
                return None
            return ndimage.distance_transform_cdt(~mask, metric="taxicab").astype(float)
        dist = {res: dist_to(p.tiles) for res, p in patches.items()}
        weight = {}
        for f in flows:
            weight[f.resource] = weight.get(f.resource, 0) + (2 if f.key[0] == "ore" else 1)
        dw = dist_to([water_tile]) if water_tile is not None else None
        m = self.v.margin
        cands = []
        for k in range(4):
            hb = hub.rotated(k).block
            w, h = hb.size
            W, H = w + 2 * m, h + 2 * m
            X, Y = np.meshgrid(np.arange(grid.x0, grid.x0 + grid.w - W + 1, 2),
                               np.arange(grid.y0, grid.y0 + grid.h - H + 1, 2))
            X, Y = X.ravel(), Y.ravel()
            ok = S.count(X, Y, (0, 0, W, H)) == W * H
            if not ok.any():
                continue
            X, Y = X[ok], Y[ok]
            ins = hub.rotated(k).inputs()
            ix = m + int(sum(p.x for p in ins) / len(ins))
            iy = m + int(sum(p.y for p in ins) / len(ins))
            jj = np.clip(X + ix - grid.x0, 0, grid.w - 1)
            ii = np.clip(Y + iy - grid.y0, 0, grid.h - 1)
            cost = np.zeros(len(X))
            nx, ny = _rot_vec(0, 1, k)  # outward normal of the input side
            for res, wt in weight.items():
                if dist[res] is not None:
                    cost += wt * dist[res][ii, jj]
                    # sources behind the input side make routes wrap around the hub (and fence in other inputs)
                    c = patches[res].center
                    behind = (c[0] - (X + ix)) * nx + (c[1] - (Y + iy)) * ny
                    cost += wt * 2.0 * np.maximum(0.0, -behind)
            if dw is not None:
                cj = np.clip(X + W // 2 - grid.x0, 0, grid.w - 1)
                ci = np.clip(Y + H // 2 - grid.y0, 0, grid.h - 1)
                cost += self.v.water_weight * dw[ci, cj]
            cost += 0.05 * C.count(X, Y, (0, 0, W, H))
            cands += [(c, k, x + m, y + m) for c, x, y in zip(cost.tolist(), X.tolist(), Y.tolist())]
        cands.sort()
        picked = []
        for c in cands:
            if all(c[1] != p[1] or abs(c[2] - p[2]) + abs(c[3] - p[3]) > 14 for p in picked):
                picked.append(c)
            if len(picked) >= self.max_sites:
                break
        return picked

    # -- power -------------------------------------------------------------------------------------------------
    def place_power(self, grid, center, boilers, engines):
        """(steam Block placed, offshore pump PE, pipe PEs) on the shore nearest `center`, or None. Occupies the
        tiles in `grid` on success."""
        gd, w = self.gd, self.world
        spots = []
        for body in w.water_bodies(30):
            for x, y, d in w.shoreline(body):
                if grid.in_bounds(x, y):
                    spots.append((_taxi((x, y), center), x, y, d))
        spots.sort()
        ok = _Sum(grid, grid.placeable_mask(allow_clear=True, avoid_resources=True))
        blocks, tried = {}, 0
        for _dist, x, y, d in spots:
            if tried > 40:
                break
            pump = PE("offshore-pump", x + 0.5, y + 0.5, d)
            foot = _tiles(gd, pump)
            if not grid.can_place([t for t in foot if not grid.is_water(*t)]):
                continue
            conns = gd.fluid_connections("offshore-pump", pump.x, pump.y, d)
            if not conns or grid.state_at(conns[0][0], conns[0][1]) == BLOCKED:
                continue
            tx, ty, dout = conns[0]
            tried += 1
            side = SIDE_OF[dout]
            if side not in blocks:
                blocks[side] = B.steam_power(boilers, engines, gd, water_side=side, pole=self.pole)
            sb = blocks[side]
            port = sb.port("pipe_in")
            vx, vy = DIR_VEC[dout]
            px, py = -vy, vx
            fits = []
            for gap in range(0, 5):
                for lat in (0, 1, -1, 2, -2, 3, -3):
                    ox = tx + vx * gap + px * lat - port.x
                    oy = ty + vy * gap + py * lat - port.y
                    if ok.count(np.array([ox]), np.array([oy]), (0, 0, *sb.size))[0] == sb.size[0] * sb.size[1]:
                        fits.append((gap + abs(lat), ox, oy))
            for _c, ox, oy in sorted(fits)[:3]:
                placed = sb.at(ox, oy)
                ents = placed.placed()
                tiles = set()
                for e in ents:
                    tiles |= _tiles(gd, e)
                if tiles & foot or not grid.can_place(tiles):
                    continue
                g = grid.copy()
                _occupy(g, gd, ents, "power")
                g.occupy(foot, _tag("power", pump))
                p_in = next(q for q in placed.placed_ports() if q.kind == "pipe_in")
                g.release([(tx, ty), (p_in.x, p_in.y)])
                try:
                    pipe = route_pipe(g, gd, Port("pipe_out", "water", tx, ty, dout), p_in, others=ents + [pump])
                except NoRoute:
                    continue
                if len(pipe) > 12:
                    continue
                _occupy(g, gd, pipe, "power")
                grid.__dict__.update(g.__dict__)
                return placed, pump, pipe
        return None

    # -- one attempt ---------------------------------------------------------------------------------------------
    def _mine(self, grid, f, target, layouts_list=(("pairs",),)):
        """Place f's mine (fewer drills / sparser ore if needed); occupies the tiles of every candidate layout."""
        gd = self.gd
        n = f.drills
        while n >= max(1, f.min_drills or math.ceil(f.drills / 2)):
            for layouts in layouts_list:
                for frac, amt in ((0.5, MIN_AMOUNT), (0.25, MIN_AMOUNT // 4)):
                    alts = place_mine(grid, gd, self.patches[f.resource], n, self.drill, self.belt, target,
                                      layouts=layouts, min_frac=frac, amount=self.amount, min_amount=amt)
                    if alts is not None:
                        f.alts, f.mine, f.drills = alts, alts[layouts[0]], n
                        for mb in alts.values():
                            _occupy(grid, gd, mb.placed(), "mine")
                        mp = f.mine.placed_ports()[0]
                        grid.occupy([(mp.x, mp.y)], "port")
                        return f.mine
            n -= 1
        raise _Attempt(f"no room for a {f.drills}-drill {nice(f.resource)} mine")

    def _settle(self, grid, f, layout):
        """Keep the ore mine's drills on one side of its belt; free the other side's tiles."""
        keep = f.alts[layout]
        kt = set()
        for e in keep.placed():
            kt |= _tiles(self.gd, e)
        for lay, mb in f.alts.items():
            if lay != layout:
                for e in mb.placed():
                    grid.release(_tiles(self.gd, e) - kt)
        f.mine, f.alts = keep, {layout: keep}

    def _route(self, grid, start, goal, others, free=(), max_nodes=None, sideload=False):
        """Belt from port `start` to port `goal` (sideload: goal is a tile of an existing belt, which stays)."""
        grid.release([(start.x, start.y)] + ([] if sideload else [(goal.x, goal.y)]) + list(free))
        # cheap reachability test first: a failing A* search explores everything it can reach
        labels, _n = ndimage.label(grid.state != BLOCKED)

        def lab(t):
            return int(labels[t[1] - grid.y0, t[0] - grid.x0]) if grid.in_bounds(*t) else 0
        a = lab((start.x, start.y))
        ends = [(goal.x + vx, goal.y + vy) for vx, vy in DIR_VEC.values()] if sideload else [(goal.x, goal.y)]
        if not a or a not in {lab(t) for t in ends}:
            raise NoRoute(f"{nice(start.item or 'belt')} start ({start.x}, {start.y}) is walled off from its goal")
        ents = route_belt(grid, self.gd, start, goal, self.belt, others=others, max_nodes=max_nodes or MAX_NODES)
        _occupy(grid, self.gd, ents, "route")
        return ents

    def _coal_to(self, grid, f, ore, coal_tree, others, tries=8):
        """Place coal flow f's mine and route its belt onto a side-load spot of the ore flow's belt (spots nearest
        the coal patch first). The coal lane is whichever side the ore mine leaves free; while the ore mine still
        has drill spots on both sides of its belt, coal may come from either side. -> (grid, new entities)."""
        gd = self.gd
        cands = []
        for lay in ore.alts:
            side = "left" if DROP_LANE[lay] == "right" else "right"  # coal goes on the lane the ore leaves free
            cands += [(t, d, side, lay) for t, d in sideload_spots(ore.route, gd, grid, side)]
        # walking distance from the coal patch: unreachable spots are skipped, nearest (by real path) first
        geo = walk_distance(grid, self.patches[COAL].tiles)

        def side_tile(c):
            vx, vy = DIR_VEC[(right if c[2] == "right" else left)(c[1])]
            return c[0][0] + vx, c[0][1] + vy

        def g_of(c):
            t = side_tile(c)
            return float(geo[t[1] - grid.y0, t[0] - grid.x0]) if grid.in_bounds(*t) else math.inf
        cands = sorted(((g_of(c), c) for c in cands), key=lambda gc: gc[0])
        cands = [(gv, c) for gv, c in cands if gv < math.inf]
        if not cands:
            raise NoRoute(f"coal can't reach the {nice(ore.resource)} belt (walled off)")
        tried, best = [], None
        for gv, c in cands:
            t, bd, side, lay = c
            if any(_taxi(t, u) < 4 and s2 == side for u, s2 in tried):
                continue
            tried.append((t, side))
            if len(tried) > tries:
                break
            gg = grid.copy()
            fc = replace(f)
            try:
                self._mine(gg, fc, side_tile(c))
                mine = fc.mine.placed()
                port = fc.mine.placed_ports()[0]
                fc.route = self._route(gg, port, Port("belt_in", COAL, t[0], t[1], bd, side), others + mine,
                                       max_nodes=int(min(COAL_NODES, 3000 + 300 * gv)), sideload=True)
            except (NoRoute, _Attempt):
                continue
            n = len(mine) + len(fc.route)
            if best is None or n < best[0]:
                best = (n, gg, fc, lay, mine)
            if len(fc.route) <= 1.25 * gv + 8 or len(tried) >= 4:
                break  # close to the walking distance (or tried enough): good enough
        if best is None:
            raise NoRoute(f"coal can't reach the {nice(ore.resource)} belt")
        _n, gg, fc, lay, mine = best
        f.mine, f.alts, f.drills, f.route = fc.mine, fc.alts, fc.drills, fc.route
        self._settle(gg, ore, lay)
        return gg, mine + f.route

    def attempt(self, base: Grid, hub: Hub, X, Y, flows, power):
        gd = self.gd
        grid = base.copy()
        hb = hub.block.at(X, Y)
        hub_ents = hb.placed()
        tiles = set()
        for e in hub_ents:
            tiles |= _tiles(gd, e)
        res_tiles = [(X + tx, Y + ty) for tx, ty in hub.reserved]
        if not grid.can_place(tiles | set(res_tiles)):
            raise _Attempt("hub doesn't fit")
        _occupy(grid, gd, hub_ents, "hub")
        grid.occupy(res_tiles, "reserved")
        ports = hb.placed_ports()
        grid.occupy([(p.x, p.y) for p in ports], "port")
        corridor = {}
        for p in ports:  # keep a straight approach free in front of every hub input
            if p.kind != "belt_in":
                continue
            vx, vy = DIR_VEC[p.direction]
            ts = [(p.x - vx * i, p.y - vy * i) for i in range(1, CORRIDOR + 1)]
            ts = [t for t in ts if grid.state_at(*t) != BLOCKED]
            grid.occupy(ts, "corridor")
            corridor[(p.x, p.y)] = ts
        flows = [replace(f, route=[], mine=None, goal=None) for f in flows]
        by_key = {f.key: f for f in flows}
        for ore, p in zip(hub.ores, [p for p in ports if p.kind == "belt_in"]):
            by_key[("ore", ore)].goal = p
        fixed = list(hub_ents)
        steam = None
        if power:
            boilers, engines, bflow = power
            bflow = replace(bflow, route=[], mine=None)
            flows.append(bflow)
            steam = self.place_power(grid, (X + hb.size[0] / 2, Y + hb.size[1] / 2), boilers, engines)
            if steam is None:
                raise _Attempt("no room for steam power near water")
            bflow.goal = next(p for p in steam[0].placed_ports() if p.kind == "belt_in")
            grid.occupy([(bflow.goal.x, bflow.goal.y)], "port")
            fixed += steam[0].placed() + [steam[1]] + steam[2]
        ore_flows = [f for f in flows if f.key[0] == "ore"]
        for f in sorted(ore_flows, key=lambda f: -f.drills):
            self._mine(grid, f, (f.goal.x, f.goal.y), ORE_LAYOUTS)

        def start(f):
            return f.mine.placed_ports()[0]
        by_port = [by_key[("ore", o)] for o in hub.ores]  # left to right along the input side
        mid = sorted(by_port, key=lambda f: abs(by_port.index(f) - (len(by_port) - 1) / 2))
        orders = [mid, by_port, by_port[::-1],
                  sorted(ore_flows, key=lambda f: _taxi((f.goal.x, f.goal.y), (start(f).x, start(f).y)))]
        orders = [o for i, o in enumerate(orders) if o not in orders[:i]]
        last = "routing failed"
        coal_tree = cKDTree(np.array(sorted(self.patches[COAL].tiles)) + 0.5)
        results = []
        for strategy in ("interleave", "ores_first"):
            for ro in orders:
                if len(results) >= self.keep or time.monotonic() > self.deadline:
                    break
                g = grid.copy()
                done = []
                fl = [replace(f) for f in flows]
                fk = {f.key: f for f in fl}
                f = ro[0]
                try:
                    for f in ro:
                        f = fk[f.key]
                        f.route = self._route(g, start(f), f.goal, fixed + done,
                                              corridor.get((f.goal.x, f.goal.y), ()))
                        done += f.route
                        if strategy == "interleave":  # its coal right away, before other ore belts fence it off
                            f = fk[("fuel", f.key[1])]
                            g, extra = self._coal_to(g, f, fk[("ore", f.key[1])], coal_tree, fixed + done)
                            done += extra
                    if strategy == "ores_first":
                        for f0 in ro:
                            f = fk[("fuel", f0.key[1])]
                            g, extra = self._coal_to(g, f, fk[("ore", f0.key[1])], coal_tree, fixed + done)
                            done += extra
                    if steam:
                        f = fk[("fuel", "boiler")]
                        self._mine(g, f, (f.goal.x, f.goal.y))
                        done += f.mine.placed()
                        f.route = self._route(g, start(f), f.goal, fixed + done)
                        done += f.route
                except (NoRoute, _Attempt) as e:
                    last = f"{f.key[0]} {nice(f.key[1])}: {e}"
                    continue
                belts = sum(1 for e in done if e.name == self.belt)
                results.append((belts, len(results), (g, hb, fl, steam)))
        if not results:
            raise _Attempt(last)
        results.sort(key=lambda r: r[:2])
        return [r[2] for r in results]

    # -- run -----------------------------------------------------------------------------------------------------
    def run(self) -> Plan:
        """Plan; if an ore mine only fits fewer drills than its furnaces need, plan once more with the furnace
        count cut to what that mine supplies (no idle furnaces to build)."""
        plan = self._run()
        gd = self.gd
        speed = gd.entity(self.furnace).get("crafting_speed") or 1
        cut = {}
        for key, n in self.furnaces.items():
            ore = ORES[key]
            got = plan.stats["mines"][f"ore:{ore}"][2]
            rec = B.smelt_recipe(gd, ore, self.furnace)
            fits = int(got * B.mining_rate(gd, self.drill, ore)[1] / (speed / rec["energy"] * rec["in"]) + 1e-9)
            if 0 < fits < n:
                cut[key] = fits
        if not cut:
            return plan
        before = dict(self.furnaces)
        self.furnaces.update(cut)
        self.reasons.append(f"re-planned with fewer furnaces {cut} (mines are smaller than planned)")
        try:
            again = self._run()
        except PlanError:
            self.furnaces = before
            return plan
        again.warnings.insert(0, "smaller mines than wanted here, so fewer furnaces: " +
                              ", ".join(f"{k} {before[k]} -> {v}" for k, v in cut.items()))
        return again

    def _run(self) -> Plan:
        gd, w = self.gd, self.world
        flows = self.flows()
        power = self.steam_size(flows) if self.stage == "electric" else None
        all_flows = flows + ([power[2]] if power else [])
        self.patches = self.pick_patches(all_flows)
        water_tile = None
        if power:
            nw = w.nearest_water_tile(*self.near)
            if nw is None:
                raise PlanError("no water body (>= 30 tiles) in the exported area for steam power")
            water_tile = nw[0]
        base = w.grid(self.area(self.patches, water_tile))
        self.amount = _amounts(w, base)
        arrays = [(ORES[k], n) for k, n in self.furnaces.items()]
        kw = dict(bus_len=self.bus_len, spare=self.spare, inserter=self.inserter, pole=self.pole, belt=self.belt,
                  furnace=self.furnace)
        proto = build_hub(gd, arrays, list(range(len(arrays))), **kw)
        sites = self.hub_sites(base, proto, self.patches, all_flows, water_tile)
        if not sites:
            raise PlanError("no free, ore-free spot big enough for the smelting hub")
        cache, plans, ok_sites = {}, [], 0
        for _cost, k, X, Y in sites:
            if time.monotonic() > self.deadline:
                self.reasons.append("time budget used up")
                break
            if ok_sites >= self.want_sites:
                break
            hb = proto.rotated(k).block
            hcx, hcy = X + hb.size[0] / 2, Y + hb.size[1] / 2
            ang = {}
            for ore, _n in arrays:
                p = self.patches[ore]
                ang[ore] = _ccw_from_bus(*_rot_vec(p.center[0] - hcx, p.center[1] - hcy, -k))
            got = False
            for order in array_orders([a[0] for a in arrays], ang):
                if order not in cache:
                    cache[order] = build_hub(gd, arrays, list(order), **kw)
                hub = cache[order].rotated(k)
                try:
                    cands = self.attempt(base, hub, X, Y, flows, power)
                except _Attempt as e:
                    self.reasons.append(f"site ({X}, {Y}) turn {k} order {order}: {e}")
                    continue
                for g, hbp, fl, steam in cands[:2]:
                    try:
                        plans.append(self.finish(base, g, hub, hbp, fl, steam))
                        got = True
                        break
                    except _Attempt as e:
                        self.reasons.append(f"site ({X}, {Y}) turn {k} order {order}: {e}")
                if got:
                    break
            ok_sites += got
        if not plans:
            raise PlanError(f"no valid {self.v.key} {self.stage} plan found", self.reasons)
        return min(plans, key=lambda p: p.stats["belts"] + 0.3 * p.stats["clear"] + 0.1 * p.stats["entities"])

    def finish(self, base, grid, hub: Hub, hb: Block, flows, steam):
        gd = self.gd
        ents = list(hb.placed())
        for f in flows:
            ents += f.mine.placed() + f.route
        if steam:
            sents = steam[0].placed()
            ents += sents + [steam[1]] + steam[2]
            sources = [e for e in ents if gd.entity(e.name).get("type") == "electric-pole"]
            try:
                ents += place_poles(grid, gd, [e for e in ents if _is_electric(gd, e.name)], self.pole,
                                    sources=sources)
            except NoRoute as e:
                raise _Attempt(f"poles: {e}")
        bus_ports = [p for p in hb.placed_ports() if p.kind == "belt_out"]
        needs = sorted({e.name for e in ents if not gd.can_build(e.name)})
        expect = {"ports": bus_ports, "strict_fuel": True}  # a coal shortfall anywhere blocks the plan
        if needs:
            expect["assume_research"] = True
        rep = validate(ents, gd, self.world, expect)
        if not rep.ok:
            raise _Attempt("validation: " + "; ".join(f"{i.kind}: {i.msg}" for i in rep.errors[:3]))
        tiles = set()
        for e in ents:
            tiles |= _tiles(gd, e)
        clear = list(base.to_clear(tiles))
        return self.describe(ents, clear, rep, hub, hb, flows, steam, needs)

    # -- summary -----------------------------------------------------------------------------------------------
    def describe(self, ents, clear, rep, hub, hb, flows, steam, needs) -> Plan:
        gd = self.gd
        speed = gd.entity(self.furnace).get("crafting_speed") or 1
        per_min = {}
        for ore, n, _blk in hub.arrays:
            rec = B.smelt_recipe(gd, ore, self.furnace)
            fl = next(f for f in flows if f.key == ("ore", ore))
            _item, rate = B.mining_rate(gd, self.drill, ore)
            crafts = min(n * speed / rec["energy"], fl.drills * rate / rec["in"])
            per_min[rec["product"]] = round(crafts * rec["out"] * 60, 1)
        tiles = set()
        for e in ents:
            tiles |= _tiles(gd, e)
        x0, y0, x1, y1 = _rect_of(tiles)
        counts = entity_counts(ents)
        belts = counts.get(self.belt, 0)
        base_items, craft_s, _crafted = raw_materials(gd, counts)
        bm = build_minutes(base_items, craft_s, len(ents), belts)
        power_kw = sum(gd.entity(e.name).get("max_energy_usage_kw") or gd.entity(e.name).get("energy_usage_kw") or 0
                       for e in ents if _is_electric(gd, e.name))
        gen_kw = steam[0].rates.get("power_kw", 0) if steam else 0
        drills = sum(f.drills for f in flows)
        coal_min = sum(B.fuel_per_min(gd, self.furnace, COAL, n) for _o, n, _b in hub.arrays)
        if B._burner(gd, self.inserter):  # input inserters: busy fraction x their power (output ones are hand-fed)
            for ore, n, _b in hub.arrays:
                rec = B.smelt_recipe(gd, ore, self.furnace)
                swings = n * speed / rec["energy"] * (rec["in"] + 0.1)
                busy = min(n, swings / gd.inserter_throughput(self.inserter, stack=1))
                coal_min += busy * (gd.entity(self.inserter).get("max_energy_usage_kw") or 0) * 60 / (
                    gd.fuel_value(COAL) / 1000)
        if B._burner(gd, self.drill):
            coal_min += B.fuel_per_min(gd, self.drill, COAL, drills)
        if steam:
            coal_min += -steam[0].rates.get(COAL, 0)
        hub_c = (round(hb.origin[0] + hb.size[0] / 2), round(hb.origin[1] + hb.size[1] / 2))
        manual = sum(1 for e in ents if (e.extra or {}).get("fuel") == "manual")
        stats = {
            "per_min": per_min, "footprint": [x1 - x0, y1 - y0], "bbox": [x0, y0, x1, y1], "belts": belts,
            "entities": len(ents), "counts": counts, "materials": base_items, "craft_s": round(craft_s),
            "build_min": bm, "power_kw": round(power_kw), "generation_kw": round(gen_kw), "drills": drills,
            "furnaces": dict(self.furnaces), "coal_per_min": round(coal_min, 1), "hub": list(hub_c),
            "mines": {f"{f.key[0]}:{f.key[1]}": [f.mine.placed_ports()[0].x, f.mine.placed_ports()[0].y, f.drills]
                      for f in flows},
            "needs_research": needs, "clear": len(clear), "manual_fuel": manual,
            "ports": [[p.kind, p.item, p.x, p.y, p.direction, p.lane] for p in hb.placed_ports()
                      if p.kind == "belt_out"],
            "assume_research": bool(needs),
        }
        warnings = []
        if needs:
            warnings.append("needs research first: " + ", ".join(nice(n) for n in needs[:6]))
        hand_min = None
        if manual:
            kw = max((gd.entity(e.name).get("max_energy_usage_kw") or gd.entity(e.name).get("energy_usage_kw") or 0)
                     for e in ents if (e.extra or {}).get("fuel") == "manual")
            hand_min = int(50 * gd.fuel_value(COAL) / 1000 / kw / 60) if kw else None
            last = f" (50 coal lasts about {hand_min} min)" if hand_min else ""
            warnings.append(f"{manual} burner drills/inserters are fed by hand{last}: refuel them regularly "
                            "(Plans tab: Helper: refuel)")
        if steam:
            warnings.append("put a few coal into the boilers by hand to start the power plant")
        nests = [o for o in self.world.obstacles_in((x0 - NEST_RANGE, y0 - NEST_RANGE, x1 + NEST_RANGE, y1 + NEST_RANGE))
                 if o.kind == "enemy"]
        if nests:
            n = min(nests, key=lambda o: _taxi((o.x, o.y), hub_c))
            warnings.append(f"{len(nests)} biter nest buildings within {NEST_RANGE} tiles of the build (nearest at "
                            f"[gps={round(n.x)},{round(n.y)}]): pollution will draw attacks, clear them or defend")
            stats["nests"] = len(nests)
        for f in flows:
            if f.drills >= f.want:
                continue
            if f.key[0] == "ore":
                warnings.append(f"the {nice(f.resource)} mine has room for {f.drills} of {f.want} drills here, "
                                "so its furnaces won't all be busy")
            else:
                what = "the boilers" if f.key[1] == "boiler" else f"the {nice(f.key[1])} furnaces"
                warnings.append(f"the coal mine for {what} has room for {f.drills} of {f.want} drills here: "
                                "enough coal, but no spare")
        warnings += [i.msg for i in rep.warnings if i.kind not in ("research", "belt_unused")][:4]
        stage_txt = ("burner bootstrap, buildable from nothing" if self.stage == "bootstrap"
                     else "electric with steam power")
        prod = ", ".join(f"{fmt_n(v)} {nice(k)}" for k, v in per_min.items())
        fur = ", ".join(f"{n} {k}" for k, n in self.furnaces.items())
        mats = ", ".join(f"{fmt_n(v)} {nice(k)}" for k, v in list(base_items.items())[:4])
        s = [f"{self.v.title} ({stage_txt}): {self.v.pitch}.",
             f"Makes about {prod} per minute with {fur} furnaces fed by {drills} drills.",
             f"Hub at [gps={hub_c[0]},{hub_c[1]}], footprint {x1 - x0} by {y1 - y0} tiles, {belts} belts, "
             f"{len(clear)} trees or rocks to clear.",
             f"Materials: about {mats}."]
        if steam:
            s.append(f"Power: {round(gen_kw / 1000, 1)} MW of steam ({steam[0].info['boilers']} boilers, "
                     f"{steam[0].info['engines']} engines) for {round(power_kw / 1000, 2)} MW of machines.")
        else:
            s.append("No power needed.")
        s.append(f"Coal use about {fmt_n(coal_min)} per minute. Helper build time roughly {bm['stocked']} min "
                 f"with materials in a chest, about {bm['scratch']} min if it gathers everything itself.")
        if hand_min:
            s.append(f"The {manual} burner drills and output inserters are fed by hand: 50 coal each lasts about "
                     f"{hand_min} minutes, so top them up every {max(5, hand_min - 2)} minutes or so (Plans tab: "
                     f"Helper: refuel).")
            stats["hand_fuel_min"] = hand_min
        if needs:
            s.append("Needs research first: " + ", ".join(nice(n) for n in needs[:5]) + ".")
        pid = f"{self.v.key[:3]}-{self.stage[:4]}-{uuid.uuid4().hex[:4]}"
        return Plan(pid, f"{self.v.title} ({self.stage})", self.v.key, self.stage, " ".join(s), ents, clear,
                    warnings, rep.to_dict(), stats, self.world.surface or "nauvis")


# ---- public API -------------------------------------------------------------------------------------------------
def plan_starter_base(world, gd, goal=None, variant="compact", **kw) -> Plan:
    """One validated starter-base Plan (raises PlanError with .reasons when no configuration passes)."""
    return Planner(world, gd, goal, variant, **kw).run()


def plan_variants(world, gd, goal=None, variants=DEFAULT_VARIANTS, **kw) -> list[Plan]:
    """2-3 validated plans with different trade-offs (variants that can't be planned here are skipped; PlanError
    only if none works)."""
    out, reasons = [], []
    for v in variants:
        try:
            out.append(plan_starter_base(world, gd, goal, v, **kw))
        except PlanError as e:
            reasons.append(f"{v}: {e} ({'; '.join(e.reasons[-3:])})")
    if not out:
        raise PlanError("no variant could be planned", reasons)
    for p in out:
        p.stats["skipped_variants"] = reasons
    return out
