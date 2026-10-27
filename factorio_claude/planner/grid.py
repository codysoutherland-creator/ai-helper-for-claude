"""Tile occupancy grid used by the router, placer and validator.

Coordinates are map tiles (x east, y south); tile (x, y) covers [x, x+1) x [y, y+1). Rects are (x0, y0, x1, y1)
with x1/y1 exclusive. Per tile:
  state     FREE / CLEARABLE (tree or rock: may be deconstructed) / BLOCKED (water, void, unknown, cliff,
            existing entity, or occupied by the plan via occupy())
  resource  ore under the tile (independent of state)
  tag       who occupied the tile ("existing:<entity name>" for pre-existing entities, or the occupy() tag)
"""
from __future__ import annotations

import numpy as np

FREE, CLEARABLE, BLOCKED = 0, 1, 2

_RES_CHARS = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s", "uranium-ore": "u", "crude-oil": "o"}


class Grid:
    def __init__(self, x0: int, y0: int, w: int, h: int):
        self.x0, self.y0, self.w, self.h = int(x0), int(y0), int(w), int(h)
        self.terrain = np.zeros((self.h, self.w), np.uint8)  # state before any occupy()
        self.state = np.zeros((self.h, self.w), np.uint8)
        self.water = np.zeros((self.h, self.w), bool)
        self.void = np.zeros((self.h, self.w), bool)       # out-of-map, void or not exported
        self.res = np.zeros((self.h, self.w), np.int16)    # 0 = none, else index into res_names
        self.res_names: list[str | None] = [None]
        self.tag = np.zeros((self.h, self.w), np.int32)     # 0 = none, else index into tags
        self.tags: list[str | None] = [None]
        self._tag_ids: dict[str, int] = {}
        self.clearables: dict[tuple[int, int], list[tuple[str, float, float]]] = {}  # tile -> [(name, x, y)]

    # --- basics -------------------------------------------------------------------------------------------
    @property
    def rect(self):
        return (self.x0, self.y0, self.x0 + self.w, self.y0 + self.h)

    def in_bounds(self, x, y) -> bool:
        return self.x0 <= x < self.x0 + self.w and self.y0 <= y < self.y0 + self.h

    def _ij(self, x, y):
        return int(y) - self.y0, int(x) - self.x0

    def copy(self) -> "Grid":
        g = Grid.__new__(Grid)
        g.__dict__.update(self.__dict__)
        for k in ("terrain", "state", "water", "void", "res", "tag"):
            setattr(g, k, getattr(self, k).copy())
        g.res_names, g.tags, g._tag_ids = list(self.res_names), list(self.tags), dict(self._tag_ids)
        g.clearables = dict(self.clearables)
        return g

    def state_at(self, x, y) -> int:
        if not self.in_bounds(x, y):
            return BLOCKED
        i, j = self._ij(x, y)
        return int(self.state[i, j])

    def free(self, x, y) -> bool:
        """True if the tile is FREE (in bounds, nothing on it, nothing to clear)."""
        return self.state_at(x, y) == FREE

    def is_water(self, x, y) -> bool:
        return self.in_bounds(x, y) and bool(self.water[self._ij(x, y)])

    def resource(self, x, y) -> str | None:
        if not self.in_bounds(x, y):
            return None
        return self.res_names[self.res[self._ij(x, y)]]

    def tag_at(self, x, y) -> str | None:
        if not self.in_bounds(x, y):
            return None
        return self.tags[self.tag[self._ij(x, y)]]

    def _tag_id(self, tag) -> int:
        if tag is None:
            return 0
        tag = str(tag)
        tid = self._tag_ids.get(tag)
        if tid is None:
            tid = self._tag_ids[tag] = len(self.tags)
            self.tags.append(tag)
        return tid

    def _res_id(self, name) -> int:
        if name in self.res_names:
            return self.res_names.index(name)
        self.res_names.append(name)
        return len(self.res_names) - 1

    # --- occupation --------------------------------------------------------------------------------------
    def occupy(self, tiles, tag=None):
        """Mark tiles BLOCKED by the plan (tag = who). Tiles outside the grid are ignored."""
        tid = self._tag_id(tag)
        for x, y in tiles:
            if self.in_bounds(x, y):
                i, j = self._ij(x, y)
                self.state[i, j] = BLOCKED
                self.tag[i, j] = tid

    def release(self, tiles):
        """Undo occupy(): tiles go back to their terrain state."""
        for x, y in tiles:
            if self.in_bounds(x, y):
                i, j = self._ij(x, y)
                self.state[i, j] = self.terrain[i, j]
                self.tag[i, j] = 0

    def can_place(self, tiles, allow_clear=True) -> bool:
        """All tiles in bounds and FREE (or CLEARABLE when allow_clear)."""
        lim = CLEARABLE if allow_clear else FREE
        for x, y in tiles:
            if self.state_at(x, y) > lim:
                return False
        return True

    def to_clear(self, tiles) -> list[tuple[float, float, str]]:
        """Obstacles (trees/rocks) that must be deconstructed to use these tiles: [(x, y, name)] (entity positions)."""
        out, seen = [], set()
        for t in tiles:
            for name, ex, ey in self.clearables.get((int(t[0]), int(t[1])), ()):
                if (ex, ey, name) not in seen:
                    seen.add((ex, ey, name))
                    out.append((ex, ey, name))
        return out

    # --- search -------------------------------------------------------------------------------------------
    def placeable_mask(self, allow_clear=True, avoid_resources=False) -> np.ndarray:
        ok = self.state <= (CLEARABLE if allow_clear else FREE)
        if avoid_resources:
            ok &= self.res == 0
        return ok

    def free_rect(self, w, h, near=None, within=None, allow_clear=True, avoid_resources=False):
        """Top-left (x, y) of a w x h rect of placeable tiles whose center is closest to `near` (default: grid
        center), fully inside `within` (rect, default whole grid). None if nothing fits."""
        ok = self.placeable_mask(allow_clear, avoid_resources)
        if within is not None:
            wx0, wy0, wx1, wy1 = within
            m = np.zeros_like(ok)
            i0, i1 = max(0, wy0 - self.y0), min(self.h, wy1 - self.y0)
            j0, j1 = max(0, wx0 - self.x0), min(self.w, wx1 - self.x0)
            if i0 < i1 and j0 < j1:
                m[i0:i1, j0:j1] = True
            ok &= m
        if w > self.w or h > self.h:
            return None
        s = np.zeros((self.h + 1, self.w + 1), np.int32)
        s[1:, 1:] = ok.astype(np.int32).cumsum(0).cumsum(1)
        win = s[h:, w:] - s[:-h, w:] - s[h:, :-w] + s[:-h, :-w]  # window sums, index = top-left
        ii, jj = np.nonzero(win == w * h)
        if len(ii) == 0:
            return None
        if near is None:
            near = (self.x0 + self.w / 2, self.y0 + self.h / 2)
        cx = self.x0 + jj + w / 2
        cy = self.y0 + ii + h / 2
        k = int(np.argmin((cx - near[0]) ** 2 + (cy - near[1]) ** 2))
        return (int(self.x0 + jj[k]), int(self.y0 + ii[k]))

    # --- debug --------------------------------------------------------------------------------------------
    def to_ascii(self, rect=None, tag_chars=None) -> str:
        """'.' free, 't' clearable, '#' blocked, '~' water, ' ' void/unknown, 'E' existing entity, '@' plan
        (or tag_chars[tag]), resource letter (i c k s u o, else '*') on free ore tiles."""
        x0, y0, x1, y1 = rect or self.rect
        rows = []
        for y in range(y0, y1):
            r = []
            for x in range(x0, x1):
                if not self.in_bounds(x, y):
                    r.append(" ")
                    continue
                i, j = self._ij(x, y)
                tag = self.tags[self.tag[i, j]]
                if self.void[i, j]:
                    c = " "
                elif self.water[i, j]:
                    c = "~"
                elif tag and tag_chars and tag in tag_chars:
                    c = tag_chars[tag]
                elif tag and tag.startswith("existing:"):
                    c = "E"
                elif tag:
                    c = "@"
                elif self.state[i, j] == BLOCKED:
                    c = "#"
                elif self.state[i, j] == CLEARABLE:
                    c = "t"
                elif self.res[i, j]:
                    c = _RES_CHARS.get(self.res_names[self.res[i, j]], "*")
                else:
                    c = "."
                r.append(c)
            rows.append("".join(r))
        return "\n".join(rows)
