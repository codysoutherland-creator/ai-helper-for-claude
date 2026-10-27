"""Game data (prototypes + force state) exported by the mod's `export_data` op, with geometry helpers.

    gd = GameData.from_link()                 # asks the running game to export, then loads claude/data.json
    gd = GameData.load(path_to_data_json)
    gd.footprint("boiler", 0.5, 1, 4)         # tiles covered by a boiler facing east centered at (0.5, 1)

Units: energy in kW, fluid rates per second, belt/inserter throughput in items/s.
Directions are 16-way defines (north 0, east 4, south 8, west 12); positions are entity centers.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12
DIR_VEC = {NORTH: (0, -1), EAST: (1, 0), SOUTH: (0, 1), WEST: (-1, 0)}
EPS = 1e-6
# Entities whose geometry isn't an axis-aligned box on the tile grid (rails, vehicles, robots).
UNSUPPORTED_GEOMETRY = {"curved-rail-a", "curved-rail-b", "half-diagonal-rail", "legacy-curved-rail",
                        "elevated-curved-rail-a", "elevated-curved-rail-b", "elevated-half-diagonal-rail",
                        "rail-support", "car", "spider-vehicle", "locomotive", "cargo-wagon", "fluid-wagon",
                        "artillery-wagon", "infinity-cargo-wagon", "construction-robot", "logistic-robot"}
DATA_FILE = "claude/data.json"


def _dict(v) -> dict:
    """Factorio serializes empty tables as [] or {}; normalize to dict."""
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _cardinal(direction: int) -> int:
    d = int(direction or 0) % 16
    if d % 4:
        raise ValueError(f"direction {direction} is not cardinal")
    return d


def rotate(x: float, y: float, direction: int) -> tuple[float, float]:
    """Rotate a vector given for a north-facing entity to `direction` (clockwise, y south)."""
    for _ in range(_cardinal(direction) // 4):
        x, y = -y, x
    return x, y


def tiles_of_box(x0: float, y0: float, x1: float, y1: float) -> set[tuple[int, int]]:
    """Tiles whose interior overlaps the box (absolute map coordinates)."""
    return {(tx, ty)
            for tx in range(math.floor(x0 + EPS), math.ceil(x1 - EPS))
            for ty in range(math.floor(y0 + EPS), math.ceil(y1 - EPS))}


def script_output() -> Path:
    """One resolver for every OS: see paths.script_output."""
    from .paths import script_output as so
    return so()


@dataclass
class GameData:
    raw: dict
    path: Path | None = None
    _producers: dict = field(default_factory=dict, repr=False)

    # ---- loading -------------------------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "GameData":
        path = Path(path)
        if path.suffix == ".gz":
            import gzip
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return cls(json.load(f), path)
        return cls(json.loads(path.read_text(encoding="utf-8")), path)

    @classmethod
    def from_link(cls, link=None, force: str | None = None) -> "GameData":
        """Ask the game to export (op export_data) and load the result. `link` = module/object with call(op, **args)."""
        if link is None:
            from .. import link as link
        res = link.call("export_data", force=force)
        return cls.load(script_output() / res.get("path", DATA_FILE))

    def __post_init__(self):
        for k in ("entities", "resources", "recipes", "items", "fluids", "qualities", "technologies",
                  "bonuses", "surfaces", "research", "utility"):
            self.raw[k] = _dict(self.raw.get(k))
        for r in self.raw["recipes"].values():
            r["ingredients"] = _list(r.get("ingredients"))
            r["products"] = _list(r.get("products"))
            r["categories"] = _list(r.get("categories"))

    @property
    def version_key(self) -> str:
        return self.raw.get("version_key", "")

    @property
    def bonuses(self) -> dict:
        return self.raw["bonuses"]

    # ---- lookups -------------------------------------------------------------------------------
    def _get(self, kind: str, name: str) -> dict:
        try:
            return self.raw[kind][name]
        except KeyError:
            raise KeyError(f"unknown {kind[:-1]} {name!r}") from None

    def entity(self, name: str) -> dict: return self._get("entities", name)
    def recipe(self, name: str) -> dict: return self._get("recipes", name)
    def item(self, name: str) -> dict: return self._get("items", name)
    def fluid(self, name: str) -> dict: return self._get("fluids", name)
    def resource(self, name: str) -> dict: return self._get("resources", name)
    def technology(self, name: str) -> dict: return self._get("technologies", name)

    def quality(self, name: str = "normal") -> dict:
        return self._get("qualities", name)

    def is_fluid(self, name: str) -> bool:
        return name.split("@")[0] in self.raw["fluids"]

    def qualities_unlocked(self) -> list[str]:
        """Unlocked qualities in level order, following the `next` chain."""
        out, q = [], "normal"
        while q and q in self.raw["qualities"]:
            if self.raw["qualities"][q].get("unlocked") or q == "normal":
                out.append(q)
            q = self.raw["qualities"][q].get("next")
        return out

    def enabled(self, recipe_name: str) -> bool:
        r = self.raw["recipes"].get(recipe_name)
        return bool(r and r.get("enabled"))

    def producers(self, item: str) -> list[str]:
        """Recipe names that list `item` among their products."""
        if not self._producers:
            for rn, r in self.raw["recipes"].items():
                for p in r["products"]:
                    self._producers.setdefault(p["name"], []).append(rn)
        return self._producers.get(item, [])

    def can_build(self, entity_name: str) -> bool:
        """True if an item that places this entity has an enabled recipe."""
        e = self.raw["entities"].get(entity_name)
        if not e:
            return False
        for item, _count in _list(e.get("items_to_place_this")):
            if any(self.enabled(r) for r in self.producers(item)):
                return True
        return False

    def machines_for(self, category: str, buildable_only: bool = False) -> list[str]:
        """Crafting machines supporting a recipe category, slowest first (character excluded)."""
        out = [n for n, e in self.raw["entities"].items()
               if e.get("type") != "character" and category in _list(e.get("crafting_categories"))
               and (not buildable_only or self.can_build(n))]
        return sorted(out, key=lambda n: (self.raw["entities"][n].get("crafting_speed") or 0, n))

    def drills_for(self, resource_category: str, buildable_only: bool = False) -> list[str]:
        out = [n for n, e in self.raw["entities"].items()
               if e.get("type") == "mining-drill" and resource_category in _list(e.get("resource_categories"))
               and (not buildable_only or self.can_build(n))]
        return sorted(out, key=lambda n: (self.raw["entities"][n].get("mining_speed") or 0, n))

    def resource_for_item(self, item: str) -> str | None:
        """Resource entity whose mining yields `item` (e.g. iron-ore, crude-oil)."""
        for rn, r in self.raw["resources"].items():
            if any(p.get("name") == item for p in _list(r.get("products"))):
                return rn
        return None

    # ---- geometry ------------------------------------------------------------------------------
    def effective_direction(self, name: str, direction: int = NORTH) -> int:
        """Direction that matters for geometry: not-rotatable entities always face north."""
        d = _cardinal(direction)
        return NORTH if "not-rotatable" in _list(self.entity(name).get("flags")) else d

    def size(self, name: str, direction: int = NORTH) -> tuple[int, int]:
        e = self.entity(name)
        w, h = int(e.get("tile_width") or 1), int(e.get("tile_height") or 1)
        return (h, w) if self.effective_direction(name, direction) in (EAST, WEST) else (w, h)

    def snap(self, name: str, x: float, y: float, direction: int = NORTH) -> tuple[float, float]:
        """Center position the engine would use for a build at (x, y): odd sizes on .5, even on integers,
        off-grid entities floored to 1/256."""
        e = self.entity(name)
        if "placeable-off-grid" in _list(e.get("flags")):
            return math.floor(x * 256) / 256, math.floor(y * 256) / 256
        w, h = self.size(name, direction)

        def s(v, n):
            return math.floor(v) + 0.5 if n % 2 else float(math.floor(v + 0.5))
        return s(x, w), s(y, h)

    def box(self, name: str, x: float, y: float, direction: int = NORTH) -> tuple[float, float, float, float]:
        """Rotated collision box in map coordinates: (x0, y0, x1, y1). Rails/vehicles raise ValueError."""
        e = self.entity(name)
        if e.get("type") in UNSUPPORTED_GEOMETRY:
            raise ValueError(f"{name}: {e.get('type')} geometry is not supported")
        direction = self.effective_direction(name, direction)
        (lx, ly), (rx, ry) = e["collision_box"]
        pts = [rotate(px, py, direction) for px, py in ((lx, ly), (rx, ry))]
        return (x + min(p[0] for p in pts), y + min(p[1] for p in pts),
                x + max(p[0] for p in pts), y + max(p[1] for p in pts))

    def footprint(self, name: str, x: float, y: float, direction: int = NORTH) -> set[tuple[int, int]]:
        """Tiles the entity's collision box overlaps when centered at (x, y) facing `direction`."""
        return tiles_of_box(*self.box(name, x, y, direction))

    def fluid_connections_full(self, name: str, x: float, y: float, direction: int = NORTH) -> list[dict]:
        """Every pipe connection: {fluidbox, production_type, filter, type, flow, dir, pos, target, tile,
        target_tile, max_underground_distance, conditional}. pos = connection point inside the entity,
        target = point just outside it (pos + dir); dir = outward direction. Underground connections are
        included. conditional = only active with a fluid recipe / fluid-requiring resource (drills)."""
        d = self.effective_direction(name, direction)
        k = d // 4
        out = []
        for fb in _list(self.entity(name).get("fluidboxes")):
            for c in _list(fb.get("connections")):
                px, py = c["positions"][k]
                cd = (c["direction"] + d) % 16
                vx, vy = DIR_VEC[cd]
                pos = (x + px, y + py)
                tgt = (pos[0] + vx, pos[1] + vy)
                out.append({"fluidbox": fb.get("index"), "production_type": fb.get("production_type"),
                            "filter": fb.get("filter"), "type": c.get("type"), "flow": c.get("flow"),
                            "dir": cd, "pos": pos, "target": tgt,
                            "tile": (math.floor(pos[0]), math.floor(pos[1])),
                            "target_tile": (math.floor(tgt[0]), math.floor(tgt[1])),
                            "max_underground_distance": c.get("max_underground_distance"),
                            "conditional": bool(fb.get("conditional"))})
        return out

    def fluid_connections(self, name: str, x: float, y: float, direction: int = NORTH) -> list[tuple[int, int, int]]:
        """Normal (non-underground) pipe connections as (tx, ty, dir): the tile OUTSIDE the entity where a
        connecting pipe must sit, and the outward direction (that pipe must connect back in dir+8)."""
        return [(*c["target_tile"], c["dir"]) for c in self.fluid_connections_full(name, x, y, direction)
                if c["type"] == "normal"]

    # ---- throughput / stats --------------------------------------------------------------------
    def belt_throughput(self, name: str, stack: int = 1) -> float:
        """Items/s on both lanes (stack > 1 for stacked belts)."""
        e = self.entity(name)
        if not e.get("belt_items_per_s"):
            raise ValueError(f"{name} is not a belt")
        return e["belt_items_per_s"] * stack

    def inserter_throughput(self, name: str, stack: int | None = None, quality: str = "normal") -> float:
        """Approximate chest-to-chest items/s: one item stack per full swing (2 half turns, whole ticks)."""
        ins = self.entity(name).get("inserter")
        if not ins:
            raise ValueError(f"{name} is not an inserter")
        if stack is None:
            b = self.bonuses
            bonus = (b.get("bulk_inserter_capacity_bonus") if ins.get("bulk") else b.get("inserter_stack_size_bonus")) or 0
            stack = 1 + (ins.get("stack_size_bonus") or 0) + bonus
        rot = ins["rotation_speed"] * (self.quality(quality).get("inserter_speed_multiplier") or 1)
        half_turn_ticks = math.ceil(0.5 / rot - EPS)
        return 60.0 * stack / (2 * half_turn_ticks)

    def pole(self, name: str) -> tuple[float, float]:
        e = self.entity(name)
        return e.get("supply_area_distance") or 0.0, e.get("max_wire_distance") or 0.0

    def quality_roll(self, start: str, quality_effect: float) -> dict[str, float]:
        """Product quality distribution for a craft at `start` quality with total quality effect (0.01 = 1%).
        Mirrors LuaQualityPrototype.get_roll_chances: jump with effect*next_probability, then keep
        climbing with each reached quality's chain_probability; capped at the highest unlocked quality."""
        qs = self.raw["qualities"]
        unlocked = set(self.qualities_unlocked())
        out = {start: 1.0}
        q = max(0.0, quality_effect)
        nxt = qs[start].get("next")
        if q <= 0 or not nxt or nxt not in unlocked:
            return out
        p_up = min(1.0, q * (qs[start].get("next_probability") or 1))
        out[start] = 1 - p_up
        cur, mass = nxt, p_up
        while mass > 0:
            nn = qs[cur].get("next")
            if not nn or nn not in unlocked:
                out[cur] = out.get(cur, 0) + mass
                break
            cont = mass * (qs[cur].get("chain_probability") or 0)
            out[cur] = out.get(cur, 0) + mass - cont
            cur, mass = nn, cont
        return {k: v for k, v in out.items() if v > 0}

    def fuel_value(self, item: str) -> float:
        """Joules per item (0 if not a fuel)."""
        return float(self.item(item).get("fuel_value") or 0)
