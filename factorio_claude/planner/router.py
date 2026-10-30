"""Belt and pipe routing (A* on the tile Grid) and electric pole placement.

    ents = route_belt(grid, gd, start_port, goal_port)        # [PE], marks the tiles in `grid`
    ents = route_pipe(grid, gd, start_port, goal_port)
    poles = place_poles(grid, gd, entities, "small-electric-pole", sources=[existing_pole_pe])

Ports (see blocks.Port): x, y = the tile just outside the block where the belt/pipe must sit, direction = flow
direction (belt_out/pipe_out: items leave the block moving `direction` onto (x, y); belt_in/pipe_in: the last belt
at (x, y) faces `direction` into the block). `lane` ("left"/"right", relative to the flow) asks for the items on one
lane of the goal belt (done by side-loading).

Engine facts (verified in 2.1.23 on a test server): underground belts pair when their positions differ by at most
max_underground_distance (yellow 5 = 4 tiles gap), input and output both face the flow direction, type "input" /
"output" is in PE.extra["type"]. Pipe-to-ground pairs up to 10 apart; its normal connection is on its `direction`
side, so the entrance faces back against the flow and the exit faces along it.

Tiles this module occupies get the tag "<prefix>:<entity>@<dir>[in|out]" (prefix "route", "pole"), so later calls
on the same grid see which way earlier belts point. Pass `others` (planned or existing entities as PE, see
world_others) so routes never point into / get fed by foreign belts or touch foreign fluid connections.
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass

from .gamedata import rotate
from .grid import BLOCKED, CLEARABLE, Grid

try:  # shared types live in blocks.py (layout agent); fall back to identical local ones until it exists
    from .blocks import PE, Port  # type: ignore
except ImportError:  # pragma: no cover - depends on build order
    @dataclass
    class PE:
        name: str
        x: float
        y: float
        direction: int = 0
        recipe: str | None = None
        extra: dict | None = None

    @dataclass
    class Port:
        kind: str
        item: str | None
        x: float
        y: float
        direction: int
        lane: str | None = None

NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12
VEC = {NORTH: (0, -1), EAST: (1, 0), SOUTH: (0, 1), WEST: (-1, 0)}
DIRS = (NORTH, EAST, SOUTH, WEST)
BELT_TYPES = {"transport-belt", "underground-belt", "splitter", "loader", "loader-1x1", "linked-belt", "lane-splitter"}
PIPE_TYPES = {"pipe", "pipe-to-ground", "infinity-pipe"}

# default costs (tiles): every belt/pipe tile costs 1
TURN = 0.5          # per belt turn (keeps routes straight)
CLEAR = 3.0         # per tree/rock tile that must be deconstructed
ORE = 0.25          # per ore tile (keep patches free for future mining)
UNDERGROUND = 4.0   # per underground pair, on top of the tiles it spans


class NoRoute(ValueError):
    """No valid route / pole layout. `.detail` may carry extra info (e.g. uncovered entities)."""

    def __init__(self, msg, detail=None):
        super().__init__(msg)
        self.detail = detail


def rev(d: int) -> int:
    return (d + 8) % 16


def left(d: int) -> int:
    return (d + 12) % 16


def right(d: int) -> int:
    return (d + 4) % 16


def _get(o, k, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def _tile_of(o) -> tuple[int, int]:
    return math.floor(_get(o, "x")), math.floor(_get(o, "y"))


def _pe(name, tx, ty, direction=0, extra=None) -> PE:
    return PE(name=name, x=tx + 0.5, y=ty + 0.5, direction=direction, extra=extra)


def _tag_info(tag):
    """'existing:ghost:pipe' / 'route:transport-belt@4' -> (entity name, dir or None, 'in'|'out'|None)."""
    if not tag:
        return None, None, None
    body = tag.rsplit(":", 1)[-1]
    if "@" not in body:
        return body, None, None
    name, d = body.split("@", 1)
    io = "in" if d.endswith("in") else "out" if d.endswith("out") else None
    return name, int(d.rstrip("inout") or 0), io


def underground_for(gd, belt: str) -> tuple[str, int] | None:
    """(underground entity, max position difference) matching the belt tier, or None."""
    speed = gd.entity(belt).get("belt_speed")
    for n, e in gd.raw["entities"].items():
        if e.get("type") == "underground-belt" and e.get("belt_speed") == speed and not e.get("hidden"):
            return n, int(e.get("max_underground_distance") or 0)
    return None


def underground_pair(gd, name: str, tx: int, ty: int, direction: int, length: int) -> list[PE]:
    """Input at tile (tx, ty) and output `length` tiles further along `direction` (cross(...) helper)."""
    md = int(gd.entity(name).get("max_underground_distance") or 0)
    if not 1 <= length <= md:
        raise ValueError(f"{name}: length {length} not in 1..{md}")
    vx, vy = VEC[direction]
    if gd.entity(name).get("type") == "pipe-to-ground":
        return [_pe(name, tx, ty, rev(direction)), _pe(name, tx + vx * length, ty + vy * length, direction)]
    return [_pe(name, tx, ty, direction, {"type": "input"}),
            _pe(name, tx + vx * length, ty + vy * length, direction, {"type": "output"})]


cross = underground_pair  # contract name: crossing a bus / obstacle with one underground pair


def world_others(world, rect=None) -> list[PE]:
    """Existing force entities (incl. ghosts, as their inner entity) as PE, for the `others` argument."""
    out = []
    for e in (world.entities_in(rect) if rect is not None else world.entities):
        name = e.name[6:] if e.name.startswith("ghost:") else e.name
        extra = {"type": e.extra["ug"]} if isinstance(e.extra, dict) and e.extra.get("ug") else None
        out.append(PE(name=name, x=e.x, y=e.y, direction=int(e.dir or 0), extra=extra))
    return out


# --------------------------------------------------------------------------------------------------------------
# what foreign entities do to neighbouring tiles
class _Env:
    def __init__(self, grid: Grid, gd, others):
        self.grid, self.gd = grid, gd
        self.belt_dir: dict = {}      # tile -> dir of a foreign belt-like entity there
        self.feeds: set = set()       # tiles some foreign belt outputs into
        self.ug: set = set()          # tiles with a foreign underground belt / pipe-to-ground (name kept)
        self.ug_names: dict = {}
        self.fluid: set = set()       # tiles where a pipe would connect to something foreign
        self.pipes: set = set()
        self.reach: set = set()       # pickup/drop tiles of existing inserters and drop tiles of existing drills
        for o in others or ():
            self._add_pe(o)
        self._add_grid_tags()

    def _etype(self, name):
        try:
            return self.gd.entity(name).get("type")
        except KeyError:
            return None

    def _add_belt(self, name, t, d, io):
        typ = self._etype(name)
        self.belt_dir[t] = d
        if typ == "underground-belt":
            self.ug.add(t)
            self.ug_names[t] = name
            if io == "in":
                return
        vx, vy = VEC[d]
        self.feeds.add((t[0] + vx, t[1] + vy))

    def _add_pe(self, o):
        name = _get(o, "name")
        typ = self._etype(name)
        x, y, d = _get(o, "x"), _get(o, "y"), int(_get(o, "direction") or 0) % 16
        if typ in BELT_TYPES:
            if typ == "splitter":  # 2 wide across the flow: both halves output forward
                px, py = VEC[right(d)]
                for s in (-0.5, 0.5):
                    self._add_belt(name, (math.floor(x + px * s), math.floor(y + py * s)), d, None)
                return
            extra = _get(o, "extra") or {}
            io = {"input": "in", "output": "out"}.get(extra.get("type")) if isinstance(extra, dict) else None
            self._add_belt(name, (math.floor(x), math.floor(y)), d, io)
            return
        if typ == "pipe-to-ground":
            t = (math.floor(x), math.floor(y))
            self.ug.add(t)
            self.ug_names[t] = name
        if typ == "pipe":
            self.pipes.add((math.floor(x), math.floor(y)))
        try:
            if typ and self.gd.entity(name).get("fluidboxes"):
                for tx, ty, _d in self.gd.fluid_connections(name, x, y, d):
                    self.fluid.add((tx, ty))
        except (KeyError, ValueError):
            pass

    def _add_reach(self, g, tid, name, typ, d):
        """Existing inserters / drills: a belt on their pickup or drop tile would mix foreign items in."""
        p = self.gd.entity(name)
        if typ == "inserter":
            ins = p.get("inserter") or {}
            vecs = [ins.get("pickup", (0, -1)), ins.get("drop", (0, 1.2))]
        else:
            vecs = [p.get("vector_to_place_result") or (0, 0)]
        ii, jj = (g.tag == tid).nonzero()
        if not len(ii):
            return
        # entity centers from the tagged tiles (several same-named entities may share the tag): every w x h block
        # of tagged tiles counts, which may add a few extra tiles to avoid next to adjacent drills
        tiles = set(zip((g.x0 + jj).tolist(), (g.y0 + ii).tolist()))
        w, h = self.gd.size(name, d)
        for tx, ty in tiles:
            if any((tx + i, ty + j) not in tiles for i in range(w) for j in range(h)):
                continue
            cx, cy = tx + w / 2, ty + h / 2
            for vx, vy in vecs:
                rx, ry = rotate(vx, vy, d)
                self.reach.add((math.floor(cx + rx), math.floor(cy + ry)))

    def _add_grid_tags(self):
        g = self.grid
        for tid, tag in enumerate(g.tags):
            if not tag:
                continue
            name, d, io = _tag_info(tag)
            typ = self._etype(name)
            if tag.startswith("existing:") and d is not None and typ in ("inserter", "mining-drill"):
                self._add_reach(g, tid, name, typ, d)
                continue
            if typ not in BELT_TYPES and typ not in PIPE_TYPES:
                continue
            ii, jj = (g.tag == tid).nonzero()
            for i, j in zip(ii.tolist(), jj.tolist()):
                t = (g.x0 + j, g.y0 + i)
                if typ in BELT_TYPES:
                    if d is not None and t not in self.belt_dir:
                        self._add_belt(name, t, d, io)
                    elif typ == "underground-belt":
                        self.ug.add(t)
                        self.ug_names.setdefault(t, name)
                elif typ == "pipe":
                    self.pipes.add(t)
                elif typ == "pipe-to-ground":
                    self.ug.add(t)
                    self.ug_names.setdefault(t, name)
                    if d is not None:  # our own: normal side known
                        vx, vy = VEC[d]
                        self.fluid.add((t[0] + vx, t[1] + vy))
                    else:  # unknown direction: avoid all neighbours
                        for vx, vy in VEC.values():
                            self.fluid.add((t[0] + vx, t[1] + vy))
        for px, py in self.pipes:
            for vx, vy in VEC.values():
                self.fluid.add((px + vx, py + vy))


# --------------------------------------------------------------------------------------------------------------
def _search(grid, start, start_dir, terminals, walkable, step_cost, jump, turn_cost, max_nodes):
    """A* over (x, y, arrival_dir). terminals: {(x, y, d): extra_cost} reached by moving onto them (the terminal
    tile itself gets no entity). jump(x, y, d) -> [(X, cost)] underground pairs: entrance at (x, y), exit at X,
    continuing at X + d. Returns [(tile, out_dir, kind)] where kind is 'b' or ('u', exit_tile)."""
    tx = [t[0] for t in terminals]
    ty = [t[1] for t in terminals]

    def h(x, y):
        return float(min(abs(x - a) + abs(y - b) for a, b in zip(tx, ty)))

    sx, sy = start
    if not walkable(sx, sy):
        raise NoRoute(f"start tile {start} is not free")
    s = (sx, sy, start_dir)
    g = {s: step_cost(sx, sy)}
    came = {s: None}
    cnt = itertools.count()
    heap = [(g[s] + h(sx, sy), next(cnt), s)]
    best_end, best_cost = None, math.inf
    expanded = 0
    while heap:
        f, _, n = heapq.heappop(heap)
        if f >= best_cost:
            break
        gn = g[n]
        if f > gn + h(n[0], n[1]) + 1e-9:
            continue
        expanded += 1
        if expanded > max_nodes:
            break
        x, y, d = n
        for o in (d, left(d), right(d)):
            vx, vy = VEC[o]
            m = (x + vx, y + vy, o)
            c = gn + (turn_cost if o != d else 0.0)
            if m in terminals:
                tc = c + terminals[m]
                if tc < best_cost:
                    best_cost, best_end = tc, (n, ("b", o))
                continue
            if not walkable(m[0], m[1]):
                continue
            c += step_cost(m[0], m[1])
            if c < g.get(m, math.inf):
                g[m] = c
                came[m] = (n, ("b", o))
                heapq.heappush(heap, (c + h(m[0], m[1]), next(cnt), m))
        if jump is not None:
            vx, vy = VEC[d]
            for (X, jc) in jump(x, y, d):
                m = (X[0] + vx, X[1] + vy, d)
                c = gn + jc
                if m in terminals:
                    tc = c + terminals[m]
                    if tc < best_cost:
                        best_cost, best_end = tc, (n, ("u", X))
                    continue
                if not walkable(m[0], m[1]):
                    continue
                c += step_cost(m[0], m[1])
                if c < g.get(m, math.inf):
                    g[m] = c
                    came[m] = (n, ("u", X))
                    heapq.heappush(heap, (c + h(m[0], m[1]), next(cnt), m))
    if best_end is None:
        raise NoRoute(f"no route from {start} (searched {expanded} nodes)")
    steps = []
    n, mv = best_end
    while n is not None:
        steps.append(((n[0], n[1]), mv))
        prev = came[n]
        if prev is None:
            break
        n, mv = prev
    steps.reverse()
    return steps, best_cost


def _check_path(steps):
    """Tiles used once; underground spans don't contain our own entrance/exit tiles on the same axis."""
    used = {}
    for (t, mv) in steps:
        for tt in ([t] if mv[0] == "b" else [t, mv[1]]):
            if tt in used:
                return False
            used[tt] = mv
    for (t, mv) in steps:
        if mv[0] == "u":
            X = mv[1]
            dx, dy = (X[0] > t[0]) - (X[0] < t[0]), (X[1] > t[1]) - (X[1] < t[1])
            k = abs(X[0] - t[0]) + abs(X[1] - t[1])
            for i in range(1, k):
                m = used.get((t[0] + dx * i, t[1] + dy * i))
                if m is not None and m[0] == "u":
                    return False
            # also our other undergrounds' exits inside this span
            for (t2, mv2) in steps:
                if mv2[0] == "u" and t2 != t:
                    for p in (t2, mv2[1]):
                        if (p[0] - t[0]) * dy == (p[1] - t[1]) * dx and 0 < (p[0] - t[0]) * dx + (p[1] - t[1]) * dy < k:
                            return False
    return True


def _make_walk(grid: Grid, avoid, clear_cost, ore_cost, tile_cost=None):
    def walkable(x, y):
        st = grid.state_at(x, y)
        return st != BLOCKED and (x, y) not in avoid

    def step_cost(x, y):
        c = 1.0
        if grid.state_at(x, y) == CLEARABLE:
            c += clear_cost
        if ore_cost and grid.resource(x, y):
            c += ore_cost
        if tile_cost is not None and grid.in_bounds(x, y):
            c += float(tile_cost[y - grid.y0, x - grid.x0])
        return c
    return walkable, step_cost


def _span_ok(grid, env, x, y, d, k, ug_name):
    """Tiles strictly between entrance and exit: known map and no same-kind underground that would intercept."""
    vx, vy = VEC[d]
    for i in range(1, k):
        t = (x + vx * i, y + vy * i)
        if not grid.in_bounds(*t) or grid.void[t[1] - grid.y0, t[0] - grid.x0]:
            return False
        if t in env.ug and env.ug_names.get(t) == ug_name:
            return False
    return True


def _occupy(grid, ents, prefix):
    for e in ents:
        io = ""
        if isinstance(e.extra, dict) and e.extra.get("type") in ("input", "output"):
            io = "in" if e.extra["type"] == "input" else "out"
        grid.occupy([(math.floor(e.x), math.floor(e.y))], f"{prefix}:{e.name}@{e.direction}{io}")


# --------------------------------------------------------------------------------------------------------------
def route_belt(grid: Grid, gd, start, goal, belt: str = "transport-belt", allow_underground=None, others=None,
               tag: str = "route", turn_cost: float = TURN, clear_cost: float = CLEAR, ore_cost: float = ORE,
               underground_cost: float = UNDERGROUND, max_nodes: int = 400_000, tile_cost=None,
               occupy: bool = True) -> list[PE]:
    """Belt from port `start` to port `goal` -> [PE] (belts, underground pairs, maybe a lane blocker belt).
    tile_cost: optional array over the grid ([y - y0, x - x0]) added to every step onto that tile (negotiated
    routing: congestion and history costs). occupy=False leaves the grid unmarked.

    The goal is side-loaded when its tile already holds a belt (existing, earlier route, or a belt in `others`):
    the route ends next to it pointing in (lane "left"/"right" picks the side, else any side or from behind).
    When goal.lane is set and differs from start.lane on a plain belt_in port, the route ends with a side-load onto
    the goal tile plus a blocker belt behind it, so every item lands on that lane.
    Undergrounds only if allow_underground, or (None) the matching underground belt is buildable.
    Marks the used tiles in `grid`; raises NoRoute."""
    env = _Env(grid, gd, others)
    S, G = _tile_of(start), _tile_of(goal)
    sd, gdir = int(_get(start, "direction")) % 16, int(_get(goal, "direction")) % 16
    lane = _get(goal, "lane")
    ug = underground_for(gd, belt)
    if allow_underground is None:
        allow_underground = bool(ug) and gd.can_build(ug[0])
    if not ug:
        allow_underground = False

    avoid = set(env.feeds) | env.reach
    extra_ents: list[PE] = []
    terminals: dict = {}
    gname, tag_dir, _io = _tag_info(grid.tag_at(*G))
    goal_is_belt = G in env.belt_dir or (grid.state_at(*G) == BLOCKED and gname and
                                         (gd.raw["entities"].get(gname) or {}).get("type") in ("transport-belt", "underground-belt"))
    if goal_is_belt:
        bd = env.belt_dir.get(G, gdir)
        if tag_dir is not None:
            bd = tag_dir
        sides = {"left": [right(bd)], "right": [left(bd)]}.get(lane, [right(bd), left(bd)])
        behind = (G[0] - VEC[bd][0], G[1] - VEC[bd][1])
        fed_from_behind = env.belt_dir.get(behind) == bd and behind in env.belt_dir
        if lane and not fed_from_behind and grid.state_at(*behind) != BLOCKED:
            # nothing feeds the goal from behind: a side input would just curve it; add a blocker belt
            extra_ents.append(_pe(belt, behind[0], behind[1], bd, {"role": "lane_blocker"}))
            avoid.add(behind)
        for a in sides:
            terminals[(G[0], G[1], a)] = 0.0
        if not lane and not fed_from_behind:
            terminals[(G[0], G[1], bd)] = 0.0
    elif lane and lane != _get(start, "lane"):
        behind = (G[0] - VEC[gdir][0], G[1] - VEC[gdir][1])
        for t in (G, behind):
            if grid.state_at(*t) == BLOCKED or t in env.feeds:
                raise NoRoute(f"lane {lane}: tiles {G} and {behind} must be free for the side-load")
        extra_ents += [_pe(belt, G[0], G[1], gdir, {"role": "lane_target"}),
                       _pe(belt, behind[0], behind[1], gdir, {"role": "lane_blocker"})]
        avoid |= {G, behind}
        a = right(gdir) if lane == "left" else left(gdir)
        terminals[(G[0], G[1], a)] = 0.0
    else:
        vx, vy = VEC[gdir]
        terminals[(G[0] + vx, G[1] + vy, gdir)] = 0.0
    avoid.discard(S)  # the block's own output belt feeds the start tile
    walkable, step_cost = _make_walk(grid, avoid, clear_cost, ore_cost, tile_cost)
    if S == G and not goal_is_belt and not extra_ents:
        if sd == rev(gdir):
            raise NoRoute("start and goal on the same tile face each other")
        ents = [_pe(belt, S[0], S[1], gdir)]
        if occupy:
            _occupy(grid, ents, tag)
        return ents

    jump = None
    if allow_underground:
        ug_name, md = ug

        def jump(x, y, d):
            out = []
            vx, vy = VEC[d]
            for k in range(2, md + 1):
                X = (x + vx * k, y + vy * k)
                if not _span_ok(grid, env, x, y, d, k, ug_name):
                    break
                if walkable(*X):
                    out.append((X, step_cost(*X) + (k - 1) + underground_cost))
            return out

    steps, _cost = _search(grid, S, sd, terminals, walkable, step_cost, jump, turn_cost, max_nodes)
    if not _check_path(steps):
        raise NoRoute("route crosses itself")
    ents = []
    for t, mv in steps:
        if mv[0] == "b":
            ents.append(_pe(belt, t[0], t[1], mv[1]))
        else:
            X = mv[1]
            d = _dir_between(t, X)
            ents += [_pe(ug[0], t[0], t[1], d, {"type": "input"}), _pe(ug[0], X[0], X[1], d, {"type": "output"})]
    ents += extra_ents
    if occupy:
        _occupy(grid, ents, tag)
    return ents


def _dir_between(a, b):
    if b[0] > a[0]:
        return EAST
    if b[0] < a[0]:
        return WEST
    return SOUTH if b[1] > a[1] else NORTH


def route_pipe(grid: Grid, gd, start, goal, pipe: str = "pipe", allow_underground=None, others=None,
               tag: str = "route", turn_cost: float = 0.1, clear_cost: float = CLEAR, ore_cost: float = ORE,
               underground_cost: float = UNDERGROUND, max_nodes: int = 400_000) -> list[PE]:
    """Pipe from port `start` to port `goal` -> [PE] (pipes, pipe-to-ground pairs).

    Pipes never sit next to foreign pipes or on foreign fluid connection tiles (fluids would mix); pipe-to-ground
    entrances/exits only connect along the route, so they may pass close to foreign fluid entities. If the goal tile
    holds a pipe already, the route ends next to it. Pipe-to-ground only if allow_underground or (None) buildable."""
    env = _Env(grid, gd, others)
    S, G = _tile_of(start), _tile_of(goal)
    sd = int(_get(start, "direction") or 0) % 16
    gdir = int(_get(goal, "direction") or 0) % 16
    ptg = "pipe-to-ground" if pipe == "pipe" else None
    if ptg and ptg not in gd.raw["entities"]:
        ptg = None
    if allow_underground is None:
        allow_underground = bool(ptg) and gd.can_build(ptg)
    if not ptg:
        allow_underground = False
    md = int(gd.entity(ptg).get("max_underground_distance") or 0) if ptg else 0

    gname, _d, _io = _tag_info(grid.tag_at(*G))
    goal_is_pipe = G in env.pipes or (grid.state_at(*G) == BLOCKED and gname == pipe)
    exempt = {S, G}
    if goal_is_pipe:
        exempt |= {(G[0] + vx, G[1] + vy) for vx, vy in VEC.values()}
    avoid = env.fluid - exempt
    walkable, step_cost = _make_walk(grid, avoid, clear_cost, ore_cost)
    walk_ug, _ = _make_walk(grid, set(), clear_cost, ore_cost)  # ptg tiles only connect along the route

    terminals = {(G[0], G[1], d): 0.0 for d in DIRS} if goal_is_pipe else {}
    if S == G and not goal_is_pipe:
        ents = [_pe(pipe, S[0], S[1])]
        _occupy(grid, ents, tag)
        return ents

    jump = None
    if allow_underground:
        def jump(x, y, d):
            out = []
            vx, vy = VEC[d]
            if (x, y) != S and not walk_ug(x, y):
                return out
            for k in range(2, md + 1):
                X = (x + vx * k, y + vy * k)
                if not _span_ok(grid, env, x, y, d, k, ptg):
                    break
                if X == G and not goal_is_pipe and d == gdir:  # exit ptg feeds the goal entity directly
                    out.append((X, 1.0 + (k - 1) + underground_cost))
                elif walk_ug(*X) and X != G:
                    out.append((X, step_cost(*X) + (k - 1) + underground_cost))
            return out

    def walkable2(x, y):
        return walkable(x, y) or (x, y) == G and not goal_is_pipe and walk_ug(x, y)

    terms = terminals
    if not goal_is_pipe:  # the last pipe sits on G: stepping off G in any direction ends the route
        terms = {(G[0] + VEC[d][0], G[1] + VEC[d][1], d): 0.0 for d in DIRS}
    steps, _cost = _search(grid, S, sd, terms, walkable2, step_cost, jump, turn_cost, max_nodes)
    # drop the virtual "move beyond G" for non-terminal neighbours: the path must end with G
    if not goal_is_pipe and steps and steps[-1][0] != G and not (steps[-1][1][0] == "u" and steps[-1][1][1] == G):
        raise NoRoute("pipe route did not end at the goal")
    if not _check_path(steps):
        raise NoRoute("route crosses itself")
    ents = []
    for t, mv in steps:
        if mv[0] == "b":
            ents.append(_pe(pipe, t[0], t[1]))
        else:
            X = mv[1]
            d = _dir_between(t, X)
            ents += [_pe(ptg, t[0], t[1], rev(d)), _pe(ptg, X[0], X[1], d)]
    _occupy(grid, ents, tag)
    return ents


# --------------------------------------------------------------------------------------------------------------
def _is_electric(gd, name) -> bool:
    try:
        e = gd.entity(name)
    except KeyError:
        return False
    es = e.get("energy_source")
    return isinstance(es, dict) and es.get("type") == "electric" and e.get("type") != "electric-pole"


def _covers(box, cx, cy, r) -> bool:
    return box[0] < cx + r and box[2] > cx - r and box[1] < cy + r and box[3] > cy - r


def place_poles(grid: Grid, gd, entities, pole: str = "small-electric-pole", sources=None, tag: str = "pole",
                clear_cost: float = CLEAR, max_poles: int = 2000) -> list[PE]:
    """Poles so that every electric entity in `entities` (consumers and generators) is inside some pole's supply
    area and all poles form one network that includes `sources` (existing poles as PE, e.g. at the power plant).
    Greedy set cover (most new consumers, preferring spots in wire reach of the network), then Prim-style MST:
    the nearest unconnected group is joined by the shortest chain of extra poles. Marks pole tiles in `grid`;
    raises NoRoute (detail = uncovered entities) if something can't be powered."""
    supply, reach = gd.pole(pole)
    pw, ph = gd.size(pole)
    sources = list(sources or [])
    work = grid.copy()
    for e in entities:  # never put a pole under a planned entity, even if the caller hasn't occupied it yet
        try:
            work.occupy(gd.footprint(e.name, e.x, e.y, e.direction or 0), "planned")
        except (KeyError, ValueError):
            pass

    def spot_ok(tx, ty):
        return all(work.state_at(tx + i, ty + j) != BLOCKED for i in range(pw) for j in range(ph))

    def spot_cost(tx, ty):
        return sum(clear_cost for i in range(pw) for j in range(ph) if work.state_at(tx + i, ty + j) == CLEARABLE)

    def center(tx, ty):
        return tx + pw / 2, ty + ph / 2

    src = []  # (cx, cy, supply, reach)
    for s in sources:
        ss, sr = gd.pole(s.name)
        src.append((s.x, s.y, ss, sr))

    consumers = []
    for e in entities:
        if _is_electric(gd, e.name):
            box = gd.box(e.name, e.x, e.y, e.direction or 0)
            if not any(_covers(box, sx, sy, ss) for sx, sy, ss, _ in src):
                consumers.append((e, box))

    # candidate spots per consumer
    cover: dict = {}
    for k, (e, box) in enumerate(consumers):
        for tx in range(math.floor(box[0] - supply - pw), math.ceil(box[2] + supply) + 1):
            for ty in range(math.floor(box[1] - supply - ph), math.ceil(box[3] + supply) + 1):
                cx, cy = center(tx, ty)
                if _covers(box, cx, cy, supply) and spot_ok(tx, ty):
                    cover.setdefault((tx, ty), set()).add(k)
    uncovered = set(range(len(consumers)))
    reachable = set().union(*cover.values()) if cover else set()
    if uncovered - reachable:
        bad = [consumers[k][0] for k in sorted(uncovered - reachable)]
        raise NoRoute(f"{len(bad)} electric entities have no free spot for a pole nearby", bad)

    net = [(sx, sy, sr) for sx, sy, _, sr in src]
    chosen: list[tuple[int, int]] = []

    def net_dist(cx, cy):
        return min((math.hypot(cx - a, cy - b) - min(r, reach) for a, b, r in net), default=0.0)

    while uncovered:
        best, best_key = None, None
        for spot, ks in cover.items():
            n = len(ks & uncovered)
            if not n:
                continue
            cx, cy = center(*spot)
            nd = net_dist(cx, cy)
            key = (n, nd <= 1e-9, -max(nd, 0.0), -spot_cost(*spot))
            if best_key is None or key > best_key:
                best, best_key = spot, key
        chosen.append(best)
        uncovered -= cover.pop(best)
        cx, cy = center(*best)
        net.append((cx, cy, reach))
        for i in range(pw):
            for j in range(ph):
                work.occupy([(best[0] + i, best[1] + j)], "pole")
        if len(chosen) > max_poles:
            raise NoRoute("too many poles")

    chosen += _connect(work, gd, pole, chosen, src, reach, pw, ph, spot_ok, center, max_poles)
    ents = [PE(name=pole, x=center(*t)[0], y=center(*t)[1], direction=0) for t in chosen]
    for p in ents:
        grid.occupy(gd.footprint(pole, p.x, p.y, 0), f"{tag}:{pole}@0")
    return ents


def _connect(work, gd, pole, chosen, src, reach, pw, ph, spot_ok, center, max_poles):
    """Extra poles joining all chosen poles and sources into one wire network (Prim over components)."""
    import numpy as np
    from scipy.spatial import cKDTree

    nodes = [(center(*t)[0], center(*t)[1], reach) for t in chosen] + [(x, y, r) for x, y, _, r in src]
    if len(nodes) <= 1:
        return []
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    maxr = max(n[2] for n in nodes)
    added = []
    ir = int(math.floor(reach))
    offsets = sorted(((dx, dy) for dx in range(-ir - 1, ir + 2) for dy in range(-ir - 1, ir + 2)
                      if 0 < math.hypot(dx, dy) <= reach + 1e-9), key=lambda o: -math.hypot(*o))
    while True:
        pts = np.array([(n[0], n[1]) for n in nodes])
        for i, j in cKDTree(pts).query_pairs(maxr + 1e-9):
            if math.hypot(*(pts[i] - pts[j])) <= min(nodes[i][2], nodes[j][2]) + 1e-9:
                union(i, j)
        roots = {find(i) for i in range(len(nodes))}
        if len(roots) <= 1:
            return added
        # grow from the component holding the sources (or the first pole)
        root = find(len(chosen)) if src else find(0)
        inside = [i for i in range(len(nodes)) if find(i) == root]
        outside = [i for i in range(len(nodes)) if find(i) != root]
        otree = cKDTree(pts[outside])

        def h(cx, cy):
            d, _ = otree.query((cx, cy))
            return max(0, math.ceil((d - reach) / reach - 1e-9))

        def hit(cx, cy):  # an outside node within wire reach of a new pole here
            for k in otree.query_ball_point((cx, cy), maxr + 1e-9):
                j = outside[k]
                if math.hypot(cx - pts[j][0], cy - pts[j][1]) <= min(reach, nodes[j][2]) + 1e-9:
                    return j
            return None

        # A* over new pole spots: cost = number of poles added
        cnt = itertools.count()
        heap, g, came = [], {}, {}
        for i in inside:
            x, y, r = nodes[i]
            for dx, dy in offsets:
                tx, ty = math.floor(x - pw / 2 + dx + 1e-9), math.floor(y - ph / 2 + dy + 1e-9)
                cx, cy = center(tx, ty)
                if math.hypot(cx - x, cy - y) > min(r, reach) + 1e-9 or (tx, ty) in g or not spot_ok(tx, ty):
                    continue
                g[(tx, ty)] = 1
                came[(tx, ty)] = None
                heapq.heappush(heap, (1 + h(cx, cy), next(cnt), (tx, ty)))
        found = None
        expanded = 0
        while heap:
            f, _, t = heapq.heappop(heap)
            cx, cy = center(*t)
            j = hit(cx, cy)
            if j is not None:
                found = (t, j)
                break
            expanded += 1
            if expanded > 200_000:
                break
            for dx, dy in offsets:
                n = (t[0] + dx, t[1] + dy)
                if n in g or not spot_ok(*n):
                    continue
                g[n] = g[t] + 1
                came[n] = t
                ncx, ncy = center(*n)
                heapq.heappush(heap, (g[n] + h(ncx, ncy), next(cnt), n))
        if found is None:
            raise NoRoute("can't connect all poles into one network (water or obstacles in the way)")
        t, j = found
        chain = []
        while t is not None:
            chain.append(t)
            t = came[t]
        for t in chain:
            added.append(t)
            for i in range(pw):
                for k in range(ph):
                    work.occupy([(t[0] + i, t[1] + k)], "pole")
            nodes.append((center(*t)[0], center(*t)[1], reach))
            parent.append(len(parent))
        union(j, root)
        if len(added) > max_poles:
            raise NoRoute("too many poles")
