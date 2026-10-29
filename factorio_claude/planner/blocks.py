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
