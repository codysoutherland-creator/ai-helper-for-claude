"""World model loaded from the mod's export_map files (see SPEC-v3-contracts.md).

Coordinates: map tiles, x east / y south; rects and bboxes are (x0, y0, x1, y1) with x1/y1 exclusive (tile edges).
Directions are 16-way defines (north 0, east 4, south 8, west 12).

    w = World.from_link()                    # export via the mod, wait, load
    w = World.load(script_output() / "claude/map/nauvis/<run_id>")
    w.patches(), w.water_bodies(), w.shoreline(w.nearest_water(0, 0)), w.grid((x0, y0, x1, y1), margin=8)
"""
from __future__ import annotations

import json
import math
import time
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from . import paths
from .grid import BLOCKED, CLEARABLE, Grid

# Fluid resources are sparse wells: grouped with a wider gap and no minimum tile count.
FLUID_RESOURCES = {"crude-oil", "sulfuric-acid-geyser", "fluorine-vent", "lithium-brine"}
FLUID_GAP = 12
CLEARABLE_KINDS = {"tree", "rock"}  # obstacles that deconstruction can remove (cliffs need explosives)

# Offshore pump rule, verified in 2.1.23 with can_place_entity(manual) on a random land/water pattern: the pump's own
# tile is not water, and the 3-wide x 2-deep block in front of it (in its facing direction) is all water.
_DIRS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}

# box = tile bounds (x0, y0, x1, y1); tiles = exact tiles for rotated boxes (corner cliffs), else None (= whole box)
Obstacle = namedtuple("Obstacle", "kind name x y orientation box tiles", defaults=(None,))
MapEntity = namedtuple("MapEntity", "name x y dir extra box")       # name "ghost:<inner>" for ghosts


@dataclass
class Patch:
    name: str
    tiles: set
    amount: int
    bbox: tuple
    center: tuple                 # amount-weighted centroid (map position, float)
    edge: bool = False            # touches the edge of the exported area (may continue beyond it)

    @property
    def size(self) -> int:
        return len(self.tiles)

    def outline(self) -> set:
        """Patch tiles with at least one 4-neighbour outside the patch."""
        t = self.tiles
        return {(x, y) for x, y in t if not ((x + 1, y) in t and (x - 1, y) in t and (x, y + 1) in t and (x, y - 1) in t)}


@dataclass
class WaterBody:
    tiles: set
    bbox: tuple
    center: tuple
    id: int = 0
    edge: bool = False

    @property
    def size(self) -> int:
        return len(self.tiles)


def _rect(r):
    if r is None:
        return None
    if isinstance(r, dict):
        return (int(math.floor(r["x0"])), int(math.floor(r["y0"])), int(math.ceil(r["x1"])), int(math.ceil(r["y1"])))
    x0, y0, x1, y1 = r
    return (int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1)))


def _rows(rows):
    return np.frombuffer("".join(rows).encode(), np.uint8).reshape(32, 32) == ord("1")


def _box(b, x, y):
    if isinstance(b, (list, tuple)) and len(b) == 4:
        return tuple(int(v) for v in b)
    return (int(math.floor(x)), int(math.floor(y)), int(math.floor(x)) + 1, int(math.floor(y)) + 1)


def _obstacle_tiles(o):
    if o.tiles is not None:
        return o.tiles
    b = o.box
    return [(x, y) for y in range(b[1], b[3]) for x in range(b[0], b[2])]


def _intersects(box, rect):
    return box[0] < rect[2] and box[2] > rect[0] and box[1] < rect[3] and box[3] > rect[1]


class World:
    def __init__(self, surface, tick, run_id, x0, y0, w, h):
        self.surface, self.tick, self.run_id = surface, tick, run_id
        self.x0, self.y0, self.w, self.h = x0, y0, w, h
        self.known = np.zeros((h, w), bool)    # exported chunk
        self.water = np.zeros((h, w), bool)
        self.void = np.zeros((h, w), bool)
        self.res = np.zeros((h, w), np.int16)  # index into res_names (0 = none)
        self.amount = np.zeros((h, w), np.int64)
        self.res_names: list = [None]
        self.obstacles: list[Obstacle] = []
        self.entities: list[MapEntity] = []
        self._chunk_obs: dict = {}
        self._chunk_ent: dict = {}
        self._water_cache = None
        self.dir = None
        self.errors = 0

    # --- loading --------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, map_dir) -> "World":
        map_dir = Path(map_dir)
        idx = json.loads((map_dir / "index.json").read_text(encoding="utf-8"))
        chunks = [tuple(c) for c in (idx.get("chunks") or [])]
        if chunks:
            cxs = [c[0] for c in chunks]
            cys = [c[1] for c in chunks]
            x0, y0 = min(cxs) * 32, min(cys) * 32
            w, h = (max(cxs) + 1) * 32 - x0, (max(cys) + 1) * 32 - y0
        else:
            x0 = y0 = w = h = 0
        world = cls(idx.get("surface"), idx.get("tick"), idx.get("run_id"), x0, y0, w, h)
        world.dir = map_dir
        world.errors = idx.get("errors") or 0
        names = {}
        for cx, cy in chunks:
            p = map_dir / f"c_{cx}_{cy}.json"
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                world.errors += 1
                continue
            i, j = cy * 32 - y0, cx * 32 - x0
            world.known[i:i + 32, j:j + 32] = True
            if d.get("water"):
                world.water[i:i + 32, j:j + 32] = _rows(d["water"])
            if d.get("void"):
                world.void[i:i + 32, j:j + 32] = _rows(d["void"])
            for name, x, y, amount in d.get("resources") or ():
                k = names.get(name)
                if k is None:
                    k = names[name] = len(world.res_names)
                    world.res_names.append(name)
                ti, tj = int(math.floor(y)) - y0, int(math.floor(x)) - x0
                if 0 <= ti < h and 0 <= tj < w:
                    world.res[ti, tj] = k
                    world.amount[ti, tj] += amount
            obs = world._chunk_obs.setdefault((cx, cy), [])
            for row in d.get("obstacles") or ():
                kind, name, x, y = row[:4]
                orient = row[4] if len(row) > 4 and row[4] != "" else None
                obs.append(len(world.obstacles))
                tiles = [tuple(t) for t in row[6]] if len(row) > 6 and row[6] else None
                world.obstacles.append(Obstacle(kind, name, x, y, orient, _box(row[5] if len(row) > 5 else None, x, y), tiles))
            ents = world._chunk_ent.setdefault((cx, cy), [])
            for row in d.get("entities") or ():
                name, x, y, direction = row[:4]
                extra = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
                ents.append(len(world.entities))
                world.entities.append(MapEntity(name, x, y, direction, extra, _box(extra.get("b"), x, y)))
        world.water &= ~world.void
        return world

    @classmethod
    def from_link(cls, surface=None, area=None, link=None, include=None, charted=None, timeout=180.0) -> "World":
        """Run export_map in the game (via factorio_claude.link or `link`), wait for it, load the files.
        area: (x0, y0, x1, y1) or {x0,y0,x1,y1}; charted=False exports uncharted generated chunks too (tests)."""
        if link is None:
            from .. import link as _link
            call = _link.call
        else:
            call = link if callable(link) else link.call
        r = _rect(area)
        args = {"surface": surface, "include": include, "charted": charted,
                "area": {"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3]} if r else None}
        res = call("export_map", **{k: v for k, v in args.items() if v is not None})
        deadline = time.monotonic() + timeout
        done = res.get("done")
        while not done:
            if time.monotonic() > deadline:
                raise TimeoutError(f"export_map {res['run_id']} not done after {timeout:.0f}s")
            time.sleep(0.1)
            done = call("export_status", run_id=res["run_id"]).get("done")
        root = paths.script_output().resolve()
        map_dir = (root / str(res["dir"])).resolve()
        if not map_dir.is_relative_to(root):  # the game names the folder; never read outside script-output
            raise OSError(f"export_map returned a folder outside script-output: {res['dir']!r}")
        while not (map_dir / "index.json").exists():  # the game may flush files a moment later
            if time.monotonic() > deadline:
                raise TimeoutError(f"{map_dir / 'index.json'} never appeared (script_output path wrong?)")
            time.sleep(0.05)
        return cls.load(map_dir)

    # --- point queries --------------------------------------------------------------------------------------
    @property
    def bbox(self):
        return (self.x0, self.y0, self.x0 + self.w, self.y0 + self.h)

    def _ij(self, x, y):
        i, j = int(math.floor(y)) - self.y0, int(math.floor(x)) - self.x0
        if 0 <= i < self.h and 0 <= j < self.w:
            return i, j
        return None

    def is_known(self, x, y) -> bool:
        ij = self._ij(x, y)
        return ij is not None and bool(self.known[ij])

    def is_water(self, x, y) -> bool:
        ij = self._ij(x, y)
        return ij is not None and bool(self.water[ij])

    def is_void(self, x, y) -> bool:
        ij = self._ij(x, y)
        return ij is not None and bool(self.void[ij])

    def resource_at(self, x, y):
        ij = self._ij(x, y)
        if ij is None or not self.res[ij]:
            return None
        return (self.res_names[self.res[ij]], int(self.amount[ij]))

    def resource_totals(self) -> dict:
        """{name: (tiles, amount)} over the whole export."""
        return {n: (int((self.res == k).sum()), int(self.amount[self.res == k].sum()))
                for k, n in enumerate(self.res_names) if k}

    # --- patches -------------------------------------------------------------------------------------------
    def patches(self, min_tiles=20, gap=2, name=None) -> list[Patch]:
        """Resource patches: same-name tiles, 8-connected allowing `gap` empty tiles between them (fluid wells
        use FLUID_GAP and no minimum). Sorted by amount, biggest first."""
        out = []
        for k, rname in enumerate(self.res_names):
            if not k or (name and rname != name):
                continue
            fluid = rname in FLUID_RESOURCES
            g = FLUID_GAP if fluid else gap
            mask = self.res == k
            # (g+1)-square dilations of two tiles are 8-adjacent iff their Chebyshev distance is <= g+1
            grown = ndimage.binary_dilation(mask, np.ones((g + 1, g + 1), bool)) if g else mask
            labels, n = ndimage.label(grown, np.ones((3, 3), int))
            labels = np.where(mask, labels, 0)
            ii, jj = np.nonzero(labels)
            lab = labels[ii, jj]
            order = np.argsort(lab, kind="stable")
            ii, jj, lab = ii[order], jj[order], lab[order]
            starts = np.flatnonzero(np.r_[True, lab[1:] != lab[:-1]])
            ends = np.r_[starts[1:], len(lab)]
            for s, e in zip(starts, ends):
                pi, pj = ii[s:e], jj[s:e]
                if not fluid and len(pi) < min_tiles:
                    continue
                amt = self.amount[pi, pj]
                tot = int(amt.sum())
                wts = amt if tot > 0 else np.ones(len(pi))
                xs, ys = pj + self.x0, pi + self.y0
                center = (float(np.average(xs + 0.5, weights=wts)), float(np.average(ys + 0.5, weights=wts)))
                bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                out.append(Patch(rname, set(zip(xs.tolist(), ys.tolist())), tot, bbox, center,
                                 self._touches_edge(pi, pj)))
        out.sort(key=lambda p: -p.amount)
        return out

    def _touches_edge(self, ii, jj) -> bool:
        """Any tile next to the export border or an unexported chunk."""
        if ii.min() == 0 or jj.min() == 0 or ii.max() == self.h - 1 or jj.max() == self.w - 1:
            return True
        unknown = ~self.known
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if unknown[ii + di, jj + dj].any():
                    return True
        return False

    # --- water ----------------------------------------------------------------------------------------------
    def _water_labels(self):
        if self._water_cache is None:
            labels, n = ndimage.label(self.water, np.ones((3, 3), int))
            self._water_cache = (labels, n, {})
        return self._water_cache

    def water_bodies(self, min_tiles=30) -> list[WaterBody]:
        """Connected (8-way) water areas with >= min_tiles tiles, biggest first."""
        labels, n, cache = self._water_labels()
        if not n:
            return []
        sizes = np.bincount(labels.ravel(), minlength=n + 1)
        out = []
        for lab in np.flatnonzero(sizes >= min_tiles):
            if lab == 0:
                continue
            body = cache.get(lab)
            if body is None:
                ii, jj = np.nonzero(labels == lab)
                xs, ys = jj + self.x0, ii + self.y0
                body = cache[lab] = WaterBody(
                    set(zip(xs.tolist(), ys.tolist())),
                    (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1),
                    (float(xs.mean() + 0.5), float(ys.mean() + 0.5)), int(lab), self._touches_edge(ii, jj))
            out.append(body)
        out.sort(key=lambda b: -b.size)
        return out

    def nearest_water_tile(self, x, y, min_tiles=30):
        """((tx, ty), WaterBody) of the closest water tile in a body of >= min_tiles, or None."""
        bodies = {b.id: b for b in self.water_bodies(min_tiles)}
        if not bodies:
            return None
        labels = self._water_labels()[0]
        ii, jj = np.nonzero(np.isin(labels, list(bodies)))
        tree = cKDTree(np.c_[jj + self.x0 + 0.5, ii + self.y0 + 0.5])
        _, k = tree.query((x, y))
        tx, ty = int(jj[k] + self.x0), int(ii[k] + self.y0)
        return (tx, ty), bodies[int(labels[ii[k], jj[k]])]

    def nearest_water(self, x, y, min_tiles=30) -> WaterBody | None:
        r = self.nearest_water_tile(x, y, min_tiles)
        return r[1] if r else None

    def shoreline(self, body: WaterBody, terrain_only=False) -> list[tuple[int, int, int]]:
        """Offshore pump spots for this body: [(x, y, direction)] where (x, y) is the land tile the pump stands on
        (entity position x+0.5, y+0.5) and direction (= the pump's direction) points at the water.
        Unless terrain_only, tiles under cliffs/entities are skipped (trees/rocks are fine: clearable)."""
        labels = self._water_labels()[0]
        wb = labels == body.id
        land = self.known & ~self.water & ~self.void
        if not terrain_only:
            land &= ~self._hard_blocked(self.bbox)
        H, W = wb.shape
        pad = np.zeros((H + 6, W + 6), bool)
        pad[3:-3, 3:-3] = wb
        out = []
        for d, (fx, fy) in _DIRS.items():
            px, py = -fy, fx
            ok = land.copy()
            for jdist in (1, 2):
                for k in (-1, 0, 1):
                    dx, dy = jdist * fx + k * px, jdist * fy + k * py
                    ok &= pad[3 + dy:3 + dy + H, 3 + dx:3 + dx + W]
            ii, jj = np.nonzero(ok)
            out.extend((int(j + self.x0), int(i + self.y0), d) for i, j in zip(ii, jj))
        out.sort(key=lambda t: (t[1], t[0], t[2]))
        return out

    def _hard_blocked(self, rect) -> np.ndarray:
        """Mask over the world arrays of tiles under cliffs/enemies/other neutral blockers and force entities."""
        m = np.zeros((self.h, self.w), bool)
        for o in self.obstacles_in(rect):
            if o.kind not in CLEARABLE_KINDS:
                for tx, ty in _obstacle_tiles(o):
                    if 0 <= ty - self.y0 < self.h and 0 <= tx - self.x0 < self.w:
                        m[ty - self.y0, tx - self.x0] = True
        for b in (e.box for e in self.entities_in(rect)):
            i0, i1 = max(0, b[1] - self.y0), min(self.h, b[3] - self.y0)
            j0, j1 = max(0, b[0] - self.x0), min(self.w, b[2] - self.x0)
            if i0 < i1 and j0 < j1:
                m[i0:i1, j0:j1] = True
        return m

    # --- spatial queries -------------------------------------------------------------------------------------
    def _chunks_for(self, rect, pad=2):
        x0, y0, x1, y1 = rect
        for cy in range((y0 - pad * 32) // 32, (y1 - 1) // 32 + 1 + pad):
            for cx in range((x0 - pad * 32) // 32, (x1 - 1) // 32 + 1 + pad):
                yield cx, cy

    def obstacles_in(self, rect) -> list[Obstacle]:
        """Obstacles whose tile box intersects rect."""
        rect = _rect(rect)
        out = []
        for c in self._chunks_for(rect, pad=1):
            for k in self._chunk_obs.get(c, ()):
                if _intersects(self.obstacles[k].box, rect):
                    out.append(self.obstacles[k])
        return out

    def entities_in(self, rect) -> list[MapEntity]:
        """Force entities (incl. ghosts) whose tile box intersects rect."""
        rect = _rect(rect)
        out = []
        for c in self._chunks_for(rect, pad=1):
            for k in self._chunk_ent.get(c, ()):
                if _intersects(self.entities[k].box, rect):
                    out.append(self.entities[k])
        return out

    # --- grid ----------------------------------------------------------------------------------------------
    def grid(self, rect=None, margin=0) -> Grid:
        """Occupancy grid over rect (+margin): water/void/unknown, cliffs, enemies and existing entities are BLOCKED,
        trees/rocks CLEARABLE, ore in the resource layer."""
        x0, y0, x1, y1 = _rect(rect) if rect is not None else self.bbox
        x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
        g = Grid(x0, y0, x1 - x0, y1 - y0)
        # copy the overlapping part of the world arrays; outside the export stays void/BLOCKED
        g.void[:] = True
        wi0, wi1 = max(y0, self.y0), min(y1, self.y0 + self.h)
        wj0, wj1 = max(x0, self.x0), min(x1, self.x0 + self.w)
        if wi0 < wi1 and wj0 < wj1:
            src = np.s_[wi0 - self.y0:wi1 - self.y0, wj0 - self.x0:wj1 - self.x0]
            dst = np.s_[wi0 - y0:wi1 - y0, wj0 - x0:wj1 - x0]
            g.void[dst] = ~self.known[src] | self.void[src]
            g.water[dst] = self.water[src]
            res = self.res[src]
            for k in np.unique(res):
                if k:
                    g.res[dst][res == k] = g._res_id(self.res_names[k])
        g.terrain[g.void | g.water] = BLOCKED
        rect = (x0, y0, x1, y1)
        for o in self.obstacles_in(rect):
            clear = o.kind in CLEARABLE_KINDS
            for tx, ty in _obstacle_tiles(o):
                if x0 <= tx < x1 and y0 <= ty < y1:
                    i, j = ty - y0, tx - x0
                    if clear:
                        if g.terrain[i, j] < CLEARABLE:
                            g.terrain[i, j] = CLEARABLE
                        g.clearables.setdefault((tx, ty), []).append((o.name, o.x, o.y))
                    else:
                        g.terrain[i, j] = BLOCKED
        g.state[:] = g.terrain
        for e in self.entities_in(rect):
            b = e.box
            tiles = [(tx, ty) for ty in range(max(b[1], y0), min(b[3], y1)) for tx in range(max(b[0], x0), min(b[2], x1))]
            tag = "existing:" + e.name
            if e.dir is not None:  # direction (+ underground half) so routers see where belts/inserters point
                ug = (e.extra or {}).get("ug") if isinstance(e.extra, dict) else None
                tag += f"@{int(e.dir) % 16}" + {"input": "in", "output": "out"}.get(ug, "")
            g.occupy(tiles, tag)
            for tx, ty in tiles:  # existing entities are part of the terrain (release() must not free them)
                g.terrain[ty - y0, tx - x0] = BLOCKED
        return g

    def summary(self) -> dict:
        """Compact overview (for Claude / logs)."""
        return {"surface": self.surface, "tick": self.tick, "bbox": self.bbox,
                "known_tiles": int(self.known.sum()), "water_tiles": int(self.water.sum()),
                "resources": {n: {"tiles": t, "amount": a} for n, (t, a) in self.resource_totals().items()},
                "obstacles": len(self.obstacles), "entities": len(self.entities)}
