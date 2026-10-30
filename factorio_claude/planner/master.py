"""Planner v3: one end-state starter base first, stages derived from it (docs/design/PLANNER-v3.md).

    mp = design_master(world, gd, state, goal, variant="hub")     # MasterPlan (raises MasterError)
    mps = master_options(world, gd, state, goal)                  # [MasterPlan], best first
    stage_entities(mp, k) / new_in(mp, k) / removed_in(mp, k)     # the base after stage k / its ghosts / removals
    check_monotone(mp) -> [issue]                                  # M1 in code (STAGE_CONFLICT)
    selfrun(mp, k) -> {ok, needs_helper, issues, rates}            # M4: runs with the helper idle?
    stage_plan(mp, k, world, gd, state) -> Plan                    # an ordinary Plan for plan_preview / plan_apply

The master plan holds every entity that is ever placed, each tagged in extra with `stage` (first stage it exists in),
`until` (the stage that removes it, or None), `zone`, `role` and, for in-place swaps, `slot`. Stages:

  S0 burner iron + coal   S1 burner copper   S2 steam power + labs   S3 automated red science
  S4 electric drills (in the burner slots, research: electric mining drill)   S5 green science (reserved in round A)

Every stage is a subset of the end state plus the planned swaps: burner drill + its burner inserter -> electric drill
in the same 3x3 slot (S4), burner output inserter -> inserter on the same tile (S2), buffer chest -> plate belt (S3/S5).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import blocks as B
from . import cost
from .blocks import PE, Port
from .grid import BLOCKED, CLEARABLE
from .onsite import dist_point_patch
from .placer import _Sum, parse_goal
from .plan import Plan, entity_counts, fmt_n, nice
from .router import NoRoute, place_poles, route_belt, route_pipe
from .state import BaseState
from .validate import validate

NORTH, EAST, SOUTH, WEST = 0, 4, 8, 12
DVEC = {NORTH: (0, -1), EAST: (1, 0), SOUTH: (0, 1), WEST: (-1, 0)}

# ---- sizing (PLANNER-v3 section 5 step 1; tests pin these) ------------------------------------------------------------
TARGET_RED, TARGET_GREEN = 0.5, 0.5        # science packs/s the end state is sized for
IRON = {"slots": 15, "burners": 4, "pairs0": 2, "pairs": 12, "pairs_res": 13}
COPPER = {"slots": 7, "burners": 4, "pairs0": 2, "pairs": 5, "pairs_res": 6}
N_RED, N_RED_RES, LABS_PER_SIDE = 5, 8, 8
LABS_AT = {2: 2, 3: 10, 5: 16}              # labs standing after stage k (built bottom-up, west + east); round 3:
#   10 labs at S3 use 30 red/min on techs up to 20 s per unit (5 labs only on 10 s ones: live 11-12/min on 30 s
#   techs), 16 at S5 use 30 red + 30 green/min on techs up to 30 s per unit
COAL_PER_SWING = 1 / 65                     # coal a burner inserter burns per item moved (engine check E4: 65 plates/coal)
BURNER_DRILL_COAL = 150 / 4000              # 150 kW / 4 MJ
FURNACE_COAL = 90 / 4000
COAL_MARGIN = 1.15
INSERTER_AVG_KW = 5.0                       # average draw of an electric inserter (max 14.7 kW, mostly idle)
DRILL_DUTY = 0.5                            # electric drills run this share of the time (live S5: 8 of 35 working)
POWER_MARGIN = 1.1
LONG_RUN = 120                              # a single belt run longer than this becomes a connector stage
FAR_APART = 400                             # total starter belt beyond this: FAR_APART warning
MIN_ORE_E = 10                              # ore tiles in an electric drill's 5x5 area for a slot to count
COLUMN_INS_FILL = 10                        # coal per fill of a smelting column's burner inserter (S0/S1)
PRE_FUEL_MIN = 60                           # S0/S1 output inserters: pre-fuel must last this long (minutes)
PORT_LANE = 3                               # tiles kept free straight out of every belt port until it is routed
POWER_COAL_W = 1.0                          # power placement: weight of the coal port's distance to the coal
MAX_ATTEMPTS = 6                            # placement alternatives per variant (deterministic: no time budget)
NEGOTIATE_ITERS = 30
NEGOTIATE_STALL = 8                         # rounds without fewer shared tiles before a layout is given up
LANE_COST = 6.0                             # negotiated routing: a step onto another belt's port lane
SHORT_PAIR = 60                            # port pairs this close must not cross each other (straight-line test)
MIN_POCKET = 600                           # free tiles an open port must still reach after a block is placed
BAN_R = 4                                   # a placement that walled in a port bans origins this close
ALT_BAN_R = 10                              # an alternative layout keeps this far from the first choice
PLACE_TRIES = 10
TAIL_TO_COAL = 0.5                        # field placement: weight of the coal leg vs the ore + coal pair to the furnaces
CROSS_W = 60                              # placement: a straight belt segment crossing a planned one (a detour)
CUT_COST = 8                              # routing: a step through one tile of the player's idle belt (dismantled)
POWER_RESERVE = 6                         # boilers the power block reserves room for (2 engines each)
SPLIT_MARGIN = 6                          # hub: furnace coal straight from the coal patch when that is this much shorter
BOILER_LOAD_S3_KW = 1100                   # sizes the boiler coal field before the loads are known (refined after)
BOILER_LOAD_S5_KW = 5200
CARD_LINES = 5

STAGES = [  # (index, key, title, gate techs, after, unlocks / what it is for)
    (0, "burner_iron", "Burner iron + coal", [], None, "iron plates; steam power research (50 plates)"),
    (1, "burner_copper", "Burner copper", [], 0, "copper plates; electronics research (10 plates)"),
    (2, "power_labs", "Steam power + labs", ["steam-power", "electronics"], 1,
     "power and 2 labs; red science research (you craft one lab by hand), then Automation from 10 hand-made packs"),
    (3, "red", "Automate red science", ["automation-science-pack", "automation"], 2,
     "red packs from assemblers into 10 labs (15/min while the burner drills mine, 30/min from S4): Electric mining "
     "drill (25 packs), Logistics (20), Logistic science pack (75)"),
    (4, "electric", "Electric drills", ["electric-mining-drill"], 3,
     "electric drills in the burner drills' slots: no coal for mining, 450 iron + 187 copper/min of capacity"),
    (5, "green", "Automate green science", ["logistic-science-pack", "logistics"], 3,
     "30 green packs/min and 16 labs (30 red + 30 green/min on techs up to 30 s per unit)"),
]
STAGE_KEYS = {s[1]: s[0] for s in STAGES}
RESEARCH_PATH = [  # (tech, stage that needs it, pack source)
    ("steam-power", 2, "trigger"), ("electronics", 2, "trigger"), ("automation-science-pack", 3, "trigger"),
    ("automation", 3, "hand"), ("electric-mining-drill", 4, "assemblers"), ("logistics", 5, "assemblers"),
    ("logistic-science-pack", 5, "assemblers")]
TRIGGER_TEXT = {"steam-power": "smelt 50 iron plates (the S0 furnaces do it)",
                "electronics": "smelt 10 copper plates (the S1 furnaces do it)",
                "automation-science-pack": "the PLAYER crafts one lab by hand (a lab the helper crafts does not count)"}
# removals the master plan may plan (role -> allowed swap kind); anything else removed = STAGE_CONFLICT. Round 3
# (B1 read strictly): only in-place swaps. upgrade = burner drill + its burner inserter -> the electric drill on the
# same 3x3; replace = burner inserter -> inserter on the same tile; retire = a temporary chest (plate buffer, the
# connector stage's coal chest) and its inserter, and only where the same stage puts a belt on that very tile.
# Nothing else (no belt, drill, furnace, boiler, lab or pole) is ever taken up again.
SWAP_ROLES = {"drill_b": {"upgrade"}, "ins_b": {"upgrade"}, "out_b": {"replace"}, "in_b": {"replace"},
              "buffer": {"retire"}, "coal_ins": {"retire"}, "coal_chest": {"retire"}, "route": {"replace"}}
REPLACE_INTO = {"in_b": "inserter", "out_b": "inserter", "route": "splitter"}   # what a replace swap puts there
VARIANTS = ("hub", "onsite")
ALIASES = {"quick": "onsite", "compact": "onsite", "balanced": "hub", "longterm": "hub", "expandable": "hub"}
TITLES = {"hub": "Central hub", "onsite": "Smelt at each patch"}
TRADEOFF = {"hub": "furnaces and science in one block; each ore field sends its ore and coal on one belt",
            "onsite": "furnaces at each ore field, so the first plates come soonest; one plate belt per metal "
                      "goes to the science"}


class MasterError(RuntimeError):
    pass


@dataclass
class Zone:
    id: str
    kind: str
    rect: tuple
    first_stage: int
    label: str
    ports: list = field(default_factory=list)     # tiles where routes of other zones may enter


@dataclass
class Swap:
    slot: str
    kind: str          # upgrade | replace | retire
    stage: int
    out: list
    into: list


@dataclass
class StageSpec:
    index: int
    key: str
    title: str
    gate: list
    after: int | None
    unlocks: str
    research: list = field(default_factory=list)


@dataclass
class MasterPlan:
    id: str
    variant: str
    title: str
    hub: tuple | None
    zones: list
    ents: list
    swaps: list
    stages: list
    rates: dict = field(default_factory=dict)
    power: dict = field(default_factory=dict)
    routes: list = field(default_factory=list)
    research_path: list = field(default_factory=list)
    removes: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    cuts: list = field(default_factory=list)     # [{name, x, y, stage, route}]: the player's idle belt tiles a
    #                                              route of `stage` stands on (dismantled in that stage)
    warnings: list = field(default_factory=list)
    surface: str = "nauvis"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ents"] = [asdict(e) for e in self.ents]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MasterPlan":
        d = dict(d)
        d["zones"] = [Zone(**{**z, "rect": tuple(z["rect"])}) for z in d.get("zones") or []]
        d["swaps"] = [Swap(**s) for s in d.get("swaps") or []]
        d["stages"] = [StageSpec(**s) for s in d.get("stages") or []]
        d["ents"] = [PE(**e) for e in d.get("ents") or []]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, directory) -> Path:
        p = Path(directory)
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"{self.id}.master.json"
        f.write_text(json.dumps(self.to_dict(), separators=(",", ":"), default=str), encoding="utf-8")
        return f

    @classmethod
    def load(cls, path) -> "MasterPlan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def zone(self, zid) -> Zone | None:
        return next((z for z in self.zones if z.id == zid), None)


# ---- stage views (section 4) ------------------------------------------------------------------------------------
def _x(e) -> dict:
    return e.extra if isinstance(e.extra, dict) else {}


def alive(e, k) -> bool:
    x = _x(e)
    return x.get("stage", 0) <= k and (x.get("until") is None or k < x["until"])


def stage_entities(mp: MasterPlan, k: int) -> list:
    """The base after stage k is built (copies with the stage's fuel/input flags set)."""
    return [_flags(e, k) for e in mp.ents if alive(e, k)]


def new_in(mp: MasterPlan, k: int) -> list:
    return [_flags(e, k) for e in mp.ents if _x(e).get("stage") == k]


def removed_in(mp: MasterPlan, k: int) -> list:
    return [e for e in mp.ents if _x(e).get("until") == k]


def _flags(e, k) -> PE:
    """Per-stage extras the validator and the mod read: S0/S1 burner output inserters are pre-fuelled by hand
    (fuel=manual), labs take hand-made packs until the science belt exists (input=manual)."""
    x = dict(_x(e))
    role = x.get("role")
    x.pop("fuel", None)
    x.pop("input", None)
    x.pop("prime", None)
    if role in ("out_b", "in_b") or (x.get("manual_until") is not None and k < x["manual_until"]):
        x["fuel"] = "manual"
        if role in ("out_b", "in_b"):
            # a column inserter moves ~65 items per coal: 10 coal last ~35 min at a furnace's 0.31 plates/s, past
            # S2 (round 2 live: one with the default 5 ran dry right after S0 was built)
            x["fuel_n"] = COLUMN_INS_FILL
    elif role in ("drill_b", "ins_b") and x.get("coal_field"):
        # a self-feeding coal field runs on the coal it mines, but only once it has some: the helper primes it
        # (start fuel, and its round restarts it if it ever stops); it is not hand-fed (round-2 live finding: a coal
        # field built while the helper had no coal never started)
        x["prime"] = 5 if role == "drill_b" else 1
    if role == "lab" and k < 3:
        x["input"] = "manual"
    return PE(e.name, e.x, e.y, e.direction, e.recipe, x)


# ---- checks (section 7.4 / 8.2) -----------------------------------------------------------------------------------
def _tiles(gd, e) -> set:
    return gd.footprint(e.name, e.x, e.y, e.direction or 0)


def check_monotone(mp: MasterPlan, gd) -> list:
    """STAGE_CONFLICT issues: overlaps inside any stage, removals that are not a planned swap of an allowed kind,
    foreign entities inside a zone, swap counts that do not add up. [] = the master plan is monotone (M1)."""
    issues = []
    ids = {_x(e).get("id"): e for e in mp.ents}
    for k in sorted({s.index for s in mp.stages} | {_x(e).get("stage", 0) for e in mp.ents}):
        seen = {}
        for e in mp.ents:
            if not alive(e, k):
                continue
            for t in _tiles(gd, e):
                o = seen.get(t)
                if o is not None:
                    issues.append({"code": "STAGE_CONFLICT", "stage": k, "tile": t,
                                   "text": f"S{k}: {e.name} ({_x(e).get('role')}, S{_x(e).get('stage')}) overlaps "
                                           f"{o.name} ({_x(o).get('role')}, S{_x(o).get('stage')}) at {t}"})
                    break
                seen[t] = e
    placed_on = {}                     # stage -> tiles its new entities stand on (in-place swaps land there)
    for e in mp.ents:
        placed_on.setdefault(_x(e).get("stage", 0), set()).update(_tiles(gd, e))
    swapped = {}
    for s in mp.swaps:
        for i in s.out:
            if i in swapped:
                issues.append({"code": "STAGE_CONFLICT", "text": f"{i} is removed by two swaps"})
            swapped[i] = s
        for i in s.into:
            e = ids.get(i)
            if e is None or _x(e).get("stage") != s.stage:
                issues.append({"code": "STAGE_CONFLICT", "text": f"swap {s.slot}: {i} is not placed in S{s.stage}"})
    for e in mp.ents:
        x = _x(e)
        if x.get("until") is None:
            continue
        s = swapped.get(x.get("id"))
        if s is None or s.stage != x["until"]:
            issues.append({"code": "STAGE_CONFLICT", "text": f"{e.name} at ({e.x}, {e.y}) is removed in "
                                                             f"S{x['until']} without a planned swap"})
            continue
        if s.kind not in SWAP_ROLES.get(x.get("role"), ()) or (
                s.kind == "replace" and [ids[i].name for i in s.into if i in ids] != [REPLACE_INTO[x["role"]]]):
            issues.append({"code": "STAGE_CONFLICT", "text": f"{e.name} ({x.get('role')}) may not be removed "
                                                             f"by a {s.kind} swap"})
        elif not (_tiles(gd, e) <= placed_on.get(x["until"], set())):
            issues.append({"code": "STAGE_CONFLICT", "text": f"{e.name} ({x.get('role')}) at ({e.x}, {e.y}) is "
                                                             f"removed in S{x['until']} but nothing of that stage "
                                                             f"takes its place (only in-place swaps)"})
        if x["until"] <= x.get("stage", 0):
            issues.append({"code": "STAGE_CONFLICT", "text": f"{e.name} at ({e.x}, {e.y}) is removed before it "
                                                             f"is built"})
    for z in mp.zones:
        zx0, zy0, zx1, zy1 = z.rect
        ports = {tuple(p) for p in z.ports}
        for e in mp.ents:
            x = _x(e)
            if x.get("zone") == z.id:
                continue
            for t in _tiles(gd, e):
                if zx0 <= t[0] < zx1 and zy0 <= t[1] < zy1 and t not in ports:
                    issues.append({"code": "STAGE_CONFLICT", "tile": t,
                                   "text": f"{e.name} (S{x.get('stage')}, zone {x.get('zone')}) stands inside zone "
                                           f"{z.id} ({z.label})"})
                    break
    up = [s for s in mp.swaps if s.kind == "upgrade"]
    retired = {i for s in mp.swaps if s.kind == "retire" for i in s.out}
    slots = {x.get("slot") for e in mp.ents for x in [_x(e)] if x.get("role") == "drill_b" and x.get("id") not in retired
             and x.get("until") is not None}      # burner-for-good drills (the boilers' coal) are never upgraded
    if len(up) != len(slots):
        issues.append({"code": "STAGE_CONFLICT", "text": f"{len(up)} upgrade swaps for {len(slots)} burner slots"})
    return issues


def swap_counts(mp: MasterPlan) -> dict:
    """B4 reference: how many entities each swap kind removes / places, per stage."""
    out = {}
    ids = {_x(e).get("id"): e for e in mp.ents}
    for s in mp.swaps:
        d = out.setdefault(slabel(s.stage), {})
        r = d.setdefault(s.kind, {"out": {}, "into": {}})
        for i in s.out:
            cost._add(r["out"], ids[i].name, 1)
        for i in s.into:
            cost._add(r["into"], ids[i].name, 1)
    return out


def coal_budget(mp: MasterPlan, gd, k: int) -> dict:
    """Coal supply vs demand per chain (items/s) for the base after stage k (section 8.1)."""
    out = {}
    for e in mp.ents:
        if not alive(e, k):
            continue
        x = _x(e)
        ch = x.get("chain")
        if not ch:
            continue
        c = out.setdefault(ch, {"supply": 0.0, "demand": 0.0})
        role = x.get("role")
        if role == "drill_b":
            c["demand"] += BURNER_DRILL_COAL + BURNER_DRILL_COAL * COAL_PER_SWING
            if x.get("coal_field"):
                c["supply"] += 0.25
        elif role == "drill_e" and x.get("coal_field"):
            c["supply"] += 0.5
        elif role == "furnace":
            c["demand"] += FURNACE_COAL
        elif role == "in_b":
            c["demand"] += (x.get("per_furnace", 0.3125) + FURNACE_COAL) * COAL_PER_SWING
        elif role == "boiler":
            lk = x.get("load_kw") or {}
            keys = [int(s) for s in lk if int(s) <= k]
            c["demand"] += (lk[str(max(keys))] if keys else 0.0) / 4000.0
    for c in out.values():
        c["supply"], c["demand"] = round(c["supply"], 4), round(c["demand"], 4)
        c["ok"] = c["supply"] >= c["demand"] * 1.1 - 1e-9
    return out


def power_budget(mp: MasterPlan, gd, k: int) -> dict:
    """Built generator capacity vs the peak load of stage k (kW)."""
    gen = load = 0.0
    for e in mp.ents:
        if not alive(e, k):
            continue
        p = gd.entity(e.name)
        es = p.get("energy_source") or {}
        if p.get("type") == "generator":
            gen += p.get("max_energy_production_kw") or 0
        elif es.get("type") == "electric" and p.get("type") != "electric-pole":
            if p.get("type") == "inserter":
                load += INSERTER_AVG_KW
            else:
                load += (p.get("max_energy_usage_kw") or p.get("energy_usage_kw") or 0) + (es.get("drain_kw") or 0)
    boilers = sum(1 for e in mp.ents if alive(e, k) and e.name == "boiler")
    gen = min(gen, boilers * 1800.0)
    return {"generation_kw": round(gen), "peak_kw": round(load), "ok": gen >= load * POWER_MARGIN - 1e-9 or load == 0}


def selfrun(mp: MasterPlan, gd, k: int, report=None) -> dict:
    """Static self-running check of the base after stage k (section 8.2): every burner has a fuel path that is not
    the helper (or S0/S1 pre-fuel that lasts PRE_FUEL_MIN), coal supply >= demand x 1.1 per chain, power capacity >=
    peak x 1.1, lanes within capacity. -> {ok, needs_helper, issues, rates}. `report`: a validate() Report of
    stage_entities(mp, k) (fuel errors there mean a burner has no fuel path)."""
    issues = []
    needs_helper = False
    hand = set()
    for e in mp.ents:
        x = _x(e)
        if alive(e, k) and x.get("manual_until") is not None and k < x["manual_until"]:
            hand.add((x.get("chain"), x["manual_until"]))
    for ch, until in sorted(hand, key=str):
        needs_helper = True
        issues.append(f"the {chain_label(ch)}'s burners are fuelled by the helper until {slabel(until)} connects "
                      f"its coal")
    ins_hand = [e for e in mp.ents if alive(e, k) and _x(e).get("role") in ("in_b", "out_b")]
    if ins_hand:
        # a burner inserter that moves ore or plates only refuels when it happens to carry coal (the input ones when
        # their furnace asks for some, the output ones never): the helper's round keeps them fuelled until S2
        # replaces them with electric inserters (round-1 live finding: they ran dry and stopped the furnaces)
        needs_helper = True
        issues.append(f"the helper keeps {len(ins_hand)} burner inserters of the smelting columns fuelled until "
                      f"S2 makes them electric")
        if k >= 2:
            issues.append(f"S{k}: {len(ins_hand)} burner inserters still move ore or plates (they run dry)")
    if report is not None:
        fuel = [i for i in report.errors if i.kind == "fuel"]
        if fuel:
            needs_helper = True
            issues += [i.msg for i in fuel[:3]]
    for ch, c in coal_budget(mp, gd, k).items():
        if not c["ok"]:
            issues.append(f"coal chain {ch}: {c['supply']:.2f}/s for {c['demand']:.2f}/s")
    pw = power_budget(mp, gd, k)
    if not pw["ok"]:
        issues.append(f"power: {pw['generation_kw']} kW built for {pw['peak_kw']} kW peak")
    for z in mp.stats.get("lanes", []):
        if z["per_lane"] > 7.5 + 1e-9:
            issues.append(f"{z['what']}: {z['per_lane']:.2f}/s on one lane")
    rates = stage_rates(mp, gd, k)
    hard = [i for i in issues if not i.startswith("the ") or "the helper" not in i]
    return {"ok": not hard, "needs_helper": needs_helper, "issues": issues, "rates": rates,
            "power": pw, "coal": coal_budget(mp, gd, k)}


def stage_rates(mp: MasterPlan, gd, k: int) -> dict:
    """Items/min the base makes after stage k (ore supply vs furnace capacity per column, red AMs)."""
    out = {}
    for ore, prod in (("iron-ore", "iron-plate"), ("copper-ore", "copper-plate")):
        ore_s = fur = 0.0
        for e in mp.ents:
            if not alive(e, k):
                continue
            x = _x(e)
            if x.get("ore") != ore:
                continue
            if x.get("role") == "drill_b":
                ore_s += 0.25
            elif x.get("role") == "drill_e":
                ore_s += 0.5
            elif x.get("role") == "furnace":
                fur += x.get("per_furnace", 0.3125)
        if fur:
            out[prod] = round(min(ore_s, fur, 7.5) * 60, 1)
    reds = sum(1 for e in mp.ents if alive(e, k) and _x(e).get("role") == "red_am")
    if reds:   # each pack: 1 copper + 1 gear (2 iron) from side A of the columns (half the plates)
        # round 3 live (real-save copy): with S0's 4 burner drills the iron side A makes ~30/min, so red runs at
        # 15-18/min until the electric drills of S4 (round 2 counted all the iron and promised 30)
        cap = min(reds * 0.1 * 60, out.get("iron-plate", 0) / 4, out.get("copper-plate", 0) / 2)
        out["automation-science-pack"] = round(cap, 1)
    greens = sum(1 for e in mp.ents if alive(e, k) and _x(e).get("role") == "green_am")
    prods = {e.recipe for e in mp.ents if alive(e, k) and _x(e).get("role") == "prod_am"}
    if greens and {"inserter", "transport-belt", "electronic-circuit", "copper-cable", "iron-gear-wheel"} <= prods:
        # each pack: inserter (1 circuit + 1 gear + 1 iron) + half a craft of belts (1 gear + 1 iron -> 2):
        # 5.5 iron + 1.5 copper; the AMs make 1 pack / 12 s each; the side-B plates cap it (side A feeds red), or
        # (on-site: one plate belt per metal split at the science) what side A has left after the red block
        fe_b = out.get("iron-plate", 0) / 2
        cu_b = out.get("copper-plate", 0) / 2
        if mp.stats.get("green_feed") == "split":
            red = out.get("automation-science-pack", 0)
            fe_b, cu_b = max(0.0, fe_b - 2 * red), max(0.0, cu_b - red)
        out["logistic-science-pack"] = round(min(greens * 5.0, fe_b / 5.5, cu_b / 1.5), 1)
    return out


# ---- geometry helpers ---------------------------------------------------------------------------------------------
def _rot_rect(r, size, k):
    x0, y0, x1, y1 = r
    w, h = size
    for _ in range(k % 4):
        x0, y0, x1, y1 = h - y1, x0, h - y0, x1
        w, h = h, w
    return (x0, y0, x1, y1)


def _shift_rect(r, dx, dy):
    return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)


def _rect_union(rs):
    rs = list(rs)
    return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))


def _ents_rect(gd, ents):
    ts = set()
    for e in ents:
        ts |= _tiles(gd, e)
    xs, ys = [t[0] for t in ts], [t[1] for t in ts]
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def _taxi(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _cross(p1, p2, q1, q2) -> bool:
    """Segments p1-p2 and q1-q2 properly intersect (shared end points do not count)."""
    p1, p2, q1, q2 = [(float(p[0]), float(p[1])) for p in (p1, p2, q1, q2)]

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return int(v > 0) - int(v < 0)
    if len({p1, p2} & {q1, q2}):
        return False
    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0


def _center(r):
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


@dataclass
class Part:
    """A block in local coordinates plus its zone rects (reserved ground) and labelled ports."""
    name: str
    ents: list
    size: tuple
    ports: dict              # label -> Port
    reserve: list = field(default_factory=list)      # [(zone id suffix, rect)]

    def rotated(self, k) -> "Part":
        b = B.Block(self.name, self.ents, self.size, list(self.ports.values()))
        rb = b.rotated(k)
        labels = list(self.ports)
        size = rb.size
        return Part(self.name, rb.entities, size, dict(zip(labels, rb.ports)),
                    [(z, _rot_rect(r, self.size, k)) for z, r in self.reserve])

    def at(self, ox, oy) -> "Part":
        return Part(self.name, [e.moved(ox, oy) for e in self.ents], self.size,
                    {k: p.moved(ox, oy) for k, p in self.ports.items()},
                    [(z, _shift_rect(r, ox, oy)) for z, r in self.reserve])

    def mirrored(self) -> "Part":
        """Left-right mirror image (east <-> west). Only for parts without mining drills (their drop point is not
        symmetric): belts, inserters, assemblers, labs, furnaces, poles mirror into a working layout."""
        W = self.size[0]
        flip = {EAST: WEST, WEST: EAST, NORTH: NORTH, SOUTH: SOUTH}
        ents = []
        for e in self.ents:     # extra["side"] stays: it names the block's logical side (A/B plates), not a compass side
            ents.append(PE(e.name, W - e.x, e.y, flip[e.direction], e.recipe, dict(_x(e))))
        ports = {k: Port(p.kind, p.item, W - 1 - p.x, p.y, flip[p.direction] if p.kind != "pole" else 0,
                         {"left": "right", "right": "left"}.get(p.lane)) for k, p in self.ports.items()}
        return Part(self.name, ents, self.size, ports, [(z, (W - r[2], r[1], W - r[0], r[3])) for z, r in self.reserve])


def _part(block: B.Block, labels, reserve=()) -> Part:
    return Part(block.name, [copy.deepcopy(e) for e in block.entities], block.size, dict(zip(labels, block.ports)),
                list(reserve))


# ---- the designer ---------------------------------------------------------------------------------------------------
class _Designer:
    def __init__(self, world, gd, state: BaseState | None, goal, variant):
        self.world, self.gd = world, gd
        self.goal = parse_goal(goal)
        near = self.goal.get("near")
        self.state = state or BaseState.fresh_start(tuple(near) if near else (0.0, 0.0))
        self.near = tuple(near) if near else (self.state.player_pos or self.state.pos or (0.0, 0.0))
        self.variant = ALIASES.get(variant, variant)
        if self.variant not in VARIANTS:
            raise MasterError(f"unknown variant {variant!r}")
        self.ents: list = []
        self.zones: list = []
        self.swaps: list = []
        self.routes: list = []
        self.warnings: list = []
        self.n = 0
        self.parts: dict = {}     # zone id -> placed Part
        self.open_ports: list = []
        self.pairs: list = []
        self.lanes: dict = {}
        self.mode: dict = {}      # ore -> "seq" | "split" (coal chain topology, hub variant)

    # ---- patches --------------------------------------------------------------------------------------------
    def _pick(self, res, near, min_amount):
        cands = [p for p in self.world.patches(min_tiles=40, name=res)]
        if not hasattr(self, "n_patches"):
            self.n_patches = {}
        self.n_patches[res] = len(cands)
        if not cands:
            return None
        big = [p for p in cands if p.amount >= min_amount] or cands[:1]
        return min(big, key=lambda p: dist_point_patch(near, p)[1])

    def patches(self):
        self.coal = self._pick("coal", self.near, 50_000)
        if self.coal is None:
            raise MasterError("no coal patch in the exported area")
        self.iron = self._pick("iron-ore", self.near, 200_000)
        if self.iron is None:
            raise MasterError("no iron ore patch in the exported area")
        self.copper = self._pick("copper-ore", self.near, 100_000)
        if self.copper is None:
            raise MasterError("no copper ore patch in the exported area")
        wt = self.world.nearest_water_tile(*dist_point_patch(self.near, self.coal)[0])
        if wt is None:
            raise MasterError("no water body in the exported area (steam power needs one)")
        self.water_tile = wt[0]
        pts = [self.near, self.water_tile] + [(b[0], b[1]) for p in (self.iron, self.copper, self.coal)
                                              for b in (p.bbox, (p.bbox[2], p.bbox[3]))]
        m = 48
        x0, y0, x1, y1 = self.world.bbox
        self.rect = (max(x0, int(min(p[0] for p in pts)) - m), max(y0, int(min(p[1] for p in pts)) - m),
                     min(x1, int(max(p[0] for p in pts)) + m), min(y1, int(max(p[1] for p in pts)) + m))
        self.grid = self.world.grid(self.rect)
        self._find_cuttable()
        self._block_reach()
        self.ok = _Sum(self.grid, self.grid.placeable_mask(allow_clear=True))
        self.ore_any = _Sum(self.grid, self.grid.res != 0)
        self.clear = _Sum(self.grid, self.grid.state == CLEARABLE)
        # real walking distance from the coal patch (lakes, cliffs and the player's builds in the way): what a coal
        # belt to a block's port will cost
        from .placer import walk_distance
        g = self.grid
        self.walk = {}
        for key, patch in (("coal", self.coal), ("iron", self.iron), ("copper", self.copper)):
            out = sorted(patch.outline() or patch.tiles,
                         key=lambda t: abs(t[0] - self.near[0]) + abs(t[1] - self.near[1]))
            src = [t for t in out if g.in_bounds(*t) and g.state_at(*t) != BLOCKED][:400]
            self.walk[key] = walk_distance(g, src, max_steps=2000)
        self.coal_walk = self.walk["coal"]

    def _block_reach(self):
        """Tiles the player's inserters (and their ghosts) pick from or drop on, and the drop tiles of their mining
        drills: nothing of the plan may stand there (an existing inserter would move foreign items into a planned
        belt, the validator's existing_link)."""
        from .gamedata import rotate
        g, gd = self.grid, self.gd
        tiles = []
        for e in self.world.entities_in(self.rect):
            name = e.name[6:] if e.name.startswith("ghost:") else e.name
            p = gd.entity(name) or {}
            if e.dir is None:
                continue
            if p.get("type") == "inserter":
                vx, vy = DVEC.get(int(e.dir) % 16, (0, 0))
                reach = 2 if "long" in name else 1
                for d in range(1, reach + 1):
                    for sgn in (1, -1):
                        tiles.append((math.floor(e.x + sgn * d * vx), math.floor(e.y + sgn * d * vy)))
            elif p.get("type") == "mining-drill" and p.get("vector_to_place_result"):
                vx, vy = rotate(*p["vector_to_place_result"], int(e.dir))
                tiles.append((math.floor(e.x + vx), math.floor(e.y + vy)))
            elif p.get("type") == "transport-belt" and (math.floor(e.x), math.floor(e.y)) not in self.cuttable:
                vx, vy = DVEC.get(int(e.dir) % 16, (0, 0))     # a working belt of theirs would feed into ours
                tiles.append((math.floor(e.x + vx), math.floor(e.y + vy)))
        free = [t for t in set(tiles) if g.in_bounds(*t) and g.state_at(*t) != BLOCKED]
        if free:
            g.occupy(free, "reach")
            for tx, ty in free:
                g.terrain[ty - g.y0, tx - g.x0] = BLOCKED

    def _find_cuttable(self):
        """Belt tiles of the player's idle lines (cluster status idle/passive): a route may cut through them (at
        CUT_COST per tile) instead of walking around; the cut tiles are dismantled in the route's stage. Working
        lines are never cut."""
        from . import existing
        self.cuttable, self.cuts = {}, {}
        if not (getattr(self, "knobs", None) or {}).get("allow_cut"):
            return      # allow_cut=False: routes go around the player's idle belts too
        try:
            cls = existing.clusters(self.world, self.gd, self.state, rect=self.rect)
        except Exception:      # noqa: BLE001 - no clusters: nothing is cut, routes go around
            return
        for c in cls:
            if c.keep_only or not (c.status.startswith("idle") or c.status in ("passive", "output_full",
                                                                               "disconnected")):
                continue
            for e in c.ents:
                if e.name == "transport-belt":
                    self.cuttable[(math.floor(e.x), math.floor(e.y))] = (e.name, e.x, e.y, int(e.dir or 0))

    def cut_grid(self, g):
        """Copy of grid g where the cuttable belt tiles are passable (CLEARABLE) + their extra cost array."""
        g = g.copy()
        cc = np.zeros((g.h, g.w))
        tid = g._tag_id("cut")
        for (tx, ty) in self.cuttable:
            if g.in_bounds(tx, ty) and str(g.tag_at(tx, ty) or "").startswith("existing:transport-belt"):
                i, j = ty - g.y0, tx - g.x0
                g.state[i, j] = CLEARABLE
                g.terrain[i, j] = CLEARABLE
                g.tag[i, j] = tid          # not a belt for the router any more (its line is cut where we cross)
                cc[i, j] = CUT_COST
        return g, cc

    def note_cuts(self, belts, stage, route):
        """Record the player's belt tiles a laid route stands on (and the upstream tile of their line that would
        feed into it): dismantled in `stage`."""
        tiles = {(math.floor(b.x), math.floor(b.y)) for b in belts}
        for t in sorted(tiles):
            if t in self.cuttable:
                self.cuts.setdefault(t, {"stage": stage, "route": route})
            for v in DVEC.values():        # an idle belt of theirs pointing into ours goes too
                n = (t[0] - v[0], t[1] - v[1])
                ce = self.cuttable.get(n)
                if ce is not None and n not in tiles and DVEC.get(ce[3] % 16) == v:
                    self.cuts.setdefault(n, {"stage": stage, "route": route})

    def walk_dist(self, key, X, Y):
        """Walking distance from patch `key` (coal / iron / copper) for arrays of tiles (inf -> 4000)."""
        g = self.grid
        i = np.clip(np.asarray(np.round(Y), dtype=int) - g.y0, 0, g.h - 1)
        j = np.clip(np.asarray(np.round(X), dtype=int) - g.x0, 0, g.w - 1)
        d = self.walk[key][i, j]
        return np.where(np.isfinite(d), d, 4000.0)

    def coal_dist(self, X, Y):
        """Walking distance from the coal patch for arrays of tiles (inf -> 4000)."""
        return self.walk_dist("coal", X, Y)

    # ---- entity bookkeeping --------------------------------------------------------------------------------
    def add(self, e: PE, *, zone, stage, until=None, **tags) -> PE:
        x = dict(_x(e))
        x.update({k: v for k, v in tags.items() if v is not None})
        x.update({"id": f"e{self.n}", "zone": zone, "stage": stage, "until": until})
        self.n += 1
        pe = PE(e.name, e.x, e.y, e.direction, e.recipe, x)
        self.ents.append(pe)
        return pe

    def occupy_zone(self, zid, rect, ports=()):
        """Block the zone's rect for everything else; its port tiles stay open and a short straight lane outside
        each belt port is kept free (PORT_LANE tiles) until the route that uses the port is laid."""
        x0, y0, x1, y1 = rect
        tiles = [(x, y) for x in range(x0, x1) for y in range(y0, y1)]
        self.grid.occupy(tiles, f"zone:{zid}")
        pt = [(p.x, p.y) for p in ports]
        self.grid.release([t for t in pt if x0 <= t[0] < x1 and y0 <= t[1] < y1])
        if not hasattr(self, "lanes"):
            self.lanes = {}
        for p in ports:
            if p.kind not in ("belt_in", "belt_out"):
                continue
            vx, vy = DVEC[p.direction]
            if p.kind == "belt_in":
                vx, vy = -vx, -vy
            lane = [(p.x + vx * i, p.y + vy * i) for i in range(0, PORT_LANE + 1)]
            lane = [t for t in lane if not (x0 <= t[0] < x1 and y0 <= t[1] < y1) and
                    (self.grid.state_at(*t) != BLOCKED or str(self.grid.tag_at(*t) or "").startswith("lane:"))]
            # the port tile itself too (outside the rect): no later block may stand on it (routes release it)
            self.grid.occupy(lane, f"lane:{zid}")
            self.lanes[(p.x, p.y)] = lane
        return [list(t) for t in pt]

    # ---- placement search --------------------------------------------------------------------------------------
    def _refresh(self):
        self.ok = _Sum(self.grid, self.grid.placeable_mask(allow_clear=True))

    # ---- routability: placing a block must not wall in a port that still needs its belt ---------------------------
    def routable(self) -> bool:
        """Every open port lies in a free region of at least MIN_POCKET tiles and every registered pair of ports
        (the two ends of a future route) lies in one region (lane tiles count as free: they are kept for routes)."""
        from scipy import ndimage
        g = self.grid
        base = g.state != BLOCKED

        def labels(ports):
            free = base.copy()
            for p in ports:
                for t in self.lanes.get((p.x, p.y), []) + [(p.x, p.y)]:
                    if g.in_bounds(*t):
                        free[t[1] - g.y0, t[0] - g.x0] = True
            lab, _n = ndimage.label(free)
            return lab, np.bincount(lab.ravel())

        def L(lab, p):
            return int(lab[p.y - g.y0, p.x - g.x0]) if g.in_bounds(p.x, p.y) else 0
        for p in self.open_ports:          # other ports' lanes count as blocked: a route may not cross them
            lab, sizes = labels([p])
            if L(lab, p) == 0 or sizes[L(lab, p)] < MIN_POCKET:
                return False
        for a, b in self.pairs:
            lab, _s = labels([a, b])
            if L(lab, a) == 0 or L(lab, a) != L(lab, b):
                return False
        short = [(a, b) for a, b in self.pairs if _taxi((a.x, a.y), (b.x, b.y)) <= SHORT_PAIR]
        for i, (a, b) in enumerate(short):     # two short flat belt links cannot cross (no undergrounds before S5);
            for c, d in short[i + 1:]:         # long ones can go around the blocks
                if _cross((a.x, a.y), (b.x, b.y), (c.x, c.y), (d.x, d.y)):
                    return False
        return True

    def forget_ports(self, ports):
        """Ports whose belt is laid: no longer open, their pairs done."""
        done = {(p.x, p.y) for p in ports}
        self.open_ports = [p for p in self.open_ports if (p.x, p.y) not in done]
        self.pairs = [(a, b) for a, b in self.pairs if (a.x, a.y) not in done and (b.x, b.y) not in done]

    def crossings(self, a, b) -> int:
        """How many registered port pairs (future belts) the straight segment a-b crosses."""
        return sum(1 for p, q in self.pairs if _cross(a, b, (p.x, p.y), (q.x, q.y)))

    def crossings_np(self, AX, AY, b):
        """Vectorised crossings(): segments (AX[i], AY[i]) - b against every registered pair."""
        AX, AY = np.asarray(AX, float), np.asarray(AY, float)
        bx, by = float(b[0]), float(b[1])
        out = np.zeros(AX.shape)

        def orient(ax, ay, bx_, by_, cx, cy):
            v = (bx_ - ax) * (cy - ay) - (by_ - ay) * (cx - ax)
            return np.sign(v)
        for p, q in self.pairs:
            px, py, qx, qy = float(p.x), float(p.y), float(q.x), float(q.y)
            o1 = orient(AX, AY, bx, by, px, py)
            o2 = orient(AX, AY, bx, by, qx, qy)
            o3 = orient(px, py, qx, qy, AX, AY)
            o4 = orient(px, py, qx, qy, bx, by)
            out += ((o1 * o2 < 0) & (o3 * o4 < 0)).astype(float)
        return out

    def commit(self, zones, opens=(), pairs=()) -> bool:
        """Occupy zones [(zid, kind, rect, first stage, label, ports)] with their port lanes and register open ports /
        port pairs; everything is undone when that walls in any open port (routable() fails)."""
        snap = (self.grid.copy(), dict(self.lanes), list(self.open_ports), list(self.pairs))
        made = []
        for zid, kind, rect, first, label, ports in zones:
            made.append(Zone(zid, kind, rect, first, label, self.occupy_zone(zid, rect, ports)))
        self.open_ports += list(opens)
        self.pairs += list(pairs)
        if self.routable():
            self.zones += made
            return True
        self.grid, self.lanes, self.open_ports, self.pairs = snap[0], snap[1], snap[2], snap[3]
        return False

    def _search(self, part: Part, window, score, *, ore_rects=(), avoid_ore=True, ore_weight=0.5, need=None,
                ban=()):
        """Best (k, ox, oy, score) for `part` (all 4 rotations): the part's bounding rect must be placeable inside
        `window`; ore_rects(rotated part) -> [(rect, rid, min_count, other_max)] constraints; score(rotated part,
        X, Y) -> cost array. need(rotated part, X, Y) -> extra boolean mask. ban: [(k, ox, oy)] placements that
        failed commit(); nothing within BAN_R tiles of them is tried again."""
        g = self.grid
        self._refresh()
        best = None
        wx0, wy0, wx1, wy1 = window
        for k in range(4):
            rp = part.rotated(k)
            w, h = rp.size
            xs = np.arange(max(wx0, g.x0), min(wx1, g.x0 + g.w) - w + 1)
            ys = np.arange(max(wy0, g.y0), min(wy1, g.y0 + g.h) - h + 1)
            if not len(xs) or not len(ys):
                continue
            X, Y = np.meshgrid(xs, ys)
            X, Y = X.ravel(), Y.ravel()
            good = self.ok.count(X, Y, (0, 0, w, h)) == w * h
            for b in ban:
                bk, bx, by = b[:3]
                r = b[3] if len(b) > 3 else BAN_R
                if bk == k or r > BAN_R:      # a wide ban (an alternative layout) covers every rotation
                    good &= ~((np.abs(X - bx) <= r) & (np.abs(Y - by) <= r))
            for r in rp.reserve:
                rr = r[1]
                good &= self.ok.count(X, Y, rr) == (rr[2] - rr[0]) * (rr[3] - rr[1])
            if not good.any():
                continue
            X, Y = X[good], Y[good]
            good = np.ones(len(X), bool)
            for rect, sumobj, lo, hi in (ore_rects(rp) if ore_rects else ()):
                c = sumobj.count(X, Y, rect)
                if lo is not None:
                    good &= c >= lo
                if hi is not None:
                    good &= c <= hi
                if not good.any():
                    break
            if need is not None:
                good &= need(rp, X, Y)
            for p in rp.ports.values():      # every belt port and its approach lane on free ground
                if p.kind not in ("belt_in", "belt_out"):
                    continue
                vx, vy = DVEC[p.direction]
                if p.kind == "belt_in":
                    vx, vy = -vx, -vy
                for i in range(0, PORT_LANE):
                    tx, ty = p.x + vx * i, p.y + vy * i
                    if 0 <= tx < w and 0 <= ty < h:
                        continue
                    good &= self.ok.count(X, Y, (tx, ty, tx + 1, ty + 1)) == 1
            if not good.any():
                continue
            X, Y = X[good], Y[good]
            sc = score(rp, X, Y).astype(float)
            if avoid_ore:
                sc += ore_weight * self.ore_any.count(X, Y, (0, 0, w, h))
            sc += 0.3 * self.clear.count(X, Y, (0, 0, w, h))
            i = int(np.argmin(sc))
            if best is None or sc[i] < best[3]:
                best = (k, int(X[i]), int(Y[i]), float(sc[i]))
        return best

    def _res_sum(self, res):
        if not hasattr(self, "_rs"):
            self._rs = {}
        if res not in self._rs:
            rid = self.grid.res_names.index(res) if res in self.grid.res_names else -1
            self._rs[res] = (_Sum(self.grid, self.grid.res == rid),
                             _Sum(self.grid, (self.grid.res != 0) & (self.grid.res != rid)))
        return self._rs[res]

    def place_field(self, zid, patch, n_slots, n_burn, target, *, self_feed=False, min_slots=None, tail_target=None,
                    commit=None, head_feed=False, mixed=False):
        """A mining field on `patch` with n_slots electric slots (the first n_burn east slots burner first), its
        head port as close to `target` as possible (tail port to tail_target for coal fields). commit(placed) ->
        bool accepts the spot (else the next best is tried, then fewer slots)."""
        res = patch.name
        mine, other = self._res_sum(res)
        min_slots = min_slots or max(2 * n_burn, 1)
        n_slots = max(n_slots, min_slots)
        bx0, by0, bx1, by1 = patch.bbox
        for n in range(n_slots, min_slots - 1, -1):
            blk = B.mining_field(n, self.gd, resource=res, self_feed=self_feed, head_feed=head_feed, mixed=mixed)
            labels = [p.kind + ":" + str(i) for i, p in enumerate(blk.ports)]
            part = _part(blk, labels)

            def cons(rp, n_burn=n_burn):
                out = []
                for e in rp.ents:
                    x = _x(e)
                    if x.get("role") == "drill_e":
                        r = B.tiles_of_box(e.x - 2.49, e.y - 2.49, e.x + 2.49, e.y + 2.49)
                        rect = (min(t[0] for t in r), min(t[1] for t in r), max(t[0] for t in r) + 1,
                                max(t[1] for t in r) + 1)
                        out.append((rect, mine, MIN_ORE_E, None))
                        out.append((rect, other, None, 0))
                    elif x.get("role") == "drill_b" and x.get("unit", 99) // 2 < n_burn:
                        fr = _ents_rect(self.gd, [e])
                        out.append((fr, mine, 4, None))
                return out

            def port_of(rp):
                ps = list(rp.ports.values())
                if self_feed:
                    return ps[-1]
                return ps[0]

            def score(rp, X, Y):
                p = port_of(rp)
                px, py = X + p.x, Y + p.y
                tg = tail_target if self_feed else target
                sc = np.abs(px - tg[0]) + np.abs(py - tg[1])
                vx, vy = DVEC[p.direction]
                sc = sc + ((vx * (tg[0] - px) + vy * (tg[1] - py)) < 0) * 12.0
                if self.pairs:        # its belt should not have to cross a belt already planned
                    sc = sc + CROSS_W * self.crossings_np(px, py, tg)
                if not self_feed:   # the corridor input (at the tail) should face the coal (real walking distance)
                    q = list(rp.ports.values())[1]
                    vx, vy = DVEC[q.direction]
                    sc = sc + TAIL_TO_COAL * self.coal_dist(X + q.x - vx * PORT_LANE, Y + q.y - vy * PORT_LANE)
                return sc
            win = (bx0 - 40, by0 - 40, bx1 + 40, by1 + 40)
            ban = []
            if zid in (getattr(self, "knobs", {}) or {}).get("field_alt", ()):
                first_best = self._search(part, win, score, ore_rects=cons, avoid_ore=False)
                if first_best is not None:
                    ban.append((first_best[0], first_best[1], first_best[2], ALT_BAN_R))
            for _try in range(PLACE_TRIES):
                best = self._search(part, win, score, ore_rects=cons, avoid_ore=False, ban=ban)
                if best is None:
                    break
                k, ox, oy, _s = best
                placed = part.rotated(k).at(ox, oy)
                placed.n_slots = n
                if commit is None or commit(placed):
                    return placed
                ban.append((k, ox, oy))
        raise MasterError(f"no room for a mining field on the {nice(res)} patch")

    def place_near(self, part: Part, target_port: str, target, radius=60, avoid_ore=True, ore_weight=2.0,
                   extra=None, commit=None):
        """Place a Part so its port `target_port` lands close to `target` (off ore if possible); commit(placed)
        -> bool accepts it (else the next best spot is tried)."""
        win = (int(target[0]) - radius, int(target[1]) - radius, int(target[0]) + radius, int(target[1]) + radius)

        def score(rp, X, Y):
            p = rp.ports[target_port]
            sc = np.abs(X + p.x - target[0]) + np.abs(Y + p.y - target[1])
            if extra is not None:
                sc = sc + extra(rp, X, Y)
            return sc
        ban = []
        for _try in range(3 * PLACE_TRIES):
            best = self._search(part, win, score, avoid_ore=avoid_ore, ore_weight=ore_weight, ban=ban)
            if best is None:
                return None
            k, ox, oy, _s = best
            placed = part.rotated(k).at(ox, oy)
            if commit is None or commit(placed):
                return placed
            ban.append((k, ox, oy))
        return None

    # ---- power -----------------------------------------------------------------------------------------------
    def place_power(self, n_boilers, near, pair_with=None, route_from=None):
        """The steam block at the shore near `near`: offshore pump, a short pipe to unit 0's water input. Its zone is
        committed (pair_with: the port its coal belt comes from must stay reachable). route_from (a coal port, every
        other belt already laid): each candidate spot's coal belt is routed for real and the spot with the shortest
        belt (plus its distance to `near`) wins; returns (placed, pump, pipe, rect, belts)."""
        gd, g = self.gd, self.grid
        spots = []
        for body in self.world.water_bodies(30):
            for x, y, d in self.world.shoreline(body):
                if g.in_bounds(x, y):
                    spots.append((_taxi((x, y), near), x, y, d))
        spots.sort()
        if route_from is not None:       # candidates spread along the shores (8+ tiles apart), nearest first
            spread = []
            for sp in spots:
                if all(abs(sp[1] - q[1]) + abs(sp[2] - q[2]) >= 8 for q in spread):
                    spread.append(sp)
                if len(spread) >= 60:
                    break
            spots = spread
        side_of = {EAST: 0, SOUTH: 1, WEST: 2, NORTH: 3}   # pump output dir -> turns of the canonical block
        cparts = []
        for end in ("water", "far"):
            canon = B.power_block(n_boilers, gd, coal_end=end)
            extra_belt = canon.size[0] * 0.5 if end == "far" else 0.0   # the far end: the whole belt row at S2
            cparts += [(_part(canon, ["water", "coal"]), extra_belt),
                       (_part(B.mirror_y(canon), ["water", "coal"]), extra_belt)]
        tried = 0
        self._refresh()
        found = []
        n_found = 12 if route_from is not None else 6
        for _d, x, y, d in spots[:400]:
            if len(found) >= n_found:
                break
            pump = PE("offshore-pump", x + 0.5, y + 0.5, d)
            foot = _tiles(gd, pump)
            if not g.can_place([t for t in foot if not g.is_water(*t)]):
                continue
            conns = gd.fluid_connections("offshore-pump", pump.x, pump.y, d)
            if not conns or g.state_at(conns[0][0], conns[0][1]) == BLOCKED:
                continue
            tx, ty, dout = conns[0]
            tried += 1
            if tried > 60:
                break
            k = side_of[dout]
            for cpart, extra_belt in cparts:
                rp = cpart.rotated(k)
                w, h = rp.size
                pin = rp.ports["water"]
                vx, vy = DVEC[dout]
                px, py = -vy, vx
                hit = None
                for gap in range(1, 6):
                    for lat in (0, 1, -1, 2, -2, 3, -3, 4, -4):
                        ox = tx + vx * gap + px * lat - pin.x
                        oy = ty + vy * gap + py * lat - pin.y
                        if self.ok.count(np.array([ox]), np.array([oy]), (0, 0, w, h))[0] != w * h:
                            continue
                        if self.ore_any.count(np.array([ox]), np.array([oy]), (0, 0, w, h))[0] > 0:
                            continue
                        placed = rp.at(ox, oy)
                        pc = placed.ports["coal"]      # the coal belt's way in: land, free, kept clear of the pipe
                        cvx, cvy = DVEC[pc.direction]
                        lane = [(pc.x - cvx * i, pc.y - cvy * i) for i in range(0, PORT_LANE + 3)]
                        if any(g.state_at(*t) == BLOCKED for t in lane):
                            continue
                        rect = (ox, oy, ox + w, oy + h)
                        gg = g.copy()
                        x0, y0, x1, y1 = rect
                        gg.occupy([(a, b) for a in range(x0, x1) for b in range(y0, y1)], "zone:power")
                        gg.occupy(foot, "power:pump")
                        p_in = placed.ports["water"]
                        gg.release([(tx, ty), (p_in.x, p_in.y)])
                        gg.occupy(lane[:PORT_LANE + 1], "lane:power")
                        try:
                            pipe = route_pipe(gg, gd, Port("pipe_out", "water", tx, ty, dout), p_in,
                                              others=placed.ents + [pump], max_nodes=20_000)
                        except NoRoute:
                            continue
                        if len(pipe) > 10:
                            continue
                        on = [(math.floor(e.x), math.floor(e.y)) for e in pipe]
                        on = [t for t in on if g.state_at(*t) == BLOCKED and not g.is_water(*t)]
                        if any(t not in self.cuttable for t in on):
                            continue       # the boiler's water input lies on someone's entity
                        placed.pipe_cuts = on          # an idle belt tile of theirs: cut (S2)
                        gg.occupy([(int(math.floor(e.x)), int(math.floor(e.y))) for e in pipe], "power:pipe")
                        le = lane[PORT_LANE]
                        cost_ = (_taxi((x, y), near) + POWER_COAL_W * float(self.coal_dist([le[0]], [le[1]])[0]) +
                                 extra_belt)
                        if pair_with is not None:    # its coal belt should not have to cross a planned belt
                            cost_ += CROSS_W * self.crossings((pair_with.x, pair_with.y), (pc.x, pc.y))
                        hit = (cost_, placed, pump, pipe, rect, gg)
                        break
                    if hit:
                        break
                if hit:
                    found.append(hit)
        old = self.grid
        found = sorted(found, key=lambda f: f[0])
        if route_from is not None:
            # every other belt is laid: route this coal belt for each candidate, keep the cheapest (belt + spot)
            rated = []
            own = self.lanes.get((route_from.x, route_from.y), [])
            # branch and bound: a belt is at least the taxicab distance long
            lb = sorted(((_taxi((route_from.x, route_from.y), (f[1].ports["coal"].x, f[1].ports["coal"].y)) +
                          0.25 * f[0], i) for i, f in enumerate(found)))
            for bound, i in lb:
                if rated and bound >= min(r[0] for r in rated):
                    break
                c_, placed, pump, pipe, rect, gg = found[i]
                pc = placed.ports["coal"]
                gt = gg.copy()
                x0, y0, x1, y1 = rect
                cvx, cvy = DVEC[pc.direction]
                pl = [(pc.x - cvx * i, pc.y - cvy * i) for i in range(0, PORT_LANE + 1)]
                gt.release([q for q in own + pl if str(gt.tag_at(*q) or "").startswith("lane:")] +
                           [(route_from.x, route_from.y), (pc.x, pc.y)])
                gc, cc = self.cut_grid(gt)
                try:
                    belts = route_belt(gc, gd, Port("belt_out", "coal", route_from.x, route_from.y,
                                                    route_from.direction),
                                       Port("belt_in", "coal", pc.x, pc.y, pc.direction), allow_underground=False,
                                       others=list(self.ents) + placed.ents, tag="route", max_nodes=400_000,
                                       occupy=False, tile_cost=cc)
                except NoRoute:
                    continue
                rated.append((len(belts) + 0.25 * c_, placed, pump, pipe, rect, gg, belts))
            rated.sort(key=lambda f: f[0])
            if (getattr(self, "knobs", {}) or {}).get("power_alt") and len(rated) > 1:
                rated = rated[1:] + rated[:1]
            for _c, placed, pump, pipe, rect, gg, belts in rated:
                self.grid = gg
                pc = placed.ports["coal"]
                if self.commit([("power", "power", rect, 2, "Steam power (S2; reserved for 6 boilers / 12 engines)",
                                 [pc])]):
                    return placed, pump, pipe, rect, belts
                self.grid = old
            raise MasterError("no spot for the steam power block that its coal belt can reach")
        if (getattr(self, "knobs", {}) or {}).get("power_alt") and len(found) > 1:
            found = found[1:] + found[:1]
        for _c, placed, pump, pipe, rect, gg in found:
            self.grid = gg
            pc = placed.ports["coal"]
            if self.commit([("power", "power", rect, 2, "Steam power (S2; reserved for 6 boilers / 12 engines)",
                             [pc])], pairs=[(pair_with, pc)] if pair_with is not None else ()):
                return placed, pump, pipe, rect
            self.grid = old
        raise MasterError("no room for the steam power block at the water")

    # ---- routes (laid by _negotiate) ----------------------------------------------------------------------------
    def _record(self, name, start, goal, belts, stage, chain, until):
        ids = []
        for b in belts:
            e = self.add(b, zone=f"route:{name}", stage=stage, until=until, role="route", chain=chain, route=name,
                         slot=f"route:{name}" if until is not None else None)
            ids.append(_x(e)["id"])
        if until is not None:      # a burner-phase coal belt: taken up (salvaged) with the burner drills it feeds
            self.swaps.append(Swap(f"route:{name}", "retire", until, ids, []))
        self.routes.append({"what": name, "from": [start.x, start.y], "to": [goal.x, goal.y], "tiles": len(belts),
                            "stage": stage, "fe": round(len(belts) * cost.C_BELT), "until": until})
        return belts


# ---- stage tagging of the blocks ------------------------------------------------------------------------------------
def _tag_field(D: _Designer, part: Part, zid, ore, first, n_burn, chain, *, coal_field=False, burner_forever=False,
               burn_stages=None, head_feed=False, retire_at=None, keep_burners=False):
    """Field entities -> master entities. Burner slots: the first n_burn east slots (stage `first`, or per-slot
    stages from burn_stages); electric drills in every slot at S4 unless burner_forever. Belt rows exist from the
    first stage a drill drops onto them (or below). head_feed: the corridor only carries coal for the burner phase
    (retired at S4 with the burner drills). retire_at: a coal field that only feeds the burner phase: everything
    retires at that stage (no electric drills)."""
    swaps = []
    bslot = {}
    for e in part.ents:
        x = _x(e)
        if x.get("role") == "drill_b" and x["unit"] // 2 < n_burn:
            bslot[x["unit"]] = (burn_stages[x["unit"] // 2] if burn_stages else first)
    electric = not burner_forever and retire_at is None
    # keep_burners: the burner slots stay burner for good and the free slots get electric drills at S4 (the boilers'
    # coal: the burners keep the steam engines turning through any power dip, the electric drills add the rest)
    extra_e = keep_burners
    drop_stage = {}
    for e in part.ents:
        x = _x(e)
        if x.get("role") == "drill_b" and x["unit"] in bslot:
            drop_stage[x["drop_row"]] = min(drop_stage.get(x["drop_row"], 9), bslot[x["unit"]])
        if x.get("role") == "drill_e" and (electric or (extra_e and x["unit"] not in bslot)):
            drop_stage[x["drop_row"]] = min(drop_stage.get(x["drop_row"], 9), 4)
    # canonical row of the last burner slot's inserter (the block is rotated: compare rows, not y)
    last_ins = max((B.field_slot_top(u // 2) for u in bslot), default=None)
    added = {}
    temp = []           # belts that retire with the burner phase
    for e in part.ents:
        x = _x(e)
        role = x.get("role")
        u = x.get("unit")
        kw = dict(zone=zid, chain=chain, ore=ore, coal_field=coal_field or None)
        if role in ("drill_b", "ins_b"):
            if u not in bslot:
                continue
            until = None if burner_forever else (retire_at or 4)
            added.setdefault(u, {})[role] = D.add(e, stage=bslot[u], until=until, slot=f"{zid}/s{u}", **kw)
        elif role == "drill_e":
            if not (electric or (extra_e and u not in bslot)):
                continue
            added.setdefault(u, {})[role] = D.add(e, stage=4, slot=f"{zid}/s{u}", **kw)
        elif role in ("ore_belt",):
            st = min((s for r, s in drop_stage.items() if r >= x["row"]), default=None)
            if st is None:
                continue
            if retire_at is not None:
                temp.append(D.add(e, stage=st, until=retire_at, slot=f"{zid}/belts", **kw))
            else:
                D.add(e, stage=st, **kw)
        elif role in ("feed", "corr"):
            if head_feed and role == "corr" and last_ins is not None and x.get("row", 0) > last_ins:
                continue        # a head-fed corridor stops at the last burner's inserter
            if retire_at is not None or (head_feed and role == "corr"):
                temp.append(D.add(e, stage=first, until=retire_at or 4, slot=f"{zid}/belts", **kw))
            else:
                D.add(e, stage=first, **kw)
        elif role == "fmerge":         # a mixed field's head: ore + coal onto one belt, from the first stage
            D.add(e, stage=first, **kw)
        elif role == "field_pole":
            if not (electric or extra_e):
                continue
            D.add(e, stage=4, **{**kw, "chain": None})
    for u, d in sorted(added.items()):
        if "drill_b" in d and "drill_e" in d:
            swaps.append(Swap(f"{zid}/s{u}", "upgrade", 4, [_x(d["drill_b"])["id"], _x(d["ins_b"])["id"]],
                              [_x(d["drill_e"])["id"]]))
        elif retire_at is not None and "drill_b" in d:   # a coal field that only serves the burner phase
            swaps.append(Swap(f"{zid}/s{u}", "retire", retire_at, [_x(d["drill_b"])["id"], _x(d["ins_b"])["id"]], []))
    if temp:
        swaps.append(Swap(f"{zid}/belts", "retire", _x(temp[0])["until"], [_x(e)["id"] for e in temp], []))
    D.swaps += swaps


def _tag_column(D: _Designer, part: Part, zid, ore, first, pairs0, pairs, side_a, b_green=True):
    """Column entities, built from the input end: the pairs0 pairs next to the merge from `first`, more up to
    `pairs` at S4 (the middle belt only grows downstream), the spare pair at the far end reserved. Side A plates
    go to the red block (plate belt S3, buffer chests until then); side B to the green block (buffer chests until
    S5), or (b_green=False: the on-site columns, whose side-A belt is split for the green block at S5) side B keeps
    its chests for good: plates for the player's own building."""
    n_res = 1 + max(_x(e).get("unit", 0) for e in part.ents if _x(e).get("role") == "furnace")
    st_pair = {}
    for i in range(n_res):
        rank = n_res - 1 - i
        if rank < pairs:
            st_pair[i] = first if rank < pairs0 else 4
    outb, oute = {}, {}
    for e in part.ents:
        x = _x(e)
        role, u, side = x.get("role"), x.get("unit"), x.get("side")
        kw = dict(zone=zid, ore=ore, chain=ore.split("-")[0], per_furnace=0.3125)
        if role == "furnace":
            if u in st_pair:
                D.add(e, stage=st_pair[u], **kw)
        elif role in ("out_b", "in_b"):
            if u in st_pair and st_pair[u] < 2:
                k = "o" if role == "out_b" else "i"
                outb[(k, u, side)] = D.add(e, stage=st_pair[u], until=2, slot=f"{zid}/{k}{u}{side}", **kw)
        elif role in ("out_e", "in_e"):
            if u in st_pair:
                k = "o" if role == "out_e" else "i"
                oute[(k, u, side)] = D.add(e, stage=max(2, st_pair[u]), slot=f"{zid}/{k}{u}{side}", **kw)
        elif role == "buffer":
            if u in st_pair:
                if side != side_a and not b_green:
                    D.add(e, stage=st_pair[u], **kw)        # a plate chest for good
                    continue
                until = 3 if side == side_a else 5
                if st_pair[u] < until:
                    D.add(e, stage=st_pair[u], until=until, slot=f"{zid}/b{u}{side}", **kw)
        elif role == "plate":
            need = [st_pair[i] for i in st_pair if 2 * i >= x["row"]]
            if not need or (side != side_a and not b_green):
                continue
            base = 3 if side == side_a else 5
            D.add(e, stage=max(base, min(need)), **kw)
        elif role == "mid":
            need = [st_pair[i] for i in st_pair if 2 * i <= x["row"]]
            if need:
                D.add(e, stage=min(need), **kw)
        elif role == "merge":
            D.add(e, stage=first, **kw)
        elif role == "col_pole":
            j = u
            need = [max(2, st_pair[i]) for i in (2 * j, 2 * j + 1) if i in st_pair]
            if need:
                D.add(e, stage=min(need), **{**kw, "chain": None})
    for key, ob in outb.items():     # burner -> electric inserter on the same tile, both columns, at S2
        if key in oute:
            D.swaps.append(Swap(_x(ob)["slot"], "replace", 2, [_x(ob)["id"]], [_x(oute[key])["id"]]))
    # buffer chests: retired by the plate belt tile of the same position (or just retired)
    by_pos = {}
    for e in D.ents:
        x = _x(e)
        if x.get("zone") == zid and x.get("role") == "plate":
            by_pos[(e.x, e.y)] = e
    for e in list(D.ents):
        x = _x(e)
        if x.get("zone") == zid and x.get("role") == "buffer" and x.get("until") is not None:
            pb = by_pos.get((e.x, e.y))
            into = [_x(pb)["id"]] if pb is not None and _x(pb)["stage"] == x["until"] else []
            D.swaps.append(Swap(x["slot"], "retire", x["until"], [x["id"]], into))


def _tag_power(D: _Designer, part: Part, zid, unit_stage: dict):
    xs = {}
    for e in part.ents:
        x = _x(e)
        if x.get("role") == "boiler":
            xs[x["unit"]] = e
    for e in part.ents:
        x = _x(e)
        role, u = x.get("role"), x.get("unit")
        kw = dict(zone=zid, chain="boiler")
        if role in ("boiler", "engine", "boiler_ins", "pipe"):
            if u in unit_stage:
                D.add(e, stage=unit_stage[u], **kw)
        elif role == "power_pole":
            if u in unit_stage:
                D.add(e, stage=unit_stage[u], **kw)
        elif role == "boiler_belt":
            # belt tile i (from the coal port): needed by every built unit whose inserter picks further along
            need = [unit_stage[uu] for uu in unit_stage if x["row"] <= uu_pick(part, uu)]
            if need:
                D.add(e, stage=min(need), **kw)


def uu_pick(part, u) -> int:
    """Belt index (distance from the coal port) at which unit u's inserter picks."""
    return part.unit_pick[u]


GREEN_ROLES = ("green_am", "green_out", "green_in", "prod_am", "prod_link", "prod_in", "prod_out", "mix_load", "mix",
               "mix_block", "feed", "feed_block", "feed_in", "green_link")


def _tag_science(D: _Designer, part: Part, zid, info):
    nl, nres = info["labs_per_side"], info["n_red_res"]
    lab_stage = {}
    order = [(j, s) for j in range(nl - 1, -1, -1) for s in ("W", "E")]
    for idx, key in enumerate(order):
        n = idx + 1
        lab_stage[key] = 2 if n <= LABS_AT[2] else 3 if n <= LABS_AT[3] else 5
    red_built = set(range(nres - N_RED, nres))
    top_red_row = info["y0"] + 3 * min(red_built) + 1
    top_lab = {k: min(j for (j, s), st in lab_stage.items() if st <= k) for k in (3, 5)}
    for e in part.ents:
        x = _x(e)
        role, u, side = x.get("role"), x.get("unit"), x.get("side")
        kw = dict(zone=zid)
        if role == "lab":
            D.add(e, stage=lab_stage[(u, side)], **kw)
        elif role == "lab_ins":
            D.add(e, stage=max(3, lab_stage[(u, side)]), **kw)
        elif role in ("red_am", "red_in", "red_out"):
            if u in red_built:
                D.add(e, stage=3, **kw)
        elif role in ("gear_am", "gear_out", "gear_in", "fe_end", "cu_load", "blocker", "link"):
            D.add(e, stage=3, **kw)
        elif role in GREEN_ROLES:
            D.add(e, stage=5, **kw)
        elif role == "mixed":
            if x["row"] >= top_red_row:
                D.add(e, stage=3, **kw)
        elif role == "spine":
            r = x["row"]
            if r >= 3 * top_lab[3] + 1:
                D.add(e, stage=3, **kw)
            elif r >= 3 * top_lab[5] + 1:
                D.add(e, stage=5, **kw)


# ---- the master design -------------------------------------------------------------------------------------------
def _coal_burners(demand, margin=COAL_MARGIN) -> int:
    per = 0.25 - BURNER_DRILL_COAL * (1 + COAL_PER_SWING) * margin
    return max(1, math.ceil(demand * margin / per - 1e-9))


def _chain_demand(n_burn, n_furn) -> float:
    return n_burn * BURNER_DRILL_COAL * (1 + COAL_PER_SWING) + n_furn * (FURNACE_COAL + (0.3125 + FURNACE_COAL) *
                                                                         COAL_PER_SWING)


def _stage_load_kw(gd, D: _Designer, k, expected=False) -> float:
    """Electric load of the base after stage k (kW): every machine at full power (sizes the engines and boilers), or
    expected=True: mining drills at DRILL_DUTY (they wait for space most of the time; the boilers' coal is sized for
    that, a short peak beyond it is a brownout, never a stop: their coal field is burner)."""
    load = 0.0
    for e in D.ents:
        if not alive(e, k):
            continue
        p = gd.entity(e.name)
        es = p.get("energy_source") or {}
        if es.get("type") == "electric" and p.get("type") not in ("electric-pole", "generator"):
            if p.get("type") == "inserter":
                kw = INSERTER_AVG_KW
            else:
                kw = (p.get("max_energy_usage_kw") or p.get("energy_usage_kw") or 0) + (es.get("drain_kw") or 0)
            if expected and p.get("type") == "mining-drill":
                kw *= DRILL_DUTY
            load += kw
    return load


def _core_links(gd, S, Yg) -> list:
    """The hub core's fixed plate links in the gap row(s) between the science block (rows <= Yg+5) and the two
    smelting columns (iron at x S..S+10, copper at S-12..S-2, both from row Yg+7). Red (S3): iron west plate (x S)
    straight north into the iron port, copper WEST plate (x S-12) east along row Yg+6 into the copper port (S-5).
    Green (S5, logistics researched): iron EAST plate (x S+10) straight north into the green iron port, copper EAST
    plate (x S-2) east along row Yg+6 under the iron A link and the iron B link (two underground pairs) into the
    green copper port (S+13)."""
    out = []

    def belt(x, y, d, role, **kw):
        out.append(B._put(gd, "transport-belt", x, y, d, extra=B._r(role, row=y, **kw)))
    for y in (Yg + 5, Yg + 6):
        belt(S, y, NORTH, "link")
    for xx in range(S - 12, S - 5):
        belt(xx, Yg + 6, EAST, "link")
    belt(S - 5, Yg + 6, NORTH, "link")
    belt(S - 5, Yg + 5, NORTH, "link")
    for y in (Yg + 5, Yg + 6):
        belt(S + 10, y, NORTH, "green_link")
    belt(S - 2, Yg + 6, EAST, "green_link")
    from .router import underground_pair
    for x0, n in ((S - 1, 2), (S + 9, 2)):
        for e in underground_pair(gd, "underground-belt", x0, Yg + 6, EAST, n):
            e.extra = dict(e.extra or {}, **B._r("green_link", row=Yg + 6))
            out.append(e)
    for xx in range(S + 2, S + 9):
        belt(xx, Yg + 6, EAST, "green_link")
    belt(S + 12, Yg + 6, EAST, "green_link")
    belt(S + 13, Yg + 6, NORTH, "green_link")
    belt(S + 13, Yg + 5, NORTH, "green_link")
    return out


ATTEMPTS = [  # placement alternatives tried in order when a layout cannot be routed without crossings
    {}, {"modes": "seq"}, {"coal_order": ("copper", "iron", "boiler")}, {"core_alt": True},
    {"field_alt": ("mine_copper",)},
    {"field_alt": ("mine_iron",)}, {"coal_order": ("boiler", "iron", "copper")}, {"power_alt": True},
    {"core_alt": True, "coal_order": ("copper", "iron", "boiler")}, {"field_alt": ("mine_iron", "mine_copper")},
    {"core_alt": True, "field_alt": ("mine_copper",)}, {"coal_order": ("iron", "boiler", "copper")},
    {"coal_order": ("boiler", "copper", "iron"), "field_alt": ("mine_copper",)},
]


def design_master(world, gd, state=None, goal=None, variant="hub", attempts=None, allow_cut=True) -> MasterPlan:
    """One full master plan (section 5). Placement alternatives (ATTEMPTS) are tried until every belt can be laid
    without crossing another. Raises MasterError with the reason when the variant cannot be built."""
    reasons = []
    t0 = time.monotonic()
    # a fixed number of alternatives, never a time budget: the same map gives the same plans offline and live
    modes = None
    wound = None          # the best layout so far whose belts wind far around (kept if nothing better comes)
    for i, knobs in enumerate(ATTEMPTS[:attempts or MAX_ATTEMPTS]):
        if knobs.get("modes") == "seq" and modes is not None and "split" not in modes.values():
            continue       # the first layout had sequential chains already: nothing new to try
        try:
            mp = _design_once(world, gd, state, goal, variant, dict(knobs, allow_cut=allow_cut))
            mp.stats["attempt"] = i
            mp.stats["seconds"] = round(time.monotonic() - t0, 1)
            d = detours(mp)
            if not d:
                return mp
            extra = sum(x["tiles"] - x["straight"] for x in d)
            if wound is None or extra < wound[0]:
                wound = (extra, mp)
            reasons.append(f"its {d[0]['what']} belt winds {d[0]['tiles']} tiles (straight {d[0]['straight']})")
        except MasterError as ex:
            reasons.append(str(ex))
            if modes is None:
                modes = getattr(ex, "modes", None)
            if str(ex).startswith(MISSING):
                break      # nothing to try: the map lacks the resource
    if wound is not None:   # every layout winds somewhere: the least winding one (master_options may drop it)
        wound[1].stats["seconds"] = round(time.monotonic() - t0, 1)
        return wound[1]
    raise MasterError(reasons[0] + (f" (and {len(reasons) - 1} other layouts tried)" if len(reasons) > 1 else ""))


MISSING = ("no coal patch", "no iron ore patch", "no copper ore patch", "no water body")


def _design_once(world, gd, state, goal, variant, knobs) -> MasterPlan:
    D = _Designer(world, gd, state, goal, variant)
    D.knobs = knobs
    try:
        return _design_steps(D, world, gd, knobs)
    except MasterError as ex:
        ex.modes = dict(D.mode)       # design_master skips the sequential retry when nothing was split
        raise


def _design_steps(D, world, gd, knobs) -> MasterPlan:
    t0 = time.monotonic()
    D.patches()
    # 1. size the coal chains (burner phase S0-S3, electric phase S4+)
    iron_d = _chain_demand(IRON["burners"], 2 * IRON["pairs0"])
    cop_d = _chain_demand(COPPER["burners"], 2 * COPPER["pairs0"])
    nc_iron = _coal_burners(iron_d)
    nc_cop = _coal_burners(cop_d)
    iron_e = 2 * IRON["pairs"] * (FURNACE_COAL + (0.3125 + FURNACE_COAL) * COAL_PER_SWING)
    cop_e = 2 * COPPER["pairs"] * (FURNACE_COAL + (0.3125 + FURNACE_COAL) * COAL_PER_SWING)
    ne_iron = max(2 * nc_iron, math.ceil(iron_e * COAL_MARGIN / 0.5))
    ne_cop = max(2 * nc_cop, math.ceil(cop_e * COAL_MARGIN / 0.5))
    D.coal_pt = dist_point_patch(D.near, D.coal)[0]

    sci = B.science_block(gd, n_red_res=N_RED_RES, labs_per_side=LABS_PER_SIDE)
    Yg, S = sci.info["Yg"], sci.info["S"]
    # hub: the ore fields send ore + coal on one belt (mining_field mixed) into columns with a mixed input; the coal
    # leg to each field is the furnaces' coal for good, so nothing of it is taken up when the drills turn electric
    hub = D.variant == "hub"
    col_i = B.smelter_column(IRON["pairs_res"], gd, ore="iron-ore", mixed_in=hub)
    col_c = B.smelter_column(COPPER["pairs_res"], gd, ore="copper-ore", mixed_in=hub)
    col_labels = ["plate_W", "plate_E", "ore", "coal"]

    if D.variant == "hub":
        # the core: science on top, copper column (side A = east) south-west, iron column (side A = west) south-east,
        # fixed plate links. Canonical south (the column inputs) is turned toward the iron field.
        core_ents = [copy.deepcopy(e) for e in sci.entities]
        ci_off, cc_off = (S, Yg + 7), (S - 12, Yg + 7)
        for e in col_i.entities:
            ee = e.moved(*ci_off)
            ee.extra = dict(_x(e), sub="iron")
            core_ents.append(ee)
        for e in col_c.entities:
            ee = e.moved(*cc_off)
            ee.extra = dict(_x(e), sub="copper")
            core_ents.append(ee)
        core_ents += _core_links(gd, S, Yg)
        ports = {"ore_i": col_i.ports[2].moved(*ci_off), "ore_c": col_c.ports[2].moved(*cc_off)}
        r_sci = (0, 0, sci.size[0], Yg + 7)
        r_ci = (ci_off[0], ci_off[1], ci_off[0] + col_i.size[0], ci_off[1] + col_i.size[1])
        r_cc = (cc_off[0], cc_off[1], cc_off[0] + col_c.size[0], cc_off[1] + col_c.size[1])
        x0 = min(r_cc[0], 0)
        y1 = max(r_ci[3], r_cc[3])
        core = Part("core", core_ents, (r_sci[2] - x0, y1), ports,
                    [("science", r_sci), ("smelt_iron", r_ci), ("smelt_copper", r_cc)])
        core = core.at(-x0, 0)
        core.size = (r_sci[2] - x0, y1)
        hub_pt = _weighted_hub(D)

        def face(rp, X, Y):   # the input side (ore ports) should face the iron patch
            p = rp.ports["ore_i"]
            ic = D.iron.center
            vx, vy = DVEC[p.direction]
            away = (vx * (ic[0] - (X + p.x)) + vy * (ic[1] - (Y + p.y))) > 0   # ports face north: iron must be south
            return np.where(away, 40.0, 0.0)

        ipt = dist_point_patch(hub_pt, D.iron)[0]
        cpt = dist_point_patch(hub_pt, D.copper)[0]

        def crossing(rp, X, Y):
            """The iron pair and the copper pair must not have to cross: along the side the ore ports are on, the
            iron ports must lie on the same side of the copper ports as the iron patch lies of the copper patch."""
            a, b = rp.ports["ore_i"], rp.ports["ore_c"]
            vx, vy = DVEC[a.direction]
            ax, ay = -vy, vx                                  # along the port side
            s = (a.x - b.x) * ax + (a.y - b.y) * ay           # same for every origin
            cx_ = (X + rp.size[0] / 2.0)
            cy_ = (Y + rp.size[1] / 2.0)
            ti = (ipt[0] - cx_) * ax + (ipt[1] - cy_) * ay
            tc = (cpt[0] - cx_) * ax + (cpt[1] - cy_) * ay
            return np.where(s * (ti - tc) < 0, 200.0, 0.0)

        def score(rp, X, Y):
            """Belt tiles the core's spot costs (real walking distances): per ore the mixed trunk from its field
            (ore + coal on one belt; the coal leg to the field costs the same wherever the core is), plus the boiler
            coal (about coal-core)."""
            cx, cy = X + rp.size[0] / 2, Y + rp.size[1] / 2

            def at(port, key):       # walking distance to the tile PORT_LANE out of the port (its approach)
                vx, vy = DVEC[port.direction]
                return D.walk_dist(key, X + port.x - vx * PORT_LANE, Y + port.y - vy * PORT_LANE)
            cost = D.walk_dist("coal", cx, cy)          # the boiler coal: the power block sits near the core
            for key, pk in (("iron", "ore_i"), ("copper", "ore_c")):
                cost = cost + at(rp.ports[pk], key)
            return cost + face(rp, X, Y) + crossing(rp, X, Y)
        r = D.rect
        win = (r[0], r[1], r[2], r[3])
        bans = ([], [])
        cores = (core, core.mirrored())
        for _try in range(PLACE_TRIES):
            cands = [(D._search(c, win, score, avoid_ore=True, ore_weight=3.0, ban=bans[i]), i)
                     for i, c in enumerate(cores)]
            cands = [(b, i) for b, i in cands if b is not None]
            if not cands:
                raise MasterError("no free ground for the central hub (science + smelting) near the patches")
            cands.sort(key=lambda t: t[0][3])
            best, ci = cands[-1] if D.knobs.get("core_alt") and len(cands) > 1 and _try == 0 else cands[0]
            k, ox, oy, _ = best
            ban = bans[ci]
            pc = cores[ci].rotated(k).at(ox, oy)
            zr = dict(pc.reserve)
            specs = []
            for zid, label, st in (("science", "Science: red (S3), labs (S2), green (S5)", 2),
                                   ("smelt_iron", "Iron smelting (S0)", 0),
                                   ("smelt_copper", "Copper smelting (S1)", 1)):
                pts = [p for key, p in pc.ports.items() if key.endswith("_i" if zid == "smelt_iron" else "_c")] \
                    if zid != "science" else []
                specs.append((zid, zid.split("_")[0], zr[zid], st, label, pts))
            if D.commit(specs, opens=list(pc.ports.values())):
                core = pc
                break
            ban.append((k, ox, oy))
        else:
            raise MasterError("the hub cannot be placed without walling in its own belt ports")
        D.hub = _center(dict(core.reserve)["science"])
        sci_ents = [e for e in core.ents if not _x(e).get("sub")]
        ci_ents = [e for e in core.ents if _x(e).get("sub") == "iron"]
        cc_ents = [e for e in core.ents if _x(e).get("sub") == "copper"]
        _tag_science(D, Part("sci", sci_ents, (0, 0), {}), "science", sci.info)
        _tag_column(D, Part("ci", ci_ents, (0, 0), {}), "smelt_iron", "iron-ore", 0, IRON["pairs0"], IRON["pairs"],
                    "W")
        _tag_column(D, Part("cc", cc_ents, (0, 0), {}), "smelt_copper", "copper-ore", 1, COPPER["pairs0"],
                    COPPER["pairs"], "W")
        # one mixed input per column (the mirror only swaps which lane carries the ore: the input inserters pick
        # from both lanes)
        col_ports = {"iron": (core.ports["ore_i"], None), "copper": (core.ports["ore_c"], None)}
        lab_pt = D.hub
        # per ore: the coal goes out to the field (its corridor feeds the burner drills until S4) and comes back on
        # the trunk's coal lane to the furnaces: a sequential chain on one belt each way, used in every stage
        D.chain_dist = {}
        for ore in ("iron", "copper"):
            p = col_ports[ore][0]
            vx, vy = DVEC[p.direction]
            ax, ay = p.x - vx * PORT_LANE, p.y - vy * PORT_LANE
            F, C = float(D.walk_dist(ore, ax, ay)), float(D.walk_dist("coal", ax, ay))
            D.chain_dist[ore] = {"field": round(F), "coal": round(C)}
            D.mode[ore] = "mixed"
    else:
        col_ports, lab_pt = {}, None
        D.hub = None

    # 2. fields (iron, copper): head toward their smelter
    fields = {}
    for ore, spec, patch, first in (("iron", IRON, D.iron, 0), ("copper", COPPER, D.copper, 1)):
        tgt = (col_ports[ore][0].x, col_ports[ore][0].y) if col_ports else _toward(patch, D.coal_pt)
        split = D.mode.get(ore) == "split"
        mixed = D.mode.get(ore) == "mixed"

        def fcommit(f, ore=ore, first=first, split=split, mixed=mixed):
            ps = list(f.ports.values())
            spec_z = [(f"mine_{ore}", "mine", _ents_rect(gd, f.ents), first,
                       f"{ore.title()} field (burner S{first}, electric S4)", ps)]
            if col_ports and (split or mixed):
                return D.commit(spec_z, opens=[ps[1]], pairs=[(ps[0], col_ports[ore][0])])
            if col_ports:
                return D.commit(spec_z, opens=[ps[1]], pairs=[(ps[0], col_ports[ore][0]), (ps[2], col_ports[ore][1])])
            return D.commit(spec_z, opens=ps)
        f = D.place_field(f"mine_{ore}", patch, spec["slots"], spec["burners"], tgt, commit=fcommit, head_feed=split,
                          mixed=mixed)
        _tag_field(D, f, f"mine_{ore}", f"{ore}-ore", first, spec["burners"], ore + "f" if split else ore,
                   head_feed=split)
        fields[ore] = f
    if D.variant == "onsite":
        for ore, spec, colb, first, side_a in (("iron", IRON, col_i, 0, "W"), ("copper", COPPER, col_c, 1, "E")):
            f = fields[ore]
            fps = list(f.ports.values())
            cpart = _part(colb, col_labels)
            vx, vy = DVEC[fps[0].direction]
            tgt = (fps[0].x + vx * 3, fps[0].y + vy * 3)

            def extra(rp, X, Y, fps=fps):
                c = rp.ports["coal"]                # the corridor exit (coal) should reach the coal port too
                return 0.5 * (np.abs(X + c.x - fps[2].x) + np.abs(Y + c.y - fps[2].y))

            def ccommit(pl, ore=ore, first=first, side_a=side_a, fps=fps):
                spec_z = [(f"smelt_{ore}", "smelt", _ents_rect(gd, pl.ents), first, f"{ore.title()} smelting "
                           f"(S{first})", [pl.ports["ore"], pl.ports["coal"], pl.ports["plate_W"], pl.ports["plate_E"]])]
                # only side A leaves the column (one plate belt to the science, split there for green at S5); side
                # B keeps its chests (plates for building)
                return D.commit(spec_z, opens=[pl.ports["plate_" + side_a]],
                                pairs=[(fps[0], pl.ports["ore"]), (fps[2], pl.ports["coal"])])
            placed = D.place_near(cpart, "ore", tgt, radius=50, extra=extra, commit=ccommit)
            if placed is None:
                raise MasterError(f"no room for the {ore} smelting column next to its field")
            _tag_column(D, placed, f"smelt_{ore}", f"{ore}-ore", first, spec["pairs0"], spec["pairs"], side_a,
                        b_green=False)
            col_ports[ore] = (placed.ports["ore"], placed.ports["coal"])
            D.parts[f"smelt_{ore}"] = placed
        # science next to the iron column's side-A plate exit
        ip = D.parts["smelt_iron"].ports["plate_W"]
        spart = _part(sci, ["cu", "fe", "gfe", "gcu"], [("science", (0, 0, sci.size[0], sci.size[1]))])
        cp = D.parts["smelt_copper"].ports["plate_E"]
        ip_b = D.parts["smelt_iron"].ports["plate_E"]
        cp_b = D.parts["smelt_copper"].ports["plate_W"]

        def sextra(rp, X, Y):
            c = rp.ports["cu"]
            return 0.6 * (np.abs(X + c.x - cp.x) + np.abs(Y + c.y - cp.y))

        def scommit(pl):
            zr = _rect_union([_ents_rect(gd, pl.ents)] + [r for _z, r in pl.reserve])
            return D.commit([("science", "science", zr, 2, "Science: labs (S2), red (S3), green (S5)",
                              [pl.ports[k] for k in ("cu", "fe", "gfe", "gcu")])],
                            pairs=[(ip, pl.ports["fe"]), (cp, pl.ports["cu"])])
        placed = D.place_near(spart, "fe", (ip.x, ip.y), radius=90, extra=sextra, ore_weight=3.0, commit=scommit)
        if placed is None:
            raise MasterError("no room for the science block")
        zr = D.zones[-1].rect
        _tag_science(D, placed, "science", sci.info)
        D.parts["science"] = placed
        D.hub = _center(zr)
        lab_pt = D.hub

    # 3. coal fields: one per chain (burner drills first, electric drills in the same slots from S4); the boilers'
    # coal belt is laid after the other belts (step 5)
    coal_fields = {}
    # the boilers' coal field keeps its burner drills for good (round-2 live finding: electric coal drills alone
    # cannot restart the boilers after a power dip - no power, no coal, no power); its free slots get electric drills
    # at S4 for the rest of the load
    nb_boiler = _coal_burners(BOILER_LOAD_S3_KW / 4000.0)
    ne_boiler = max(2 * nb_boiler, math.ceil(BOILER_LOAD_S5_KW / 4000.0 * COAL_MARGIN / 0.5))
    cspec = {}
    for ore, nc, ne, first, spec in (("iron", nc_iron, ne_iron, 0, IRON), ("copper", nc_cop, ne_cop, 1, COPPER)):
        fport = list(fields[ore].ports.values())[1]
        if D.mode.get(ore) == "split":
            # furnace coal straight to the column (burner drills sized for the S0/S1 furnaces, electric from S4);
            # a small burner-only coal field feeds the ore field's corridor until the electric drills (retired S4)
            nbk = _coal_burners(_chain_demand(0, 2 * spec["pairs0"]))
            cspec[ore] = (ore, nbk, ne, first, col_ports[ore][1])
            nbf = _coal_burners(_chain_demand(spec["burners"], 0))
            cspec[ore + "f"] = (ore + "f", nbf, 2 * nbf, first, fport)
        else:
            cspec[ore] = (ore, nc, ne, first, fport)
    order = [c for c in D.knobs.get("coal_order", ("iron", "copper", "boiler")) if c != "boiler"]
    order = [c2 for c in order for c2 in (c, c + "f") if c2 in cspec]

    def place_coal(chain, nb, ne, first, tgt):
        tt = (tgt.x, tgt.y) if tgt is not None else D.water_tile

        def kcommit(f):
            tail = list(f.ports.values())[-1]
            spec_z = [(f"coal_{chain}", "coal", _ents_rect(gd, f.ents), first, f"Coal for the {chain_label(chain)} "
                       f"(S{first})", list(f.ports.values()))]
            if tgt is not None:
                return D.commit(spec_z, pairs=[(tail, tgt)])
            return D.commit(spec_z, opens=[tail])
        f = D.place_field(f"coal_{chain}", D.coal, ne, nb, None, self_feed=True, tail_target=tt,
                          min_slots=2 * min(nb, 2), commit=kcommit)
        coal_fields[chain] = (f, nb, ne)
    for c in order:
        place_coal(*cspec[c])
    place_coal("boiler", nb_boiler, ne_boiler, 2, None)      # its belt is routed after every other one (step 5)
    for chain in coal_fields:
        if chain == "boiler":
            continue
        first = 0 if chain.startswith("iron") else 1
        _tag_field(D, coal_fields[chain][0], f"coal_{chain}", "coal", first, coal_fields[chain][1], chain,
                   coal_field=True, retire_at=4 if chain.endswith("f") else None)

    # 4. routes, in stage order (S0 iron chain, S1 copper chain, S3 plates for onsite)
    fi, fc = fields["iron"], fields["copper"]
    fiP, fcP = list(fi.ports.values()), list(fc.ports.values())
    tail = {c: list(coal_fields[c][0].ports.values())[-1] for c in coal_fields}
    nets = []
    for ore, fP, first in (("iron", fiP, 0), ("copper", fcP, 1)):
        if D.mode.get(ore) == "mixed":
            nets.append((f"{ore} ore + coal", fP[0], col_ports[ore][0], first, None, f"{ore}-ore"))
            nets.append((f"coal to the {ore} field", tail[ore], fP[1], first, ore, "coal"))
            continue
        nets.append((f"{ore} ore", fP[0], col_ports[ore][0], first, None, f"{ore}-ore"))
        if D.mode.get(ore) == "split":
            nets.append((f"coal to the {ore} furnaces", tail[ore], col_ports[ore][1], first, ore, "coal"))
            nets.append((f"coal to the {ore} field", tail[ore + "f"], fP[1], first, ore + "f", "coal", 4))
        else:
            nets.append((f"coal to the {ore} furnaces", fP[2], col_ports[ore][1], first, ore, "coal"))
            nets.append((f"coal to the {ore} field", tail[ore], fP[1], first, ore, "coal"))
    if D.variant == "onsite":
        sc = D.parts["science"]
        nets += [("iron plates", D.parts["smelt_iron"].ports["plate_W"], sc.ports["fe"], 3, None, "iron-plate"),
                 ("copper plates", D.parts["smelt_copper"].ports["plate_E"], sc.ports["cu"], 3, None,
                  "copper-plate")]
    # 5. power: a provisional spot first (its coal belt negotiated with the others, so it is never walled in), then,
    # with every other belt laid, the shore spots near the boilers' coal are tried with their coal belt routed for
    # real; a better one replaces the provisional block (poles carry the power anywhere for a few wood and cables)
    boiler_tail = list(coal_fields["boiler"][0].ports.values())[-1]
    prov = D.place_power(POWER_RESERVE, (boiler_tail.x, boiler_tail.y), pair_with=boiler_tail)
    nets.append(("coal to the boilers", boiler_tail, prov[0].ports["coal"], 2, "boiler", "coal"))
    deferred = _negotiate(D, nets, defer={"coal to the boilers"})
    placed, pump, pipe, prect = prov
    bbelts = deferred["coal to the boilers"]
    snap = (D.grid.copy(), list(D.zones), dict(D.lanes), list(D.open_ports), list(D.pairs))
    from .router import _occupy as r_occupy
    try:
        g = D.grid
        x0, y0, x1, y1 = prect
        g.release([(a, b) for a in range(x0, x1) for b in range(y0, y1)] +
                  [(math.floor(e.x), math.floor(e.y)) for e in bbelts] +
                  [t for e in [pump] + pipe for t in _tiles(gd, e)])
        D.zones = [z for z in D.zones if z.id != "power"]
        D.lanes[(boiler_tail.x, boiler_tail.y)] = []
        D.open_ports.append(boiler_tail)
        alt = D.place_power(POWER_RESERVE, (boiler_tail.x, boiler_tail.y), route_from=boiler_tail)
        if len(alt[4]) + 4 < len(bbelts):
            placed, pump, pipe, prect, bbelts = alt
            pc = placed.ports["coal"]
            own = D.lanes.pop((pc.x, pc.y), [])
            D.grid.release([q for q in own if str(D.grid.tag_at(*q) or "").startswith("lane:")])
            D.grid.release([(boiler_tail.x, boiler_tail.y), (pc.x, pc.y)])
            r_occupy(D.grid, bbelts, "route")
        else:
            raise MasterError("the provisional power spot is as good")
    except MasterError:
        D.grid, D.zones, D.lanes, D.open_ports, D.pairs = snap
    D.parts["power"] = placed
    pc = placed.ports["coal"]
    D._record("coal to the boilers", boiler_tail, pc, bbelts, 2, "boiler", None)
    D.note_cuts(bbelts, 2, "coal to the boilers")
    D.forget_ports([boiler_tail, pc])
    # belt index of every unit's inserter pickup (the belt runs from the coal port along the boiler row)
    belt_tiles = [e for e in placed.ents if _x(e).get("role") == "boiler_belt"]
    pin = placed.ports["coal"]

    def idx_of(e):
        return int(round(abs(e.x - 0.5 - pin.x) + abs(e.y - 0.5 - pin.y))) - 1
    for e in belt_tiles:
        e.extra["row"] = idx_of(e)
    placed.unit_pick = {}
    for e in placed.ents:
        x = _x(e)
        if x.get("role") == "boiler_ins":
            # the inserter picks from the belt tile behind it (its direction = pickup side)
            dx, dy = DVEC[e.direction]
            t = (math.floor(e.x + dx), math.floor(e.y + dy))
            bt = next(b for b in belt_tiles if (math.floor(b.x), math.floor(b.y)) == t)
            placed.unit_pick[x["unit"]] = _x(bt)["row"]
    D.add(pump, zone="power", stage=2, role="pump", chain="boiler")
    for p in pipe:
        D.add(p, zone="power", stage=2, role="water_pipe")
    for t in getattr(placed, "pipe_cuts", None) or []:
        D.cuts.setdefault(t, {"stage": 2, "route": "water pipe"})
    if D.variant == "onsite":
        # S5: a splitter on each plate belt near the science block (Logistics is researched by then) sends half of
        # it on to the green block, under the belts already laid where needed (round 3: one plate belt per metal,
        # not a second belt from the far column's other side)
        _branch_green(D, gd)
        D.stats_extra = {"green_feed": "split"}
    D.conns = _connectors(D, coal_fields)

    # 6. power units per stage, then the boiler coal field's burner drills per stage (burner for good: black start)
    unit_stage = {}
    for k in (2, 3, 4, 5):
        load = _stage_load_kw(gd, D, k) + (_extra_load(k))
        need = max(1, math.ceil(load * POWER_MARGIN / 1800))
        for u in range(need):
            unit_stage.setdefault(u, k)
    _tag_power(D, placed, "power", unit_stage)
    loads = {k: _stage_load_kw(gd, D, k) + _extra_load(k) for k in (2, 3, 4, 5)}
    expect = {k: _stage_load_kw(gd, D, k, expected=True) + _extra_load(k) for k in (2, 3, 4, 5)}
    n_b = {k: sum(1 for u, s in unit_stage.items() if s <= k) for k in loads}
    for e in D.ents:
        if e.name == "boiler":
            e.extra["load_kw"] = {str(k): round(expect[k] / max(1, n_b[k]), 1) for k in loads}
    bstages, have = [], 0
    for k in (2, 3):         # burners for the S2/S3 load: enough to restart the engines; electric drills add the rest
        need = _coal_burners(expect[k] / 4000.0)
        while have < need:
            bstages.append(k)
            have += 1
    cf = coal_fields["boiler"]
    n_slots_b = getattr(cf[0], "n_slots", cf[2])
    nb_b = min(len(bstages), (n_slots_b + 1) // 2)
    if len(bstages) > nb_b:
        D.warnings.append(f"the boiler coal field has room for {nb_b} burner drills; S{bstages[-1]} needs "
                          f"{len(bstages)}")
    _tag_field(D, cf[0], "coal_boiler", "coal", 2, nb_b, "boiler", coal_field=True, burn_stages=bstages[:nb_b],
               burner_forever=True, keep_burners=True)
    D.unit_stage = unit_stage
    D.loads = loads

    # 7. poles: in-zone covers per stage (science), then one network per stage joined by pole chains
    _poles(D, gd)

    mp = _finish(D, gd, world, t0)
    return mp


def slabel(k) -> str:
    """Stage label: S0..S5, connector stages S0b / S1b (index k + 0.5)."""
    return f"S{int(k)}" + ("b" if k != int(k) else "")


def chain_label(chain: str) -> str:
    """'iron' -> 'iron line', 'ironf' -> 'iron field' (the burner-phase coal of a split chain), 'boiler' -> 'boilers'."""
    if chain == "boiler":
        return "boilers"
    return f"{chain[:-1]} field" if chain.endswith("f") else f"{chain} line"


def _connectors(D: _Designer, coal_fields) -> dict:
    """Section 7.1: a coal leg longer than LONG_RUN becomes its own stage (S0b / S1b) right after the chain's
    first stage. Until it is built the chain's burners are hand-fed by the helper's round (fuel=manual until the
    connector) from a chest the coal field fills (burner inserter + chest at its tail, retired by the connector)."""
    out = {}
    for chain, first in (("iron", 0), ("copper", 1)):
        leg = next((r for r in D.routes if r["what"] == f"coal to the {chain} field"), None)
        if leg is None or leg["tiles"] <= LONG_RUN:
            continue
        split = D.mode.get(chain) == "split"
        fchain = chain + "f" if split else chain        # the chain whose burners hang on this leg
        conn = first + 0.5
        out[chain] = {"stage": conn, "tiles": leg["tiles"], "fe": leg["fe"]}
        leg["stage"] = conn
        moved = {f"coal to the {chain} field"}
        if not split:                 # sequential: the furnace coal comes on from the field's corridor
            moved.add(f"coal to the {chain} furnaces")
            for r in D.routes:
                if r["what"] == f"coal to the {chain} furnaces":
                    r["stage"] = conn
        for e in D.ents:
            x = e.extra
            role = x.get("role")
            if x.get("route") in moved:
                x["stage"] = conn
            elif x.get("zone") == f"mine_{chain}" and role in ("ins_b", "corr"):
                x["stage"] = conn
            elif (x.get("chain") == fchain and role in ("drill_b", "furnace", "in_b") and not x.get("coal_field")
                  and x.get("stage", 0) < conn):
                x["manual_until"] = conn
        f = coal_fields[fchain][0]
        tail = list(f.ports.values())[-1]
        vx, vy = DVEC[tail.direction]
        ins = D.add(PE("burner-inserter", tail.x + 0.5, tail.y + 0.5, (tail.direction + 8) % 16), zone=f"coal_{fchain}",
                    stage=first, until=conn, role="coal_ins", chain=fchain, slot=f"coal_{fchain}/out")
        chest = D.add(PE("wooden-chest", tail.x + vx + 0.5, tail.y + vy + 0.5, 0), zone=f"coal_{fchain}",
                      stage=first, until=conn, role="coal_chest", chain=fchain, slot=f"coal_{fchain}/out")
        # the leg's first two belt tiles take their place (it leaves the coal field straight: _route_net)
        at = {(math.floor(e.x), math.floor(e.y)): e for e in D.ents if e.extra.get("route") == f"coal to the {chain} field"}
        into = [at[t].extra["id"] for t in ((math.floor(ins.x), math.floor(ins.y)), (math.floor(chest.x),
                                                                                   math.floor(chest.y))) if t in at]
        D.swaps.append(Swap(f"coal_{fchain}/out", "retire", conn, [ins.extra["id"], chest.extra["id"]], into))
    return out


def _is_leg(name: str) -> bool:
    return name.startswith("coal to the") and name.endswith("field")


def _route_net(g, gd, name, s, t, item, *, others, tile_cost):
    """route_belt for one net of the negotiation. A coal leg to a field always leaves its coal field straight for
    two tiles: a connector stage parks a burner inserter (first tile) and a chest (second tile) there until the leg
    is built, and the leg's belts must take exactly their place (in-place swap, B1)."""
    if not _is_leg(name):
        return route_belt(g, gd, Port("belt_out", item, s.x, s.y, s.direction),
                          Port("belt_in", item, t.x, t.y, t.direction), allow_underground=False, others=others,
                          tag="route", max_nodes=600_000, tile_cost=tile_cost, occupy=False)
    vx, vy = DVEC[s.direction]
    gg = g.copy()
    gg.occupy([(s.x, s.y)], "lane:leg")
    belts = route_belt(gg, gd, Port("belt_out", item, s.x + vx, s.y + vy, s.direction),
                       Port("belt_in", item, t.x, t.y, t.direction), allow_underground=False, others=others,
                       tag="route", max_nodes=600_000, tile_cost=tile_cost, occupy=False)
    return [B._put(gd, "transport-belt", s.x, s.y, s.direction)] + belts


def _negotiate(D: _Designer, nets, iters=NEGOTIATE_ITERS, defer=()):
    """Negotiated congestion routing (PathFinder): every belt is routed on the blocks-only grid (other belts' port
    lanes blocked, its own open), paying for tiles other belts use now (present cost, doubled each round) and for
    tiles that were contested before (history cost); repeated until no tile is used twice. Non-overlapping belt
    paths cannot feed into each other (each belt points at the next tile of its own path), so the result is laid
    as is. nets: [(name, start Port, goal Port, stage, chain, item)]."""
    lanes = dict(D.lanes)
    others = list(D.ents)
    g0, cutc = D.cut_grid(D.grid)   # every port lane open: another belt's lane costs extra instead of being a wall
    lane_cost = {}
    hard = {}
    for i, (name, s, t, _st, _ch, _it, *_u) in enumerate(nets):
        for p in (s, t):
            for q in lanes.get((p.x, p.y), []) + [(p.x, p.y)]:
                lane_cost.setdefault(q, set()).add(i)
        if name.startswith("coal to the") and name.endswith("field"):   # the coal chest's tile (connector stage)
            vx, vy = DVEC[s.direction]
            hard[(s.x + vx, s.y + vy)] = i
    g0.release([q for q in lane_cost if g0.in_bounds(*q) and str(g0.tag_at(*q) or "").startswith("lane:")])
    g0.release([(p.x, p.y) for n in nets for p in (n[1], n[2])])
    shape = (g0.h, g0.w)
    hist = cutc.copy()
    paths = {}
    pres_fac = 0.5
    best_over, stall = 0, 0
    order = sorted(range(len(nets)), key=lambda i: _taxi((nets[i][1].x, nets[i][1].y), (nets[i][2].x, nets[i][2].y)))
    for it in range(iters):
        for i in order:
            name, s, t, _st, _ch, item = nets[i][:6]
            use = np.zeros(shape)
            for j, p in paths.items():
                if j != i:
                    for (x, y) in p["tiles"]:
                        use[y - g0.y0, x - g0.x0] += 1
            for q, owners in lane_cost.items():
                if i not in owners and g0.in_bounds(*q):
                    use[q[1] - g0.y0, q[0] - g0.x0] += LANE_COST / max(pres_fac, 1e-9) / (1 + it)
            g = g0.copy()
            g.occupy([q for q, o in hard.items() if o != i], "lane:hard")
            cost_ = hist + pres_fac * use * (1 + it)
            try:
                belts = _route_net(g, D.gd, name, s, t, item, others=others, tile_cost=cost_)
            except NoRoute as ex:
                raise MasterError(f"no belt route for {name}: {ex}") from ex
            paths[i] = {"belts": belts, "tiles": [(math.floor(b.x), math.floor(b.y)) for b in belts]}
        cnt = {}
        for i, p in paths.items():
            for q in p["tiles"]:
                cnt[q] = cnt.get(q, 0) + 1
                if q in lane_cost and i not in lane_cost[q] and _is_port_tile(q, nets):
                    cnt[q] += 1        # standing on another belt's port tile is never allowed
        over = [q for q, c in cnt.items() if c > 1]
        if not over:
            break
        best_over = min(best_over, len(over)) if it else len(over)
        stall = stall + 1 if it and len(over) >= best_over else 0
        if stall >= NEGOTIATE_STALL:
            fixed = _endgame(D, g0, nets, paths, over, lanes, lane_cost, hard, others, cutc)
            if fixed is not None:
                paths = fixed
                break
            D.last_negotiation = {"paths": paths, "over": over, "nets": nets}
            raise MasterError(f"the belts would have to cross ({len(over)} tiles); no layout without crossings")
        for (x, y) in over:
            hist[y - g0.y0, x - g0.x0] += 1.0
        pres_fac *= 1.6
    else:
        raise MasterError(f"the belts would have to cross ({len(over)} tiles); no layout without crossings")
    from .router import _occupy as r_occupy
    for i, (name, s, t, st, ch, item, *_u) in enumerate(nets):
        own = []
        for p in (s, t):
            own += D.lanes.pop((p.x, p.y), [])
        D.grid.release([q for q in own if str(D.grid.tag_at(*q) or "").startswith("lane:")])
        D.grid.release([(s.x, s.y), (t.x, t.y)])
    out = {}
    for i, (name, s, t, st, ch, item, *until) in enumerate(nets):
        r_occupy(D.grid, paths[i]["belts"], "route")
        if name in defer:       # the caller records it (or rips it up for a better one)
            out[name] = paths[i]["belts"]
            continue
        D._record(name, s, t, paths[i]["belts"], st, ch, until[0] if until else None)
        D.note_cuts(paths[i]["belts"], st, name)
    D.forget_ports([p for n in nets for p in (n[1], n[2])])
    return out
    for i, (name, s, t, st, ch, item, *_u) in enumerate(nets):   # coal chest tile of a coal leg stays free
        if name.startswith("coal to the") and name.endswith("field"):
            vx, vy = DVEC[s.direction]
            q = (s.x + vx, s.y + vy)
            if D.grid.state_at(*q) != BLOCKED:
                D.grid.occupy([q], "buffer")


def _route_late(D: _Designer, nets):
    """Belts of a stage after Logistics (S5): laid one by one on the grid with every earlier belt in place,
    underground pairs allowed (they cross the earlier belts)."""
    from .router import _occupy as r_occupy
    for name, s, t, st, ch, item, *until in nets:
        own = []
        for p in (s, t):
            own += D.lanes.pop((p.x, p.y), [])
        D.grid.release([q for q in own if str(D.grid.tag_at(*q) or "").startswith("lane:")])
        D.grid.release([(s.x, s.y), (t.x, t.y)])
        g, cc = D.cut_grid(D.grid)
        try:
            belts = route_belt(g, D.gd, Port("belt_out", item, s.x, s.y, s.direction),
                               Port("belt_in", item, t.x, t.y, t.direction), allow_underground=True,
                               others=list(D.ents), tag="route", max_nodes=600_000, occupy=False, tile_cost=cc)
        except NoRoute as ex:
            raise MasterError(f"no belt route for {name}: {ex}") from ex
        r_occupy(D.grid, belts, "route")
        D._record(name, s, t, belts, st, ch, until[0] if until else None)
        D.note_cuts(belts, st, name)
        D.forget_ports([s, t])


BRANCH_TRIES = 24           # splitter spots tried per plate belt (from the science end back)


def _branch_green(D: _Designer, gd):
    """On-site variant, S5: per metal a splitter replaces one straight tile of the S3 plate belt ("iron plates" /
    "copper plates", in place: the belt tile is the splitter's left or right half) and its other output is routed
    to the green block's port (underground pairs allowed: Logistics is researched). The spot nearest the science
    block whose branch is shortest wins. Raises MasterError when no spot works."""
    from .router import _occupy as r_occupy
    sc = D.parts["science"]
    g = D.grid
    for metal, rname, gkey in (("iron", "iron plates", "gfe"), ("copper", "copper plates", "gcu")):
        belts = [e for e in D.ents if _x(e).get("route") == rname]
        goal = sc.ports[gkey]
        item = f"{metal}-plate"
        own = D.lanes.pop((goal.x, goal.y), [])
        g.release([q for q in own if str(g.tag_at(*q) or "").startswith("lane:")] + [(goal.x, goal.y)])
        tiles = {(math.floor(b.x), math.floor(b.y)) for b in D.ents if b.name in ("transport-belt", "underground-belt")}
        best, tried = None, 0
        for i in range(len(belts) - 3, 0, -1):
            if tried >= BRANCH_TRIES:
                break
            b, pv, nx = belts[i], belts[i - 1], belts[i + 1]
            if {b.name, pv.name, nx.name} != {"transport-belt"} or not (pv.direction == b.direction == nx.direction):
                continue
            d = b.direction
            vx, vy = DVEC[d]
            tx, ty = math.floor(b.x), math.floor(b.y)
            for lx, ly in ((-vy, vx), (vy, -vx)):
                lat, out, back = (tx + lx, ty + ly), (tx + lx + vx, ty + ly + vy), (tx + lx - vx, ty + ly - vy)
                if not all(g.in_bounds(*t) and g.state_at(*t) != BLOCKED for t in (lat, out)) or back in tiles:
                    continue
                tried += 1
                spl = PE("splitter", tx + 0.5 + lx * 0.5, ty + 0.5 + ly * 0.5, d)
                gg = g.copy()
                gg.occupy([lat], "route:splitter")
                gc, cc = D.cut_grid(gg)
                try:
                    br = route_belt(gc, gd, Port("belt_out", item, out[0], out[1], d),
                                    Port("belt_in", item, goal.x, goal.y, goal.direction), allow_underground=True,
                                    others=list(D.ents) + [spl], tag="route", max_nodes=300_000, occupy=False,
                                    tile_cost=cc)
                except NoRoute:
                    continue
                cost_ = len(br) + 0.25 * (len(belts) - i)
                if best is None or cost_ < best[0]:
                    best = (cost_, b, spl, lat, out, br)
        if best is None:
            raise MasterError(f"no spot for the splitter that feeds the green block its {metal} plates")
        _c, b, spl, lat, out, br = best
        bx = _x(b)
        bx["until"] = 5
        bx["slot"] = f"route:{rname}/split"
        se = D.add(spl, zone=bx["zone"], stage=5, role="splitter", route=rname, slot=bx["slot"])
        D.swaps.append(Swap(bx["slot"], "replace", 5, [bx["id"]], [_x(se)["id"]]))
        g.occupy([lat], "route:splitter")
        r_occupy(g, br, "route")
        name = f"{metal} plates to green"
        D._record(name, Port("belt_out", item, out[0], out[1], b.direction), goal, br, 5, None, None)
        D.note_cuts(br, 5, name)
        D.forget_ports([goal])


def _endgame(D: _Designer, g0, nets, paths, over, lanes, lane_cost, hard, others, cutc=None):
    """Negotiation stalled with a few shared tiles: keep every belt that touches none of them, then lay the contested
    ones strictly one after the other (earlier ones are walls for later ones), trying their orders. -> paths or
    None."""
    import itertools as it_
    from .router import _occupy as r_occupy
    over = set(over)
    involved = [i for i, p in paths.items() if over & set(p["tiles"])]
    if len(involved) > 5:
        return None
    base = g0.copy()
    for i, p in paths.items():
        if i not in involved:
            r_occupy(base, p["belts"], "route")
    cost0 = cutc.copy() if cutc is not None else np.zeros((g0.h, g0.w))
    for orderp in it_.permutations(involved):
        g = base.copy()
        out = {i: p for i, p in paths.items() if i not in involved}
        ok = True
        for i in orderp:
            name, s, t, _st, _ch, item = nets[i][:6]
            cost_ = cost0.copy()
            for q, owners in lane_cost.items():
                if i not in owners and g.in_bounds(*q):
                    cost_[q[1] - g.y0, q[0] - g.x0] += LANE_COST
            gg = g.copy()
            gg.occupy([q for q, o in hard.items() if o != i], "lane:hard")
            # port tiles of nets not laid yet stay free for them
            gg.occupy([(p.x, p.y) for j in orderp if j != i and j not in out for p in (nets[j][1], nets[j][2])],
                      "lane:port")
            try:
                belts = _route_net(gg, D.gd, name, s, t, item, others=others, tile_cost=cost_)
            except NoRoute:
                ok = False
                break
            out[i] = {"belts": belts, "tiles": [(math.floor(b.x), math.floor(b.y)) for b in belts]}
            r_occupy(g, belts, "route")
        if ok:
            return out
    return None


def _is_port_tile(q, nets) -> bool:
    return any(q == (p.x, p.y) for n in nets for p in (n[1], n[2]))


def _extra_load(k) -> float:
    """Load not in the master's entities (none since the green block is laid out: its machines count as built)."""
    return 0.0


def _toward(patch, pt):
    return dist_point_patch(pt, patch)[0]


def _weighted_hub(D: _Designer):
    pts = [(dist_point_patch(D.coal_pt, D.iron)[0], 3.0), (dist_point_patch(D.coal_pt, D.copper)[0], 1.0),
           (D.coal_pt, 1.5), (D.water_tile, 1.0)]
    w = sum(p[1] for p in pts)
    return (sum(p[0][0] * p[1] for p in pts) / w, sum(p[0][1] * p[1] for p in pts) / w)


def _poles(D: _Designer, gd):
    """Science poles per stage (greedy cover on the block's free tiles), then per stage the pole networks of the
    power block, columns, science and fields are joined by chains of poles outside the zones."""
    sci = next(z for z in D.zones if z.id == "science")
    used = set()
    for e in D.ents:
        if _x(e).get("zone") == "science":
            used |= _tiles(gd, e)
    x0, y0, x1, y1 = sci.rect
    cands =[(x, y) for x in range(x0, x1) for y in range(y0, y1) if (x, y) not in used]
    G = gd
    placed_poles = []
    for k in (2, 3, 4, 5):
        needs = [e for e in D.ents if _x(e).get("zone") == "science" and _x(e).get("stage") == k
                 and (G.entity(e.name).get("energy_source") or {}).get("type") == "electric"]
        needs = [e for e in needs if not any(B.powered_by(G, p, e) for p in placed_poles)]
        if not needs:
            continue
        free = [c for c in cands if c not in {(math.floor(p.x), math.floor(p.y)) for p in placed_poles}]
        try:
            new = B._cover(G, "small-electric-pole", needs, free)
        except ValueError as ex:
            raise MasterError(f"science block: {ex}") from ex
        for p in new:
            placed_poles.append(p)
            D.add(p, zone="science", stage=k, role="sci_pole")
    # networks per stage: join every pole of stage <= k into one network (chains outside the zones)
    for k in (2, 3, 4, 5):
        src = [e for e in D.ents if e.name == "small-electric-pole" and alive(e, k)]
        if not src:
            continue
        try:
            chain = place_poles(D.grid, gd, [], "small-electric-pole", sources=[PE(e.name, e.x, e.y, 0) for e in src],
                                tag="pole")
        except NoRoute as ex:
            raise MasterError(f"S{k}: the pole network cannot be joined: {ex}") from ex
        for p in chain:
            D.add(p, zone=f"poles:s{k}", stage=k, role="link_pole")


def _finish(D: _Designer, gd, world, t0) -> MasterPlan:
    stages = []
    for idx, key, title, gate, after, unlocks in STAGES:
        stages.append(StageSpec(idx, key, title, list(gate), after, unlocks,
                                [{"tech": t, "source": src} for t, st, src in RESEARCH_PATH if st == idx + 1]))
    for chain, c in (getattr(D, "conns", None) or {}).items():
        k = c["stage"]
        stages.append(StageSpec(k, f"connect_{chain}", f"Coal belt to the {chain} field", [], int(k),
                                f"coal reaches the {chain} drills and furnaces by belt, so the helper stops carrying "
                                f"it ({c['tiles']} belt tiles, {c['fe']} iron)"))
    stages.sort(key=lambda s: s.index)
    for s in stages:   # a main stage after a connector waits for it
        if s.index == int(s.index) and s.after is not None:
            prev = [x.index for x in stages if x.index < s.index]
            s.after = max(prev) if prev else None
    h = hashlib.sha1(json.dumps([[e.name, e.x, e.y] for e in D.ents[:400]]).encode()).hexdigest()[:6]
    mp = MasterPlan(id=f"m{D.variant[:3]}-{h}", variant=D.variant, title=TITLES[D.variant], hub=D.hub,
                    zones=D.zones, ents=D.ents, swaps=D.swaps, stages=stages, routes=D.routes,
                    warnings=list(D.warnings), surface=world.surface or "nauvis")
    mp.cuts = []
    for t, c in sorted((getattr(D, "cuts", None) or {}).items()):
        name, x, y, _d = D.cuttable[t]
        st = next((r["stage"] for r in D.routes if r["what"] == c["route"]), c["stage"])   # connector stages move
        mp.cuts.append({"name": name, "x": x, "y": y, "stage": st, "route": c["route"]})
    mp.power = {"units": {str(u): s for u, s in sorted(D.unit_stage.items())}, "loads_kw": {
        f"S{k}": round(v) for k, v in D.loads.items()}, "boilers_reserved": 10}
    total = sum(r["tiles"] for r in D.routes)
    longest = max((r["tiles"] for r in D.routes), default=0)
    mp.stats = {"belt_tiles_routes": total, "longest_run": longest, "seconds": round(time.monotonic() - t0, 1),
                "patches": {"iron": [round(v) for v in D.iron.center], "copper": [round(v) for v in D.copper.center],
                            "coal": [round(v) for v in D.coal.center]}, "near": list(D.near),
                "chains": dict(D.mode), "chain_dist": getattr(D, "chain_dist", {}),
                **(getattr(D, "stats_extra", None) or {})}
    if total > FAR_APART:
        only = [nice(r).replace(" ore", "") for r, n in (getattr(D, "n_patches", None) or {}).items() if n == 1]
        mp.warnings.append(f"Ore is far apart: the connections need {total} belt tiles "
                           f"({round(total * cost.C_BELT)} iron)" +
                           (f"; there is no closer {' or '.join(only)} patch in the scanned area." if only else "."))
    for r in D.routes:
        if r["tiles"] > LONG_RUN:
            mp.warnings.append(f"{r['what']}: {r['tiles']} belt tiles in one run ({r['fe']} iron).")
    mp.research_path = research_path(gd)
    return mp


def research_path(gd) -> list:
    out = []
    techs = gd.raw.get("technologies") or {}
    for t, st, src in RESEARCH_PATH:
        tt = techs.get(t) or {}
        trig = tt.get("research_trigger")
        out.append({"tech": t, "stage": st, "source": "trigger" if trig else src,
                    "packs": 0 if trig else int(tt.get("unit_count") or 0),
                    "unit_s": (tt.get("unit_energy") or 600) / 60.0, "done": bool(tt.get("researched")),
                    "trigger": TRIGGER_TEXT.get(t) if trig else None})
    return out


def gate_open(gd, gate) -> bool:
    techs = gd.raw.get("technologies") or {}
    return all((techs.get(t) or {}).get("researched") for t in gate)


# ---- validation of every stage (7.3) ---------------------------------------------------------------------------------
GREEN_STAGE = 5                          # S5: the green block (validated like every other stage)


def stage_of(mp: MasterPlan, k) -> StageSpec:
    return next(s for s in mp.stages if s.index == k)


def plan_id(mp: MasterPlan, k) -> str:
    return f"{mp.id}-{slabel(k).lower()}"


def validate_stage(mp: MasterPlan, gd, world, k: int):
    """validate() of the whole base after stage k against the map (existing builds as context; entities of this
    master plan that already stand in the world, or are ghosts of it, are the plan's own, not obstacles; the
    player's idle belt tiles the stages up to k cut are gone)."""
    return validate(stage_entities(mp, k), gd, world_without_master(world, mp, k), {"assume_research": True})


def world_without_master(world, mp: MasterPlan, k=None):
    """Copy of a World without the force entities (and ghosts) standing exactly where the master plan has the same
    entity (earlier stages that are built already), and without the player's belt tiles the master cuts in stages
    <= k (all cuts when k is None)."""
    mine = {(e.name, float(e.x), float(e.y)) for e in mp.ents}
    mine |= {(c["name"], float(c["x"]), float(c["y"])) for c in (mp.cuts or []) if k is None or c["stage"] <= k}
    if not any(((e.name[6:] if e.name.startswith("ghost:") else e.name), float(e.x), float(e.y)) in mine
               for e in world.entities):
        return world
    keep, remap = [], {}
    for i, e in enumerate(world.entities):
        name = e.name[6:] if e.name.startswith("ghost:") else e.name
        if (name, float(e.x), float(e.y)) in mine:
            continue
        remap[i] = len(keep)
        keep.append(e)
    w = copy.copy(world)
    w.entities = keep
    w._chunk_ent = {c: [remap[i] for i in idx if i in remap] for c, idx in world._chunk_ent.items()}
    return w


def check_all(mp: MasterPlan, gd, world) -> dict:
    """B1 for one master plan: every stage validates, the plan is monotone, the self-running check per stage."""
    out = {"stages": {}, "monotone": check_monotone(mp, gd), "swap_counts": swap_counts(mp)}
    for k in [s.index for s in mp.stages]:
        rep = validate_stage(mp, gd, world, k)
        sr = selfrun(mp, gd, k, rep)
        out["stages"][k] = {"ok": rep.ok, "errors": [i.to_dict() for i in rep.errors[:12]],
                            "n_errors": len(rep.errors), "warnings": len(rep.warnings), "selfrun": sr,
                            "entities": sum(1 for e in mp.ents if alive(e, k)), "report": rep.to_dict()}
    out["ok"] = not out["monotone"] and all(v["ok"] for v in out["stages"].values())
    return out


# ---- bill / ETA per stage (8.3, simplified until round B calibrates it) ----------------------------------------------
def _site(ents):
    if not ents:
        return None
    return (sum(e.x for e in ents) / len(ents), sum(e.y for e in ents) / len(ents))


def research_minutes(gd, techs, labs, pack_rate_s=None, hand_pack_s=5.5) -> float:
    """Minutes to research `techs` (not yet done) with `labs` labs: packs made by hand (hand_pack_s each) or by
    assemblers (pack_rate_s packs/s), whichever is given; triggers count 0 here (their stage makes them happen)."""
    tt = gd.raw.get("technologies") or {}
    total = 0.0
    for t in techs:
        d = tt.get(t) or {}
        if d.get("researched") or d.get("research_trigger"):
            continue
        n = int(d.get("unit_count") or 0)
        unit = (d.get("unit_energy") or 600) / 60.0
        lab_s = n * unit / max(1, labs)
        make_s = n / pack_rate_s if pack_rate_s else n * hand_pack_s
        total += max(lab_s, make_s) if pack_rate_s else lab_s + make_s
    return total / 60.0


ZONE_ORDER = ("coal", "mine", "smelt", "route", "power", "science", "poles")


def _zone_rank(zid: str) -> int:
    for i, p in enumerate(ZONE_ORDER):
        if zid.startswith(p):
            return i
    return len(ZONE_ORDER)


def _helper_coal(mp: MasterPlan, gd, k) -> tuple:
    """(coal/min the helper can take from coal chests, coal/min the hand-fed burners burn) at stage k."""
    fed = {(_x(e).get("chain")) for e in mp.ents if alive(e, k) and _x(e).get("manual_until") is not None
           and k < _x(e)["manual_until"]}
    if not fed:
        return 0.0, 0.0
    bud = coal_budget(mp, gd, k)
    sup = sum(bud[c]["supply"] for c in fed if c in bud)
    dem = sum(bud[c]["demand"] for c in fed if c in bud)
    return sup * 60, dem * 60


GATHER_SHARE = 0.6        # of the helper's pure hand-mining rate left once it walks to and between the patches
ROUTE_TILE_S = 1.0        # helper seconds per tile of a belt run on top of placing it (built box by box along it)
HELPER_FRESH = round(GATHER_SHARE * 28.0, 2)   # a helper with no mining measured yet (28/min pure, real-save copy)


def helper_gather_rate(state) -> float:
    """Raw items/min the helper gathers for a v3 stage. Its life average (mined / (mining + walking)) counts every
    walk it made while BUILDING (round 2: 4.4/min on the real-save copy while it mined 28/min whenever it mined),
    so the pure mining rate x GATHER_SHARE, never below the default and capped."""
    if not state:
        return HELPER_FRESH
    s = state.helper_stats or {}
    mined = sum((s.get("mined") or {}).values())
    mining_min = (s.get("mining_ticks") or 0) / 3600.0
    if mining_min >= 1.0 and mined > 0:
        return round(max(cost.H_DEFAULT, min(25.0, GATHER_SHARE * mined / mining_min)), 2)
    # no mining measured yet (a fresh helper): the pure hand-mining rate it showed on the real-save copy (~28/min)
    # x GATHER_SHARE, not the v2 life average H_DEFAULT (round 3 live: S0 offered at 59.5 min with 8.5/min, built in
    # 24.4; the next stage's offer, with the helper's own 17.7/min, came within 50 %)
    return max(HELPER_FRESH, state.helper_rate())


def roadmap_eta(mp: MasterPlan, gd, state: BaseState, horizon=600.0, salvage_clusters=None, takes=None,
                status=None, scale=None, craft_parallel=None) -> dict:
    """Per stage {bill, short, salvage, eta: {start, gate_open, complete, first_output, build}} in minutes from now:
    ONE cost.simulate run over the whole roadmap (the v2 ETA engine): the helper gathers what is short at its
    measured rate, plates and hand-fed coal come from the stages already running, research waits are steps of their
    own (hand-made packs for Automation, packs from the red assemblers after S3). Stages that status (stage_status)
    calls done are standing already: they cost nothing and produce from minute 0. eta.build = complete - start (the
    stage's own build time). salvage_clusters / takes (default: the master's own review, section 11): the player's
    idle builds S0 takes apart, coal S0 takes out of idle machines."""
    status = status or {}
    scale = ETA_SCALE if scale is None else scale
    craft_parallel = CRAFT_PARALLEL if craft_parallel is None else craft_parallel
    H = helper_gather_rate(state)
    # stock: what the helper and the player carry, and the chests near them - and the master's own plate buffer and
    # coal chests wherever they are (the helper takes plates from those first: stage_plan's take_from)
    pts = [(e.x, e.y) for e in mp.ents if _x(e).get("role") in ("buffer", "coal_chest")]
    stock = dict(state.stock(points=pts)) if state else {}
    rv = mp.stats.get("review") or {}
    if takes is None:
        takes = rv.get("takes") or []
    for r in takes:
        cost._add(stock, r[4], r[5])
    if salvage_clusters is None:
        salv0 = dict(rv.get("salvage") or {})
        dis0 = float(rv.get("dismantle_s") or 0.0)
    else:
        salv0, dis0 = {}, 0.0
        for c in salvage_clusters:
            for k, v in c.items(gd).items():
                cost._add(salv0, k, v)
            dis0 += c.removal_s()
    pos = state.pos if state else None
    steps, info = [], {}
    prev_rates, prev_coal = {}, (0.0, 0.0)
    prev = None
    for s in mp.stages:
        k = s.index
        new = new_in(mp, k)
        gone = removed_in(mp, k)
        if status.get(k) == "done":     # standing already: its output runs from now, nothing to build
            rates = stage_rates(mp, gd, k)
            coal = _helper_coal(mp, gd, k)
            adds = {kk: v - prev_rates.get(kk, 0) for kk, v in rates.items() if abs(v - prev_rates.get(kk, 0)) > 1e-9}
            if coal[0] != prev_coal[0]:
                adds["coal"] = coal[0] - prev_coal[0]
            burns = {"coal": coal[1] - prev_coal[1]} if coal[1] != prev_coal[1] else {}
            steps.append(cost.Step(f"{slabel(k)} done", adds=adds, burns=burns))
            info[k] = {"bill": {}, "salvage": {}, "done": True}
            prev_rates, prev_coal, prev = rates, coal, k
            continue
        labs = sum(1 for e in mp.ents if alive(e, prev) and e.name == "lab") if prev is not None else 0
        red_s = (stage_rates(mp, gd, prev).get("automation-science-pack", 0) / 60.0) if prev is not None else 0
        if s.gate and not gate_open(gd, s.gate):
            tt = gd.raw.get("technologies") or {}
            items, secs = {}, 0.0
            for t in s.gate:
                d = tt.get(t) or {}
                if d.get("researched") or d.get("research_trigger"):
                    continue
                n = int(d.get("unit_count") or 0)
                unit = (d.get("unit_energy") or 600) / 60.0
                lab_s = n * unit / max(1, labs)
                if red_s:
                    secs += max(lab_s, n / red_s)
                else:
                    cost._add(items, "automation-science-pack", n)
                    secs += lab_s
            steps.append(cost.Step(f"{slabel(k)} research", items=items, extra_s=secs, kind="research"))
        groups = {}
        for e in new:
            groups.setdefault(_x(e).get("zone", ""), []).append(e)
        salvage = cost.entity_items(gd, entity_counts(gone)) if gone else {}
        dis = sum(existing_s(e) for e in gone)
        cuts = [c for c in mp.cuts or [] if c["stage"] == k]
        if cuts:       # the player's idle belt tiles this stage's belts cross: dismantled first, the belts reused
            cost._add(salvage, "transport-belt", len(cuts))
            dis += len(cuts) * (0.5 + cost.REACH_S)
        if prev is None and (salv0 or dis0):      # S0: the player's idle builds it takes apart
            for kk, v in salv0.items():
                cost._add(salvage, kk, v)
            dis += dis0
        if salvage:     # dismantling comes first: its parts are what the stage is built from
            steps.append(cost.Step(f"{slabel(k)} salvage", salvage=dict(salvage), dismantle_s=dis, kind="dismantle"))
        for zid in sorted(groups, key=lambda z: (_zone_rank(z), z)):
            es = groups[zid]
            # a belt run is built box by box along its length (round 3 live, real-save copy: S0b's 166-tile coal leg
            # took 7.7 min against 4.5 predicted, ~1 s more per tile than a compact block)
            extra = ROUTE_TILE_S * len(es) if zid.startswith("route:") else 0.0
            steps.append(cost.Step(f"{slabel(k)} {zid}", items=cost.bill(es, gd), site=_site(es), n_entities=len(es),
                                   extra_s=extra))
        rates = stage_rates(mp, gd, k)
        coal = _helper_coal(mp, gd, k)
        adds = {kk: v - prev_rates.get(kk, 0) for kk, v in rates.items() if abs(v - prev_rates.get(kk, 0)) > 1e-9}
        if coal[0] != prev_coal[0]:
            adds["coal"] = coal[0] - prev_coal[0]
        burns = {"coal": coal[1] - prev_coal[1]} if coal[1] != prev_coal[1] else {}
        steps.append(cost.Step(f"{slabel(k)} done", adds=adds, burns=burns))
        info[k] = {"bill": cost.bill(new, gd), "salvage": salvage}
        prev_rates, prev_coal, prev = rates, coal, k
    sim = cost.simulate(steps, stock, gd, H=H, start=pos, horizon=horizon / scale, craft_parallel=craft_parallel)
    when = {}
    for t, name in sim["timeline"]:
        when.setdefault(name, round(t * scale, 1))
    out = {}
    t_prev = 0.0
    have = dict(stock)
    for s in mp.stages:
        k = s.index
        lab = slabel(k)
        if info[k].get("done"):
            out[k] = {"bill": {}, "salvage": {}, "short": {}, "done": True,
                      "eta": {"start": 0.0, "gate_open": 0.0, "complete": 0.0, "first_output": 0.0, "build": 0.0}}
            continue
        names = [st.name for st in steps if st.name.startswith(lab + " ")]
        starts = [when[n] for n in names if n in when]
        done = when.get(f"{lab} done")
        gate = when.get(f"{lab} research")
        c = cost.cover(info[k]["bill"], have, gd)
        start = min(starts) if starts else None
        out[k] = {"bill": {kk: round(v) for kk, v in sorted(info[k]["bill"].items(), key=lambda kv: -kv[1])},
                  "salvage": info[k]["salvage"],
                  "short": {kk: round(v) for kk, v in c.short.items() if v >= 0.5},
                  "eta": {"start": start,
                          "gate_open": (round(gate + _step_min(steps, f"{lab} research"), 1)
                                        if gate is not None else t_prev),
                          "complete": done, "first_output": round(done + cost.WARMUP, 1) if done is not None else None,
                          "build": round(done - start, 1) if done is not None and start is not None else None}}
        have = dict(c.left)
        t_prev = done if done is not None else t_prev
    for k in out:       # what the estimate rests on (calibration: tests/live_stages.py compares it with the game)
        if not out[k].get("done"):
            out[k]["sim"] = {"helper_rate": H, "gathered": sim.get("gathered"), "craft_min": sim.get("craft_min")}
    if sim.get("first_plates") is not None and 0 in out and not out[0].get("done"):
        out[0]["eta"]["first_output"] = round(sim["first_plates"] * scale, 1)
    return out


# the v2 ETA engine (cost.simulate) on v3 stages, against the real helper on the real-save copy (round 2, B2): measured
# / predicted build minutes 0.59, 0.73, 0.90, 0.73, 0.64, 0.40 for S0..S4 (the helper takes plates from the running
# base's chests and salvages a lot; the v2 calibration was a from-scratch start). Scaled by the median.
ETA_SCALE = 1.0
CRAFT_PARALLEL = False

FUEL_TAKE = {"idle_no_input", "idle_no_power", "output_full", "disconnected", "idle_no_fuel"}
REMOVE_GAIN_MIN = 3.0          # section 11: one threshold, always applied (no "tidy" wording shortcut)
REMOVE_GAIN_FRAC = 0.10
REMOVE_MAX = 12                # clusters per review
REMOVE_CAP = 0.5               # at most this share of the player's non-belt entities / belt tiles


def review(mp: MasterPlan, world, gd, state, allow=False) -> dict:
    """Rebuild review (section 11), always run: coal S0 takes out of the player's idle machines, and the idle
    clusters outside the master's zones that S0 takes apart because their parts make S0 sooner by at least
    max(REMOVE_GAIN_MIN, REMOVE_GAIN_FRAC x S0's time) each. Labs, full chests and working builds are never taken
    apart, nor a belt line the master already cuts. allow=False (the player wants to keep their builds): nothing
    is taken apart; what would have been is listed as could_remove with its gain. Stored in mp.stats["review"]
    (used by roadmap_eta and stage_plan(S0)); returns it."""
    from . import existing
    state = state or BaseState.fresh_start()
    cls = existing.clusters(world, gd, state)
    takes = []
    for c in cls:
        if c.status in FUEL_TAKE:
            for e in c.ents:
                n = int((e.fuel or {}).get("coal", 0))
                if n >= 1:
                    takes.append([e.name, e.x, e.y, e.unit, "coal", n])
    zone_rects = [z.rect for z in mp.zones]

    def in_zone(c):
        x0, y0, x1, y1 = c.bbox
        return any(x0 < r[2] and r[0] < x1 and y0 < r[3] and r[1] < y1 for r in zone_rects)
    cut = {(math.floor(c["x"]), math.floor(c["y"])) for c in mp.cuts or []}
    cands = [c for c in cls if not c.keep_only and not in_zone(c) and c.ents
             and not any(e.type == "lab" for e in c.ents)
             and not any((math.floor(e.x), math.floor(e.y)) in cut for e in c.ents)
             and (c.status.startswith("idle") or c.status in ("output_full", "disconnected")
                  or (c.status == "passive" and c.kind == "belt_line"))]
    non_belt = sum(len(c.machines) for c in cls) or 1
    belts = sum(c.belt_tiles for c in cls) or 1
    t0 = roadmap_eta(mp, gd, state, salvage_clusters=[], takes=takes)[0]["eta"]["complete"] or 180.0
    chosen, t_cur, rows = [], t0, []
    for c in sorted(cands, key=lambda c: -c.fe(gd) / max(c.removal_s(), 1.0)):
        if len(chosen) >= REMOVE_MAX:
            break
        if (sum(len(x.machines) for x in chosen) + len(c.machines)) > REMOVE_CAP * non_belt or \
                (sum(x.belt_tiles for x in chosen) + c.belt_tiles) > REMOVE_CAP * belts:
            continue
        t = roadmap_eta(mp, gd, state, salvage_clusters=chosen + [c], takes=takes)[0]["eta"]["complete"] or 180.0
        gain = t_cur - t
        if gain >= max(REMOVE_GAIN_MIN, REMOVE_GAIN_FRAC * t_cur):
            chosen.append(c)
            rows.append({"cluster": c.id, "label": c.label(), "gps": c.gps(), "stage": 0, "status": c.status,
                         "gain_min": round(gain, 1), "reason": f"idle ({c.status.replace('_', ' ')}); its parts build "
                                                               f"S0 {round(gain, 1)} min sooner"})
            t_cur = t
    could, could_gain, could_take = [], 0.0, 0
    if not allow:            # listed, not done: the player keeps everything (their machines' coal too)
        could, could_gain = rows, round(t0 - t_cur, 1)
        chosen, rows, t_cur = [], [], t0
        could_take = sum(r[5] for r in takes)
        if takes:
            t0 = roadmap_eta(mp, gd, state, salvage_clusters=[], takes=[])[0]["eta"]["complete"] or t0
            t_cur = t0
            takes = []
    salv, dis = {}, 0.0
    for c in chosen:
        for k, v in c.items(gd).items():
            cost._add(salv, k, v)
        dis += c.removal_s()
    out = {"takes": takes, "removes": rows, "remove_rows": [r for c in chosen for r in c.rows()],
           "could_remove": could, "could_gain": could_gain, "could_take_coal": could_take, "allowed": bool(allow),
           "salvage": salv, "dismantle_s": round(dis, 1), "s0_min_before": round(t0, 1), "s0_min_after": round(t_cur, 1),
           "checked": len(cands), "kept": [{"label": c.label(), "gps": c.gps(), "status": c.status} for c in cls
                                           if c not in chosen and len(c.machines) >= 3][:6],
           "coal_taken": sum(r[5] for r in takes)}
    mp.stats["review"] = out
    mp.removes = rows
    return out


def _step_min(steps, name) -> float:
    st = next((s for s in steps if s.name == name), None)
    return (st.extra_s / 60.0) if st else 0.0


def existing_s(e) -> float:
    return 0.5 + cost.REACH_S


# ---- stage status, cards and the brief (section 12) ------------------------------------------------------------------
def stage_status(mp: MasterPlan, gd, world=None) -> dict:
    """{k: done | now | ready | waiting | later}. done = every entity of the stage stands in the world already."""
    built = set()
    if world is not None:
        for e in world.entities:
            if not e.name.startswith("ghost:"):
                built.add((e.name, float(e.x), float(e.y)))
    out = {}
    now = None

    raw = {}       # stage k: everything it places for good stands (its temporary entities may be gone already)
    for s in mp.stages:
        new = new_in(mp, s.index)
        raw[s.index] = bool(new) and all((e.name, float(e.x), float(e.y)) in built for e in new
                                         if _x(e).get("until") is None)
    for s in mp.stages:
        k = s.index
        new = new_in(mp, k)
        # an entity a later stage retires (S0's coal chest, retired by S0b) counts as built once that stage is done
        done = bool(new) and all((e.name, float(e.x), float(e.y)) in built or
                                 (_x(e).get("until") is not None and raw.get(_x(e)["until"]))
                                 for e in new)
        if done:
            out[k] = "done"
            continue
        prev_ok = s.after is None or out.get(s.after) == "done" or (now is not None and s.after == now)
        if now is None:
            now = k
            out[k] = "now" if gate_open(gd, s.gate) else "waiting"
        elif gate_open(gd, s.gate) and prev_ok:
            out[k] = "ready"
        else:
            out[k] = "waiting" if not gate_open(gd, s.gate) else "later"
    return out


GATE_SHORT = {"steam-power": "Steam power", "electronics": "Electronics", "automation": "Automation",
              "electric-mining-drill": "Electric mining drill", "logistic-science-pack": "Logistic science pack",
              "logistics": "Logistics"}
SHORT_TITLE = {0: "iron+coal", 1: "copper", 2: "power+labs", 3: "red science", 4: "electric drills",
               5: "green science"}
# the one place the helper inserts science packs (owner's decision D1, PLANNER-v3 section 13): no assembler exists
# before Automation, so its 10 red packs are crafted by hand from mined plates and put into a lab, as a player would
D1_TEXT = ("Exception: Automation's 10 red packs are hand-made and put in a lab (no assembler exists before it); "
           "every later pack comes from the assemblers.")


def short_title(s) -> str:
    if s.key.startswith("connect_"):
        return f"coal belt to {s.key[8:]}"
    return SHORT_TITLE.get(s.index, s.key)


def roadmap_line(mp: MasterPlan, status: dict, eta: dict | None = None) -> str:
    """'Roadmap: S0 iron+coal ~8m (now) > S0b coal belt to iron ~15m > ...': every stage with the minutes from now
    until it is built (roadmap_eta), its status when it is done / now / ready; stages behind research say so in
    the Plans tab roadmap rows and the brief (gate)."""
    parts = []
    for s in mp.stages:
        txt = f"{slabel(s.index)} {short_title(s)}"
        e = ((eta or {}).get(s.index) or {}).get("eta") or {}
        st = status.get(s.index)
        if st == "done":
            txt += " (done)"
        else:
            if e.get("complete") is not None:
                txt += f" ~{fmt_n(e['complete'])}m"
            if st in ("now", "ready"):
                txt += f" ({st})"
        parts.append(txt)
    return "Roadmap: " + " > ".join(parts)


def roadmap_label(mp: MasterPlan, k, eta: dict | None = None) -> str:
    """One Plans-tab roadmap row (PLANNER-v3 12.2): 'S3 Automate red science (Central hub): ~10 min to build,
    needs 160 iron plate, 75 copper plate, 20 gear | replaces 4 plate chests'."""
    s = stage_of(mp, k)
    e = (eta or {}).get(k) or {}
    txt = f"{slabel(k)} {s.title} ({mp.title})"
    build = (e.get("eta") or {}).get("build")
    bill = e.get("bill") or {}
    bits = []
    if build is not None:
        bits.append(f"~{fmt_n(build)} min to build")
    if bill:
        bits.append("needs " + ", ".join(f"{fmt_n(v)} {nice(n)}" for n, v in list(bill.items())[:3]))
    txt += (": " + ", ".join(bits)) if bits else ""
    gone = entity_counts(removed_in(mp, k))
    if gone:
        txt += " | replaces in place " + ", ".join(f"{n} {nice(name)}" for name, n in list(gone.items())[:3])
    return txt


def self_running_from(mp: MasterPlan, gd) -> int | None:
    """The first stage from which the base runs with the helper idle (static selfrun), or None."""
    for s in mp.stages:
        sr = selfrun(mp, gd, s.index)
        if sr["ok"] and not sr["needs_helper"]:
            return s.index
    return None


def card(mp: MasterPlan, gd, eta: dict, status: dict, now: int, recommended: bool = False, why=None) -> str:
    """At most CARD_LINES short lines (PLANNER-v3 12.1, round 3: plain words, no option names or jargon):
    1 title + one-line trade-off; 2 the stage now: ready in ~N min (the same figure as its roadmap entry), what it
    makes, what is short, belt tiles of the whole plan; 3 the roadmap with minutes from now per stage; 4 what of the
    player's it touches (nothing unless they allowed it), from when it runs alone, the hand-made-packs exception;
    5 the loudest warning."""
    e = eta.get(now, {})
    rates = stage_rates(mp, gd, now)
    total = sum(r["tiles"] for r in mp.routes)
    short = e.get("short") or {}
    need_txt = ("needs " + ", ".join(f"{fmt_n(v)} {nice(k)}" for k, v in list(short.items())[:2]) + " more"
                if short else "you have everything")
    out_txt = ", ".join(f"{fmt_n(v)} {nice(k).replace(' plate', '').replace('science pack', 'packs')}/min"
                        for k, v in rates.items() if k in ("iron-plate", "copper-plate", "automation-science-pack",
                                                           "logistic-science-pack"))
    head = f"{mp.title} - {TRADEOFF[mp.variant]}."
    if recommended:
        head = f"{mp.title} (recommended{' ' + why if why else ''}) - {TRADEOFF[mp.variant]}."
    now_title = stage_of(mp, now).title
    l2 = (f"Now {slabel(now)} {now_title}: ready in ~{fmt_n((e.get('eta') or {}).get('complete') or 0)} min, "
          f"{out_txt or 'no output yet'}; {need_txt}. Whole plan: {total} belt tiles.")
    l3 = roadmap_line(mp, status, eta)
    rv = mp.stats.get("review") or {}
    parts = []
    if rv.get("removes"):
        n = sum(1 for _ in rv.get("remove_rows") or [])
        parts.append(f"S0 takes apart {len(rv['removes'])} idle builds of yours ({n} entities, "
                     f"{fmt_n(rv['s0_min_before'] - rv['s0_min_after'])} min sooner)")
    if mp.cuts:
        parts.append(f"cuts {len(mp.cuts)} tiles of your idle belts where new belts cross")
    if rv.get("coal_taken"):
        parts.append(f"takes {rv['coal_taken']} coal out of your idle machines")
    if parts:
        l4 = "With your OK: " + "; ".join(parts) + "."
    else:
        l4 = "Keeps everything of yours"
        if rv.get("could_remove"):
            l4 += (f" (taking {len(rv['could_remove'])} idle builds apart would make S0 "
                   f"{fmt_n(rv.get('could_gain') or 0)} min sooner: ask if you want that)")
        l4 += "."
    srf = self_running_from(mp, gd)
    l4 += (f" Runs without the helper from {slabel(srf)}." if srf is not None else
           " Needs the helper's coal round at every stage.")
    if any(r["tech"] == "automation" and not r.get("done") for r in mp.research_path):
        l4 += " " + D1_TEXT
    lines = [head, l2, l3, l4]
    if mp.warnings:
        lines.append(mp.warnings[0])
    return "\n".join(lines[:CARD_LINES])


def reserved_rows(mp: MasterPlan, k: int) -> list:
    """stats.reserved: zones a later stage still builds in (rendered as outlines, the helper keeps off them)."""
    out = []
    for z in mp.zones:
        later = any(_x(e).get("zone") == z.id and _x(e).get("stage", 0) > k for e in mp.ents) or z.first_stage > k
        if later:
            out.append({"id": z.id, "kind": z.kind, "rect": list(z.rect), "stage": z.first_stage, "label": z.label})
    return out[:64]


def stage_plan(mp: MasterPlan, k: int, world, gd, state=None, eta=None, status=None, validation=None) -> Plan:
    """Stage k as an ordinary Plan: its new entities as ghosts, its swaps as removals, validated as the whole base
    after stage k (closed: every belt ends in a consumer, every burner has a fuel path, every electric entity a pole
    path to a generator of a stage <= k)."""
    s = stage_of(mp, k)
    ents = new_in(mp, k)
    vdict = validation if validation is not None else validate_stage(mp, gd, world, k).to_dict()
    removes = [[e.name, e.x, e.y, None, f"{slabel(k)} {SWAP_KIND_WORD.get(_swap_kind(mp, e), 'swap')}"]
               for e in removed_in(mp, k)]
    removes += [[c["name"], c["x"], c["y"], None, f"{slabel(k)}: the {c['route']} belt crosses your idle belt here"]
                for c in mp.cuts or [] if c["stage"] == k]
    rv = mp.stats.get("review") or {}
    if k == mp.stages[0].index and rv.get("remove_rows"):   # S0: the idle builds the review takes apart
        removes += [list(r) for r in rv["remove_rows"]]
    eta = eta or {}
    st = eta.get(k, {})
    rates = stage_rates(mp, gd, k)
    stats = {"master": {"id": mp.id, "stage": k, "key": s.key, "stages": [x.key for x in mp.stages]},
             "gate": s.gate, "gate_open": gate_open(gd, s.gate), "reserved": reserved_rows(mp, k),
             "reserve": [list(r["rect"]) for r in reserved_rows(mp, k)],
             "eta": {"first_plates": st.get("eta", {}).get("first_output"), "complete": st.get("eta", {}).get("complete"),
                     "start": st.get("eta", {}).get("start"), "build": st.get("eta", {}).get("build")},
             "bill": {"need": st.get("bill"), "short": st.get("short"), "salvage": st.get("salvage")},
             "per_min": rates, "belt_tiles": sum(len([e for e in ents if e.name == "transport-belt"]) for _ in [0]),
             "swap_counts": swap_counts(mp).get(slabel(k), {}), "connect_existing": True, "assume_research": True,
             "stage_label": f"{slabel(k)} {s.title}", "status": (status or {}).get(k)}
    xs = [e.x for e in mp.ents if alive(e, k)] or [0]
    ys = [e.y for e in mp.ents if alive(e, k)] or [0]
    stats["bbox"] = [math.floor(min(xs)) - 2, math.floor(min(ys)) - 2, math.ceil(max(xs)) + 2, math.ceil(max(ys)) + 2]
    if k == mp.stages[0].index and rv.get("takes"):
        stats["takes"] = [list(r) for r in rv["takes"]]
    chests = [[e.x, e.y] for e in mp.ents if alive(e, k) and _x(e).get("role") == "coal_chest"]
    if chests:   # the helper's round takes coal for the hand-fed burners here
        stats["coal_from"] = chests
    # the plates of the stages already running: the helper takes them from the plate buffer chests first, wherever
    # they are (on a far-apart map it would otherwise mine and smelt by hand next to the build: live 4004 run)
    bufs = [[e.x, e.y] for e in mp.ents if _x(e).get("role") == "buffer" and _x(e).get("stage", 0) < k
            and (_x(e).get("until") is None or _x(e)["until"] >= k)]
    if bufs:
        stats["take_from"] = bufs[:64]
    # this stage's poles are wired to the earlier stages' poles explicitly (a ghost pole auto-connects to the nearest
    # pole only, which may be a player's)
    stats["wire_with"] = [[e.x, e.y] for e in mp.ents if e.name == "small-electric-pole" and alive(e, k)
                          and _x(e).get("stage", 0) < k][:1000]
    # earlier stages' hand-fed burners stay in the helper's round while this stage is built and after
    keep = [[e.name, e.x, e.y, _x(e).get("prime") or _x(e).get("fuel_n") or HAND_FILL.get(e.name, 10),
             1 if _x(e).get("prime") and _x(e).get("fuel") != "manual" else 0]
            for e in stage_entities(mp, k)
            if _x(e).get("stage", 0) < k and (_x(e).get("fuel") == "manual" or _x(e).get("prime"))]
    if keep:
        stats["keep_fuelled"] = keep[:500]
    summary = stage_summary(mp, k, st, rates)
    p = Plan(id=plan_id(mp, k), title=f"{mp.title}: {slabel(k)} {s.title}", variant=mp.variant, stage=s.key,
             summary=summary, entities=ents, warnings=list(mp.warnings[:2]), validation=vdict, stats=stats,
             surface=mp.surface, removes=removes)
    return p


SWAP_KIND_WORD = {"upgrade": "upgrade", "replace": "replace", "retire": "retire"}
HAND_FILL = {"burner-mining-drill": 50, "stone-furnace": 30, "burner-inserter": 5, "boiler": 50}   # plans.lua FILL


def _swap_kind(mp, e):
    i = _x(e).get("id")
    return next((s.kind for s in mp.swaps if i in s.out), None)


def stage_summary(mp: MasterPlan, k: int, st: dict, rates: dict) -> str:
    s = stage_of(mp, k)
    new = new_in(mp, k)
    gone = removed_in(mp, k)
    cnt = entity_counts(new)
    top = ", ".join(f"{n} {nice(name)}" for name, n in list(cnt.items())[:4])
    lines = [f"{slabel(k)} {s.title}: {top}.", f"Unlocks: {s.unlocks}."]
    if gone:
        g = entity_counts(gone)
        lines.append("Replaces in place: " + ", ".join(f"{n} {nice(name)}" for name, n in g.items()) + ".")
    if s.gate:
        lines.append("Needs research: " + ", ".join(s.gate) + ".")
    e = st.get("eta") or {}
    if e:
        lines.append(f"Done in ~{fmt_n(e.get('complete', 0))} min from now.")
    return "\n".join(lines)


def brief(mp: MasterPlan, gd, eta: dict, status: dict, recommended=False) -> dict:
    """Section 12.5: the roadmap for Claude (no entity dump). Every eta is minutes from now (stages already built
    count 0); eta.build is the stage's own build time."""
    now = next((k for k, v in status.items() if v in ("now", "waiting")), 0)
    rows = []
    last = mp.stages[-1].index
    for s in mp.stages:
        k = s.index
        e = eta.get(k, {})
        sr = selfrun(mp, gd, k)
        rows.append({"stage": slabel(k), "index": k, "key": s.key, "title": s.title, "status": status.get(k),
                     "gate": s.gate, "gate_eta_min": (e.get("eta") or {}).get("gate_open"),
                     "bill": {"need": e.get("bill"), "short": e.get("short")}, "eta": e.get("eta"),
                     "places": entity_counts(new_in(mp, k)), "removes": entity_counts(removed_in(mp, k)),
                     "cuts": sum(1 for c in mp.cuts or [] if c["stage"] == k),
                     "swaps": {kind: n for kind, n in _swap_n(mp, k).items()}, "unlocks": s.unlocks,
                     "per_min_after": stage_rates(mp, gd, k), "plan_id": plan_id(mp, k),
                     "self_running": bool(sr["ok"] and not sr["needs_helper"]), "needs_helper": sr["needs_helper"],
                     "helper_does": [i for i in sr["issues"] if "helper" in i][:2]})
    total = sum(r["tiles"] for r in mp.routes)
    srf = self_running_from(mp, gd)
    rv = mp.stats.get("review") or {}
    return {"master": mp.id, "variant": mp.variant, "title": mp.title, "tradeoff": TRADEOFF[mp.variant],
            "recommended": recommended, "now": slabel(now), "far_apart": total > FAR_APART, "roadmap": rows,
            "self_running_from": slabel(srf) if srf is not None else None,
            "end_state": {"per_min": stage_rates(mp, gd, last),
                          "power_mw": round(power_budget(mp, gd, last)["generation_kw"] / 1000, 1),
                          "belts": {"total": total, "longest": max((r["tiles"] for r in mp.routes), default=0),
                                    "retired_at_S4": sum(r["tiles"] for r in mp.routes if r.get("until") == 4)},
                          "chains": mp.stats.get("chains"),
                          "zones": [{"kind": z.kind, "label": z.label, "stage": z.first_stage} for z in mp.zones]},
            "science": science_note(mp, gd),
            "exceptions": [D1_TEXT] if any(r["tech"] == "automation" and not r.get("done")
                                           for r in mp.research_path) else [],
            "research_path": mp.research_path, "warnings": mp.warnings[:3],
            "removal_allowed": bool(rv.get("allowed")),
            "takes_apart": {"clusters": len(rv.get("removes") or []), "entities": len(rv.get("remove_rows") or []),
                            "gain_min": round((rv.get("s0_min_before") or 0) - (rv.get("s0_min_after") or 0), 1),
                            "could_remove": len(rv.get("could_remove") or []), "could_gain_min": rv.get("could_gain"),
                            "could_take_coal": rv.get("could_take_coal") or 0, "belt_tiles_cut": len(mp.cuts or [])},
            "review": {k: rv.get(k) for k in ("removes", "coal_taken", "s0_min_before", "s0_min_after", "kept",
                                              "checked")}}


LAB_UNIT_S = (10, 15, 20, 30)        # seconds per research unit of the red / red + green techs of a starter base


def science_note(mp: MasterPlan, gd) -> dict:
    """What the labs use of the packs the assemblers make, per research speed (round 3: 5 labs consumed 30 red/min
    only on 10 s techs; the card promised 30 on every tech), and how much of the plate capacity the science uses
    (the fields and columns are sized for one full ore lane of iron: the rest backs up or fills plate chests for
    building)."""
    out = {}
    for k, label in ((3, "after_S3"), (max(s.index for s in mp.stages), "end")):
        r = stage_rates(mp, gd, k)
        labs = sum(1 for e in stage_entities(mp, k) if e.name == "lab")
        red = r.get("automation-science-pack", 0)
        green = r.get("logistic-science-pack", 0)
        per = {}
        for u in LAB_UNIT_S:
            cap = labs * 60.0 / u                    # pack sets per minute the labs consume
            per[f"{u}s"] = round(min(red, cap), 1) if not green else {"red": round(min(red, cap), 1),
                                                                      "green": round(min(green, cap), 1)}
        out[label] = {"labs": labs, "red_per_min": red, "green_per_min": green, "labs_use_per_unit_time": per}
    end = stage_rates(mp, gd, max(s.index for s in mp.stages))
    red, green = end.get("automation-science-pack", 0), end.get("logistic-science-pack", 0)
    out["plates"] = {"iron_per_min": end.get("iron-plate"), "copper_per_min": end.get("copper-plate"),
                     "science_uses_iron": round(2 * red + 5.5 * green, 1),
                     "science_uses_copper": round(red + 1.5 * green, 1),
                     "note": "the smelting is sized for one full ore lane of iron; what the science does not use "
                             "backs up (on-site: fills the plate chests of the columns' far side, for building)"}
    return out


def _swap_n(mp, k) -> dict:
    out = {}
    for s in mp.swaps:
        if s.stage == k:
            out[s.kind] = out.get(s.kind, 0) + 1
    return out


SCORE_BELT = 0.15          # score per belt tile of the whole plan: ~7 belt tiles weigh as much as 1 minute sooner


def score(mp: MasterPlan, eta: dict) -> float:
    """Lower is better: half the minutes to the first plates + half the minutes to automated red science + 0.15 per
    belt tile of the end state (round 3: the base is used for hours; a compact plan costs less iron and walking and
    is easier to extend, so ~7 belt tiles count as much as one minute sooner; before, 22 tiles did and a 650-tile
    sprawl beat a 475-tile hub)."""
    s0 = (eta.get(0, {}).get("eta") or {}).get("first_output") or 180
    s3 = (eta.get(3, {}).get("eta") or {}).get("complete") or 180
    belts = sum(r["tiles"] for r in mp.routes)
    return round(0.5 * s0 + 0.5 * s3 + SCORE_BELT * belts, 1)


def master_options(world, gd, state=None, goal=None, variants=VARIANTS, allow_removal=False) -> list:
    """Every variant as a full master plan with its roadmap, best first: [(MasterPlan, info)] where info =
    {eta, status, now, card, check, score, recommended}. Variants that cannot be built are left out with a reason
    in info of the others (key 'not_offered')."""
    out, reasons = [], []
    for v in variants:
        try:
            # a new belt may cut the player's IDLE belt lines where it crosses them (a few tiles, listed on the card
            # and in the stage's removals, approved with the stage); taking whole builds apart or coal out of their
            # machines needs allow_removal (round 3: off by default)
            mp = design_master(world, gd, state, goal, v, allow_cut=True)
        except MasterError as ex:
            reasons.append(f"{TITLES.get(ALIASES.get(v, v), v)}: {ex}")
            continue
        chk = check_all(mp, gd, world)
        if not chk["ok"]:
            bad = chk["monotone"][:1] or [e for st in chk["stages"].values() for e in st["errors"][:1]]
            reasons.append(f"{mp.title}: does not validate ({(bad[0].get('text') or bad[0].get('msg')) if bad else '?'})")
            continue
        review(mp, world, gd, state, allow=allow_removal)
        status = stage_status(mp, gd, world)
        eta = roadmap_eta(mp, gd, state or BaseState.fresh_start(), status=status)
        now = next((k for k, v in status.items() if v in ("now", "waiting")), 0)
        info = {"eta": eta, "status": status, "now": now, "check": chk, "score": score(mp, eta)}
        out.append((mp, info))
    # proportional belts (M4): a variant whose belts wind far around (a belt over twice its straight distance and
    # 80+ tiles longer) is not offered when another variant does without
    for mp, info in out:
        info["detours"] = detours(mp)
    clean = [t for t in out if not t[1]["detours"]]
    if clean and len(clean) < len(out):
        for mp, info in out:
            if info["detours"]:
                d = info["detours"][0]
                reasons.append(f"{mp.title}: its {d['what']} belt would wind {d['tiles']} tiles for a straight "
                               f"distance of {d['straight']}")
        out = clean
    out.sort(key=lambda t: t[1]["score"])
    for i, (mp, info) in enumerate(out):
        info["recommended"] = i == 0
        info["not_offered"] = reasons
        info["why"] = why_recommended(out[0], out[1] if len(out) > 1 else None) if i == 0 else None
        info["card"] = card(mp, gd, info["eta"], info["status"], info["now"], recommended=i == 0, why=info["why"])
    if not out:
        raise MasterError("no master plan works here: " + "; ".join(reasons))
    return out


def why_recommended(best, other) -> str | None:
    """One sentence: why the recommended master beats the runner-up (belt tiles, red science, first plates)."""
    if other is None:
        return None
    (mb, ib), (mo, io) = best, other
    bb, bo = sum(r["tiles"] for r in mb.routes), sum(r["tiles"] for r in mo.routes)

    def t(info, k, key="complete"):
        return ((info["eta"].get(k) or {}).get("eta") or {}).get(key)
    parts = []
    if bo - bb >= 10:
        parts.append(f"{bo - bb} fewer belt tiles")
    elif bb - bo >= 10:
        parts.append(f"{bb - bo} more belt tiles")
    r_b, r_o = t(ib, 3), t(io, 3)
    if r_b is not None and r_o is not None:
        d = round(r_o - r_b)
        parts.append(f"red science {d} min sooner" if d >= 2 else f"red science {-d} min later" if d <= -2 else
                     "red science at about the same time")
    f_b, f_o = t(ib, 0, "first_output"), t(io, 0, "first_output")
    if f_b is not None and f_o is not None and abs(f_o - f_b) >= 2:
        parts.append(f"first plates {round(abs(f_o - f_b))} min {'sooner' if f_b < f_o else 'later'}")
    return f"over {mo.title}: " + ", ".join(parts) if parts else None


DETOUR_FACTOR, DETOUR_MIN = 2.0, 80


def detours(mp: MasterPlan) -> list:
    """Routes that wind far around: tiles > max(DETOUR_FACTOR x straight, straight + DETOUR_MIN)."""
    out = []
    for r in mp.routes:
        straight = _taxi(r["from"], r["to"])
        if r["tiles"] > max(DETOUR_FACTOR * straight, straight + DETOUR_MIN):
            out.append({"what": r["what"], "tiles": r["tiles"], "straight": straight})
    return sorted(out, key=lambda d: d["straight"] - d["tiles"])
