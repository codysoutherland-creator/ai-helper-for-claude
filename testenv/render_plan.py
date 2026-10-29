"""Render a World (map export) and optionally a Plan over it to a PNG, offline (numpy + zlib, no PIL).

    python testenv/render_plan.py <map_dir> <out.png> [--plan plan.json ...] [--data data.json]
                                  [--rect x0 y0 x1 y1] [--scale 3] [--no-ghosts]
    from testenv.render_plan import render; render(world, "out.png", plans=[plan], gd=gd)

Colours: ground brown, water blue, ore iron blue-grey / copper orange / coal black / stone tan, cliffs and rocks
dark grey, trees dark green. Existing buildings are drawn muted, existing ghosts as cyan-tinted, plan entities in
full colour (belts yellow, inserters green, furnaces white, drills orange, poles brown, pipes/pumps blue,
boilers/engines red, labs/assemblers violet). Existing entities a plan removes (planner v2 `removes`) are outlined
red with a red X. Markers: plan hub / iron site (magenta), mine ports and on-site rows (cyan), `marks` (x, y, rgb).
Grid lines every 32 tiles, labelled in tile coordinates.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

ORE = {"iron-ore": (104, 132, 168), "copper-ore": (204, 110, 60), "coal": (22, 22, 22), "stone": (186, 164, 112),
       "crude-oil": (150, 0, 150), "uranium-ore": (0, 200, 0)}
GROUND, UNKNOWN, WATER = (78, 68, 52), (36, 36, 36), (36, 78, 150)

_FONT = {  # 3x5 digits
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", "-": "000000111000000",
}


def ent_color(name: str):
    n = name
    if "belt" in n or "splitter" in n:
        return (235, 205, 40)
    if "inserter" in n:
        return (110, 220, 110)
    if "pole" in n or "substation" in n:
        return (150, 85, 40)
    if "furnace" in n:
        return (235, 235, 235)
    if "drill" in n:
        return (255, 140, 0)
    if "pipe" in n or "pump" in n:
        return (70, 170, 255)
    if "boiler" in n or "engine" in n:
        return (240, 50, 50)
    if "lab" in n or "assembling" in n:
        return (190, 110, 240)
    if "chest" in n:
        return (170, 120, 70)
    return (255, 255, 255)


def png_bytes(a: np.ndarray) -> bytes:
    h, w, _ = a.shape
    raw = b"".join(b"\x00" + a[i].tobytes() for i in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _text(img, x, y, s, color, px=2):
    for ch in s:
        g = _FONT.get(ch)
        if g:
            for k, bit in enumerate(g):
                if bit == "1":
                    i, j = y + (k // 3) * px, x + (k % 3) * px
                    img[max(i, 0):max(i + px, 0), max(j, 0):max(j + px, 0)] = color
        x += 4 * px


def _box(gd, name, x, y, d):
    if gd is not None:
        try:
            return gd.box(name, x, y, d or 0)
        except Exception:  # unknown prototype: 1x1
            pass
    return (math.floor(x), math.floor(y), math.floor(x) + 1, math.floor(y) + 1)


def render(world, out, plans=(), gd=None, rect=None, scale=3, marks=(), ghosts=True, mute_existing=True):
    """Draw `world` (planner World) and `plans` (Plan objects or entity lists) into a PNG at `out`.
    rect: (x0, y0, x1, y1) tile window (default: the whole export). -> (x0, y0, scale) of the image.
    A plan's placed stage 2 (`plan.next_stage`, planner v2) is drawn too, each entity outlined in cyan."""
    big, x0, y0 = draw(world, plans, gd, rect, scale, marks, ghosts, mute_existing)
    Path(out).write_bytes(png_bytes(big))
    return x0, y0, scale


def draw(world, plans=(), gd=None, rect=None, scale=3, marks=(), ghosts=True, mute_existing=True):
    """render() without writing: -> (RGB array, x0, y0)."""
    plans = list(plans)
    for p in list(plans):
        nxt = getattr(p, "next_stage", None)
        if nxt is not None and nxt not in plans:
            plans.append(nxt)
    x0, y0, x1, y1 = (int(math.floor(rect[0])), int(math.floor(rect[1])), int(math.ceil(rect[2])),
                      int(math.ceil(rect[3]))) if rect else tuple(int(v) for v in world.bbox)
    W, H = x1 - x0, y1 - y0
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = UNKNOWN
    # overlap of the window with the world arrays
    wi0, wi1 = max(y0, world.y0), min(y1, world.y0 + world.h)
    wj0, wj1 = max(x0, world.x0), min(x1, world.x0 + world.w)
    if wi0 < wi1 and wj0 < wj1:
        src = np.s_[wi0 - world.y0:wi1 - world.y0, wj0 - world.x0:wj1 - world.x0]
        dst = np.s_[wi0 - y0:wi1 - y0, wj0 - x0:wj1 - x0]
        sub = img[dst]
        sub[world.known[src]] = GROUND
        res = world.res[src]
        for k, n in enumerate(world.res_names):
            if k:
                sub[res == k] = ORE.get(n, (255, 0, 255))
        sub[world.water[src]] = WATER
        img[dst] = sub

    def fill(b, c):
        i0, i1 = max(int(math.floor(b[1])) - y0, 0), min(int(math.ceil(b[3])) - y0, H)
        j0, j1 = max(int(math.floor(b[0])) - x0, 0), min(int(math.ceil(b[2])) - x0, W)
        if i0 < i1 and j0 < j1:
            img[i0:i1, j0:j1] = c

    for o in world.obstacles_in((x0, y0, x1, y1)):
        c = (40, 90, 40) if o.kind == "tree" else (20, 20, 20) if o.kind == "cliff" else (230, 30, 30) \
            if o.kind == "enemy" else (95, 95, 95)
        tiles = o.tiles if o.tiles is not None else None
        if tiles:
            for tx, ty in tiles:
                fill((tx, ty, tx + 1, ty + 1), c)
        else:
            fill(o.box, c)
    for e in world.entities_in((x0, y0, x1, y1)):
        ghost = e.name.startswith("ghost:")
        if ghost and not ghosts:
            continue
        n = e.name[6:] if ghost else e.name
        c = np.array(ent_color(n), float)
        if ghost:
            c = c * 0.35 + np.array((0, 150, 170)) * 0.65
        elif mute_existing:
            c = c * 0.55 + np.array((60, 60, 60)) * 0.45
        fill(e.box, c.astype(np.uint8))
    hubs, ports, removed, later = [], [], [], []
    for p in plans:
        ents = getattr(p, "entities", p)
        st2 = (getattr(p, "stats", {}) or {}).get("class") == "stage2"
        for e in ents:
            b = _box(gd, e.name, e.x, e.y, e.direction)
            fill(b, ent_color(e.name))
            if st2:
                later.append(b)
        st = getattr(p, "stats", {}) or {}
        if st.get("hub"):
            hubs.append(st["hub"])
        for v in (st.get("mines") or {}).values():
            ports.append(v[:2])
        sites = st.get("sites") or {}
        for v in (sites.values() if isinstance(sites, dict) else [s["at"] for s in sites]):
            ports.append((v[0] - 0.5, v[1] - 0.5))
        gone = {(r[0], float(r[1]), float(r[2])) for r in getattr(p, "removes", None) or []}
        if gone:
            for e in world.entities_in((x0, y0, x1, y1)):
                if (e.name, float(e.x), float(e.y)) in gone:
                    removed.append(e.box)
    big = img.repeat(scale, 0).repeat(scale, 1)
    if scale >= 3:   # stage 2 (placed, built later): cyan outline per entity
        for b in later:
            _outline(big, b, x0, y0, scale, (0, 255, 255))
    red = (255, 30, 30)
    for b in removed:  # red outline + X over each removed entity
        i0, i1 = (b[1] - y0) * scale, (b[3] - y0) * scale - 1
        j0, j1 = (b[0] - x0) * scale, (b[2] - x0) * scale - 1
        if i1 < 0 or j1 < 0 or i0 >= H * scale or j0 >= W * scale:
            continue
        i0c, i1c, j0c, j1c = max(i0, 0), min(i1, H * scale - 1), max(j0, 0), min(j1, W * scale - 1)
        big[i0c, j0c:j1c + 1] = red
        big[i1c, j0c:j1c + 1] = red
        big[i0c:i1c + 1, j0c] = red
        big[i0c:i1c + 1, j1c] = red
        n = max(i1 - i0, 1)
        for k in range(n + 1):
            jj = j0 + (j1 - j0) * k // n
            for ii, jx in ((i0 + k, jj), (i0 + k, j1 - (jj - j0))):
                if 0 <= ii < H * scale and 0 <= jx < W * scale:
                    big[ii, jx] = red
    for t in range(W):
        if (t + x0) % 32 == 0:
            big[:, t * scale] = (0, 0, 0) if (t + x0) else (255, 255, 255)
            _text(big, t * scale + 3, 3, str(t + x0), (255, 255, 255))           # x labels along the top
    for t in range(H):
        if (t + y0) % 32 == 0:
            big[t * scale, :] = (0, 0, 0) if (t + y0) else (255, 255, 255)
            if t > 12:
                _text(big, 3, t * scale + 3, str(t + y0), (255, 255, 120))       # y labels down the left

    def cross(x, y, c, r=7):
        j, i = int((x - x0) * scale), int((y - y0) * scale)
        big[max(i - 1, 0):max(i + 2, 0), max(j - r, 0):max(j + r, 0)] = c
        big[max(i - r, 0):max(i + r, 0), max(j - 1, 0):max(j + 2, 0)] = c
    for x, y in ports:
        cross(x + 0.5, y + 0.5, (0, 255, 255), 5)
    for x, y in hubs:
        cross(x, y, (255, 0, 255), 10)
    for x, y, c in marks:
        cross(x, y, c, 8)
    return big, x0, y0


def _outline(big, b, x0, y0, scale, c):
    H, W = big.shape[0], big.shape[1]
    i0, i1 = int((b[1] - y0) * scale), int((b[3] - y0) * scale) - 1
    j0, j1 = int((b[0] - x0) * scale), int((b[2] - x0) * scale) - 1
    if i1 < 0 or j1 < 0 or i0 >= H or j0 >= W:
        return
    i0c, i1c, j0c, j1c = max(i0, 0), min(i1, H - 1), max(j0, 0), min(j1, W - 1)
    big[i0c, j0c:j1c + 1] = c
    big[i1c, j0c:j1c + 1] = c
    big[i0c:i1c + 1, j0c] = c
    big[i0c:i1c + 1, j1c] = c


def site_boxes(plan, pad=6) -> list:
    """Tile windows around each site of a planner v2 plan (and of its placed stage 2): its build_order boxes, the
    removed entities' bounding box. Long thin boxes (a pole line) are skipped: the overview shows them."""
    out = []
    for p in (plan, getattr(plan, "next_stage", None)):
        if p is None:
            continue
        for b in (p.stats or {}).get("build_order") or []:
            w, h = b[2] - b[0], b[3] - b[1]
            if max(w, h) <= 60:
                out.append((b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad))
    rm = getattr(plan, "removes", None) or []
    if rm:
        xs, ys = [r[1] for r in rm], [r[2] for r in rm]
        if max(xs) - min(xs) <= 60 and max(ys) - min(ys) <= 60:
            out.append((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))
    return out


def render_sheet(world, out, plan, gd=None, scale=10, pad=6, cols=3, gap=6):
    """One PNG with a zoomed panel (`scale` px per tile) per site of `plan` (see site_boxes), side by side, so the
    layout of each pair row / plant / lab block can be judged (the overview is a few px per entity). Panels keep
    their own tile grid lines every 32 tiles and the coordinate labels. -> list of the windows drawn."""
    boxes = site_boxes(plan, pad)
    if not boxes:
        return []
    panels = [draw(world, [plan], gd, rect=b, scale=scale)[0] for b in boxes]
    rows = [panels[i:i + cols] for i in range(0, len(panels), cols)]
    W = max(sum(p.shape[1] for p in r) + gap * (len(r) - 1) for r in rows)
    H = sum(max(p.shape[0] for p in r) for r in rows) + gap * (len(rows) - 1)
    sheet = np.zeros((H, W, 3), np.uint8)
    sheet[:] = (255, 255, 255)
    y = 0
    for r in rows:
        x = 0
        for p in r:
            sheet[y:y + p.shape[0], x:x + p.shape[1]] = p
            x += p.shape[1] + gap
        y += max(p.shape[0] for p in r) + gap
    Path(out).write_bytes(png_bytes(sheet))
    return boxes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("map_dir")
    ap.add_argument("out")
    ap.add_argument("--plan", action="append", default=[])
    ap.add_argument("--data", help="export_data json (exact entity sizes)")
    ap.add_argument("--rect", nargs=4, type=int)
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--no-ghosts", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from factorio_claude.planner.gamedata import GameData
    from factorio_claude.planner.plan import Plan
    from factorio_claude.planner.world import World
    world = World.load(a.map_dir)
    gd = GameData.load(a.data) if a.data else None
    plans = [Plan.load(p) for p in a.plan]
    render(world, a.out, plans, gd, tuple(a.rect) if a.rect else None, a.scale, ghosts=not a.no_ghosts)
    print(a.out)


if __name__ == "__main__":
    main()
