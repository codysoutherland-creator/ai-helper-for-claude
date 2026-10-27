"""Minimal static checks for planner blocks (stand-in until planner.validate exists; also used by the live test).

check(block, gd, ore=None) -> list of error strings (empty = ok). ore: set of map tiles with resources (drills).
"""
import math

from factorio_claude.planner.blocks import powered_by, pole_components, _electric, _burner
from factorio_claude.planner.gamedata import rotate, DIR_VEC


def _tile(x, y):
    return math.floor(x), math.floor(y)


def check(block, gd, ore=None, local=True):
    errs = []
    ents = block.entities if local else block.placed()
    ports = block.ports if local else block.placed_ports()
    ox, oy = (0, 0) if local else block.origin
    w, h = block.size
    occ = {}
    for i, e in enumerate(ents):
        for t in gd.footprint(e.name, e.x, e.y, e.direction):
            if t in occ:
                errs.append(f"overlap {e.name}@({e.x},{e.y}) with {ents[occ[t]].name} at {t}")
            occ[t] = i
            if not (ox <= t[0] < ox + w and oy <= t[1] < oy + h):
                errs.append(f"{e.name} at {t} outside block")
    belts = {t: e for e in ents if gd.entity(e.name).get("type") in ("transport-belt", "splitter", "underground-belt")
             for t in gd.footprint(e.name, e.x, e.y, e.direction)}
    for p in ports:
        if p.kind == "pole":
            if occ.get((p.x, p.y)) is None or ents[occ[(p.x, p.y)]].name.find("pole") < 0:
                errs.append(f"pole port {p} is not on a pole")
            continue
        if ox <= p.x < ox + w and oy <= p.y < oy + h:
            errs.append(f"port {p} inside block")
        dx, dy = DIR_VEC[p.direction]
        if p.kind == "belt_out":
            src = belts.get((p.x - dx, p.y - dy))
            if not src or src.direction != p.direction:
                errs.append(f"belt_out {p} not fed by a belt pointing at it")
        elif p.kind == "belt_in":
            dst = belts.get((p.x + dx, p.y + dy))
            if not dst or dst.direction == (p.direction + 8) % 16:
                errs.append(f"belt_in {p} does not enter a belt")
        elif p.kind in ("pipe_in", "pipe_out"):
            j = occ.get((p.x + dx, p.y + dy))
            conns = gd.fluid_connections(ents[j].name, ents[j].x, ents[j].y, ents[j].direction) if j is not None else []
            if not any((c[0], c[1]) == (p.x, p.y) for c in conns):
                errs.append(f"{p.kind} {p} has no fluid connection")
    for t, b in belts.items():
        dx, dy = DIR_VEC[b.direction]
        nxt = belts.get((t[0] + dx, t[1] + dy))
        if nxt and nxt.direction == (b.direction + 8) % 16:
            errs.append(f"head-on belts at {t}")
    # inserters and drills deliver to something real
    for e in ents:
        proto = gd.entity(e.name)
        if proto.get("type") == "inserter":
            ins = proto["inserter"]
            for what, vec in (("pickup", ins["pickup"]), ("drop", ins["drop"])):
                vx, vy = rotate(*vec, e.direction)
                t = _tile(e.x + vx, e.y + vy)
                if t not in occ:
                    errs.append(f"inserter at ({e.x},{e.y}) {what} {t} has nothing")
        if proto.get("type") == "mining-drill":
            vx, vy = rotate(*proto["vector_to_place_result"], e.direction)
            t = _tile(e.x + vx, e.y + vy)
            if t not in belts and t not in occ:
                errs.append(f"drill at ({e.x},{e.y}) drops on {t}: nothing there")
            if ore is not None:
                r = proto.get("mining_drill_radius", 1)
                x0, y0 = (e.x + block.origin[0], e.y + block.origin[1]) if local else (e.x, e.y)
                if not any(x0 - r < tx + 1 and tx < x0 + r and y0 - r < ty + 1 and ty < y0 + r for tx, ty in ore):
                    errs.append(f"drill at ({e.x},{e.y}) has no ore in its area")
    # power
    poles = [e for e in ents if gd.entity(e.name).get("type") == "electric-pole"]
    for e in ents:
        if gd.entity(e.name).get("type") != "electric-pole" and _electric(gd, e.name):
            if not any(powered_by(gd, p, e) for p in poles):
                errs.append(f"{e.name} at ({e.x},{e.y}) unpowered")
    if poles and len(pole_components(gd, poles)) > 1:
        errs.append(f"poles form {len(pole_components(gd, poles))} networks")
    # fuel: every burner is fed by an inserter or declared manual
    drops = set()
    for e in ents:
        ins = gd.entity(e.name).get("inserter")
        if ins:
            vx, vy = rotate(*ins["drop"], e.direction)
            drops.add(occ.get(_tile(e.x + vx, e.y + vy)))
    for i, e in enumerate(ents):
        if _burner(gd, e.name) and not (e.extra or {}).get("fuel") and i not in drops:
            if gd.entity(e.name).get("type") == "inserter":
                ins = gd.entity(e.name)["inserter"]
                vx, vy = rotate(*ins["pickup"], e.direction)
                if _tile(e.x + vx, e.y + vy) in belts:
                    continue  # self-fuels from the belt it picks from
            errs.append(f"burner {e.name} at ({e.x},{e.y}) has no fuel supply")
    # fluids: every boiler reachable from a pipe_in; every generator touches a steam source
    fl = [i for i, e in enumerate(ents)
          if any(not fb.get("conditional") for fb in gd.entity(e.name).get("fluidboxes") or [])]
    if fl:
        adj = {i: set() for i in fl}
        for i in fl:
            e = ents[i]
            for c in gd.fluid_connections(e.name, e.x, e.y, e.direction):
                j = occ.get((c[0], c[1]))
                if j is not None and j in adj:
                    f = ents[j]
                    back = gd.fluid_connections(f.name, f.x, f.y, f.direction)
                    if any(occ.get((b[0], b[1])) == i for b in back):
                        adj[i].add(j)
        starts = {occ.get((p.x + DIR_VEC[p.direction][0], p.y + DIR_VEC[p.direction][1]))
                  for p in ports if p.kind == "pipe_in"}
        seen, stack = set(), [s for s in starts if s is not None]
        while stack:
            a = stack.pop()
            if a in seen:
                continue
            seen.add(a)
            stack += adj[a]
        for i in fl:
            if i not in seen:
                errs.append(f"{ents[i].name} at ({ents[i].x},{ents[i].y}) not connected to the water input")
    return errs
