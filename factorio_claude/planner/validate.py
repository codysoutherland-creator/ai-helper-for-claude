"""Static validator for planned layouts (SPEC-v3-contracts.md). Blocks placement when it reports errors.

    rep = validate(entities, gd, world_or_grid, expect)
    rep.ok, rep.errors, rep.warnings, rep.stats        # Issue{kind, x, y, msg}

entities: PE-like objects (name, x, y, direction, recipe, extra), dicts with the same keys, or [name, x, y, dir,
recipe] lists. Underground belts give their half in extra["type"|"ug"|"belt_to_ground_type"] = "input"/"output".
world_or_grid: World (terrain + existing force entities are used as context), Grid (terrain; existing entities only
as opaque tiles), or None (no terrain checks).

expect (all optional):
  ports           [Port|dict{kind, item?, x, y, direction?, lane?, per_min?}] block ports: belt_in = items enter
                  the belt there, belt_out = declared sink, pipe_in/pipe_out = fluid enters/leaves, pole = the pole
                  network there is fed from outside. x, y = tile just outside the block (or the edge entity's tile).
  sources/sinks/fluid_sources/fluid_sinks   same as the belt_in/belt_out/pipe_in/pipe_out ports
  power           "external": every pole network counts as fed from outside (only coverage is checked)
  manual_fuel     True (all burners are fuelled by hand) | [entity names] | [(x, y)]; a PE's own
                  extra {"fuel": "manual"} counts too
  containers      {(x, y): [items]} known chest contents (otherwise unfilled chests count as "unknown contents")
  rates           {item: per_min} planned output; also turns throughput shortfalls into errors
  strict          True: throughput shortfalls (inserters, power, fluids) are errors instead of warnings
  strict_fuel     True: fuel supply shortfalls on belts (incl. what burner inserters burn) are errors
  connect_existing True: planned belts/inserters may feed into / take from existing lines (otherwise an error)
  assume_research True: entities/recipes that aren't unlocked are warnings (requires_research), not errors

Checks: unknown/unbuildable entities and recipes, grid alignment, overlaps, terrain (water/void/cliffs/existing
entities), drills on minable ore, offshore pumps on a shoreline, belt graph (head-on belts, belts into splitter sides
or underground exits, underground pairing, dead ends, starved pickups, lane overloads), inserter pickup/drop targets,
machine inputs/outputs, fluid networks (gaps, pipe-to-ground pairing, mixed fluids, sources), power (pole coverage,
networks without a generator, capacity), fuel paths to every burner, throughput vs the plan.
All rates are items per second unless a message says /min.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .gamedata import DIR_VEC, EPS, GameData, rotate, tiles_of_box
from .grid import BLOCKED, CLEARABLE, Grid

OPP = {0: 8, 4: 12, 8: 0, 12: 4}
DIR_NAME = {0: "north", 4: "east", 8: "south", 12: "west"}
BELT_TYPES = {"transport-belt", "underground-belt", "splitter", "lane-splitter", "loader", "loader-1x1", "linked-belt"}
CRAFTER_TYPES = {"assembling-machine", "furnace", "rocket-silo"}
CONTAINER_TYPES = {"container", "logistic-container", "linked-container", "infinity-container", "proxy-container",
                   "cargo-wagon", "car", "spider-vehicle", "cargo-landing-pad", "space-platform-hub"}
GENERATOR_TYPES = {"generator", "solar-panel", "burner-generator", "fusion-generator", "electric-energy-interface",
                   "lightning-attractor"}
OUTPUT_PRIORITIES = {"secondary-output", "primary-output", "solar", "tertiary"}
LANES = ("left", "right")


@dataclass
class Issue:
    kind: str
    x: float
    y: float
    msg: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "x": self.x, "y": self.y, "msg": self.msg}


@dataclass
class Report:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": [i.to_dict() for i in self.errors],
                "warnings": [i.to_dict() for i in self.warnings], "stats": self.stats}

    def summary(self, n: int = 8) -> str:
        """Short plain-text summary (for logs / Claude)."""
        head = f"{'OK' if self.ok else 'FAILED'}: {len(self.errors)} errors, {len(self.warnings)} warnings"
        lines = [head] + [f"error {i.kind}: {i.msg}" for i in self.errors[:n]]
        lines += [f"warning {i.kind}: {i.msg}" for i in self.warnings[:max(0, n - len(self.errors))]]
        return "\n".join(lines)


def validate(entities, gd: GameData, world_or_grid=None, expect: dict | None = None) -> Report:
    return _Validator(gd, world_or_grid, expect or {}).run(list(entities))


# ---- small helpers ---------------------------------------------------------------------------------------------
def _p(x, y) -> str:
    return f"({_n(x)}, {_n(y)})"


def _n(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _get(o, k, default=None):
    if isinstance(o, dict):
        return o.get(k, default)
    return getattr(o, k, default)


def _tile(px, py) -> tuple[int, int]:
    return math.floor(px + EPS), math.floor(py + EPS)


def _items_txt(items, n=4) -> str:
    items = sorted(i for i in items if i != "*")
    return ", ".join(items[:n]) + (f" +{len(items) - n}" if len(items) > n else "") if items else "nothing"


def _xy_key(v):
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return (float(v[0]), float(v[1]))
    return None


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


class _R:
    """One entity (planned or existing context)."""
    __slots__ = ("i", "name", "x", "y", "dir", "recipe", "extra", "p", "type", "existing", "tiles", "box", "io",
                 "inputs", "outputs", "fuel_in", "recipes", "node", "label")

    def __init__(self, i, name, x, y, d, recipe, extra, proto, existing):
        self.i, self.name, self.x, self.y, self.dir = i, name, float(x), float(y), d
        self.recipe, self.extra, self.p, self.existing = recipe, extra or {}, proto, existing
        self.type = proto.get("type", "")
        self.tiles, self.box, self.io = set(), None, None
        self.inputs, self.outputs, self.fuel_in, self.recipes = set(), set(), False, []
        self.node = None
        self.label = f"{name} at {_p(self.x, self.y)}"


class _Node:
    """A belt-graph node: a belt / underground tile, or a whole splitter."""
    __slots__ = ("r", "kind", "dir", "tiles", "outs", "ins", "out_tiles")

    def __init__(self, r, kind, d, tiles):
        self.r, self.kind, self.dir, self.tiles = r, kind, d, tiles
        self.outs = []      # [(node, mapping)] mapping: "keep" | 0 | 1 (all items onto that lane) |
        #                     (src, dst): only lane src gets through, onto lane dst (side-load onto an underground)
        self.ins = []       # [(node, mapping)]
        self.out_tiles = []


def _dst_lane(mp, ln):
    """Lane an item on lane ln ends up on across an edge with this mapping (None: it can't pass)."""
    if mp == "keep":
        return ln
    if isinstance(mp, tuple):
        return mp[1] if ln == mp[0] else None
    return mp


class _Validator:
    def __init__(self, gd: GameData, ctx, expect: dict):
        self.gd, self.ctx, self.ex = gd, ctx, expect
        self.world = ctx if ctx is not None and hasattr(ctx, "entities_in") else None
        self.grid: Grid | None = ctx if isinstance(ctx, Grid) else None
        self.errors, self.warnings = [], []
        self._seen = set()
        self.strict = bool(expect.get("strict") or expect.get("rates"))
        self.recs: list[_R] = []
        self.tilemap: dict = {}
        self.stats: dict = {}

    # ---- issue helpers ----------------------------------------------------------------------------------------
    def err(self, kind, r_or_xy, msg, warn=False):
        x, y = (r_or_xy.x, r_or_xy.y) if isinstance(r_or_xy, _R) else r_or_xy
        key = (kind, msg)
        if key in self._seen:
            return
        self._seen.add(key)
        (self.warnings if warn else self.errors).append(Issue(kind, x, y, msg))

    def warn(self, kind, r_or_xy, msg):
        self.err(kind, r_or_xy, msg, warn=True)

    def shortfall(self, kind, r, msg):
        self.err(kind, r, msg, warn=not self.strict)

    # ---- main -------------------------------------------------------------------------------------------------
    def run(self, entities) -> Report:
        self._load(entities)
        plan = [r for r in self.recs if not r.existing]
        if plan:
            self._context()
            self._placement()
            self._targets()
            self._belt_graph()
            self._items()
            self._rates()
            self._machine_io()
            self._fluids()
            self._power()
            self._fuel()
            self._expect_rates()
        self._stats()
        return Report(ok=not self.errors, errors=self.errors, warnings=self.warnings, stats=self.stats)

    # ---- loading ----------------------------------------------------------------------------------------------
    def _load(self, entities):
        assume = bool(self.ex.get("assume_research"))
        self.requires_research = set()
        for e in entities:
            if isinstance(e, (list, tuple)):
                name, x, y = e[0], e[1], e[2]
                d = e[3] if len(e) > 3 else 0
                recipe = e[4] if len(e) > 4 else None
                extra = e[5] if len(e) > 5 else None
            else:
                name, x, y = _get(e, "name"), _get(e, "x"), _get(e, "y")
                d = _get(e, "direction", None)
                d = _get(e, "dir", 0) if d is None else d
                recipe, extra = _get(e, "recipe"), _get(e, "extra")
            extra = dict(extra) if isinstance(extra, dict) else {}
            d = int(d or 0)
            pos = (float(x), float(y))
            proto = self.gd.raw["entities"].get(name)
            if proto is None:
                self.err("unknown_entity", pos, f"{name} at {_p(*pos)}: no such entity in this game")
                continue
            r = _R(len(self.recs), name, x, y, d, recipe, extra, proto, False)
            if d % 4 or not 0 <= d < 16:
                self.err("position", r, f"{r.label}: direction {d} is not north/east/south/west (0/4/8/12)")
                continue
            r.io = self._io(r)
            try:
                r.box = self.gd.box(name, r.x, r.y, d)
            except ValueError as exc:
                self.warn("unsupported", r, f"{r.label}: {exc}; not checked")
                continue
            r.tiles = tiles_of_box(*r.box)
            sx, sy = self.gd.snap(name, r.x, r.y, d)
            if abs(sx - r.x) > 1e-6 or abs(sy - r.y) > 1e-6:
                w, h = self.gd.size(name, d)
                self.err("position", r, f"{r.label} is off-grid: a {w}x{h} entity must be centered at "
                                        f"{'x.5' if w % 2 else 'whole x'} / {'y.5' if h % 2 else 'whole y'} "
                                        f"(e.g. {_p(sx, sy)})")
            if not self.gd.can_build(name):
                self.requires_research.add(name)
                self.err("research", r, f"{name} isn't buildable yet: no unlocked recipe makes it", warn=assume)
            if recipe:
                rc = self.gd.raw["recipes"].get(recipe)
                if rc is None:
                    self.err("recipe", r, f"{r.label}: unknown recipe {recipe}")
                    r.recipe = None
                elif r.type not in CRAFTER_TYPES:
                    self.err("recipe", r, f"{r.label}: a {r.type} can't have a recipe ({recipe})")
                    r.recipe = None
                else:
                    if not set(rc["categories"]) & set(proto.get("crafting_categories") or []):
                        self.err("recipe", r, f"{r.label} can't craft {recipe} (category "
                                              f"{'/'.join(rc['categories'])})")
                    if not self.gd.enabled(recipe):
                        self.requires_research.add(recipe)
                        self.err("research", r, f"{r.label}: recipe {recipe} isn't unlocked yet", warn=assume)
            self.recs.append(r)

    @staticmethod
    def _io(r):
        if r.type != "underground-belt":
            return None
        for k in ("type", "ug", "belt_to_ground_type", "io"):
            v = r.extra.get(k)
            if v in ("input", "output"):
                return v
        return "input"

    def _context(self):
        """Grid for terrain + existing entities (World only) as context records."""
        plan = [r for r in self.recs if not r.existing]
        xs = [t[0] for r in plan for t in r.tiles] or [0]
        ys = [t[1] for r in plan for t in r.tiles] or [0]
        rect = (min(xs) - 12, min(ys) - 12, max(xs) + 13, max(ys) + 13)
        if self.world is not None:
            self.grid = self.world.grid(rect)
            here = {(r.name, r.x, r.y): r for r in plan}
            for me in self.world.entities_in(rect):
                name = me.name[6:] if me.name.startswith("ghost:") else me.name
                proto = self.gd.raw["entities"].get(name)
                if proto is None or (name, float(me.x), float(me.y)) in here:
                    continue
                r = _R(len(self.recs), name, me.x, me.y, int(me.dir or 0) % 16, (me.extra or {}).get("recipe"),
                       me.extra, proto, True)
                r.io = (me.extra or {}).get("ug") or "input"
                try:
                    r.box = self.gd.box(name, r.x, r.y, r.dir if r.dir % 4 == 0 else 0)
                except ValueError:
                    continue
                r.tiles = tiles_of_box(*r.box)
                self.recs.append(r)
        for r in self.recs:
            for t in r.tiles:
                self.tilemap.setdefault(t, []).append(r)

    def at(self, t) -> _R | None:
        rs = self.tilemap.get(t)
        return rs[0] if rs else None

    def _existing_tag(self, t) -> str | None:
        """Name of an existing entity on tile t known only from the grid (Grid context)."""
        if self.grid is None or self.world is not None:
            return None
        tag = self.grid.tag_at(*t)
        if tag and tag.startswith("existing:"):
            n = tag[9:].split("@", 1)[0]
            return n[6:] if n.startswith("ghost:") else n
        return None

    # ---- placement --------------------------------------------------------------------------------------------
    def _placement(self):
        g = self.grid
        clear = set()
        for t, rs in self.tilemap.items():
            plan = [r for r in rs if not r.existing]
            if len(rs) > 1 and plan:
                a, b = rs[0], rs[1]
                if b.existing and not a.existing:
                    a, b = b, a
                self.err("overlap", plan[0], f"{b.label} overlaps {a.label} at tile {_p(*t)}")
        for r in self.recs:
            if r.existing:
                continue
            if r.type == "offshore-pump":
                self._offshore(r)
            if r.type == "mining-drill":
                self._drill_area(r)
            if g is None:
                continue
            for t in sorted(r.tiles):
                if r.type == "offshore-pump" and t != _tile(r.x, r.y):
                    continue
                if g.is_water(*t):
                    self.err("terrain", r, f"{r.label} stands on water at tile {_p(*t)}")
                    break
                tag = g.tag_at(*t)
                if g.state_at(*t) == BLOCKED:
                    if tag and tag.startswith("existing:"):
                        n = tag[9:].split("@", 1)[0]
                        if n in (r.name, "ghost:" + r.name):
                            continue  # the same entity is already there (e.g. re-validating applied ghosts)
                        if self.world is None:  # with a World the existing entity is a record: reported as overlap
                            self.err("terrain", r, f"{r.label} overlaps existing {n} at tile {_p(*t)}")
                    else:
                        why = "a cliff/blocking obstacle" if not g.void[g._ij(*t)] else "out-of-map or unexplored ground"
                        self.err("terrain", r, f"{r.label} is blocked by {why} at tile {_p(*t)}")
                    break
                if g.state_at(*t) == CLEARABLE:
                    clear.update(g.to_clear([t]))
        self.to_clear = sorted(clear)

    def _offshore(self, r):
        g = self.grid
        if g is None:
            return
        tx, ty = _tile(r.x, r.y)
        fx, fy = DIR_VEC[r.dir]
        px, py = -fy, fx
        bad = [(tx + j * fx + k * px, ty + j * fy + k * py) for j in (1, 2) for k in (-1, 0, 1)
               if not g.is_water(tx + j * fx + k * px, ty + j * fy + k * py)]
        if g.is_water(tx, ty) or bad:
            why = f"its own tile {_p(tx, ty)} is water" if g.is_water(tx, ty) else \
                f"{len(bad)} of the 6 tiles in front of it ({DIR_NAME[r.dir]}) aren't water, e.g. {_p(*bad[0])}"
            self.err("offshore", r, f"{r.label} is not on a shoreline: {why}. It must stand on land facing "
                                    f"water with a 3-wide x 2-deep block of water in front")

    def _drill_area(self, r):
        p = r.p
        rad = p.get("mining_drill_radius") or 0
        area = tiles_of_box(r.x - rad, r.y - rad, r.x + rad, r.y + rad)
        r.extra["_area"] = area
        if self.grid is None:
            r.outputs = {"*"}
            return
        counts = defaultdict(int)
        for t in area:
            n = self.grid.resource(*t)
            if n:
                counts[n] += 1
        cats = set(p.get("resource_categories") or [])
        ok = {n: c for n, c in counts.items() if (self.gd.raw["resources"].get(n) or {}).get("category") in cats}
        if not counts:
            self.err("drill_resource", r, f"{r.label} has no ore in its mining area "
                                          f"{_p(min(t[0] for t in area), min(t[1] for t in area))}.."
                                          f"{_p(max(t[0] for t in area), max(t[1] for t in area))}")
        elif not ok:
            self.err("drill_resource", r, f"{r.label} can't mine {_items_txt(counts)} (resource category)")
        elif len(ok) > 1:
            self.warn("drill_mixed", r, f"{r.label} covers {_items_txt(ok)}: its output will be mixed")
        res = max(ok, key=ok.get) if ok else None
        r.extra["_resource"] = res
        if res:
            r.extra["_resources"] = ok
            for n in ok:
                for pr in self.gd.raw["resources"][n].get("products") or []:
                    if pr.get("type", "item") == "item":
                        r.outputs.add(pr["name"])
            if (self.gd.raw["resources"][res].get("required_fluid")):
                self.warn("drill_fluid", r, f"{r.label}: {res} needs {self.gd.raw['resources'][res]['required_fluid']}"
                                            " piped in (not checked)")

    # ---- inserter / drill targets -----------------------------------------------------------------------------
    def _targets(self):
        """Pickup/drop tiles of inserters and drop tiles of drills."""
        self.inserters, self.drills = [], []
        for r in self.recs:
            if r.type == "inserter":
                ins = r.p.get("inserter") or {}
                px, py = rotate(*ins.get("pickup", (0, -1)), r.dir)
                dx, dy = rotate(*ins.get("drop", (0, 1.2)), r.dir)
                r.extra["_pick"] = (r.x + px, r.y + py)
                r.extra["_drop"] = (r.x + dx, r.y + dy)
                self.inserters.append(r)
            elif r.type == "mining-drill" and any(r.p.get("vector_to_place_result") or ()):  # pumpjacks: (0, 0)
                vx, vy = rotate(*r.p["vector_to_place_result"], r.dir)
                r.extra["_drop"] = (r.x + vx, r.y + vy)
                self.drills.append(r)

    # ---- belt graph -------------------------------------------------------------------------------------------
    def _belt_graph(self):
        self.nodes: list[_Node] = []
        self.node_at: dict = {}
        for r in self.recs:
            if r.type not in BELT_TYPES:
                continue
            if r.type in ("splitter", "lane-splitter"):
                n = _Node(r, "split", r.dir, sorted(r.tiles))
            elif r.type == "underground-belt":
                n = _Node(r, "ug_" + ("out" if r.io == "output" else "in"), r.dir, sorted(r.tiles))
            elif r.type == "transport-belt":
                n = _Node(r, "belt", r.dir, sorted(r.tiles))
            else:
                n = _Node(r, "end", r.dir, sorted(r.tiles))
            r.node = n
            self.nodes.append(n)
            for t in n.tiles:
                self.node_at[t] = n
        side_in = defaultdict(list)
        headon = set()
        for n in self.nodes:
            if n.kind == "ug_in":
                self._pair_underground(n)
                continue
            if n.kind == "end":
                continue
            vx, vy = DIR_VEC[n.dir]
            for t in n.tiles:
                o = (t[0] + vx, t[1] + vy)
                n.out_tiles.append(o)
                m = self.node_at.get(o)
                if m is None or m is n:
                    continue
                plan = not (n.r.existing and m.r.existing)
                if n.r.existing != m.r.existing and m.dir != OPP[n.dir] and not self.ex.get("connect_existing"):
                    self.err("existing_belt", (n if not n.r.existing else m).r,
                             f"{n.r.label}{' (existing)' if n.r.existing else ''} feeds into {m.r.label}"
                             f"{' (existing)' if m.r.existing else ''}: planned and existing belt lines would mix")
                if m.dir == n.dir:
                    if m.kind == "ug_out":
                        if plan:
                            self.err("belt_into_back", n.r, f"{n.r.label} runs into the back of underground exit "
                                                            f"{m.r.label}; items stop there")
                        continue
                    self._edge(n, m, "keep")
                elif m.dir == OPP[n.dir]:
                    a, b = sorted((n, m), key=lambda k: k.r.i)
                    if plan and frozenset((a.r.i, b.r.i)) not in headon:
                        headon.add(frozenset((a.r.i, b.r.i)))
                        self.err("belt_headon", a.r, f"{a.r.label} faces {DIR_NAME[a.dir]} into {b.r.label} facing "
                                                     f"{DIR_NAME[b.dir]}: head-on, nothing can pass")
                else:
                    if m.kind in ("split", "end"):
                        if plan:
                            self.err("belt_into_side", n.r, f"{n.r.label} runs into the side of {m.r.label}; "
                                                            f"items stop there")
                        continue
                    side_in[id(m)].append((n, m))
        # side inputs: a belt with exactly one side input and nothing from behind is a curve; else sideloads
        for lst in side_in.values():
            m = lst[0][1]
            back = any(mp == "keep" for _, mp in m.ins)
            for n, _ in lst:
                if m.kind == "belt" and not back and len(lst) == 1:
                    self._edge(n, m, "keep")
                else:
                    vx, vy = DIR_VEC[n.dir]
                    rx, ry = -DIR_VEC[m.dir][1], DIR_VEC[m.dir][0]
                    dst = 1 if (-vx * rx - vy * ry) > 0 else 0
                    if m.kind in ("ug_in", "ug_out"):
                        # 2.1.23: only the half of the underground tile without the hood takes side input (the
                        # back half of an entrance, the front half of an exit), so only the incoming lane on that
                        # side gets through; the other one backs up
                        mx, my = DIR_VEC[m.dir]
                        if m.kind == "ug_in":
                            mx, my = -mx, -my
                        nrx, nry = -vy, vx       # right of the incoming belt
                        src = 1 if nrx * mx + nry * my > 0 else 0
                        self._edge(n, m, (src, dst))
                        if not (n.r.existing and m.r.existing):
                            self.warn("ug_sideload", n.r, f"{n.r.label} side-loads onto {m.r.label}: only its "
                                                          f"{LANES[src]} lane gets through")
                    else:
                        self._edge(n, m, dst)
        for n in self.nodes:
            if n.kind == "ug_out" and not n.ins and not n.r.existing:
                self.err("underground", n.r, f"{n.r.label} (exit) has no underground entrance facing "
                                             f"{DIR_NAME[n.dir]} within {self._ug_dist(n.r)} tiles behind it")
        # components (for grouping messages)
        uf = _UF()
        for n in self.nodes:
            uf.find(id(n))
            for m, _ in n.outs:
                uf.union(id(n), id(m))
        self.comp = {id(n): uf.find(id(n)) for n in self.nodes}

    def _edge(self, a, b, mapping):
        a.outs.append((b, mapping))
        b.ins.append((a, mapping))

    def _ug_dist(self, r) -> int:
        return int(r.p.get("max_underground_distance") or self.gd.raw["utility"].get("max_underground_belt_distance")
                   or 5)

    def _pair_underground(self, n):
        vx, vy = DIR_VEC[n.dir]
        t = n.tiles[0]
        maxd = self._ug_dist(n.r)
        for k in range(1, maxd + 1):
            m = self.node_at.get((t[0] + k * vx, t[1] + k * vy))
            if m is None or m.r.name != n.r.name or m.kind not in ("ug_in", "ug_out"):
                continue
            if m.dir not in (n.dir, OPP[n.dir]):
                continue  # perpendicular undergrounds don't interfere (engine)
            if m.kind == "ug_out" and m.dir == n.dir:
                self._edge(n, m, "keep")
                n.out_tiles.append(m.tiles[0])
                if n.r.existing and not m.r.existing:
                    self.err("underground", m.r, f"{m.r.label} lands inside the underground span of existing "
                                                 f"{n.r.label} and would take over its pairing")
                return
            if n.r.existing and m.r.existing:
                return
            bad = n.r if not n.r.existing else m.r
            if m.dir != n.dir:  # the engine pairs with it anyway (flipping its type): both lines break
                self.err("underground", bad, f"{n.r.label} (entrance) would pair with the opposite-facing "
                                             f"{m.r.label}; both lines break")
            else:
                self.err("underground", bad, f"{n.r.label} (entrance) is cut off by another entrance {m.r.label} "
                                             f"before any exit")
            return
        if not n.r.existing:
            self.err("underground", n.r, f"{n.r.label} (entrance, {DIR_NAME[n.dir]}) has no matching exit within "
                                         f"{maxd} tiles")

    def _lane_of(self, node, pt) -> int:
        """Lane (0 left, 1 right) a drop at point pt lands on."""
        vx, vy = DIR_VEC[node.dir]
        rx, ry = -vy, vx
        t = _tile(*pt)
        cx, cy = t[0] + 0.5, t[1] + 0.5
        s = (pt[0] - cx) * rx + (pt[1] - cy) * ry
        return 0 if s < -1e-6 else 1

    # ---- what each entity accepts / offers ---------------------------------------------------------------------
    def _fuel_items(self, cats) -> set:
        cats = set(cats or [])
        if not hasattr(self, "_fuel_cache"):
            self._fuel_cache = {}
        k = tuple(sorted(cats))
        if k not in self._fuel_cache:
            self._fuel_cache[k] = {n for n, it in self.gd.raw["items"].items()
                                   if it.get("fuel_value") and set(it.get("fuel_categories") or []) & cats}
        return self._fuel_cache[k]

    def _burner_fuel(self, r) -> set:
        es = r.p.get("energy_source") or {}
        return self._fuel_items(es.get("fuel_categories")) if es.get("type") == "burner" else set()

    def _category_inputs(self, cats) -> set:
        out = set()
        for rc in self.gd.raw["recipes"].values():
            if set(rc["categories"]) & set(cats):
                out.update(i["name"] for i in rc["ingredients"] if i.get("type", "item") == "item")
        return out

    def accepts(self, r, items: set) -> set:
        """Subset of items this entity takes from an inserter/drill ('*' = unknown items)."""
        if r is None:
            return set()
        if r.existing and r.type in CONTAINER_TYPES | CRAFTER_TYPES:
            return set(items)
        t = r.type
        if t in CONTAINER_TYPES:
            return set(items)
        ok = set(self._burner_fuel(r))
        if t in CRAFTER_TYPES:
            if r.recipe:
                ok |= {i["name"] for i in self.gd.raw["recipes"][r.recipe]["ingredients"] if i.get("type", "item") == "item"}
            elif t == "furnace":
                ok |= self._category_inputs(r.p.get("crafting_categories") or [])
        elif t == "lab":
            ok |= set(r.p.get("lab_inputs") or [])
        elif t == "ammo-turret":
            ok |= {n for n, it in self.gd.raw["items"].items() if it.get("type") == "ammo"}
        if not ok:
            return set()
        return (items & ok) | ({"*"} if "*" in items else set())

    def offers(self, r) -> set:
        """Items an inserter can take out of this entity."""
        if r is None:
            return set()
        if r.existing and r.type in CONTAINER_TYPES | CRAFTER_TYPES:
            return {"*"}
        if r.type in CONTAINER_TYPES:
            return r.outputs
        if r.type in CRAFTER_TYPES:
            return r.outputs
        return set()

    def _recipe_products(self, rc) -> set:
        return {p["name"] for p in rc["products"] if p.get("type", "item") == "item"}

    def _machine_recipes(self, r):
        """Recipes a crafter runs: its own, or for a furnace the ones its inputs allow."""
        if r.recipe:
            return [r.recipe]
        if r.type != "furnace":
            return []
        cats = set(r.p.get("crafting_categories") or [])
        out = []
        for rn, rc in self.gd.raw["recipes"].items():
            if not set(rc["categories"]) & cats or rc.get("hidden"):
                continue
            ing = [i["name"] for i in rc["ingredients"]]
            if ing and all(i in r.inputs for i in ing):
                out.append(rn)
        out.sort(key=lambda rn: (not self.gd.enabled(rn), rn))
        return out

    # ---- item reachability ------------------------------------------------------------------------------------
    def _resolve(self, pt):
        """(record, node) at a point: node for belts, record for other entities, or an opaque existing name."""
        t = _tile(*pt)
        n = self.node_at.get(t)
        if n is not None:
            return n.r, n
        r = self.at(t)
        if r is not None:
            return r, None
        name = self._existing_tag(t)
        if name and name in self.gd.raw["entities"]:
            proto = self.gd.raw["entities"][name]
            r = _R(-1, name, t[0] + 0.5, t[1] + 0.5, 0, None, {}, proto, True)
            if r.type in BELT_TYPES:
                r.type = "container"  # an existing belt we can't see the line of: treat as an open target
            return r, None
        return None, None

    def _items(self):
        """Fixed point of which items reach which belt lanes / machines."""
        self.links = []   # (src_rec, src_node, dst_rec, dst_node, mover_rec, kind) kind "ins"|"drill"
        for r in self.inserters:
            s, sn = self._resolve(r.extra["_pick"])
            d, dn = self._resolve(r.extra["_drop"])
            r.extra["_src"], r.extra["_dst"] = (s, sn), (d, dn)
            self.links.append((s, sn, d, dn, r, "ins"))
        for r in self.drills:
            d, dn = self._resolve(r.extra["_drop"])
            r.extra["_dst"] = (d, dn)
            self.links.append((r, None, d, dn, r, "drill"))
        if not self.ex.get("connect_existing"):
            for s, sn, d, dn, mover, kind in self.links:
                if mover.existing:
                    hit = [x for x in ((sn.r if sn is not None else None), d) if x is not None and not x.existing]
                    if hit:
                        self.err("existing_link", hit[0], f"existing {mover.label} reaches into planned "
                                                          f"{hit[0].label}: foreign items would mix in")
                else:
                    hit = [x for x in ((sn.r if sn is not None else None), (dn.r if dn is not None else None))
                           if x is not None and x.existing and x.i >= 0]
                    if hit:
                        self.err("existing_link", mover, f"{mover.label} reaches onto the existing belt "
                                                         f"{hit[0].label}: planned and existing lines would mix")
        self.lane_items = {id(n): (set(), set()) for n in self.nodes}
        self.inject = defaultdict(lambda: (set(), set()))   # node id -> lane item sets added there
        self.sinks = set()
        self._sources_and_sinks()
        containers = {}
        for k, v in (self.ex.get("containers") or {}).items():
            key = _xy_key(k) if not isinstance(k, str) else None
            if key:
                containers[key] = set(v)
        for r in self.recs:
            if r.type in CONTAINER_TYPES and not r.existing:
                r.outputs = set(containers.get((r.x, r.y), ()))
        for _ in range(30):
            changed = False
            for r in self.recs:
                if r.type in CRAFTER_TYPES and not r.existing:
                    r.recipes = self._machine_recipes(r)
                    outs = set()
                    for rn in r.recipes:
                        outs |= self._recipe_products(self.gd.raw["recipes"][rn])
                    if outs - r.outputs:
                        r.outputs |= outs
                        changed = True
            for s, sn, d, dn, mover, kind in self.links:
                if s is None or d is None:
                    continue
                if kind == "drill":
                    avail = set(s.outputs)
                elif sn is not None:
                    li = self.lane_items[id(sn)]
                    avail = li[0] | li[1]
                else:
                    avail = set(self.offers(s))
                if not avail:
                    continue
                if dn is not None:
                    if dn is sn:
                        continue
                    lane = self._lane_of(dn, mover.extra["_drop"])
                    tgt = self.inject[id(dn)][lane]
                    if avail - tgt:
                        tgt |= avail
                        changed = True
                else:
                    got = self.accepts(d, avail)
                    if got - d.inputs:
                        d.inputs |= got
                        changed = True
                    if d.type in CONTAINER_TYPES and not d.existing and got - d.outputs:
                        d.outputs |= got
                        changed = True
            if self._propagate_items():
                changed = True
            if not changed:
                break

    def _sources_and_sinks(self):
        ex = self.ex
        ports = list(ex.get("ports") or [])
        srcs = [("belt_in", p) for p in ex.get("sources") or []] + [("belt_out", p) for p in ex.get("sinks") or []]
        srcs += [(_get(p, "kind"), p) for p in ports]
        self.src_rates = []      # (node, lane|None, item, per_s)
        self.sink_nodes = {}     # node id -> expected {item: per_s}
        for kind, p in srcs:
            if kind not in ("belt_in", "belt_out"):
                continue
            x, y = float(_get(p, "x")), float(_get(p, "y"))
            d = _get(p, "direction")
            t = _tile(x, y)
            n = self.node_at.get(t)
            if n is None and d is not None and kind == "belt_in":
                v = DIR_VEC.get(int(d) % 16)
                if v:
                    n = self.node_at.get((t[0] + v[0], t[1] + v[1]))
            if n is None and kind == "belt_out":
                n = next((m for m in self.nodes if t in m.out_tiles), None)
            if n is None:
                self.err("port", (x, y), f"{kind} port at {_p(x, y)}: no belt there to connect")
                continue
            items = _get(p, "items") or ([_get(p, "item")] if _get(p, "item") else ["*"])
            per_s = (_get(p, "per_min") or 0) / 60.0
            lane = _get(p, "lane")
            lane = {"left": 0, "right": 1, 0: 0, 1: 1}.get(lane)
            if kind == "belt_in":
                for it in items:
                    for ln in ((lane,) if lane is not None else (0, 1)):
                        self.inject[id(n)][ln].add(it)
                    self.src_rates.append((n, lane, it, per_s / len(items)))
            else:
                exp = self.sink_nodes.setdefault(id(n), {})
                for it in items:
                    exp[it] = exp.get(it, 0) + per_s / len(items)

    def _propagate_items(self) -> bool:
        changed = False
        q = deque(self.nodes)
        inq = {id(n) for n in self.nodes}
        while q:
            n = q.popleft()
            inq.discard(id(n))
            li = self.lane_items[id(n)]
            inj = self.inject.get(id(n))
            if inj:
                for ln in (0, 1):
                    if inj[ln] - li[ln]:
                        li[ln].update(inj[ln])
                        changed = True
            for m, mp in n.outs:
                lm = self.lane_items[id(m)]
                grew = False
                for ln in (0, 1):
                    dst = _dst_lane(mp, ln)
                    if dst is not None and li[ln] - lm[dst]:
                        lm[dst].update(li[ln])
                        grew = True
                if grew:
                    changed = True
                    if id(m) not in inq:
                        q.append(m)
                        inq.add(id(m))
        return changed

    # ---- rates ------------------------------------------------------------------------------------------------
    def _crafts_per_s(self, r, rn) -> float:
        rc = self.gd.raw["recipes"][rn]
        return (r.p.get("crafting_speed") or 0) / max(rc.get("energy") or 0.5, 1e-9)

    @staticmethod
    def _amount(p) -> float:
        a = p.get("amount")
        if a is None:
            a = ((p.get("amount_min") or 0) + (p.get("amount_max") or 0)) / 2
        return a * (p.get("probability", p.get("independent_probability", 1)) or 1)

    def _machine_need(self, r) -> dict:
        """Ingredient items/s at full speed (first recipe)."""
        if not r.recipes:
            return {}
        rn = r.recipes[0]
        c = self._crafts_per_s(r, rn)
        return {i["name"]: i["amount"] * c for i in self.gd.raw["recipes"][rn]["ingredients"]
                if i.get("type", "item") == "item"}

    def _machine_out(self, r) -> dict:
        if not r.recipes:
            return {}
        rn = r.recipes[0]
        c = self._crafts_per_s(r, rn)
        return {p["name"]: self._amount(p) * c for p in self.gd.raw["recipes"][rn]["products"]
                if p.get("type", "item") == "item"}

    def _fuel_need(self, r, item) -> float:
        kw = r.p.get("max_energy_usage_kw") or r.p.get("energy_usage_kw") or 0
        fv = self.gd.fuel_value(item) if item in self.gd.raw["items"] else 0
        eff = (r.p.get("energy_source") or {}).get("effectivity") or 1
        return kw * 1000 / fv / eff if fv else 0.0

    def _drill_rate(self, r) -> dict:
        res = r.extra.get("_resource")
        if not res:
            return {}
        rp = self.gd.raw["resources"][res]
        bonus = self.gd.bonuses.get("mining_drill_productivity_bonus") or 0 \
            if r.p.get("uses_force_mining_productivity_bonus") else 0
        ops = (r.p.get("mining_speed") or 0) / max(rp.get("mining_time") or 1, 1e-9) * (1 + bonus)
        return {p["name"]: self._amount(p) * ops for p in rp.get("products") or [] if p.get("type", "item") == "item"}

    def _ins_tp(self, r) -> float:
        try:
            return self.gd.inserter_throughput(r.name)
        except (ValueError, KeyError):
            return 1.0

    def _topo(self):
        indeg = {id(n): 0 for n in self.nodes}
        for n in self.nodes:
            for m, _ in n.outs:
                indeg[id(m)] += 1
        q = deque(n for n in self.nodes if indeg[id(n)] == 0)
        order, seen = [], set()
        while q:
            n = q.popleft()
            order.append(n)
            seen.add(id(n))
            for m, _ in n.outs:
                indeg[id(m)] -= 1
                if indeg[id(m)] == 0:
                    q.append(m)
        rest = [n for n in self.nodes if id(n) not in seen]
        if rest:
            r = rest[0].r
            if any(not n.r.existing for n in rest):
                self.warn("belt_loop", r, f"belts loop back on themselves around {r.label}; rates there are rough")
        return order + rest

    def _rates(self):
        """Lane flows with consumption, machine speeds; belt overloads and starvation."""
        order = self._topo()
        # consumers per belt node: (inserter, dst, buckets[(items, demand)])
        n_feed = defaultdict(int)       # (dst id, item) -> number of movers bringing it
        n_out = defaultdict(int)        # src id -> number of inserters taking from it
        for s, sn, d, dn, mover, kind in self.links:
            if d is None or s is None:
                continue
            if kind == "ins" and sn is None:
                n_out[s.i] += 1
            if dn is None:
                avail = self._avail(s, sn, kind)
                for it in self.accepts(d, avail):
                    n_feed[(d.i, it)] += 1
        self.n_out, self.n_feed = n_out, n_feed
        scale = defaultdict(lambda: 1.0)
        self.overloads = {}
        # inflows of unknown rate (sources without per_min, chests, existing entities): don't judge supply there
        unk, unknown_in = set(), defaultdict(set)
        for n, lane, item, per_s in self.src_rates:
            if per_s <= 0:
                unk.add(id(n))
        for s, sn, d, dn, mover, kind in self.links:
            if s is None or d is None or kind != "ins" or sn is not None:
                continue
            if s.existing or s.type in CONTAINER_TYPES:
                if dn is not None:
                    unk.add(id(dn))
                else:
                    unknown_in[d.i] |= self.accepts(d, self.offers(s))
        for n in self._topo():
            if id(n) in unk:
                for m, _ in n.outs:
                    unk.add(id(m))
        self.unk, self.unknown_in = unk, unknown_in
        for it in range(3):
            final = it == 2
            received = defaultdict(lambda: defaultdict(float))   # dst rec index -> item -> per_s
            inj = defaultdict(lambda: [defaultdict(float), defaultdict(float)])
            direct = []
            for s, sn, d, dn, mover, kind in self.links:
                if s is None or d is None:
                    continue
                if kind == "drill":
                    rate = self._drill_rate(s)
                elif sn is None and s.type in CRAFTER_TYPES and not s.existing:
                    tp = self._ins_tp(mover)
                    out = {k: v * scale[s.i] / max(1, n_out[s.i]) for k, v in self._machine_out(s).items()}
                    tot = sum(out.values())
                    f = min(1.0, tp / tot) if tot > 0 else 0
                    rate = {k: v * f for k, v in out.items()}
                else:
                    rate = {}
                if dn is not None and dn is not sn:
                    lane = self._lane_of(dn, mover.extra["_drop"])
                    for k, v in rate.items():
                        inj[id(dn)][lane][k] += v
                elif dn is None and rate:
                    direct.append((d, rate))
            for n, lane, item, per_s in self.src_rates:
                for ln in ((lane,) if lane is not None else (0, 1)):
                    inj[id(n)][ln][item] += per_s / (1 if lane is not None else 2)
            for d, rate in direct:
                acc = self.accepts(d, set(rate))
                for k, v in rate.items():
                    if k in acc:
                        received[d.i][k] += v
            consumers = defaultdict(list)
            for s, sn, d, dn, mover, kind in self.links:
                if kind != "ins" or sn is None or d is None or dn is sn:
                    continue
                consumers[id(sn)].append((mover, d, dn))
            flow = {id(n): [defaultdict(float), defaultdict(float)] for n in self.nodes}
            self.arrived = {}
            self.starve = defaultdict(lambda: [0.0, 0.0, 0, None])   # (comp, item) -> [demand, got, n, rec]
            for n in order:
                lanes = flow[id(n)]
                for ln in (0, 1):
                    for k, v in inj[id(n)][ln].items():
                        lanes[ln][k] += v
                if n.kind != "split":
                    cap = n.r.p.get("lane_items_per_s") or (n.r.p.get("belt_items_per_s") or 0) / 2
                    for ln in (0, 1):
                        tot = sum(lanes[ln].values())
                        if not cap or tot <= cap * 1.001:
                            continue
                        key = (self.comp[id(n)], ln)
                        if final and key not in self.overloads and not n.r.existing:
                            self.overloads[key] = (n, ln, tot, cap, dict(lanes[ln]))
                        for k in lanes[ln]:     # the rest backs up: the lane moves at most its capacity
                            lanes[ln][k] *= cap / tot
                for mover, d, dn in consumers.get(id(n), ()):
                    self._consume(n, mover, d, dn, lanes, received, final)
                outs = n.outs
                if not outs:
                    self.arrived[id(n)] = lanes
                    continue
                share = 1.0 / len(outs) if n.kind == "split" else 1.0
                for m, mp in outs:
                    fm = flow[id(m)]
                    for ln in (0, 1):
                        dst = _dst_lane(mp, ln)
                        if dst is None:
                            continue
                        for k, v in lanes[ln].items():
                            fm[dst][k] += v * share
            # machine speed factors from what they receive
            for r in self.recs:
                if r.existing or r.type not in CRAFTER_TYPES or not r.recipes:
                    continue
                need = self._machine_need(r)
                f = 1.0
                for k, v in need.items():
                    if v > 0 and k not in unknown_in[r.i] and "*" not in unknown_in[r.i]:
                        f = min(f, received[r.i].get(k, 0) / v)
                fuel = self._burner_fuel(r)
                if fuel and not self._manual(r) and not (fuel & unknown_in[r.i]) and "*" not in unknown_in[r.i]:
                    got_fuel = fuel & set(received[r.i])
                    fneed = max((self._fuel_need(r, k) for k in got_fuel), default=0)
                    got = sum(v for k, v in received[r.i].items() if k in fuel)
                    if not got_fuel:
                        f = 0.0      # never fuelled: never runs
                    elif fneed > 0:
                        f = min(f, got / fneed)
                scale[r.i] = max(0.0, min(1.0, f))
        self.scale, self.received = scale, received
        for (comp, ln), (n, lane, tot, cap, items) in self.overloads.items():
            self.err("belt_overload", n.r, f"{n.r.label}: {LANES[lane]} lane must carry {tot:.2f}/s "
                                           f"({_items_txt(items)}) but holds {cap:g}/s")
        for (comp, item), (dem, got, cnt, r) in self.starve.items():
            if dem > 0 and got < dem * 0.95:
                fuel_short = bool(self.ex.get("strict_fuel")) and (
                    item == "fuel" or bool((self.gd.raw["items"].get(item) or {}).get("fuel_value")))
                (self.err if fuel_short else self.shortfall)("belt_supply", r, f"{item} on the belt line through {r.label}: {got:.2f}/s arrives "
                                                 f"but {cnt} inserter(s) downstream want {dem:.2f}/s")

    def _manual(self, r) -> bool:
        """Fuelled by hand: expect manual_fuel, or the PE's own extra {"fuel": "manual"} (blocks.py)."""
        mf = self.ex.get("manual_fuel")
        if mf is True or r.extra.get("fuel") == "manual":
            return True
        if not mf:
            return False
        for v in mf:
            if v == r.name or (_xy_key(v) == (r.x, r.y)):
                return True
        return False

    def _avail(self, s, sn, kind) -> set:
        if kind == "drill":
            return set(s.outputs)
        if sn is not None:
            li = self.lane_items[id(sn)]
            return li[0] | li[1]
        return set(self.offers(s))

    def _consume(self, n, mover, d, dn, lanes, received, final):
        avail = set(lanes[0]) | set(lanes[1])
        tp = self._ins_tp(mover)
        opportunistic = dn is not None or d.type in CONTAINER_TYPES or d.existing
        if opportunistic:
            buckets = [(avail, tp)]
        else:
            acc = self.accepts(d, avail | self.lane_items[id(mover.extra["_src"][1])][0]
                               | self.lane_items[id(mover.extra["_src"][1])][1])
            need = self._machine_need(d) if d.type in CRAFTER_TYPES else {}
            buckets = []
            for k, v in need.items():
                if k in acc:
                    buckets.append(({k}, v / max(1, self.n_feed[(d.i, k)])))
            fuel = self._burner_fuel(d) & acc
            if fuel and not self._manual(d):
                fneed = max(self._fuel_need(d, k) for k in fuel)
                nf = max(self.n_feed[(d.i, k)] for k in fuel)
                buckets.append((fuel, fneed / max(1, nf)))
            if d.type == "lab":
                buckets += [({k}, 0.05) for k in acc if k != "*"]
            if not buckets and acc:
                buckets = [(acc, tp)]
        total = sum(b[1] for b in buckets)
        f = min(1.0, tp / total) if total > 0 else 1.0
        if not opportunistic and not self._manual(mover) and self._self_fuels(mover):
            mf = self._burner_fuel(mover) & avail
            if mf and total > 0:  # its own fuel: busy fraction x its power
                busy = min(1.0, total * f / tp) if tp else 1.0
                own = busy * max(self._fuel_need(mover, k) for k in mf)
                buckets.append((mf, own / f if f > 0 else own))
        comp = self.comp[id(mover.extra["_src"][1])]
        unknown = id(n) in self.unk
        for items, dem in buckets:
            dem *= f
            have = sum(lanes[ln].get(k, 0) for ln in (0, 1) for k in items)
            take = min(dem, have)
            if have > 0:
                for ln in (0, 1):
                    for k in items:
                        v = lanes[ln].get(k, 0)
                        if v:
                            lanes[ln][k] = v - take * v / have
                            received[d.i][k] += take * v / have
            if unknown:
                if take < dem:
                    for k in sorted(items)[:1]:
                        received[d.i][k] += dem - take  # assume the unknown inflow covers the rest
                continue
            if final and not opportunistic and "*" not in items:
                for k in sorted(items)[:1]:
                    st = self.starve[(comp, k if len(items) == 1 else "fuel")]
                    st[0] += dem
                    st[1] += take
                    st[2] += 1
                    st[3] = st[3] or mover

    # ---- per-entity checks ------------------------------------------------------------------------------------
    def _machine_io(self):
        takers = defaultdict(int)     # rec index -> inserters taking out of it
        for s, sn, d, dn, mover, kind in self.links:
            if kind == "ins" and s is not None and sn is None:
                takers[s.i] += 1
        for r in self.inserters:
            if r.existing:
                continue
            (s, sn), (d, dn) = r.extra["_src"], r.extra["_dst"]
            pk, dp = _tile(*r.extra["_pick"]), _tile(*r.extra["_drop"])
            if s is None:
                self.err("inserter_pickup", r, f"{r.label} (picks up from the {DIR_NAME[r.dir]}) has nothing to "
                                               f"pick up from at tile {_p(*pk)}")
            if d is None:
                self.err("inserter_drop", r, f"{r.label} drops onto empty ground at tile {_p(*dp)} "
                                             f"({DIR_NAME[OPP[r.dir]]} side)")
            if s is None or d is None:
                continue
            if s is d:
                self.err("inserter_pickup", r, f"{r.label} picks up from and drops into the same {s.name}")
                continue
            if sn is None and s.type not in CONTAINER_TYPES | CRAFTER_TYPES and not s.existing:
                self.err("inserter_pickup", r, f"{r.label} picks up from {s.label}, which has no output inventory")
                continue
            if dn is None and not self.accepts(d, {"*"}) and not d.existing:
                self.err("inserter_drop", r, f"{r.label} drops into {d.label}, which takes no items")
                continue
            avail = self._avail(s, sn, "ins")
            got = avail if dn is not None else self.accepts(d, avail)
            if not got and dn is None:
                if sn is not None:
                    if not avail:
                        start = self._line_start(sn)
                        self.err("inserter_starved", r, f"{r.label} picks from {sn.r.label}, but nothing ever arrives "
                                                        f"there: the belt line starting at {start.r.label} is fed by "
                                                        f"nothing")
                    else:
                        self.err("inserter_starved", r, f"{r.label} picks from {sn.r.label}, which carries "
                                                        f"{_items_txt(avail)}, but {d.label} takes none of it")
                elif avail:
                    self.err("inserter_starved", r, f"{r.label} moves {_items_txt(avail)} from {s.label} into "
                                                    f"{d.label}, which doesn't take it")
                else:
                    self.warn("inserter_starved", r, f"{r.label}: {s.label} never receives anything to pass on")
            elif not avail and dn is not None and sn is None:
                self.warn("inserter_starved", r, f"{r.label}: {s.label} never receives anything to pass on")
        for r in self.drills:
            if r.existing:
                continue
            d, dn = r.extra["_dst"]
            dp = _tile(*r.extra["_drop"])
            if d is None:
                self.err("drill_output", r, f"{r.label} drops its ore onto bare ground at tile {_p(*dp)}")
            elif dn is None and r.outputs and "*" not in r.outputs and not self.accepts(d, r.outputs):
                self.err("drill_output", r, f"{r.label} drops {_items_txt(r.outputs)} into {d.label}, which "
                                            f"doesn't take it")
        for r in self.recs:
            if r.existing or r.type not in CRAFTER_TYPES | {"lab"}:
                continue
            if r.type == "assembling-machine" and not r.recipe:
                self.err("recipe", r, f"{r.label} has no recipe set")
                continue
            if r.type == "lab":
                if not r.inputs:
                    self.err("machine_input", r, f"{r.label} never receives science packs")
                continue
            if "*" in r.inputs:
                continue
            if r.type == "furnace" and not r.recipe:
                if not r.recipes:
                    self.err("machine_input", r, f"{r.label} never receives anything it can smelt "
                                                 f"(gets {_items_txt(r.inputs)})")
                    continue
            elif r.recipe:
                miss = [i["name"] for i in self.gd.raw["recipes"][r.recipe]["ingredients"]
                        if i.get("type", "item") == "item" and i["name"] not in r.inputs]
                if miss:
                    self.err("machine_input", r, f"{r.label} ({r.recipe}) never receives {_items_txt(miss)}")
            outs = {k for k in r.outputs}
            if r.type != "rocket-silo" and outs and not takers.get(r.i):
                self.err("machine_output", r, f"{r.label}: nothing takes its output ({_items_txt(outs)}) out")
            self._io_throughput(r)
        # belts: dead ends (items nobody takes) and lines that carry nothing
        self._dead_ends()

    def _io_throughput(self, r):
        need = self._machine_need(r)
        tps = defaultdict(float)
        for s, sn, d, dn, mover, kind in self.links:
            if d is r and kind == "ins":
                for k in self.accepts(r, self._avail(s, sn, kind)):
                    tps[k] += self._ins_tp(mover)
            if d is r and kind == "drill":
                for k, v in self._drill_rate(s).items():
                    tps[k] += v
        for k, v in need.items():
            if k in tps and tps[k] < v * 0.999:
                self.shortfall("inserter_throughput", r, f"{r.label} ({r.recipes[0]}) needs {v:.2f}/s {k} but its "
                                                         f"input inserters move at most {tps[k]:.2f}/s")
        out = sum(self._machine_out(r).values())
        cap = sum(self._ins_tp(m) for s, sn, d, dn, m, kind in self.links if s is r and kind == "ins" and sn is None)
        if cap and out > cap * 1.001:
            self.shortfall("inserter_throughput", r, f"{r.label} ({r.recipes[0]}) makes {out:.2f}/s but its output "
                                                     f"inserters move at most {cap:.2f}/s")

    def _line_start(self, n):
        seen = {id(n)}
        while n.ins:
            m = n.ins[0][0]
            if id(m) in seen:
                break
            seen.add(id(m))
            n = m
        return n

    def _dead_ends(self):
        consumed = defaultdict(set)
        for s, sn, d, dn, mover, kind in self.links:
            if kind == "ins" and sn is not None and d is not None and dn is not sn:
                li = self.lane_items[id(sn)]
                avail = li[0] | li[1]
                consumed[id(sn)] |= avail if (dn is not None or d.type in CONTAINER_TYPES or d.existing) \
                    else self.accepts(d, avail)
        unc = {id(n): set() for n in self.nodes}
        q = deque(self._topo())
        inq = {id(n) for n in q}
        while q:
            n = q.popleft()
            inq.discard(id(n))
            inj = self.inject.get(id(n))
            cur = set(unc[id(n)])
            if inj:
                cur |= inj[0] | inj[1]
            cur -= consumed[id(n)]
            for m, _ in n.outs:
                if cur - unc[id(m)]:
                    unc[id(m)] |= cur
                    if id(m) not in inq:
                        q.append(m)
                        inq.add(id(m))
            if not n.outs and n.kind not in ("end", "ug_in"):
                left = {i for i in cur if i != "*"}
                if left and id(n) not in self.sink_nodes and not n.r.existing and not self._opaque_out(n):
                    o = n.out_tiles[0] if n.out_tiles else None
                    into = self.at(o) if o else None
                    where = f"into {into.name}" if into else f"into nothing at {_p(*o)}" if o else ""
                    self.err("belt_dead_end", n.r, f"{n.r.label} ends ({DIR_NAME[n.dir]} {where}): "
                                                   f"{_items_txt(left)} put on this line is never taken off")
        for n in self.nodes:
            li = self.lane_items[id(n)]
            if not n.r.existing and not (li[0] or li[1]) and not n.ins and not consumed[id(n)] and n.kind != "end" \
                    and not self.inject.get(id(n)):
                self.warn("belt_unused", n.r, f"{n.r.label}: belt line carries nothing")

    def _opaque_out(self, n) -> bool:
        return any(self._existing_tag(t) for t in n.out_tiles)

    # ---- fluids -----------------------------------------------------------------------------------------------
    def _fluid_boxes(self, r):
        """Active fluidboxes: (index, production_type, filter). Conditional boxes only for fluid recipes."""
        out = []
        boxes = r.p.get("fluidboxes") or []
        fluid_recipe = None
        if r.recipe:
            rc = self.gd.raw["recipes"][r.recipe]
            fluid_recipe = rc
        for fb in boxes:
            if fb.get("conditional"):
                if r.type == "mining-drill":
                    res = r.extra.get("_resource")
                    if not (res and self.gd.raw["resources"][res].get("required_fluid")):
                        continue
                elif not fluid_recipe or not any(x.get("type") == "fluid" for x in
                                                 fluid_recipe["ingredients"] + fluid_recipe["products"]):
                    continue
            out.append(fb)
        return out

    def _fluids(self):
        conns = {}       # (tile, dir) -> (rec, fbindex, flow)
        ugs = []
        nodes = []
        for r in self.recs:
            fbs = self._fluid_boxes(r)
            if not fbs:
                continue
            active = {fb.get("index") for fb in fbs}
            try:
                full = self.gd.fluid_connections_full(r.name, r.x, r.y, r.dir)
            except (ValueError, KeyError):
                continue
            for fb in fbs:
                nodes.append((r.i, fb.get("index")))
            for c in full:
                if c["fluidbox"] not in active:
                    continue
                if c["type"] == "underground":
                    ugs.append((r, c))
                else:
                    conns[(c["tile"], c["dir"])] = (r, c)
        if not nodes:
            return
        uf = _UF()
        for k in nodes:
            uf.find(k)
        open_ends = defaultdict(list)
        for (tile, d), (r, c) in conns.items():
            other = conns.get((c["target_tile"], OPP[d]))
            if other is None:
                if not r.existing:
                    open_ends[(r.i, c["fluidbox"])].append((c["target_tile"], d, r))
                continue
            r2, c2 = other
            if c2["target_tile"] != tile:
                continue
            if {c["flow"], c2["flow"]} in ({"input"}, {"output"}):
                continue
            uf.union((r.i, c["fluidbox"]), (r2.i, c2["fluidbox"]))
        # pipe-to-ground pairing
        by_tile = {}
        for r, c in ugs:
            by_tile[c["tile"]] = (r, c)
        for r, c in ugs:
            vx, vy = DIR_VEC[c["dir"]]
            tx, ty = c["tile"]
            m = c.get("max_underground_distance") or self.gd.raw["utility"].get("max_pipe_to_ground_distance") or 10
            partner, why = None, None
            for k in range(1, int(m) + 1):
                o = by_tile.get((tx + k * vx, ty + k * vy))
                if o is None or o[0].name != r.name:
                    continue
                if o[1]["dir"] not in (c["dir"], OPP[c["dir"]]):
                    continue  # perpendicular: the engine skips it
                if o[1]["dir"] == OPP[c["dir"]]:
                    partner = o
                else:
                    why = o
                break
            if partner:
                uf.union((r.i, c["fluidbox"]), (partner[0].i, partner[1]["fluidbox"]))
            elif not r.existing:
                if why:
                    self.err("pipe_to_ground", r, f"{r.label} goes underground to the {DIR_NAME[c['dir']]} but the "
                                                  f"next {r.name} ({why[0].label}) faces the same way; rotate it to "
                                                  f"face {DIR_NAME[c['dir']]} (connection side out)")
                else:
                    self.err("pipe_to_ground", r, f"{r.label} goes underground to the {DIR_NAME[c['dir']]} but no "
                                                  f"{r.name} facing back is within {int(m)} tiles")
        # sources / sinks per system
        recs = {r.i: r for r in self.recs}
        systems = defaultdict(lambda: {"src": defaultdict(list), "need": [], "filters": defaultdict(list),
                                       "members": [], "supply": defaultdict(float), "demand": defaultdict(float)})
        for k in nodes:
            r, idx = recs[k[0]], k[1]
            sysd = systems[uf.find(k)]
            sysd["members"].append(r)
            fb = next(fb for fb in self._fluid_boxes(r) if fb.get("index") == idx)
            if fb.get("filter"):
                sysd["filters"][fb["filter"]].append(r)
            pt = fb.get("production_type")
            if r.type == "offshore-pump":
                f = fb.get("filter") or "water"
                sysd["src"][f].append(r)
                sysd["supply"][f] += r.p.get("pumping_speed_per_s") or 0
            elif r.type == "boiler":
                if pt == "output":
                    sysd["src"][fb.get("filter") or "steam"].append(r)
                    sysd["supply"][fb.get("filter") or "steam"] += self._boiler_steam(r)
                elif pt == "input":
                    sysd["need"].append((r, fb.get("filter") or "water"))
                    sysd["demand"][fb.get("filter") or "water"] += self._boiler_steam(r)
            elif r.type == "generator":
                f = fb.get("filter") or "steam"
                sysd["need"].append((r, f))
                sysd["demand"][f] += r.p.get("fluid_usage_per_s") or 0
            elif r.type == "mining-drill" and pt == "output":
                res = r.extra.get("_resource")
                for p in (self.gd.raw["resources"].get(res) or {}).get("products") or []:
                    if p.get("type") == "fluid":
                        sysd["src"][p["name"]].append(r)
            elif r.type in CRAFTER_TYPES and r.recipe:
                rc = self.gd.raw["recipes"][r.recipe]
                fl = [x["name"] for x in (rc["products"] if pt == "output" else rc["ingredients"])
                      if x.get("type") == "fluid"]
                same = [fb2 for fb2 in self._fluid_boxes(r) if fb2.get("production_type") == pt]
                pos = [fb2.get("index") for fb2 in same].index(idx)
                if pos < len(fl):
                    if pt == "output":
                        sysd["src"][fl[pos]].append(r)
                    else:
                        sysd["need"].append((r, fl[pos]))
        for kind, key in (("pipe_in", "fluid_sources"), ("pipe_out", "fluid_sinks")):
            ps = [p for p in (self.ex.get(key) or [])] + [p for p in (self.ex.get("ports") or []) if _get(p, "kind") == kind]
            for p in ps:
                x, y = float(_get(p, "x")), float(_get(p, "y"))
                t = _tile(x, y)
                cand = [t]
                d = _get(p, "direction")
                if d is not None and int(d) % 16 in DIR_VEC:
                    v = DIR_VEC[int(d) % 16]
                    cand += [(t[0] + v[0], t[1] + v[1]), (t[0] - v[0], t[1] - v[1])]
                hit = None
                for c in cand:
                    r = self.at(c)
                    if r is not None and self._fluid_boxes(r):
                        hit = r
                        break
                if hit is None:
                    self.err("port", (x, y), f"{kind} port at {_p(x, y)}: no pipe there to connect")
                    continue
                fbi = self._fluid_boxes(hit)[0].get("index")
                sysd = systems[uf.find((hit.i, fbi))]
                fl = _get(p, "item") or _get(p, "fluid") or "*"
                if kind == "pipe_in":
                    sysd["src"][fl].append(hit)
                    sysd["supply"][fl] += 1e9
                else:
                    sysd["need"].append((hit, fl))
        self.fluid_systems = len(systems)
        # a boiler whose water side has no water makes no steam
        for s in list(systems.values()):
            for r in s["src"].get("steam", ()):
                if r.type != "boiler":
                    continue
                ins = [fb.get("index") for fb in self._fluid_boxes(r) if fb.get("production_type") == "input"]
                wsys = systems.get(uf.find((r.i, ins[0]))) if ins else None
                if wsys is not None and not ({"water", "*"} & set(wsys["src"])):
                    s["supply"]["steam"] -= self._boiler_steam(r)
        self.steam_supply = 0.0
        for root, s in systems.items():
            fluids = set(s["src"]) | set(s["filters"])
            fluids.discard("*")
            if len(fluids) > 1 and any(not r.existing for r in s["members"]):
                a, b = sorted(fluids)[:2]
                ra = (s["src"].get(a) or s["filters"].get(a))[0]
                rb = (s["src"].get(b) or s["filters"].get(b))[0]
                self.err("fluid_mix", ra, f"one pipe network joins {a} ({ra.label}) and {b} ({rb.label}); "
                                          f"fluids would mix")
            for r, f in s["need"]:
                if r.existing:
                    continue
                if f in s["src"] or ("*" in s["src"]):
                    continue
                self.err("fluid_source", r, f"{r.label} gets no {f}: its pipe network has no {f} source"
                                            f"{self._gap_hint(root, r, open_ends, uf)}")
            if s["src"] and not s["need"]:
                r = next(iter(s["src"].values()))[0]
                if not r.existing and r.type != "pipe":
                    self.warn("fluid_unused", r, f"{r.label}: its {_items_txt(s['src'])} goes nowhere")
            for f, dem in s["demand"].items():
                sup = s["supply"].get(f, 0)
                if f in s["src"] and dem > sup * 1.001:
                    r = next(r for r, ff in s["need"] if ff == f)
                    self.shortfall("fluid_throughput", r, f"{f} network through {r.label} needs {dem:g}/s but its "
                                                          f"sources supply {sup:g}/s")
            if "steam" in s["src"]:
                self.steam_supply += min(s["supply"].get("steam", 0), s["demand"].get("steam", 0) or 0)
        self.fluid_uf, self.fluid_systems_map = uf, systems

    def _gap_hint(self, root, r, open_ends, uf) -> str:
        """Where the network containing `root` most likely breaks: a 1-2 tile gap to another fluid network, else
        the open end of a dead-end pipe run (nearest to r)."""
        ends = [e for (ri, fbi), lst in open_ends.items() if uf.find((ri, fbi)) == root for e in lst]
        near = sorted(ends, key=lambda e: abs(e[0][0] - r.x) + abs(e[0][1] - r.y))
        for t, d, pr in near:
            vx, vy = DIR_VEC[d]
            for k in (0, 1):
                if self.at((t[0] + k * vx, t[1] + k * vy)) is not None:
                    break
                o = self.at((t[0] + (k + 1) * vx, t[1] + (k + 1) * vy))
                if o is not None and self._fluid_boxes(o):
                    other = {uf.find((o.i, fb.get("index"))) for fb in self._fluid_boxes(o)}
                    if root not in other:
                        gap = _p(*t) if k == 0 else f"{_p(*t)}..{_p(t[0] + vx, t[1] + vy)}"
                        return f"; gap at tile {gap} between {pr.label} and {o.label}"
        conn = defaultdict(int)
        for t, d, pr in ends:
            conn[pr.i] += 1
        for t, d, pr in near:
            if pr.type == "pipe" and conn[pr.i] == 3:   # a pipe connected on one side only: end of a run
                return f"; the pipe run ends at {pr.label} (open to the {DIR_NAME[d]})"
        own = [e for e in near if e[2] is r]
        others = [o for o in self.recs if o is not r and self._fluid_boxes(o)
                  and root not in {uf.find((o.i, fb.get("index"))) for fb in self._fluid_boxes(o)}]
        if own and others:   # the side facing the nearest other fluid network
            own.sort(key=lambda e: min(abs(e[0][0] + 0.5 - o.x) + abs(e[0][1] + 0.5 - o.y) for o in others))
        if own:
            return f"; nothing is attached to its connection at tile {_p(*own[0][0])} ({DIR_NAME[own[0][1]]} side)"
        return ""

    def _boiler_steam(self, r) -> float:
        kw = r.p.get("max_energy_usage_kw") or 0
        tgt = r.p.get("target_temperature") or 165
        out = next((fb.get("filter") for fb in r.p.get("fluidboxes") or [] if fb.get("production_type") == "output"),
                   "steam")
        fl = self.gd.raw["fluids"].get(out) or {}
        hc = fl.get("heat_capacity") or 200
        dt = tgt - (fl.get("default_temperature") or 15)
        return kw * 1000 / (hc * dt) if dt > 0 else 0

    # ---- power ------------------------------------------------------------------------------------------------
    def _power(self):
        poles = [r for r in self.recs if r.type == "electric-pole"]
        cons, gens = [], []
        for r in self.recs:
            es = r.p.get("energy_source") or {}
            if r.type == "electric-pole":
                continue
            if r.type in GENERATOR_TYPES or (es.get("type") == "electric" and es.get("usage_priority") in OUTPUT_PRIORITIES):
                gens.append(r)
            elif es.get("type") == "electric" and r.type != "accumulator":
                cons.append(r)
        if not cons and not gens:
            return
        uf = _UF()
        for p in poles:
            uf.find(p.i)
        for a_i, a in enumerate(poles):
            ra = a.p.get("max_wire_distance") or 0
            for b in poles[a_i + 1:]:
                if math.hypot(a.x - b.x, a.y - b.y) <= min(ra, b.p.get("max_wire_distance") or 0) + 1e-9:
                    uf.union(a.i, b.i)
        self._cover = defaultdict(list)      # tile -> poles whose supply area reaches it
        for p in poles:
            d = p.p.get("supply_area_distance") or 0
            for t in tiles_of_box(p.x - d, p.y - d, p.x + d, p.y + d):
                self._cover[t].append(p)
        covering = self._covering

        ext = set()
        if self.ex.get("power") in ("external", True):
            ext = {uf.find(p.i) for p in poles}
        for p in poles:
            if p.existing:
                ext.add(uf.find(p.i))
        for pt in [q for q in (self.ex.get("ports") or []) if _get(q, "kind") == "pole"] + \
                list(self.ex.get("power_sources") or []):
            x, y = (float(_get(pt, "x")), float(_get(pt, "y"))) if not isinstance(pt, (list, tuple)) else map(float, pt[:2])
            near = [p for p in poles if math.hypot(p.x - x, p.y - y) <= (p.p.get("max_wire_distance") or 0) + 1e-9]
            if not near:
                self.err("port", (x, y), f"pole port at {_p(x, y)}: no pole of the plan within wire reach")
            for p in near:
                ext.add(uf.find(p.i))
        net_gen = defaultdict(list)
        supply_kw = 0.0
        for g in gens:
            cv = covering(g)
            if not cv:
                if not g.existing:
                    self.err("power_generator", g, f"{g.label} isn't inside any electric pole's supply area; "
                                                   f"its power goes nowhere")
                continue
            net_gen[uf.find(cv[0].i)].append(g)
            if not g.existing:
                supply_kw += g.p.get("max_energy_production_kw") or 0
        net_cons = defaultdict(list)
        demand_kw = 0.0
        for c in cons:
            if c.existing:
                continue
            demand_kw += (c.p.get("max_energy_usage_kw") or c.p.get("energy_usage_kw") or 0) + \
                ((c.p.get("energy_source") or {}).get("drain_kw") or 0)
            cv = covering(c)
            if not cv:
                near = min(poles, key=lambda p: math.hypot(p.x - c.x, p.y - c.y)) if poles else None
                hint = f" (nearest pole {near.label}, {math.hypot(near.x - c.x, near.y - c.y):.1f} tiles away, " \
                       f"supply reach {near.p.get('supply_area_distance'):g})" if near else " (the plan has no poles)"
                self.err("power_coverage", c, f"{c.label} isn't inside any electric pole's supply area{hint}")
                continue
            net_cons[uf.find(cv[0].i)].append(c)
        live = [n for n in net_cons if net_gen.get(n) or n in ext]
        for net, cs in net_cons.items():
            if net in live:
                continue
            members = [p for p in poles if uf.find(p.i) == net]
            others = [p for p in poles if uf.find(p.i) != net and (uf.find(p.i) in live or net_gen.get(uf.find(p.i)))]
            hint = ""
            if others:
                a, b = min(((a, b) for a in members for b in others), key=lambda ab: math.hypot(ab[0].x - ab[1].x,
                                                                                                 ab[0].y - ab[1].y))
                hint = f"; closest powered pole {b.label} is {math.hypot(a.x - b.x, a.y - b.y):.1f} tiles from " \
                       f"{a.label} (wire reach {min(a.p.get('max_wire_distance') or 0, b.p.get('max_wire_distance') or 0):g})"
            names = ", ".join(c.label for c in cs[:3]) + (f" +{len(cs) - 3}" if len(cs) > 3 else "")
            self.err("power_network", cs[0], f"{len(cs)} consumer(s) ({names}) are on a pole network "
                                             f"({len(members)} poles) with no generator{hint}")
        if len([n for n in live if net_gen.get(n)]) > 1:
            self.warn("power_split", gens[0], f"{len(live)} separate power networks with their own generators")
        steam_gens = [g for g in gens if g.type == "generator" and not g.existing]
        if steam_gens and hasattr(self, "steam_supply"):
            usage = sum(g.p.get("fluid_usage_per_s") or 0 for g in steam_gens)
            prod = sum(g.p.get("max_energy_production_kw") or 0 for g in steam_gens)
            if usage:
                supply_kw -= prod * (1 - min(1.0, self.steam_supply / usage))
        self.power = {"demand_kw": round(demand_kw, 1), "supply_kw": round(supply_kw, 1),
                      "networks": len(set(net_cons) | set(net_gen)), "poles": len(poles)}
        if gens and demand_kw > supply_kw * 1.001 and not ext:
            g = gens[0]
            self.shortfall("power_capacity", g, f"machines can draw {demand_kw:.0f} kW but the generators "
                                                f"({g.label}{f' +{len(gens) - 1}' if len(gens) > 1 else ''}) supply "
                                                f"{supply_kw:.0f} kW"
                                                f"{' (steam engines short of steam)' if steam_gens and supply_kw < 1 else ''}")

    def _covering(self, r) -> list:
        """Poles whose supply area overlaps r's collision box."""
        out = []
        for t in r.tiles:
            for p in self._cover.get(t, ()):
                d = p.p.get("supply_area_distance") or 0
                if p not in out and r.box[0] < p.x + d and r.box[2] > p.x - d and r.box[1] < p.y + d \
                        and r.box[3] > p.y - d:
                    out.append(p)
        return out

    def _covered(self, r) -> bool:
        return bool(self._covering(r))

    # ---- fuel -------------------------------------------------------------------------------------------------
    def _fuel(self):
        self.fuel_ok = {}
        for r in self.recs:
            if r.existing or (r.p.get("energy_source") or {}).get("type") != "burner":
                continue
            fuel = self._burner_fuel(r)
            ok = bool(r.inputs & fuel) or "*" in r.inputs or self._manual(r)
            if not ok and self._self_fuels(r):
                ok = True  # burner inserters refuel themselves from fuel they pick up (and deliver)
            if not ok and r.type == "boiler" and "*" in r.inputs:
                ok = True
            if not ok:
                cats = "/".join((r.p.get("energy_source") or {}).get("fuel_categories") or [])
                self.err("fuel", r, f"{r.label} has no fuel supply: nothing brings it {cats} fuel (coal/wood on a "
                                    f"belt + inserter, a coal drill feeding it, or mark it manual_fuel)")

    def _self_fuels(self, r) -> bool:
        """A burner inserter keeps itself fuelled only with fuel it picks up, and it only picks up fuel its drop
        target takes (a belt / chest, or a machine burning or using it)."""
        if r.type != "inserter" or (r.p.get("energy_source") or {}).get("type") != "burner":
            return False
        fuel = self._burner_fuel(r)
        s, sn = r.extra.get("_src", (None, None))
        d, dn = r.extra.get("_dst", (None, None))
        if s is None or d is None:
            return False
        got = self._avail(s, sn, "ins") & (fuel | {"*"})
        if not got:
            return False
        return dn is not None or d.type in CONTAINER_TYPES or bool(self.accepts(d, got))

    # ---- planned rates ----------------------------------------------------------------------------------------
    def _expect_rates(self):
        self.produced = defaultdict(float)
        for r in self.recs:
            if r.existing:
                continue
            if r.type in CRAFTER_TYPES and r.recipes:
                for k, v in self._machine_out(r).items():
                    self.produced[k] += v * self.scale[r.i]
            elif r.type == "mining-drill" and r.extra.get("_dst", (None,))[0] is not None:
                for k, v in self._drill_rate(r).items():
                    self.produced[k] += v
        delivered = defaultdict(float)
        for nid, exp in self.sink_nodes.items():
            lanes = self.arrived.get(nid)
            if lanes:
                for ln in (0, 1):
                    for k, v in lanes[ln].items():
                        delivered[k] += v
        self.delivered = delivered
        for nid, exp in self.sink_nodes.items():
            n = next(m for m in self.nodes if id(m) == nid)
            for k, v in exp.items():
                if k != "*" and v > 0:
                    got = (self.arrived.get(nid) or [{}, {}])
                    got = got[0].get(k, 0) + got[1].get(k, 0)
                    if got < v * 0.95:
                        self.err("rate", n.r, f"belt_out port after {n.r.label} should deliver {v * 60:.0f}/min {k}; "
                                              f"layout delivers ~{got * 60:.0f}/min")
        sinks = [n for n in self.nodes if id(n) in self.sink_nodes]
        for k, per_min in (self.ex.get("rates") or {}).items():
            at_sinks = bool(delivered.get(k))
            got = delivered[k] if at_sinks else self.produced.get(k, 0)
            if got * 60 < per_min * 0.95:
                plan = [r for r in self.recs if not r.existing]
                where = sinks[0].r if at_sinks else next((r for r in plan if k in r.outputs), plan[0])
                how = (f"delivered at the belt_out port after {where.label}" if at_sinks
                       else f"made by all machines (e.g. {where.label})")
                self.err("rate", where, f"plan expects {per_min:g}/min {k}; ~{got * 60:.0f}/min {how}")

    def _stats(self):
        plan = [r for r in self.recs if not r.existing]
        counts = defaultdict(int)
        for r in plan:
            counts[r.name] += 1
        st = {"entities": len(plan), "counts": dict(sorted(counts.items())),
              "existing_context": len(self.recs) - len(plan)}
        if getattr(self, "to_clear", None):
            st["to_clear"] = len(self.to_clear)
        if self.requires_research:
            st["requires_research"] = sorted(self.requires_research)
        if hasattr(self, "power"):
            st["power"] = self.power
        if hasattr(self, "produced"):
            st["produced_per_min"] = {k: round(v * 60, 2) for k, v in sorted(self.produced.items()) if v > 0}
        if getattr(self, "delivered", None):
            st["delivered_per_min"] = {k: round(v * 60, 2) for k, v in sorted(self.delivered.items()) if v > 0}
        if hasattr(self, "nodes"):
            st["belt_nodes"] = len(self.nodes)
        if hasattr(self, "fluid_systems"):
            st["fluid_systems"] = self.fluid_systems
        self.stats = st
        self.to_clear_list = getattr(self, "to_clear", [])
