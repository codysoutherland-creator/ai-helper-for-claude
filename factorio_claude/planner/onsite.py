"""On-site placement for planner v2 (PLANNER-v2.md sections 5 and 7): rows of burner pairs or drill-to-chest
blocks placed directly on an ore patch, patch choice and patch-to-patch distances. No ore belts.

    rows = place_rows(grid, gd, lambda n: B.burner_pair_row(n, gd, ore="iron-ore"), patch, total=4, target=(x, y))
"""
from __future__ import annotations

import math

import numpy as np
from scipy.spatial import cKDTree

from .gamedata import tiles_of_box
from .placer import _Sum, _amounts, _drill_rects, _occupy, _tiles

MIN_DRILL_ORE = 1500      # ore under a burner drill's 2x2 area: 15/min lasts > 1.5 h (fresh start tiles hold ~500)
ORE_PENALTY = 0.5         # per non-drill footprint tile on ore (keep the patch for mining)
CLEAR_PENALTY = 0.3       # per tree/rock to clear


def outline_tree(patch):
    pts = np.array(sorted(patch.outline() or patch.tiles), float) + 0.5
    return pts, cKDTree(pts)


def patch_gap(a, b) -> tuple:
    """(tile of a, tile of b, euclidean distance) of the closest pair of outline tiles."""
    pa, _ta = outline_tree(a)
    pb, tb = outline_tree(b)
    d, k = tb.query(pa)
    i = int(np.argmin(d))
    return (tuple(np.floor(pa[i]).astype(int)), tuple(np.floor(pb[k[i]]).astype(int)), float(d[i]))


def dist_point_patch(p, patch) -> tuple:
    """(nearest tile, euclidean distance) from point p to the patch outline."""
    pts, tree = outline_tree(patch)
    d, k = tree.query(p)
    return tuple(np.floor(pts[k]).astype(int)), float(d)


def pick_patch(world, resource, near, min_amount, anchor=None, anchor_weight=0.0):
    """The patch of `resource` minimising distance to `near` (+ anchor_weight x distance to `anchor` patch),
    among patches holding >= min_amount ore (else the biggest)."""
    cands = world.patches(min_tiles=12, name=resource)
    if not cands:
        return None
    big = [p for p in cands if p.amount >= min_amount] or cands[:1]

    def key(p):
        k = dist_point_patch(near, p)[1] if near is not None else 0.0
        if anchor is not None and anchor_weight:
            k += anchor_weight * patch_gap(p, anchor)[2]
        return k
    return min(big, key=key)


def place_block(grid, gd, block, patch, target, amount=None, min_amount=MIN_DRILL_ORE, allow_ore_footprint=True):
    """Best placement of canonical `block` (any rotation) with every drill on `patch` ore (>= 3 of its 4 area tiles,
    no other resource, >= min_amount ore under it), every tile placeable; nearest `target`. -> placed Block | None."""
    res = patch.name
    ok = _Sum(grid, grid.placeable_mask(allow_clear=True))
    rid = grid.res_names.index(res) if res in grid.res_names else -1
    if rid < 0:
        return None
    mine = _Sum(grid, grid.res == rid)
    anyore = _Sum(grid, grid.res != 0)
    other = _Sum(grid, (grid.res != 0) & (grid.res != rid))
    clear = _Sum(grid, grid.state == 1)
    rich = _Sum(grid, np.where(grid.res == rid, amount, 0)) if amount is not None else None
    bx0, by0, bx1, by1 = patch.bbox
    best = None
    for k in range(4):
        b = block.rotated(k)
        w, h = b.size
        X, Y = np.meshgrid(np.arange(bx0 - w, bx1 + 1), np.arange(by0 - h, by1 + 1))
        X, Y = X.ravel(), Y.ravel()
        good = np.ones(len(X), bool)
        pen = np.zeros(len(X))
        for (fr, mr), e in zip(_drill_rects(gd, b), b.entities):
            area = (fr[2] - fr[0]) * (fr[3] - fr[1])
            good &= ok.count(X, Y, fr) == area
            if mr is not None:
                marea = (mr[2] - mr[0]) * (mr[3] - mr[1])
                good &= mine.count(X, Y, mr) >= max(1, math.ceil(marea * 0.75))
                good &= other.count(X, Y, mr) == 0
                if rich is not None:
                    good &= rich.count(X, Y, mr) >= min_amount
            else:
                pen += ORE_PENALTY * anyore.count(X, Y, fr)
                if not allow_ore_footprint:
                    good &= anyore.count(X, Y, fr) == 0
            pen += CLEAR_PENALTY * clear.count(X, Y, fr)
            if not good.any():
                break
        if not good.any():
            continue
        X, Y, pen = X[good], Y[good], pen[good]
        cx, cy = X + w / 2, Y + h / 2
        score = np.abs(cx - target[0]) + np.abs(cy - target[1]) + pen
        i = int(np.argmin(score))
        if best is None or score[i] < best[0]:
            best = (float(score[i]), b.at(int(X[i]), int(Y[i])))
    return best[1] if best else None


def place_free(grid, gd, block, target, radius=40, margin=1):
    """Best placement of `block` (any rotation) on placeable tiles off ore (trees/rocks may be cleared), with a
    free `margin` ring around it, nearest `target` within `radius`. -> placed Block | None (grid not changed)."""
    ok = _Sum(grid, grid.placeable_mask(allow_clear=True, avoid_resources=True))
    clear = _Sum(grid, grid.state == 1)
    best = None
    for k in range(4):
        b = block.rotated(k)
        w, h = b.size
        W, H = w + 2 * margin, h + 2 * margin
        X, Y = np.meshgrid(np.arange(math.floor(target[0] - radius), math.ceil(target[0] + radius) + 1),
                           np.arange(math.floor(target[1] - radius), math.ceil(target[1] + radius) + 1))
        X, Y = X.ravel() - margin, Y.ravel() - margin
        good = ok.count(X, Y, (0, 0, W, H)) == W * H
        if not good.any():
            continue
        X, Y = X[good], Y[good]
        cx, cy = X + W / 2, Y + H / 2
        score = np.abs(cx - target[0]) + np.abs(cy - target[1]) + CLEAR_PENALTY * clear.count(X, Y, (0, 0, W, H))
        i = int(np.argmin(score))
        if best is None or score[i] < best[0]:
            best = (float(score[i]), b.at(int(X[i]) + margin, int(Y[i]) + margin))
    return best[1] if best else None


def place_rows(grid, gd, make, patch, total, target, max_row=8, max_rows=4, amount=None, tag="onsite"):
    """Place `total` units of block `make(n)` on `patch` in up to `max_rows` rows of at most `max_row` units,
    nearest `target`; occupies the grid. amount: ore per grid tile (placer._amounts) for the richness check.
    -> [placed Block] (fewer units when the patch is short of room)."""
    rows, left = [], total
    tgt = target
    while left > 0 and len(rows) < max_rows:
        got = None
        for n in range(min(left, max_row), 0, -1):
            got = place_block(grid, gd, make(n), patch, tgt, amount)
            if got is not None:
                break
        if got is None:
            break
        _occupy(grid, gd, got.placed(), tag)
        rows.append(got)
        left -= len([e for e in got.entities if gd.entity(e.name).get("type") == "mining-drill"])
        ox, oy = got.origin
        tgt = (ox + got.size[0] / 2, oy + got.size[1] / 2)
    return rows


def footprint(gd, ents) -> set:
    t = set()
    for e in ents:
        t |= _tiles(gd, e)
    return t


def ore_tiles_under(grid, gd, ents) -> int:
    return sum(1 for t in footprint(gd, ents) if grid.resource(*t))


__all__ = ["place_rows", "place_block", "pick_patch", "patch_gap", "dist_point_patch", "tiles_of_box", "_amounts"]
