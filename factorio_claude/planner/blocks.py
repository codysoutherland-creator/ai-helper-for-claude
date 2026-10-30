"""Layout library: parametric building blocks for the planner (starter base set).

Every block function returns a `Block` in LOCAL coordinates (tile (0, 0) = top-left of the block, entity positions
are centers, 16-way directions). `block.origin` is where local (0, 0) goes on the map (mining_outpost sets it from
the patch; others default to (0, 0) and the placer moves them with `block.at(x, y)`); `block.placed()` gives map
entities. Ports are the tiles just OUTSIDE the block where a belt/pipe must connect, direction = flow direction
(belt_in/pipe_in: flowing into the block). A "pole" port is the block's edge pole itself (inside the block).

    gd = GameData.load(...)
    b = smelter_array(12, "stone-furnace", "mixed_lane", "north", gd=gd)
    b = mining_outpost(patch, output_side="west", gd=gd, max_drills=10)    # patch: world.Patch or tile set
    b.rotated(1).at(100, 40).placed()

Research: with a GameData that has recipes, defaults pick what the force can build now (`options(gd)`) and
`block.requires` lists entities that still need research. gd=None uses vanilla geometry and no research checks.
Inserter direction = pickup side (verified engine fact).
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from .gamedata import GameData, rotate, tiles_of_box, NORTH, EAST, SOUTH, WEST, DIR_VEC

SIDES = {"north": 0, "east": 1, "south": 2, "west": 3}
_VANILLA = Path(__file__).with_name("blocks_vanilla.json")


# ---- data types ---------------------------------------------------------------------------------------------
@dataclass
class PE:
    """Planned entity. extra: free-form, e.g. {"fuel": "manual"} for burners nothing feeds."""
    name: str
    x: float
    y: float
    direction: int = 0
    recipe: str | None = None
    extra: dict | None = None

    def moved(self, dx: float, dy: float) -> "PE":
        return replace(self, x=self.x + dx, y=self.y + dy)

    def as_list(self) -> list:
        """[name, x, y, dir, recipe?] as plan_preview takes it."""
        out = [self.name, self.x, self.y, self.direction]
        return out + [self.recipe] if self.recipe else out


@dataclass
class Port:
    kind: str                  # belt_in | belt_out | pipe_in | pipe_out | pole
    item: str | None
    x: int
    y: int
    direction: int             # flow direction (pole: 0)
    lane: str | None = None    # "left" / "right" (relative to flow) when only one lane carries `item`

    def moved(self, dx: int, dy: int) -> "Port":
        return replace(self, x=self.x + dx, y=self.y + dy)


@dataclass
class Block:
    name: str
    entities: list
    size: tuple
    ports: list
    rates: dict = field(default_factory=dict)   # per minute: item -> +produced / -consumed; power_kw, fuel_per_min…
    notes: list = field(default_factory=list)
    origin: tuple = (0, 0)                      # map tile of local (0, 0)
    requires: list = field(default_factory=list)  # entity names not buildable with current research
    info: dict = field(default_factory=dict)      # extras: recipe, offshore_pumps, drills, ...

    def at(self, x: int, y: int) -> "Block":
        return replace(self, origin=(int(x), int(y)))

    def placed(self) -> list:
        """Entities in map coordinates."""
        ox, oy = self.origin
        return [e.moved(ox, oy) for e in self.entities]

    def placed_ports(self) -> list:
        ox, oy = self.origin
        return [p.moved(ox, oy) for p in self.ports]

    def port(self, kind: str, item: str | None = None) -> Port:
        for p in self.ports:
            if p.kind == kind and (item is None or p.item == item):
                return p
        raise KeyError(f"{self.name}: no {kind} port for {item}")

    def counts(self) -> dict:
        out = {}
        for e in self.entities:
            out[e.name] = out.get(e.name, 0) + 1
        return out

    def rotated(self, turns: int = 1) -> "Block":
        """Rotate clockwise by 90° steps inside the block (origin stays the top-left)."""
        b = self
        for _ in range(turns % 4):
            w, h = b.size
            ents = [replace(e, x=h - e.y, y=e.x, direction=(e.direction + 4) % 16) for e in b.entities]
            ports = [replace(p, x=h - 1 - p.y, y=p.x, direction=(p.direction + 4) % 16 if p.kind != "pole" else 0)
                     for p in b.ports]
            b = replace(b, entities=ents, ports=ports, size=(h, w))
        return b


# ---- geometry / research helpers -----------------------------------------------------------------------------
@lru_cache(maxsize=1)
def vanilla() -> GameData:
    """Geometry-only GameData for the vanilla entities blocks use (no research info)."""
    return GameData(json.loads(_VANILLA.read_text(encoding="utf-8")))


def _geo(gd):
    return gd if gd is not None else vanilla()


def buildable(gd, name: str) -> bool:
    """True when research allows building `name` (always True without research info)."""
    if gd is None or not gd.raw.get("recipes") or not any("enabled" in r for r in gd.raw["recipes"].values()):
        return True
    return gd.can_build(name)


def choose(gd, *names: str) -> str:
    """First buildable entity of `names` (in preference order); the last one if none is."""
    for n in names:
        if buildable(gd, n):
            return n
    return names[-1]


def options(gd) -> dict:
    """What the force can use now: best default entities and feature flags."""
    return {
        "drill": choose(gd, "electric-mining-drill", "burner-mining-drill"),
        "furnace": choose(gd, "steel-furnace", "stone-furnace"),
        "inserter": choose(gd, "inserter", "burner-inserter"),
        "pole": choose(gd, "medium-electric-pole", "small-electric-pole") if buildable(gd, "small-electric-pole")
        or buildable(gd, "medium-electric-pole") else None,
        "belt": choose(gd, "transport-belt"),
        "underground": buildable(gd, "underground-belt"),
        "splitter": buildable(gd, "splitter"),
        "long_handed": buildable(gd, "long-handed-inserter"),
        "steam": all(buildable(gd, n) for n in ("offshore-pump", "boiler", "steam-engine", "small-electric-pole")),
    }


def _put(G, name, tx, ty, direction=NORTH, **kw) -> PE:
    """Entity whose top-left tile is (tx, ty)."""
    w, h = G.size(name, direction)
    return PE(name, tx + w / 2, ty + h / 2, direction, **kw)


def _electric(G, name) -> bool:
    return (G.entity(name).get("energy_source") or {}).get("type") == "electric"


def _burner(G, name) -> bool:
    return (G.entity(name).get("energy_source") or {}).get("type") == "burner"


def _finish(block: Block, gd) -> Block:
    block.requires = sorted(n for n in block.counts() if not buildable(gd, n))
    if block.requires:
        block.notes.append("needs research: " + ", ".join(block.requires))
    return block


def _box(G, e: PE):
    return G.box(e.name, e.x, e.y, e.direction)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def powered_by(G, pole: PE, e: PE) -> bool:
    d = G.pole(pole.name)[0]
    return _overlap((pole.x - d, pole.y - d, pole.x + d, pole.y + d), _box(G, e))


def _wired(G, a: PE, b: PE) -> bool:
    reach = min(G.pole(a.name)[1], G.pole(b.name)[1])
    return math.hypot(a.x - b.x, a.y - b.y) <= reach + 1e-9


def pole_components(G, poles: list) -> list:
    """Connected pole groups (by wire reach) as lists of indices."""
    seen, comps = set(), []
    for i in range(len(poles)):
        if i in seen:
            continue
        comp, q = [], deque([i])
        seen.add(i)
        while q:
            a = q.popleft()
            comp.append(a)
            for b in range(len(poles)):
                if b not in seen and _wired(G, poles[a], poles[b]):
                    seen.add(b)
                    q.append(b)
        comps.append(comp)
    return comps


def _connect(G, poles: list, candidates: list) -> list:
    """Add candidate poles (shortest chains) until all poles form one network. Returns the poles added."""
    added = []
    while True:
        cur = poles + added
        comps = pole_components(G, cur)
        if len(comps) <= 1:
            return added
        main = set(comps[0])
        nodes = cur + [c for c in candidates if all((c.x, c.y) != (p.x, p.y) for p in cur)]
        prev = {i: None for i in main}
        q = deque(main)
        hit = None
        while q and hit is None:
            a = q.popleft()
            for b in range(len(nodes)):
                if b not in prev and _wired(G, nodes[a], nodes[b]):
                    prev[b] = a
                    if b < len(cur):
                        hit = b
                        break
                    q.append(b)
        if hit is None:
            return added  # can't connect; caller reports
        b = prev[hit]
        while b is not None and b not in main:
            if b >= len(cur):
                added.append(nodes[b])
            b = prev[b]


def _cover(G, pole: str, needs: list, candidates: list) -> list:
    """Greedy: poles from candidate tiles [(x, y)] so every entity in `needs` is in some pole's supply area.
    Picks, for the first uncovered entity, the candidate that covers the most uncovered entities."""
    placed, todo = [], list(needs)
    cands = [_put(G, pole, cx, cy) for cx, cy in candidates]
    while todo:
        e = todo[0]
        best = max((c for c in cands if powered_by(G, c, e)),
                   key=lambda c: (sum(powered_by(G, c, t) for t in todo), -c.y, -c.x), default=None)
        if best is None:
            raise ValueError(f"no pole spot can power {e.name} at ({e.x}, {e.y})")
        placed.append(best)
        cands.remove(best)
        todo = [t for t in todo if not powered_by(G, best, t)]
    return placed + _connect(G, placed, cands)


def _per_min(v) -> float:
    return round(v * 60, 4)


def fuel_per_min(G, entity: str, fuel: str = "coal", count: float = 1) -> float:
    """Fuel items/min for `count` burner entities running flat out."""
    kw = G.entity(entity).get("max_energy_usage_kw") or G.entity(entity).get("energy_usage_kw") or 0
    fv = G.fuel_value(fuel) / 1000.0  # kJ
    return round(count * kw * 60 / fv, 4) if fv else 0.0


def mining_rate(G, drill: str, resource: str, prod_bonus: float = 0.0) -> tuple[str, float]:
    """(item, items/s) of one drill on `resource`."""
    r = G.resource(resource)
    item = (r.get("products") or [{"name": resource}])[0]["name"]
    return item, G.entity(drill)["mining_speed"] / (r.get("mining_time") or 1) * (1 + prod_bonus)


def smelt_recipe(G, ore: str, furnace: str) -> dict:
    """The furnace recipe that takes `ore`: {name, ingredient amount, product, product amount, energy}."""
    cats = set(G.entity(furnace).get("crafting_categories") or ["smelting"])
    best = None
    for rn, r in G.raw["recipes"].items():
        ing = r.get("ingredients") or []
        if len(ing) == 1 and ing[0]["name"] == ore and cats & set(r.get("categories") or []) and r.get("products"):
            if best is None or (r.get("enabled") and not best[1].get("enabled")) or rn == ore.replace("-ore", "-plate"):
                best = (rn, r)
    if best is None:
        raise ValueError(f"no {furnace} recipe takes {ore}")
    rn, r = best
    p = r["products"][0]
    return {"name": rn, "in": ing_amount(r), "product": p["name"], "out": p.get("amount", 1), "energy": r["energy"]}


def ing_amount(r) -> float:
    return r["ingredients"][0].get("amount", 1)


def _belt_lane(G, belt) -> float:
    e = G.entity(belt)
    return e.get("lane_items_per_s") or e.get("belt_items_per_s", 15) / 2


# ---- mining ------------------------------------------------------------------------------------------------
def _patch(patch, resource):
    if hasattr(patch, "tiles"):
        return set(patch.tiles), resource or patch.name
    if isinstance(patch, dict):
        return set(patch), resource or next(iter(patch.values()), None)
    return set(patch), resource


def _inv_tile(t, k):
    """Map tile -> canonical tile (undo k clockwise turns)."""
    x, y = t[0] + 0.5, t[1] + 0.5
    for _ in range(k):
        x, y = y, -x
    return math.floor(x), math.floor(y)


def _fwd_pt(x, y, k):
    for _ in range(k):
        x, y = -y, x
    return x, y


def mining_outpost(patch_tiles, drill=None, belt="transport-belt", pole=None, output_side="north", gd=None, *,
                   resource=None, min_ore=None, max_drills=None, grid=None, merge=True, prod_bonus=None):
    """Drills covering the given patch region, in pairs facing a belt; belts flow toward `output_side` and
    (merge=True) collect into one output belt. Drills only where their mining area holds >= min_ore ore tiles
    (and, with `grid`, on placeable ground with no other resource in the mining area). Electric drills get poles in gap rows (one pole per 4 drills).
    patch_tiles: world.Patch, set of (x, y) map tiles, or {tile: resource}. Origin set to map coords."""
    G = _geo(gd)
    tiles, resource = _patch(patch_tiles, resource)
    if not tiles:
        raise ValueError("empty patch")
    drill = drill or choose(gd, "electric-mining-drill", "burner-mining-drill")
    electric = _electric(G, drill)
    if electric and not pole:
        pole = choose(gd, "medium-electric-pole", "small-electric-pole")
    s = G.size(drill)[0]
    rad = G.entity(drill).get("mining_drill_radius") or s / 2
    k = SIDES[output_side]
    ore = {_inv_tile(t, k) for t in tiles}

    def map_tiles(ts):
        out = set()
        for tx, ty in ts:
            x, y = _fwd_pt(tx + 0.5, ty + 0.5, k)
            out.add((math.floor(x), math.floor(y)))
        return out

    area_cache = {}

    def area(tx, ty):  # ore tiles mined by a drill with top-left (tx, ty)
        key = (tx, ty)
        if key not in area_cache:
            c = (tx + s / 2, ty + s / 2)
            area_cache[key] = tiles_of_box(c[0] - rad, c[1] - rad, c[0] + rad, c[1] + rad) & ore
        return area_cache[key]

    n_area = len(tiles_of_box(0, 0, 2 * rad, 2 * rad))
    min_ore = min_ore if min_ore is not None else max(1, n_area // 4)
    pair_w, period = 2 * s + 1, (2 * s + 1 if electric else s)
    xs, ys = [t[0] for t in ore], [t[1] for t in ore]
    cx0, cx1, cy0, cy1 = min(xs), max(xs), min(ys), max(ys)
    ok_cache = {}

    def placeable(tx, ty):
        if grid is None:
            return True
        if (tx, ty) not in ok_cache:
            c = (tx + s / 2, ty + s / 2)
            mined = map_tiles(tiles_of_box(c[0] - rad, c[1] - rad, c[0] + rad, c[1] + rad))
            ok_cache[(tx, ty)] = (grid.can_place(map_tiles((tx + i, ty + j) for i in range(s) for j in range(s)))
                                  and all(grid.resource(*t) in (None, resource) for t in mined))  # no mixed ore
        return ok_cache[(tx, ty)]

    def layout(ox, oy):
        drills = []
        X = cx0 - ox
        while X <= cx1:
            Y = cy0 - oy
            while Y <= cy1:
                rows = [Y + 1, Y + s + 1] if electric else [Y]
                for ry in rows:
                    for side, dx in ((0, 0), (1, s + 1)):
                        tx = X + dx
                        a = area(tx, ry)
                        if len(a) >= min_ore and placeable(tx, ry):
                            drills.append((tx, ry, side, X, Y))
                Y += period
            X += pair_w
        return drills

    best = None
    for ox in range(pair_w):
        for oy in range(period):
            d = layout(ox, oy)
            if max_drills:
                d = sorted(d, key=lambda t: (t[1], t[0]))[:max_drills]
            cov = len(set().union(*(area(t[0], t[1]) for t in d))) if d else 0
            key = (cov, -len(d))
            if best is None or key > best[0]:
                best = (key, d, ox, oy)
    (covered, _), drills, ox, oy = best
    if not drills:
        raise ValueError(f"no drill spot with >= {min_ore} ore tiles")

    ents = []
    for tx, ty, side, X, Y in drills:
        ents.append(_put(G, drill, tx, ty, EAST if side == 0 else WEST,
                         extra=None if electric else {"fuel": "manual"}))
    # drop point must be on the pair's belt column; belts end at the last drop row
    last_drop = {}
    for e, (tx, ty, side, X, Y) in zip(ents, drills):
        vx, vy = rotate(*G.entity(drill)["vector_to_place_result"], e.direction)
        if math.floor(e.x + vx) != X + s:
            raise ValueError(f"{drill} output does not land on the belt column")
        last_drop[X] = max(last_drop.get(X, -10 ** 9), math.floor(e.y + vy))
    pairs = sorted({d[3] for d in drills})
    poles = []
    if electric:
        gap_rows = sorted({d[4] for d in drills} | {d[4] + period for d in drills})
        needs = list(ents)
        cands = [(X + s - 1, Y) for X in range(pairs[0], pairs[-1] + 1, pair_w)
                 for Y in range(min(gap_rows), max(gap_rows) + 1, period)]
        if grid is not None:
            cands = [c for c in cands if grid.can_place(map_tiles([c]))]
        poles = _cover(G, pole, needs, cands)
    top = min([d[1] for d in drills] + [p.y - 0.5 for p in poles])
    top = int(top)
    belts = []
    for X in pairs:
        belts += [_put(G, belt, X + s, y, NORTH) for y in range(top, last_drop[X] + 1)]
    ports = []
    item, rate = mining_rate(G, drill, resource, prod_bonus or 0) if resource else (resource, 0)
    if len(pairs) > 1 and merge:
        xa, xb = pairs[0] + s, pairs[-1] + s
        belts += [_put(G, belt, x, top - 1, EAST) for x in range(xa, xb)]
        belts.append(_put(G, belt, xb, top - 1, NORTH))
        ports.append(Port("belt_out", item, xb, top - 2, NORTH))
    else:
        ports += [Port("belt_out", item, X + s, top - 1, NORTH) for X in pairs]
    ents += belts + poles
    if poles:
        p0 = min(poles, key=lambda p: (p.y, p.x))
        ports.append(Port("pole", None, math.floor(p0.x), math.floor(p0.y), 0))
    # normalize to local coords
    fp = set()
    for e in ents:
        fp |= G.footprint(e.name, e.x, e.y, e.direction)
    bx0, by0 = min(t[0] for t in fp), min(t[1] for t in fp)
    bx1, by1 = max(t[0] for t in fp) + 1, max(t[1] for t in fp) + 1
    ents = [e.moved(-bx0, -by0) for e in ents]
    ports = [p.moved(-bx0, -by0) for p in ports]
    b = Block("mining_outpost", ents, (bx1 - bx0, by1 - by0), ports)
    b = b.rotated(k)
    corners = [_fwd_pt(x, y, k) for x in (bx0, bx1) for y in (by0, by1)]
    b.origin = (int(round(min(c[0] for c in corners))), int(round(min(c[1] for c in corners))))
    n = len(drills)
    per_lane = _belt_lane(G, belt)
    if item:
        b.rates = {item: _per_min(rate * n)}
        n_last = sum(1 for d in drills if d[3] == pairs[-1])
        if len(pairs) > 1 and merge and rate * (n - n_last) > per_lane + 1e-9:
            b.notes.append(f"merged output: all columns but the last share one lane ({per_lane}/s) - "
                           f"{rate * (n - n_last):.2f}/s will back up")
        if rate * n > G.belt_throughput(belt):
            b.notes.append(f"{n} drills make {rate * n:.2f}/s > one {belt} ({G.belt_throughput(belt)}/s)")
    if electric:
        b.rates["power_kw"] = round(n * (G.entity(drill).get("energy_usage_kw") or 0), 1)
    else:
        b.rates["coal"] = -fuel_per_min(G, drill, "coal", n)
        b.notes.append("burner drills: fuel manually (helper refuel) - nothing feeds them")
    b.info = {"drills": n, "covered": covered, "patch_tiles": len(tiles), "resource": resource}
    b.notes.append(f"{n} {drill} cover {covered}/{len(tiles)} ore tiles")
    return _finish(b, gd)


def burner_mining_column(n=4, drill="burner-mining-drill", belt="transport-belt", output_side="north", gd=None, *,
                         sides=2, resource=None):
    """n drills (burner by default) in a column on both sides (sides=2) or one side of a belt flowing to
    `output_side`. No poles: for burner drills only; the placer puts it on ore. Fuel is manual."""
    G = _geo(gd)
    if _electric(G, drill):
        raise ValueError("burner_mining_column is for burner drills; use mining_outpost for electric drills")
    s = G.size(drill)[0]
    ents = []
    rows = math.ceil(n / sides)
    for i in range(n):
        side, r = (i % sides, i // sides) if sides == 2 else (0, i)
        ents.append(_put(G, drill, 0 if side == 0 else s + 1, r * s, EAST if side == 0 else WEST,
                         extra={"fuel": "manual"}))
    last = max(math.floor(e.y + rotate(*G.entity(drill)["vector_to_place_result"], e.direction)[1]) for e in ents)
    ents += [_put(G, belt, s, y, NORTH) for y in range(last + 1)]
    w = 2 * s + 1 if sides == 2 else s + 1
    b = Block("burner_mining_column", ents, (w, rows * s), [Port("belt_out", None, s, -1, NORTH)])
    if resource:
        item, rate = mining_rate(G, drill, resource)
        b.ports[0].item = item
        b.rates[item] = _per_min(rate * n)
    b.rates["coal"] = -fuel_per_min(G, drill, "coal", n)
    b.notes.append(f"place on {resource or 'ore'}: every drill footprint must be on ore; fuel manually")
    return _finish(b.rotated(SIDES[output_side]), gd)


# ---- on-site bootstrap (planner v2) ------------------------------------------------------------------------
def burner_pair_row(n=4, gd=None, *, drill="burner-mining-drill", furnace="stone-furnace",
                    inserter="burner-inserter", chest="wooden-chest", ore="iron-ore", output_side="north"):
    """n burner drills on ore, each dropping straight into a stone furnace in front of it (direct insertion, no
    belts); a burner inserter takes the plates out of each furnace into its own chest. Engine-verified as the
    validator case drill_furnace_ok. Canonical (north): per unit i, columns 2i..2i+1: chest (2i, 0), inserter
    (2i, 1), furnace rows 2-3, drill rows 4-5 (on the ore, facing north). Every burner is hand-fed (fuel=manual:
    the helper's refuel covers them). info["drill_tiles"]: local tiles that must be ore."""
    G = _geo(gd)
    s = G.size(drill)[0]
    fs = G.size(furnace)[0]
    if s != 2 or fs != 2:
        raise ValueError("burner_pair_row expects 2x2 drills and furnaces")
    man = {"fuel": "manual"}
    ents, drill_tiles = [], []
    for i in range(n):
        x = 2 * i
        ents.append(_put(G, chest, x, 0))
        ents.append(_put(G, inserter, x, 1, SOUTH, extra=dict(man)))
        ents.append(_put(G, furnace, x, 2, extra=dict(man)))
        d = _put(G, drill, x, 4, NORTH, extra=dict(man))
        vx, vy = rotate(*G.entity(drill)["vector_to_place_result"], NORTH)
        if not (x <= math.floor(d.x + vx) <= x + 1 and 2 <= math.floor(d.y + vy) <= 3):
            raise ValueError(f"{drill} does not drop into the furnace in front of it")
        ents.append(d)
        drill_tiles += [(x + a, 4 + b) for a in range(2) for b in range(2)]
    b = Block("burner_pair_row", ents, (2 * n, 6), [])
    rec = smelt_recipe(G, ore, furnace)
    _item, rate = mining_rate(G, drill, ore)
    speed = G.entity(furnace).get("crafting_speed") or 1
    crafts = min(rate / rec["in"], speed / rec["energy"])          # per unit, per s
    b.rates = {rec["product"]: _per_min(crafts * rec["out"] * n),
               "coal": -round(fuel_per_min(G, drill, "coal", n) + fuel_per_min(G, furnace, "coal", n) * min(
                   1.0, crafts / (speed / rec["energy"])) + _ins_coal(G, inserter, crafts * rec["out"]) * n, 4)}
    b.info = {"pairs": n, "ore": ore, "product": rec["product"], "drill_tiles": drill_tiles, "chest": chest}
    b.notes.append(f"{n} burner pairs: {b.rates[rec['product']]}/min {rec['product']} into {n} chests; "
                   "drills, furnaces and inserters are fuelled by hand")
    return _finish(b.rotated(SIDES[output_side]), gd)


def _ins_coal(G, inserter, items_per_s) -> float:
    """Coal/min a burner inserter burns moving items_per_s (energy while swinging, ~busy fraction)."""
    if not _burner(G, inserter):
        return 0.0
    try:
        tp = G.inserter_throughput(inserter, stack=1) or 1
    except KeyError:  # geometry-only data (no qualities): a burner inserter moves ~0.6 items/s
        tp = 0.6
    busy = min(1.0, items_per_s / tp)
    return busy * fuel_per_min(G, inserter, "coal", 1)


def drill_to_chest(n=2, gd=None, *, drill="burner-mining-drill", chest="wooden-chest", resource="coal",
                   output_side="north"):
    """n burner drills on `resource`, each dropping straight into its own chest (coal/stone gathering for the
    bootstrap; no belts). Canonical (north): per unit i: chest (2i, 0), drill rows 1-2 facing north. Fuel by hand
    (a coal drill's chest is where the helper refuels from)."""
    G = _geo(gd)
    s = G.size(drill)[0]
    if s != 2:
        raise ValueError("drill_to_chest expects 2x2 drills")
    ents, drill_tiles = [], []
    for i in range(n):
        x = 2 * i
        d = _put(G, drill, x, 1, NORTH, extra={"fuel": "manual"})
        vx, vy = rotate(*G.entity(drill)["vector_to_place_result"], NORTH)
        cx, cy = math.floor(d.x + vx), math.floor(d.y + vy)
        if cy != 0 or not x <= cx <= x + 1:
            raise ValueError(f"{drill} does not drop onto row 0")
        ents.append(_put(G, chest, cx, 0))
        ents.append(d)
        drill_tiles += [(x + a, 1 + b) for a in range(2) for b in range(2)]
    b = Block("drill_to_chest", ents, (2 * n, 3), [])
    item, rate = mining_rate(G, drill, resource)
    b.rates = {item: _per_min(rate * n)}
    cpm = fuel_per_min(G, drill, "coal", n)
    if item == "coal":
        b.rates["coal"] = round(b.rates["coal"] - cpm, 4)
    else:
        b.rates["coal"] = -cpm
    b.info = {"drills": n, "resource": resource, "item": item, "drill_tiles": drill_tiles, "chest": chest}
    b.notes.append(f"{n} burner drills on {resource} into chests: {_per_min(rate * n)}/min gross")
    return _finish(b.rotated(SIDES[output_side]), gd)


# ---- smelting ----------------------------------------------------------------------------------------------
def smelter_array(n, furnace=None, fuel_mode="mixed_lane", output_side="north", inserters=None, gd=None, *,
                  belt="transport-belt", pole=None, ore="iron-ore", fuel="coal", input_inserter=None):
    """A column of n furnaces: input belt | inserters | furnaces | inserters | output belt, both belts flowing
    toward `output_side`. fuel_mode:
      mixed_lane - one input belt, ore on the left lane + fuel on the right (port lanes say which);
      separate   - ore and fuel arrive on their own belts (ports on the input end, left and right) and are
                   side-loaded onto the two lanes inside the block;
      none       - electric furnaces (no fuel).
    Burner output inserters can't refuel themselves (no burner leech in 2.1): they are marked fuel=manual.
    Electric inserters/furnaces get poles in the inserter columns. input_inserter (default = inserters) can be a
    faster one when a fast furnace needs more input than one inserter swings (e.g. stone in electric furnaces)."""
    G = _geo(gd)
    furnace = furnace or choose(gd, "stone-furnace")
    inserters = inserters or "burner-inserter"
    if _electric(G, furnace):
        fuel_mode = "none"
    if fuel_mode not in ("mixed_lane", "separate", "none"):
        raise ValueError(f"fuel_mode {fuel_mode!r}")
    s = G.size(furnace)[0]
    H = n * s
    xin, xi, xf, xo, xout = 0, 1, 2, 2 + s, 3 + s
    ents = []
    for i in range(n):
        y = i * s
        ents.append(_put(G, furnace, xf, y))
        ents.append(_put(G, input_inserter or inserters, xi, y, WEST))
        out_extra = {"fuel": "manual"} if _burner(G, inserters) else None
        ents.append(_put(G, inserters, xo, y, WEST, extra=out_extra))
    ents += [_put(G, belt, xin, y, NORTH) for y in range(H)]
    ents += [_put(G, belt, xout, y, NORTH) for y in range((n - 1) * s + 1)]  # ends at the last output inserter
    rec = smelt_recipe(G, ore, furnace)
    ports = [Port("belt_out", rec["product"], xout, -1, NORTH, "right")]
    h = H
    if fuel_mode == "mixed_lane":
        ports += [Port("belt_in", ore, xin, H, NORTH, "left"), Port("belt_in", fuel, xin, H, NORTH, "right")]
    elif fuel_mode == "separate":
        # row H: ore side-loads from the west onto the left lane, fuel from the east onto the right lane;
        # row H+1: a belt behind the merge tile keeps it straight
        ents += [_put(G, belt, xin, H, NORTH), _put(G, belt, xin, H + 1, NORTH, extra={"role": "straightener"})]
        ents += [_put(G, belt, x, H, WEST) for x in range(1, xout + 1)]
        ports += [Port("belt_in", ore, -1, H, EAST), Port("belt_in", fuel, xout + 1, H, WEST)]
        h = H + 2
    else:
        ports += [Port("belt_in", ore, xin, H, NORTH)]
    electric = [e for e in ents if _electric(G, e.name)]
    if electric:
        pole = pole or choose(gd, "medium-electric-pole", "small-electric-pole")
        used = {(math.floor(e.x), math.floor(e.y)) for e in ents}
        cands = [(x, y) for x in (xi, xo) for y in range(H) if (x, y) not in used]
        poles = _cover(G, pole, electric, cands)
        ents += poles
        p0 = min(poles, key=lambda p: (p.y, p.x))
        ports.append(Port("pole", None, math.floor(p0.x), math.floor(p0.y), 0))
    b = Block("smelter_array", ents, (xout + 1, h), ports)
    speed = G.entity(furnace).get("crafting_speed") or 1
    crafts = n * speed / rec["energy"]  # per s
    b.rates = {ore: -_per_min(crafts * rec["in"]), rec["product"]: _per_min(crafts * rec["out"])}
    lane = _belt_lane(G, belt)
    if crafts * rec["out"] > lane + 1e-9:
        b.notes.append(f"output {crafts * rec['out']:.2f}/s exceeds one lane ({lane}/s): use <= "
                       f"{int(lane / (speed / rec['energy'] * rec['out']))} furnaces")
    need = speed / rec["energy"] * rec["in"]
    have = G.inserter_throughput(input_inserter or inserters)
    if need > have + 1e-9:
        b.notes.append(f"input {input_inserter or inserters} moves ~{have:.2f}/s but each furnace needs {need:.2f}/s "
                       f"{ore} (without inserter capacity research): use a faster input_inserter")
    if fuel_mode != "none" and crafts * rec["in"] > lane + 1e-9:
        b.notes.append(f"ore {crafts * rec['in']:.2f}/s exceeds the ore lane ({lane}/s)")
    if fuel_mode != "none":
        b.rates[fuel] = -fuel_per_min(G, furnace, fuel, n)
        if _burner(G, inserters):
            b.notes.append(f"output {inserters}s need fuel by hand (measured ~90 items per coal: 10 coal "
                           "last ~45 min per furnace); input inserters fuel themselves from the fuel lane")
    if electric:
        b.rates["power_kw"] = round(sum(G.entity(e.name).get("max_energy_usage_kw") or 0 for e in electric), 1)
    b.info = {"recipe": rec["name"], "furnaces": n, "fuel_mode": fuel_mode}
    return _finish(b.rotated(SIDES[output_side]), gd)


# ---- power -------------------------------------------------------------------------------------------------
def steam_power(boilers=1, engines=None, gd=None, *, boiler="boiler", engine="steam-engine",
                inserter="burner-inserter", belt="transport-belt", pole=None, water_side="west", fuel="coal",
                feed="belt"):
    """Boilers in a row (water chained through them) with up to 2 steam engines each, burner inserters feeding
    fuel from a belt behind them. Canonical (water_side="west"): water pipe_in at the west end of the boiler row,
    engines north, fuel belt along the south (fuel belt_in at its east end). Units are paired around a 1-tile
    gap column holding the poles (and a pipe joining the pair's water). The offshore pump is placed by the
    placer at the pipe_in port: 1 pump : 20 boilers : 40 engines.
    feed="manual" (planner v2 stage 2, a small plant for labs): no fuel belt and no inserters; the boilers are
    fuelled by hand (extra fuel=manual, the helper's coal round keeps them filled)."""
    G = _geo(gd)
    engines = 2 * boilers if engines is None else engines
    if not boilers <= engines <= 2 * boilers:
        raise ValueError("need boilers <= engines <= 2*boilers")
    pole = pole or choose(gd, "medium-electric-pole", "small-electric-pole")
    ew, eh = G.size(engine)
    bw, bh = G.size(boiler)
    two = engines > boilers
    top_e1 = eh if two else 0          # engine1 rows top_e1 .. top_e1+eh-1
    yb = top_e1 + eh                   # boiler top row
    yi, yc = yb + bh, yb + bh + 1      # inserter row, fuel belt row
    if feed not in ("belt", "manual"):
        raise ValueError(f"feed {feed!r}")
    manual = feed == "manual"
    ents, poles, xs = [], [], []
    for i in range(boilers):
        x = (i // 2) * (2 * bw + 1) + (i % 2) * (bw + 1)
        xs.append(x)
        ents.append(_put(G, boiler, x, yb, NORTH, extra={"fuel": "manual"} if manual else None))
        ents.append(_put(G, engine, x, top_e1, NORTH))
        if i < engines - boilers:
            ents.append(_put(G, engine, x, 0, NORTH))
        if not manual:
            ents.append(_put(G, inserter, x + bw // 2, yi, SOUTH))
    w = xs[-1] + bw + (boilers % 2)  # a lone last unit keeps its gap column for the poles
    for i in range(0, boilers, 2):
        gx = xs[i] + bw
        if i + 1 < boilers:
            ents.append(_put(G, "pipe", gx, yb + bh - 1))
    if not manual:
        ents += [_put(G, belt, x, yc, WEST) for x in range(w)]
    gens = [e for e in ents if e.name == engine]
    cands = [(xs[i] + bw, y) for i in range(0, boilers, 2) for y in range(0, yb)]
    poles = _cover(G, pole, gens, cands)
    ents += poles
    # water input: the first boiler's west connection
    wc = [c for c in G.fluid_connections(boiler, ents[0].x, ents[0].y, NORTH) if c[2] == WEST][0]
    p0 = min(poles, key=lambda p: (p.y, p.x))
    ports = [Port("pipe_in", "water", wc[0], wc[1], EAST)]
    if not manual:
        ports.append(Port("belt_in", fuel, w, yc, WEST))
    ports.append(Port("pole", None, math.floor(p0.x), math.floor(p0.y), 0))
    b = Block("steam_power", ents, (w, yb + bh if manual else yc + 1), ports)
    e_kw = G.entity(engine).get("max_energy_production_kw") or 900
    b_kw = G.entity(boiler).get("max_energy_usage_kw") or 1800
    steam = boilers * min(b_kw, 2 * e_kw) / e_kw * (G.entity(engine).get("fluid_usage_per_s") or 30)
    pump = G.entity("offshore-pump").get("pumping_speed_per_s") or 1200
    b.rates = {"power_kw": round(min(engines * e_kw, boilers * b_kw), 1), fuel: -fuel_per_min(G, boiler, fuel, boilers),
               "water": -_per_min(steam)}
    b.info = {"boilers": boilers, "engines": engines, "offshore_pumps": math.ceil(steam / pump)}
    b.notes.append(f"{boilers} boilers : {engines} engines, needs {b.info['offshore_pumps']} offshore pump(s) "
                   "at the pipe_in port")
    if boilers > 20:
        b.notes.append("more than 20 boilers on one water input: split into several blocks")
    return _finish(b.rotated({"west": 0, "north": 1, "east": 2, "south": 3}[water_side]), gd)


def lab_row(n=2, gd=None, *, lab="lab", gap=1):
    """n labs in a row, `gap` free tiles between them (poles are placed afterwards by router.place_poles, which
    covers them and joins them to the power line). Science packs go in by hand (the player or the helper's insert
    task): no inserters, no belts. rates["power_kw"] = the labs' draw."""
    G = _geo(gd)
    w, h = G.size(lab)
    ents = [_put(G, lab, i * (w + gap), 0, extra={"input": "manual"}) for i in range(n)]
    b = Block("lab_row", ents, (n * w + (n - 1) * gap, h), [])
    b.rates = {"power_kw": round(n * (G.entity(lab).get("energy_usage_kw") or
                                      G.entity(lab).get("max_energy_usage_kw") or 60), 1)}
    b.info = {"labs": n}
    return _finish(b, gd)


# ---- logistics helpers -------------------------------------------------------------------------------------
def bus_start(lanes, length=6, gd=None, *, belt="transport-belt", output_side="north", group=4):
    """Parallel belts (one per item, in order) flowing toward `output_side`, a 1-tile gap after every `group`
    lanes (room for splitters/undergrounds later). belt_in ports at the start, belt_out at the end."""
    G = _geo(gd)
    ents, ports, x = [], [], 0
    for i, item in enumerate(lanes):
        if i and i % group == 0:
            x += 1
        ents += [_put(G, belt, x, y, NORTH) for y in range(length)]
        ports += [Port("belt_in", item, x, length, NORTH), Port("belt_out", item, x, -1, NORTH)]
        x += 1
    b = Block("bus_start", ents, (x, length), ports)
    b.notes.append(f"{len(lanes)} lanes, {G.belt_throughput(belt)}/s each")
    return _finish(b.rotated(SIDES[output_side]), gd)


def lane_merge(left_item, right_item, gd=None, *, belt="transport-belt", output_side="north"):
    """Side-load two belts onto the two lanes of one (e.g. ore + coal -> a mixed-lane smelter feed).
    left_item arrives from the left (west when flowing north), right_item from the right."""
    G = _geo(gd)
    ents = [_put(G, belt, 1, 0, NORTH), _put(G, belt, 1, 1, NORTH, extra={"role": "straightener"}),
            _put(G, belt, 0, 0, EAST), _put(G, belt, 2, 0, WEST)]
    ports = [Port("belt_in", left_item, -1, 0, EAST), Port("belt_in", right_item, 3, 0, WEST),
             Port("belt_out", left_item, 1, -1, NORTH, "left"), Port("belt_out", right_item, 1, -1, NORTH, "right")]
    b = Block("lane_merge", ents, (3, 2), ports)
    b.notes.append(f"each lane carries at most {_belt_lane(G, belt)}/s")
    return _finish(b.rotated(SIDES[output_side]), gd)


def coal_splitter(gd=None, *, item="coal", belt="transport-belt", splitter="splitter", output_side="north"):
    """Split one belt into two (needs the splitter: logistics research). Outputs left (x=0) and right (x=1)."""
    G = _geo(gd)
    ents = [_put(G, splitter, 0, 0, NORTH), _put(G, belt, 0, 1, NORTH)]
    ports = [Port("belt_in", item, 0, 2, NORTH), Port("belt_out", item, 0, -1, NORTH),
             Port("belt_out", item, 1, -1, NORTH)]
    return _finish(Block("coal_splitter", ents, (2, 2), ports).rotated(SIDES[output_side]), gd)


def inserter_tap(gd=None, *, item="coal", inserter="burner-inserter", belt="transport-belt", output_side="north"):
    """Branch without a splitter: an inserter moves items from the main belt (left, passes through) onto a
    side belt (right). ~0.8 items/s with a burner inserter (enough for ~1.7 boilers or ~35 stone furnaces)."""
    G = _geo(gd)
    ents = [_put(G, belt, 0, 0, NORTH), _put(G, belt, 0, 1, NORTH), _put(G, inserter, 1, 1, WEST),
            _put(G, belt, 2, 0, NORTH), _put(G, belt, 2, 1, NORTH)]
    ports = [Port("belt_in", item, 0, 2, NORTH), Port("belt_out", item, 0, -1, NORTH),
             Port("belt_out", item, 2, -1, NORTH, "right")]
    b = Block("inserter_tap", ents, (3, 2), ports)
    b.rates = {item: _per_min(G.inserter_throughput(inserter, stack=1))}
    if _burner(G, inserter) and not G.fuel_value(item):
        ents[2].extra = {"fuel": "manual"}
    return _finish(b.rotated(SIDES[output_side]), gd)


# ---- planner v3: master-plan blocks (docs/design/PLANNER-v3.md section 6) -------------------------------------------
# Every entity carries extra {"role": ..., "unit": i, "row": r}: the master plan stage-tags entities from these, and
# derives swap slots and pole positions from data instead of geometry. Where the two phases differ a block holds
# BOTH (the burner drill + burner inserter of a slot and the electric drill that replaces them; the burner and the
# electric output inserter of one tile; a buffer chest and the plate belt that replaces it). A block is therefore
# never validated as a whole, only as the stage subsets master.stage_entities() picks.
FIELD_W = 9           # corridor | slot (3) | ore belt | slot (3) | spare column
FIELD_PERIOD = 7      # rows per two slots along the belt: slot (3) + gap row (pole) + slot (3)
FIELD_BELT_X = 4
COLUMN_W = 11         # plate | out | furnace (2) | in | ORE/COAL | in | furnace (2) | out | plate


def _r(role, **kw) -> dict:
    d = {"role": role}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def field_slot_top(s: int) -> int:
    """Top row of the s-th slot along a mining field's belt (row 0 is the head row)."""
    return 1 + FIELD_PERIOD * (s // 2) + (4 if s % 2 else 0)


def _drop(G, e) -> tuple:
    vx, vy = rotate(*G.entity(e.name)["vector_to_place_result"], e.direction)
    return e.x + vx, e.y + vy


def mining_field(n_slots, gd=None, *, resource=None, self_feed=False, head_feed=False, mixed=False,
                 belt="transport-belt", pole="small-electric-pole", burner="burner-mining-drill",
                 electric="electric-mining-drill", burner_inserter="burner-inserter") -> Block:
    """One lattice, two phases (PLANNER-v3 6.1). Canonical: the ore belt (x=4) flows north to the head (row 0) and
    out at port (4, -1); slot k (k even: west x 1..3 facing east, k odd: east x 5..7 facing west) is a 3x3
    electric-drill slot at rows field_slot_top(k // 2) .. +2. An east slot's burner phase: the 2x2 burner drill in
    the slot's belt-side columns and top rows (it drops on the same belt lane as the electric drill) and a burner
    inserter at (7, top) feeding it coal from the corridor belt at x=8. The corridor flows north from port (8, yt)
    at the tail to port (8, -1) at the head, so coal leaves the field right next to the ore (coal on the right, as
    the smelter column takes it) and the coal chain continues. Only east slots have a burner phase. Small poles sit
    in the gap rows at x=3 (pitch 7, 4 drills each). self_feed (coal fields): the head turns the coal belt east into
    its own corridor, which then flows south and leaves at the tail port (8, yt). head_feed (a field whose furnaces
    get their coal another way, PLANNER-v3 round 2 "split" chains): the corridor is fed at the head, port (8, -1)
    flowing south, right next to the ore port, and ends at the tail (the burner phase only; retired with the burner
    drills). Entity extras: role drill_b/ins_b/drill_e (unit = slot k, drop_row), ore_belt/feed/corr (row),
    field_pole (unit = period).
    mixed (planner v3 round 3, the hub's ore fields): the ore and the coal that has passed the corridor leave on ONE
    belt, ore on its left lane and coal on its right (a merge at the head, the smelter column's merge turned
    around: both field lanes side-load onto the left lane, the coal onto the right one; role fmerge). That belt
    feeds a column with a mixed input straight (smelter_column(mixed_in=True)): one trunk instead of an ore belt and
    a coal belt side by side, and the coal leg to the field stays in use after the electric drills (it carries the
    furnaces' coal on). The block is then 2 rows taller (the merge rows on top) and its ports are [mixed out at
    (6, -1), coal in at the tail]."""
    if mixed and (self_feed or head_feed):
        raise ValueError("a mixed field is fed at the tail and is not a coal field")
    G = _geo(gd)
    if n_slots < 1:
        raise ValueError("a mining field needs at least one slot")
    per_side = math.ceil(n_slots / 2)
    ents, max_drop = [], 0
    for k in range(n_slots):
        west, top = k % 2 == 0, field_slot_top(k // 2)
        if west:
            de = _put(G, electric, 1, top, EAST, extra=_r("drill_e", unit=k))
            pair = []
        else:
            de = _put(G, electric, 5, top, WEST, extra=_r("drill_e", unit=k))
            pair = [_put(G, burner, 5, top, WEST, extra=_r("drill_b", unit=k)),
                    _put(G, burner_inserter, 7, top, EAST, extra=_r("ins_b", unit=k))]
        lanes = []
        for d in [de] + pair[:1]:
            px, py = _drop(G, d)
            if math.floor(px) != FIELD_BELT_X:
                raise ValueError(f"{d.name} in slot {k} does not drop on the field belt")
            lanes.append("left" if px - FIELD_BELT_X < 0.5 else "right")
            d.extra["drop_row"] = math.floor(py)
            max_drop = max(max_drop, math.floor(py))
        if len(set(lanes)) != 1:
            raise ValueError(f"slot {k}: burner and electric drill drop on different lanes")
        de.extra["lane"] = lanes[0]
        ents += [de] + pair
    yt = field_slot_top(per_side - 1) + 3          # first row below the last slot: the corridor's tail port row
    start = 1 if self_feed else 0
    ents += [_put(G, belt, FIELD_BELT_X, r, NORTH, extra=_r("ore_belt", row=r)) for r in range(start, max_drop + 1)]
    if self_feed:
        ents += [_put(G, belt, x, 0, EAST, extra=_r("feed", row=0)) for x in range(FIELD_BELT_X, 8)]
        ents += [_put(G, belt, 8, r, SOUTH, extra=_r("corr", row=r)) for r in range(0, yt)]
        ports = [Port("belt_out", "coal", 8, yt, SOUTH)]
    elif head_feed:
        # down to the last burner inserter's row only: a belt that runs past every consumer and stops
        last = max(e.y for e in ents if (e.extra or {}).get("role") == "ins_b") if any(
            (e.extra or {}).get("role") == "ins_b" for e in ents) else 0
        ents += [_put(G, belt, 8, r, SOUTH, extra=_r("corr", row=r)) for r in range(0, math.floor(last) + 1)]
    else:
        ents += [_put(G, belt, 8, r, NORTH, extra=_r("corr", row=r)) for r in range(0, yt)]
    for p in range(math.ceil(per_side / 2)):
        ents.append(_put(G, pole, 3, 1 + FIELD_PERIOD * p + 3, extra=_r("field_pole", unit=p)))
    item = None
    if resource:
        item = mining_rate(G, electric, resource)[0]
    if head_feed:
        ports = [Port("belt_out", item, FIELD_BELT_X, -1, NORTH), Port("belt_in", "coal", 8, -1, SOUTH)]
    elif not self_feed:
        ports = [Port("belt_out", item, FIELD_BELT_X, -1, NORTH), Port("belt_in", "coal", 8, yt, NORTH),
                 Port("belt_out", "coal", 8, -1, NORTH)]
    # the block is as tall as its entities (a field with a single slot pair still carries its gap-row pole below it)
    h = max([yt] + [math.ceil(e.y + G.size(e.name, e.direction)[1] / 2) for e in ents])
    if mixed:
        # merge rows -2..-1: ore (x 4) turns east and side-loads onto the mixed belt's west (left) lane at (6, -2),
        # coal (x 8) turns west onto its east (right) lane; (6, -1) is the straightener behind it (a belt fed from
        # behind takes side inputs as side-loads); then everything moves 2 rows down
        M_ = [(FIELD_BELT_X, -1, NORTH), (FIELD_BELT_X, -2, EAST), (5, -2, EAST), (6, -2, NORTH), (6, -1, NORTH),
              (8, -1, NORTH), (8, -2, WEST), (7, -2, WEST)]
        ents += [_put(G, belt, x, y, d, extra=_r("fmerge", row=y, note="straightener" if (x, y) == (6, -1) else None))
                 for x, y, d in M_]
        ents = [e.moved(0, 2) for e in ents]
        ports = [Port("belt_out", item, 6, -1, NORTH), Port("belt_in", "coal", 8, yt + 2, NORTH)]
        h += 2
        yt += 2
    b = Block("mining_field", ents, (FIELD_W, h), ports)
    rate_e = mining_rate(G, electric, resource)[1] if resource else 0.5
    rate_b = mining_rate(G, burner, resource)[1] if resource else 0.25
    b.info = {"slots": n_slots, "burner_slots": n_slots // 2, "resource": resource, "item": item,
              "self_feed": self_feed, "rate_electric": rate_e, "rate_burner": rate_b, "tail_row": yt}
    b.rates = {"power_kw": round(n_slots * (G.entity(electric).get("energy_usage_kw") or 90), 1)}
    return b


def smelter_column(n_pairs, gd=None, *, ore="iron-ore", furnace="stone-furnace", belt="transport-belt",
                   pole="small-electric-pole", chest="wooden-chest", mixed_in=False) -> Block:
    """Mirrored smelting column (PLANNER-v3 6.2), canonical: plate belts at x=0 and x=10 flow north out of the top
    (ports (0, -1) and (10, -1)); pair i = furnaces at rows 2i..2i+1 (x 2-3 and 7-8); the middle belt (x=5) flows
    north from the merge row L = 2n: ore side-loads onto its west (left) lane from port (4, L+1), coal onto its east
    (right) lane from port (6, L+1). Input inserters (x 4, 6) and output inserters (x 1, 9) each come as a burner one
    (in_b / out_b, until power) and the electric one on the same tile (in_e / out_e): a burner inserter that moves ore
    AND coal only refuels when its furnace asks for coal, and dies holding ore when that comes too late (round-1
    live finding), so from steam power on every column inserter is electric. Until the plate belts exist each output
    inserter drops into a buffer chest on the plate-belt tile. Poles: (1, 4j+1), (9, 4j+1) and (4, 4j+1), one per
    two pairs per inserter column. mixed_in: no merge; the middle belt runs on to row L+1 and takes a belt that
    already carries ore and coal on separate lanes (mining_field(mixed=True)) straight from port (5, L+2); ports
    [plate W, plate E, mixed in]. The input inserters pick from both lanes, so which lane holds the ore does not
    matter."""
    G = _geo(gd)
    if n_pairs < 1:
        raise ValueError("a smelter column needs at least one furnace pair")
    L = 2 * n_pairs
    ents = []
    for i in range(n_pairs):
        y = 2 * i
        for side, (xp, xo, xf, xi, din, dout) in (("W", (0, 1, 2, 4, EAST, EAST)), ("E", (10, 9, 7, 6, WEST, WEST))):
            ents.append(_put(G, furnace, xf, y, extra=_r("furnace", unit=i, side=side)))
            ents.append(_put(G, "burner-inserter", xi, y, din, extra=_r("in_b", unit=i, side=side)))
            ents.append(_put(G, "inserter", xi, y, din, extra=_r("in_e", unit=i, side=side)))
            ents.append(_put(G, "burner-inserter", xo, y, dout, extra=_r("out_b", unit=i, side=side)))
            ents.append(_put(G, "inserter", xo, y, dout, extra=_r("out_e", unit=i, side=side)))
            ents.append(_put(G, chest, xp, y, extra=_r("buffer", unit=i, side=side)))
    for r in range(0, L - 1):
        for xp, side in ((0, "W"), (10, "E")):
            ents.append(_put(G, belt, xp, r, NORTH, extra=_r("plate", row=r, side=side)))
    if mixed_in:
        ents += [_put(G, belt, 5, r, NORTH, extra=_r("mid", row=r)) for r in range(0, L + 2)]
    else:
        ents += [_put(G, belt, 5, r, NORTH, extra=_r("mid", row=r)) for r in range(0, L + 1)]
        ents += [_put(G, belt, 4, L, EAST, extra=_r("merge", row=L)),
                 _put(G, belt, 6, L, WEST, extra=_r("merge", row=L)),
                 _put(G, belt, 5, L + 1, NORTH, extra=_r("merge", row=L + 1, note="straightener"))]
    for j in range(math.ceil(n_pairs / 2)):
        # out-inserter columns (x 1, 9) and the free row of the west in-inserter column (x 4): one pole there covers
        # the input inserters of both sides (x 4 and 6) of two pairs
        for xo, side in ((1, "W"), (9, "E"), (4, "C")):
            ents.append(_put(G, pole, xo, 4 * j + 1, extra=_r("col_pole", unit=j, side=side)))
    rec = smelt_recipe(G, ore, furnace)
    ports = [Port("belt_out", rec["product"], 0, -1, NORTH), Port("belt_out", rec["product"], 10, -1, NORTH)]
    if mixed_in:
        ports.append(Port("belt_in", ore, 5, L + 2, NORTH))
    else:
        ports += [Port("belt_in", ore, 4, L + 1, NORTH, "left"), Port("belt_in", "coal", 6, L + 1, NORTH, "right")]
    b = Block("smelter_column", ents, (COLUMN_W, L + 2), ports)
    speed = G.entity(furnace).get("crafting_speed") or 1
    per_furnace = speed / rec["energy"] * rec["out"]
    b.info = {"pairs": n_pairs, "furnaces": 2 * n_pairs, "ore": ore, "product": rec["product"],
              "per_furnace": per_furnace, "merge_row": L}
    b.rates = {rec["product"]: _per_min(per_furnace * 2 * n_pairs), "coal": -fuel_per_min(G, furnace, "coal",
                                                                                           2 * n_pairs)}
    return b


def mirror_y(block: Block) -> Block:
    """Mirror a block top-bottom (north <-> south); east/west facing entities keep their direction. Only for blocks
    whose entities are symmetric under the mirror (belts, inserters, boilers, steam engines, pipes, poles)."""
    H = block.size[1]
    flip = {NORTH: SOUTH, SOUTH: NORTH, EAST: EAST, WEST: WEST}
    ents = [replace(e, y=H - e.y, direction=flip[e.direction]) for e in block.entities]
    ports = [replace(p, y=H - 1 - p.y, direction=flip[p.direction] if p.kind != "pole" else 0) for p in block.ports]
    return replace(block, entities=ents, ports=ports, info=dict(block.info, mirrored=True))


def power_block(n_boilers=10, gd=None, *, boiler="boiler", engine="steam-engine", inserter="burner-inserter",
                belt="transport-belt", pole="small-electric-pole", coal_end="water") -> Block:
    """Steam block reserved at end-state size (PLANNER-v3 6.4), canonical with the water on the west: unit i = a
    boiler (facing north) at x_i, two steam engines in series above it and a burner inserter below it that feeds it
    from the coal belt; units pair around a 1-tile gap column (a pipe joins the pair's water at the boiler row and one
    small pole at the engine joint powers the pair's 4 engines, pitch 7). The coal belt runs EAST from port (-1, yc)
    next to the water input (fed from below, port (0, yc+1)), so unit 0 (west) is built first and later units only
    extend the belt downstream. Burner inserters only: the block black-starts. Ports: pipe_in water at unit 0's
    west connection, belt_in coal."""
    G = _geo(gd)
    ew, eh = G.size(engine)
    bw, bh = G.size(boiler)
    yb, yi = 2 * eh, 2 * eh + bh
    yc = yi + 1
    xs = [(i // 2) * (2 * bw + 1) + (i % 2) * (bw + 1) for i in range(n_boilers)]
    ents = []
    for i, x in enumerate(xs):
        ents.append(_put(G, boiler, x, yb, NORTH, extra=_r("boiler", unit=i)))
        ents.append(_put(G, engine, x, eh, NORTH, extra=_r("engine", unit=i)))
        ents.append(_put(G, engine, x, 0, NORTH, extra=_r("engine", unit=i)))
        ents.append(_put(G, inserter, x + bw // 2, yi, SOUTH, extra=_r("boiler_ins", unit=i)))
        if i % 2 == 1:
            ents.append(_put(G, "pipe", x - 1, yb + bh - 1, extra=_r("pipe", unit=i)))
            ents.append(_put(G, pole, x - 1, eh - 1, extra=_r("power_pole", unit=i - 1)))
    if n_boilers % 2:
        ents.append(_put(G, pole, xs[-1] + bw, eh - 1, extra=_r("power_pole", unit=n_boilers - 1)))
    w = xs[-1] + bw + (n_boilers % 2)
    for x in range(w):
        ents.append(_put(G, belt, x, yc, EAST if coal_end == "water" else WEST, extra=_r("boiler_belt", row=x)))
    first = ents[0]
    wc = [c for c in G.fluid_connections(boiler, first.x, first.y, NORTH) if c[2] == WEST][0]
    # coal_end "water": the coal comes in from below (the land side) at the water end of the belt row (the first
    # belt turns east); "far": it comes in at the far end and the belt runs back toward the water (all of it at S2)
    coal = Port("belt_in", "coal", 0, yc + 1, NORTH) if coal_end == "water" else Port("belt_in", "coal", w, yc, WEST)
    ports = [Port("pipe_in", "water", wc[0], wc[1], EAST), coal]
    b = Block("power_block", ents, (w, yc + 1), ports)
    b.info = {"boilers": n_boilers, "unit_x": xs, "boiler_kw": G.entity(boiler).get("max_energy_usage_kw") or 1800,
              "engine_kw": G.entity(engine).get("max_energy_production_kw") or 900}
    return b


SCI_S = 7     # x of the science belt ("spine") in science_block's local coordinates


def science_block(gd=None, *, n_red_res=8, labs_per_side=5, belt="transport-belt") -> Block:
    """Red science + labs on one science belt, with the green block's ground reserved (PLANNER-v3 6.5-6.7).
    Canonical (north up): the spine (x=7) flows north from the lowest red output inserter to the labs. West of it the
    red column: mixed belt (x=1, copper on the right lane, gears on the left) | input inserters (x=2) | AM1 (x 3-5,
    red unit i at rows 16+3i..) | output inserters (x=6) dropping red packs on the spine's east (right) lane. Below
    it the gear AM1 (rows Yg+1..Yg+3) takes iron from the iron belt end (x=7, two inserters) and puts gears on the
    mixed belt; copper side-loads onto the mixed belt at its start (x=1, row Yg+4) from port (2, Yg+5). Iron port:
    (7, Yg+5). East of the spine: the green block (green output inserters at x=8 drop on the spine's west (left)
    lane; reserved, PLANNER-v3 6.6). Labs: rows 0..3*labs_per_side-1, west (x 3-5) and east (x 9-11) of the spine,
    fed by inserters at x=6 / x=8. info["reserve"] = local rects kept free for the green block."""
    G = _geo(gd)
    S = SCI_S
    nl = labs_per_side
    y0 = 3 * nl + 1                  # first red row (one gap row under the labs)
    Yg = y0 + 3 * n_red_res          # gap row above the gear AM
    ents = []
    for j in range(nl):              # lab j: rows 3j..3j+2; built from the bottom (j = nl-1) up
        for side, xl, xi, d in (("W", S - 4, S - 1, EAST), ("E", S + 2, S + 1, WEST)):
            ents.append(_put(G, "lab", xl, 3 * j, extra=_r("lab", unit=j, side=side)))
            ents.append(_put(G, "inserter", xi, 3 * j + 1, d, extra=_r("lab_ins", unit=j, side=side)))
    for i in range(n_red_res):       # red unit i: rows y0+3i..; built from the bottom (i = n_red_res-1) up
        y = y0 + 3 * i
        ents.append(_put(G, "assembling-machine-1", S - 4, y, recipe="automation-science-pack",
                         extra=_r("red_am", unit=i)))
        ents.append(_put(G, "inserter", S - 5, y + 1, WEST, extra=_r("red_in", unit=i)))
        ents.append(_put(G, "inserter", S - 1, y + 1, WEST, extra=_r("red_out", unit=i)))
    ents.append(_put(G, "assembling-machine-1", S - 4, Yg + 1, recipe="iron-gear-wheel", extra=_r("gear_am")))
    ents.append(_put(G, "inserter", S - 5, Yg + 2, EAST, extra=_r("gear_out")))
    ents += [_put(G, "inserter", S - 1, Yg + r, EAST, extra=_r("gear_in", unit=r)) for r in (1, 2)]
    ents += [_put(G, belt, S, Yg + r, NORTH, extra=_r("fe_end", row=Yg + r)) for r in (1, 2, 3, 4)]
    ents.append(_put(G, belt, S - 5, Yg + 4, WEST, extra=_r("cu_load", row=Yg + 4)))
    ents.append(_put(G, belt, S - 6, Yg + 5, NORTH, extra=_r("blocker", row=Yg + 5)))
    ents += [_put(G, belt, S - 6, r, NORTH, extra=_r("mixed", row=r)) for r in range(y0 + 1, Yg + 5)]
    ents += [_put(G, belt, S, r, NORTH, extra=_r("spine", row=r)) for r in range(1, Yg - 1)]
    ports = [Port("belt_in", "copper-plate", S - 5, Yg + 5, NORTH), Port("belt_in", "iron-plate", S, Yg + 5, NORTH)]
    gents, gports = _green_part(G, S, y0, Yg, belt)
    ents += gents
    ports += gports
    b = Block("science_block", ents, (S + 17, Yg + 6), ports)
    b.info = {"S": S, "y0": y0, "Yg": Yg, "n_red_res": n_red_res, "labs_per_side": nl,
              "reserve": [("green", (S + 1, y0, S + 17, Yg + 6))], "green_rows": GREEN_N}
    return b


GREEN_N = 6          # green-pack assemblers: 6 x (1 pack / 12 s at AM1 speed 0.5) = 0.5 green/s


def _green_part(G, S, y0, Yg, belt="transport-belt"):
    """The green block (PLANNER-v3 6.6) east of the spine, inside the ground the science block reserves for it.
    Canonical columns (x relative to the spine S): S+1 green output inserters (drop on the spine's WEST lane; red
    drops on the east one) | S+2..S+4 six logistic-science-pack AM1 (rows y0+3i) | S+5 their input inserters | S+6
    the MIX belt (north; transport belts side-loaded onto its east lane at the bottom, inserters dropped on its west
    lane: separate lanes, so neither item can block the other) | S+7 producer output inserters | S+8..S+10 the
    producer stack, one AM1 each, direct insertion between neighbours (pitch 4): copper cable (y0+8) -> electronic
    circuit (y0+12) -> inserter (y0+16) <- gear (y0+20) -> transport belt (y0+24) | S+11 producer input inserters
    | S+12 the FEED belt (north; iron side-loaded onto its west lane, copper onto its east lane). Ports at the
    bottom row (Yg+5): iron (S+10), copper (S+13). Rates at 0.5 green/s: 2.75 iron/s, 0.75 copper/s; cable 1.5/s
    over two inserters (an inserter moves ~0.83/s), gears 0.75/s (two iron inserters into the gear AM)."""
    if Yg + 5 < y0 + 29:
        raise ValueError("the green block needs n_red_res >= 8 (30 rows)")
    am = "assembling-machine-1"
    ins = "inserter"
    E = []

    def put(name, x, y, d=NORTH, role="", recipe=None, **kw):
        E.append(_put(G, name, x, y, d, recipe=recipe, extra=_r(role, **kw)))
    for i in range(GREEN_N):
        y = y0 + 3 * i
        put(am, S + 2, y, recipe="logistic-science-pack", role="green_am", unit=i)
        put(ins, S + 1, y + 1, EAST, role="green_out", unit=i)
        put(ins, S + 5, y + 1, EAST, role="green_in", unit=i)
    rows = {"copper-cable": y0 + 8, "electronic-circuit": y0 + 12, "inserter": y0 + 16, "iron-gear-wheel": y0 + 20,
            "transport-belt": y0 + 24}
    for rec, y in rows.items():
        put(am, S + 8, y, recipe=rec, role="prod_am", unit=rec)
    yk, yc, yi, yg, yt = (rows[r] for r in ("copper-cable", "electronic-circuit", "inserter", "iron-gear-wheel",
                                            "transport-belt"))
    # direct insertion (inserter direction = the side it picks from)
    put(ins, S + 8, yk + 3, NORTH, role="prod_link", unit="cable")      # cable -> circuit (x2: 1.5 cable/s)
    put(ins, S + 9, yk + 3, NORTH, role="prod_link", unit="cable")
    put(ins, S + 9, yc + 3, NORTH, role="prod_link", unit="circuit")    # circuit -> inserter
    put(ins, S + 9, yi + 3, SOUTH, role="prod_link", unit="gear_up")    # gear -> inserter (picks from the south)
    put(ins, S + 9, yg + 3, NORTH, role="prod_link", unit="gear_down")  # gear -> belt
    # plates from the feed belt (x S+12) into each producer
    for y in (yk + 1, yc + 1, yi + 1, yg, yg + 1, yt + 1):
        put(ins, S + 11, y, EAST, role="prod_in", row=y)
    # outputs onto the MIX belt: inserters dropped from the east (west lane), belts side-loaded from the east
    put(ins, S + 7, yi + 1, EAST, role="prod_out", unit="inserter")
    put(ins, S + 8, yt + 3, NORTH, role="prod_out", unit="transport-belt")
    put(belt, S + 8, yt + 4, WEST, role="mix_load", row=yt + 4)
    put(belt, S + 7, yt + 4, WEST, role="mix_load", row=yt + 4)
    for r in range(y0, yt + 6):
        put(belt, S + 6, r, NORTH, role="mix" if r < yt + 5 else "mix_block", row=r)
    # FEED: iron from the west, copper from the east, both side-loaded onto the tile above a blocker
    for r in range(yk + 1, yt + 6):
        put(belt, S + 12, r, NORTH, role="feed" if r < yt + 5 else "feed_block", row=r)
    put(belt, S + 10, yt + 4, EAST, role="feed_in", row=yt + 4)         # iron: port (S+10, Yg+5) below it
    put(belt, S + 11, yt + 4, EAST, role="feed_in", row=yt + 4)
    put(belt, S + 13, yt + 4, WEST, role="feed_in", row=yt + 4)         # copper: port (S+13, Yg+5) below it
    ports = [Port("belt_in", "iron-plate", S + 10, Yg + 5, NORTH), Port("belt_in", "copper-plate", S + 13, Yg + 5, NORTH)]
    return E, ports
