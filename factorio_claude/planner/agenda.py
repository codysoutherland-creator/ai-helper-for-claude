"""Helper-agenda ETA model for planner v2 on-site plans (PLANNER-v2.md 4.3, round 2).

The first model treated coal from drills 100 tiles away, and coal the helper "would gather", as instantly inside the
burners; in a real build the burners starved. This model simulates what the mod's helper actually does after Approve,
in the same order as `agent.work_area`:

  1. S0 takes: coal out of the listed idle machines of the base (walk + one action per entity);
  2. listed dismantling (mining time + walking; the salvage goes into its inventory);
  3. gathering what is still missing (the mod's material planner: walk to the patch, hand-mine, smelt plates in one
     furnace near the plan centre in growing chunks, chop trees for wood) and hand-crafting;
  4. placing the ghosts site by site, nearest first, a few coal into each new burner when it carries some;
  5. the logistics round, every ROUND_MIN minutes (start to start): every hand-fed burner below half a fill is
     visited in plan order (coal drills first) - a third of a fill each first, then topped up - and when the helper runs
     out it fetches coal: the plan's coal chests first, then hand-mining at the nearest coal tile.

Burners only run while they hold fuel. Coal drills fill their chest; that coal reaches the other burners only when the
helper carries it. Distances are taxicab x 0.85 at WALK tiles/s (cost.walk_s).

    r = simulate(agenda, gd)   # {first_plates, target, complete, sustained, helper_share, timeline, ...}
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import cost

MINE_S = {"iron-ore": 2.0, "copper-ore": 2.0, "stone": 2.0, "coal": 2.0, "wood": 0.8}   # helper s per raw unit
MINE_EFF = 1.0          # extra time per mined unit (stepping between tiles ...): none measurable on test slots, where
                        # first plates came 5-25 % sooner than this model predicts (seeds 1001/2002/4004, no debug ops)
MINE_RADIUS = 400       # the mod's material planner looks this far from the build site for ore (agent.lua)
SMELT_S = 3.2           # stone furnace s per plate (the helper smelts in one furnace)
SMELTER_TILES = 8       # the helper's furnace stands about this far from where it mines the ore
FIRST_LOAD = 8          # ore mined before the helper's smelter is started (agent.lua FIRST_LOAD)
CHUNK_MAX = 40
ROUND_MIN = 2.5         # logistics round period, minutes (agent.lua ROUND_PERIOD = 9000 ticks)
FETCH_MAX = 400         # coal per fetch (agent.lua MAX_FETCH_FUEL)
FETCH_TRIPS = 3         # fetches per refuel run (agent.lua MAX_FETCHES)
WARM = 0.15             # minutes from fuel to the first plate (drill 4 s per ore + furnace 3.2 s)
DT = 1 / 12             # simulation step, minutes (5 s)
HORIZON = 120.0
SUSTAIN_WINDOW = 20.0   # minutes measured after full rate to judge whether the round keeps up
MIN_RUN = 45.0          # simulate at least this long: the helper's share is measured over the last SHARE_WINDOW
SHARE_WINDOW = 10.0
START_FUEL = {"mining-drill": 8, "furnace": 5, "inserter": 1, "boiler": 5}    # agent.lua START_FUEL (~3.5 min)
FILL = {"mining-drill": 50, "furnace": 30, "inserter": 5, "boiler": 50}       # plans.lua FILL (~20 min of work)
LOW_FRAC = 1 / 3        # a round trip starts when a burner holds less than this share of a fill (~7 min of fuel
                        # left: a round period plus the trip); it is also the starter amount (agent.lua refuel)
# what the ETA counts (the card and the brief say it): the helper does all the work; the player's inventory is used
# only while the player is connected and the mod setting allows it (agent.lua takes from connected players only); the
# player's own hand work is not counted
ASSUMES = ("the helper does the work; your inventory counts only while you are connected (the helper takes from it); "
           "your own hand work is not counted")
TARGET_HOLD = 5.0      # full rate counts from when the plate output averages >= 95 % of it over this many minutes


@dataclass
class Unit:
    """A group of hand-fed burners that produce together (one burner pair, one coal or stone drill)."""
    name: str
    pos: tuple
    kind: str                   # pair | coal | stone | repair
    item: str                   # what it makes (iron-plate, coal, stone ...)
    rate: float                 # items/min while running
    burn: float                 # coal/min while running
    fill: float                 # coal per full fill (round)
    start: float                # coal the helper puts in when placing it (if it carries some)
    entities: int = 1           # burner entities (one refuel action each)
    site: int = -1              # build site index (-1: exists already)
    fuel: float = 0.0
    built: float | None = None
    running_since: float | None = None


@dataclass
class Site:
    name: str
    pos: tuple
    items: dict                 # items placed here (no fuel)
    n: int                      # entities placed here
    units: list = field(default_factory=list)   # indices into Agenda.units


@dataclass
class Agenda:
    stock: dict
    start: tuple
    sites: list
    units: list
    target: dict = field(default_factory=dict)          # {item: per_min} of the plan's units
    takes: list = field(default_factory=list)           # [(pos, {item: n}, entities)]
    dismantles: list = field(default_factory=list)      # [(pos, seconds, {item: n})]
    patch: object = None                                # patch(raw, near) -> nearest (x, y) of that resource | None
    centre: tuple | None = None                         # where the helper smelts and searches (plan centre)
    player: tuple | None = None                         # where it takes items from the player
    player_items: dict = field(default_factory=dict)    # part of `stock` that sits in the player's inventory
    period: float = ROUND_MIN
    order: list | None = None                           # site build order (indices; None = nearest first)


def _walk(a, b) -> float:
    return cost.walk_s(a, b) / 60.0


def _chunks(n: float) -> int:
    """Ore loads the mod's planner splits n ore into (8, then x1.5 up to 40 each)."""
    if n <= FIRST_LOAD + 4:
        return 1
    left, chunk, k = n - FIRST_LOAD, FIRST_LOAD, 1
    while left > 0:
        chunk = min(math.floor(chunk * 1.5), CHUNK_MAX)
        left -= chunk
        k += 1
    return k


class _Sim:
    def __init__(self, ag: Agenda, gd, horizon):
        self.ag, self.gd, self.horizon = ag, gd, horizon
        self.inv = {k: float(v) for k, v in (ag.stock or {}).items() if v > 0}
        self.pos = ag.start or ag.centre or (0.0, 0.0)
        self.t = 0.0
        self.units = ag.units
        self.store = {}                 # coal-drill unit index -> coal in its chest
        self.made = {}                  # item -> produced by plan units
        self.timeline = []
        self.gathered = {}
        self.first = None
        self.tgt = None
        self.events = []                # (t, kind, payload) fuel deliveries / placements in time order
        self.clock = 0.0
        self.trace = []
        self.player_left = {}
        self.busy_round = 0.0           # helper minutes spent on rounds
        self.busy = []                  # (start, end) of each round cycle
        self.trips = 0
        self.fail = None

    # ---- before the build (closed form: no plan unit runs yet) ---------------------------------------------------
    def note(self, label):
        self.timeline.append((round(self.t, 1), label))

    def act(self, minutes, pos=None, label=None):
        if label:
            self.note(label)
        if pos is not None:
            minutes += _walk(self.pos, pos)
            self.pos = pos
        self.t += minutes

    def pre_build(self):
        ag = self.ag
        for pos, items, n in ag.takes:
            self.act(cost.PLACE_S * n / 60, pos, "take coal out of idle machines")
            for k, v in items.items():
                cost._add(self.inv, k, v)
        for pos, secs, salvage in ag.dismantles:
            self.act(secs / 60, pos, "dismantle")
            for k, v in salvage.items():
                cost._add(self.inv, k, v)
        # S0 repairs: existing burners refuelled before anything is built (they exist: site -1)
        for i, u in enumerate(self.units):
            if u.site != -1:
                continue
            u.built = 0.0
            give = min(u.fill, self.inv.get("coal", 0))
            if give <= 0:
                continue
            self.act(cost.PLACE_S * u.entities / 60, u.pos, f"refuel {u.name}")
            self.inv["coal"] -= give
            self.events.append((self.t, "fuel", i, give))
        self.player_left = dict(ag.player_items or {})
        return True

    def prepare_site(self, s) -> bool:
        """The mod's build_ghosts round for one site: the materials it lacks (from the player, by gathering and
        hand-crafting) plus the coal for the burners' start fuel."""
        ag = self.ag
        c = cost.cover(s.items, self.inv, self.gd)
        # items taken from the player: one walk to them (the mod's take_player step)
        if ag.player is not None and self.player_left:
            mine = {k: max(0.0, self.inv.get(k, 0) - self.player_left.get(k, 0)) for k in self.player_left}
            if any(c.used.get(k, 0) > mine[k] + 1e-9 for k in self.player_left):
                self.act(cost.PLACE_S / 60, ag.player, "take items from you")
                for k in self.player_left:
                    self.player_left[k] = max(0.0, self.player_left[k] - max(0.0, c.used.get(k, 0) - mine[k]))
        short = {k: v for k, v in c.short.items() if v > 1e-6}
        unknown = [k for k in short if k not in MINE_S]
        if unknown:
            self.fail = "needs " + ", ".join(unknown)
            return False
        self.inv = dict(c.left)
        fuel = sum(self.units[i].start for i in s.units)
        coal_short = max(0.0, fuel - self.inv.get("coal", 0))
        if coal_short > 0.5 and self.store:     # the plan's coal chests first (agent.lua coal_from), then mining
            self.run_until(self.t)
            if sum(self.store.values()) > 0.5:
                src = max((i for i in self.store), key=lambda i: self.store[i])
                self.act(cost.PLACE_S / 60, self.units[src].pos, "coal from the coal chests")
                self.run_until(self.t)
                got = self.take_store(coal_short)
                cost._add(self.inv, "coal", got)
                coal_short -= got
        if coal_short > 0.5:
            cost._add(short, "coal", coal_short)
        if short:
            self.gather(short)
            cost._add(self.inv, "coal", coal_short)
        if c.craft_s:
            self.act(c.craft_s / 60, None, "hand-craft")
        return self.fail is None

    def gather(self, short):
        """Mine (and smelt) what is missing: one patch after the other, nearest first; plates are smelted near the
        plan centre in chunks while the rest is mined."""
        ag = self.ag
        mine_s, smelt_s = 0.0, 0.0
        order = sorted(short, key=lambda k: (k != "coal", k))
        self.note("gather " + ", ".join(f"{round(short[k])} {k}" for k in order))
        for raw in order:
            n = short[raw]
            p = ag.patch(raw, ag.centre or self.pos) if ag.patch else None
            if p is None and raw != "wood":
                self.fail = f"no {raw} to mine"
                self.t = self.horizon
                return
            mine = n * MINE_S[raw] * MINE_EFF
            walk = 0.0
            if p is not None:
                walk += _walk(self.pos, p)
                self.pos = p
            if raw in ("iron-ore", "copper-ore") and p is not None:
                # ore carried in loads to the helper's furnace, which stands next to the patch (agent.lua new_smelter)
                walk += 2 * _chunks(n) * SMELTER_TILES * 0.85 / cost.WALK / 60
                smelt_s += n * SMELT_S
            mine_s += mine
            self.t += walk + mine / 60
            cost._add(self.gathered, raw, n)
        # the smelter works while the helper mines: only the part it is still busy with adds time
        tail = max(0.0, FIRST_LOAD * MINE_S["iron-ore"] + smelt_s - mine_s) if smelt_s else 0.0
        self.t += tail / 60

    # ---- build: site by site (the plan's order, else nearest first), start fuel in plan order --------------------
    def build(self):
        ag = self.ag
        todo = list(ag.order) if ag.order else list(range(len(ag.sites)))
        first = True
        while todo:
            k = todo[0] if ag.order else min(todo, key=lambda i: cost.walk_s(self.pos, ag.sites[i].pos))
            todo.remove(k)
            s = ag.sites[k]
            if not first and ag.order:
                # agent.lua build_ghosts: moving on to the next site, the burners built so far get topped up first
                self.events.sort(key=lambda e: e[0])
                self.round_cycle()
            first = False
            if not self.prepare_site(s):
                self.t = self.horizon
                return self.t
            self.act(0, s.pos, f"build {s.name}")
            per = s.n and 1.0 / max(1, len(s.units)) or 0
            for ui in s.units:
                u = self.units[ui]
                self.t += cost.PLACE_S * s.n * per / 60
                give = min(u.start, self.inv.get("coal", 0))
                if give > 0:
                    self.inv["coal"] -= give
                self.events.append((self.t, "built", ui, give))
            if not s.units:
                self.t += cost.PLACE_S * s.n / 60
        self.note("built")
        return self.t

    # ---- the clock (production) ---------------------------------------------------------------------------------
    def run_until(self, t_end):
        """Advance plan production from self.clock to t_end, applying events whose time has come."""
        while self.clock < t_end - 1e-9 and self.clock < self.horizon:
            step = min(DT, t_end - self.clock)
            while self.events and self.events[0][0] <= self.clock + 1e-9:
                self.apply(self.events.pop(0))
            for i, u in enumerate(self.units):
                if u.built is None or u.fuel <= 1e-9:
                    u.running_since = None
                    continue
                use = min(u.fuel, u.burn * step)
                f = use / (u.burn * step) if u.burn > 0 else 1.0
                u.fuel -= use
                if u.running_since is None:
                    u.running_since = self.clock
                if self.clock + step - u.running_since < WARM:
                    continue
                out = u.rate * step * f
                if u.kind == "coal":
                    self.store[i] = self.store.get(i, 0.0) + out
                cost._add(self.made, u.item, out)
                if u.kind == "pair" and self.first is None and out > 0:
                    self.first = self.clock + step
            self.clock += step
            self.trace.append((self.clock, dict(self.made)))
        while self.events and self.events[0][0] <= self.clock + 1e-9:
            self.apply(self.events.pop(0))

    def held_target(self):
        """Full rate = the earliest time from which the plan's output, averaged over every TARGET_HOLD-minute window
        that follows, stays at >= 95 % of the target until the end of the simulation (held, not a peak on start
        fuel). None when the last window is below it."""
        tr, target = self.trace, self.ag.target
        if not tr or not target:
            return None
        oks, j = [], 0
        for t, a in tr:
            while j < len(tr) and tr[j][0] < t + TARGET_HOLD - 1e-9:
                j += 1
            if j >= len(tr):
                break
            b = tr[j][1]
            oks.append((t, all((b.get(k, 0) - a.get(k, 0)) / TARGET_HOLD >= 0.95 * v for k, v in target.items())))
        held = None
        for t, ok in reversed(oks):
            if not ok:
                break
            held = t
        if held is None:
            return None
        return max(held, self.first or held)

    def apply(self, ev):
        _t, kind, ui, amount = ev
        u = self.units[ui]
        if kind == "built":
            u.built = _t
            u.fuel += amount
        elif kind == "fuel":
            u.fuel += amount

    # ---- the logistics round --------------------------------------------------------------------------------------
    def take_store(self, n):
        got = 0.0
        for i in sorted(self.store, key=lambda i: -self.store[i]):
            k = min(self.store[i], n - got)
            self.store[i] -= k
            got += k
            if got >= n - 1e-9:
                break
        return got

    def fetch(self, need, t, mine=True):
        """One coal trip: the plan's coal chests first, then hand-mining (mine=False: the chests only, as the
        round's top-up pass does). -> (minutes, coal got, end position)."""
        ag = self.ag
        need = min(need, FETCH_MAX)
        mins, pos, got = 0.0, self.pos, 0.0
        stores = [self.units[i] for i in self.store if self.store[i] > 0.5]
        if stores:
            src = min(stores, key=lambda u: cost.walk_s(pos, u.pos)).pos
            mins += _walk(pos, src) + cost.PLACE_S / 60 * max(1, len(stores))
            pos = src
            self.run_until(t + mins)
            got = self.take_store(need)
        if got < need - 0.5 and mine:
            p = ag.patch("coal", pos) if ag.patch else None
            if p is not None:
                mins += _walk(pos, p) + (need - got) * MINE_S["coal"] * MINE_EFF / 60
                pos = p
                cost._add(self.gathered, "coal", need - got)
                got = need
        self.trips += 1
        return mins, got, pos

    def round_cycle(self):
        """One round, when any burner is below LOW_FRAC of a fill: fetch coal (the starter amounts may be hand-mined,
        the top-up only comes from the coal chests), allocate starters first then top-ups in plan order, deliver
        nearest first. Events (fuel arriving) are scheduled at visit times; returns the cycle's helper minutes."""
        t0 = self.t
        self.run_until(self.t)
        needy = [i for i, u in enumerate(self.units) if u.built is not None and u.fuel < u.fill * LOW_FRAC]
        if not needy:
            return 0.0
        # a coal trip passes the sites anyway: every burner gets topped up, so they stay in step and the next trip is
        # only due when the first of them runs low again (agent.lua refuel low_frac)
        targets = [i for i, u in enumerate(self.units) if i in needy or (u.built is not None and u.fuel < u.fill - 0.5)]
        # agent.lua refuel (ordered): 1. fetch - the starter amounts (hand-mining allowed), then once the full top-up
        # from the coal chests only; 2. allocate - starters first, the rest in plan order; 3. deliver nearest first
        fuel = {i: self.units[i].fuel for i in targets}

        def starter(u):
            return max(1.0, u.fill * LOW_FRAC)
        for mine in (True, False):
            for _trip in range(FETCH_TRIPS if mine else 1):
                carry = self.inv.get("coal", 0)
                need = sum(max(0.0, (starter(self.units[i]) if mine else self.units[i].fill) - fuel[i]) for i in targets)
                if carry >= need - 0.5:
                    break
                self.run_until(self.t)
                if not mine and not any(v > 0.5 for v in self.store.values()):
                    break
                mins, got, pos = self.fetch(need - carry, self.t, mine=mine)
                self.t += mins
                self.pos = pos
                cost._add(self.inv, "coal", got)
                if got <= 0.5:
                    break
        left = self.inv.get("coal", 0)
        alloc = {}
        for i in targets:
            alloc[i] = min(left, max(0.0, starter(self.units[i]) - fuel[i]))
            left -= alloc[i]
        for i in targets:
            add = min(left, max(0.0, self.units[i].fill - fuel[i] - alloc[i]))
            alloc[i] += add
            left -= add
        todo = [i for i in targets if alloc[i] > 0.5]
        while todo:
            i = min(todo, key=lambda j: cost.walk_s(self.pos, self.units[j].pos))
            todo.remove(i)
            u = self.units[i]
            self.t += _walk(self.pos, u.pos) + cost.PLACE_S * u.entities / 60
            self.pos = u.pos
            self.inv["coal"] -= alloc[i]
            self.events.append((self.t, "fuel", i, alloc[i]))
        self.events.sort(key=lambda e: e[0])
        self.busy_round += self.t - t0
        self.busy.append((t0, self.t))
        return self.t - t0

    def run(self):
        self.clock = 0.0
        self.trace = []
        if not self.pre_build():
            return
        # burners placed early run on their start fuel while the next site is gathered and built
        self.build()
        if self.fail:
            return
        self.complete = self.t
        self.events.sort(key=lambda e: e[0])
        self.run_until(self.t)
        cycles = 0
        self.round_start = self.t
        until = min(self.horizon, max(self.complete + SUSTAIN_WINDOW + TARGET_HOLD, MIN_RUN))
        while self.t < self.horizon:
            start = self.t
            self.round_cycle()
            cycles += 1
            nxt = max(start + self.ag.period, self.t)
            self.run_until(nxt)
            self.t = max(self.t, nxt)
            if self.clock >= until:
                break
        self.cycles = cycles


def simulate(ag: Agenda, gd, horizon=HORIZON) -> dict:
    """-> {first_plates, target, complete, sustained, per_min, helper_share, timeline, gathered, trips, assumes}
    (minutes; None beyond the horizon). target = full rate held from then on (held_target); sustained = plate output
    over the SUSTAIN_WINDOW after the build is complete divided by the target (below 0.9: the round can't keep the
    burners fed)."""
    s = _Sim(ag, gd, horizon)
    s.complete = None
    s.cycles = 0
    s.clock = 0.0
    s.trace = []
    s.run()

    def m(v):
        return None if v is None or v > horizon else round(v, 1)
    s.tgt = s.held_target() if s.complete is not None else None
    sustained, per_min = None, {}
    if s.trace:
        # steady state: the SUSTAIN_WINDOW after everything is built (and full rate, when it was reached later)
        t_a = max(s.tgt or 0.0, s.complete or 0.0)
        t_b = min(t_a + SUSTAIN_WINDOW, s.clock)
        a = next((x for x in s.trace if x[0] >= t_a - 1e-9), s.trace[-1])
        b = next((x for x in reversed(s.trace) if x[0] <= t_b + 1e-9), s.trace[-1])
        span = b[0] - a[0]
        if span >= 5:
            per_min = {k: round((b[1].get(k, 0) - a[1].get(k, 0)) / span, 1) for k in b[1]}
            if ag.target:
                sustained = round(min(per_min.get(k, 0) / v for k, v in ag.target.items() if v), 2)
    # the helper's share of time on the round: everything after the build (a fill lasts ~20 min, so a short window
    # at the end can miss every trip and report 0 %)
    w0, w1 = min(getattr(s, "round_start", 0) or 0, max(0.0, s.clock - SHARE_WINDOW)), s.clock
    busy = sum(max(0.0, min(b, w1) - max(a, w0)) for a, b in s.busy)
    return {"first_plates": m(s.first), "target": m(s.tgt), "complete": m(s.complete),
            "sustained": sustained, "per_min": per_min,
            "helper_share": round(min(1.0, busy / max(1e-9, w1 - w0)), 2) if s.cycles else 0.0,
            "timeline": s.timeline[:14], "gathered": {k: round(v) for k, v in s.gathered.items()},
            "trips": s.trips, "fail": s.fail, "assumes": ASSUMES}
