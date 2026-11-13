"""The autopilot's deterministic part (AUTOPILOT.md 4.2-4.6): the Snapshot it observes, goal status, the Director's
rules R0-R9 and progress / stuck tracking. Pure functions on plain data: no link, no Claude, unit-tested
(tests/test_autopilot.py). autopilot.py runs them in the loop and executes what they return.

    snap = Snapshot.from_observe(link.call("autopilot_observe"), masters)
    action = Director(gd_techs).step(snap, mem)     # -> Action or None (None: nothing for the rules to do)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import knowledge as K

TICKS_MIN = 3600
STUCK_MIN = 5.0                 # game minutes without progress = STUCK (Q1 fails at 10)
RUNG_GAP_MIN = 1.5              # each further rung 1.5 game minutes later: the whole ladder runs within 10 minutes
LADDERS_TO_HALT = 3
BLOCK_GOAL_MIN = 30.0
VERIFY_AFTER_MIN = 5.0
VERIFY_SHORT = 0.7              # measured below 70% of the plan's rate = a shortfall
REFUEL_COOLDOWN_MIN = 5.0
BRIDGE_COOLDOWN_MIN = 10.0
RESEARCH_COOLDOWN_MIN = 0.5
STAGE_RETRY_MIN = 8.0
HELPER_IDLE_MIN = 1.0           # an applied plan with ghosts and an idle helper: restart its build after this
PACKS_COOLDOWN_MIN = 15.0
PACKS_IDLE_RETRY_MIN = 3.0       # the packs not delivered and the helper idle: try again sooner
REVIEW_EVERY_MIN = 60.0
EXPAND_RETRY_MIN = 20.0
EXPAND_TRIES = 3
POWER_TIGHT = 0.9               # load / capacity at which power grows whatever the current goal
LOW_POWER_MACHINES = 3          # more machines than this short of power: grow power
REVERIFY_MIN = 10.0
DEFEND_WINDOW_MIN = 30.0        # an attack this recent with no turret near it gets a turret group
DEFEND_COOLDOWN_MIN = 15.0      # one group per spot per 15 game minutes
THREAT_EVERY_MIN = 10.0         # nests the pollution reaches are re-read this often
RED = "automation-science-pack"
# roadmap template -> planner/expand.py template (the others are the starter base's or research's)
EXPAND = {"mall": "mall", "power_grow": "power_grow", "steel_column": "steel", "science_block": "science",
          "oil": "oil", "oil_plant": "oil_plant", "iron_grow": "iron_grow", "copper_grow": "copper_grow"}
# research an expansion template waits for (the mall's cells and machines include electric drills: round 3 moved the
# mall up to the electric-drill stage, so its research is a gate)
EXPAND_GATE = {"mall": ["automation", "electric-mining-drill", "logistics"], "power_grow": [], "steel": ["steel-processing"],
               "science": ["logistic-science-pack", "logistics"], "oil": ["oil-gathering", "fluid-handling"],
               "oil_plant": ["oil-processing", "plastics"], "iron_grow": ["electric-mining-drill"],
               "copper_grow": ["electric-mining-drill"]}
# plates per science pack (red: 1 copper + a gear; green: an inserter + a belt) - what a pack rate asks of the smelting
PLATES_PER = {"automation-science-pack": {"iron-plate": 2.0, "copper-plate": 1.0},
              "logistic-science-pack": {"iron-plate": 5.5, "copper-plate": 1.5}}
TIGHT = 0.85                    # a plate used at >= 85% of what is made: nothing left over, the chain waits on it
SLACK = 0.5                     # a plate used at < 50% of what is made: over-produced (it backs up; nothing breaks)
GROW_COOLDOWN_MIN = 20.0        # one more smelting cell per plate per 20 game minutes
SHIPPED_ROUND = 3               # roadmap goals of rounds <= this have a template the autopilot can build
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PACKS = ("automation-science-pack", "logistic-science-pack", "military-science-pack", "chemical-science-pack")


def as_list(x) -> list:
    """Factorio serialises an empty table as {} and an array as a list: always a list."""
    if isinstance(x, dict):
        return list(x.values())
    return list(x or [])


def as_dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def safe_name(v) -> str | None:
    """A prototype name (lowercase, digits, dashes) or None: free text never passes."""
    s = str(v or "")
    return s if NAME_RE.match(s) else None


def safe_id(v) -> str | None:
    s = str(v or "")
    return s if ID_RE.match(s) else None


@dataclass
class Action:
    kind: str                   # research | bootstrap_packs | ask_lab | starter | adopt | next_stage | power_bridge
    #                             | refuel | helper_restart | verify | wait | plateau
    rule: str                   # R0..R9
    goal: str | None = None
    reason: str = ""
    args: dict = field(default_factory=dict)
    step: str = ""              # status line "next step" text

    def to_dict(self) -> dict:
        return {"kind": self.kind, "rule": self.rule, "goal": self.goal, "reason": self.reason, "args": self.args,
                "step": self.step}


# ---- snapshot ------------------------------------------------------------------------------------------------------
@dataclass
class Snapshot:
    tick: int = 0
    speed: float = 1.0
    on: bool = False
    epoch: int = 0
    paused: bool = False
    research: dict = field(default_factory=dict)      # current, progress, queue [], done set, units_done
    made: dict = field(default_factory=dict)          # items/min over 10 game minutes
    used: dict = field(default_factory=dict)          # items/min consumed over 10 game minutes (machines + hand crafts)
    totals: dict = field(default_factory=dict)        # items made so far (packs, plates)
    power: dict = field(default_factory=dict)
    power_gaps: list = field(default_factory=list)
    burners_out: int = 0
    burners_out_at: list = field(default_factory=list)
    census: dict = field(default_factory=dict)
    problems: dict = field(default_factory=dict)
    helper: dict = field(default_factory=dict)
    plans: list = field(default_factory=list)
    labs: list = field(default_factory=list)
    ap: dict = field(default_factory=dict)            # autopilot_get
    masters: dict = field(default_factory=dict)       # master id -> {"owner", "stages": [{k, label, plan, row, gate}],
    #                                                   "templates": {expansion template: stage index}}
    enemies: dict = field(default_factory=dict)       # {peaceful, spawners}
    skipped: set = field(default_factory=set)         # stage plan ids the planner counts as built (Memory.skipped)
    defense: dict = field(default_factory=dict)       # defense.lua status: attacks, turrets, low_ammo, researched

    @classmethod
    def from_observe(cls, o: dict, masters: dict | None = None, power_gaps=None) -> "Snapshot":
        o = as_dict(o)
        r = as_dict(o.get("research"))
        research = {"current": safe_name(r.get("current")), "progress": float(r.get("progress") or 0),
                    "queue": [q for q in (safe_name(x) for x in as_list(r.get("queue"))) if q],
                    "done": {d for d in (safe_name(x) for x in as_list(r.get("done"))) if d},
                    "units_done": float(r.get("units_done") or 0)}
        ap = as_dict(o.get("autopilot"))
        return cls(tick=int(o.get("tick") or 0), speed=float(o.get("speed") or 1), on=bool(ap.get("on")),
                   epoch=int(ap.get("epoch") or 0), paused=bool(ap.get("paused")), research=research,
                   made={k: float(v) for k, v in as_dict(o.get("made")).items() if safe_name(k)},
                   used={k: float(v) for k, v in as_dict(o.get("used")).items() if safe_name(k)},
                   totals={k: float(v) for k, v in as_dict(o.get("totals")).items() if safe_name(k)},
                   power=as_dict(o.get("power")), power_gaps=as_list(power_gaps if power_gaps is not None else []),
                   burners_out=int(o.get("burners_out") or 0), burners_out_at=as_list(o.get("burners_out_at")),
                   census={k: int(v) for k, v in as_dict(o.get("census")).items() if safe_name(k)},
                   problems=as_dict(o.get("problems")), helper=as_dict(o.get("helper")),
                   plans=[as_dict(p) for p in as_list(o.get("plans"))], labs=as_list(o.get("labs")), ap=ap,
                   masters=masters or {}, enemies=as_dict(o.get("enemies")), defense=as_dict(o.get("defense")))

    # -- derived ----------------------------------------------------------------------------------------------------
    @property
    def minute(self) -> float:
        return self.tick / TICKS_MIN

    def done(self, tech: str) -> bool:
        return tech in self.research.get("done", set())

    def plan(self, pid: str) -> dict | None:
        return next((p for p in self.plans if p.get("id") == pid), None)

    def labs_n(self) -> int:
        return len(self.labs) or int(self.census.get("lab", 0))

    def research_busy(self) -> int:
        return len(self.research.get("queue") or []) + (1 if self.research.get("current") else 0)

    def automated_packs(self) -> set:
        """Pack types the base makes (any made per minute), excluding hand-crafted ones (the helper's crafts)."""
        return {p for p in PACKS if self.made.get(p, 0) > 0.05}

    def research_packs(self) -> set:
        """Packs research may count on: what the base makes, plus red always (the first pack every base automates;
        queued red techs wait in the queue harmlessly until it flows). A tech needing any other pack the base does
        not make would block the labs."""
        return self.automated_packs() | {RED}

    @property
    def peaceful(self) -> bool:
        """No enemies to fight: peaceful mode, or no spawners on the map (weapons research is skipped then)."""
        e = self.enemies
        if not e:
            return True
        return bool(e.get("peaceful")) or int(e.get("spawners") or 0) == 0

    def use_kw(self) -> float:
        return sum(float(as_dict(n).get("use_kw") or 0) for n in as_list(self.power.get("networks")))

    def counters(self) -> dict:
        return as_dict(self.helper.get("counters"))

    def handcraft_share(self) -> float:
        return float(self.counters().get("share_1h") or 0)

    def foreign_burners(self) -> list:
        """Out-of-fuel burners (samples) outside the autopilot's applied plans (those are their coal round's job)."""
        own = [as_list(p.get("bbox")) for p in self.plans
               if p.get("owner") == "autopilot" and p.get("status") in ("applied", "applying") and p.get("bbox")]
        out = []
        for b in self.burners_out_at:
            b = as_list(b)
            if len(b) >= 2 and not any(len(o) == 4 and o[0] - 1 <= b[0] <= o[2] + 1 and o[1] - 1 <= b[1] <= o[3] + 1
                                       for o in own):
                out.append([float(b[0]), float(b[1])])
        return out

    def open_power_gaps(self) -> list:
        """Power gaps that are the base's problem: reachable, and not part of an autopilot plan still being built
        (its own poles come with it)."""
        building = [as_list(p.get("bbox")) for p in self.plans
                    if p.get("owner") == "autopilot" and p.get("status") in ("applied", "applying")
                    and int(p.get("ghosts") or 0) > 0 and p.get("bbox")]

        def inside(pt):
            pt = as_dict(pt)
            return any(len(o) == 4 and o[0] - 1 <= pt.get("x", 1e9) <= o[2] + 1 and
                       o[1] - 1 <= pt.get("y", 1e9) <= o[3] + 1 for o in building)
        out = []
        for g in self.power_gaps:
            g = as_dict(g)
            if g.get("unreachable") or not g.get("a") or not g.get("b"):
                continue
            if inside(g.get("b")) or (not g.get("cover") and inside(g.get("a"))):
                continue
            out.append(g)
        return out

    def low_power(self) -> int:
        return int(self.power.get("low_power") or 0) + int(self.power.get("no_power") or 0)


# ---- goals (roadmap.json) ------------------------------------------------------------------------------------------
def active_master(snap: Snapshot) -> str | None:
    """The autopilot's master: one whose plans are owned by the autopilot (the newest), else None."""
    best = None
    for mid, m in snap.masters.items():
        if m.get("owner") == "autopilot" and not m.get("abandoned"):
            if best is None or m.get("seq", 0) > snap.masters[best].get("seq", 0):
                best = mid
    return best


def adoptable_master(snap: Snapshot) -> str | None:
    """A master the player approved earlier (one of its stages applied) that the autopilot can continue (D5)."""
    for mid, m in snap.masters.items():
        applied = any((st.get("row") or {}).get("status") == "applied" for st in m.get("stages", []))
        if m.get("owner") != "autopilot" and not m.get("abandoned") and applied:
            return mid
    return None


def master_progress(snap: Snapshot, mid: str | None) -> dict:
    """{done: [k], next: stage dict or None, all_done: bool, top_started: highest stage applied or built} from the
    plan rows of a master. A stage in snap.skipped counts as built: the planner found it on the map (the player's own
    builds cover it) although the mod never applied its plan."""
    m = snap.masters.get(mid or "") or {}
    stages = m.get("stages") or []

    def built(s):
        return (s.get("row") or {}).get("built") or s.get("plan") in snap.skipped

    def building(s):
        return (s.get("row") or {}).get("status") in ("applied", "applying")

    done = [s["k"] for s in stages if built(s)]
    # the next stage to offer skips stages the helper is building: one with a few ghosts it can never place
    # (something in the way) held every later stage back for ever (live, 0.6.1). The planner checks the order.
    nxt = next((s for s in stages if not built(s) and not building(s)), None) or \
        next((s for s in stages if not built(s)), None)
    started = [s["k"] for s in stages if built(s) or
               (s.get("row") or {}).get("status") in ("applied", "applying")]
    return {"done": done, "next": nxt, "all_done": bool(stages) and nxt is None, "stages": stages,
            "top_started": max(started) if started else -1, "top_done": max(done) if done else -1}


def metric(snap: Snapshot, cond: dict) -> tuple[bool, float]:
    """(holds, value) of one roadmap condition."""
    m = cond.get("metric")
    if m == "power_split":
        v = float(len(snap.open_power_gaps()))
        return v <= cond.get("max", 0), v
    if m == "power_satisfaction":
        v = 1.0 if snap.low_power() == 0 else 0.5
        return v >= cond.get("min", 0.9), v
    if m == "burners_out_of_fuel":
        v = float(len(snap.foreign_burners()))
        return v <= cond.get("max", 0), v
    if m == "research_queued":
        v = float(snap.research_busy())
        return (v >= cond.get("min", 1)) or snap.labs_n() == 0, v
    if m == "master_built":
        mp = master_progress(snap, active_master(snap) or adoptable_master(snap))
        top = max([k for k in mp["done"]] or [-1])
        v = float(top)
        return mp["all_done"] or v >= cond.get("min_stage", 5), v
    if m in ("master_stage_built", "master_stage_started"):
        mp = master_progress(snap, active_master(snap) or adoptable_master(snap))
        v = float(mp["top_done"] if m == "master_stage_built" else mp["top_started"])
        return v >= cond.get("min", 0), v
    if m == "rate":
        v = float(snap.made.get(cond["item"], 0))
        return v >= cond.get("min", 0), v
    if m == "tech":
        v = 1.0 if snap.done(cond["tech"]) else 0.0
        return v >= 1, v
    if m == "power_peak_ratio":
        cap = float(snap.power.get("capacity_kw") or 0)
        use = snap.use_kw()
        v = use / cap if cap > 0 else 1.0
        # "headroom" only means something once the base draws real power (round-1 judge: G4 was done at 63 game
        # minutes with almost no load)
        return v <= cond.get("max", 0.66) and use >= cond.get("min_load_kw", 0) and cap > 0, v
    if m == "smelt_capacity":
        c = snap.census
        v = 18.75 * (c.get("stone-furnace", 0) + 2 * c.get("steel-furnace", 0) + 2 * c.get("electric-furnace", 0))
        return v >= cond.get("min", 0), v
    if m == "mall_stocked":
        v = float(snap.ap.get("mall_stocked") if snap.ap.get("mall_stocked") is not None
                  else snap.ap.get("mall_chests") or 0)
        return v >= cond.get("min", 1), v
    return False, 0.0


def goal_status(snap: Snapshot, blocked: dict | None = None, mem: "Memory | None" = None) -> list:
    """Every roadmap goal: {id, title, done, available (template shipped), ready (needs done, ready conditions
    hold), blocked, values}. A goal is never done before its needs. With `mem`: a goal once done stays done (latched:
    no repeated milestones, no flicker), and a goal with a production rate counts as done only after two
    observations in a row at the target."""
    out, done = [], set()
    latched = mem.latched if mem is not None else set()
    hits = mem.goal_hits if mem is not None else {}
    for g in K.roadmap():
        res = [metric(snap, c) for c in g["done"]]
        raw = all(r[0] for r in res) and all(n in done for n in g["needs"])
        if raw and any(c.get("metric") == "rate" for c in g["done"]) and mem is not None:
            hits[g["id"]] = hits.get(g["id"], 0) + 1
            raw = hits[g["id"]] >= 2
        elif mem is not None:
            hits[g["id"]] = 0
        ok = raw or g["id"] in latched
        if ok:
            done.add(g["id"])
            if mem is not None:     # G0 (restore) too: its upkeep rules (R4, R5, R1) keep running anyway
                latched.add(g["id"])
        out.append({"id": g["id"], "title": g["title"], "done": ok, "values": [round(r[1], 2) for r in res],
                    "available": g["round"] <= SHIPPED_ROUND, "template": g["template"], "needs": g["needs"],
                    "templates": list(g.get("templates") or [g["template"]]),
                    "techs": g.get("techs", []), "ready_when": g.get("ready", [])})
    for x in out:
        x["ready"] = all(n in done for n in x["needs"]) and all(metric(snap, c)[0] for c in x["ready_when"])
        x["blocked"] = bool(blocked and blocked.get(x["id"], -1) > snap.minute)
    return out


def current_goal(goals: list, order: list | None = None) -> dict | None:
    """First open goal (in `order` if given, else roadmap order) that is ready, shipped and not blocked."""
    by = {g["id"]: g for g in goals}
    ids = [i for i in (order or []) if i in by] + [g["id"] for g in goals if g["id"] not in (order or [])]
    for i in ids:
        g = by[i]
        if not g["done"] and g["ready"] and g["available"] and not g["blocked"]:
            return g
    return None


def open_options(goals: list) -> list:
    """Goals Claude may choose between at a milestone: open, ready, shipped, not blocked (roadmap order)."""
    return [g for g in goals if not g["done"] and g["ready"] and g["available"] and not g["blocked"]]


# ---- progress (4.6) ------------------------------------------------------------------------------------------------
def progress_key(snap: Snapshot) -> dict:
    """The signals that count as progress. Steady production alone does not, and neither does the helper mining or
    smelting by hand (round 1: 30 game minutes of hand-mining for 39 ghosts reset the stuck clock again and again):
    research units, techs, entities built, ghosts left, plan states."""
    c = snap.counters()
    ghosts = sum(int(p.get("ghosts") or 0) for p in snap.plans
                 if p.get("owner") == "autopilot" and p.get("status") in ("applied", "applying"))
    return {"units": round(float(snap.research.get("units_done") or 0), 1),
            "techs": len(snap.research.get("done") or ()),
            "owned": int(snap.ap.get("owned") or 0),
            "placed": int(float(c.get("placed") or 0)),
            "ghosts": ghosts,
            "plans": tuple(sorted((str(p.get("id")), str(p.get("status")), bool(p.get("built"))) for p in snap.plans))}


def progressed(prev: dict | None, cur: dict) -> bool:
    if prev is None:
        return True
    return (cur["units"] > prev["units"] + 0.05 or cur["techs"] > prev["techs"] or cur["owned"] > prev["owned"]
            or cur.get("placed", 0) > prev.get("placed", 0) or cur["ghosts"] < prev["ghosts"]
            or cur["plans"] != prev["plans"])


# ---- memory the Director keeps between steps -----------------------------------------------------------------------
@dataclass
class Memory:
    cooldown: dict = field(default_factory=dict)      # key -> game minute until which the action is not repeated
    attempts: dict = field(default_factory=dict)      # key -> count
    blocked: dict = field(default_factory=dict)       # goal id -> game minute until which it is skipped
    goal_order: list = field(default_factory=list)    # Claude's preferred order (milestone decisions)
    built_at: dict = field(default_factory=dict)      # plan id -> game minute it was first seen built
    verified: dict = field(default_factory=dict)      # plan id -> result
    asked_lab: bool = False
    helper_idle_since: float | None = None
    notes: list = field(default_factory=list)
    latched: set = field(default_factory=set)         # goals done once (they stay done)
    goal_hits: dict = field(default_factory=dict)     # goal id -> observations in a row at its target
    need_power: bool = False                          # VERIFY found machines without power: grow power first
    need_plate: str | None = None                     # VERIFY found this plate the bottleneck: one more smelting cell
    failed: dict = field(default_factory=dict)        # expansion template -> last error
    skipped: set = field(default_factory=set)         # stage plan ids the planner reports built (see master_progress)

    def ready(self, key: str, now: float) -> bool:
        return self.cooldown.get(key, -1e9) <= now

    def hold(self, key: str, now: float, minutes: float):
        self.cooldown[key] = now + minutes

    def bump(self, key: str) -> int:
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.attempts[key]


# ---- the Director (4.3) --------------------------------------------------------------------------------------------
class Director:
    """Deterministic rules, first match wins. techs: the game's technology table (GameData.raw["technologies"]) for
    the research order; stage_rates: optional {plan id: {item: per_min}} for R7."""

    def __init__(self, techs: dict | None = None, stage_rates: dict | None = None):
        self.techs = techs or {}
        self.stage_rates = stage_rates or {}

    # each rule returns an Action or None
    PLANNER_KINDS = ("starter", "adopt", "next_stage", "expand", "abandon")

    def step(self, snap: Snapshot, mem: Memory, goal: dict | None = None, goals: list | None = None,
             planner_busy: bool = False) -> Action | None:
        """First rule that applies. planner_busy: a planner job runs in the background; rules that would start
        another one wait (research, fuel, power poles, helper restarts and verification go on)."""
        self.goals = goals if goals is not None else goal_status(snap, mem.blocked)
        for rule in (self.r0_safety, self.r10_defense, self.r1c_research_clean, self.r1_research, self.r1b_bootstrap, self.r4_power,
                     self.r5_fuel, self.r3_starter, self.r2_next_stage, self.r8_expand, self.r6_helper,
                     self.r7_verify, self.r9_mall):
            if planner_busy and rule in (self.r3_starter, self.r2_next_stage, self.r8_expand):
                continue
            a = rule(snap, mem, goal)
            if a is not None:
                return a
        return None

    # R0: a queued autopilot plan waits for the player to leave its ground (the mod defers it): say so, do nothing
    def r0_safety(self, snap, mem, goal):
        wf = as_dict(snap.ap.get("waiting_for"))
        if wf and any(as_dict(q).get("deferred") for q in as_list(snap.ap.get("queue"))):
            key = "r0"
            if mem.ready(key, snap.minute):
                mem.hold(key, snap.minute, 2)
                return Action("wait", "R0", goal and goal["id"], "waiting for the player to finish nearby",
                              {"at": [wf.get("x"), wf.get("y")]}, "waiting for you to finish nearby")
        return None

    # R1: keep 3 techs queued, ranked by the doctrine, only with packs the base makes (red always: see
    # Snapshot.research_packs); no weapons in a peaceful game; Automation from hand-made packs (R1b)
    def r1_research(self, snap, mem, goal):
        if snap.labs_n() == 0 or snap.research_busy() >= 3 or not mem.ready("r1", snap.minute):
            return None
        packs = snap.research_packs()
        needed = list(goal.get("techs", [])) if goal else []      # the current goal's techs, then the doctrine order
        techs = K.research_order(_GD(self.techs), researched=snap.research["done"], packs_made=packs,
                                 needed=needed, limit=6, peaceful=snap.peaceful)
        techs = [t for t in techs if t != snap.research.get("current") and t not in snap.research.get("queue", [])]
        if not techs:
            return None
        mem.hold("r1", snap.minute, RESEARCH_COOLDOWN_MIN)
        return Action("research", "R1", goal and goal["id"], "research queue short",
                      {"techs": techs[: 3 - snap.research_busy()]}, "research " + techs[0])

    # R1c: a queued (or running) tech whose science packs the base does not make blocks the labs (round 1: Claude
    # queued Automation 2 before any green pack existed, the labs sat idle ~30 game minutes): take it out
    def r1c_research_clean(self, snap, mem, goal):
        if not self.techs or not mem.ready("r1c", snap.minute):
            return None
        cur = snap.research.get("current")
        queue = ([cur] if cur else []) + [t for t in snap.research.get("queue", []) if t != cur]
        if cur == "automation" and not snap.done("automation"):
            return None                       # Automation runs on its 10 hand-made packs (the one exception)
        bad = K.research_blocked(_GD(self.techs), queue, snap.research_packs())
        bad += [t for t in queue if snap.peaceful and K.is_combat(t) and t not in bad]
        if not bad:
            return None
        mem.hold("r1c", snap.minute, 2)
        keep = [t for t in queue if t not in bad]
        return Action("research_set", "R1", goal and goal["id"],
                      "queued research needs packs the base does not make" if not all(K.is_combat(t) for t in bad)
                      else "weapons research in a peaceful game", {"queue": keep, "removed": bad},
                      "research " + (keep[0] if keep else "-"))

    def r1b_bootstrap(self, snap, mem, goal):
        """Automation needs 10 red packs before any assembler exists: the helper crafts them (a bootstrap exception)
        and puts them in a lab. The red-science trigger itself needs a lab crafted by a PLAYER (D3)."""
        if snap.done("automation") or snap.labs_n() == 0:
            return None
        if not snap.done("automation-science-pack"):
            if snap.done("steam-power") and snap.done("electronics") and not mem.asked_lab:
                mem.asked_lab = True
                return Action("ask_lab", "R1", goal and goal["id"], "red science needs a lab crafted by a player",
                              {}, "waiting for you: craft one lab by hand")
            return None
        powered = [l for l in snap.labs if len(as_list(l)) >= 2 and (len(as_list(l)) < 4 or as_list(l)[3])]
        if not powered:
            return None                       # R4 connects an unpowered lab first
        idle = snap.helper.get("alive") and not snap.helper.get("current") and not int(snap.helper.get("queue") or 0)
        last = mem.cooldown.get("packs", -1e9) - PACKS_COOLDOWN_MIN
        if not mem.ready("packs", snap.minute) and not (idle and snap.minute - last >= PACKS_IDLE_RETRY_MIN):
            return None
        mem.hold("packs", snap.minute, PACKS_COOLDOWN_MIN)
        lab = as_list(powered[0])
        return Action("bootstrap_packs", "R1", goal and goal["id"], "Automation needs 10 hand-made red packs",
                      {"lab": [lab[0], lab[1]], "count": 10, "research": "automation"},
                      "10 red packs for Automation")

    # R2: the active master's next stage: offer it (re-checked against the map) when the mod has nothing queued
    def r2_next_stage(self, snap, mem, goal):
        mid = active_master(snap)
        if not mid:
            return None
        mp = master_progress(snap, mid)
        st = mp["next"]
        if st is None:
            return None
        row = st.get("row")
        status = (row or {}).get("status")
        if status in ("applied", "applying") or (row or {}).get("queued"):
            return None                               # building / in its grace period (R6 watches the helper)
        if status == "future":
            return None                               # waits for research (R1) or the stage before it
        if status == "proposed" and row.get("owner") == "autopilot" and not row.get("blocked") \
                and not row.get("superseded"):
            # proposed, not queued: the autopilot was off when it was offered; re-sending queues it
            pass
        key = f"stage:{st.get('plan')}"
        if not mem.ready(key, snap.minute):
            return None
        if row and row.get("blocked") in ("removal_blocked", "conflict") and mem.attempts.get(key, 0) >= 2:
            # re-checking does not help: the stage takes apart (or stands on) the player's things. Leave that master
            # (what is built stays) and plan a new one around the player's builds (ORG-6)
            mem.hold(key, snap.minute, 60)
            return Action("abandon", "R2", goal and goal["id"],
                          f"{st.get('label')} keeps needing the player's ground ({row.get('blocked')})",
                          {"master": mid}, "plan a new base next to yours")
        n = mem.bump(key)
        mem.hold(key, snap.minute, STAGE_RETRY_MIN)
        return Action("next_stage", "R2", goal and goal["id"],
                      f"{st.get('label')} {('blocked: ' + str(row.get('blocked'))) if row and row.get('blocked') else (status or 'not in the game')}",
                      {"master": mid, "stage": st.get("k"), "plan": st.get("plan"), "attempt": n},
                      f"plan {st.get('label')}")

    # R3: no master yet -> the starter base (or adopt one the player approved)
    def r3_starter(self, snap, mem, goal):
        if active_master(snap):
            return None
        adopt = adoptable_master(snap)
        key = "adopt" if adopt else "starter"
        if not mem.ready(key, snap.minute):
            return None
        n = mem.bump(key)
        if n > 3:
            if key == "starter" and not mem.cooldown.get("starter_given_up"):
                mem.cooldown["starter_given_up"] = snap.minute
                mem.blocked["G1"] = snap.minute + 24 * 60
                return Action("plateau", "R3", "G1", "no starter base fits here without taking apart the player's "
                              "builds", {"why": "starter"}, "starter base does not fit")
            return None
        mem.hold(key, snap.minute, 10)
        if adopt:
            return Action("adopt", "R3", "G1", "continue the starter base the player approved", {"master": adopt},
                          "continue the starter base")
        return Action("starter", "R3", "G1", "no master plan yet", {"attempt": n}, "plan the starter base")

    # R4: a network with consumers and no generator near a powered one: poles across the gap
    def r4_power(self, snap, mem, goal):
        for g in snap.open_power_gaps():
            key = f"bridge:{g.get('net')}"
            if mem.ready(key, snap.minute):
                mem.hold(key, snap.minute, BRIDGE_COOLDOWN_MIN)
                if mem.bump(key) > 3:
                    continue
                what = (f"a {g.get('b_name')} without power" if g.get("cover") else
                        f"power network {g.get('net')} has no generator")
                return Action("power_bridge", "R4", "G0", what,
                              {"a": g["a"], "b": g["b"], "d": g.get("d"), "net": g.get("net"),
                               "cover": bool(g.get("cover"))}, "connect the unpowered grid")
        return None

    # R5: burners out of fuel: a refuel round around them (the plan's own coal round covers its hand-fed burners)
    def r5_fuel(self, snap, mem, goal):
        if snap.burners_out <= 0 or not mem.ready("r5", snap.minute) or not snap.burners_out_at:
            return None
        # burners of the autopilot's own applied plans are the plan's coal round's job (and a burner just built
        # waits for its start fuel): only the others count here
        spots = []
        for b in snap.foreign_burners():
            if not any(abs(b[0] - s[0]) < 24 and abs(b[1] - s[1]) < 24 for s in spots):
                spots.append(b)
        if not spots:
            return None
        mem.hold("r5", snap.minute, REFUEL_COOLDOWN_MIN)
        return Action("refuel", "R5", "G0", f"{snap.burners_out} burners out of fuel", {"at": spots[:4]},
                      "refuel burners")

    # R6: the helper halted, or idle while an applied autopilot plan still has ghosts: restart its build
    def r6_helper(self, snap, mem, goal):
        h = snap.helper
        building = [p for p in snap.plans if p.get("owner") == "autopilot" and p.get("status") == "applied"
                    and int(p.get("ghosts") or 0) > 0]
        if not building:
            mem.helper_idle_since = None
            return None
        idle = not h.get("alive") or h.get("halted") or (not h.get("current") and not int(h.get("queue") or 0))
        busy_round = h.get("current") in ("round",) and not int(h.get("queue") or 0)
        if not (idle or busy_round):
            mem.helper_idle_since = None
            return None
        if mem.helper_idle_since is None:
            mem.helper_idle_since = snap.minute
            return None
        if snap.minute - mem.helper_idle_since < HELPER_IDLE_MIN:
            return None
        p = building[0]
        key = f"helper:{p['id']}"
        if not mem.ready(key, snap.minute):
            return None
        n = mem.bump(key)
        mem.hold(key, snap.minute, 2)
        mem.helper_idle_since = None
        return Action("helper_restart", "R6", goal and goal["id"],
                      f"helper {'halted' if h.get('halted') else 'idle'} with {p.get('ghosts')} ghosts left",
                      {"plan": p["id"], "attempt": n, "halted": safe_text(h.get("halted"))}, "build " + p["id"])

    # R7: a stage built more than 5 game minutes ago: compare its rates with the plan; a shortfall with a known
    # cause turns into a fix (fuel round, power growth); a surplus (full output) is fine; checked again later
    def r7_verify(self, snap, mem, goal):
        for p in snap.plans:
            pid = p.get("id")
            if p.get("owner") != "autopilot" or not p.get("built"):
                continue
            mem.built_at.setdefault(pid, snap.minute)
            prev = mem.verified.get(pid)
            if prev is not None and (prev.get("ok") or (prev.get("n", 1) >= 2 and not str(prev.get("fix") or "").startswith("more_")) or
                                     snap.minute - prev.get("at", 0) < REVERIFY_MIN):
                continue
            if snap.minute - mem.built_at[pid] < VERIFY_AFTER_MIN:
                continue
            want = self.stage_rates.get(pid) or {}
            short = {}
            for item, rate in want.items():
                if item in ("iron-plate", "copper-plate", "automation-science-pack", "logistic-science-pack") \
                        and rate > 0 and snap.made.get(item, 0) < VERIFY_SHORT * rate:
                    short[item] = [round(snap.made.get(item, 0), 1), round(rate, 1)]
            cause = diagnose(snap) if short else None
            fix = None
            neck = bottleneck(snap, short, want) if short else None
            if neck and cause not in ("no_fuel", "no_power", "low_power"):
                # the chain waits on a plate: every plate made is used (or fewer are made than the target asks).
                # A full furnace somewhere else is not the reason (live run: S3 made 8 of 13.5 red/min, R7 blamed a
                # full copper furnace and called it surplus while the red assemblers waited for iron)
                cause, fix = f"{neck}_short", "more_" + neck.split("-")[0]
                mem.need_plate = neck
            elif short and cause in ("full_output",):
                fix = "surplus"                # made less because nothing takes more: headroom, not a fault
            elif short and cause == "no_fuel":
                fix = "fuel_round"
            elif short and cause in ("no_power", "low_power"):
                fix = "power"
                mem.need_power = True
            elif short:
                fix = "recheck"
            ok = not short or fix == "surplus"
            mem.verified[pid] = {"ok": ok, "short": short, "at": snap.minute, "n": (prev or {}).get("n", 0) + 1,
                                 "fix": fix}
            args = {"plan": pid, "ok": ok, "short": short, "cause": cause, "fix": fix, "bbox": p.get("bbox"),
                    "balance": material_balance(snap, want)}
            if fix == "fuel_round":
                return Action("fuel_round", "R7", goal and goal["id"], f"{pid} short of fuel", args,
                              "refuel " + pid)
            return Action("verify", "R7", goal and goal["id"], "stage built: rates checked", args, "check " + pid)
        return None

    # R8: the next expansion the roadmap asks for (planner/expand.py): the mall (once the starter base's electric
    # drills are being built), power growth (when power use passes 66% of capacity, or VERIFY saw machines without
    # power), steel. The master plan grows: the new stage is appended and R2 offers it like any other stage.
    def r8_expand(self, snap, mem, goal):
        mid = active_master(snap)
        if not mid:
            return None
        m = snap.masters.get(mid) or {}
        have = m.get("templates") or {}
        goals = getattr(self, "goals", None) or goal_status(snap, mem.blocked)
        by = {g["id"]: g for g in goals}
        order = [i for i in mem.goal_order if i in by] + [g["id"] for g in goals if g["id"] not in mem.goal_order]
        mp = master_progress(snap, mid)
        # power before demand (PWR-1): machines short of power, or the load at 90% of capacity, grows power whatever
        # the goal (round 3: the mall comes with the electric drills now, before G4's needs are met)
        cap = float(snap.power.get("capacity_kw") or 0)
        if snap.low_power() > LOW_POWER_MACHINES or (cap > 0 and snap.use_kw() >= POWER_TIGHT * cap):
            mem.need_power = True
        tpl = self.grow_template(snap, mem, mp)
        if tpl:
            key = f"expand:{tpl}"
            n = mem.bump(key)
            mem.hold(key, snap.minute, GROW_COOLDOWN_MIN)
            plate = mem.need_plate
            mem.need_plate = None
            b = material_balance(snap).get(plate) or {}
            return Action("expand", "R8", goal and goal["id"],
                          f"{plate} is the bottleneck ({b.get('made', 0):.0f}/min made, {b.get('used', 0):.0f} used): "
                          f"one more smelting cell", {"master": mid, "template": tpl, "after": mp["top_started"],
                                                      "attempt": n}, f"more {plate.split('-')[0]}")
        for gid in order:
            g = by[gid]
            tpl = next_template(snap, g, have, mp, mem)
            if not tpl:
                continue
            key = f"expand:{tpl}"
            if not mem.ready(key, snap.minute):
                continue
            n = mem.bump(key)
            if n > EXPAND_TRIES:
                mem.blocked[gid] = snap.minute + 60
                mem.attempts[key] = 0
                return Action("plateau", "R8", gid, f"{tpl} does not fit here ({mem.failed.get(tpl) or 'no room'})",
                              {"why": tpl}, f"{tpl} does not fit")
            mem.hold(key, snap.minute, EXPAND_RETRY_MIN)
            mem.need_power = False if tpl == "power_grow" else mem.need_power
            return Action("expand", "R8", gid, f"{g['title']}: plan the {tpl.replace('_', ' ')} expansion",
                          {"master": mid, "template": tpl, "after": mp["top_started"], "attempt": n},
                          f"plan the {tpl.replace('_', ' ')}")
        # nothing the roadmap can still build: say so (the base keeps running and researching)
        if mp["all_done"] and not any(not g["done"] and g["available"] for g in goals if g["id"] != "G0"):
            if mem.ready("plateau", snap.minute):
                mem.hold("plateau", snap.minute, 30)
                return Action("plateau", "R8", goal and goal["id"], "every shipped goal is done; research continues",
                              {}, "research")
        return None

    def grow_template(self, snap, mem, mp) -> str | None:
        """iron_grow / copper_grow when VERIFY found that plate the bottleneck, it is still used up (TIGHT) and the
        starter base's own mining upgrade (the electric drills) is built: before it, that stage is the fix."""
        plate = mem.need_plate
        if not plate:
            return None
        tpl = f"{plate.split('-')[0]}_grow"
        if any(not snap.done(t) for t in EXPAND_GATE.get(tpl, [])):
            return None
        if any(s.get("key") == "electric" and not (s.get("row") or {}).get("built") for s in mp["stages"]):
            return None                  # S4's electric drills come first (they are the bigger step)
        if any(s.get("template") == tpl and not (s.get("row") or {}).get("built") for s in mp["stages"]):
            return None                  # the last cell is still being built
        b = material_balance(snap).get(plate) or {}
        if not b.get("tight"):
            mem.need_plate = None        # the shortfall is gone
            return None
        if not mem.ready(f"expand:{tpl}", snap.minute):
            return None
        return tpl

    # R10: defence. Not in a peaceful game. An attack in the last DEFEND_WINDOW_MIN with no turret near it: research gun
    # turrets first (in front of the queue), then place a turret group facing the attack. Without attacks, a nest the
    # pollution cloud already reaches gets a group on the base's side facing it (threats are re-read every
    # THREAT_EVERY_MIN). One group per spot per DEFEND_COOLDOWN_MIN.
    def r10_defense(self, snap, mem, goal):
        d = snap.defense
        if not d or d.get("peaceful") or snap.enemies.get("peaceful"):
            return None
        recent = [as_dict(t) for t in as_list(d.get("attacks"))
                  if float(as_dict(t).get("age_s") or 1e9) <= DEFEND_WINDOW_MIN * 60 and
                  not int(as_dict(t).get("turrets_near") or 0)]
        if not d.get("researched"):
            if recent and "gun-turret" not in snap.research.get("queue", []) and                     snap.research.get("current") != "gun-turret" and mem.ready("defend_research", snap.minute):
                mem.hold("defend_research", snap.minute, 5)
                queue = ["gun-turret"] + [t for t in ([snap.research.get("current")] + snap.research.get("queue", []))
                                          if t and t != "gun-turret"]
                return Action("research_set", "R10", "G0", "biters attacked and there are no turrets",
                              {"queue": queue[:6], "removed": []}, "research gun turrets")
            return None
        for t in recent:
            key = f"defend:{int(float(t['x']) // 24)}:{int(float(t['y']) // 24)}"
            if mem.ready(key, snap.minute):
                mem.hold(key, snap.minute, DEFEND_COOLDOWN_MIN)
                return Action("defend", "R10", "G0", f"biters destroyed a {t.get('what')} here",
                              {"x": t["x"], "y": t["y"], "toward_x": t["x"], "toward_y": t["y"], "n": 4},
                              "turrets where biters attacked")
        if mem.ready("threats", snap.minute):
            mem.hold("threats", snap.minute, THREAT_EVERY_MIN)
            return Action("defend_threats", "R10", "G0", "check nests the pollution reaches", {},
                          "check nests near the base")
        return None

    # R9: the mall exists but the helper still hand-crafts a lot: name the items (the mall row's reserved slots get
    # them in the next mall extension; until then the share is reported, never hidden)
    def r9_mall(self, snap, mem, goal):
        c = snap.counters()
        if not (snap.ap.get("mall_chests") and float(c.get("share_1h") or 0) > K.handcraft()["mall_pressure_share"]):
            return None
        if not mem.ready("r9", snap.minute):
            return None
        mem.hold("r9", snap.minute, 30)
        short = sorted(as_dict(c.get("short")).items(), key=lambda kv: -float(kv[1] or 0))[:3]
        return Action("note", "R9", goal and goal["id"], "hand-craft share above 40% with a mall",
                      {"share_1h": c.get("share_1h"), "short": [k for k, _ in short if safe_name(k)]},
                      "mall")


def next_template(snap: Snapshot, g: dict, have: dict, mp: dict, mem: Memory) -> str | None:
    """The expansion template (planner/expand.py) goal `g` asks for now, or None: its templates in order, skipping
    those the master has; one waits for its research (EXPAND_GATE) and for a stage of the same template still being
    built. power_grow may come again (VERIFY saw machines without power)."""
    if not g["available"] or g["blocked"]:
        return None
    for name in g.get("templates") or [g["template"]]:
        tpl = EXPAND.get(name)
        if not tpl:
            return None
        if tpl == "power_grow":
            if not (mem.need_power or (not g["done"] and g["ready"])):
                return None
        elif g["done"] or not g["ready"]:
            return None
        if tpl in have and tpl != "power_grow":
            continue                     # built (or planned) already: the goal's next template, if any
        if any(not snap.done(t) for t in EXPAND_GATE.get(tpl, [])):
            return None
        if any(s.get("template") == tpl and not (s.get("row") or {}).get("built") for s in mp["stages"]):
            return None                  # a stage of this template is still being built: wait for it
        return tpl
    return None


def goal_actionable(snap: Snapshot, g: dict | None, masters_have: dict | None = None) -> bool:
    """Can the autopilot still DO something for goal g (as opposed to waiting for a rate to show)? Round 3: with
    every red + green tech researched and the second science block built, G8 (60 science/min) stayed the current goal;
    nothing could raise the rate (no research takes the packs), and the stuck ladder ran replan, restart, ask Claude
    and block goal on a finished base. A goal whose templates are all built, whose techs are all done or out of reach
    and whose master stages are all built is only being watched."""
    if g is None:
        return False
    if g["id"] in ("G0",):
        return True
    have = masters_have or {}
    for name in g.get("templates") or [g["template"]]:
        tpl = EXPAND.get(name)
        if tpl and (tpl not in have or tpl == "power_grow"):
            return True                  # an expansion still to plan (or its research to wait for)
    if g["template"] == "starter":
        mp = master_progress(snap, active_master(snap) or adoptable_master(snap))
        if not mp["all_done"]:
            return True
    techs = [t for t in g.get("techs") or [] if not snap.done(t)]
    if techs and (snap.research.get("current") in techs or set(techs) & set(snap.research.get("queue") or [])):
        return True
    return False


class _GD:
    def __init__(self, techs):
        self.raw = {"technologies": techs}


def safe_text(v, n=60) -> str | None:
    """A short status text from the game (the helper's own halt reason) with anything but plain words removed."""
    if v is None:
        return None
    s = re.sub(r"[^A-Za-z0-9 ,.:;_()/-]", " ", str(v))
    return " ".join(s.split())[:n]


def material_balance(snap: Snapshot, want: dict | None = None) -> dict:
    """{plate: {made, used, need, tight, slack}} per minute over 10 game minutes. need: the plates the targets in
    `want` ask for (PLATES_PER of their pack rates, or a plate target itself). tight: everything made is used (the
    chain waits on it); slack: well over half of it is left over."""
    want = want or {}
    out = {}
    for plate in ("iron-plate", "copper-plate"):
        made, used = float(snap.made.get(plate, 0)), float(snap.used.get(plate, 0))
        need = sum(float(r) * PLATES_PER.get(i, {}).get(plate, 0) for i, r in want.items())
        if not any(i in PLATES_PER for i in want):
            need = float(want.get(plate, 0))
        tight = made > 0 and used >= TIGHT * made     # (fewer made than needed with plenty left over: nothing takes it)
        out[plate] = {"made": round(made, 1), "used": round(used, 1), "need": round(need, 1), "tight": bool(tight),
                      "slack": bool(made > 0 and used < SLACK * made)}
    return out


def bottleneck(snap: Snapshot, short: dict, want: dict | None = None) -> str | None:
    """The plate a shortfall comes from, or None: a short plate itself, or for short science the plate its packs
    need that is used up (the one with the least left over first)."""
    bal = material_balance(snap, {i: r for i, r in (want or {}).items() if i in short})
    cands = []
    for item in short:
        if item in bal and bal[item]["tight"]:
            cands.append(item)
        for plate in PLATES_PER.get(item, {}):
            if bal[plate]["tight"]:
                cands.append(plate)
    if not cands:
        return None
    return min(cands, key=lambda p: (bal[p]["made"] - bal[p]["used"]) / max(bal[p]["made"], 1.0))


def diagnose(snap: Snapshot) -> str:
    """Known causes of a shortfall, from the problem groups: no_fuel / no_power / output_full / no_input."""
    counts = {}
    for g in as_list(snap.problems.get("groups")):
        g = as_dict(g)
        counts[str(g.get("status"))] = counts.get(str(g.get("status")), 0) + int(g.get("count") or 0)
    for cause in ("no_fuel", "no_power", "low_power", "item_ingredient_shortage", "no_ingredients", "full_output",
                  "no_input_fluid",
                  "waiting_for_source_items", "no_minable_resources"):
        if counts.get(cause):
            return cause
    return "unknown"


# ---- stuck ladder (4.6) --------------------------------------------------------------------------------------------
LADDER = ("replan", "restart_helper", "ask_claude", "block_goal")


@dataclass
class StuckTracker:
    last_key: dict | None = None
    last_progress_min: float = 0.0
    rung: int = 0
    ladders: int = 0
    periods: list = field(default_factory=list)       # [{start, end, rungs, resolved}]
    _open: dict | None = None

    def update(self, snap: Snapshot) -> str | None:
        """-> the ladder step to take now ("replan", ...), "halt", or None. Call once per observation."""
        key = progress_key(snap)
        now = snap.minute
        if progressed(self.last_key, key):
            self.last_key = key
            self.last_progress_min = now
            if self._open:
                self._open.update(end=now, resolved=True)
                self.periods.append(self._open)
                self._open = None
            self.rung = 0
            self.ladders = 0
            return None
        self.last_key = key
        idle = now - self.last_progress_min
        if idle < STUCK_MIN + RUNG_GAP_MIN * self.rung:
            return None
        if self._open is None:
            self._open = {"start": self.last_progress_min, "end": None, "rungs": [], "resolved": False}
        step = LADDER[self.rung]
        self._open["rungs"].append(step)
        self.rung += 1
        if self.rung >= len(LADDER):
            self.rung = 0
            self.ladders += 1
            self.last_progress_min = now          # the next ladder starts its own 5 minutes
            if self.ladders >= LADDERS_TO_HALT:
                return "halt"
        return step

    def update_idle(self, snap: Snapshot) -> None:
        """No goal to work on: keep the progress clock running without climbing the ladder."""
        self.last_key = progress_key(snap)
        self.last_progress_min = snap.minute
        self.rung = 0
        return None

    def stuck_now(self, snap: Snapshot) -> float:
        return snap.minute - self.last_progress_min


# ---- prompt material (no free text) --------------------------------------------------------------------------------
def prompt_snapshot(snap: Snapshot, goals: list | None = None) -> dict:
    """A compact (<= ~2 KB) view of the snapshot for Claude: numbers, prototype names and our own ids only."""
    def top(d, n):
        return dict(sorted(((k, round(v, 1)) for k, v in d.items() if safe_name(k)), key=lambda kv: -kv[1])[:n])
    plans = [{"id": safe_id(p.get("id")), "status": safe_name(p.get("status")), "owner": safe_name(p.get("owner")),
              "ghosts": int(p.get("ghosts") or 0), "built": bool(p.get("built"))}
             for p in snap.plans[:12] if safe_id(p.get("id"))]
    c = snap.counters()
    return {"game_min": round(snap.minute, 1),
            "research": {"current": snap.research.get("current"), "queue": snap.research.get("queue", [])[:5],
                         "done_n": len(snap.research.get("done") or ())},
            "made_per_min": top(snap.made, 12),
            "power": {"capacity_kw": snap.power.get("capacity_kw"), "low_or_no_power": snap.low_power(),
                      "split_grids": len(snap.power_gaps)},
            "burners_out_of_fuel": snap.burners_out,
            "census": top({k: float(v) for k, v in snap.census.items()}, 15),
            "helper": {"alive": bool(snap.helper.get("alive")), "task": safe_name(snap.helper.get("current")),
                       "queue": int(snap.helper.get("queue") or 0), "halted": bool(snap.helper.get("halted")),
                       "craft_s_10min": c.get("craft_s_10min"), "handcraft_share_1h": c.get("share_1h")},
            "plans": plans,
            "goals": [{"id": g["id"], "done": g["done"], "ready": g["ready"], "shipped": g["available"]}
                      for g in (goals or [])]}
