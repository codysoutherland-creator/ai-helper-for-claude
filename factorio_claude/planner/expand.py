"""Master growth past the starter base (docs/design/AUTOPILOT.md section 8, autopilot round 2).

    info = extend(mp, world, gd, "mall")          # appends one stage to the master plan (in place) -> info dict
    info = extend(mp, world, gd, "power_grow")
    info = extend(mp, world, gd, "steel")
    templates_built(mp) -> {"mall": stage index, ...}

Every expansion is a block on the master's grid (the ground of every stage, built or not, every reserved zone, the
player's builds and the keep-out boxes are obstacles), fed by INSERTER TAPS on the master's own plate or coal belts
(an inserter beside a plate belt moves plates onto a new belt: nothing of the master is taken apart or rebuilt, and
the consumers upstream of the tap keep their priority), with short straight routes (undergrounds once Logistics is
researched) and poles joined to the master's network. The stage is validated as the whole base after it (like every
master stage) before it is appended; a failure leaves the master unchanged and raises ExpandError.

Templates:
  mall        mall row (blocks.mall_block) in a reserved zone; iron + copper taps; its output chests are the helper's
              first material source (AUTOPILOT.md 7.1)
  power_grow  the power block's reserved units (boiler + 2 engines each), in the zone the starter base reserved
  steel       a small steel column (stone furnaces): iron plates and coal from taps, steel into a depot chest
  radar       one radar (repeatable): the first next to the hub, each later one on the edge of the base's built
              bounding box toward uncharted ground / ore the autopilot needs (hints), spaced so the areas the radars
              keep charted tile without much overlap; a pole chain joins it to the master's network
  blue        blue science (round 4, roadmap G10): blocks.blue_block beside the bus (iron + copper taps), steel and
              plastic out of their depot chests, petroleum from the oil plant's header and water from an offshore pump
              piped to its sulfur plant, the packs on a belt to a row of labs chained from one of the master's labs
  robots      the robot mall (round 5, roadmap G11): blocks.robot_block beside the bus (iron + copper taps), steel and
              plastic from their depots (or a tap on a belt that carries only them), crude oil branched off the oil
              plant's crude pipes to its own refinery, water from an offshore pump; robots, roboports and logistic
              chests into its chests (the helper's mall sources)
  roboports   the roboport grid (repeatable, rule R12): roboports covering the built area with their construction
              areas, one network (logistic areas overlap), storage chests beside the first
Stage indices: the mall comes right after the stage the caller names (S4 -> 4.7, label "S4m") so the starter base's
last stages are built from the mall; every other expansion gets the next whole number (S6, S7, ...).
"""
from __future__ import annotations

import copy
import math

import numpy as np

from . import blocks as B
from . import master as M
from .blocks import PE, Port
from .grid import BLOCKED
from .placer import _Sum
from .router import NoRoute, place_poles, route_belt, world_others

NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12
DVEC = {NORTH: (0, -1), EAST: (1, 0), SOUTH: (0, 1), WEST: (-1, 0)}
MARGIN = 72                     # tiles around the master the expansion may use
TAP_TRIES = 30                  # tap candidates routed per input before the next block spot is tried
SPOT_TRIES = 6                  # block spots tried per template
GAP = 2                         # free tiles kept around a new block (walkway; doctrine ORG-3 wants >= 3 between blocks)
TEMPLATES = ("mall", "power_grow", "steel", "science", "oil", "oil_plant", "iron_grow", "copper_grow", "mall_add",
             "radar", "blue", "robots", "roboports")
STAGE_KEYS = {"mall": "x_mall", "power_grow": "x_power", "steel": "x_steel", "science": "x_science", "oil": "x_oil",
              "oil_plant": "x_oilplant", "iron_grow": "x_iron", "copper_grow": "x_copper", "mall_add": "x_malladd",
              "radar": "x_radar", "blue": "x_blue", "robots": "x_robots", "roboports": "x_roboports"}
TITLES = {"mall": "Mall (belts, inserters, drills, assemblers from assemblers)", "power_grow": "More steam power",
          "steel": "Steel smelting on the plate bus",
          "science": "Second red + green science block (+30/min each) and more iron",
          "oil": "Pumpjacks on crude oil", "oil_plant": "Oil refinery and plastic",
          "iron_grow": "More iron: a smelting cell onto the plate bus",
          "copper_grow": "More copper: a smelting cell onto the plate bus",
          "mall_add": "More mall items (machines in the mall row's free slots)",
          "radar": "Radar (charts the map around it)",
          "blue": "Blue science: sulfur, engines, advanced circuits, chemical science packs into the labs",
          "robots": "Robot mall: batteries, frames, construction and logistic robots, roboports, logistic chests",
          "roboports": "Roboports: construction robots build in their area"}
# (radar: 2.1 locks its recipe behind the Radar tech - 20 red packs - although 1.1 had it from the start; verified in
# the data export, tests/fixtures/data_fresh.json.gz)
GATES = {"mall": ["automation", "electric-mining-drill", "logistics"], "power_grow": [], "steel": ["steel-processing"],
         "science": ["logistic-science-pack", "logistics"], "oil": ["oil-gathering", "fluid-handling"],
         "oil_plant": ["oil-processing", "plastics"], "iron_grow": ["electric-mining-drill"],
         "copper_grow": ["electric-mining-drill"], "mall_add": [],     # mall_add: the added items' techs (its info)
         "radar": ["radar"],
         # (Automation 2 for the block's assemblers 2 comes before Fluid handling, i.e. before the oil stages)
         "blue": ["engine", "advanced-circuit", "sulfur-processing", "chemical-science-pack"],
         # round 5 (G11): the robot mall's recipes - frames (Robotics: Battery + Electric engine <- Lubricant <-
         # Advanced oil processing), roboports / storage / passive provider chests / construction robots (Construction
         # robotics), logistic robots (Logistic robotics); 2.1 names from the data export. Requester chests are behind
         # Logistic system, which needs space science in Space Age: not on the roadmap
         "robots": ["construction-robotics", "logistic-robotics"], "roboports": ["construction-robotics"]}
GROW = {"iron_grow": "iron-ore", "copper_grow": "copper-ore"}   # repeatable: the autopilot adds one per plate shortfall
REPEATABLE = ("power_grow", "mall_add", "radar", "roboports") + tuple(GROW)
WIDE_MARGIN = 150               # steel / science may lay the bus and put its smelting cells on a farther patch
WIDE_TEMPLATES = ("steel", "science", "oil", "oil_plant", "iron_grow", "copper_grow", "blue", "robots", "roboports")


class ExpandError(RuntimeError):
    pass


def _x(e) -> dict:
    return e.extra if isinstance(e.extra, dict) else {}


def templates_built(mp) -> dict:
    """{template: stage index} of the expansions this master already has."""
    out = {}
    for s in mp.stages:
        for t, key in STAGE_KEYS.items():
            if s.key == key or s.key.startswith(key + "_"):
                out.setdefault(t, s.index)
    return out


def _tiles(gd, e) -> set:
    return gd.footprint(e.name, e.x, e.y, e.direction or 0)


def _tag(e) -> str:
    io = {"input": "in", "output": "out"}.get(_x(e).get("type"), "")
    return f"existing:{e.name}@{int(e.direction or 0) % 16}{io}"


# ---- the grid ---------------------------------------------------------------------------------------------------------
class _Ctx:
    """The master's ground as one grid: world terrain + the world's entities + every master entity (any stage, as if
    built) + every reserved zone + keep-out boxes. Expansion entities are added with add()."""

    def __init__(self, mp, world, gd, keepout=(), free_zones=(), margin=MARGIN):
        self.mp, self.world, self.gd = mp, world, gd
        xs = [e.x for e in mp.ents] or [0.0]
        ys = [e.y for e in mp.ents] or [0.0]
        wx0, wy0, wx1, wy1 = world.bbox
        self.rect = (max(wx0, math.floor(min(xs)) - margin), max(wy0, math.floor(min(ys)) - margin),
                     min(wx1, math.ceil(max(xs)) + margin), min(wy1, math.ceil(max(ys)) + margin))
        g = world.grid(self.rect)
        for e in mp.ents:
            ts = [t for t in _tiles(gd, e) if g.in_bounds(*t)]
            g.occupy(ts, _tag(e))
            for tx, ty in ts:
                g.terrain[ty - g.y0, tx - g.x0] = BLOCKED
        free_zones = set(free_zones)
        for z in mp.zones:
            if z.id in free_zones:
                continue
            x0, y0, x1, y1 = z.rect
            ts = [(x, y) for x in range(x0, x1) for y in range(y0, y1) if g.in_bounds(x, y) and g.state_at(x, y) != BLOCKED]
            g.occupy(ts, f"zone:{z.id}")
        for b in keepout or ():
            x0, y0, x1, y1 = (math.floor(b[0]), math.floor(b[1]), math.ceil(b[2]), math.ceil(b[3]))
            ts = [(x, y) for x in range(x0, x1) for y in range(y0, y1) if g.in_bounds(x, y) and g.state_at(x, y) != BLOCKED]
            g.occupy(ts, "keepout")
        self.grid = g
        self.new: list = []           # PE of the expansion (map coordinates)
        # taps use fast inserters once Fast inserter is researched (2.3 items/s each instead of 0.83)
        self.fast = "fast-inserter" if gd.can_build("fast-inserter") else None
        self.shape = None             # route shape limits (ratio, slack, turns); None: the strict defaults
        self.others = world_others(world, self.rect) + [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e)))
                                                          for e in mp.ents]

    def add(self, ents, tag="exp"):
        for e in ents:
            ts = [t for t in _tiles(self.gd, e) if self.grid.in_bounds(*t)]
            self.grid.occupy(ts, _tag(e))
            for tx, ty in ts:
                self.grid.terrain[ty - self.grid.y0, tx - self.grid.x0] = BLOCKED
            self.new.append(e)
            self.others.append(e)

    def free(self, t) -> bool:
        return self.grid.in_bounds(*t) and self.grid.state_at(*t) != BLOCKED

    def sums(self):
        g = self.grid
        return _Sum(g, g.placeable_mask(allow_clear=True)), _Sum(g, g.res != 0)


# ---- what the master's belts carry ------------------------------------------------------------------------------------
def belt_items(ents, gd, world=None) -> dict:
    """{tile: (entity name, direction, set of items, n downstream consumer pickups)} for every transport belt of
    `ents` (the validator's item propagation over the planned base)."""
    from .validate import _Validator
    v = _Validator(gd, world, {"assume_research": True, "connect_existing": True})
    try:
        v.run(list(ents))
    except Exception:  # noqa: BLE001 - no items known: no taps
        return {}
    if not hasattr(v, "nodes") or not hasattr(v, "lane_items"):
        return {}
    picks = {}
    for s, sn, d, dn, mover, kind in getattr(v, "links", []):
        if kind == "ins" and sn is not None:
            picks[id(sn)] = picks.get(id(sn), 0) + 1
    # consumers downstream of every node (bounded walk)
    down = {}
    for n in v.nodes:
        k, seen, m = 0, {id(n)}, n
        for _ in range(400):
            if not m.outs:
                break
            m = m.outs[0][0]
            if id(m) in seen:
                break
            seen.add(id(m))
            k += picks.get(id(m), 0)
        down[id(n)] = k
    out = {}
    for n in v.nodes:
        if n.kind != "belt":
            continue
        li = v.lane_items.get(id(n)) or (set(), set())
        items = (set(li[0]) | set(li[1])) - {"*"}
        for t in n.tiles:
            out[t] = (n.r.name, n.dir, items, down[id(n)])
    return out


def tap_candidates(ctx: _Ctx, items_at: dict, item: str, n_ins: int = 1, clear: int = 0) -> list:
    """Spots for an inserter tap on a belt carrying `item`: [(belt tile, side dir, [inserter PE], drop tile, flow dir,
    downstream consumers)]. The inserter stands beside the belt and drops onto the free tile beyond it (the new
    belt's first tile). n_ins = 2: two inserters on consecutive tiles of the belt drop onto two consecutive tiles."""
    out = []
    for t, (name, d, items, down) in items_at.items():
        # only a belt that carries nothing else (an inserter takes whatever passes: a mixed ore + coal trunk would
        # send ore down a coal feed)
        if set(items) != {item} or name != "transport-belt":
            continue
        for side in ((d + 4) % 16, (d + 12) % 16):
            nx, ny = DVEC[side]
            fx, fy = DVEC[d]
            # reach 1: an inserter right beside the belt; reach 2: a long-handed inserter over the neighbouring belt
            # (the second belt of a bus: its copper is taken from the iron side)
            for reach in (1, 2):
                if reach == 2 and (t[0] + nx, t[1] + ny) not in items_at:
                    continue
                ok, ins, drops = True, [], []
                for i in range(n_ins):
                    b = (t[0] + fx * i, t[1] + fy * i)
                    bi = items_at.get(b)
                    if bi is None or set(bi[2]) != {item} or bi[1] != d:
                        ok = False
                        break
                    T = (b[0] + reach * nx, b[1] + reach * ny)
                    D = (b[0] + 2 * reach * nx, b[1] + 2 * reach * ny)
                    if not (ctx.free(T) and ctx.free(D)) or (reach == 2 and (b[0] + nx, b[1] + ny) not in items_at):
                        ok = False
                        break
                    ins.append(PE((ctx.fast or "inserter") if reach == 1 else "long-handed-inserter", T[0] + 0.5,
                                  T[1] + 0.5,
                                  (side + 8) % 16, None, {"role": "tap", "item": item}))
                    drops.append(D)
                if ok and clear:
                    # open ground beyond the drop (a bus stretch where no block stands yet)
                    D = drops[-1]
                    ok = all(ctx.free((D[0] + nx * j, D[1] + ny * j)) for j in range(1, clear + 1))
                if ok:
                    # a pole right beside the tap inserters (on their row, before or after them along the belt)
                    T0 = (math.floor(ins[0].x), math.floor(ins[0].y))
                    T1 = (math.floor(ins[-1].x), math.floor(ins[-1].y))
                    spot = next((p for p in ((T0[0] - fx, T0[1] - fy), (T1[0] + fx, T1[1] + fy)) if ctx.free(p)
                                 and p not in drops), None)
                    if spot is None:
                        continue
                    ins = ins + [PE("small-electric-pole", spot[0] + 0.5, spot[1] + 0.5, 0, None,
                                    {"role": "tap_pole"})]
                    out.append((t, side, ins, drops, d, down))
    return out


def _reachable(g, a, b) -> bool:
    """a and b (tiles) in one 4-connected region of non-blocked tiles (both counted free). A fast pre-check: a
    route that would need an underground to get out of a pocket is still tried, with a small node budget."""
    from scipy import ndimage
    free = g.state != BLOCKED
    for t in (a, b):
        if g.in_bounds(*t):
            free[t[1] - g.y0, t[0] - g.x0] = True
    lab, _n = ndimage.label(free)

    def L(t):
        return int(lab[t[1] - g.y0, t[0] - g.x0]) if g.in_bounds(*t) else 0
    return L(a) != 0 and L(a) == L(b)


ROUTE_RATIO, ROUTE_SLACK, ROUTE_TURNS = 1.4, 12, 6     # doctrine ORG-1: proportional, few turns (no spaghetti)
SHAPES = ((ROUTE_RATIO, ROUTE_SLACK, ROUTE_TURNS),       # first try: strict
          (2.0, 16, 10))                                  # then: relaxed (a block that fits beats none), never loops


def route_shape(ents, start, goal) -> dict:
    """{tiles, manhattan, ratio, turns} of a routed belt (its tiles in route order; undergrounds count as straight)."""
    belts = [e for e in ents if "belt" in e.name]
    m = abs(int(start.x) - int(goal.x)) + abs(int(start.y) - int(goal.y))
    dirs = [int(e.direction or 0) for e in belts]
    turns = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
    tiles = len(belts)       # an underground pair counts its two ends (the gap between them is free ground)
    return {"tiles": tiles, "manhattan": m, "ratio": round(tiles / max(1, m), 2), "turns": turns}


def proportional(shape: dict, limits=None) -> bool:
    """A feed that is not spaghetti: at most 1.4x its end points' taxicab distance + 12 tiles, at most 6 turns
    (limits = (ratio, slack, turns) overrides)."""
    ratio, slack, turns = limits or (ROUTE_RATIO, ROUTE_SLACK, ROUTE_TURNS)
    return shape["tiles"] <= ratio * shape["manhattan"] + slack and shape["turns"] <= turns


def _route(ctx: _Ctx, start: Port, goal: Port, tag="exp", shape_check=True, wide=False):
    """A belt from port `start` to port `goal` on the expansion grid, or None. shape_check: a route that snakes
    (doctrine ORG-1, see proportional) counts as no route, so the caller tries its next spot or tap (round 3 judge:
    nested feeder loops across the map). wide: the full search budget even when the ends are not in one free region
    (a long feed that must pass under the base's belts: the blue block's steel and plastic)."""
    g = ctx.grid
    S, G = (int(start.x), int(start.y)), (int(goal.x), int(goal.y))
    dist = abs(S[0] - G[0]) + abs(S[1] - G[1])
    budget = min(250_000, 4000 + 600 * dist) if wide or _reachable(g, S, G) else min(20_000, 2000 + 100 * dist)
    gc = g.copy()
    try:
        ents = route_belt(gc, ctx.gd, start, goal, allow_underground=None, others=ctx.others, tag=tag,
                          max_nodes=budget)
    except NoRoute:
        return None
    if shape_check and not proportional(route_shape(ents, start, goal), getattr(ctx, "shape", None)):
        return None
    return ents


def _feed(ctx: _Ctx, items_at, item, goal: Port, n_ins=1, max_len=160, allow_cell=True):
    """A tap on a belt carrying `item` and a belt from it to port `goal`. -> (entities, info) or None. The tap with the
    shortest straight-line distance (plus a penalty for consumers downstream of it) is routed first. Plates with no
    reachable tap (a starter hub's plate lines run between its blocks) come from a smelting cell of their own
    (allow_cell)."""
    got = _tap_feed(ctx, items_at, item, goal, n_ins, max_len)
    if got is None and allow_cell and item in ("iron-plate", "copper-plate"):
        got = _cell(ctx, items_at, item.replace("-plate", "-ore"), (goal.x, goal.y), goal)
    return got


def _tap_feed(ctx: _Ctx, items_at, item, goal: Port, n_ins=1, max_len=160, clear=0, wide=False):
    cands = tap_candidates(ctx, items_at, item, n_ins, clear)
    if not cands and n_ins > 1:
        cands = tap_candidates(ctx, items_at, item, 1, clear)
    if not cands:
        return None
    gx, gy = goal.x, goal.y

    def key(c):
        D = c[3][-1]
        return abs(D[0] - gx) + abs(D[1] - gy) + 6 * c[5]
    cands.sort(key=key)
    seen = set()
    tries = 0
    for t, side, ins, drops, d, down in cands:
        if tries >= TAP_TRIES:
            break
        k = (drops[-1], side)
        if k in seen:
            continue
        seen.add(k)
        tries += 1
        # stub belts under all drops but the last (flowing along the source belt), then the route from the last drop
        stub = [PE("transport-belt", D[0] + 0.5, D[1] + 0.5, d, None, {"role": "tap_belt"}) for D in drops[:-1]]
        save = ctx.grid.copy()
        for e in ins + stub:
            ctx.grid.occupy(list(_tiles(ctx.gd, e)), _tag(e))
        D = drops[-1]
        start = Port("belt_out", item, D[0], D[1], d)
        route = _route(ctx, start, goal, wide=wide)
        if route is None or len(route) > max_len:
            ctx.grid = save
            continue
        ctx.grid = save
        ents = ins + stub + [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role="feed", item=item))
                             for e in route]
        ctx.add(ents)
        return ents, {"item": item, "tap": list(t), "belt_tiles": len(route) + len(stub), "inserters": len(ins)}
    return None


# ---- smelting cells: an electric ore field, a smelting column at its head, coal from a tap -----------------------------
MIN_ORE_E = 10                  # ore tiles under an electric drill's 5x5 area for a slot to count (as master.py)
CELL = {"iron-ore": {"slots": 12, "pairs": 6}, "copper-ore": {"slots": 4, "pairs": 2}}   # 225 iron, 75 copper/min
CELL_FEED_MAX = 64              # belt tiles from a cell to the bus: farther patches are not worth a lone feeder belt
CELL_COAL_MAX = 80              # (round 3 judge: 130-340-tile single feeders from far patches read as spaghetti; a
#                                 bus fed by taps on the starter base's surplus instead is short and tidy)
ELECTRIC_ONLY = {"buffer", "in_b", "out_b", "drill_b", "ins_b", "corr", "feed"}


def _field_part(gd, ore, slots):
    blk = B.mining_field(slots, gd, resource=ore)
    ents = [e for e in blk.entities if _x(e).get("role") not in ELECTRIC_ONLY]
    return M.Part("cell_field", [copy.deepcopy(e) for e in ents], blk.size, {"ore": blk.ports[0]})


def _column_part(gd, ore, pairs, merged=True):
    """An electric smelting column (blocks.smelter_column without its burner bootstrap). merged: both plate belts
    turn inward at the top and side-load onto one belt (west plates on its left lane, east on its right), which
    leaves at port "plate" (5, -3) flowing north: every plate of the column on one full belt."""
    blk = B.smelter_column(pairs, gd, ore=ore)
    ents = [copy.deepcopy(e) for e in blk.entities if _x(e).get("role") not in ELECTRIC_ONLY]
    ports = dict(zip(["plate_W", "plate_E", "ore", "coal"], blk.ports))
    if merged:
        put = B._put
        ents += [put(gd, "transport-belt", 0, -1, NORTH, extra=B._r("plate_merge", row=-1)),
                 put(gd, "transport-belt", 10, -1, NORTH, extra=B._r("plate_merge", row=-1))]
        ents += [put(gd, "transport-belt", x, -2, EAST, extra=B._r("plate_merge", row=-2)) for x in range(0, 5)]
        ents += [put(gd, "transport-belt", x, -2, WEST, extra=B._r("plate_merge", row=-2)) for x in range(6, 11)]
        ents.append(put(gd, "transport-belt", 5, -2, NORTH, extra=B._r("plate_merge", row=-2)))
        item = ports["plate_W"].item
        ports = {"plate": Port("belt_out", item, 5, -3, NORTH), "ore": ports["ore"], "coal": ports["coal"]}
        ents = [e.moved(0, 2) for e in ents]
        ports = {k: p.moved(0, 2) for k, p in ports.items()}
        return M.Part("cell_column", ents, (blk.size[0], blk.size[1] + 2), ports)
    return M.Part("cell_column", ents, blk.size, ports)


def _place_field(ctx: _Ctx, gd, ore, slots, target):
    """An electric field on the nearest patch of `ore` with free ground (drills only on ore of that kind)."""
    g = ctx.grid
    if ore not in g.res_names:
        return None
    rid = g.res_names.index(ore)
    mine, other = _Sum(g, g.res == rid), _Sum(g, (g.res != 0) & (g.res != rid))
    ok, _ore = ctx.sums()
    patches = sorted(ctx.world.patches(min_tiles=40, name=ore),
                     key=lambda p: abs(p.center[0] - target[0]) + abs(p.center[1] - target[1]))
    for n in range(slots, max(2, slots // 2) - 1, -2):     # at least half the slots: a cell feeds its column
        part = _field_part(gd, ore, n)
        best = None
        for patch in patches[:4]:
            bx0, by0, bx1, by1 = patch.bbox
            for k in range(4):
                rp = part.rotated(k)
                w, h = rp.size
                xs = np.arange(max(bx0 - 8, g.x0 + 1), min(bx1 + 8, g.x0 + g.w - 1) - w + 1)
                ys = np.arange(max(by0 - 8, g.y0 + 1), min(by1 + 8, g.y0 + g.h - 1) - h + 1)
                if not len(xs) or not len(ys):
                    continue
                X, Y = np.meshgrid(xs, ys)
                X, Y = X.ravel(), Y.ravel()
                good = ok.count(X, Y, (0, 0, w, h)) == w * h      # the whole rect: it becomes a reserved zone
                for e in rp.ents:
                    if gd.entity(e.name).get("type") != "mining-drill":
                        fx = [t for t in _tiles(gd, e)]
                        for tx, ty in fx:
                            good &= ok.count(X, Y, (tx, ty, tx + 1, ty + 1)) == 1
                        continue
                    x0, y0 = math.floor(e.x - 1.5), math.floor(e.y - 1.5)
                    good &= ok.count(X, Y, (x0, y0, x0 + 3, y0 + 3)) == 9
                    good &= mine.count(X, Y, (x0 - 1, y0 - 1, x0 + 4, y0 + 4)) >= MIN_ORE_E
                    good &= other.count(X, Y, (x0 - 1, y0 - 1, x0 + 4, y0 + 4)) == 0
                    if not good.any():
                        break
                p = rp.ports["ore"]
                vx, vy = DVEC[p.direction]
                for i in range(3):            # the head port and its lane on free ground
                    good &= ok.count(X, Y, (p.x + vx * i, p.y + vy * i, p.x + vx * i + 1, p.y + vy * i + 1)) == 1
                if not good.any():
                    continue
                X, Y = X[good], Y[good]
                sc = np.abs(X + p.x - target[0]) + np.abs(Y + p.y - target[1])
                i = int(np.argmin(sc))
                if best is None or sc[i] < best[0]:
                    best = (float(sc[i]), rp.at(int(X[i]), int(Y[i])))
        if best is not None:
            return best[1]
    return None


def _depot(ctx: _Ctx, port: Port, item):
    """An inserter on a belt_out port tile taking from the belt end into a chest one tile further: [PE] or None."""
    vx, vy = DVEC[port.direction]
    T, C = (port.x, port.y), (port.x + vx, port.y + vy)
    if not (ctx.free(T) and ctx.free(C)):
        return None
    return [PE("inserter", T[0] + 0.5, T[1] + 0.5, (port.direction + 8) % 16, None, {"role": "depot_in", "item": item}),
            PE("iron-chest", C[0] + 0.5, C[1] + 0.5, 0, None, {"role": "depot", "item": item, "holds": 0,
                                                              "bar": DEPOT_BAR})]


def _cell(ctx: _Ctx, items_at, ore, target, goal: Port | None, *, slots=None, pairs=None, max_plate=None):
    """A smelting cell for `ore` near `target`: electric field on the patch, a column at its head, coal from a tap,
    the column's plates (one merged belt) to port `goal` (or a depot). max_plate: longest plate belt to the goal.
    -> (ents, info) or None (the grid is unchanged then)."""
    gd = ctx.gd
    spec = CELL.get(ore, {"slots": 4, "pairs": 2})
    slots, pairs = slots or spec["slots"], pairs or spec["pairs"]
    base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)

    def undo():
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    field = _place_field(ctx, gd, ore, slots, target)
    if field is None:
        return None
    ctx.add(field.ents)
    fp = field.ports["ore"]
    vx, vy = DVEC[fp.direction]
    head = (fp.x + vx * 3, fp.y + vy * 3)
    col = _column_part(gd, ore, pairs)
    coal_drops = [c[3][-1] for c in tap_candidates(ctx, items_at, "coal")]
    CO = np.array(coal_drops, float) if coal_drops else None

    def score(rp, X, Y, anchor):
        po, pc = rp.ports["ore"], rp.ports["coal"]
        sc = np.abs(X + po.x - anchor[0]) + np.abs(Y + po.y - anchor[1])
        if CO is not None:
            sc = sc + 0.5 * np.min(np.abs((X + pc.x)[:, None] - CO[None, :, 0]) +
                                   np.abs((Y + pc.y)[:, None] - CO[None, :, 1]), 1)
        if goal is not None:
            pw = rp.ports["plate"]
            sc = sc + 0.7 * (np.abs(X + pw.x - goal.x) + np.abs(Y + pw.y - goal.y))
        return sc
    spots = []
    # the column at the field's head (smelting at the patch); an outpost with no coal in reach sends its ORE to a
    # column next to the goal instead (one long ore belt: the coal is at the base)
    anchors = [head] + ([(goal.x, goal.y)] if goal is not None else [])
    coal_far = CO is not None and float(np.min(np.abs(CO[:, 0] - head[0]) + np.abs(CO[:, 1] - head[1]))) > 60
    if coal_far and goal is not None:
        anchors.reverse()         # no coal near the patch: one long ore trunk to a column at the base
    for anchor in anchors:
        win = (int(anchor[0]) - 40, int(anchor[1]) - 40, int(anchor[0]) + 40, int(anchor[1]) + 40)
        spots += _spots(ctx, col, lambda rp, X, Y, anchor=anchor: score(rp, X, Y, anchor), win, n=4)
    for _sc, _m, _k, placed in spots:
        b2, nn, no = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(placed.ents)
        for p in placed.ports.values():
            ctx.grid.release([(p.x, p.y)])
        got = None
        ore_route = _route(ctx, Port("belt_out", ore, fp.x, fp.y, fp.direction), placed.ports["ore"])
        if ore_route is not None and max_plate is not None and len(ore_route) > max_plate:
            ore_route = None          # the long leg is the ore belt here (column at the goal): same cap
        if ore_route is not None:
            ctx.add([PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role="feed", item=ore))
                     for e in ore_route])
            coal = _feed(ctx, items_at, "coal", placed.ports["coal"], max_len=CELL_COAL_MAX, allow_cell=False)
            if coal is not None:
                pp = placed.ports["plate"]
                if goal is not None:
                    r = _route(ctx, Port("belt_out", pp.item, pp.x, pp.y, pp.direction), goal)
                    if r is not None and max_plate is not None and len(r) > max_plate:
                        r = None
                    plate = None if r is None else [PE(e.name, e.x, e.y, e.direction, e.recipe,
                                                       dict(_x(e), role="feed", item=pp.item)) for e in r]
                else:
                    plate = _depot(ctx, pp, pp.item)
                if plate is not None:
                    ctx.add(plate)
                    got = (coal, plate)
        if got is not None:
            coal, plate = got
            ents = ctx.new[n_new:]
            chests = [[e.x, e.y] for e in ents if _x(e).get("role") == "depot"]
            return ents, {"item": ore.replace("-ore", "-plate"), "cell": ore, "slots": len(
                [e for e in field.ents if _x(e).get("role") == "drill_e"]), "pairs": pairs,
                "belt_tiles": len(ore_route) + coal[1]["belt_tiles"] + (len(plate) if goal else 0),
                "chests": chests, "coal_tap": coal[1]["tap"],
                "zones": [M._ents_rect(gd, field.ents), M._ents_rect(gd, placed.ents)]}
        ctx.grid = b2
        ctx.new, ctx.others = ctx.new[:nn], ctx.others[:no]
    undo()
    return None


def pole_for(mp, gd) -> str:
    """Medium poles once the master makes steel (its depot is a helper source) and they are researched: a 7x7 supply
    area instead of 5x5, so about half as many poles, and no wood (small poles are hand-made: wood + cables)."""
    if "steel" in templates_built(mp) and gd.can_build("medium-electric-pole"):
        return "medium-electric-pole"
    return "small-electric-pole"


def _poles(ctx: _Ctx, k):
    """Poles so every new electric entity is covered and joined to the master's network (its poles alive at k)."""
    gd, mp = ctx.gd, ctx.mp
    own = [e for e in ctx.new if gd.entity(e.name).get("type") == "electric-pole"]
    master_poles = [PE(e.name, e.x, e.y, 0) for e in mp.ents if M.alive(e, k)
                    and gd.entity(e.name).get("type") == "electric-pole"]
    need = [e for e in ctx.new if gd.entity(e.name).get("type") != "electric-pole"]
    pole = pole_for(mp, gd)
    try:
        poles = place_poles(ctx.grid, gd, need, pole, sources=master_poles + own, tag="pole")
    except NoRoute as e:
        # reserved room (not entities) next to the machines without a pole spot may take a pole after all
        g = ctx.grid
        free = []
        for b in (e.detail or []):
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    t = (math.floor(b.x) + dx, math.floor(b.y) + dy)
                    if str(g.tag_at(*t) or "").startswith("zone:"):
                        free.append(t)
        g.release(free)
        try:
            poles = place_poles(ctx.grid, gd, need, pole, sources=master_poles + own, tag="pole")
        except NoRoute as e2:
            bad = ", ".join(f"{b.name} at {b.x},{b.y}" for b in (e2.detail or [])[:3])
            raise ExpandError(f"no pole layout for the expansion: {e2} ({bad})") from e2
    for p in poles:
        p.extra = {"role": "exp_pole"}
    ctx.new += poles
    ctx.others += poles
    return poles


# ---- placing a block ---------------------------------------------------------------------------------------------------
def _orientations(part: M.Part, mirror_ok=True):
    for mirror in ((False, True) if mirror_ok else (False,)):
        p = part.mirrored() if mirror else part
        for k in range(4):
            yield mirror, k, p.rotated(k)


def _spots(ctx: _Ctx, part: M.Part, score, window, n=SPOT_TRIES, avoid_ore=3.0, mirror_ok=True, shape=None):
    """Best placements of `part` (all 8 orientations): [(score, mirror, k, placed Part)], the block plus GAP tiles
    around it and its reserve rects on placeable ground. shape: the local rects (canonical orientation, no mirror)
    that hold the block's entities when it is not a full rectangle (the robot block: its row and the fluid section
    above one end of it); only they (plus GAP) need free ground."""
    ok, ore = ctx.sums()
    g = ctx.grid
    wx0, wy0, wx1, wy1 = window
    found = []
    for mirror, k, rp in _orientations(part, mirror_ok):
        w, h = rp.size
        xs = np.arange(max(wx0, g.x0 + GAP), min(wx1, g.x0 + g.w - GAP) - w + 1)
        ys = np.arange(max(wy0, g.y0 + GAP), min(wy1, g.y0 + g.h - GAP) - h + 1)
        if not len(xs) or not len(ys):
            continue
        X, Y = np.meshgrid(xs, ys)
        X, Y = X.ravel(), Y.ravel()
        if shape and not mirror:
            good = np.ones(len(X), bool)
            for r in shape:
                r0, r1, r2, r3 = M._rot_rect(r, part.size, k)
                good &= ok.count(X, Y, (r0 - GAP, r1 - GAP, r2 + GAP, r3 + GAP)) == \
                    (r2 - r0 + 2 * GAP) * (r3 - r1 + 2 * GAP)
        else:
            good = ok.count(X, Y, (-GAP, -GAP, w + GAP, h + GAP)) == (w + 2 * GAP) * (h + 2 * GAP)
        for _z, rr in rp.reserve:
            good &= ok.count(X, Y, rr) == (rr[2] - rr[0]) * (rr[3] - rr[1])
        for p in rp.ports.values():      # every port and its approach lane on free ground
            if p.kind not in ("belt_in", "belt_out"):
                continue
            vx, vy = DVEC[p.direction]
            if p.kind == "belt_in":
                vx, vy = -vx, -vy
            for i in range(0, 3):
                tx, ty = p.x + vx * i, p.y + vy * i
                if 0 <= tx < w and 0 <= ty < h:
                    continue
                good &= ok.count(X, Y, (tx, ty, tx + 1, ty + 1)) == 1
        if not good.any():
            continue
        X, Y = X[good], Y[good]
        sc = score(rp, X, Y).astype(float) + avoid_ore * ore.count(X, Y, (0, 0, w, h))
        order = np.argsort(sc)[: n * 3]
        for i in order:
            found.append((float(sc[i]), mirror, k, rp.at(int(X[i]), int(Y[i]))))
    found.sort(key=lambda f: f[0])
    out = []
    for f in found:       # spread: the alternatives are at least 8 tiles apart
        c = f[3]
        if all(abs(c.ents[0].x - o[3].ents[0].x) + abs(c.ents[0].y - o[3].ents[0].y) >= 8 for o in out):
            out.append(f)
        if len(out) >= n:
            break
    return out


def _stage_index(mp, template, after=None) -> float:
    """The mall (S4m) and urgent power growth (S4p: machines short of power) go right after the stage being built
    when later starter stages still wait; everything else after the last stage."""
    idx = [s.index for s in mp.stages]
    if template in ("mall", "power_grow") and after is not None and any(i > after for i in idx):
        k = int(after) + (0.7 if template == "mall" else 0.6)
        while k in idx:
            k += 0.05
        return round(k, 2)
    return float(int(max(idx)) + 1) if idx else 6.0


def _append_stage(mp, k, template, gate, after, unlocks, ents, zone_rect, label):
    zid = f"x_{template}" if not any(z.id == f"x_{template}" for z in mp.zones) else f"x_{template}_{int(k * 10)}"
    n0 = len(mp.ents)
    rects = [] if zone_rect is None else (zone_rect if isinstance(zone_rect[0], (list, tuple)) else [zone_rect])
    # an entry ("bus", rect) is the plate bus's corridor: later expansions put their taps in its tap rows
    kinds = [r[0] if isinstance(r[0], str) else template for r in rects]
    rects = [r[1] if isinstance(r[0], str) else r for r in rects]
    zids = [zid if i == 0 else f"{zid}_{i}" for i in range(len(rects))]

    def zone_of(e):     # an entity belongs to the zone it stands in (feeds and taps outside: the first zone's id)
        for z, r in zip(zids, rects):
            if r[0] <= e.x < r[2] and r[1] <= e.y < r[3]:
                return z
        return zid
    for i, e in enumerate(ents):
        x = dict(_x(e))
        x.update({"id": f"x{n0 + i}", "zone": zone_of(e), "stage": k, "until": None})
        mp.ents.append(PE(e.name, e.x, e.y, e.direction, e.recipe, x))
    for z, r, kind in zip(zids, rects, kinds):
        mp.zones.append(M.Zone(z, kind, tuple(int(v) for v in r), k, label if kind != "bus" else
                               "Plate bus: iron and copper, room for 7 more lanes", []))
    key = STAGE_KEYS[template] if not any(s.key == STAGE_KEYS[template] for s in mp.stages) else \
        f"{STAGE_KEYS[template]}_{int(k * 10)}"
    mp.stages.append(M.StageSpec(k, key, TITLES[template], list(gate), after, unlocks))
    mp.stages.sort(key=lambda s: s.index)
    mp.stats.setdefault("expansions", []).append({"template": template, "stage": k, "zone": zid,
                                                  "entities": len(ents)})


def _rollback(mp, snap):
    mp.ents, mp.zones, mp.stages, mp.stats = snap


def _validate(mp, gd, world, k, template):
    """The base after the new stage validates, apart from what was already wrong before it: a live map drifts from
    the plan (ore under a starter drill mined out, a belt the player changed) and that is not the expansion's fault
    (round 2 run: a depleted starter-field tile refused every later expansion)."""
    rep = M.validate_stage(mp, gd, world, k)
    errs = [i for i in rep.errors if i.kind not in ("research",)]
    if errs:
        prev = max((s.index for s in mp.stages if s.index < k), default=None)
        if prev is not None:
            old = {(i.kind, i.msg) for i in M.validate_stage(mp, gd, world, prev).errors}
            errs = [i for i in errs if (i.kind, i.msg) not in old]
    if errs:
        raise ExpandError(f"{template}: the base after the new stage does not validate: "
                          + "; ".join(f"{i.kind}: {i.msg}" for i in errs[:4]))
    return rep


def _last_alive_stage(mp):
    return max(s.index for s in mp.stages)


# ---- the plate bus (doctrine ORG-4, SCALE-2): every expansion block hangs off it ----------------------------------------
BUS_LEN = 96                    # round 3: 128 left ~60 tiles of empty bus (256 hand-made belts) for years
BUS_LANES = (("iron-plate", 0), ("copper-plate", 1))
# depot chests are limited (inventory bar, in stacks): an unlimited chest at a belt's end takes everything that passes,
# so the belt never backs up and its taps on the starter base's lines run flat out (round 3 smoke run: red science
# fell from 30 to 3/min while the bus end depots and the steel depot filled)
DEPOT_BAR = 2
STEEL_BAR = 4
BUS_RESERVE = (-3, 9)          # rows of the bus corridor kept free: tap rows north (-1..-3), 7 more lanes south
TAP_ROWS = (-3, 0)             # rows released for the taps and their first belt tiles


def _bus_part(gd, length=BUS_LEN):
    """The expansion bus, canonical flowing EAST: iron plates on row 0, copper plates on row 1 (x 0..length-1), a
    depot chest at each end (plates for the helper), head ports at (-1, 0) / (-1, 1). Its corridor (rows -4..8) is
    reserved: the tap rows on the north side and room for 7 more lanes on the south (bus_lane_plan: iron x4, copper
    x4, circuits, steel...)."""
    ents = []
    for item, row in BUS_LANES:
        ents += [B._put(gd, "transport-belt", x, row, EAST, extra=B._r("bus", row=x, item=item))
                 for x in range(length)]
        ents.append(B._put(gd, "inserter", length, row, WEST, extra=B._r("depot_in", item=item)))
        ents.append(B._put(gd, "iron-chest", length + 1, row, extra=B._r("depot", item=item, holds=0, bar=DEPOT_BAR)))
    ents.append(B._put(gd, "small-electric-pole", length, 2, extra=B._r("exp_pole")))
    ports = {"iron": Port("belt_in", "iron-plate", -1, 0, EAST), "copper": Port("belt_in", "copper-plate", -1, 1, EAST)}
    return M.Part("bus", ents, (length + 2, 2), ports, [("bus", (-1, BUS_RESERVE[0], length + 2, BUS_RESERVE[1]))])


def bus_items(mp) -> dict:
    """items_at (belt_items form) for the bus belts of the master: {tile: (name, dir, {item}, 0)}."""
    out = {}
    for e in mp.ents:
        x = _x(e)
        if x.get("role") == "bus" and e.name == "transport-belt":
            out[(math.floor(e.x), math.floor(e.y))] = (e.name, int(e.direction or 0), {x.get("item")}, 0)
    return out


def _bus_rows(ctx: _Ctx, bus_ents, release=True):
    """Release (or occupy again) the tap rows beside the bus: free ground for taps and their first belt tiles."""
    if not bus_ents:
        return
    belts = [e for e in bus_ents if _x(e).get("role") == "bus"]
    rows = {}
    for e in belts:
        rows.setdefault(_x(e).get("item"), []).append(e)
    iron = rows.get("iron-plate") or belts
    cop = rows.get("copper-plate") or []
    d = int(iron[0].direction or 0)
    # the side away from the copper belt is the tap side
    if cop:
        nx = math.floor(iron[0].x) - math.floor(cop[0].x)
        ny = math.floor(iron[0].y) - math.floor(cop[0].y)
    else:
        nx, ny = DVEC[(d + 12) % 16]
    tiles = []
    for e in iron:
        bx, by = math.floor(e.x), math.floor(e.y)
        for i in range(1, 4):
            tiles.append((bx + nx * i, by + ny * i))
    g = ctx.grid
    if release:
        g.release([t for t in tiles if str(g.tag_at(*t) or "").startswith("zone:")])
    else:
        g.occupy([t for t in tiles if g.state_at(*t) != BLOCKED], "zone:bus")


def _place_bus(mp, world, gd, ctx: _Ctx, hub):
    part = _bus_part(gd)
    pts = {}
    for ore in ("iron-ore", "copper-ore"):
        ps = sorted(world.patches(min_tiles=40, name=ore),
                    key=lambda p: abs(p.center[0] - hub[0]) + abs(p.center[1] - hub[1]))[:2]
        cand = []
        for p in ps:
            out = sorted(p.outline() or p.tiles, key=lambda t: abs(t[0] - hub[0]) + abs(t[1] - hub[1]))
            cand += [(t[0], t[1]) for t in out[:80:5]]
        pts[ore] = np.array(cand, float) if cand else None
    if pts["iron-ore"] is None or pts["copper-ore"] is None:
        raise ExpandError("bus: no iron or copper ore patch in reach for its smelting cells")

    def score(rp, X, Y):
        pi = rp.ports["iron"]
        sc = np.zeros(len(X))
        for ore in ("iron-ore", "copper-ore"):
            P = pts[ore]
            sc = sc + np.min(np.abs((X + pi.x)[:, None] - P[None, :, 0]) +
                             np.abs((Y + pi.y)[:, None] - P[None, :, 1]), 1)
        cx, cy = X + rp.size[0] / 2, Y + rp.size[1] / 2
        return sc + 0.5 * (np.abs(cx - hub[0]) + np.abs(cy - hub[1]))
    spots = [s for s in _spots(ctx, part, score, ctx.rect, n=SPOT_TRIES, avoid_ore=4.0) if not s[1]]
    if not spots:
        raise ExpandError("bus: no free straight corridor for the plate bus near the base")
    return spots


class _guarded:
    """While one port of a block is fed, the block's OTHER port tiles (and the tile straight out of each) are
    blocked: a route must never end on, or side-load into, another input's belt. kinds: the port kinds guarded (the
    blue block guards its pipe ports too: a belt over a pipe port leaves the pipe no way in)."""

    def __init__(self, ctx: _Ctx, ports: dict, keep, kinds=("belt_in", "belt_out")):
        self.ctx, self.tiles = ctx, []
        for label, p in ports.items():
            if label == keep or p.kind not in kinds:
                continue
            vx, vy = DVEC[p.direction]
            if p.kind in ("belt_in", "pipe_in"):
                vx, vy = -vx, -vy
            for i in range(2):
                t = (p.x + vx * i, p.y + vy * i)
                if ctx.grid.in_bounds(*t) and ctx.grid.state_at(*t) != BLOCKED:
                    self.tiles.append(t)

    def __enter__(self):
        self.ctx.grid.occupy(self.tiles, "guard:port")
        return self

    def __exit__(self, *exc):
        g = self.ctx.grid       # _feed may have swapped the grid object: release on the current one
        g.release([t for t in self.tiles if g.tag_at(*t) == "guard:port"])
        return False


def _occupy_rect(ctx: _Ctx, rect, tag):
    x0, y0, x1, y1 = rect
    g = ctx.grid
    g.occupy([(x, y) for x in range(x0, x1) for y in range(y0, y1) if g.in_bounds(x, y) and g.state_at(x, y) != BLOCKED],
             tag)


def _attach(ctx: _Ctx, part: M.Part, items_at, needs: dict, near, tag, n=SPOT_TRIES, mirror_ok=True, bus_ents=None,
            n_ins=None, clear=6):
    """Place `part` beside the bus and feed each of its ports by a tap. needs: {port label: item} (taps on
    `items_at`, the bus) or {port label: (item, items_at of its own, max belt tiles)} (e.g. coal from the starter
    base's coal belts). n_ins: {item: tap inserters} (default 2 for iron plates, else 1); clear: open tiles wanted
    beyond a tap's drop on `items_at` (a bus stretch where no block stands yet). -> (score, placed, feeds) or raises
    ExpandError."""
    n_ins = n_ins or {"iron-plate": 2}
    spec = {}
    for label, v in needs.items():
        if isinstance(v, tuple):
            spec[label] = (v[0], v[1], v[2] if len(v) > 2 else 60)
        else:
            spec[label] = (v, items_at, 60)
    drops = {}
    for label, (item, ia, _ml) in spec.items():
        cands = tap_candidates(ctx, ia, item, clear=clear if ia is items_at else 0)
        if not cands and ia is items_at:        # no open bus stretch left: any free tap spot beside it
            cands = tap_candidates(ctx, ia, item, clear=min(clear, 2))
        drops[label] = np.array([c[3][-1] for c in cands], float)
        if not len(drops[label]):
            raise ExpandError(f"{tag}: no free tap spot for {item}")

    def score(rp, X, Y):
        sc = np.zeros(len(X))
        for label in spec:
            p, P = rp.ports[label], drops[label]
            # the bus feeds weigh full, a feed from elsewhere (coal) less: the block sits at the bus
            w8 = 1.0 if spec[label][1] is items_at else 0.3
            sc = sc + w8 * np.min(np.abs((X + p.x)[:, None] - P[None, :, 0]) + np.abs((Y + p.y)[:, None] - P[None, :, 1]), 1)
        cx, cy = X + rp.size[0] / 2, Y + rp.size[1] / 2
        return sc + 0.1 * (np.abs(cx - near[0]) + np.abs(cy - near[1]))
    first = list(spec)[0]           # the window: around the first need's taps (the bus)
    d0 = drops[first]
    win = (int(d0[:, 0].min()) - 70, int(d0[:, 1].min()) - 70, int(d0[:, 0].max()) + 70, int(d0[:, 1].max()) + 70)
    reasons = []
    if bus_ents:
        _bus_rows(ctx, bus_ents, release=False)        # no block on the bus's tap rows
    spots = _spots(ctx, part, score, win, n=n, mirror_ok=mirror_ok)
    if bus_ents:
        _bus_rows(ctx, bus_ents, release=True)
    for sc, mirror, _k, placed in spots:
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(placed.ents)
        for _z, rr in placed.reserve:
            _occupy_rect(ctx, rr, f"zone:{tag}")
        for p in placed.ports.values():      # the port and its neighbours stay routable (side-loads arrive there)
            near_p = [(p.x + dx, p.y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
            ctx.grid.release([t for t in near_p if str(ctx.grid.tag_at(*t) or "").startswith("zone:")] +
                             [(p.x, p.y)])
        feeds = []
        for label, (item, ia, max_len) in spec.items():
            with _guarded(ctx, placed.ports, label):
                f = _tap_feed(ctx, ia, item, placed.ports[label], n_ins=n_ins.get(item, 1),
                              max_len=max_len, clear=min(clear, 2) if ia is items_at else 0)
            if f is None:
                break
            feeds.append(f)
        if len(feeds) == len(spec):
            return sc, placed, feeds
        reasons.append(f"{placed.ents[0].x:.0f},{placed.ents[0].y:.0f}: no tap route for "
                       f"{list(spec.values())[len(feeds)][0]}")
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError(f"{tag}: " + ("; ".join(reasons[:3]) or "no free ground beside the bus"))


# ---- templates -------------------------------------------------------------------------------------------------------
MALL_FEED_MAX = 80              # belt tiles of a mall feed from a tap on the starter base's plate lines
MALL_TAPS = {"iron-plate": 3, "copper-plate": 1}     # tap inserters per mall input (a basic inserter moves ~50/min)
MALL_BUS_SHARE = 1 / 3          # a mall on the bus taps the bus's upstream third: it gets the first pick of the plates


def _bus_ents(mp) -> list:
    return [e for e in mp.ents if _x(e).get("role") == "bus"]


def _bus_rows_of(bus_ents) -> dict:
    """{tile: bus row} (rows count from the bus head) of the bus belts."""
    return {(math.floor(e.x), math.floor(e.y)): int(_x(e).get("row", 0)) for e in bus_ents
            if _x(e).get("role") == "bus" and e.name == "transport-belt"}


def bus_slice(items_at: dict, bus_ents, lo=None, hi=None) -> dict:
    """The part of a bus's items_at from bus row lo (incl.) to hi (excl.)."""
    rows = _bus_rows_of(bus_ents)
    return {t: v for t, v in items_at.items() if t in rows and (lo is None or rows[t] >= lo) and
            (hi is None or rows[t] < hi)}


def mall_tap_rows(mp, bus_ents) -> list:
    """Bus rows the mall stage's tap inserters take from (empty when the mall is not on this bus)."""
    rows = _bus_rows_of(bus_ents)
    mall_k = templates_built(mp).get("mall")
    out = []
    if mall_k is None:
        return out
    for e in mp.ents:
        x = _x(e)
        if x.get("role") != "tap" or x.get("stage") != mall_k or "inserter" not in e.name:
            continue
        dx, dy = DVEC[int(e.direction or 0) % 16]          # a tap inserter faces its pickup (the bus belt)
        r = 2 if e.name == "long-handed-inserter" else 1
        t = (math.floor(e.x) + r * dx, math.floor(e.y) + r * dy)
        if t in rows:
            out.append(rows[t])
    return out


def _after_mall(mp, bus_ents):
    """First bus row downstream of the mall's taps (None: the mall does not take from this bus)."""
    rows = mall_tap_rows(mp, bus_ents)
    return max(rows) + 2 if rows else None


def _new_bus(mp, world, gd, ctx: _Ctx, hub, items_at, then=None):
    """The plate bus (doctrine ORG-4): two straight belts, iron and copper, fed at the head by a smelting cell per
    metal at its ore patch (electric drills, a column at the field's head, coal from a tap) - or, with no cell room,
    by a tap on the starter base's plate line. then(bus_ents) -> anything: the block that hangs off the new bus
    (raises ExpandError to try the next bus spot). -> (bus entities, cell feeds, zones, then's result)."""
    reasons = []
    for _sc, _m, _k, bus in _place_bus(mp, world, gd, ctx, hub):
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(bus.ents)
        bus_rect = bus.reserve[0][1]
        _occupy_rect(ctx, bus_rect, "zone:bus")
        for p in bus.ports.values():
            ctx.grid.release([(p.x, p.y)])
        feeds = []
        for label, ore in (("iron", "iron-ore"), ("copper", "copper-ore")):
            goal = bus.ports[label]
            with _guarded(ctx, bus.ports, label):
                f = _cell(ctx, items_at, ore, (goal.x, goal.y), goal, max_plate=CELL_FEED_MAX) or \
                    _tap_feed(ctx, items_at, ore.replace("-ore", "-plate"), goal, n_ins=3, max_len=120)
            if f is None:
                break
            feeds.append(f)
        where = f"bus at {bus.ents[0].x:.0f},{bus.ents[0].y:.0f}"
        if len(feeds) == 2:
            zones = [("bus", tuple(bus_rect))] + [z for f in feeds for z in f[1].get("zones") or []]
            if then is None:
                return bus.ents, [f[1] for f in feeds], zones, None
            try:
                got = then(bus.ents)
                return bus.ents, [f[1] for f in feeds], zones, got
            except ExpandError as e:
                reasons.append(f"{where}: {e}")
        else:
            reasons.append(f"{where}: no smelting cell or tap for {['iron', 'copper'][len(feeds)]}")
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError("bus: " + ("; ".join(reasons[:3]) or "no free straight corridor near the base"))


def _on_bus(mp, world, gd, ctx: _Ctx, hub, items_at, attach):
    """attach(bus_ents) on the master's plate bus, or on a new one (laid with its smelting cells in this stage) when
    the master has none. -> (attach's result, new cell feeds, new zones)."""
    bus_ents = _bus_ents(mp)
    if bus_ents:
        return attach(bus_ents), [], []
    _b, cells, zones, got = _new_bus(mp, world, gd, ctx, hub, items_at, then=attach)
    return got, cells, zones


def _mall(mp, world, gd, ctx: _Ctx, k, researched):
    """The mall row (blocks.mall_block) with a dedicated plate supply. In order of preference:
      1. the master has a plate bus: the mall taps its upstream third (first pick, before steel and science);
      2. taps on the starter base's own iron and copper plate lines near the hub (a short feed, nothing else to build
         first; the starter base's electric drills give ~450 iron/min against ~225 for 30 red + 30 green science);
      3. a new plate bus with its own smelting cells, the mall on it.
    Round 3: the mall used to always come with its bus and cells - 650 to 915 entities, built by hand before any of
    it worked, so on the real-save copy the mall came online in the run's last minutes."""
    items = B.mall_block(gd, None).info["items"]
    done = set(researched or ())
    want = [i for i in items if B.MALL_GATE.get(i) is None or B.MALL_GATE[i] in done]
    blk = B.mall_block(gd, want)
    mall = M._part(blk, ["iron", "copper"], blk.info["reserve"])
    alive = [e for e in mp.ents if M.alive(e, k)]
    items_at = belt_items(alive, gd, M.world_without_master(world, mp, k))
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))

    def result(placed, feeds, zones=(), supply="starter"):
        chests = [[e.x, e.y] for e in ctx.new if _x(e).get("role") in ("mall_chest", "depot")]
        mall_zone = M._rect_union([M._ents_rect(gd, placed.ents)] + [rr for _z, rr in placed.reserve])
        return {"ents": list(ctx.new), "zone": [mall_zone] + list(zones), "feeds": feeds, "chests": chests,
                "items": blk.info["items"], "supply": supply}

    def on_bus(bus_ents):
        bus = bus_items_from(bus_ents)
        n_rows = len(_bus_rows_of(bus_ents)) // len(BUS_LANES)
        up = bus_slice(bus, bus_ents, hi=max(8, int(n_rows * MALL_BUS_SHARE)))
        _bus_rows(ctx, bus_ents, release=True)
        try:
            got = _attach(ctx, mall, up or bus, {"iron": "iron-plate", "copper": "copper-plate"}, hub, "x_mall",
                          bus_ents=bus_ents, n_ins=MALL_TAPS)
        finally:
            _bus_rows(ctx, bus_ents, release=False)
        return got

    reasons = []
    if _bus_ents(mp):
        _sc, placed, taps = on_bus(_bus_ents(mp))
        return result(placed, [t[1] for t in taps], supply="bus")
    try:
        _sc, placed, taps = _attach(ctx, mall, items_at, {"iron": ("iron-plate", items_at, MALL_FEED_MAX),
                                                          "copper": ("copper-plate", items_at, MALL_FEED_MAX)},
                                    hub, "x_mall", n=10, n_ins=MALL_TAPS, clear=0)
        return result(placed, [t[1] for t in taps])
    except ExpandError as e:
        reasons.append(f"starter plate taps: {e}")
    try:
        _b, cells, zones, (_sc, placed, taps) = _new_bus(mp, world, gd, ctx, hub, items_at, then=on_bus)
    except ExpandError as e:
        raise ExpandError("mall: " + "; ".join(reasons + [str(e)])[:600]) from e
    return result(placed, cells + [t[1] for t in taps], zones, supply="new bus")


def mall_items(mp) -> set:
    """Items the master's mall machines make (the mall row's mall_am units, any stage)."""
    return {_x(e).get("unit") for e in mp.ents if _x(e).get("role") == "mall_am" and _x(e).get("unit")}


def _mall_anchor(mp, mall_k):
    """The mall row's copper cable machine at its head (one per row): where the row's layout is pinned."""
    return next((e for e in mp.ents if _x(e).get("stage") == mall_k and _x(e).get("role") == "head_am"
                 and _x(e).get("unit") == "copper-cable"), None)


def _mall_part(mp, gd, mall_k):
    """The mall row as the master placed it, re-derived from blocks.mall_block: (orientation, offset, built items)
    such that mall_block(gd, items) in that orientation at that offset is exactly the mall stage's row."""
    items = sorted((i for i in mall_items(mp) if i in B.MALL_SLOTS), key=B.MALL_SLOTS.index)
    anchor = _mall_anchor(mp, mall_k)
    if anchor is None or not items:
        raise ExpandError("mall_add: the master has no mall row")
    have = {(e.name, e.x, e.y, int(e.direction or 0)) for e in mp.ents}
    blk = B.mall_block(gd, items)
    part = M._part(blk, ["iron", "copper"], blk.info["reserve"])
    for mirror, k, rp in _orientations(part):
        a = next(e for e in rp.ents if _x(e).get("role") == "head_am" and _x(e).get("unit") == "copper-cable")
        ox, oy = anchor.x - a.x, anchor.y - a.y
        if abs(ox - round(ox)) > 1e-6 or abs(oy - round(oy)) > 1e-6:
            continue
        keys = {(e.name, e.x + ox, e.y + oy, int(e.direction or 0)) for e in rp.ents}
        if keys <= have:            # (later mall_add stages' machines are part of the row too)
            return (mirror, k), (ox, oy), items
    raise ExpandError("mall_add: cannot re-derive the mall row's layout")


def _mall_add(mp, world, gd, ctx: _Ctx, k, researched):
    """Defence v2: the mall row gets the items whose research came after it was built (firearm magazines once Gun
    turrets is researched, fast inserters, ...): their machines go into the row's empty slots / its reserved room at
    the east end, belt A (and belt B) run on to them. Nothing of the row moves: the slots before keep their place
    (blocks.MALL_SLOTS only ever grows at its end)."""
    mall_k = templates_built(mp).get("mall")
    if mall_k is None:
        raise ExpandError("mall_add: the master has no mall")
    done = set(researched or ())
    (mirror, rot), _off, items = _mall_part(mp, gd, mall_k)
    add = [i for i, t in B.MALL_GATE.items() if t in done and i in B.MALL_SLOTS and i not in items]
    anchor = _mall_anchor(mp, mall_k)
    have = {(e.name, e.x, e.y, int(e.direction or 0)) for e in mp.ents}
    why = ""
    while True:
        want = [i for i in B.MALL_SLOTS if i in items or i in add]
        blk = B.mall_block(gd, want)
        add = [i for i in add if i in blk.info["items"]]       # (an item whose direct-insertion neighbour is missing)
        if not add:
            raise ExpandError("mall_add: nothing new for the mall" + (f" ({why})" if why else ""))
        rp = M._part(blk, ["iron", "copper"], blk.info["reserve"])
        rp = (rp.mirrored() if mirror else rp).rotated(rot)
        a = next(e for e in rp.ents if _x(e).get("role") == "head_am" and _x(e).get("unit") == "copper-cable")
        placed = rp.at(anchor.x - a.x, anchor.y - a.y)
        new = [e for e in placed.ents if (e.name, e.x, e.y, int(e.direction or 0)) not in have]
        blocked = [e for e in new if any(not ctx.free(t) for t in _tiles(gd, e))]
        if not blocked:
            break
        # an empty slot in the middle of the row may hold a pole the mall stage needed (2002 onsite: the fast
        # inserter's slot): that item waits, the others still go in
        slots = blk.info["slots"]
        bad = set()
        for e in blocked:
            u = _x(e).get("unit")
            if u in add:
                bad.add(u)
            elif isinstance(u, (int, float)):
                bad |= {i for i in add if slots[i] <= u <= slots[i] + 3}
        bad = bad or {add[-1]}
        e = blocked[0]
        why = f"{e.name} at {e.x},{e.y} is blocked"
        add = [i for i in add if i not in bad]
    ctx.add(new)
    gate = sorted({B.MALL_GATE[i] for i in add})
    chests = [[e.x, e.y] for e in new if _x(e).get("role") in ("mall_chest", "depot")]
    return {"ents": list(ctx.new), "zone": None, "feeds": [], "chests": chests, "items": add, "gate": gate}


def bus_items_from(ents) -> dict:
    out = {}
    for e in ents:
        x = _x(e)
        if x.get("role") == "bus" and e.name == "transport-belt":
            out[(math.floor(e.x), math.floor(e.y))] = (e.name, int(e.direction or 0), {x.get("item")}, 0)
    return out


def _power_part(mp, gd):
    """The master's power block re-derived at its full reserved size: (placed canonical entities with their unit
    index, units already in the master)."""
    pw = [e for e in mp.ents if _x(e).get("zone") == "power" and _x(e).get("role") in
          ("boiler", "engine", "boiler_ins", "pipe", "power_pole", "boiler_belt")]
    boilers = {_x(e)["unit"]: e for e in pw if _x(e).get("role") == "boiler"}
    if 0 not in boilers:
        raise ExpandError("power_grow: the master has no power block")
    have = {(e.name, e.x, e.y, int(e.direction or 0)) for e in pw}
    n_res = int((mp.power or {}).get("boilers_reserved") or M.POWER_RESERVE)
    n_res = min(max(n_res, len(boilers)), M.POWER_RESERVE)
    for end in ("water", "far"):
        canon = B.power_block(n_res, gd, coal_end=end)
        for blk in (canon, B.mirror_y(canon)):
            for kk in range(4):
                rb = blk.rotated(kk)
                b0 = next(e for e in rb.entities if _x(e).get("role") == "boiler" and _x(e).get("unit") == 0)
                ox, oy = boilers[0].x - b0.x, boilers[0].y - b0.y
                if abs(ox - round(ox)) > 1e-6 or abs(oy - round(oy)) > 1e-6:
                    continue
                placed = [e.moved(ox, oy) for e in rb.entities]
                keys = {(e.name, e.x, e.y, int(e.direction or 0)) for e in placed}
                if have <= keys:
                    return placed, set(boilers)
    raise ExpandError("power_grow: cannot re-derive the power block's layout")


def _power_grow(mp, world, gd, ctx: _Ctx, k, researched, units=2):
    placed, built = _power_part(mp, gd)
    all_units = sorted({_x(e).get("unit") for e in placed if _x(e).get("role") == "boiler"})
    add_units = [u for u in all_units if u not in built][:units]
    if not add_units:
        raise ExpandError("power_grow: the power block's reserved room is used up")
    have = {(e.name, e.x, e.y) for e in mp.ents}
    pick = {}
    belts = [e for e in placed if _x(e).get("role") == "boiler_belt"]
    # the belt tiles from the coal port up to the new units' inserters
    ins = [e for e in placed if _x(e).get("role") == "boiler_ins"]
    for e in ins:
        dx, dy = DVEC[e.direction]
        pick[_x(e)["unit"]] = (math.floor(e.x + dx), math.floor(e.y + dy))
    order = sorted(belts, key=lambda b: _x(b).get("row", 0))
    coal_in = [b for b in belts if (b.name, b.x, b.y) in have]
    # which end of the belt row is fed: the end next to a built belt
    first_built = next((i for i, b in enumerate(order) if (b.name, b.x, b.y) in have), 0)
    rev_order = first_built > len(order) // 2
    seq = list(reversed(order)) if rev_order else order
    need_upto = max(next((i for i, b in enumerate(seq) if (math.floor(b.x), math.floor(b.y)) == pick[u]), 0)
                    for u in add_units)
    new = [e for e in placed if _x(e).get("unit") in add_units and (e.name, e.x, e.y) not in have
           and _x(e).get("role") != "boiler_belt"]
    new += [b for i, b in enumerate(seq) if i <= need_upto and (b.name, b.x, b.y) not in have]
    if not coal_in:
        raise ExpandError("power_grow: the boiler coal belt is not part of the master")
    blocked = [e for e in new if any(not ctx.grid.in_bounds(*t) or (ctx.grid.state_at(*t) == BLOCKED and
                                     not str(ctx.grid.tag_at(*t) or "").startswith("zone:power"))
                                     for t in _tiles(gd, e))]
    if blocked:
        raise ExpandError(f"power_grow: {len(blocked)} entities of the reserved units are blocked (e.g. "
                          f"{blocked[0].name} at {blocked[0].x}, {blocked[0].y})")
    for e in new:
        e.extra = dict(_x(e), chain="boiler")
    ctx.add(new)
    zone = mp.zone("power")
    return {"ents": list(ctx.new), "zone": None, "units": add_units, "zone_id": zone.id if zone else "power"}


def _downstream(mp, bus_ents) -> dict:
    """items_at of the bus part a steel or science block may tap: downstream of the mall's taps when the mall hangs
    off this bus (the mall keeps the first pick), else the whole bus."""
    bus = bus_items_from(bus_ents)
    lo = _after_mall(mp, bus_ents)
    part = bus_slice(bus, bus_ents, lo=lo) if lo is not None else bus
    return part or bus


def _steel(mp, world, gd, ctx: _Ctx, k, researched, pairs=2):
    """A small steel column beside the plate bus (laid now, with its smelting cells, when the master has none): iron
    plates from a bus tap downstream of the mall's, coal from a tap on the starter base's coal belts, all its steel
    into a depot chest (medium poles, steel furnaces, assembling machines 2 come from there)."""
    col = _column_part(gd, "iron-plate", pairs)          # smelt_recipe: iron plates -> steel plates
    pp = col.ports["plate"]
    vx, vy = DVEC[pp.direction]
    ents = list(col.ents) + [
        PE("inserter", pp.x + 0.5, pp.y + 0.5, (pp.direction + 8) % 16, None, {"role": "depot_in", "item": "steel-plate"}),
        PE("iron-chest", pp.x + vx + 0.5, pp.y + vy + 0.5, 0, None, {"role": "depot", "item": "steel-plate", "holds": 0,
                                                                      "bar": STEEL_BAR})]
    # the depot sits on rows -2..-1 above the column: shift everything down so the part starts at row 0
    ents = [e.moved(0, 2) for e in ents]
    ports = {k2: p.moved(0, 2) for k2, p in col.ports.items() if k2 in ("ore", "coal")}
    part = M.Part("steel_column", ents, (col.size[0], col.size[1] + 2), ports,
                  [("steel", (0, 0, col.size[0], col.size[1] + 2))])
    alive = [e for e in mp.ents if M.alive(e, k)]
    items_at = belt_items(alive, gd, M.world_without_master(world, mp, k))
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))

    def attach(bus_ents):
        _bus_rows(ctx, bus_ents, release=True)
        return _attach(ctx, part, _downstream(mp, bus_ents), {"ore": "iron-plate", "coal": ("coal", items_at, 100)},
                       hub, "x_steel", n=10, mirror_ok=False, bus_ents=bus_ents)
    (sc, placed, feeds), cells, zones = _on_bus(mp, world, gd, ctx, hub, items_at, attach)
    zr = M._rect_union([M._ents_rect(gd, placed.ents)] + [rr for _z, rr in placed.reserve])
    chests = [[e.x, e.y] for e in ctx.new if _x(e).get("role") == "depot"]
    return {"ents": list(ctx.new), "zone": [zr] + zones, "feeds": cells + [f[1] for f in feeds], "chests": chests,
            "score": round(sc, 1), "new_bus": bool(cells)}


SCIENCE_CELL_MAX = CELL_FEED_MAX     # belt tiles from the science block's extra iron cell to the bus


def _science(mp, world, gd, ctx: _Ctx, k, researched):
    """A second red + green science block with its own labs beside the plate bus (blocks.science_block, the starter
    base's engine-checked block: +30 red and +30 green per minute), its four plate inputs tapped from the bus
    downstream of the mall; first an extra iron smelting cell (12 furnaces, on an iron patch within reach) whose plates
    side-load onto the bus iron belt near its head (+225 iron/min: the block uses ~225)."""
    blk = B.science_block(gd, n_red_res=M.N_RED_RES, labs_per_side=5)
    nres = blk.info["n_red_res"]
    red_built = set(range(nres - M.N_RED, nres))
    ents = [copy.deepcopy(e) for e in blk.entities
            if not (_x(e).get("role") in ("red_am", "red_in", "red_out") and _x(e).get("unit") not in red_built)]
    w, h = blk.size
    part = M.Part("science_block", ents, (w, h), dict(zip(["cu", "fe", "gfe", "gcu"], blk.ports)), [])
    alive = [e for e in mp.ents if M.alive(e, k)]
    items_at = belt_items(alive, gd, M.world_without_master(world, mp, k))
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    cell_box = {}

    def attach(bus_ents):
        iron = sorted([e for e in bus_ents if _x(e).get("item") == "iron-plate" and _x(e).get("role") == "bus"],
                      key=lambda e: _x(e).get("row", 0))
        _bus_rows(ctx, bus_ents, release=True)       # the cell's belt side-loads onto the bus from its tap side
        cell_box.pop("info", None)
        if len(iron) > 4:              # more iron onto the bus: a cell whose plates side-load onto its iron belt
            t = iron[3]
            goal = Port("belt_in", "iron-plate", math.floor(t.x), math.floor(t.y), int(t.direction or 0))
            got = _cell(ctx, items_at, "iron-ore", (goal.x, goal.y), goal, slots=12, pairs=6,
                        max_plate=SCIENCE_CELL_MAX)
            if got is not None:
                cell_box["info"] = got[1]
        _bus_rows(ctx, bus_ents, release=True)
        return _attach(ctx, part, _downstream(mp, bus_ents), {"cu": "copper-plate", "fe": "iron-plate",
                                                              "gfe": "iron-plate", "gcu": "copper-plate"},
                       hub, "x_science", n=10, mirror_ok=False, bus_ents=bus_ents)
    (sc, placed, feeds), cells, zones = _on_bus(mp, world, gd, ctx, hub, items_at, attach)
    cell_info = cell_box.get("info")
    zr = M._ents_rect(gd, placed.ents)
    zones = [zr] + zones + (cell_info.get("zones") if cell_info else [])
    return {"ents": list(ctx.new), "zone": zones,
            "feeds": cells + [f[1] for f in feeds] + ([cell_info] if cell_info else []),
            "chests": [[e.x, e.y] for e in ctx.new if _x(e).get("role") == "depot"], "score": round(sc, 1),
            "new_bus": bool(cells)}


# ---- oil (roadmap G9, autopilot round 3) ------------------------------------------------------------------------------
OIL_WELLS = 4                   # pumpjacks per field: ~10 crude/s each at 100% yield, a basic refinery uses 20/s
OIL_PATCHES_TRIED = 3


def _jack_out(gd, e):
    """(target tile, flow direction) of a pumpjack's crude output connection."""
    c = gd.fluid_connections_full(e.name, e.x, e.y, int(e.direction or 0))[0]
    return tuple(c["target_tile"]), int(c["dir"])


def _oil(mp, world, gd, ctx: _Ctx, k, researched):
    """Pumpjacks on the crude-oil wells nearest the base (up to OIL_WELLS, each output facing the base). Pumping crude
    is what finishes the Oil processing trigger tech; the refinery comes in the next stage (oil_plant), so the field's
    outputs stay open for its pipes."""
    alive = [e for e in mp.ents if M.alive(e, k)]
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    g = ctx.grid
    pats = [p for p in world.patches(name="crude-oil") if any(g.in_bounds(*t) for t in p.tiles)]
    if not pats:
        raise ExpandError("oil: no crude oil within reach of the base on the charted map (a radar or a walk would find "
                          "more)")
    pats.sort(key=lambda p: abs(p.center[0] - hub[0]) + abs(p.center[1] - hub[1]))
    reasons = []
    for patch in pats[:OIL_PATCHES_TRIED]:
        wells = sorted((t for t in patch.tiles if g.in_bounds(*t)),
                       key=lambda t: abs(t[0] - hub[0]) + abs(t[1] - hub[1]))
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        outs, jacks = set(), []
        for w in wells:
            if len(jacks) >= OIL_WELLS:
                break
            best = None
            for d in (NORTH, EAST, SOUTH, WEST):
                pj = PE("pumpjack", w[0] + 0.5, w[1] + 0.5, d, None, {"role": "pumpjack"})
                fp = _tiles(gd, pj)
                if not all(ctx.free(t) for t in fp) or fp & outs:
                    break                        # the footprint is the same in every direction
                tgt, _fd = _jack_out(gd, pj)
                if not ctx.free(tgt) or tgt in outs:
                    continue
                dist = abs(tgt[0] - hub[0]) + abs(tgt[1] - hub[1])
                if best is None or dist < best[0]:
                    best = (dist, pj, tgt)
            if best is None:
                continue
            _d, pj, tgt = best
            ctx.add([pj])
            ctx.grid.occupy([tgt], "guard:oil_out")       # its pipe goes there (oil_plant)
            outs.add(tgt)
            jacks.append(pj)
        if jacks:
            ctx.grid.release(list(outs))
            # one zone per pumpjack (wells sit among other blocks: a box around all of them would overlap them)
            zone = [M._ents_rect(gd, [j]) for j in jacks]
            return {"ents": list(ctx.new), "zone": zone, "feeds": [], "chests": [],
                    "wells": [[e.x, e.y] for e in jacks], "patch": [round(patch.center[0], 1),
                                                                    round(patch.center[1], 1)]}
        reasons.append(f"crude oil at {patch.center[0]:.0f},{patch.center[1]:.0f}: no free well")
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError("oil: " + "; ".join(reasons[:3]))


def _pipe(ctx: _Ctx, start: Port, goal: Port):
    from .router import route_pipe
    gc = ctx.grid.copy()
    try:
        return route_pipe(gc, ctx.gd, start, goal, others=ctx.others, tag="exp")
    except NoRoute:
        return None


def _oil_plant(mp, world, gd, ctx: _Ctx, k, researched):
    """The oil plant (blocks.oil_plant_block: a refinery on basic oil processing, a chemical plant on plastic, a plastic
    depot) near the base's coal belts, coal by a tap, crude piped from each pumpjack of the oil stage (the first pipe
    to the refinery's crude input, the others into that pipe)."""
    jacks = [e for e in mp.ents if e.name == "pumpjack" and _x(e).get("role") == "pumpjack"]
    if not jacks:
        raise ExpandError("oil plant: no pumpjacks yet (the oil stage places them)")
    blk = B.oil_plant_block(gd)
    part = M.Part("oil_plant", [copy.deepcopy(e) for e in blk.entities], blk.size,
                  dict(zip(["coal", "crude"], blk.ports)), list(blk.info["reserve"]))
    alive = [e for e in mp.ents if M.alive(e, k)]
    items_at = belt_items(alive, gd, M.world_without_master(world, mp, k))
    coal = np.array([c[3][-1] for c in tap_candidates(ctx, items_at, "coal")], float)
    if not len(coal):
        raise ExpandError("oil plant: no coal belt to tap for the plastic")
    fx = sum(e.x for e in jacks) / len(jacks)
    fy = sum(e.y for e in jacks) / len(jacks)

    def score(rp, X, Y):
        pc, po = rp.ports["coal"], rp.ports["crude"]
        sc = np.min(np.abs((X + pc.x)[:, None] - coal[None, :, 0]) + np.abs((Y + pc.y)[:, None] - coal[None, :, 1]), 1)
        return sc + 0.5 * (np.abs(X + po.x - fx) + np.abs(Y + po.y - fy))
    xs = list(coal[:, 0]) + [fx]
    ys = list(coal[:, 1]) + [fy]
    win = (int(min(xs)) - 40, int(min(ys)) - 40, int(max(xs)) + 40, int(max(ys)) + 40)
    reasons = []
    for _sc, _m, _k, placed in _spots(ctx, part, score, win, n=10, mirror_ok=False):
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(placed.ents)
        for _z, rr in placed.reserve:
            _occupy_rect(ctx, rr, "zone:x_oil_plant")
        for p in placed.ports.values():
            ctx.grid.release([(p.x, p.y)])
        got = None
        with _guarded(ctx, placed.ports, "coal"):
            feed = _tap_feed(ctx, items_at, "coal", placed.ports["coal"], n_ins=1, max_len=100)
        if feed is not None:
            goal = placed.ports["crude"]
            pipes, ok = [], True
            for j in sorted(jacks, key=lambda e: abs(e.x - goal.x) + abs(e.y - goal.y)):
                tgt, fd = _jack_out(gd, j)
                start = Port("pipe_out", "crude-oil", tgt[0], tgt[1], fd)
                if pipes:          # into the nearest plain pipe already laid (the router ends next to it)
                    near = min((e for e in pipes if e.name == "pipe"),
                               key=lambda e: abs(e.x - tgt[0]) + abs(e.y - tgt[1]), default=None)
                    to = Port("pipe_in", "crude-oil", math.floor(near.x), math.floor(near.y), NORTH) if near else goal
                else:
                    to = goal
                r = _pipe(ctx, start, to)
                if r is None:
                    if not pipes:
                        ok = False
                        break
                    continue                 # this well stays unpiped (it pumps until its own box is full)
                r = [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role="crude_pipe")) for e in r]
                ctx.add(r)
                pipes += r
            if ok:
                got = (feed, pipes)
        if got is not None:
            feed, pipes = got
            zr = M._rect_union([M._ents_rect(gd, placed.ents)] + [rr for _z, rr in placed.reserve])
            return {"ents": list(ctx.new), "zone": zr, "feeds": [feed[1]],
                    "chests": [[e.x, e.y] for e in placed.ents if _x(e).get("role") == "depot"],
                    "pipes": len(pipes)}
        reasons.append(f"{placed.ents[0].x:.0f},{placed.ents[0].y:.0f}: "
                       + ("no coal tap route" if feed is None else "no crude pipe route"))
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError("oil plant: " + ("; ".join(reasons[:3]) or "no free ground near the coal belts"))


def _grow(ore):
    def design(mp, world, gd, ctx: _Ctx, k, researched):
        """One more smelting cell for `ore` (CELL: 12 drills / 6 furnace pairs for iron, ~225 plates/min) whose plates
        side-load onto the plate bus's belt of that metal near its head; with no bus yet, the bus itself (it comes
        with a cell per metal). The autopilot adds one when its rate check finds that plate the bottleneck."""
        plate = ore.replace("-ore", "-plate")
        alive = [e for e in mp.ents if M.alive(e, k)]
        items_at = belt_items(alive, gd, M.world_without_master(world, mp, k))
        hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
        bus_ents = _bus_ents(mp)
        if not bus_ents:
            _b, cells, zones, _got = _new_bus(mp, world, gd, ctx, hub, items_at)
            return {"ents": list(ctx.new), "zone": zones, "feeds": cells, "chests": [], "new_bus": True}
        rows = sorted([e for e in bus_ents if _x(e).get("item") == plate and _x(e).get("role") == "bus"
                       and e.name == "transport-belt"], key=lambda e: _x(e).get("row", 0))
        if not rows:
            raise ExpandError(f"{plate}: the bus has no {plate} belt")
        reasons = []
        for t in rows[3:12:2] or rows[:1]:          # near the head, upstream of every tap
            goal = Port("belt_in", plate, math.floor(t.x), math.floor(t.y), int(t.direction or 0))
            _bus_rows(ctx, bus_ents, release=True)
            try:
                got = _cell(ctx, items_at, ore, (goal.x, goal.y), goal, max_plate=CELL_FEED_MAX * 2)
            finally:
                _bus_rows(ctx, bus_ents, release=False)
            if got is not None:
                ents, info = got
                return {"ents": list(ctx.new), "zone": info.get("zones"), "feeds": [info], "chests": []}
            reasons.append(f"bus row {_x(t).get('row', 0)}: no {ore} patch with room within reach")
        raise ExpandError(f"{plate}: " + "; ".join(reasons[:3]))
    return design


# ---- blue science (roadmap G10, autopilot round 4) ---------------------------------------------------------------------
BLUE_FEED_MAX = 160             # belt tiles of a feed into the blue block (steel / plastic from their depots, bus taps)
BLUE_PACKS_MAX = 220            # belt tiles from the block to the labs (the labs stand where the starter base put them)
BLUE_PIPE_MAX = 180             # pipes (an underground pair counts two) of the petroleum and of the water line
WATER_TRIES = 12                # offshore pump spots tried (spread along the shores, nearest the block first)
LAB_ROW, LAB_ROW_MIN = 6, 3     # new labs chained from an old one (~13 packs/min: ~7 labs on a 30 s tech)
LAB_FEEDS = 4                   # lab rows offered to the block's placement
FLUID_TYPES = ("assembling-machine", "furnace", "boiler", "generator", "offshore-pump", "mining-drill", "pump",
               "storage-tank")
VEC_DIR = {v: d for d, v in DVEC.items()}


def _releasable(ctx: _Ctx, t, zone=None) -> bool:
    """Free ground, or reserved room of zone `zone` where nothing stands (the source's own zone: the tiles beside a
    depot chest, the oil header, a lab stack). Other zones' room stays theirs (the bus's lanes, the mall's slots, the
    power block's units)."""
    g = ctx.grid
    return g.in_bounds(*t) and (g.state_at(*t) != BLOCKED or (zone is not None and g.tag_at(*t) == f"zone:{zone}"))


def _release_zone(ctx: _Ctx, tiles):
    g = ctx.grid
    g.release([t for t in tiles if str(g.tag_at(*t) or "").startswith("zone:")])


def _depots(mp, item) -> list:
    return [e for e in mp.ents if _x(e).get("role") == "depot" and _x(e).get("item") == item and "chest" in e.name]


def _chest_feed(ctx: _Ctx, chests, item, goal: Port, max_len=BLUE_FEED_MAX):
    """An inserter taking `item` out of one of the master's depot chests (the steel column's, the oil plant's plastic
    one) onto a new belt routed to port `goal`. The belt backs up when the block takes less: then the chest fills
    again for the helper, as before. -> (entities, info) or None (the grid unchanged)."""
    for ch in chests:
        cx, cy = math.floor(ch.x), math.floor(ch.y)
        sides = sorted(DVEC, key=lambda d: abs(cx + 2 * DVEC[d][0] - goal.x) + abs(cy + 2 * DVEC[d][1] - goal.y))
        for d in sides:
            vx, vy = DVEC[d]
            T, D = (cx + vx, cy + vy), (cx + 2 * vx, cy + 2 * vy)
            z = _x(ch).get("zone")
            if not (_releasable(ctx, T, z) and _releasable(ctx, D, z)):
                continue
            save = ctx.grid.copy()
            _release_zone(ctx, [T, D])
            ins = PE("inserter", T[0] + 0.5, T[1] + 0.5, (d + 8) % 16, None, {"role": "depot_out", "item": item})
            ctx.grid.occupy([T], _tag(ins))
            route = _route(ctx, Port("belt_out", item, D[0], D[1], d), goal, wide=True)
            ctx.grid = save
            if route is None or len(route) > max_len:
                continue
            ents = [ins] + [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role="feed", item=item))
                            for e in route]
            ctx.add(ents)
            return ents, {"item": item, "chest": [ch.x, ch.y], "belt_tiles": len(route), "inserters": 1}
    return None


def _fluid_targets(ctx: _Ctx) -> set:
    """Tiles where a pipe would join a machine of the master or the map (its fluid connection targets)."""
    out = set()
    for e in ctx.others:
        if e.name in ("pipe", "pipe-to-ground") or ctx.gd.entity_or_empty(e.name).get("type") not in FLUID_TYPES:
            continue
        try:
            conns = ctx.gd.fluid_connections_full(e.name, e.x, e.y, int(e.direction or 0))
        except (ValueError, KeyError):
            continue
        out |= {tuple(c["target_tile"]) for c in conns if c.get("type") != "underground"}
    return out


def _ptg_compress(gd, ents) -> tuple:
    """Straight stretches of a routed pipe as pipe-to-ground pairs (the router takes them only to pass under
    something; a 150-tile water line was 150 pipes for the helper to place). Only the inner tiles of a straight run:
    its first and last pipe stay plain (they join whatever comes before and after in any direction); a pair spans at
    most the pipe-to-ground's reach, pairs follow each other back to back. -> (entities, tiles freed underground)."""
    ptg = "pipe-to-ground"
    if ptg not in (gd.raw.get("entities") or {}) or not gd.can_build(ptg):
        return list(ents), []
    md = int(gd.entity(ptg).get("max_underground_distance") or 10)
    out, removed, i, n = [], [], 0, len(ents)
    while i < n:
        if ents[i].name != "pipe":
            out.append(ents[i])
            i += 1
            continue
        j, d = i, None
        while j + 1 < n and ents[j + 1].name == "pipe":
            v = (round(ents[j + 1].x - ents[j].x), round(ents[j + 1].y - ents[j].y))
            if abs(v[0]) + abs(v[1]) != 1 or (d is not None and v != d):
                break
            d, j = v, j + 1
        run = ents[i:j + 1]
        i = j + 1
        if d is None or len(run) < 6:
            out += run
            continue
        inner, k = run[1:-1], 0
        out.append(run[0])
        while k < len(inner):
            if len(inner) - k < 4:
                out += inner[k:]
                break
            span = min(md, len(inner) - k - 1)
            a, b = inner[k], inner[k + span]
            out += [PE(ptg, a.x, a.y, VEC_DIR[(-d[0], -d[1])], None, dict(_x(a))),
                    PE(ptg, b.x, b.y, VEC_DIR[d], None, dict(_x(b)))]
            removed += [(math.floor(e.x), math.floor(e.y)) for e in inner[k + 1:k + span]]
            k += span + 1
        out.append(run[-1])
    return out, removed


def _add_pipe(ctx: _Ctx, ents, role):
    """Add a routed pipe (compressed, its underground tiles kept free of anything that could pair with it)."""
    ents, under = _ptg_compress(ctx.gd, ents)
    ents = [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role=role)) for e in ents]
    ctx.add(ents)
    ctx.grid.occupy(under, "guard:pipe_span")
    return ents


def _petroleum_feed(ctx: _Ctx, mp, goal: Port, role="oil_pipe", fluid="petroleum-gas", out_role="petroleum_pipe"):
    """A pipe from the oil plant's petroleum header (the pipes between the refinery's 3rd output and the plastic
    plant) to port `goal`: it starts on a free tile beside a header pipe, never on a machine's connection tile (the
    refinery's heavy and light oil outputs sit right next to the header) nor beside any other pipe. role / fluid: the
    master's pipes to branch from (round 5: the robot block's crude oil from the oil plant's crude pipes, role
    crude_pipe)."""
    header = {(math.floor(e.x), math.floor(e.y)) for e in mp.ents if _x(e).get("role") == role and e.name == "pipe"}
    if not header:
        return None
    zone = next((_x(e).get("zone") for e in mp.ents if _x(e).get("role") == role), None)
    forbid = _fluid_targets(ctx)
    pipes = {(math.floor(e.x), math.floor(e.y)) for e in ctx.others if e.name in ("pipe", "pipe-to-ground")} - header
    cands = []
    for hx, hy in header:
        for d, (vx, vy) in DVEC.items():
            t = (hx + vx, hy + vy)
            if t in header or t in forbid or not _releasable(ctx, t, zone):
                continue
            if any((t[0] + a, t[1] + b) in pipes for a, b in DVEC.values()):
                continue
            cands.append((abs(t[0] - goal.x) + abs(t[1] - goal.y), t, d))
    for _dist, t, d in sorted(set(cands))[:6]:
        save = ctx.grid.copy()
        _release_zone(ctx, [t])
        r = _pipe(ctx, Port("pipe_out", fluid, t[0], t[1], d), goal)
        if r is None or len(r) > BLUE_PIPE_MAX:
            ctx.grid = save
            continue
        ents = _add_pipe(ctx, r, out_role)
        return ents, {"item": fluid, "from": list(t), "pipes": len(ents)}
    return None


def _water_feed(ctx: _Ctx, world, goal: Port):
    """An offshore pump on the shore nearest port `goal` (spots 8+ tiles apart, WATER_TRIES of them) and a pipe from it
    to the port (the chemical plant's water input). An offshore pump needs no power."""
    gd, g = ctx.gd, ctx.grid
    spots = []
    for body in world.water_bodies(30):
        for x, y, d in world.shoreline(body):
            if g.in_bounds(x, y):
                spots.append((abs(x - goal.x) + abs(y - goal.y), x, y, d))
    spread = []
    for sp in sorted(spots):
        if all(abs(sp[1] - q[1]) + abs(sp[2] - q[2]) >= 8 for q in spread):
            spread.append(sp)
        if len(spread) >= WATER_TRIES:
            break
    for _dist, x, y, d in spread:
        pump = PE("offshore-pump", x + 0.5, y + 0.5, d, None, {"role": "water_pump"})
        foot = _tiles(gd, pump)
        if not all(ctx.free(t) for t in foot if not g.is_water(*t)):
            continue
        conns = gd.fluid_connections("offshore-pump", pump.x, pump.y, d)
        if not conns or not ctx.free((conns[0][0], conns[0][1])):
            continue
        tx, ty, dout = conns[0]
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add([pump])
        r = _pipe(ctx, Port("pipe_out", "water", tx, ty, dout), goal)
        if r is None or len(r) > BLUE_PIPE_MAX:
            ctx.grid = base
            ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
            continue
        ents = _add_pipe(ctx, r, "water_pipe")
        return [pump] + ents, {"item": "water", "pump": [pump.x, pump.y], "pipes": len(ents)}
    return None


def _ins_reach(ctx: _Ctx) -> set:
    """Pickup and drop tiles of the master's (and the map's) inserters: a new machine there would get their items."""
    out = set()
    for e in ctx.others:
        if "inserter" not in e.name:
            continue
        vx, vy = DVEC.get(int(e.direction or 0) % 16, (0, 0))
        r = 2 if e.name == "long-handed-inserter" else 1
        tx, ty = math.floor(e.x), math.floor(e.y)
        out |= {(tx + r * vx, ty + r * vy), (tx - r * vx, ty - r * vy)}
    return out


def _lab_feeds(ctx: _Ctx, mp) -> list:
    """Where the chemical science packs go: a row of LAB_ROW new labs (fewer where the ground is short) chained from
    one of the master's labs - an inserter takes the red and green packs its spine inserter puts in and passes them
    on to the row's first lab, the next inserter to the next lab, ... (labs pass packs on to labs) - with the packs
    belt ending at an inserter into one of the new labs (the chain carries them down the row). The labs of the second
    science block first, then the starter base's. The old labs keep researching what needs red and green only.
    Round 4: the free strips beside a science block's lab columns hold its poles on most maps, so a belt along them
    rarely fits. -> [(name, entities, entry Port, tiles)], at most LAB_FEEDS."""
    gd = ctx.gd
    labs = [e for e in mp.ents if e.name == "lab" and _x(e).get("role") == "lab"]
    labs.sort(key=lambda e: (not str(_x(e).get("zone") or "").startswith("x_science"), int(_x(e).get("unit") or 0)))
    reach = _ins_reach(ctx)
    belts = {}
    for e in ctx.others:
        if e.name in ("transport-belt", "underground-belt", "splitter"):
            for t in _tiles(gd, e):
                belts[t] = int(e.direction or 0)

    def tile(p):
        return math.floor(p[0]), math.floor(p[1])

    def box(c):           # the 3x3 tiles of a lab centred at c
        return [(math.floor(c[0]) + dx, math.floor(c[1]) + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]

    def ok(ts, zone):
        return all(_releasable(ctx, t, zone) and t not in reach for t in ts)
    out, used = [], set()
    for L in labs:
        zl = _x(L).get("zone")
        for s, (sx, sy) in DVEC.items():
            if len(out) >= LAB_FEEDS:
                return out
            for n in range(LAB_ROW, LAB_ROW_MIN - 1, -1):
                cs = [(L.x + 4 * kk * sx, L.y + 4 * kk * sy) for kk in range(1, n + 1)]
                chain = [(L.x + (4 * kk - 2) * sx, L.y + (4 * kk - 2) * sy) for kk in range(1, n + 1)]
                tiles = [t for c in cs for t in box(c)] + [tile(c) for c in chain]
                if set(tiles) & used or not ok(tiles, zl):
                    continue
                entry = None
                for m in range(n):                    # the packs' inserter beside one of the new labs
                    for p in ((sy, -sx), (-sy, sx)):
                        c = cs[m]
                        ti, bt, en, a1, a2 = ((c[0] + q * p[0], c[1] + q * p[1]) for q in (2, 3, 4, 5, 6))
                        extra = [tile(ti), tile(bt), tile(en)]
                        # the entry and its approach lane on open ground (not reserved room the route cannot enter):
                        # the pack belt arrives straight from beyond it
                        lane = [tile(en), tile(a1), tile(a2)]
                        if not ok(extra, zl) or not all(ctx.free(t) for t in lane) or set(extra + lane) & set(tiles):
                            continue
                        # no belt of the base runs into the packs belt's tile from a side
                        if any((t[0] + DVEC[bd][0], t[1] + DVEC[bd][1]) == tile(bt) for t, bd in belts.items()):
                            continue
                        entry = (m, p, ti, bt, en)
                        break
                    if entry:
                        break
                if entry is None:
                    continue
                m, p, ti, bt, en = entry
                pdir, back = VEC_DIR[p], VEC_DIR[(-p[0], -p[1])]
                zid = str(_x(L).get("zone"))
                ents = [PE("lab", c[0], c[1], 0, None, {"role": "blue_lab", "unit": kk, "labs": zid})
                        for kk, c in enumerate(cs)]
                ents += [PE("inserter", c[0], c[1], VEC_DIR[(-sx, -sy)], None, {"role": "lab_chain", "unit": kk})
                         for kk, c in enumerate(chain)]
                ents += [PE("inserter", ti[0], ti[1], pdir, None, {"role": "lab_feed_ins", "unit": m}),
                         PE("transport-belt", bt[0], bt[1], back, None, {"role": "lab_feed"})]
                tiles += [tile(ti), tile(bt)]
                used |= set(tiles)
                e = tile(en)
                out.append((f"{n} labs from the lab at {L.x:g},{L.y:g}", ents,
                            Port("belt_in", "chemical-science-pack", e[0], e[1], back), tiles))
                break
    return out


def _blue(mp, world, gd, ctx: _Ctx, k, researched):
    """Blue science (roadmap G10): blocks.blue_block beside the plate bus (iron and copper from bus taps downstream of
    the mall's), steel from the steel column's depot chest and plastic from the oil plant's (an inserter out of each
    chest onto a belt to the block), petroleum gas piped from the oil plant's header to the block's sulfur plant, water
    from an offshore pump of its own, and the packs on a belt to a row of new labs chained from one of the master's
    labs (_lab_feeds). Every feed is routed per block spot; a spot where one fails is given up for the next."""
    bus_ents = _bus_ents(mp)
    if not bus_ents:
        raise ExpandError("blue: the master has no plate bus yet (the steel stage lays it)")
    steel, plastic = _depots(mp, "steel-plate"), _depots(mp, "plastic-bar")
    if not steel:
        raise ExpandError("blue: no steel depot (the steel stage)")
    if not plastic or not any(_x(e).get("role") == "oil_pipe" for e in mp.ents):
        raise ExpandError("blue: no oil plant yet (petroleum gas and plastic come from it)")
    feeds = _lab_feeds(ctx, mp)
    if not feeds:
        raise ExpandError("blue: no room for a row of labs chained from the master's labs")
    blk = B.blue_block(gd)
    labels = ["fe", "pl", "cu", "st", "water", "petrol", "blue"]
    part = M.Part("blue_block", [copy.deepcopy(e) for e in blk.entities], blk.size, dict(zip(labels, blk.ports)), [])
    alive = [e for e in mp.ents if M.alive(e, k)]
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    bus = _downstream(mp, bus_ents)
    _bus_rows(ctx, bus_ents, release=True)
    pts = {}
    for label, item in (("fe", "iron-plate"), ("cu", "copper-plate")):
        pts[label] = np.array([c[3][-1] for c in tap_candidates(ctx, bus, item, clear=2)] or
                              [c[3][-1] for c in tap_candidates(ctx, bus, item)], float)
        if not len(pts[label]):
            raise ExpandError(f"blue: no free tap spot for {item} on the bus")
    pts["st"] = np.array([[e.x, e.y] for e in steel], float)
    pts["pl"] = np.array([[e.x, e.y] for e in plastic], float)
    pts["petrol"] = np.array([[e.x, e.y] for e in mp.ents if _x(e).get("role") == "oil_pipe"], float)
    shore = [(x, y) for body in world.water_bodies(30) for x, y, _d in world.shoreline(body)
             if ctx.grid.in_bounds(x, y)]
    if not shore:
        raise ExpandError("blue: no water within reach for the sulfur plant")
    shore.sort(key=lambda t: abs(t[0] - hub[0]) + abs(t[1] - hub[1]))
    pts["water"] = np.array(shore[:400:4] or shore[:1], float)
    pts["blue"] = np.array([[f[2].x, f[2].y] for f in feeds], float)
    weight = {"fe": 1.0, "cu": 1.0, "st": 0.5, "pl": 0.5, "petrol": 0.7, "water": 0.2, "blue": 0.5}
    # the bus taps are on one side of the bus (its tap rows): a block on the other side cannot reach them (no
    # underground spans the bus corridor's reserved lanes)
    fe0 = next(e for e in bus_ents if _x(e).get("item") == "iron-plate" and e.name == "transport-belt")
    cu0 = next((e for e in bus_ents if _x(e).get("item") == "copper-plate" and e.name == "transport-belt"), None)
    nx, ny = ((math.floor(fe0.x) - math.floor(cu0.x), math.floor(fe0.y) - math.floor(cu0.y)) if cu0 is not None
              else DVEC[(int(fe0.direction or 0) + 12) % 16])

    def score(rp, X, Y):
        sc = np.zeros(len(X))
        for label, P in pts.items():
            p = rp.ports[label]
            dx, dy = (X + p.x)[:, None] - P[None, :, 0], (Y + p.y)[:, None] - P[None, :, 1]
            near = np.argmin(np.abs(dx) + np.abs(dy), 1)
            sc = sc + weight[label] * np.min(np.abs(dx) + np.abs(dy), 1)
            # a source behind the port (on the block's side of it) means a route around the block: the router's
            # long detours there cross themselves (2002 onsite); a port facing its source is preferred
            vx, vy = DVEC[p.direction]
            ahead = -(dx[np.arange(len(X)), near] * vx + dy[np.arange(len(X)), near] * vy)
            sc = sc + weight[label] * (np.maximum(0, -ahead) if p.kind == "belt_out" else np.maximum(0, ahead))
        for label in ("fe", "cu"):
            p = rp.ports[label]
            side = (X + p.x - math.floor(fe0.x)) * nx + (Y + p.y - math.floor(fe0.y)) * ny
            sc = sc + 400.0 * (side <= 0)
        return sc + 0.1 * (np.abs(X + rp.size[0] / 2 - hub[0]) + np.abs(Y + rp.size[1] / 2 - hub[1]))
    allp = np.concatenate([P for P in pts.values() if len(P)])
    win = (int(allp[:, 0].min()) - 50, int(allp[:, 1].min()) - 50, int(allp[:, 0].max()) + 50,
           int(allp[:, 1].max()) + 50)
    guard = sorted({t for f in feeds for t in f[3] + [(f[2].x, f[2].y)]})
    _bus_rows(ctx, bus_ents, release=False)          # no block on the bus's tap rows,
    ctx.grid.occupy([t for t in guard if ctx.grid.state_at(*t) != BLOCKED], "guard:labs")    # nor on a lab feed
    spots = _spots(ctx, part, score, win, n=8, mirror_ok=False)
    _bus_rows(ctx, bus_ents, release=True)
    reasons = []
    kinds = ("belt_in", "belt_out", "pipe_in", "pipe_out")
    for sc, _m, _k, placed in spots:
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(placed.ents)
        ctx.grid.release([(p.x, p.y) for p in placed.ports.values()])
        P = placed.ports
        got, why = {}, None
        steps = (("fe", lambda: _tap_feed(ctx, bus, "iron-plate", P["fe"], n_ins=2, max_len=BLUE_FEED_MAX, clear=2,
                                          wide=True)),
                 ("cu", lambda: _tap_feed(ctx, bus, "copper-plate", P["cu"], n_ins=1, max_len=BLUE_FEED_MAX, clear=2,
                                          wide=True)),
                 ("st", lambda: _chest_feed(ctx, steel, "steel-plate", P["st"])),
                 ("pl", lambda: _chest_feed(ctx, plastic, "plastic-bar", P["pl"])),
                 ("petrol", lambda: _petroleum_feed(ctx, mp, P["petrol"])),
                 ("water", lambda: _water_feed(ctx, world, P["water"])))
        for label, fn in steps:
            with _guarded(ctx, P, label, kinds):
                f = fn()
            if f is None:
                why = {"fe": "no iron tap route", "cu": "no copper tap route", "st": "no steel feed from its depot",
                       "pl": "no plastic feed from the oil plant's depot", "petrol": "no petroleum pipe route",
                       "water": "no water pipe from a shore"}[label]
                break
            got[label] = f
        if why is None:
            why = "no pack belt to the labs"
            for name, ents, entry, tiles in feeds:
                save = ctx.grid.copy()       # (a lab row that gets no belt keeps its zone's reserved room)
                g = ctx.grid
                g.release([t for t in tiles + [(entry.x, entry.y)] if g.tag_at(*t) == "guard:labs"])
                _release_zone(ctx, tiles + [(entry.x, entry.y)])
                g.occupy(tiles, "guard:labs")
                bp = P["blue"]
                r = _route(ctx, Port("belt_out", bp.item, bp.x, bp.y, bp.direction), entry, wide=True)
                if r is not None and len(r) <= BLUE_PACKS_MAX:
                    g = ctx.grid
                    g.release([t for t in tiles if g.tag_at(*t) == "guard:labs"])
                    belt = [PE(e.name, e.x, e.y, e.direction, e.recipe, dict(_x(e), role="feed",
                                                                             item="chemical-science-pack"))
                            for e in r]
                    ctx.add(belt + ents)
                    got["blue"] = (belt + ents, {"item": "chemical-science-pack", "labs": name,
                                                 "belt_tiles": len(r) + sum(1 for e in ents if e.name != "inserter")})
                    why = None
                    break
                ctx.grid = save
        if why is None:
            g = ctx.grid             # the other lab feeds' tiles are free ground again
            g.release([t for t in guard if g.tag_at(*t) == "guard:labs"])
            zr = M._ents_rect(gd, placed.ents)
            return {"ents": list(ctx.new), "zone": zr, "feeds": [got[l][1] for l in ("fe", "cu", "st", "pl", "blue")],
                    "fluids": [got["petrol"][1], got["water"][1]], "chests": [], "score": round(sc, 1),
                    "labs": got["blue"][1]["labs"], "rate": blk.rates.get("chemical-science-pack")}
        reasons.append(f"{placed.ents[0].x:.0f},{placed.ents[0].y:.0f}: {why}")
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError("blue: " + ("; ".join(reasons[:4]) or "no free ground for the block near the bus"))


def blue_rate(mp, gd, k) -> dict:
    """{chemical-science-pack: per min} the blue stages alive at k can make (its machines: the packs, the engines and
    the advanced circuits per 1.5 packs, whichever is least), for the autopilot's rate check (R7)."""
    tot = {"chemical-science-pack": 0.0, "engine-unit": 0.0, "advanced-circuit": 0.0}
    for e in mp.ents:
        if not M.alive(e, k) or e.recipe not in tot or _x(e).get("role") not in ("blue_chem", "blue_am"):
            continue
        r = (gd.raw.get("recipes") or {}).get(e.recipe) or {}
        amount = float(next((p.get("amount") or 1 for p in r.get("products") or [] if p.get("name") == e.recipe), 1))
        speed = float(gd.entity_or_empty(e.name).get("crafting_speed") or 0.5)
        tot[e.recipe] += 60.0 * amount * speed / float(r.get("energy") or 1)
    if not tot["chemical-science-pack"]:
        return {}
    return {"chemical-science-pack": round(min(tot["chemical-science-pack"], tot["engine-unit"],
                                               tot["advanced-circuit"] / 1.5), 1)}


# ---- the radar network (exploration) -----------------------------------------------------------------------------------
RADAR = "radar"
RADAR_KW = 300.0                # its draw when the data export has none (2.1: 300 kW, no drain)
RADAR_NEARBY_CHUNKS = 3         # 2.x radar max_distance_of_nearby_sector_revealed (kept charted all the time)
RADAR_SCAN_CHUNKS = 14          # max_distance_of_sector_revealed (the slow long-range scan, one chunk at a time)
RADAR_OVERLAP = 0.25            # the squares two radars keep charted may overlap by a quarter of their side
RADAR_EDGE = (3, 8, 14)         # tiles outside the base's built bounding box the ring of candidate spots runs
RADAR_STEP = 4                  # tiles between candidate spots along the ring
RADAR_HUB_R = 24                # the first radar: within this many tiles of the hub
RADAR_SPOTS = 16                # candidate spots whose poles are routed before the template gives up
RADAR_POLES_MAX = 12            # a spot that needs a longer pole chain than this is not worth it
OCTANTS = 8                     # direction index: 0 east, clockwise (y south) in steps of 45 degrees


def radar_reach(gd) -> tuple:
    """(chunks kept charted around a radar's chunk, chunks its long-range scan reaches) from the data export (older
    exports have neither: the 2.x defaults)."""
    p = gd.entity_or_empty(RADAR) if hasattr(gd, "entity_or_empty") else {}
    near = int(p.get("max_distance_of_nearby_sector_revealed") or RADAR_NEARBY_CHUNKS)
    scan = int(p.get("max_distance_of_sector_revealed") or RADAR_SCAN_CHUNKS)
    return near, scan


def radar_kw(gd) -> float:
    p = gd.entity_or_empty(RADAR) if hasattr(gd, "entity_or_empty") else {}
    return float(p.get("max_energy_usage_kw") or p.get("energy_usage_kw") or RADAR_KW)


def radar_spacing(gd) -> float:
    """Tiles (along x or y) between two radars: the side of the square one keeps charted, (2 N + 1) chunks, less
    RADAR_OVERLAP. 2.1: 7 chunks = 224 tiles -> 168."""
    near, _scan = radar_reach(gd)
    return (2 * near + 1) * 32 * (1 - RADAR_OVERLAP)


def radars_of(mp, world=None) -> list:
    """(x, y) of every radar the base has or plans: the master's (any stage) and the map's (the player's own and
    ghosts; the master's built ones are on the map too, counted once)."""
    out = [(e.x, e.y) for e in mp.ents if e.name == RADAR]
    for e in (getattr(world, "entities", None) or ()):
        if e.name in (RADAR, "ghost:" + RADAR) and not any(abs(e.x - x) < 2 and abs(e.y - y) < 2 for x, y in out):
            out.append((e.x, e.y))
    return out


def octant(dx, dy) -> int:
    """Direction index of a vector (0 east, 2 south, 4 west, 6 north)."""
    return int(round(math.atan2(dy, dx) / (math.pi / 4))) % OCTANTS


def _unknown_share(world, x, y, half) -> float:
    """Share of the exported map within `half` tiles of (x, y) that is not charted (0 when none of it is exported:
    nothing to tell)."""
    x0, y0, x1, y1 = world.bbox
    i0, i1 = max(y0, math.floor(y - half)), min(y1, math.ceil(y + half))
    j0, j1 = max(x0, math.floor(x - half)), min(x1, math.ceil(x + half))
    if i1 <= i0 or j1 <= j0:
        return 0.0
    return float(1.0 - world.known[i0 - y0:i1 - y0, j0 - x0:j1 - x0].mean())


def radar_scores(world, gd, centre, spots, hints=None) -> list:
    """Score each candidate spot (x, y) for the next radar, higher first: the share of uncharted ground in its
    direction (the game's chunk survey per octant in hints["octants"] when given, else the export's own charted
    mask around the spot), counted once more for every resource in hints["toward"] the charted map has no patch of
    (it has to be found), plus how straight the spot points at the nearest known patch outside the base of each
    resource in hints["toward"] (radar keeps it charted: the expansion that needs it plans on fresh data). A small
    penalty per tile from the centre keeps pole chains short among equals."""
    hints = hints or {}
    survey = {}
    for o in hints.get("octants") or ():
        o = o if isinstance(o, dict) else {}
        if o.get("dir") is not None and float(o.get("total") or 0) > 0:
            survey[int(o["dir"]) % OCTANTS] = 1.0 - float(o.get("charted") or 0) / float(o["total"])
    _near, scan = radar_reach(gd)
    cx, cy = centre
    targets, missing = [], 0
    for name in hints.get("toward") or ():
        pats = [p for p in world.patches(name=name) if math.hypot(p.center[0] - cx, p.center[1] - cy) > 24]
        if pats:
            p = min(pats, key=lambda p: math.hypot(p.center[0] - cx, p.center[1] - cy))
            targets.append((p.center[0] - cx, p.center[1] - cy))
        else:
            missing += 1
    out = []
    for x, y in spots:
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy) or 1.0
        explore = survey.get(octant(dx, dy)) if survey else None
        if explore is None:
            explore = _unknown_share(world, x, y, scan * 32 / 2)
        ore = sum(max(0.0, (dx * tx + dy * ty) / (d * (math.hypot(tx, ty) or 1.0))) for tx, ty in targets)
        out.append(((1 + missing) * explore + ore - d / 1000.0, (x, y)))
    out.sort(key=lambda s: -s[0])
    return out


def _radar_spot_ok(ctx: _Ctx, x, y) -> bool:
    """A radar centred on tile (x, y): its 3x3 and a walkway tile around it free, no ore under it."""
    g = ctx.grid
    for tx in range(x - 2, x + 3):
        for ty in range(y - 2, y + 3):
            if not ctx.free((tx, ty)):
                return False
            if abs(tx - x) <= 1 and abs(ty - y) <= 1 and g.res[ty - g.y0, tx - g.x0] != 0:
                return False
    return True


def _radar(mp, world, gd, ctx: _Ctx, k, researched):
    """Radar network: one radar, powered by a short pole chain from the master's network. With no radar on the map or
    in the master: next to the hub (the base's own ground stays charted and its nests and patches current). Else on a
    ring just outside the base's built bounding box (every master entity alive at k), at least radar_spacing tiles
    (along x or y) from every other radar so the squares they keep charted tile, in the direction radar_scores ranks
    best (uncharted ground, ore the autopilot needs; ctx.hints)."""
    alive = [e for e in mp.ents if M.alive(e, k)]
    if not alive:
        raise ExpandError("radar: the master has nothing built to power a radar from")
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    have = radars_of(mp, world)
    hints = getattr(ctx, "hints", None) or {}
    first = not have
    hx, hy = math.floor(hub[0]), math.floor(hub[1])
    centre = hub
    if first:
        spots = sorted(((x, y) for x in range(hx - RADAR_HUB_R, hx + RADAR_HUB_R + 1)
                        for y in range(hy - RADAR_HUB_R, hy + RADAR_HUB_R + 1)),
                       key=lambda t: (abs(t[0] - hx) + abs(t[1] - hy), t))
        ranked = [(0.0, t) for t in spots if _radar_spot_ok(ctx, *t)]
    else:
        bx0, by0, bx1, by1 = M._ents_rect(gd, alive)
        gap = radar_spacing(gd)
        ring = []
        for e in RADAR_EDGE:
            x0, y0, x1, y1 = bx0 - e - 2, by0 - e - 2, bx1 + e + 1, by1 + e + 1     # radar centre tiles
            ring += [(x, y) for x in range(x0, x1 + 1, RADAR_STEP) for y in (y0, y1)]
            ring += [(x, y) for y in range(y0 + RADAR_STEP, y1, RADAR_STEP) for x in (x0, x1)]
        ring = [t for t in ring if all(max(abs(t[0] + 0.5 - rx), abs(t[1] + 0.5 - ry)) >= gap for rx, ry in have)]
        if not ring:
            raise ExpandError(f"radar: every spot on the base's edge is within {gap:.0f} tiles of a radar already")
        cands = [t for t in ring if _radar_spot_ok(ctx, *t)]
        centre = ((bx0 + bx1) / 2, (by0 + by1) / 2)
        ranked = radar_scores(world, gd, centre, cands, hints)
    reasons = []
    for score, (x, y) in ranked[:RADAR_SPOTS]:
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        r = PE(RADAR, x + 0.5, y + 0.5, NORTH, None, {"role": "radar"})
        ctx.add([r])
        try:
            poles = _poles(ctx, k)
            if len(poles) > RADAR_POLES_MAX:
                raise ExpandError(f"{len(poles)} poles to reach it")
        except ExpandError as e:
            reasons.append(f"{x},{y}: {e}")
            ctx.grid = base
            ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
            continue
        return {"ents": list(ctx.new), "zone": M._ents_rect(gd, [r]), "feeds": [], "chests": [],
                "radar": [r.x, r.y], "first": first, "dir": None if first else octant(r.x - centre[0], r.y - centre[1]),
                "score": round(score, 3), "poles": len(poles)}
    raise ExpandError("radar: " + ("; ".join(reasons[:3]) or "no free 3x3 spot off the ore on the base's edge"))


# ---- the robot network (roadmap G11, autopilot round 5) -----------------------------------------------------------------
ROBOPORT = "roboport"
ROBOPORT_LOGISTIC_R = 25        # 2.1: a roboport's logistic area is 50x50 tiles, its construction area 110x110 (engine-
ROBOPORT_CONSTRUCTION_R = 55    # checked: tests/live_robots.py RB2; older data exports have no radius fields)
ROBOPORT_LINK = 2               # neighbours' logistic areas overlap by at least this many tiles (one network)
ROBOPORTS_PER_STAGE = 4         # the robot mall makes one about every 3-4 game minutes and keeps 10 in its chest
ROBOPORT_STEP = 4               # candidate spots every 4 tiles around the roboports there are
NEED_STEP = 8                   # the built area is sampled every 8 tiles for coverage
HUB_STORAGE = 2                 # storage chests beside the first roboport (what the robots pick up goes there)
HUB_R = 60                      # the first roboport: within this many tiles of the hub (a dense hub has no 6x6 nearer)
MALL_WEIGHT = 6                 # a mall chest brought into the logistic area counts like 6 built-area points
ROBOPORT_KW = 50.0              # its drain; charging robots draws up to 2 MW more (power growth, R8, covers it)


def roboport_radii(gd) -> tuple:
    """(logistic radius, construction radius) of a roboport: the data export's when it has them (export_data.lua since
    round 5), else 2.1's 25 / 55."""
    p = gd.entity_or_empty(ROBOPORT) if hasattr(gd, "entity_or_empty") else {}
    return (int(p.get("logistic_radius") or ROBOPORT_LOGISTIC_R),
            int(p.get("construction_radius") or ROBOPORT_CONSTRUCTION_R))


def roboports_of(mp, world=None) -> list:
    """(x, y) of every roboport the base has or plans: the master's (any stage) and the map's (the player's own and
    ghosts; the master's built ones are on the map too, counted once)."""
    out = [(e.x, e.y) for e in mp.ents if e.name == ROBOPORT]
    for e in (getattr(world, "entities", None) or ()):
        if e.name in (ROBOPORT, "ghost:" + ROBOPORT) and not any(abs(e.x - x) < 2 and abs(e.y - y) < 2 for x, y in out):
            out.append((e.x, e.y))
    return out


def _need_points(gd, ents) -> list:
    """The built area to cover: the tiles of the given entities, sampled every NEED_STEP tiles (one point per cell that
    holds something: a sprawled base's empty ground between its blocks needs no coverage)."""
    cells = {}
    for e in ents:
        for tx, ty in _tiles(gd, e):
            cells.setdefault((tx // NEED_STEP, ty // NEED_STEP), (tx + 0.5, ty + 0.5))
    return sorted(cells.values())


def roboport_coverage(mp, gd, world=None, k=None) -> dict:
    """{"n": roboports, "points": built-area points, "uncovered": points no roboport's construction area reaches,
    "mall_out": mall chests outside every logistic area} for the master's entities alive at k (default: every stage):
    what the next roboport stage has to cover (masters summary for R12; repeatable as the base grows)."""
    ents = [e for e in mp.ents if k is None or M.alive(e, k)]
    pts = _need_points(gd, ents)
    log, con = roboport_radii(gd)
    have = roboports_of(mp, world)
    unc = [p for p in pts if not any(max(abs(p[0] - x), abs(p[1] - y)) <= con for x, y in have)]
    out = [e for e in ents if _x(e).get("role") == "mall_chest" and
           not any(max(abs(e.x - x), abs(e.y - y)) <= log for x, y in have)]
    return {"n": len(have), "points": len(pts), "uncovered": len(unc), "mall_out": len(out)}


def _robots(mp, world, gd, ctx: _Ctx, k, researched):
    """The robot mall (roadmap G11): blocks.robot_block beside the plate bus (iron and copper from bus taps downstream of
    the mall's), steel out of the steel column's depot and plastic out of the oil plant's (an inserter and a belt
    each, as the blue block's), crude oil branched off the oil plant's crude pipes to its own refinery (advanced oil
    processing: the lubricant needs heavy oil) and water from an offshore pump of its own. Its product chests (robots,
    roboports, storage and passive provider chests) are the helper's mall sources. Every feed is routed per block
    spot; a spot where one fails is given up for the next."""
    bus_ents = _bus_ents(mp)
    if not bus_ents:
        raise ExpandError("robots: the master has no plate bus yet (the steel stage lays it)")
    steel, plastic = _depots(mp, "steel-plate"), _depots(mp, "plastic-bar")
    if not steel:
        raise ExpandError("robots: no steel depot (the steel stage)")
    if not plastic:
        raise ExpandError("robots: no plastic depot (the oil plant)")
    crude = [e for e in mp.ents if _x(e).get("role") == "crude_pipe" and e.name == "pipe"]
    if not crude:
        raise ExpandError("robots: no crude oil pipe to branch from (the oil plant pipes it from the pumpjacks)")
    blk = B.robot_block(gd)
    labels = ["fe", "st", "cu", "pl", "water", "crude"]
    part = M.Part("robot_block", [copy.deepcopy(e) for e in blk.entities], blk.size, dict(zip(labels, blk.ports)), [])
    alive = [e for e in mp.ents if M.alive(e, k)]
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    bus = _downstream(mp, bus_ents)
    _bus_rows(ctx, bus_ents, release=True)
    pts = {}
    for label, item in (("fe", "iron-plate"), ("cu", "copper-plate")):
        pts[label] = np.array([c[3][-1] for c in tap_candidates(ctx, bus, item, clear=2)] or
                              [c[3][-1] for c in tap_candidates(ctx, bus, item)], float)
        if not len(pts[label]):
            raise ExpandError(f"robots: no free tap spot for {item} on the bus")
    # steel and plastic: out of their depot chests, or - when the blue stage's feeds took the chests' free sides (2002
    # hub: the plastic chest has none left) - by a tap on a master belt that carries only that item (those feeds)
    items_all = belt_items(alive, gd, M.world_without_master(world, mp, k))
    for label, item, chests in (("st", "steel-plate", steel), ("pl", "plastic-bar", plastic)):
        pts[label] = np.array([[e.x, e.y] for e in chests] + [c[3][-1] for c in tap_candidates(ctx, items_all, item)],
                              float)
    pts["crude"] = np.array([[e.x, e.y] for e in crude], float)
    shore = [(x, y) for body in world.water_bodies(30) for x, y, _d in world.shoreline(body)
             if ctx.grid.in_bounds(x, y)]
    if not shore:
        raise ExpandError("robots: no water within reach for the refinery and the acid plant")
    shore.sort(key=lambda t: abs(t[0] - hub[0]) + abs(t[1] - hub[1]))
    pts["water"] = np.array(shore[:400:4] or shore[:1], float)
    weight = {"fe": 1.0, "cu": 1.0, "st": 0.5, "pl": 0.5, "crude": 0.4, "water": 0.2}

    def score(rp, X, Y):
        sc = np.zeros(len(X))
        for label, P in pts.items():
            p = rp.ports[label]
            dx, dy = (X + p.x)[:, None] - P[None, :, 0], (Y + p.y)[:, None] - P[None, :, 1]
            near = np.argmin(np.abs(dx) + np.abs(dy), 1)
            sc = sc + weight[label] * np.min(np.abs(dx) + np.abs(dy), 1)
            vx, vy = DVEC[p.direction]       # a source behind the port means a route around the block (as _blue)
            ahead = -(dx[np.arange(len(X)), near] * vx + dy[np.arange(len(X)), near] * vy)
            sc = sc + weight[label] * np.maximum(0, ahead)
        return sc + 0.1 * (np.abs(X + rp.size[0] / 2 - hub[0]) + np.abs(Y + rp.size[1] / 2 - hub[1]))
    allp = np.concatenate([P for P in pts.values() if len(P)])
    win = (int(allp[:, 0].min()) - 60, int(allp[:, 1].min()) - 60, int(allp[:, 0].max()) + 60,
           int(allp[:, 1].max()) + 60)
    _bus_rows(ctx, bus_ents, release=False)          # no block on the bus's tap rows
    spots = _spots(ctx, part, score, win, n=8, mirror_ok=False, shape=blk.info["shape"])
    _bus_rows(ctx, bus_ents, release=True)
    reasons = []
    kinds = ("belt_in", "belt_out", "pipe_in", "pipe_out")
    for sc, _m, rot, placed in spots:
        base, n_new, n_oth = ctx.grid.copy(), len(ctx.new), len(ctx.others)
        ctx.add(placed.ents)
        ctx.grid.release([(p.x, p.y) for p in placed.ports.values()])
        P = placed.ports
        got, why = {}, None
        steps = (("fe", lambda: _tap_feed(ctx, bus, "iron-plate", P["fe"], n_ins=1, max_len=BLUE_FEED_MAX, clear=2,
                                          wide=True)),
                 ("cu", lambda: _tap_feed(ctx, bus, "copper-plate", P["cu"], n_ins=1, max_len=BLUE_FEED_MAX, clear=2,
                                          wide=True)),
                 ("st", lambda: _chest_feed(ctx, steel, "steel-plate", P["st"]) or
                  _tap_feed(ctx, items_all, "steel-plate", P["st"], max_len=BLUE_FEED_MAX, wide=True)),
                 ("pl", lambda: _chest_feed(ctx, plastic, "plastic-bar", P["pl"]) or
                  _tap_feed(ctx, items_all, "plastic-bar", P["pl"], max_len=BLUE_FEED_MAX, wide=True)),
                 ("crude", lambda: _petroleum_feed(ctx, mp, P["crude"], role="crude_pipe", fluid="crude-oil",
                                                   out_role="robot_crude")),
                 ("water", lambda: _water_feed(ctx, world, P["water"])))
        for label, fn in steps:
            with _guarded(ctx, P, label, kinds):
                f = fn()
            if f is None:
                why = {"fe": "no iron tap route", "cu": "no copper tap route", "st": "no steel feed from its depot",
                       "pl": "no plastic feed from the oil plant's depot", "crude": "no crude oil pipe route",
                       "water": "no water pipe from a shore"}[label]
                break
            got[label] = f
        if why is None:
            zones = [M._shift_rect(M._rot_rect(r, blk.size, rot), placed.ents[0].x - part.rotated(rot).ents[0].x,
                                   placed.ents[0].y - part.rotated(rot).ents[0].y) for r in blk.info["shape"]]
            zones = [tuple(int(round(v)) for v in z) for z in zones]
            chests = [[e.x, e.y] for e in placed.ents if _x(e).get("role") == "mall_chest"]
            return {"ents": list(ctx.new), "zone": zones, "feeds": [got[l][1] for l in ("fe", "cu", "st", "pl")],
                    "fluids": [got["crude"][1], got["water"][1]], "chests": chests, "score": round(sc, 1),
                    "items": list(blk.info["items"]), "rate": blk.rates.get("flying-robot-frame")}
        reasons.append(f"{placed.ents[0].x:.0f},{placed.ents[0].y:.0f}: {why}")
        ctx.grid = base
        ctx.new, ctx.others = ctx.new[:n_new], ctx.others[:n_oth]
    raise ExpandError("robots: " + ("; ".join(reasons[:4]) or "no free ground for the block near the bus"))


def _roboport_ok(ctx: _Ctx, x, y) -> bool:
    """A roboport with its top-left tile at (x, y): its 4x4 and a walkway tile around it free, no ore under it."""
    g = ctx.grid
    for tx in range(x - 1, x + 5):
        for ty in range(y - 1, y + 5):
            if not ctx.free((tx, ty)):
                return False
            if 0 <= tx - x < 4 and 0 <= ty - y < 4 and g.res[ty - g.y0, tx - g.x0] != 0:
                return False
    return True


def _roboports(mp, world, gd, ctx: _Ctx, k, researched):
    """The roboport grid (repeatable): up to ROBOPORTS_PER_STAGE roboports that cover the master's built area with
    their construction areas (110x110 in 2.1), each one's logistic area (50x50) overlapping one of the roboports there
    are so they form ONE network. The first stage puts the first roboport by the mall (the mall's chests, else the
    hub) with HUB_STORAGE storage chests beside it (the robots' store; the helper puts the robot mall's construction
    robots into that roboport); every later one picks, among free spots linked to the network, the one that covers
    the most of the built area still uncovered (sampled every NEED_STEP tiles) - a mall chest it brings into the
    network's logistic area counts MALL_WEIGHT points (only those become passive providers the robots take from) -
    nearest the hub among equals. The stage's poles join the master's network (extend). Called again as the base
    grows: it raises ExpandError when everything is covered."""
    alive = [e for e in mp.ents if M.alive(e, k)]
    if not alive:
        raise ExpandError("roboports: the master has nothing built to power a roboport from")
    hub = mp.hub or (sum(e.x for e in alive) / len(alive), sum(e.y for e in alive) / len(alive))
    log_r, con_r = roboport_radii(gd)
    link_r = 2 * log_r - ROBOPORT_LINK
    have = roboports_of(mp, world)
    need = _need_points(gd, alive)
    unc = [p for p in need if not any(max(abs(p[0] - x), abs(p[1] - y)) <= con_r for x, y in have)]
    malls = [(e.x, e.y) for e in alive if _x(e).get("role") == "mall_chest"]
    out_mall = [c for c in malls if not any(max(abs(c[0] - x), abs(c[1] - y)) <= log_r for x, y in have)]
    first = not have
    if not first and not unc and not out_mall:
        raise ExpandError("roboports: the robots' construction areas cover the whole base already (and the network "
                          "the mall)")
    hx, hy = math.floor(hub[0]), math.floor(hub[1])
    placed, chests = [], []

    def gain_at(cx, cy):
        return (sum(1 for p in unc if max(abs(p[0] - cx), abs(p[1] - cy)) <= con_r) +
                MALL_WEIGHT * sum(1 for c in out_mall if max(abs(c[0] - cx), abs(c[1] - cy)) <= log_r))
    while len(placed) < ROBOPORTS_PER_STAGE and (unc or out_mall or not (have or placed)):
        net = have + [(e.x, e.y) for e in placed]
        seen, spots = set(), []

        def consider(t):
            if t in seen:
                return
            seen.add(t)
            cx, cy = t[0] + 2, t[1] + 2
            if net and not any(max(abs(cx - x), abs(cy - y)) <= link_r for x, y in net):
                return                                           # (not linked to the network)
            g = gain_at(cx, cy)
            if g or not net:
                spots.append((-g + 0.001 * (abs(cx - hub[0]) + abs(cy - hub[1])), t))
        if not net:            # the first: the spot near the hub that reaches the most mall chests (they become the
            for x in range(hx - HUB_R, hx + HUB_R + 1, 2):      # network's passive providers) and built area
                for y in range(hy - HUB_R, hy + HUB_R + 1, 2):
                    consider((x, y))
        for rx, ry in net:     # around the network, every ROBOPORT_STEP tiles
            bx, by = math.floor(rx) - 2, math.floor(ry) - 2              # top-left of that roboport
            for dx in range(-link_r, link_r + 1, ROBOPORT_STEP):
                for dy in range(-link_r, link_r + 1, ROBOPORT_STEP):
                    consider((bx + dx, by + dy))
        for c in out_mall:     # right around the mall chests still outside the logistic area, every 2 tiles (a mall
            for dx in range(-log_r + 2, log_r - 1, 2):           # stands in a dense part of the base)
                for dy in range(-log_r + 2, log_r - 1, 2):
                    consider((math.floor(c[0]) - 2 + dx, math.floor(c[1]) - 2 + dy))
        spots.sort()
        pick = None
        for _s, (x, y) in spots:          # (best first; most spots by a mall stand on the base: cheap to test)
            if _roboport_ok(ctx, x, y):
                pick = (x, y)
                break
        if pick is None:
            if not placed:
                raise ExpandError("roboports: no free 4x4 spot " + ("near the hub" if first else
                                                                     "linked to the robot network"))
            break
        x, y = pick
        r = PE(ROBOPORT, x + 2.0, y + 2.0, NORTH, None, {"role": "roboport", "unit": len(have) + len(placed)})
        ctx.add([r])
        placed.append(r)
        unc = [p for p in unc if max(abs(p[0] - r.x), abs(p[1] - r.y)) > con_r]
        out_mall = [c for c in out_mall if max(abs(c[0] - r.x), abs(c[1] - r.y)) > log_r]
        if first and len(placed) == 1:
            # the robots' store: storage chests on free tiles right beside the first roboport
            ring = [(x + i, y - 2) for i in range(4)] + [(x + i, y + 5) for i in range(4)] + \
                   [(x - 2, y + j) for j in range(4)] + [(x + 5, y + j) for j in range(4)]
            for t in ring:
                if len(chests) >= HUB_STORAGE:
                    break
                if ctx.free(t) and all(ctx.free((t[0] + a, t[1] + b)) for a, b in DVEC.values()):
                    c = PE("storage-chest", t[0] + 0.5, t[1] + 0.5, NORTH, None, {"role": "robot_storage"})
                    ctx.add([c])
                    chests.append(c)
    return {"ents": list(ctx.new), "zone": [M._ents_rect(gd, [e]) for e in placed + chests], "feeds": [],
            "chests": [], "roboports": [[e.x, e.y] for e in placed], "storage": [[c.x, c.y] for c in chests],
            "first": first, "uncovered": len(unc)}


DESIGN = {"mall": _mall, "power_grow": _power_grow, "steel": _steel, "science": _science, "oil": _oil,
          "oil_plant": _oil_plant, "iron_grow": _grow("iron-ore"), "copper_grow": _grow("copper-ore"),
          "mall_add": _mall_add, "radar": _radar, "blue": _blue, "robots": _robots, "roboports": _roboports}


def _free_zones(mp, template) -> tuple:
    """Zones an expansion builds inside (their ground is its own): the power block's for power_grow, the mall's for
    mall_add."""
    if template == "power_grow":
        return ("power",)
    if template == "mall_add":
        mall_k = templates_built(mp).get("mall")
        return tuple(z.id for z in mp.zones if z.first_stage == mall_k and z.kind != "bus")
    return ()


def extend(mp, world, gd, template, *, after=None, keepout=(), researched=None, validate=True, hints=None) -> dict:
    """Append one expansion stage for `template` to master plan `mp` (in place) and return {stage, label, key,
    entities, feeds, chests, ...}. after: the last built stage (the mall is put right after it when later starter
    stages are not built yet). hints: what the game knows beyond the export for the template (radar: the chunk
    survey {"octants": [...]} and {"toward": [resource]}). Raises ExpandError (the master unchanged) when the
    template does not fit."""
    if template not in DESIGN:
        raise ExpandError(f"unknown expansion template {template!r}")
    if template in templates_built(mp) and template not in REPEATABLE:
        raise ExpandError(f"{template}: this master has it already")
    if template == "power_grow" and after is not None:
        # the power block's units are chained (water and coal pass through the boilers before them): new units come
        # after the last stage that adds a unit of its own
        last = max((float(_x(e).get("stage") or 0) for e in mp.ents if _x(e).get("zone") == "power"
                    and _x(e).get("role") == "boiler"), default=0.0)
        after = max(float(after), last)
    k = _stage_index(mp, template, after)
    if researched is None:
        researched = [n for n, t in (gd.raw.get("technologies") or {}).items() if t.get("researched")]
    snap = (list(mp.ents), list(mp.zones), list(mp.stages), copy.deepcopy(mp.stats))
    err = None
    for shape in SHAPES:        # strict routes first; relaxed only when nothing fits that way
        ctx = _Ctx(mp, world, gd, keepout, free_zones=_free_zones(mp, template),
                   margin=WIDE_MARGIN if template in WIDE_TEMPLATES else MARGIN)
        ctx.shape = shape
        ctx.hints = dict(hints or {})
        try:
            info = DESIGN[template](mp, world, gd, ctx, k, researched)
            break
        except ExpandError as e:
            err = e
            if template in ("power_grow", "radar"):      # (no routes: a relaxed shape changes nothing)
                raise
    else:
        raise err
    info["route_limits"] = list(ctx.shape)
    if template not in ("power_grow", "radar"):      # (the radar routes its poles per spot it tries)
        _poles(ctx, k)
    ents = list(ctx.new)
    after_k = max((s.index for s in mp.stages if s.index < k), default=None)
    gate = info.get("gate") or GATES[template]
    unlocks = {"mall": "the helper takes belts, inserters, drills, assemblers, gears and circuits from the mall "
                       "instead of hand-crafting them",
               "power_grow": f"{2 * len(info.get('units') or [])} more steam engines "
                             f"({0.9 * 2 * len(info.get('units') or []):.1f} MW)",
               "iron_grow": "about 225 more iron plates per minute onto the bus",
               "copper_grow": "about 75 more copper plates per minute onto the bus",
               "steel": "steel plates for medium poles, steel furnaces and assembling machines 2",
               "science": "30 more red and 30 more green science packs per minute into 10 more labs",
               "oil": "crude oil pumped: that finishes Oil processing (refinery and chemical plant recipes)",
               "oil_plant": "petroleum gas and ~50 plastic per minute (Advanced circuit, Sulfur processing, Chemical "
                            "science pack research next)",
               "mall_add": "the mall makes " + ", ".join(info.get("items") or []) + " too (the helper takes them "
                           "there instead of hand-crafting them)",
               "radar": f"the {2 * radar_reach(gd)[0] + 1}x{2 * radar_reach(gd)[0] + 1} chunks around the radar "
                        f"stay charted (patches and nests current) and it scans farther out ({radar_kw(gd):.0f} kW)",
               "blue": f"about {info.get('rate') or 13:.0f} chemical science packs per minute into the labs (sulfur "
                       f"from petroleum gas and water, engines from steel, advanced circuits from plastic)",
               "robots": f"about {info.get('rate') or 2:.1f} flying robot frames per minute: construction robots first, "
                         f"then logistic robots, plus roboports, storage and passive provider chests (the helper takes "
                         f"them from its chests)",
               "roboports": f"{len(info.get('roboports') or [])} roboport(s): construction robots build the "
                            f"autopilot's ghosts in their {2 * roboport_radii(gd)[1]}x{2 * roboport_radii(gd)[1]} areas "
                            f"(the helper only walks to the rest)"
                            + (", storage chests beside the first" if info.get("storage") else "")
               }[template]
    label = f"{M.slabel(k)} {TITLES[template]}"
    _append_stage(mp, k, template, gate, after_k, unlocks, ents, info.get("zone"), label)
    if template == "power_grow":
        for e in mp.ents[-len(ents):]:
            e.extra["zone"] = info.get("zone_id", "power")
    if template in ("blue", "robots"):
        # the depot-chest inserters, the oil header's pipe and the lab feed stand in other stages' reserved room:
        # they belong to that zone (as mall_add's machines do; the robot mall's depot feeds and crude branch too)
        own = {z.id for z in mp.zones if z.first_stage == k}
        old = [z for z in mp.zones if z.id not in own and z.kind != "bus"]
        for e in mp.ents[-len(ents):]:
            ts = _tiles(gd, e)
            z = next((z for z in old if any(z.rect[0] <= t[0] < z.rect[2] and z.rect[1] <= t[1] < z.rect[3]
                                            for t in ts)), None)
            if z is not None:
                e.extra["zone"] = z.id
    if template == "mall_add":          # the new slot stands in the mall's own zone (its reserved room)
        own = [z for z in mp.zones if z.id in _free_zones(mp, template)]
        for e in mp.ents[-len(ents):]:
            z = next((z for z in own if z.rect[0] <= e.x < z.rect[2] and z.rect[1] <= e.y < z.rect[3]), None)
            if z is not None:
                e.extra["zone"] = z.id
    if validate:
        try:
            rep = _validate(mp, gd, world, k, template)
        except ExpandError:
            _rollback(mp, snap)
            raise
        issues = M.check_monotone(mp, gd)
        if issues:
            _rollback(mp, snap)
            raise ExpandError(f"{template}: {issues[0]['text']}")
        info["warnings"] = len(rep.warnings)
    mp.stats.setdefault("version", 1)
    mp.stats["version"] = int(mp.stats["version"]) + 1
    if info.get("chests"):
        mp.stats.setdefault("mall_chests", [])
        mp.stats["mall_chests"] = (mp.stats["mall_chests"] + info["chests"])[:64]
    belt_tiles = sum(f.get("belt_tiles", 0) for f in info.get("feeds") or [])
    return {"stage": k, "label": label, "key": mp.stages[[s.index for s in mp.stages].index(k)].key,
            "entities": len(ents), "feeds": info.get("feeds"), "chests": info.get("chests"),
            "items": info.get("items"), "feed_belt_tiles": belt_tiles, "units": info.get("units"),
            "zone": info.get("zone"), "warnings": info.get("warnings"), "radar": info.get("radar"),
            "dir": info.get("dir"), "roboports": info.get("roboports"), "storage": info.get("storage"),
            "uncovered": info.get("uncovered")}
