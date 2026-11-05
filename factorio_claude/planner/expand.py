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
TEMPLATES = ("mall", "power_grow", "steel", "science", "oil", "oil_plant")
STAGE_KEYS = {"mall": "x_mall", "power_grow": "x_power", "steel": "x_steel", "science": "x_science", "oil": "x_oil",
              "oil_plant": "x_oilplant"}
TITLES = {"mall": "Mall (belts, inserters, drills, assemblers from assemblers)", "power_grow": "More steam power",
          "steel": "Steel smelting on the plate bus",
          "science": "Second red + green science block (+30/min each) and more iron",
          "oil": "Pumpjacks on crude oil", "oil_plant": "Oil refinery and plastic"}
GATES = {"mall": ["automation", "electric-mining-drill", "logistics"], "power_grow": [], "steel": ["steel-processing"],
         "science": ["logistic-science-pack", "logistics"], "oil": ["oil-gathering", "fluid-handling"],
         "oil_plant": ["oil-processing", "plastics"]}
WIDE_MARGIN = 150               # steel / science may lay the bus and put its smelting cells on a farther patch
WIDE_TEMPLATES = ("steel", "science", "oil", "oil_plant")


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


def _route(ctx: _Ctx, start: Port, goal: Port, tag="exp", shape_check=True):
    """A belt from port `start` to port `goal` on the expansion grid, or None. shape_check: a route that snakes
    (doctrine ORG-1, see proportional) counts as no route, so the caller tries its next spot or tap (round 3 judge:
    nested feeder loops across the map)."""
    g = ctx.grid
    S, G = (int(start.x), int(start.y)), (int(goal.x), int(goal.y))
    dist = abs(S[0] - G[0]) + abs(S[1] - G[1])
    budget = min(250_000, 4000 + 600 * dist) if _reachable(g, S, G) else min(20_000, 2000 + 100 * dist)
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


def _tap_feed(ctx: _Ctx, items_at, item, goal: Port, n_ins=1, max_len=160, clear=0):
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
        route = _route(ctx, start, goal)
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


def _spots(ctx: _Ctx, part: M.Part, score, window, n=SPOT_TRIES, avoid_ore=3.0, mirror_ok=True):
    """Best placements of `part` (all 8 orientations): [(score, mirror, k, placed Part)], the block plus GAP tiles
    around it and its reserve rects on placeable ground."""
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
    blocked: a route must never end on, or side-load into, another input's belt."""

    def __init__(self, ctx: _Ctx, ports: dict, keep):
        self.ctx, self.tiles = ctx, []
        for label, p in ports.items():
            if label == keep or p.kind not in ("belt_in", "belt_out"):
                continue
            vx, vy = DVEC[p.direction]
            if p.kind == "belt_in":
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


DESIGN = {"mall": _mall, "power_grow": _power_grow, "steel": _steel, "science": _science, "oil": _oil,
          "oil_plant": _oil_plant}


def extend(mp, world, gd, template, *, after=None, keepout=(), researched=None, validate=True) -> dict:
    """Append one expansion stage for `template` to master plan `mp` (in place) and return {stage, label, key,
    entities, feeds, chests, ...}. after: the last built stage (the mall is put right after it when later starter
    stages are not built yet). Raises ExpandError (the master unchanged) when the template does not fit."""
    if template not in DESIGN:
        raise ExpandError(f"unknown expansion template {template!r}")
    if template in templates_built(mp) and template != "power_grow":
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
        ctx = _Ctx(mp, world, gd, keepout, free_zones=("power",) if template == "power_grow" else (),
                   margin=WIDE_MARGIN if template in WIDE_TEMPLATES else MARGIN)
        ctx.shape = shape
        try:
            info = DESIGN[template](mp, world, gd, ctx, k, researched)
            break
        except ExpandError as e:
            err = e
            if template == "power_grow":
                raise
    else:
        raise err
    info["route_limits"] = list(ctx.shape)
    if template != "power_grow":
        _poles(ctx, k)
    ents = list(ctx.new)
    after_k = max((s.index for s in mp.stages if s.index < k), default=None)
    gate = GATES[template]
    unlocks = {"mall": "the helper takes belts, inserters, drills, assemblers, gears and circuits from the mall "
                       "instead of hand-crafting them",
               "power_grow": f"{2 * len(info.get('units') or [])} more steam engines "
                             f"({0.9 * 2 * len(info.get('units') or []):.1f} MW)",
               "steel": "steel plates for medium poles, steel furnaces and assembling machines 2",
               "science": "30 more red and 30 more green science packs per minute into 10 more labs",
               "oil": "crude oil pumped: that finishes Oil processing (refinery and chemical plant recipes)",
               "oil_plant": "petroleum gas and ~50 plastic per minute (Advanced circuit, Sulfur processing, Chemical "
                            "science pack research next)"}[template]
    label = f"{M.slabel(k)} {TITLES[template]}"
    _append_stage(mp, k, template, gate, after_k, unlocks, ents, info.get("zone"), label)
    if template == "power_grow":
        for e in mp.ents[-len(ents):]:
            e.extra["zone"] = info.get("zone_id", "power")
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
            "zone": info.get("zone"), "warnings": info.get("warnings")}
