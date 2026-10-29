"""Starter-base planner v2 (docs/design/PLANNER-v2.md): state-aware, distance-aware, rebuild-aware variants.

    plans = plan_options(world, gd, state, goal, variants=3, allow_removal=True)   # [Plan], best first

Variants (old keys are aliases: compact -> quick, expandable -> longterm):
  quick     S0 (coal out of idle machines, refuel fixable ones) + S1 burner pairs on the iron patch and coal drills
            into chests: plates soonest, no belts
  balanced  S0 + S1 iron and copper pairs, coal and stone drills; stage 2 (power + research) estimated
  longterm  the v1 hub engine (central smelting + bus); when its gates fail it is re-staged: stage 1 = the quick
            start, the hub is an estimate for later
  rebuild   the best of quick/balanced plus the justified removal of specific existing clusters (section 8)
Every plan carries stats bill / eta / belts / gates / score and a card summary with bill + ETA + trade-off. The ETA comes
from agenda.simulate: what the helper really does after Approve, coal carried by its logistics round included.
"""
from __future__ import annotations

import copy
import math
import re
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from . import agenda as AG
from . import blocks as B
from . import cost, existing, quality
from .onsite import dist_point_patch, patch_gap, place_rows
from .placer import _amounts, _occupy, parse_goal
from .plan import Plan, entity_counts, fmt_n, nice, world_without
from .stage2 import plan_stage2
from .state import BaseState
from .validate import validate

ALIASES = {"compact": "quick", "expandable": "longterm", "balanced": "balanced"}
TITLES = {"quick": "Quick start", "balanced": "Balanced", "longterm": "Long-term hub", "rebuild": "Rebuild"}
TITLE_GROW = "Long-term (grow on site)"
PITCH = {"quick": "burner pairs on the iron patch",
         "balanced": "iron and copper smelted on their patches",
         "longterm": "central smelting hub with a bus",
         "grow": "twice the smelting, on the patches",
         "rebuild": "reuses parts of your old builds"}
TRADEOFF = {"quick": "iron plates soonest and cheapest; copper comes with stage 2",
            "balanced": "iron and copper from the start, a bit more to build and one more site for the helper to "
                        "keep fuelled",
            "longterm": "most expandable layout, but long belts and a big bill before the first plate",
            "grow": "twice the plates per minute for longer to build and more coal to carry; the rows leave room for "
                    "the electric columns of stage 3"}
MIN_PATCH = {"iron-ore": 100_000, "copper-ore": 50_000, "coal": 30_000, "stone": 20_000}
UNIT_RATE = 15.0                 # plates (or ore) per minute of one burner drill / pair
REMOVE_GAIN_MIN = 3.0            # section 8.3 thresholds
REMOVE_GAIN_FRAC = 0.10
REMOVE_GAIN_ASKED = 0.5          # the player asked to rebuild / tidy up: any clear gain is offered
MAX_CANDIDATES = 8
GROUP_GAP = 10                   # same-kind clusters this close form one candidate (two rows of one block)
NONE_MIN = 180.0                 # an ETA beyond the horizon counts as this in the score
NEST_NEAR = 120                  # biter nests this close to a site: warning (v1 NEST_RANGE)
NEST_AVOID = 40                  # a site this close to a nest is avoided when the resource has another patch
FUEL_TAKE = {"idle_no_input", "idle_no_power", "output_full", "disconnected"}   # idle: their fuel is free to take
FUEL_ITEMS = {"coal": 1.0, "wood": 0.5, "solid-fuel": 3.0}                      # coal equivalents (fuel value / 4 MJ)
REPAIR_MIN_ORE = 20              # furnaces holding fewer ore than this are not worth an S0 refuel trip
FAR_COAL = 100                   # a site farther than this from the coal patch: one spare coal drill
RES_ORDER = ("coal", "iron-ore", "copper-ore", "stone")    # plan entity order = the order the helper fuels them
PRODUCT = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "coal": "coal", "stone": "stone"}
UNIT_WORD = {"iron-ore": "iron pairs", "copper-ore": "copper pairs", "coal": "coal drills", "stone": "stone drills"}


@dataclass
class Spec:
    variant: str
    iron: int = 4
    copper: int = 0
    stone: int = 0
    coal: int | None = None      # None: sized from the fuel the rows burn
    target: dict = field(default_factory=dict)
    stage2: bool = False         # estimate stage 2 (power + research) and count it in the score


@dataclass
class Layout:
    rows: dict                   # resource -> [placed Block]
    ents: list
    sites: dict                  # resource -> (x, y) centre of its rows
    units: dict                  # resource -> units placed
    burn: float                  # coal/min of everything
    grid: object = None


@dataclass
class Repair:
    """S0: helper-only fix of an idle cluster (refuel drills that drop into a furnace/chest, furnaces holding ore)."""
    cluster: object
    rows: list                   # refuel rows [name, x, y, unit, n]
    units: list                  # agenda Units (site -1)
    once: dict                   # items it gives once (plates from ore already inside)


@dataclass
class Convert:
    """R7 'reuse' (round 3): an idle burner-drill line of the player's on ore that drops onto a belt nothing smelts
    from becomes burner pairs WHERE IT STANDS: its belt is taken up (the only removal), a stone furnace + burner
    inserter + chest go in front of every drill (coal/stone drills: a chest). The drills stay; the plan lists them
    (extra existing=True: no ghost, no bill) so the helper's round fuels them."""
    cluster: object
    res: str
    block: object                # B.Block at its map origin: new entities + the player's drills (extra existing)
    n: int                       # drills converted
    belts: list                  # removes rows [name, x, y, unit, cluster id] (the belt line only)
    drills: list                 # existing.Ent of the converted drills
    skipped: int = 0             # drills left alone (too little ore under them, or no room in front)

    def drill_rows(self) -> list:
        return [[d.name, d.x, d.y, d.unit, self.cluster.id] for d in self.drills]

    def removal_s(self) -> float:
        n = len(self.belts)
        xs = [r[1] for r in self.belts] or [0]
        ys = [r[2] for r in self.belts] or [0]
        return round(n * (existing.MINING_S["transport-belt"] + cost.REACH_S) +
                     ((max(xs) - min(xs)) + (max(ys) - min(ys))) / cost.WALK, 1)

    def items(self) -> dict:
        out = {}
        for r in self.belts:
            cost._add(out, r[0], 1)
        return out


CONVERT_MIN_ORE = 600            # ore under a converted drill's area: 40 min of work at 15/min, else it is left alone


class PlannerV2:
    def __init__(self, world, gd, state: BaseState | None = None, goal=None, *, allow_removal=True,
                 time_budget=40.0):
        self.world, self.gd = world, gd
        self.goal = parse_goal(goal)
        text = (self.goal.get("text") or "").lower()
        self.scale = float(self.goal.get("scale") or 1.0)
        near = self.goal.get("near")
        self.state = state or BaseState.fresh_start(tuple(near) if near else (0.0, 0.0))
        self.near = tuple(near) if near else (self.state.pos or (0.0, 0.0))
        self.allow_removal = allow_removal
        self.rebuild_words = bool(re.search(r"rebuild|from scratch|start over|tear (it )?down|replace everything",
                                            text))
        # asked to clean up / reuse old builds: removals with any clear gain are offered (caps still apply)
        self.tidy_words = self.rebuild_words or bool(re.search(
            r"tidy|clean ?up|dismantle|take apart|reuse (my|the) old|replace (my|the) old|move (my|the)", text))
        self.longterm_words = bool(re.search(r"long[- ]term|expandable|\bbus\b|megabase", text))
        self.electric_words = bool(re.search(r"\belectric|\bsteam|\bpower", text))
        self.time_budget = time_budget
        self.H = self.state.helper_rate()
        self.clusters = existing.clusters(world, gd, state)
        self.groups = existing.groups(self.clusters, GROUP_GAP)
        self.reasons: list = []
        self.skipped: dict = {}          # resource -> why its optional site was left out (biter nests)
        self.rebuild_plan = None
        self.hub_skip = None
        self._conv = None
        self.nests = np.array([(o.x, o.y) for o in world.obstacles if o.kind == "enemy"], float).reshape(-1, 2)
        self._nest_tree = cKDTree(self.nests) if len(self.nests) else None
        self._trees: dict = {}
        self._patches()

    # ---- analysis ------------------------------------------------------------------------------------------
    def nest_dist(self, p) -> float:
        if self._nest_tree is None or p is None:
            return math.inf
        return float(self._nest_tree.query(p)[0])

    def _pick(self, res, near, anchor=None, anchor_weight=0.0):
        """The patch of `res` minimising distance to `near` (+ anchor_weight x gap to `anchor`), among patches with
        enough ore; a patch whose nearest part sits next to biter nests only when there is no other."""
        cands = self.world.patches(min_tiles=12, name=res)
        if not cands:
            return None
        big = [p for p in cands if p.amount >= MIN_PATCH[res]] or cands[:1]

        def key(p):
            tile, d = dist_point_patch(near, p)
            k = d + (anchor_weight * patch_gap(p, anchor)[2] if anchor is not None and anchor_weight else 0.0)
            if self.nest_dist(tile) < NEST_AVOID:
                k += 1000.0
            return k
        return min(big, key=key)

    def _patches(self):
        coal0 = self._pick("coal", self.near)
        self.iron = self._pick("iron-ore", self.near, anchor=coal0, anchor_weight=0.5)
        if self.iron is None:
            raise RuntimeError("no iron ore patch in the exported area")
        tgt = dist_point_patch(self.near, self.iron)[0]
        if coal0 is not None:
            tgt = patch_gap(self.iron, coal0)[0]
        self.iron_target = self._away_from_nests(self.iron, tgt)
        self.coal = self._pick("coal", self.iron_target)
        self.copper = self._pick("copper-ore", self.iron_target)
        self.stone = self._pick("stone", self.iron_target)
        self.dist = {}
        for name, p in (("coal", self.coal), ("copper-ore", self.copper), ("stone", self.stone)):
            self.dist[name] = round(patch_gap(self.iron, p)[2]) if p is not None else None
        self.dist["player"] = round(dist_point_patch(self.near, self.iron)[1])
        self.topology = self._topology()
        pts = [self.near] + [(b[0], b[1]) for p in (self.iron, self.coal, self.copper, self.stone) if p
                             for b in (p.bbox, (p.bbox[2], p.bbox[3]))]
        x0, y0, x1, y1 = self.world.bbox
        m = 24
        self.rect = (max(x0, int(min(p[0] for p in pts)) - m), max(y0, int(min(p[1] for p in pts)) - m),
                     min(x1, int(max(p[0] for p in pts)) + m), min(y1, int(max(p[1] for p in pts)) + m))

    def _away_from_nests(self, patch, toward):
        """The outline tile of `patch` nearest `toward` that is at least NEST_AVOID from any nest (else `toward`)."""
        if patch is None or self._nest_tree is None:
            return toward
        pts = np.array(sorted(patch.outline() or patch.tiles), float) + 0.5
        d, _ = self._nest_tree.query(pts)
        ok = pts[d >= NEST_AVOID]
        if not len(ok):
            return toward
        i = int(np.argmin(np.abs(ok[:, 0] - toward[0]) + np.abs(ok[:, 1] - toward[1])))
        return (int(math.floor(ok[i][0])), int(math.floor(ok[i][1])))

    def _topology(self) -> dict:
        """Section 5.2: transport per ore for the electric stage (bootstrap is always on-site)."""
        out = {}
        for ore in ("copper-ore", "coal"):
            d = self.dist.get(ore)
            if d is None:
                continue
            out[ore] = "belt_ore" if d <= 40 else ("onsite_belt_plates" if d <= 100 else "onsite_ferry")
        kinds = set(out.values())
        name = "CENTRAL" if kinds <= {"belt_ore"} else ("ON-SITE" if kinds <= {"onsite_ferry"} else "HYBRID")
        return {"name": name, "transport": out}

    def patch_point(self, raw, near):
        """Nearest tile of the plan's patch of `raw` to `near` (agenda: where the helper hand-mines it)."""
        p = {"iron-ore": self.iron, "copper-ore": self.copper, "coal": self.coal, "stone": self.stone}.get(raw)
        if p is None or near is None:
            return None
        if raw not in self._trees:
            pts = np.array(sorted(p.outline() or p.tiles), float) + 0.5
            self._trees[raw] = (pts, cKDTree(pts))
        pts, tree = self._trees[raw]
        d, k = tree.query(near)
        if d > AG.MINE_RADIUS:   # the mod's material planner doesn't look further than this for ore to mine
            return None
        return (float(pts[k][0]), float(pts[k][1]))

    # ---- layout --------------------------------------------------------------------------------------------
    def _chest(self) -> str:
        if self.state.stock().get("wood", 0) >= 8:
            return "wooden-chest"
        cx, cy = self.iron_target
        trees = [o for o in self.world.obstacles_in((cx - 40, cy - 40, cx + 40, cy + 40)) if o.kind == "tree"]
        return "wooden-chest" if len(trees) >= 4 else "iron-chest"

    def layout(self, spec: Spec, removes=(), converting=()) -> Layout:
        gd = self.gd
        gone = list(removes) + [r for cv in converting for r in cv.drill_rows()]
        w = world_without(self.world, gone) if gone else self.world
        grid = w.grid(self.rect)
        amount = _amounts(w, grid)
        chest = self._chest()
        rows, sites, units = {}, {}, {}
        ents = []
        conv_units = {}
        for cv in converting:   # the player's drills, turned into pairs where they stand (their belt is removed)
            _occupy(grid, gd, cv.block.placed(), "v2:conv")
            rows.setdefault(cv.res, []).append(cv.block)
            ents.extend(cv.block.placed())
            conv_units[cv.res] = conv_units.get(cv.res, 0) + cv.n
            units[cv.res] = units.get(cv.res, 0) + cv.n

        def centre_of(rs):
            xs = [r.origin[0] + r.size[0] / 2 for r in rs]
            ys = [r.origin[1] + r.size[1] / 2 for r in rs]
            return (round(sum(xs) / len(xs), 1), round(sum(ys) / len(ys), 1))
        for res in conv_units:
            sites[res] = centre_of(rows[res])

        def put(res, make, patch, n, target):
            n = max(0, n - conv_units.get(res, 0))
            if not n or patch is None:
                return
            target = self._away_from_nests(patch, target)
            if res in ("copper-ore", "stone") and self.nest_dist(target) < NEST_AVOID:
                # an optional site on a patch the biters sit on: left out (the iron and coal rows never are)
                self.skipped[res] = (f"the {nice(res).replace(' ore', '')} patch has biter nests "
                                     f"[gps={round(target[0])},{round(target[1])}]: no {UNIT_WORD[res]} there")
                return
            rs = place_rows(grid, gd, make, patch, n, target, amount=amount, tag=f"v2:{res}")
            if not rs:
                self.reasons.append(f"no room for {nice(res)} rows")
                return
            rows.setdefault(res, []).extend(rs)
            pe = [e for r in rs for e in r.placed()]
            ents.extend(pe)
            units[res] = units.get(res, 0) + sum(1 for e in pe if e.name == "burner-mining-drill")
            sites[res] = centre_of(rows[res])

        put("iron-ore", lambda n: B.burner_pair_row(n, gd, ore="iron-ore", chest=chest), self.iron, spec.iron,
            self.iron_target)
        k = sites.get("iron-ore", self.iron_target)
        if spec.copper and self.copper is not None and "copper-ore" not in conv_units:
            put("copper-ore", lambda n: B.burner_pair_row(n, gd, ore="copper-ore", chest=chest), self.copper,
                spec.copper, dist_point_patch(k, self.copper)[0])
        burn = sum(-r.rates.get("coal", 0) for res, rs in rows.items() if res not in ("stone", "coal") for r in rs)
        if spec.stone and self.stone is not None and "stone" not in conv_units:
            put("stone", lambda n: B.drill_to_chest(n, gd, resource="stone", chest=chest), self.stone, spec.stone,
                dist_point_patch(k, self.stone)[0])
        burn += sum(-r.rates.get("coal", 0) for r in rows.get("stone", []))
        conv_coal = sum(r.rates.get("coal", 0) for r in rows.get("coal", []))   # converted coal drills (net coal)
        n_coal = spec.coal if spec.coal is not None else max(1, math.ceil((burn - conv_coal) / 12.75 - 1e-9))
        n_coal += conv_units.get("coal", 0)
        if spec.coal is None and self.coal is not None and lay_far(sites, self.coal, FAR_COAL):
            n_coal += 1   # the helper carries the coal far: one more drill so the chests fill faster than it is used
        if self.coal is not None:
            put("coal", lambda n: B.drill_to_chest(n, gd, resource="coal", chest=chest), self.coal, n_coal,
                dist_point_patch(k, self.coal)[0])
        # plan order = the order the helper fuels them: coal drills first (they make the fuel), then each pair
        ordered = [e for res in RES_ORDER for r in rows.get(res, []) for e in r.placed()]
        return Layout(rows, ordered, sites, units, round(burn, 2), grid)

    # ---- agenda (units, sites) for the ETA model --------------------------------------------------------------------
    def _typ(self, name):
        try:
            return self.gd.entity(name).get("type") or ""
        except KeyError:
            return ""

    def units_sites(self, lay: Layout):
        """Agenda units (one per drill: a burner pair, a coal or a stone drill) and build sites (one per resource)."""
        gd = self.gd
        units, sites = [], []
        for res in RES_ORDER:
            blocks = lay.rows.get(res) or []
            if not blocks:
                continue
            site_ents, ui = [], []
            for b in blocks:
                pe = b.placed()
                site_ents += pe
                drills = [e for e in pe if self._typ(e.name) == "mining-drill"]
                others = [e for e in pe if self._typ(e.name) != "mining-drill"]
                n = max(1, len(drills))
                net = b.rates.get("coal", 0) / n
                for d in drills:
                    grp = [d] + [o for o in others if abs(o.x - d.x) + abs(o.y - d.y) <= 5 and
                                 (abs(o.x - d.x) < 1.5 or abs(o.y - d.y) < 1.5)]
                    for o in grp[1:]:
                        others.remove(o)
                    hand = [e for e in grp if (e.extra or {}).get("fuel") == "manual"]
                    fill = sum(AG.FILL.get(self._typ(e.name), 2) for e in hand)
                    # the player's own drill (a conversion) gets no start fuel when placed: the round fuels it
                    start = sum(AG.START_FUEL.get(self._typ(e.name), 0) for e in hand
                                if not (e.extra or {}).get("existing"))
                    if res == "coal":
                        kind, item, rate, burn = "coal", "coal", UNIT_RATE, UNIT_RATE - net
                    elif res == "stone":
                        kind, item, rate, burn = "stone", "stone", UNIT_RATE, -net
                    else:
                        kind, item, rate, burn = "pair", PRODUCT[res], UNIT_RATE, -net
                    ui.append(len(units))
                    units.append(AG.Unit(f"{nice(item)} {len(ui)}", (d.x, d.y), kind, item, rate, max(0.01, burn),
                                         fill, start, len(hand), len(sites)))
            new = [e for e in site_ents if not (e.extra or {}).get("existing")]
            sites.append(AG.Site(UNIT_WORD.get(res, res), lay.sites[res], cost.bill(new, gd, fuel=False),
                                 len(new), ui))
        return units, sites

    def takes(self, removing_ids=(), need=0.0) -> list:
        """S0 reuse (section 8.2): fuel out of idle machines of the base (not working, not being removed) - their fuel
        does nothing there and needs no dismantling. Nearest the helper first, up to `need` coal equivalents.
        -> rows [name, x, y, unit, item, count, cluster]."""
        if need <= 0:
            return []
        start = self.state.pos or self.near
        cands = []
        for c in self.clusters:
            if c.id in removing_ids or c.keep_only:
                continue
            for e in c.machines:
                if e.status in FUEL_TAKE and e.fuel and e.unit is not None:
                    cands.append((abs(e.x - start[0]) + abs(e.y - start[1]), c, e))
        cands.sort(key=lambda t: t[0])
        rows, got = [], 0.0
        for _d, c, e in cands:
            if got >= need:
                break
            for item, n in e.fuel.items():
                if item in FUEL_ITEMS and n >= 1:
                    rows.append([e.name, e.x, e.y, e.unit, item, int(n), c.id])
                    got += n * FUEL_ITEMS[item]
        return rows

    def conversions(self) -> dict:
        """Clusters that can be turned into burner pairs where they stand (Convert): idle burner drills on one ore,
        nothing but belts besides them, none dropping into a furnace/chest yet (those are S0 repairs). The pair is
        canonical blocks.burner_pair_row(1) (drill_to_chest(1) for coal/stone) rotated to the drill's direction and
        anchored on the drill; its other tiles must be free once the belts are gone. -> {cluster id: Convert}."""
        if self._conv is not None:
            return self._conv
        gd = self.gd
        out = {}
        chest = self._chest()
        for c in self.clusters:
            if c.keep_only or c.status == "working" or c.kind != "drill_line":
                continue
            drills = [e for e in c.ents if e.type == "mining-drill"]
            others = [e for e in c.ents if e.type != "mining-drill"]
            if not drills or not others or any(e.type not in existing.BELT_TYPES for e in others):
                continue
            if any(d.name != "burner-mining-drill" or d.status == "working" for d in drills) or \
                    any(_drops_into(d, c, gd) for d in drills):
                continue
            belts = [[e.name, e.x, e.y, e.unit, c.id] for e in others]
            w = world_without(self.world, belts + [[d.name, d.x, d.y] for d in drills])
            x0, y0, x1, y1 = c.bbox
            grid = w.grid((int(x0) - 8, int(y0) - 8, int(x1) + 8, int(y1) + 8))
            used, ents, ok_drills, skipped, res_all = set(), [], [], 0, set()
            for d in drills:
                tiles = gd.footprint(d.name, d.x, d.y, d.dir or 0)
                ores = [self.world.resource_at(tx + 0.5, ty + 0.5) for tx, ty in tiles]
                names = {o[0] for o in ores if o}
                amount = sum(o[1] for o in ores if o)
                if len(names) != 1 or amount < CONVERT_MIN_ORE:
                    skipped += 1
                    continue
                res = names.pop()
                if res in ("iron-ore", "copper-ore"):
                    blk = B.burner_pair_row(1, gd, ore=res, chest=chest)
                elif res in ("coal", "stone"):
                    blk = B.drill_to_chest(1, gd, resource=res, chest=chest)
                else:
                    skipped += 1
                    continue
                k = int(d.dir or 0) // 4
                b = blk.rotated(k)
                bd = next(e for e in b.entities if e.name == d.name)
                ox, oy = d.x - bd.x, d.y - bd.y
                if abs(ox - round(ox)) > 1e-6 or abs(oy - round(oy)) > 1e-6:
                    skipped += 1
                    continue
                placed = [e for e in b.at(int(round(ox)), int(round(oy))).placed() if e.name != d.name]
                foot = set()
                for e in placed:
                    foot |= gd.footprint(e.name, e.x, e.y, e.direction)
                if foot & used or not grid.can_place(foot):
                    skipped += 1
                    continue
                used |= foot | tiles
                res_all.add(res)
                ents += placed + [B.PE(d.name, d.x, d.y, int(d.dir or 0), extra={"fuel": "manual", "existing": True})]
                ok_drills.append(d)
            if not ok_drills or len(res_all) != 1 or len(ok_drills) < max(1, len(drills) // 2):
                continue
            res = res_all.pop()
            xs = [math.floor(e.x - 1) for e in ents]
            ys = [math.floor(e.y - 1) for e in ents]
            bx, by = min(xs), min(ys)
            blk = B.Block("converted_" + ("pairs" if res.endswith("-ore") else "drills"),
                          [e.moved(-bx, -by) for e in ents],
                          (max(math.ceil(e.x + 1) for e in ents) - bx, max(math.ceil(e.y + 1) for e in ents) - by),
                          [], origin=(bx, by))
            ref = (B.burner_pair_row(len(ok_drills), gd, ore=res, chest=chest) if res.endswith("-ore") else
                   B.drill_to_chest(len(ok_drills), gd, resource=res, chest=chest))
            blk.rates = dict(ref.rates)
            blk.info = {"converted": len(ok_drills), "cluster": c.id}
            out[c.id] = Convert(c, res, blk, len(ok_drills), belts, ok_drills, skipped)
        self._conv = out
        return out

    def repairs(self, removing_ids=()) -> list:
        """S0 repair (section 10): refuel idle burner drills that sit on ore and drop into a furnace or chest of the
        same build, and furnaces that hold ore but no fuel. Only for clusters the state says are idle for lack of
        fuel; everything else gets a 'why not' reason (see review_idle)."""
        gd = self.gd
        out = []
        for c in self.clusters:
            if c.id in removing_ids or c.keep_only:
                continue
            drills = [e for e in c.machines if e.type == "mining-drill" and e.status == "idle_no_fuel" and
                      self.world.resource_at(e.x, e.y) and _drops_into(e, c, gd)]
            fed = [e for e in c.machines if e.type == "furnace" and e.status == "idle_no_fuel" and
                   any(k.endswith("-ore") for k in e.items)]
            ore_in = sum(v for e in fed for k, v in e.items.items() if k.endswith("-ore"))
            if not drills and ore_in < REPAIR_MIN_ORE:   # a few ore left in a furnace: not worth a trip
                continue
            rows, units, once = [], [], {}
            targets = set()
            for d in drills:
                res = (self.world.resource_at(d.x, d.y) or (None,))[0]
                into = _drop_target(d, c, gd)
                item = PRODUCT.get(res, res)
                ents = [d] + ([into] if into is not None and into.type == "furnace" else [])
                if into is not None and into.type == "furnace":
                    targets.add(id(into))
                fill = sum(AG.FILL.get(e.type, 2) for e in ents)
                units.append(AG.Unit(f"your {nice(d.name)}", (d.x, d.y), "pair" if len(ents) > 1 else "stone",
                                     item if len(ents) > 1 else res, UNIT_RATE, 2.25 + (1.08 if len(ents) > 1 else 0),
                                     fill, 0, len(ents), -1))
                for e in ents:
                    rows.append([e.name, e.x, e.y, e.unit, AG.FILL.get(e.type, 2)])
            for f in fed:
                if id(f) in targets:
                    continue
                rows.append([f.name, f.x, f.y, f.unit, AG.FILL["furnace"]])
                for k, v in f.items.items():
                    if k.endswith("-ore"):
                        cost._add(once, k.replace("-ore", "-plate"), v)
            c.action = "repair"
            if units:
                c.reason = (f"idle for lack of fuel: the helper refuels {len(rows)} of its machines first and "
                            f"{len(units)} drills start producing again")
            else:
                c.reason = (f"idle for lack of fuel: the helper refuels {len(rows)} furnaces that still hold "
                            f"{round(ore_in)} ore")
            out.append(Repair(c, rows, units, once))
        return out

    def review_idle(self, used_ids, s2plan=None, took=()) -> list:
        """A one-line verdict for every idle cluster the plan does not repair or remove (shown to the player):
        [(cluster, why)]; `took`: clusters whose fuel the plan takes."""
        out = []
        s2_labs = (s2plan.stats.get("labs") or {}).get("reused") if s2plan is not None else 0
        for c in self.clusters:
            if c.id in used_ids or c.status in ("working", "passive", "unknown") or c.keep_only:
                continue
            st = c.status
            fuel = sum(v * FUEL_ITEMS.get(k, 0) for k, v in c.fuel().items())
            labs = any(e.type == "lab" for e in c.ents)
            if st == "idle_no_power" and labs and s2_labs:
                why = "stage 2 powers them where they stand"
            elif st == "idle_no_power" and labs:
                why = "no power; stage 2 builds new labs by the iron instead (a pole line here costs more)"
            elif st == "idle_no_power":
                feeds = any(e.type == "furnace" for e in c.ents)
                why = ("its electric inserters have no power and nothing brings it ore: power alone would not start "
                       "it, so the plan leaves it as it is" if feeds else "no power; the plan's pole line does not "
                       "go there")
            elif st == "idle_no_input":
                d = existing.dist_to_patch(c, self.iron)
                why = ("nothing feeds it" + (f" and the iron ore is {round(d)} tiles away" if d < 10 ** 6 else "") +
                       ": feeding it by hand would cost more helper time than new burner pairs on the patch")
            elif st == "output_full":
                why = "its output is blocked"
            elif st == "idle_no_fuel":
                why = "no fuel, and refuelling it alone makes nothing: its output goes nowhere that runs"
            else:
                why = st.replace("_", " ")
            if fuel >= 1 and c.id in took:
                why += f"; its {round(fuel)} coal is used to start the new burners"
            # c.action/c.reason may hold another variant's decision (clusters are shared): everything listed here
            # is kept by THIS plan, so the verdict is always computed for it
            out.append((c, why))
        return out

    # ---- one plan --------------------------------------------------------------------------------------------
    def build(self, spec: Spec, removing=(), with_s2=True, converting=()) -> Plan:
        """One plan: stage 1 of `spec` (+ its placed stage 2), dismantling the clusters in `removing` and turning
        the Converts in `converting` into pairs where they stand (only their belts are removed)."""
        gd = self.gd
        removes = [r for c in removing for r in c.rows()] + [r for cv in converting for r in cv.belts]
        rm_ids = {m for c in removing for m in c.member_ids} | {cv.cluster.id for cv in converting}
        gone = removes + [r for cv in converting for r in cv.drill_rows()]   # what the new layout may stand on
        lay = self.layout(spec, removes, converting)
        if not lay.ents:
            raise RuntimeError("nothing could be placed (no room on the patches)")
        units, sites = self.units_sites(lay)
        reps = self.repairs(rm_ids)
        rep_units = [u for r in reps for u in r.units]
        stock = self.stock_for(lay)
        salvage = {}
        for c in removing:
            for k, v in c.items(gd).items():
                cost._add(salvage, k, v)
        for cv in converting:
            for k, v in cv.items().items():
                cost._add(salvage, k, v)
        first_fill = sum(u.fill for u in units) + sum(u.fill for u in rep_units)
        have_coal = stock.get("coal", 0) + 0.5 * stock.get("wood", 0) + salvage.get("coal", 0)
        take_rows = self.takes(rm_ids, first_fill - have_coal)
        takes_by = {}
        for r in take_rows:
            c = next(c for c in self.clusters if c.id == r[6])
            t = takes_by.setdefault(c.id, [c.center, {}, 0])
            cost._add(t[1], r[4], r[5])
            t[2] += 1
        target = {}
        for u in units:
            if u.kind == "pair":
                cost._add(target, u.item, u.rate)
        for item in list(target):   # full rate = the variant's target; more (a converted line) is a bonus, not a cost
            if spec.target.get(item):
                target[item] = min(target[item], spec.target[item])
        bb_x = [e.x for e in lay.ents]
        bb_y = [e.y for e in lay.ents]
        centre = ((min(bb_x) + max(bb_x)) / 2, (min(bb_y) + max(bb_y)) / 2)
        usable = self.state.player_usable
        for s in sites:   # the mod's fuel list starts with the S0 repairs, then the plan's burners
            s.units = [i + len(rep_units) for i in s.units]
        ag = AG.Agenda(stock=stock, start=self.state.pos or self.near, sites=sites, units=rep_units + units,
                       target=target, takes=list(takes_by.values()),
                       dismantles=[(c.center, c.removal_s(), c.items(gd)) for c in removing] +
                       [(cv.cluster.center, cv.removal_s(), cv.items()) for cv in converting],
                       patch=None, centre=centre,
                       player=self.state.player_pos if usable else None,
                       player_items=dict(self.state.player_inv) if usable else {})
        # build order (payback, section 9.2): plates first, or the coal drills first; whichever the model says
        # gets plates flowing sooner overall. The mod builds the sites in exactly this order.
        names = [s.name for s in sites]
        coal_i = [i for i, s in enumerate(sites) if s.name == UNIT_WORD["coal"]]
        rest = [i for i in range(len(sites)) if i not in coal_i]
        best = None
        for order in ([*rest, *coal_i], [*coal_i, *rest]) if coal_i and rest else ([*range(len(sites))],):
            a2 = copy.deepcopy(ag)
            a2.patch = self.patch_point
            a2.order = order
            e = AG.simulate(a2, gd)
            sus = e.get("sustained")
            k = (e["first_plates"] if e["first_plates"] is not None else NONE_MIN) + \
                (e["target"] if e["target"] is not None else NONE_MIN) + \
                (5 if sus is not None and sus < quality.FUEL_SUSTAIN else 0)   # the gate it would get
            if best is None or k < best[0] - 1e-9:
                best = (k, e, order)
        _k, eta, order = best
        build_order = []
        for i in order:
            res = next(r for r in RES_ORDER if UNIT_WORD[r] == names[i])
            x0 = min(b.origin[0] for b in lay.rows[res])
            y0 = min(b.origin[1] for b in lay.rows[res])
            x1 = max(b.origin[0] + b.size[0] for b in lay.rows[res])
            y1 = max(b.origin[1] + b.size[1] for b in lay.rows[res])
            build_order.append([x0, y0, x1, y1])
        new_ents = [e for e in lay.ents if not (e.extra or {}).get("existing")]
        need = cost.bill(new_ents, gd, fuel=False)
        cov = cost.cover(need, stock, gd)
        cov2 = cost.cover(need, _merge(stock, salvage), gd)
        need_raw = cost.raw_of(need, gd)
        per_min = {}
        for res, n in lay.units.items():
            per_min[PRODUCT.get(res, res)] = UNIT_RATE * n
        per_min["coal"] = round(per_min.get("coal", 0) - lay.burn - 2.25 * lay.units.get("coal", 0), 1)
        s2plan, s2 = None, None
        if spec.stage2 and with_s2:
            # stage 2 placed on top of stage 1 (power + labs, copper pairs when stage 1 has none); the old estimate
            # only when it can't be placed
            s2plan = plan_stage2(self, spec, lay, eta, per_min, gone, cov2.left, rm_ids)
            s2 = self.s2_summary(s2plan, lay) if s2plan is not None else self.stage2(lay, removing, cov2.left)
        took = sum(r[5] * FUEL_ITEMS.get(r[4], 0) for r in take_rows)
        bill = {"need": {k: round(v) for k, v in need.items()}, "need_raw": _round(need_raw),
                "have": {k: round(v) for k, v in cov.used.items()}, "salvage": {k: round(v) for k, v in salvage.items()},
                "short": _round(cov2.short), "craft_s": round(cov2.craft_s),
                "fuel": {"start": round(sum(u.start for u in units)), "burn_per_min": round(lay.burn, 1),
                         "first_fill": round(first_fill), "have": round(have_coal - salvage.get("coal", 0)),
                         "salvage": round(salvage.get("coal", 0)), "takes": round(took),
                         "mined": round(eta["gathered"].get("coal", 0))}}
        w = world_without(self.world, gone) if gone else self.world
        rep = validate(lay.ents, gd, w, {})
        if not rep.ok:
            raise RuntimeError("validation: " + "; ".join(f"{i.kind}: {i.msg}" for i in rep.errors[:3]))
        tiles = set()
        for e in lay.ents:
            tiles |= gd.footprint(e.name, e.x, e.y, e.direction)
        clear = list(lay.grid.to_clear(tiles))
        xs = [t[0] for t in tiles]
        ys = [t[1] for t in tiles]
        bbox = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
        counts = entity_counts(new_ents)
        k = lay.sites.get("iron-ore")
        coal_site = lay.sites.get("coal")
        far = max((cost.walk_s(coal_site, s) * cost.WALK / 0.85 for s in lay.sites.values()), default=0) \
            if coal_site else 0
        site_rows = []
        for res in RES_ORDER:
            if res in lay.sites:
                fp = _footprint(gd, lay.rows[res])
                site_rows.append({"what": UNIT_WORD[res], "n": lay.units.get(res, 0), "size": fp,
                                  "at": list(lay.sites[res]), "nest": round(min(self.nest_dist(lay.sites[res]),
                                                                                  10 ** 6))})
        coal_from = [[e.x, e.y] for r in lay.rows.get("coal", []) for e in r.placed() if "chest" in e.name]
        stats = {
            "class": "bootstrap", "stage_label": "stage 1 (burner)", "topology": self.topology["name"],
            "transport": self.topology["transport"], "per_min": per_min, "counts": counts,
            "entities": len(lay.ents), "belts": 0, "belt_tiles": 0, "longest_run": 0, "routes": [],
            "belt_fe": 0, "bbox": bbox, "footprint": _footprint(gd, lay.rows.get("iron-ore") or []),
            "sites": {r: list(v) for r, v in lay.sites.items()}, "site_rows": site_rows, "units": lay.units,
            "coal_burn": lay.burn, "bill": bill,
            "eta": {k2: eta[k2] for k2 in ("first_plates", "target", "complete", "sustained", "assumes")},
            "round": {"period_min": AG.ROUND_MIN, "tiles": round(far), "helper_share": eta["helper_share"],
                      "trips": eta["trips"]},
            "timeline": eta["timeline"][:12], "gathered": eta["gathered"], "helper_rate": self.H,
            "smelt_dist": 0 if lay.units.get("iron-ore") else None,
            "needs_research": sorted({e.name for e in lay.ents if not gd.can_build(e.name)}),
            "clear": len(clear), "manual_fuel": sum(1 for e in lay.ents if (e.extra or {}).get("fuel") == "manual"),
            "hub": list(k) if k else None, "dist": self.dist, "target": target,
            "takes": take_rows, "coal_from": coal_from, "build_order": build_order,
            "build_order_names": [names[i] for i in order],
            "refuel_existing": [r for rp in reps for r in rp.rows],
            "converted": [{"cluster": cv.cluster.id, "label": cv.cluster.label(), "gps": cv.cluster.gps(),
                           "res": cv.res, "drills": cv.n, "skipped": cv.skipped, "belts": len(cv.belts)}
                          for cv in converting],
            "repairs": [{"cluster": rp.cluster.id, "label": rp.cluster.label(), "gps": rp.cluster.gps(),
                         "reason": rp.cluster.reason} for rp in reps],
            "nest_sites": [s for s in site_rows if s["nest"] < NEST_NEAR],
            "skipped": [self.skipped[r] for r in ("copper-ore", "stone") if r in self.skipped and
                        ((r == "copper-ore" and spec.copper) or (r == "stone" and spec.stone))] +
                       [f"the helper hand-mines {nice(r).replace(' ore', '')} next to biter nests "
                        f"[gps={round(pp[0])},{round(pp[1])}]" for r in sorted(eta["gathered"])
                        for pp in [self.patch_point(r, centre)] if pp is not None and
                        self.nest_dist(pp) < NEST_AVOID],
            "patches": {r: [round(p.center[0]), round(p.center[1])] for r, p in
                        (("iron-ore", self.iron), ("copper-ore", self.copper), ("coal", self.coal),
                         ("stone", self.stone)) if p is not None},
        }
        if eta.get("fail"):
            stats["eta_fail"] = eta["fail"]
        used = {rp.cluster.id for rp in reps} | {r[6] for r in take_rows} | rm_ids
        took_ids = {r[6] for r in take_rows}
        idle = self.review_idle(used - took_ids, s2plan, took_ids)
        stats["idle_review"] = [{"label": c.label(), "gps": c.gps(), "status": c.status, "action":
                                 ("reuse" if c.id in took_ids else "keep"), "reason": why}
                                for c, why in idle][:8]
        stats["stages"] = [{"name": "burner smelting (stage 1)", "now": True}]
        if s2 is not None:
            stats["stages"].append(s2)
            if s2.get("after_stage1_min") is not None:
                stats["stage2_min"] = s2["after_stage1_min"]
            if (s2.get("eta") or {}).get("science_start") is not None:
                stats["science_min"] = s2["eta"]["science_start"]
        stats["stages"].append(self.stage3(s2))
        pid = f"{spec.variant[:3]}-boot-{uuid.uuid4().hex[:4]}"
        plan = Plan(pid, TITLES.get(spec.variant, spec.variant), spec.variant, "bootstrap", "", lay.ents, clear,
                    [], rep.to_dict(), stats, self.world.surface or "nauvis", removes=removes)
        plan.next_stage = s2plan
        if s2plan is not None:
            # stage 2's ground stays free while stage 1 is built (the helper puts its own smelting furnaces elsewhere)
            plan.stats["reserve"] = [list(gd.box(e.name, e.x, e.y, e.direction)) for e in s2plan.entities]
            # stage 2's logistics round also keeps stage 1's hand-fed burners going (a new round replaces the old
            # one in the mod), and takes coal from stage 1's coal chests
            s2plan.stats["keep_fuelled"] = [[e.name, e.x, e.y, AG.FILL.get(self._typ(e.name), 10)]
                                            for e in lay.ents if (e.extra or {}).get("fuel") == "manual"]
            s2plan.stats["coal_from"] = coal_from
            s2plan.stats["take_from"] = [[e.x, e.y] for res in ("iron-ore", "copper-ore", "stone")
                                         for r in lay.rows.get(res, []) for e in r.placed() if "chest" in e.name]
        return plan

    def stock_for(self, lay: Layout) -> dict:
        return self.state.stock(points=list(lay.sites.values()))

    # ---- removal decision (section 8.3) ---------------------------------------------------------------------
    def candidates(self, spec: Spec, keep_plan: Plan) -> list:
        """Up to MAX_CANDIDATES clusters worth testing: not working, never kept-only, whose PARTS (not their fuel or
        contents: those can be taken without removing anything) cover part of what this variant (and its next
        stage) is short of; most useful per helper minute first."""
        gd = self.gd
        need = dict(keep_plan.stats["bill"]["need"])
        stock = self.state.stock(points=[tuple(v) for v in (keep_plan.stats.get("sites") or {}).values()])
        base = cost.fe_equiv(cost.cover(need, stock, gd).short)
        scored = []
        for c in self.clusters + self.groups:
            # labs are never taken apart for stage 1: stage 2 powers them where they stand (round 3: a removal must
            # have a destination in the plan it belongs to)
            if c.keep_only or c.kind in ("storage", "labs") or c.status == "working":
                continue
            if any(e.type == "lab" for e in c.ents):
                continue
            useful = base - cost.fe_equiv(cost.cover(need, _merge(stock, c.items(gd, contents=False)), gd).short)
            c.useful = round(useful, 1)
            if useful > 0.5:
                scored.append((useful / (1 + c.removal_s() / 60), c))
        scored.sort(key=lambda t: -t[0])
        return [c for _u, c in scored[:MAX_CANDIDATES]]

    def gscore(self, p: Plan, s2=True) -> float:
        """score() with the plan's quality gates (as the cards show it). The removal bookkeeping gates (a reason is
        attached only once the removal is decided; the caps are checked by decide itself) don't count here."""
        n_base = sum(len(c.machines) for c in self.clusters)
        b_base = sum(c.belt_tiles for c in self.clusters)
        iss = [i for i in quality.assess(p, self.state, base_entities=n_base, base_belts=b_base,
                                         rebuild_words=self.rebuild_words)
               if i.code not in ("REMOVAL_UNJUSTIFIED", "REMOVES_TOO_MUCH")]
        return score(p, iss, s2=s2)

    def decide(self, spec: Spec, keep_plan: Plan):
        """Greedy keep / reuse / replace decision: -> (rebuild Plan or None, [review rows]). Each candidate cluster
        is tried as a removal (its parts feed the new rows) and, when it is an idle drill line on ore, as a
        conversion where it stands (only its belt removed); the better of the two counts."""
        base = self.gscore(keep_plan, s2=False)
        cur_plan, cur_score, chosen, conv, review = keep_plan, base, [], [], []
        convs = self.conversions()
        cands = self.candidates(spec, keep_plan)
        for cid, cv in convs.items():   # convertible lines are always tried (their parts may not be "useful")
            if all(c.id != cid for c in cands):
                cands.append(cv.cluster)
        thr = REMOVE_GAIN_ASKED if self.tidy_words else max(REMOVE_GAIN_MIN, REMOVE_GAIN_FRAC * base)
        n_base = sum(len(c.machines) for c in self.clusters)
        b_base = sum(c.belt_tiles for c in self.clusters)

        def over_cap(cs, cvs) -> bool:  # section 8.3: at most half of the base's machines and of its belt tiles
            if self.rebuild_words:
                return False
            belts = sum(c.belt_tiles for c in cs) + sum(len(cv.belts) for cv in cvs)
            return sum(len(c.machines) for c in cs) > 0.5 * max(1, n_base) or (b_base and belts > 0.5 * b_base)
        t0 = time.monotonic()
        alt = {}    # cluster id -> {action: gain} of the last round it was tried in
        while cands and time.monotonic() - t0 < self.time_budget:
            best = None
            taken = {m for c0 in chosen for m in c0.member_ids} | {cv.cluster.id for cv in conv}
            for c in cands:
                if taken & set(c.member_ids):
                    continue
                opts = [("replace", c, None)]
                if c.id in convs:
                    opts.insert(0, ("reuse", c, convs[c.id]))
                for kind, _c, cv in opts:
                    rm = chosen + ([c] if kind == "replace" else [])
                    cvs = conv + ([cv] if kind == "reuse" else [])
                    if over_cap(rm, cvs):
                        review.append({"cluster": c.id, "label": c.label(), "gps": c.gps(), "status": c.status,
                                       "gain_min": None, "action": kind,
                                       "verdict": "keep: would remove more than half of your base"})
                        continue
                    try:
                        p = self.build(spec, rm, with_s2=False, converting=cvs)
                    except Exception as e:  # noqa: BLE001 - a candidate that can't be planned is skipped
                        review.append({"cluster": c.id, "label": c.label(), "gain_min": None, "action": kind,
                                       "verdict": f"skipped: {e}"})
                        continue
                    g = round(cur_score - self.gscore(p, s2=False), 1)
                    alt.setdefault(c.id, {})[kind] = g
                    need = thr * (2 if c.status == "working" else 1)
                    review.append({"cluster": c.id, "label": c.label(), "gps": c.gps(), "status": c.status,
                                   "gain_min": g, "needed_min": round(need, 1), "action": kind,
                                   "verdict": kind if g >= need else "keep"})
                    # a conversion keeps the player's drills: preferred when it is about as good (within 0.5 min)
                    key = g + (0.5 if kind == "reuse" else 0.0)
                    if g >= need and (best is None or key > best[0]):
                        best = (key, g, kind, c, cv, p)
            if best is None:
                break
            _key, g, kind, c, cv, p = best
            c.gain_min = g
            if kind == "reuse":
                c.action = "reuse"
                c.reason = self._why_convert(cv, keep_plan, p)
                conv.append(cv)
            else:
                c.action = "replace"
                c.reason = self._why(c, keep_plan, p, g)
                g_re = (alt.get(c.id) or {}).get("reuse")
                if g_re is not None:   # the cheaper-looking repair was checked too: say why it lost
                    c.reason += (f". Turning them into pairs where they stand (only the belt removed) was checked "
                                 f"too: ~{fmt_n(max(0.0, g_re))} min gain against ~{fmt_n(g)} min, because their "
                                 "parts are worth more in the new rows")
                chosen.append(c)
            cands.remove(c)
            cur_plan, cur_score = p, self.gscore(p, s2=False)
        if not chosen and not conv:
            return None, review
        if spec.stage2:   # the chosen version again, now with its stage 2 placed
            cur_plan = self.build(spec, chosen, converting=conv)
        for c in chosen:  # members of a chosen group are replaced with it
            for m in self.clusters:
                if m.id in c.member_ids and m is not c:
                    m.action, m.reason, m.gain_min = "replace", c.reason, c.gain_min
        return cur_plan, review

    def _why_convert(self, cv: Convert, keep, p) -> str:
        """One-line reason for turning a drill line into pairs where it stands."""
        c = cv.cluster
        word = nice(cv.res).replace(" ore", "")
        if cv.res.endswith("-ore"):
            how = ("a stone furnace, a burner inserter and a chest go in front of each drill, so the same drills make "
                   f"{fmt_n(UNIT_RATE * cv.n)} {word} plates/min where they stand")
        else:
            how = f"a chest goes in front of each drill, so the same drills fill chests with {word} where they stand"
        left = f" ({cv.skipped} drills with too little ore left are left as they are)" if cv.skipped else ""
        return (f"your {cv.n} burner drills on the {word} patch {c.gps()} have no fuel and drop onto a "
                f"{len(cv.belts)}-tile belt that nothing smelts from: only the belt is taken up and {how}{left}; "
                f"no new drills are needed: {self._gain_txt(keep, p)}")

    def _why(self, c, keep, p, gain) -> str:
        """One-line reason for replacing cluster c: where it is, why it does nothing, what its parts give."""
        d = existing.dist_to_patch(c, self.iron)
        what = {"furnace_row": "stone furnaces", "drill_line": "burner drills", "labs": "labs"}.get(
            c.kind, c.kind.replace("_", " "))
        st = {"idle_no_fuel": "no fuel", "idle_no_input": "nothing to smelt", "idle_no_power": "no power",
              "output_full": "output blocked", "disconnected": "not connected"}.get(c.status, c.status.replace("_", " "))
        need = dict(keep.stats["bill"]["need"])
        items = c.items(self.gd, contents=False)
        useful = sorted(((k, v) for k, v in items.items() if k in need), key=lambda kv: -kv[1]) or \
            sorted(items.items(), key=lambda kv: -kv[1])
        give = ", ".join(_pl(v, k) for k, v in useful[:3]) or "their parts"
        where = f"{round(d)} tiles from the iron ore" if d < 10 ** 6 else "far from the iron ore"
        n = len(c.machines) or len(c.ents)
        if c.kind == "drill_line":
            drills = [e for e in c.machines if e.type == "mining-drill"]
            ores = {(self.world.resource_at(e.x, e.y) or (None,))[0] for e in drills} - {None}
            on = f"on the {nice(sorted(ores)[0]).replace(' ore', '')} patch " if len(ores) == 1 else ""
            out = "" if any(_drops_into(e, c, self.gd) for e in drills) else \
                " and drop onto a belt instead of into furnaces, so refuelling them alone makes no plates"
            return (f"{n} {what} {on}{c.gps()} have {st}{out}; picking them up gives {give} for the new rows "
                    f"instead of crafting them: {self._gain_txt(keep, p)}")
        return (f"{n} {what} {c.gps()} stand {where} with {st}; picking them up gives {give} for the new rows "
                f"instead of crafting them from scratch: {self._gain_txt(keep, p)}")

    def _gain_txt(self, keep, p) -> str:
        """What a removal changes, in the player's terms: first plates / full rate sooner, gates it avoids."""
        a, b = keep.stats.get("eta") or {}, p.stats.get("eta") or {}
        out = []
        for k, word in (("first_plates", "first plates"), ("target", "full rate")):
            if a.get(k) is not None and b.get(k) is not None and abs(a[k] - b[k]) >= 0.1:
                out.append(f"{word} {fmt_n(abs(a[k] - b[k]))} min {'sooner' if b[k] < a[k] else 'later'}")
        ga = {(i.code, i.text) for i in quality.assess(keep, self.state)}
        gb = {(i.code, i.text) for i in quality.assess(p, self.state)}
        for code, text in sorted(ga - gb):
            if code in ("NESTS_NEAR", "HELPER_BUSY", "FUEL_SHORT", "SLOW_START", "BILL_UNREACHABLE"):
                out.append("avoids: " + text.split(" [gps")[0])
        return ", ".join(out) or "a slightly better plan overall"

    # ---- long-term (v1 hub engine) ----------------------------------------------------------------------------
    def longterm(self) -> Plan | None:
        from . import placer
        cap = quality.BELT_CAPS["longterm"][1]
        far = {r: d for r, d in self.dist.items() if r in ("coal", "copper-ore") and d is not None and d > cap}
        if far:   # a central hub needs ore belts longer than the hard cap here: not even tried (it takes 30 s)
            r, d = max(far.items(), key=lambda kv: kv[1])
            self.hub_skip = {"belt_tiles": None, "longest_run": None, "hub": None, "bill_raw": {},
                             "why_not": [f"the {nice(r).replace(' ore', '')} patch is {d} tiles from the iron, so its "
                                         f"ore belt would be longer than {cap} tiles"]}
            return None
        goal = dict(self.goal)
        goal["near"] = tuple(self.iron_target)
        goal.pop("stage", None)
        try:
            p = placer.Planner(self.world, self.gd, goal, "expandable", time_budget=min(30.0, self.time_budget),
                               want_sites=2, keep=2).run()
        except Exception as e:  # noqa: BLE001
            self.reasons.append(f"long-term hub: {e}")
            return None
        p.variant = "longterm"
        p.title = TITLES["longterm"]
        st = p.stats
        st["class"] = "longterm"
        st["stage_label"] = "later stage (hub)"
        st["topology"] = "CENTRAL"
        st["belt_tiles"] = sum(1 for e in p.entities if "belt" in e.name)
        if not st.get("routes"):
            runs = belt_runs(p.entities)
            st["routes"] = [{"what": "belt", "tiles": n} for n in sorted(runs, reverse=True)[:8]]
        st["longest_run"] = max((r["tiles"] for r in st["routes"]), default=0)
        st["belt_fe"] = round(cost.C_BELT * st["belt_tiles"])
        hub = st.get("hub")
        if hub:
            st["smelt_dist"] = round(dist_point_patch(tuple(hub), self.iron)[1])
        stock = self.state.stock(points=[tuple(hub)] if hub else [])
        need = cost.bill(p.entities, self.gd)
        cov = cost.cover(need, stock, self.gd)
        st["bill"] = {"need": {k: round(v) for k, v in need.items()}, "need_raw": _round(cost.raw_of(need, self.gd)),
                      "have": {k: round(v) for k, v in cov.used.items()}, "salvage": {}, "short": _round(cov.short),
                      "craft_s": round(cov.craft_s)}
        step = cost.Step("hub", need, tuple(hub) if hub else None, len(p.entities),
                         {"iron-plate": st["per_min"].get("iron-plate", 0)}, {"coal": st.get("coal_per_min", 0)})
        eta = cost.simulate([step], stock, self.gd, H=self.H, start=self.state.pos,
                            target={"iron-plate": st["per_min"].get("iron-plate", 0)})
        st["eta"] = {k: eta[k] for k in ("first_plates", "target", "complete", "assumes")}
        return p

    def grow_longterm(self, hub: Plan | None) -> Plan | None:
        """The long-term variant when the central hub is not a sensible build here (round 3): never a hub with
        150-400 tile ore belts, not even as a 'later stage'. Instead twice the on-site smelting (8 iron + 4 copper
        pairs, 2 stone drills) in rows that leave room to extend, stage 2 placed on top, and stage 3 = electric
        drills and furnace columns at each patch. The hub that was checked is reported (why it is not offered)."""
        try:
            p = self.build(self.spec("longterm"))
        except Exception as e:  # noqa: BLE001
            self.reasons.append(f"longterm (grow on site): {e}")
            return None
        p.title = TITLE_GROW
        p.stats["grow"] = True
        if hub is None and getattr(self, "hub_skip", None):
            p.stats["hub_check"] = dict(self.hub_skip)
        if hub is not None:
            hs = hub.stats
            iss = quality.assess(hub, self.state)
            p.stats["hub_check"] = {"belt_tiles": hs.get("belt_tiles", 0), "longest_run": hs.get("longest_run", 0),
                                    "hub": hs.get("hub"), "bill_raw": hs["bill"].get("need_raw") or {},
                                    "why_not": [i.text for i in iss if i.level == "hard"][:3] or
                                    ["its ore belts are longer than on-site smelting needs"]}
        return p

    # ---- variants ----------------------------------------------------------------------------------------------
    def spec(self, variant) -> Spec:
        s = self.scale
        if variant == "quick":
            return Spec("quick", iron=max(1, round(4 * s)), target={"iron-plate": 60 * s}, stage2=True)
        if variant == "balanced":
            return Spec("balanced", iron=max(1, round(4 * s)), copper=max(1, round(2 * s)), stone=1,
                        target={"iron-plate": 60 * s, "copper-plate": 30 * s}, stage2=True)
        if variant == "longterm":   # "grow on site": twice the smelting, rows with room to extend, then stage 2
            return Spec("longterm", iron=max(2, round(8 * s)), copper=max(1, round(4 * s)), stone=2,
                        target={"iron-plate": 120 * s, "copper-plate": 60 * s}, stage2=True)
        raise KeyError(variant)

    def finish(self, p: Plan, *, recommended=False) -> Plan:
        """Gates, score, summary card, removal summary."""
        n_base = sum(len(c.machines) for c in self.clusters)
        b_base = sum(c.belt_tiles for c in self.clusters)
        ids = {r[4] for r in p.removes}
        p.stats["clusters"] = [c.to_dict(self.gd) for c in self.clusters + self.groups
                               if c.id in ids or (c.id < existing.GROUP_BASE and (c.action != "keep" or
                                                                                 c.status != "passive"))][:14]
        iss = quality.assess(p, self.state, base_entities=n_base, base_belts=b_base,
                             rebuild_words=self.rebuild_words)
        st = p.stats
        if p.variant != "longterm" or st.get("grow"):
            used = {r["cluster"] for r in st.get("repairs") or []}
            ign = [c.label() + " " + c.gps() for c in self.clusters
                   if c.status == "idle_no_fuel" and c.id not in used and c.id not in ids and _cheap_fix(c, self)]
            if ign:
                iss += quality.assess(Plan("x", "", "", "", "", [], stats={"idle_fixable_ignored": ign[:2]}))
        st["gates"] = [i.to_dict() for i in iss]
        st["build_now"] = not quality.hard(iss)
        st["score"] = score(p, iss, self.clusters)
        st["helper_rate"] = self.H
        rm = [c for c in self.clusters + self.groups if c.id in ids]
        cnt = {}
        for r in p.removes:
            cnt[r[0]] = cnt.get(r[0], 0) + 1
        conv = {r["cluster"]: r for r in st.get("converted") or []}

        def rm_label(c):
            if c.id in conv:   # reuse: only the belt goes, the drills stay and become pairs
                return f"the {conv[c.id]['belts']} belts in front of your {conv[c.id]['drills']} burner drills"
            return c.label()
        st["removes_summary"] = {"count_by_name": cnt,
                                 "clusters": [{"label": rm_label(c), "gps": c.gps(), "reason": c.reason,
                                               "action": "reuse" if c.id in conv else "replace",
                                               "gain_min": c.gain_min} for c in rm]}
        st["recommended"] = recommended
        st["tradeoff"] = TRADEOFF["grow"] if st.get("grow") else (TRADEOFF.get(p.variant) or st.get("tradeoff") or "")
        nxt = getattr(p, "next_stage", None)
        if nxt is not None:   # the placed stage 2: its own id, gates and card (shown when the player asks for it)
            nxt.id = f"{p.id}-s2"
            nxt.title = f"{p.title}: stage 2 (power + research)"
            nxt.stats["parent"] = p.id
            s2i = quality.assess(nxt, self.state)
            nxt.stats["gates"] = [i.to_dict() for i in s2i]
            nxt.stats["build_now"] = False      # only once stage 1 runs (the player asks; re-validated then)
            for row in st.get("stages") or []:
                if row.get("placed"):
                    row["id"] = nxt.id
                    row["gates"] = nxt.stats["gates"]
            nxt.summary = card_stage2(nxt, p)
        p.summary = card(p, self)
        p.warnings = [i.text for i in iss if i.level == "hard"] + [i.text for i in iss if i.level == "soft"] + \
            [w for w in p.warnings if w]
        p.warnings = p.warnings[:6]
        return p

    def stage2_items(self, k, removing=()) -> tuple:
        """Items of stage 2 (steam power at the water nearest the iron site, labs there, poles, red science for
        automation + electric mining drills) -> (items, info). Labs of the base are reused where they stand unless
        `removing` takes them (then they come back as items and are rebuilt next to the iron site)."""
        gd = self.gd
        gone = {m for c in removing for m in c.member_ids}
        nw = self.world.nearest_water_tile(*k) if k else None
        d_water = round(math.hypot(nw[0][0] - k[0], nw[0][1] - k[1])) if nw else 30
        labs = [c for c in self.clusters if c.kind == "labs" and c.id not in gone]
        labs_have = sum(c.counts.get("lab", 0) for c in labs)
        lab_d = round(min(math.hypot(c.center[0] - k[0], c.center[1] - k[1]) for c in labs)) if labs and k else 0
        poles = math.ceil((d_water + lab_d) / 6.5) + 4
        packs = 0
        for t in ("automation", "electric-mining-drill"):
            tech = gd.raw["technologies"].get(t) or {}
            if not tech.get("researched"):
                packs += int(tech.get("unit_count") or 0)
        items = {"offshore-pump": 1, "boiler": 1, "steam-engine": 2, "small-electric-pole": poles, "pipe": 4,
                 "lab": max(0, 2 - labs_have), "automation-science-pack": packs, "coal": cost.BOILER_FUEL}
        items = {cost.item_of(gd, n) if n in gd.raw["entities"] else n: v for n, v in items.items() if v}
        return items, {"water_tiles": d_water, "labs_reused": labs_have, "lab_distance": lab_d, "poles": poles,
                       "packs": packs}

    def stage2(self, lay: Layout, removing=(), left=None) -> dict:
        """Estimate of stage 2 (power + research), not placed now. Its bill is covered from what stage 1 leaves
        (stock + salvage), then from stage 1's own output. -> {name, now: False, bill_raw, short, after_stage1_min}"""
        gd = self.gd
        if not all(B.buildable(gd, n) for n in ("offshore-pump", "boiler", "steam-engine", "small-electric-pole",
                                                "lab")):
            missing = "steam power" if not B.buildable(gd, "boiler") else "labs"
            return {"name": "power + research", "now": False, "after": f"research {missing} (crafting triggers)"}
        k = lay.sites.get("iron-ore")
        items, info = self.stage2_items(k, removing)
        c = cost.cover(items, left or {}, gd)
        rate = {"iron-ore": UNIT_RATE * lay.units.get("iron-ore", 0),
                "copper-ore": UNIT_RATE * lay.units.get("copper-ore", 0),
                "stone": UNIT_RATE * lay.units.get("stone", 0),
                "coal": max(1.0, 12.75 * lay.units.get("coal", 0) - lay.burn)}
        mins = 0.0
        for raw, v in c.short.items():
            r = rate.get(raw, 0) + self.H * 0.5
            mins = max(mins, v / max(1.0, r))
        walk = 2 * (info["water_tiles"] + info["lab_distance"]) * 0.85 / cost.WALK
        build = (c.craft_s + cost.PLACE_S * (info["poles"] + 6) + walk * 3) / 60   # ~3 trips carrying packs
        research = info["packs"] * 10 / max(1, max(info["labs_reused"], 2)) / 60
        return dict(name="power + research", now=False, bill_raw=_round(cost.raw_of(items, gd)),
                    short=_round(c.short), after_stage1_min=round(mins + build + research, 1), **info)

    def s2_summary(self, s2: Plan, lay: Layout) -> dict:
        """The stages[] row of a placed stage 2 (what the card and the brief show)."""
        st = s2.stats
        eta = st.get("eta") or {}
        labs = st.get("labs") or {}
        parts = []
        if st.get("copper_pairs"):
            cs = next((s for s in st.get("sites") or [] if s["what"] == "copper pairs"), None)
            parts.append(f"{st['copper_pairs']} copper pairs" + (f" {_gps(cs['at'])}" if cs else ""))
        if st.get("plant"):
            eng = (st.get("counts") or {}).get("steam-engine", 0)
            parts.append(f"a hand-fed boiler + {eng} steam engine{'s' if eng != 1 else ''} at the water "
                         f"{_gps(st['plant'])}")
        if labs.get("reused"):
            parts.append(f"{st.get('poles', 0)} small poles to your {labs['reused']} idle labs "
                         f"{_gps(labs['at'])} (powered where they stand)")
        elif labs.get("new"):
            parts.append(f"{labs['new']} new labs next to the iron pairs {_gps(labs['at'])} and "
                         f"{st.get('poles', 0)} small poles")
        return {"name": "power + research", "now": False, "placed": True, "id": s2.id, "what": parts,
                "bill_raw": st["bill"].get("need_raw") or {}, "short": st["bill"].get("short") or {},
                "eta": {k: v for k, v in eta.items() if k != "assumes"}, "assumes": eta.get("assumes"),
                "research": st.get("research") or [], "research_packs": st.get("research_packs", 0),
                "unlocks": st.get("research_steps") or [], "power_kw": st.get("power_kw"),
                "after_stage1_min": (round(eta["power_on"] - eta["start"], 1)
                                     if eta.get("power_on") is not None and eta.get("start") is not None else None)}

    def stage3(self, s2=None) -> dict:
        """Stage 3 (electric drills -> short belt -> furnace column at each patch, red science assemblers): the
        research it waits for and, after a placed stage 2, when that research is done; it is planned from the then
        current state when the player asks."""
        gd = self.gd
        need = [t for t in ("electric-mining-drill", "automation") if
                not (gd.raw["technologies"].get(t) or {}).get("researched")]
        after = ("research " + " and ".join(nice(t) for t in need)) if need else "stage 2 has power"
        out = {"name": "electric drills and furnace columns at each patch, red science assemblers", "now": False,
               "after": after}
        eta = (s2 or {}).get("eta") or {}
        if eta.get("science_start") is not None:
            out["ready_min"] = max([eta.get(t) or eta["science_start"] for t in need] or [eta["science_start"]])
        return out

    def options(self, n=3, keys=None) -> list[Plan]:
        t_end = time.monotonic() + self.time_budget * 2
        plans: dict = {}
        for v in ("quick", "balanced"):
            try:
                plans[v] = self.build(self.spec(v))
            except Exception as e:  # noqa: BLE001
                self.reasons.append(f"{v}: {e}")
        if not plans:
            raise RuntimeError("no on-site variant could be planned: " + "; ".join(self.reasons[-3:]))
        rebuild = None
        review = []
        if self.allow_removal and self.clusters:
            base_key = "balanced" if "balanced" in plans else "quick"
            best = None
            for v in (base_key, "quick"):
                if v not in plans or time.monotonic() > t_end:
                    continue
                for c in self.clusters + self.groups:
                    if c.action == "replace":
                        c.action, c.reason, c.gain_min = "keep", "", None
                rp, rv = self.decide(self.spec(v), plans[v])
                review += rv
                if rp is not None and (best is None or self.gscore(rp) < self.gscore(best)):
                    best = rp
                if rp is not None:
                    break
            if best is not None:
                rebuild = best
                base = rebuild.variant
                rebuild.stats["base_variant"] = base
                rebuild.variant = "rebuild"
                rebuild.title = f"{TITLES['rebuild']} ({TITLES[base].lower()})"
                rebuild.id = "reb-" + rebuild.id.split("-", 1)[1]
                rebuild.stats["net_gain"] = net_gain(plans[base], rebuild, self.gscore)
        self.rebuild_plan = rebuild
        longterm = None
        want = keys or self._wanted(n, rebuild is not None)
        if "longterm" in want:
            # the central hub only when it is a sensible build here (short belts, smelting by the iron, a first stage
            # the player can afford); otherwise "grow on site" - never a hub with 150-400 tile ore belts
            hub = self.longterm() if time.monotonic() < t_end else None
            if hub is not None and not quality.hard(quality.assess(hub, self.state)):
                longterm = hub
            else:
                longterm = self.grow_longterm(hub)
        out = []
        for key in want:
            p = {"quick": plans.get("quick"), "balanced": plans.get("balanced"), "rebuild": rebuild,
                 "longterm": longterm}.get(key)
            if p is not None:
                out.append(p)
        seen, rows = set(), []
        for r in sorted((r for r in review if r.get("gain_min") is not None), key=lambda r: -r["gain_min"]):
            if (r["cluster"], r.get("action")) not in seen:   # a cluster tried as a removal and as a conversion
                seen.add((r["cluster"], r.get("action")))
                rows.append(r)
        seen = {k[0] for k in seen}
        for p in out:
            p.stats["review"] = rows[:10]
            p.stats["reviewed"] = len(seen)
            self.finish(p)
        # recommended: lowest score among build-now plans (long-term when the goal asks for it)
        now = [p for p in out if p.stats["build_now"]] or out
        rec = min(now, key=lambda p: p.stats["score"])
        if self.longterm_words and longterm is not None and longterm.stats["build_now"]:
            rec = longterm
        rec.stats["recommended"] = True
        for p in out:   # every card again: they point at each other (the Rebuild card, the recommended one)
            p.summary = card(p, self)
        out.sort(key=lambda p: (not p.stats["recommended"], not p.stats["build_now"], p.stats["score"]))
        return out

    def _wanted(self, n, have_rebuild) -> list:
        n = max(1, min(4, int(n or 3)))
        order = ["quick", "balanced", "rebuild" if have_rebuild else "longterm"]
        order.append("longterm" if have_rebuild else "rebuild")
        if self.longterm_words and "longterm" not in order[:n]:
            order.insert(n - 1, "longterm")
        return [k for k in order if k != "rebuild" or have_rebuild][:n]


# ---- scoring / summaries ------------------------------------------------------------------------------------------
def score(p: Plan, issues=None, clusters=None, s2=True) -> float:
    """Section 9.3 (lower is better, minutes). Round 3: plus half the time until red science research can start
    (stage 2 placed: the path to automation science counts, not only the first plates); s2=False leaves the stage-2
    term out (the removal decision compares stage 1 with and without a removal)."""
    st = p.stats
    eta = st.get("eta") or {}
    first = eta.get("first_plates")
    tgt = eta.get("target")
    s = (NONE_MIN if first is None else first) + (NONE_MIN if tgt is None else tgt)
    s += 0.02 * (st.get("belt_tiles") or 0)
    if s2 and st.get("science_min") is not None:     # placed stage 2: time until red science research starts
        s += 0.5 * st["science_min"]
    elif s2 and st.get("stage2_min") is not None:    # stage 2 only estimated: minutes after stage 1
        s += 0.5 * st["stage2_min"]
    if issues is None:
        issues = [quality.Issue(g["code"], g["level"], g["text"]) for g in st.get("gates") or []]
    soft, hard = quality.counts(issues)
    s += 5 * soft + 10 * hard
    if clusters:
        working = [c for c in clusters if c.status == "working"]
        if working:
            kept = [c for c in working if c.action != "replace"]
            s += 3 * (1 - len(kept) / len(working))
    rm = p.removes or []
    s += 0.05 * sum(1 for r in rm if "belt" not in r[0]) + 0.01 * sum(1 for r in rm if "belt" in r[0])
    return round(s, 1)


def net_gain(keep: Plan, p: Plan, gscore=None) -> dict:
    a, b = keep.stats["eta"], p.stats["eta"]

    def d(k):
        if a.get(k) is None or b.get(k) is None:
            return None
        return round(a[k] - b[k], 1)
    fe_k = cost.fe_equiv(keep.stats["bill"]["short"])
    fe_p = cost.fe_equiv(p.stats["bill"]["short"])
    sk = gscore(keep) if gscore else score(keep)
    sp = gscore(p) if gscore else score(p)
    return {"eta_first_plates_min": d("first_plates"), "eta_target_min": d("target"),
            "belt_tiles": (keep.stats.get("belt_tiles") or 0) - (p.stats.get("belt_tiles") or 0),
            "fe": round(fe_k - fe_p, 1), "score_min": round(sk - sp, 1)}


def _round(d):
    return {k: round(v) if v >= 1 else round(v, 2) for k, v in d.items() if v > 1e-6}


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        cost._add(out, k, v)
    return out


def _footprint(gd, rows):
    if not rows:
        return [0, 0]
    x0 = min(r.origin[0] for r in rows)
    y0 = min(r.origin[1] for r in rows)
    x1 = max(r.origin[0] + r.size[0] for r in rows)
    y1 = max(r.origin[1] + r.size[1] for r in rows)
    return [x1 - x0, y1 - y0]


def lay_far(sites: dict, coal_patch, limit) -> bool:
    """Any placed site farther than `limit` tiles from the coal patch."""
    return any(dist_point_patch(tuple(p), coal_patch)[1] > limit for p in sites.values())


def _drop_target(drill, cluster, gd):
    """The furnace/chest of the cluster a drill drops into, or None."""
    try:
        from .gamedata import rotate
        vx, vy = rotate(*gd.entity(drill.name)["vector_to_place_result"], drill.dir)
    except (KeyError, TypeError):
        return None
    px, py = math.floor(drill.x + vx), math.floor(drill.y + vy)
    for e in cluster.ents:
        if e.type in ("furnace", "container") and e.box[0] <= px < e.box[2] and e.box[1] <= py < e.box[3]:
            return e
    return None


def _drops_into(drill, cluster, gd) -> bool:
    """A drill of the cluster drops into a furnace/chest of the same cluster."""
    return _drop_target(drill, cluster, gd) is not None


def _cheap_fix(c, pl=None) -> bool:
    """Refuelling alone gets it producing: furnaces holding ore, or drills on ore dropping into a furnace/chest."""
    if any(e.type == "furnace" and any(k.endswith("-ore") for k in e.items) for e in c.machines):
        return True
    if pl is not None:
        return any(e.type == "mining-drill" and pl.world.resource_at(e.x, e.y) and _drops_into(e, c, pl.gd)
                   for e in c.machines)
    return False


def belt_runs(ents) -> list:
    """Sizes of 4-connected belt groups (a stand-in for single runs)."""
    tiles = {(math.floor(e.x), math.floor(e.y)) for e in ents if "belt" in e.name or "splitter" in e.name}
    seen, out = set(), []
    for t in tiles:
        if t in seen:
            continue
        stack, n = [t], 0
        seen.add(t)
        while stack:
            x, y = stack.pop()
            n += 1
            for u in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if u in tiles and u not in seen:
                    seen.add(u)
                    stack.append(u)
        out.append(n)
    return out


def _raw_txt(d, keys=("iron-ore", "copper-ore", "stone", "coal", "wood")) -> str:
    names = {"iron-ore": "iron", "copper-ore": "copper", "stone": "stone", "coal": "coal", "wood": "wood"}
    parts = [f"{fmt_n(d[k])} {names[k]}" for k in keys if d.get(k, 0) >= 0.5]
    return ", ".join(parts) or "nothing"


def _gps(p) -> str:
    return f"[gps={round(p[0])},{round(p[1])}]"


def card(p: Plan, pl: PlannerV2) -> str:
    """Plans tab card (<= 8 lines, plain text; one sentence or two per line). Round 3: the coal line says what keeps
    the burners going (and that it needs the helper), the removal line lists every removal with its reason, the
    stage-2 line gives what is placed, its bill and when red science research can start."""
    st = p.stats
    eta = st.get("eta") or {}
    title = p.title
    pitch = PITCH["grow"] if st.get("grow") else PITCH.get(p.variant, "")
    first = eta.get("first_plates")
    lines = []
    pm = st.get("per_min") or {}
    iron = pm.get("iron-plate", 0)
    copper = pm.get("copper-plate", 0)
    first_txt = f"First plates in ~{fmt_n(max(1, first))} min" if first is not None else "First plates: over 2 hours"
    tgt = eta.get("target")
    rate = f"{fmt_n(iron)} iron" + (f" + {fmt_n(copper)} copper" if copper else "") + "/min"
    tail = f", {rate} after ~{fmt_n(max(1, tgt))} min" if tgt is not None and iron else ""
    rec = " (recommended)" if st.get("recommended") else ""
    later = "" if st.get("build_now", True) else " Not a good first step now."
    lines.append(f"{title}{rec} - {pitch}. {first_txt}{tail}.{later}")
    # bill
    b = st.get("bill") or {}
    need = b.get("need_raw") or {}
    short = b.get("short") or {}
    gathered = st.get("gathered") or {}
    mined = {k: v for k, v in gathered.items() if k != "coal" and v >= 1}
    if not short:
        have_txt = "you have all of it"
    elif mined:
        have_txt = f"short {_raw_txt(short)}: the helper gathers it first (counted in the times)"
    else:
        have_txt = f"short {_raw_txt(short)}"
    sal = b.get("salvage") or {}
    sal_txt = " counting the parts it picks up" if sal else ""
    st_ = pl.state
    if st_.player_inv and not st_.player_usable:   # the helper only takes from a connected player (agent.lua)
        sal_txt += " (your own inventory is not counted: you are offline or the helper may not use it)"
    lines.append(f"Needs {_raw_txt(need)}; {have_txt}{sal_txt}.")
    # coal: what starts the burners, what keeps them going, and that it needs the helper
    fu = b.get("fuel") or {}
    rd = st.get("round") or {}
    units = st.get("units") or {}
    if fu.get("start") or fu.get("first_fill"):
        src = []
        if fu.get("have"):
            src.append(f"{fmt_n(fu['have'])} you carry")
        if fu.get("takes"):
            src.append(f"{fmt_n(fu['takes'])} out of your idle machines")
        if fu.get("salvage"):
            src.append(f"{fmt_n(fu['salvage'])} from what it takes apart")
        if fu.get("mined"):
            src.append(f"~{fmt_n(fu['mined'])} mined by hand")
        burn = fu.get("burn_per_min") or st.get("coal_burn") or 0
        txt = (f"Coal: {fmt_n(fu.get('start') or fu['first_fill'])} to start the burners, then they burn "
               f"~{fmt_n(burn)}/min (coal at hand: {', '.join(src) or 'none, mined by hand'}).")
        if units.get("coal"):
            share = rd.get("helper_share") or 0
            txt += (f" {units['coal']} coal drills fill chests at ~{fmt_n(15 * units['coal'] - 2.25 * units['coal'])}"
                    f"/min; the helper carries the coal to the burners every {fmt_n(rd.get('period_min', 2.5))} min "
                    f"(sites up to {fmt_n(rd.get('tiles', 0))} tiles apart, ~{round(100 * share)}% of its time). "
                    "Without the helper the burners run dry in ~20 min.")
        lines.append(txt)
    # sites
    sites = st.get("site_rows") or []
    clear = len(p.deconstruct or [])
    clear_txt = f" Clears {clear} trees/rocks in the way." if clear else ""
    if sites:
        parts = [f"{s['n']} {s['what']} {s['size'][0]}x{s['size'][1]} {_gps(s['at'])}" for s in sites]
        lines.append(f"Sites: {'; '.join(parts)}. Belts {st.get('belt_tiles', 0)} tiles. "
                     f"{'No power needed' if not st.get('power_kw') else 'Power ' + fmt_n(st['power_kw']) + ' kW'}."
                     f"{clear_txt}")
    else:
        belts = st.get("belt_tiles", 0)
        fp = st.get("footprint") or [0, 0]
        k = st.get("hub")
        where = f" around {_gps(k)}" if k else ""
        lines.append(f"Belts {belts} tiles (longest {st.get('longest_run', 0)}). Footprint {fp[0]} by {fp[1]}{where}."
                     f"{clear_txt}")
    # S0 + removals
    rm = st.get("removes_summary") or {}
    takes = st.get("takes") or []
    s0 = ""
    if takes:
        n_ent = len({(r[1], r[2]) for r in takes})
        coal = sum(r[5] for r in takes if r[4] == "coal")
        first_c = next((c for c in pl.clusters if c.id == takes[0][6]), None)
        s0 = (f"; takes {fmt_n(coal)} coal out of {n_ent} idle machines of yours"
              f"{' ' + first_c.gps() if first_c else ''} (they have nothing to do)")
    if st.get("repairs"):
        r0 = st["repairs"][0]
        s0 += f"; refuels your {r0['label']} {r0['gps']} so it runs again"
    if not p.removes:
        reb = pl.rebuild_plan
        if reb is not None and p.variant != "rebuild":
            rcs = (reb.stats.get("removes_summary") or {}).get("clusters") or []
            what = "; ".join(f"{c['label']} {c['gps']}" for c in rcs) or "some old builds"
            rf = (reb.stats.get("eta") or {}).get("first_plates")
            cmp_txt = (f" (its first plates ~{fmt_n(max(1, rf))} min, here ~{fmt_n(max(1, first))} min)"
                       if rf is not None and first is not None else "")
            lines.append(f"Removes nothing of yours{s0}. The {reb.title} card takes apart {what} instead{cmp_txt}.")
        elif st.get("reviewed"):
            best = (st.get("review") or [None])[0]
            if best and best.get("gain_min") is not None and best["gain_min"] > 0:
                lines.append(f"Removes nothing of yours{s0}. Checked {st['reviewed']} old builds: the best (taking "
                             f"apart {best['label']} {best.get('gps', '')}) would save ~{fmt_n(best['gain_min'])} min, "
                             f"under the {fmt_n(best.get('needed_min') or REMOVE_GAIN_MIN)} min it takes to be worth it.")
            else:
                lines.append(f"Removes nothing of yours{s0}. Checked {st['reviewed']} old builds, none worth "
                             "taking apart.")
        else:
            lines.append(f"Removes nothing of yours{s0}.")
    else:
        ng = st.get("net_gain") or {}
        gain = []
        if ng.get("eta_first_plates_min"):
            gain.append(f"first plates {fmt_n(abs(ng['eta_first_plates_min']))} min "
                        f"{'sooner' if ng['eta_first_plates_min'] > 0 else 'later'}")
        if ng.get("eta_target_min"):
            gain.append(f"full rate {fmt_n(abs(ng['eta_target_min']))} min "
                        f"{'sooner' if ng['eta_target_min'] > 0 else 'later'}")
        bv = TITLES.get(st.get("base_variant", ""), "").lower()
        each = []
        for c in rm.get("clusters") or []:
            each.append(f"{c['label']} {c['gps']}" + (f" ({c['reason']})" if c.get("reason") else ""))
        vs = f"{bv.capitalize()} (which keeps them)" if bv else "keeping them"
        lines.append(f"Removes exactly {len(p.removes)} of your entities: {'; '.join(each)}. Compared with {vs}: "
                     f"{', '.join(gain) or 'see the reasons'}. Items come back to you{s0}.")
    # stage 2 (placed) / later stages
    s2 = next((s for s in st.get("stages") or [] if s and not s.get("now")), None)
    if s2 is not None:
        e2 = s2.get("eta") or {}
        if s2.get("placed"):
            when = []
            if e2.get("power_on") is not None:
                when.append(f"power on ~{fmt_n(e2['power_on'])} min from now")
            if e2.get("science_start") is not None:
                when.append(f"red science research from ~{fmt_n(e2['science_start'])} min")
            if e2.get("automation") is not None:
                when.append(f"Automation done ~{fmt_n(e2['automation'])} min")
            lab = any(u.get("tech") == "automation-science-pack" for u in s2.get("unlocks") or [])
            lab_txt = " Red science unlocks when you craft one lab by hand (the helper's crafting doesn't count)."                 if lab else ""
            lines.append(f"Stage 2 (placed and checked; approve it once this one runs): {', '.join(s2['what'])}. "
                         f"Needs {_raw_txt(s2.get('bill_raw') or {})}; {', '.join(when) or 'timing unknown'}.{lab_txt}")
        elif s2.get("after_stage1_min") is not None:
            lines.append(f"Next: {s2['name']}, about {s2['after_stage1_min']} min after stage 1 "
                         f"(needs {_raw_txt(s2.get('bill_raw') or {})}).")
        elif s2.get("after"):
            lines.append(f"Then: {s2['name']} after {s2['after']}.")
    # trade-off (+ the hub that was checked)
    if p.variant == "rebuild":
        base = st.get("base_variant", "quick")
        lines.append(f"Trade-off: like {TITLES.get(base, base).lower()}, but takes apart the old builds above.")
    else:
        hc = st.get("hub_check")
        if hc and hc.get("belt_tiles") is not None:
            hub_txt = (f" A central hub was checked and is not offered: {hc['belt_tiles']} belt tiles, longest run "
                       f"{hc['longest_run']} ({hc['why_not'][0]}).")
        elif hc:
            hub_txt = f" A central hub is not offered: {hc['why_not'][0]}."
        else:
            hub_txt = ""
        lines.append(f"Trade-off: {st.get('tradeoff') or TRADEOFF.get(p.variant, '')}.{hub_txt}")
    gates = st.get("gates") or []
    idle = [r for r in st.get("idle_review") or [] if r.get("action") in ("keep", "reuse") and r.get("reason")]
    if idle and len(lines) + min(1, len(gates)) < 8:
        r = max(idle, key=lambda r: len(r.get("label", "")))
        lines.append(f"Kept as it is: your {r['label']} {r['gps']}: {r['reason']}.")
    for g in gates:
        if len(lines) >= 8:
            break
        lines.append(f"Warning: {g['text']}.")
    return "\n".join(_sentence(x) for x in lines[:8])  # one Plans tab line each


def card_stage2(s2: Plan, parent: Plan) -> str:
    """Card of a placed stage 2 (shown when the player asks for it after stage 1 runs)."""
    st = s2.stats
    eta = st.get("eta") or {}
    b = st.get("bill") or {}
    row = next((s for s in parent.stats.get("stages") or [] if s.get("placed")), {}) or {}
    lines = [f"{s2.title} - after {parent.title.lower()}: {', '.join(row.get('what') or [])}."]
    lines.append(f"Needs {_raw_txt(b.get('need_raw') or {})}; from stage 1's plates (estimate: "
                 f"{eta.get('assumes', '')}).")
    when = [f"{k.replace('_', ' ')} ~{fmt_n(v)} min" for k, v in eta.items()
            if k not in ("assumes", "start") and v is not None]
    if when:
        lines.append("From now: " + ", ".join(when) + ".")
    if st.get("plant"):
        lines.append(f"The boiler is fed by hand: ~{fmt_n(st.get('boiler_coal_per_min') or 0)} coal/min for "
                     f"{fmt_n(st.get('power_kw') or 0)} kW of labs; the helper's coal round includes it (and keeps "
                     "stage 1's burners going).")
    if st.get("research"):
        lines.append(f"Research: {', '.join(nice(t) for t in st['research'])} ({st.get('research_packs', 0)} red packs, "
                     "hand-crafted by you or the helper and put into the labs).")
    if st.get("research_steps"):
        lines.append("Unlocks on the way: " + "; ".join(f"{nice(r['tech'])} by {r['unlocked_by']}"
                                                         for r in st["research_steps"]) + ".")
    for g in st.get("gates") or []:
        if len(lines) >= 8:
            break
        lines.append(f"Warning: {g['text']}.")
    return "\n".join(_sentence(x) for x in lines[:8])


def _pl(n, name) -> str:
    """'3 stone furnaces', '1 lab', '63 coal'."""
    word = nice(name)
    if round(n) != 1 and name not in ("coal", "stone", "wood") and not word.endswith(("s", "ore")):
        word += "s"
    return f"{round(n)} {word}"


def _sentence(s: str) -> str:
    s = s.strip()
    return s[0].upper() + s[1:] if s else s


# ---- public API ------------------------------------------------------------------------------------------------
def plan_options(world, gd, state=None, goal=None, variants=3, allow_removal=True, keys=None, **kw) -> list[Plan]:
    """Planner v2 variants for `world` from `state` (None: fresh start), best first (see module doc)."""
    if keys:
        keys = [ALIASES.get(k, k) for k in keys]
    return PlannerV2(world, gd, state, goal, allow_removal=allow_removal, **kw).options(variants, keys)
