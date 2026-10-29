"""Existing builds as clusters (PLANNER-v2.md section 8): segment the force's non-ghost entities, classify them,
read their status, and price keeping, repairing or replacing them.

    cl = clusters(world, gd, state)          # [Cluster], biggest salvage value first
    c.removal_s                              # helper seconds to dismantle it
    c.rows()                                 # removes rows [name, x, y, unit] for a Plan

Never part of a cluster (never removed): ghosts of any kind, characters, vehicles, rails, and chests holding more
than 200 items (kept, noted "empty it first to allow moving").
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from . import cost

BELT_TYPES = {"transport-belt", "underground-belt", "splitter", "lane-splitter"}
NEVER = {"character", "car", "spider-vehicle", "locomotive", "cargo-wagon", "fluid-wagon", "artillery-wagon",
         "straight-rail", "curved-rail-a", "curved-rail-b", "half-diagonal-rail", "rail-ramp", "elevated-straight-rail",
         "elevated-curved-rail-a", "elevated-curved-rail-b", "elevated-half-diagonal-rail", "legacy-straight-rail",
         "legacy-curved-rail", "rail-support", "space-platform-hub", "cargo-landing-pad"}
FULL_CHEST = 200
GROUP_BASE = 1000           # ids of grouped candidates (several same-kind clusters next to each other)
MINING_S = {"transport-belt": 0.1, "underground-belt": 0.1, "splitter": 0.1, "inserter": 0.1, "electric-pole": 0.1,
            "furnace": 0.2, "mining-drill": 0.3, "lab": 0.2, "boiler": 0.2, "generator": 0.3, "container": 0.1,
            "assembling-machine": 0.2, "offshore-pump": 0.1, "pipe": 0.1, "pipe-to-ground": 0.1}
FURNACE_PER_MIN = 18.75     # stone furnace plates/min (0.3125/s)
STATUS_MAP = {"working": "working", "normal": "working", "low_power": "working", "no_fuel": "idle_no_fuel",
              "no_power": "idle_no_power", "no_ingredients": "idle_no_input", "no_input_fluid": "idle_no_input",
              "waiting_for_source_items": "idle_no_input", "no_minable_resources": "idle_no_input",
              "missing_required_fluid": "idle_no_input", "no_research_in_progress": "idle_no_input",
              "full_output": "output_full", "waiting_for_space_in_destination": "output_full",
              "full_burnt_result_output": "output_full"}


@dataclass
class Ent:
    name: str
    type: str
    x: float
    y: float
    dir: int
    box: tuple
    unit: int | None = None
    status: str | None = None
    items: dict = field(default_factory=dict)   # contents (fuel, input, output, chest)
    fuel: dict = field(default_factory=dict)    # the fuel part of `items` (can be taken out without removing it)


@dataclass
class Cluster:
    id: int
    kind: str
    ents: list
    status: str = "unknown"
    reasons: dict = field(default_factory=dict)        # status -> count
    touches: list = field(default_factory=list)       # ids of clusters this one connects (belt lines)
    note: str = ""
    keep_only: bool = False                           # never offered for removal (full chest ...)
    action: str = "keep"                              # keep | repair | reuse | replace
    reason: str = ""
    gain_min: float | None = None
    members: list = field(default_factory=list)       # grouped candidate: ids of the clusters it joins

    @property
    def member_ids(self) -> list:
        return self.members or [self.id]

    # ---- derived -------------------------------------------------------------------------------------------
    @property
    def counts(self) -> dict:
        return dict(Counter(e.name for e in self.ents).most_common())

    @property
    def bbox(self) -> tuple:
        return (min(e.box[0] for e in self.ents), min(e.box[1] for e in self.ents),
                max(e.box[2] for e in self.ents), max(e.box[3] for e in self.ents))

    @property
    def center(self) -> tuple:
        b = self.bbox
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

    @property
    def belt_tiles(self) -> int:
        return sum(1 for e in self.ents if e.type in BELT_TYPES)

    @property
    def machines(self) -> list:
        return [e for e in self.ents if e.type not in BELT_TYPES and e.type != "electric-pole"]

    def items(self, gd, contents=True) -> dict:
        """Salvage: each entity's own item (pickup refunds in full) plus its contents (contents=False: only the
        entities - their fuel can be taken out without taking anything apart, so it never justifies a removal)."""
        out = {}
        for e in self.ents:
            cost._add(out, cost.item_of(gd, e.name), 1)
            if contents:
                for k, v in e.items.items():
                    cost._add(out, k, v)
        return out

    def fuel(self) -> dict:
        out = {}
        for e in self.ents:
            for k, v in e.fuel.items():
                cost._add(out, k, v)
        return out

    def fe(self, gd) -> float:
        return cost.fe_equiv(cost.raw_of(self.items(gd), gd))

    def removal_s(self) -> float:
        """Helper seconds: mining time + reach overhead per entity + walking along the cluster."""
        b = self.bbox
        walk = (b[2] - b[0]) + (b[3] - b[1])
        return round(sum(MINING_S.get(e.type, 0.2) + cost.REACH_S for e in self.ents) + walk / cost.WALK, 1)

    def label(self) -> str:
        c = self.counts
        top = ", ".join(f"{n} {k.replace('-', ' ')}{'s' if n > 1 and not k.endswith('s') else ''}"
                        for k, n in list(c.items())[:3])
        return top

    def gps(self) -> str:
        cx, cy = self.center
        return f"[gps={round(cx)},{round(cy)}]"

    def rows(self) -> list:
        """removes rows [name, x, y, unit, cluster id]."""
        return [[e.name, e.x, e.y, e.unit, self.id] for e in self.ents]

    def rate_now(self) -> dict:
        return _rates([e for e in self.machines if e.status == "working"])

    def rate_fixed(self) -> dict:
        return _rates(self.machines)

    def to_dict(self, gd=None) -> dict:
        d = {"id": self.id, "kind": self.kind, "label": self.label(), "status": self.status, "reasons": self.reasons,
             "count_by_name": self.counts, "gps": self.gps(), "bbox": list(self.bbox), "belt_tiles": self.belt_tiles,
             "action": self.action, "reason": self.reason, "gain_min": self.gain_min, "removal_s": self.removal_s()}
        if gd is not None:
            d["fe_equiv"] = self.fe(gd)
        if self.note:
            d["note"] = self.note
        return d


def _rates(machines) -> dict:
    out = {}
    for e in machines:
        if e.type == "mining-drill":
            cost._add(out, "ore", 15.0 if "burner" in e.name else 30.0)
        elif e.type == "furnace":
            cost._add(out, "plates", FURNACE_PER_MIN)
    return out


def _gap(a, b) -> float:
    """Tiles of empty ground between two tile boxes (0 = touching, Chebyshev)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return max(dx, dy)


def _components(ents, max_gap=1):
    """Connected groups of entities whose boxes are at most `max_gap` empty tiles apart."""
    if not ents:
        return []
    pts = np.array([((e.box[0] + e.box[2]) / 2, (e.box[1] + e.box[3]) / 2) for e in ents])
    tree = cKDTree(pts)
    parent = list(range(len(ents)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j in tree.query_pairs(r=max_gap + 6):
        if _gap(ents[i].box, ents[j].box) <= max_gap:
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b
    groups = defaultdict(list)
    for i in range(len(ents)):
        groups[find(i)].append(ents[i])
    return list(groups.values())


def _kind(ents) -> str:
    c = Counter(e.type for e in ents)
    if c.get("furnace", 0) >= 2:
        return "furnace_row"
    if c.get("mining-drill"):
        return "drill_line"
    if c.get("lab"):
        return "labs"
    if c.get("boiler") or c.get("generator") or c.get("offshore-pump"):
        return "power_plant"
    if c.get("assembling-machine"):
        return "assembly"
    if c.get("container") and len(c) == 1:
        return "storage"
    if c.get("electric-pole") and len(c) == 1:
        return "power_line"
    if set(c) <= BELT_TYPES:
        return "belt_line"
    return "mixed"


def clusters(world, gd, state=None, rect=None) -> list[Cluster]:
    """Clusters of existing (non-ghost) force entities in `rect` (default: the whole export)."""
    idx = state.machine_index() if state is not None else {}
    chest_items = {}
    if state is not None:
        for c in state.chests:
            chest_items[(c.get("name"), float(c.get("x", 0)), float(c.get("y", 0)))] = dict(c.get("contents") or {})
    ents, kept = [], []
    has_power = False
    src = world.entities_in(rect) if rect else world.entities
    for me in src:
        if me.name.startswith("ghost:"):
            continue
        try:
            typ = gd.entity(me.name).get("type") or (me.extra or {}).get("type") or ""
        except KeyError:
            typ = (me.extra or {}).get("type") or ""
        if typ in NEVER or me.name in NEVER:
            continue
        unit = (me.extra or {}).get("unit")
        e = Ent(me.name, typ, float(me.x), float(me.y), int(me.dir or 0), tuple(me.box), unit)
        m = idx.get(("unit", unit)) if unit is not None else None
        m = m or idx.get((me.name, e.x, e.y))
        if m:
            e.status = STATUS_MAP.get(str(m.get("status") or ""), "disconnected" if m.get("status") else None)
            if e.status == "disconnected" and m.get("status") in ("idle", "unknown"):
                e.status = None
            for k in ("fuel_items", "input", "output"):
                for it, n in (m.get(k) or {}).items():
                    cost._add(e.items, it, n)
            if m.get("fuel_item") and m.get("fuel"):
                cost._add(e.items, m["fuel_item"], m["fuel"])
                cost._add(e.fuel, m["fuel_item"], m["fuel"])
            for it, n in (m.get("fuel_items") or {}).items():
                cost._add(e.fuel, it, n)
        if typ == "container":
            e.items = chest_items.get((me.name, e.x, e.y), {})
            if sum(e.items.values()) > FULL_CHEST:
                kept.append(e)
                continue
        if typ in ("generator", "solar-panel", "burner-generator"):
            has_power = True
        ents.append(e)
    # electric consumers without any generator in the base: no power (when the state didn't say)
    for e in ents:
        if e.status is None and not has_power and _electric(gd, e.name) and e.type != "electric-pole":
            e.status = "idle_no_power"
    belts = [e for e in ents if e.type in BELT_TYPES]
    poles = [e for e in ents if e.type == "electric-pole"]
    mach = [e for e in ents if e.type not in BELT_TYPES and e.type != "electric-pole"]
    out: list[Cluster] = []
    for g in _components(mach, 1):
        out.append(Cluster(len(out), _kind(g), g))
    # belts: 4-connected runs; short lines that serve one machine cluster belong to it
    owner = {}
    for c in out:
        for e in c.ents:
            owner[id(e)] = c
    mtree = cKDTree(np.array([((e.box[0] + e.box[2]) / 2, (e.box[1] + e.box[3]) / 2) for e in mach])) if mach else None
    for g in _components(belts, 0):
        touch = set()
        if mtree is not None:
            for b in g:
                for k in mtree.query_ball_point(((b.box[0] + b.box[2]) / 2, (b.box[1] + b.box[3]) / 2), r=3.0):
                    if _gap(b.box, mach[k].box) <= 1:
                        touch.add(owner[id(mach[k])].id)
        span = _span(g)
        if len(touch) == 1:
            c = out[next(iter(touch))]
            if span <= 2 * _span(c.ents) + 6:
                c.ents.extend(g)
                continue
        bl = Cluster(len(out), "belt_line", g, touches=sorted(touch))
        out.append(bl)
    # poles: with the cluster whose machines they power, else power lines
    lone = []
    for p in poles:
        d = (gd.pole(p.name)[0] if _has_pole(gd, p.name) else 2.5)
        best = None
        for c in out:
            if c.kind == "belt_line":
                continue
            for e in c.machines:
                if _electric(gd, e.name) and _gap((p.x - d, p.y - d, p.x + d, p.y + d), e.box) <= 0:
                    best = c
                    break
            if best:
                break
        if best:
            best.ents.append(p)
        else:
            lone.append(p)
    for g in _pole_groups(gd, lone):
        out.append(Cluster(len(out), "power_line", g))
    for e in kept:
        out.append(Cluster(len(out), "storage", [e], keep_only=True, note="holds more than 200 items: empty it first "
                                                                           "to allow moving"))
    for c in out:
        _status(c)
    return out


def groups(cls: list, gap=10) -> list[Cluster]:
    """Candidate groups: same-kind machine clusters (furnace rows, drill lines, labs) whose boxes are at most `gap`
    tiles apart, joined (e.g. the two rows of one furnace block). Only groups of >= 2 clusters are returned; their
    ids start at GROUP_BASE and `members` lists the clusters they join."""
    kinds = ("furnace_row", "drill_line", "labs")
    out = []
    for kind in kinds:
        cs = [c for c in cls if c.kind == kind and not c.keep_only]
        parent = {c.id: c.id for c in cs}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if _gap(a.bbox, b.bbox) <= gap:
                    parent[find(a.id)] = find(b.id)
        comp = defaultdict(list)
        for c in cs:
            comp[find(c.id)].append(c)
        for members in comp.values():
            if len(members) < 2:
                continue
            g = Cluster(GROUP_BASE + len(out), kind, [e for m in members for e in m.ents],
                        members=[m.id for m in members])
            _status(g)
            out.append(g)
    return out


def _span(ents) -> int:
    x0 = min(e.box[0] for e in ents)
    y0 = min(e.box[1] for e in ents)
    x1 = max(e.box[2] for e in ents)
    y1 = max(e.box[3] for e in ents)
    return max(x1 - x0, y1 - y0)


def _has_pole(gd, name):
    try:
        gd.pole(name)
        return True
    except Exception:
        return False


def _electric(gd, name) -> bool:
    try:
        return (gd.entity(name).get("energy_source") or {}).get("type") == "electric"
    except KeyError:
        return False


def _pole_groups(gd, poles):
    if not poles:
        return []
    parent = list(range(len(poles)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    tree = cKDTree(np.array([(p.x, p.y) for p in poles]))
    for i, j in tree.query_pairs(r=8.0):
        reach = min(gd.pole(poles[i].name)[1], gd.pole(poles[j].name)[1]) if _has_pole(gd, poles[i].name) else 7.5
        if math.hypot(poles[i].x - poles[j].x, poles[i].y - poles[j].y) <= reach + 1e-9:
            parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, p in enumerate(poles):
        groups[find(i)].append(p)
    return list(groups.values())


def _status(c: Cluster):
    ms = c.machines
    st = Counter(e.status for e in ms if e.status)
    c.reasons = dict(st)
    if c.kind in ("belt_line", "power_line", "storage") or not ms:
        c.status = "passive"
        return
    if not st:
        c.status = "unknown"
        return
    if st.get("working", 0) * 2 >= len(ms):
        c.status = "working"
        return
    c.status = max((k for k in st if k != "working"), key=lambda k: st[k])


def dist_to_patch(c: Cluster, patch) -> float:
    """Taxicab tiles from the cluster's box to the nearest tile of `patch` (world.Patch)."""
    if patch is None:
        return math.inf
    pts = np.array(list(patch.tiles)) + 0.5
    x0, y0, x1, y1 = c.bbox
    dx = np.maximum(0, np.maximum(x0 - pts[:, 0], pts[:, 0] - x1))
    dy = np.maximum(0, np.maximum(y0 - pts[:, 1], pts[:, 1] - y1))
    return float(np.min(dx + dy))


def summarize(cls: list, gd) -> dict:
    """Totals for world_summary / plan stats."""
    idle = [c for c in cls if c.status.startswith("idle") or c.status in ("output_full", "disconnected")]
    return {"clusters": len(cls), "entities": sum(len(c.ents) for c in cls),
            "belt_tiles": sum(c.belt_tiles for c in cls),
            "idle": [{"label": c.label(), "status": c.status, "gps": c.gps()} for c in
                     sorted(idle, key=lambda c: -len(c.ents))[:6]]}
