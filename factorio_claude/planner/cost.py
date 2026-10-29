"""Material bill, stock, salvage and the ETA model of planner v2 (docs/design/PLANNER-v2.md sections 4.2-4.4).

Everything is counted in ITEMS first and raw materials second: a belt already in an inventory covers a belt in the
plan; only what is still missing is expanded through recipes down to raw "ore equivalents"
(iron-ore, copper-ore, stone, coal, wood), where a plate costs 1 ore + 0.072 coal and a brick 2 stone.

    need = bill(plan.entities, gd)                   # {item: n} incl. start fuel
    c = cover(need, stock, gd)                       # what stock covers, what is short (raw), hand-craft seconds
    eta = simulate(steps, stock, gd, H=8.5)          # minutes to first plates / target rate / complete
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---- constants (section 3 / 4.3; tests pin them) ----------------------------------------------------------------
WALK = 8.9              # character running speed, tiles/s
PLACE_S = 0.4           # helper seconds per placed entity (reach, place, small walks)
REACH_S = 0.3           # helper overhead per mined (dismantled) entity on top of its mining time
H_DEFAULT = 8.5         # helper end-to-end gather rate (raw units/min: mine, walk, smelt), measured 2026-10
H_MIN_SAMPLE_MIN = 3.0  # minutes of helper samples before its measured rate replaces the default
PLATE_COAL = 0.072      # coal per plate in a stone furnace (90 kW x 3.2 s / 4 MJ)
DT = 0.5                # simulation step, minutes
HORIZON = 180.0         # minutes simulated; ETAs beyond are None
WARMUP = 0.5            # minutes from placing a machine to its first output
START_FUEL = 5          # coal put into each new burner entity
BOILER_FUEL = 20        # coal put into each new boiler
CARRY = 1000            # items the helper carries per logistics trip
RAW = ("iron-ore", "copper-ore", "stone", "coal", "wood")
# smelting: item -> {raw input: amount} (furnace work, not hand crafting)
SMELT = {"iron-plate": {"iron-ore": 1.0, "coal": PLATE_COAL}, "copper-plate": {"copper-ore": 1.0, "coal": PLATE_COAL},
         "stone-brick": {"stone": 2.0, "coal": PLATE_COAL}, "steel-plate": {"iron-plate": 5.0, "coal": 5 * PLATE_COAL}}
FE_WEIGHT = {"iron-ore": 1.0, "copper-ore": 0.8, "stone": 0.3, "coal": 0.2, "wood": 0.3}
# priorities for transported flows (section 6.1)
PRIORITY = {"iron-ore": 1.5, "copper-ore": 1.0, "coal": 0.8, "stone": 0.3}
C_BELT = 1.5            # Fe per belt tile
C_POLE = 0.07           # Cu-ish per tile of pole line
LANE_PER_MIN = 450      # items/min on one yellow lane


def _add(d, k, v):
    if v:
        d[k] = d.get(k, 0) + v


def item_of(gd, entity: str) -> str:
    """The item that places `entity` (and that picking it up gives back)."""
    try:
        its = gd.entity(entity).get("items_to_place_this") or []
    except KeyError:
        its = []
    if its:
        first = its[0]
        return first[0] if isinstance(first, (list, tuple)) else first.get("name", entity)
    return entity


def _energy_type(gd, entity):
    try:
        return (gd.entity(entity).get("energy_source") or {}).get("type")
    except KeyError:
        return None


def entity_items(gd, counts: dict) -> dict:
    """{entity name: n} -> {item: n}."""
    out = {}
    for name, n in counts.items():
        _add(out, item_of(gd, name), n)
    return out


def bill(entities, gd, fuel=True) -> dict:
    """Items for `entities` (PE list or {name: n}) plus the coal to start their burners."""
    counts = entities if isinstance(entities, dict) else _counts(entities)
    out = entity_items(gd, counts)
    if fuel:
        coal = 0
        for name, n in counts.items():
            try:
                typ = gd.entity(name).get("type")
            except KeyError:
                continue
            if typ == "boiler":
                coal += BOILER_FUEL * n
            elif _energy_type(gd, name) == "burner":
                coal += START_FUEL * n
        _add(out, "coal", coal)
    return out


def _counts(entities) -> dict:
    out = {}
    for e in entities:
        _add(out, e.name, 1)
    return out


def _recipe(gd, item):
    r = gd.raw["recipes"].get(item)
    if r and not r.get("hidden") and r.get("ingredients") and "smelting" not in (r.get("categories") or []):
        return r
    return None


@dataclass
class Cover:
    used: dict = field(default_factory=dict)        # stock items consumed
    short: dict = field(default_factory=dict)       # raw (or unmakeable) items still missing
    craft_s: float = 0.0                            # hand-crafting seconds for what gets crafted
    left: dict = field(default_factory=dict)        # stock after covering

    @property
    def ok(self) -> bool:
        return not any(v > 1e-6 for v in self.short.values())


def cover(need: dict, have: dict, gd, depth=14) -> Cover:
    """Cover `need` items from `have`: same item first, then through recipes (hand crafting), smelting (plates from
    ore) down to raw. Fractions are kept (coal per plate)."""
    left = {k: v for k, v in (have or {}).items() if v > 0}
    c = Cover(left=left)
    todo = {k: v for k, v in need.items() if v > 0}
    for _ in range(depth):
        nxt = {}
        for item, n in todo.items():
            take = min(n, left.get(item, 0))
            if take > 0:
                left[item] -= take
                _add(c.used, item, take)
                n -= take
            if n <= 1e-9:
                continue
            if item in SMELT:
                for raw, k in SMELT[item].items():
                    _add(nxt, raw, n * k)
                continue
            r = None if item in RAW else _recipe(gd, item)
            if r is None:
                _add(c.short, item, n)
                continue
            prod = next((p.get("amount", 1) for p in r.get("products") or [] if p.get("name") == item), 1) or 1
            crafts = math.ceil(n / prod - 1e-9)
            c.craft_s += crafts * float(r.get("energy") or 0.5)
            for ing in r["ingredients"]:
                if ing.get("type", "item") == "item":
                    _add(nxt, ing["name"], crafts * ing.get("amount", 1))
        todo = nxt
        if not todo:
            break
    for k, v in todo.items():  # depth exhausted: count as short
        _add(c.short, k, v)
    c.left = {k: v for k, v in left.items() if v > 1e-9}
    c.short = {k: round(v, 3) for k, v in c.short.items() if v > 1e-6}
    return c


def raw_of(items: dict, gd) -> dict:
    """Raw ore equivalents of `items` (nothing in stock)."""
    return cover(items, {}, gd).short


def fe_equiv(raw: dict) -> float:
    """Scoring weight of raw materials: Fe + 0.8 Cu + 0.3 stone + 0.2 coal (+ 0.3 wood); other items count 1."""
    return round(sum(v * FE_WEIGHT.get(k, 1.0) for k, v in raw.items()), 2)


def shortfall(need: dict, have: dict, gd) -> dict:
    return cover(need, have, gd).short


# ---- ETA simulation (section 4.3) ---------------------------------------------------------------------------------
@dataclass
class Step:
    """One build step of a plan, in payback order."""
    name: str
    items: dict = field(default_factory=dict)      # consumed when the step is built (new entities + start fuel)
    site: tuple | None = None                      # where the helper works
    n_entities: int = 0                            # placed entities (helper PLACE_S each)
    adds: dict = field(default_factory=dict)       # items/min produced once running (e.g. iron-plate)
    burns: dict = field(default_factory=dict)      # items/min consumed once running (coal)
    dismantle_s: float = 0.0                       # helper seconds mining existing entities
    salvage: dict = field(default_factory=dict)    # items gained by the dismantling
    extra_s: float = 0.0                           # other helper/lab time (research)
    kind: str = "build"                            # build | repair | dismantle | research


def walk_s(a, b) -> float:
    if a is None or b is None:
        return 0.0
    return (abs(a[0] - b[0]) + abs(a[1] - b[1])) * 0.85 / WALK  # taxicab x ~0.85 ~ real paths with diagonals


def simulate(steps, stock: dict, gd, H: float = H_DEFAULT, start=None, existing_rates=None, target=None,
             round_share: float = 0.0, horizon=HORIZON, dt=DT) -> dict:
    """Discrete simulation (dt minutes) of the helper building `steps` in order from `stock`.
    target: {item: per_min} the plan's rate goal (eta.target). round_share: share of the helper's time spent on the
    logistics round once the first step runs (carrying plates/coal between sites). -> {first_plates, target,
    complete (minutes or None beyond the horizon), timeline: [(minute, step name)], gathered: {raw: n}}."""
    have = {k: float(v) for k, v in (stock or {}).items() if v > 0}
    rates = {}                       # plan production (items/min) once active
    burns = {}
    ext = dict(existing_rates or {})
    pending = []                     # (t_active, adds, burns)
    t, busy_until, pos = 0.0, 0.0, start
    first, tgt, done_t = None, None, None
    timeline, gathered = [], {}
    target = target or {}
    i = 0
    started = False

    fuel = [0.0]                     # coal inside the running burners (their start fuel)

    def produce(dt_):
        # burners run on their own fuel first, then on coal in stock; production scales with the coal supply
        need = sum(v for k, v in burns.items()) * dt_
        coal = fuel[0] + have.get("coal", 0) + rates.get("coal", 0) * dt_
        f = 1.0 if need <= 1e-9 else max(0.0, min(1.0, coal / need))
        for k, v in rates.items():
            _add(have, k, v * dt_ * (f if k != "coal" else 1.0))
        use = need * f
        inside = min(fuel[0], use)
        fuel[0] -= inside
        have["coal"] = max(0.0, have.get("coal", 0) - (use - inside))
        for k, v in ext.items():
            _add(have, k, v * dt_)
        return f

    while t < horizon:
        while pending and pending[0][0] <= t + 1e-9:
            _ta, adds, brn, start_fuel = pending.pop(0)
            for k, v in adds.items():
                _add(rates, k, v)
            for k, v in brn.items():
                _add(burns, k, v)
            fuel[0] += start_fuel
        f = produce(dt)
        if first is None and rates.get("iron-plate", 0) * f > 0:
            first = t
        if tgt is None and target and all(rates.get(k, 0) * f >= v * 0.999 for k, v in target.items()):
            tgt = t
        if i >= len(steps):
            if done_t is None:
                done_t = max(t, busy_until)
            if (first is not None or not any("iron-plate" in s.adds for s in steps)) and (tgt is not None or not target):
                break
            # nothing left to build: the helper keeps the burners fed (its refuel round)
            if t + 1e-9 >= busy_until and burns and fuel[0] + have.get("coal", 0) < 1 and                     rates.get("coal", 0) < burns.get("coal", 0):
                _add(have, "coal", H * dt)
                _add(gathered, "coal", H * dt)
            t += dt
            continue
        if t + 1e-9 < busy_until:
            t += dt
            continue
        s = steps[i]
        c = cover(s.items, have, gd)
        if c.ok:
            have = dict(c.left)
            dur = (c.craft_s + PLACE_S * s.n_entities + walk_s(pos, s.site) + s.dismantle_s + s.extra_s) / 60.0
            pos = s.site or pos
            for k, v in (s.salvage or {}).items():
                _add(have, k, v)
            busy_until = t + dur
            if s.adds or s.burns:
                pending.append((busy_until + WARMUP, dict(s.adds), dict(s.burns),
                                float(s.items.get("coal", 0)) if s.burns else 0.0))
                pending.sort(key=lambda p: p[0])
            timeline.append((round(t, 1), s.name))
            started = True
            i += 1
            continue  # the next step may start at once if nothing is short (no time passes)
        # gather the scarcest raw (by ore-equivalent weight), coal first when the running burners are starving
        share = 1.0 - (round_share if started else 0.0)
        if any(k not in RAW for k in c.short):  # something nobody can gather (e.g. a science-gated item)
            t = horizon
            break
        budget = H * dt * share
        short = dict(c.short)
        if burns and fuel[0] + have.get("coal", 0) < 1 and rates.get("coal", 0) < burns.get("coal", 0):
            short = {"coal": max(short.get("coal", 0), budget)}  # running burners starve: coal first
        while budget > 1e-9 and short:  # the scarcest raw first; time left over goes to the next one
            raw = max(short, key=lambda k: short[k] * FE_WEIGHT.get(k, 1.0))
            amt = min(budget, short.pop(raw))
            _add(have, raw, amt)
            _add(gathered, raw, amt)
            budget -= amt
        t += dt
    if done_t is None and i >= len(steps):
        done_t = max(t, busy_until)

    def m(v):
        return None if v is None or v > horizon else round(v, 1)
    return {"first_plates": m(first), "target": m(tgt), "complete": m(done_t), "timeline": timeline,
            "gathered": {k: round(v) for k, v in gathered.items()}, "assumes": "helper only, player not counted",
            "helper_rate": H}


def helper_rate(stats: dict | None) -> float:
    """Measured helper gather rate (raw units/min) when there are enough samples, else H_DEFAULT."""
    if not stats:
        return H_DEFAULT
    mined = sum((stats.get("mined") or {}).values())
    ticks = (stats.get("mining_ticks") or 0) + (stats.get("walking_ticks") or 0)
    minutes = ticks / 3600
    if minutes < H_MIN_SAMPLE_MIN or mined <= 0:
        return H_DEFAULT
    return round(max(2.0, min(40.0, mined / minutes)), 2)
