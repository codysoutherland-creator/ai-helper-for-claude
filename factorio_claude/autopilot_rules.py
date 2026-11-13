"""The autopilot's deterministic part (AUTOPILOT.md 4.2-4.6): the Snapshot it observes, goal status, the Director's
rules R0-R12 and progress / stuck tracking. Pure functions on plain data: no link, no Claude, unit-tested
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
# defence v2 (doctrine RES-5)
DEFENSE_TECHS = ("gun-turret", "stone-wall")    # researched before the first attack (with enemies on the map)
NEST_NEAR_TILES = 160           # a nest within 5 chunks of the base's buildings: defence research at once
DEFEND_RESEARCH_HOLD_MIN = 5.0
WALLS_EVERY_MIN = 5.0           # turret groups without a wall arc are walled (2 per check)
THREAT_GROUPS_MAX = 2           # turret groups placed per threats check (the helper builds and arms each)
FORECAST_CHUNKS = 1             # a nest this many chunks outside the pollution cloud is defended before it reaches it
FORECAST_GROWING = 1            # ... one chunk more while the cloud grows,
FORECAST_FAST = 0.02            # ... and one more while it grows by more than 2% a minute
FORECAST_TILES_PER_CHUNK = 32.0  # ranking: each chunk the cloud still has to spread counts like 32 tiles farther away
# radar network (R11)
RADAR_TECH = "radar"            # 2.1: the radar recipe is locked behind it (20 red packs; data export, not 1.1's free radar)
RADAR_KW = 300.0                # what one radar draws, all the time
RADAR_HEADROOM = 0.8            # a radar only while the load with it stays under 80% of capacity (R8 grows at 90%)
RADAR_SPAN = 192                # one more radar per 192 tiles of the master's span (its bounding box's longer side)
RADAR_MAX = 6
RADAR_RETRY_MIN = 20.0
RADAR_RESEARCH_HOLD_MIN = 10.0
RADAR_FULL = "of a radar already"   # the planner's error: no edge spot is far enough from the radars there are
RADAR_REGROW = 32.0             # ... the exploring radar is tried again once the master's span grew by this much
RECHART_MIN = 8                 # charted chunks grown by this many (and RECHART_SHARE): expansions that found nothing
RECHART_SHARE = 0.1             # on the charted map are tried again
# robot network (R12, roadmap G11)
ROBOT_TECH = "construction-robotics"
ROBOTS_PER_PORT = 25            # construction robots the network should have per roboport (one batch of seeds at most
ROBOTS_MIN, ROBOTS_MAX = 20, 200   # SEED_BATCH: the robot mall keeps 50 in its chest)
SEED_BATCH = 50
SEED_COOLDOWN_MIN = 5.0
LOGISTIC_ROBOTS = 20            # logistic robots once Logistic robotics is researched (after the construction robots)
LOGISTIC_CHESTS_MIN = 10.0      # mall chests inside the logistic area marked for upgrade at most this often
ROBOPORTS_RETRY_MIN = 15.0
ROBOT_STALL_MIN = 5.0           # a plan whose ghosts are all the robots' and none went in this long: the helper builds
# expansion template -> the resource it failed to find on the charted map (its error says so: radar explores for it)
EXPLORE_FOR = {"oil": "crude-oil", "iron_grow": "iron-ore", "copper_grow": "copper-ore"}
# (the grow templates' bus fallback says "no iron or copper ore patch in reach")
EXPLORE_ERR = re.compile(r"no crude oil|charted|patch with room|no [a-z -]+ patch")
RED = "automation-science-pack"
# roadmap template -> planner/expand.py template (the others are the starter base's or research's)
EXPAND = {"mall": "mall", "power_grow": "power_grow", "steel_column": "steel", "science_block": "science",
          "oil": "oil", "oil_plant": "oil_plant", "iron_grow": "iron_grow", "copper_grow": "copper_grow", "blue": "blue",
          "robots": "robots"}
# research an expansion template waits for (the mall's cells and machines include electric drills: round 3 moved the
# mall up to the electric-drill stage, so its research is a gate)
EXPAND_GATE = {"mall": ["automation", "electric-mining-drill", "logistics"], "power_grow": [], "mall_add": [],
               "steel": ["steel-processing"],
               "science": ["logistic-science-pack", "logistics"], "oil": ["oil-gathering", "fluid-handling"],
               "oil_plant": ["oil-processing", "plastics"], "iron_grow": ["electric-mining-drill"],
               "copper_grow": ["electric-mining-drill"], "radar": [RADAR_TECH],
               # round 4: blue science (engines, advanced circuits, sulfur; the pack recipe last - all red + green)
               "blue": ["engine", "advanced-circuit", "sulfur-processing", "chemical-science-pack"],
               # round 5 (G11): the robot mall's recipes (Robotics and its chain are their prerequisites), the roboport
               # grid R12 grows (roboports and storage chests: Construction robotics)
               "robots": ["construction-robotics", "logistic-robotics"], "roboports": [ROBOT_TECH]}
# plates per science pack (red: 1 copper + a gear; green: an inserter + a belt) - what a pack rate asks of the smelting
# (blue: an engine - a steel plate of 5 iron, a gear, two pipes - and 1.5 advanced circuits - 3 green circuits, 6 cables)
PLATES_PER = {"automation-science-pack": {"iron-plate": 2.0, "copper-plate": 1.0},
              "logistic-science-pack": {"iron-plate": 5.5, "copper-plate": 1.5},
              "chemical-science-pack": {"iron-plate": 12.0, "copper-plate": 7.5}}
TIGHT = 0.85                    # a plate used at >= 85% of what is made: nothing left over, the chain waits on it
SLACK = 0.5                     # a plate used at < 50% of what is made: over-produced (it backs up; nothing breaks)
GROW_COOLDOWN_MIN = 20.0        # one more smelting cell per plate per 20 game minutes
SHIPPED_ROUND = 5               # roadmap goals of rounds <= this have a template the autopilot can build (5: robots)
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
    #                             | refuel | helper_restart | verify | wait | plateau | seed_robots | logistic_chests
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
    chart: dict = field(default_factory=dict)         # {chunks: charted by the force, radars: built}
    robots: dict = field(default_factory=dict)        # robot network (autopilot.lua robot_status): networks, roboports,
    #                                                   construction / available / logistic robots, storage, ports,
    #                                                   storages, upgradable mall chests, providers (passive providers)

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
                   masters=masters or {}, enemies=as_dict(o.get("enemies")), defense=as_dict(o.get("defense")),
                   chart=as_dict(o.get("chart")), robots=as_dict(o.get("robots")))

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
    if m == "robot_network":
        # a roboport with construction robots in its network (the robot mall's robots put in by the helper)
        r = snap.robots
        v = float(r.get("construction") or 0) if int(r.get("roboports") or 0) >= cond.get("min_roboports", 1) else 0.0
        return v >= cond.get("min_robots", 1), v
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
    charted_seen: int = 0                             # charted chunks when expansions were last retried (recharted)
    radar_span: float = -1.0                          # the master's span when R11 last planned a radar
    robot_wait: dict = field(default_factory=dict)    # plan id -> (game minute, ghosts) since its ghosts are all the
    #                                                   robots' (R6 waits for them; ROBOT_STALL_MIN without one built:
    #                                                   the helper builds them)

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
                     self.r5_fuel, self.r3_starter, self.r2_next_stage, self.r8_expand, self.r11_radar,
                     self.r12_robots, self.r6_helper, self.r7_verify, self.r9_mall):
            if planner_busy and rule in (self.r3_starter, self.r2_next_stage, self.r8_expand, self.r11_radar):
                continue
            if planner_busy and rule == self.r12_robots:
                rule = self.r12_robots_upkeep         # (the roboport expansion waits; seeding goes on)
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
                                 needed=needed, limit=6, peaceful=snap.peaceful,
                                 defend=not snap.peaceful and RED in snap.automated_packs())
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

    # R6: the helper halted, or idle while an applied autopilot plan still has ghosts: restart its build. Round 5: a plan
    # whose ghosts are all ones the construction robots will build (plan row robot_ghosts: inside a network with robots
    # and the items in it) is left to them while they make progress; ROBOT_STALL_MIN without one of its ghosts built
    # (something the robots cannot do) the helper builds them after all (no_robots)
    def r6_helper(self, snap, mem, goal):
        h = snap.helper
        building, stalled = [], []
        for p in snap.plans:
            if p.get("owner") != "autopilot" or p.get("status") != "applied" or int(p.get("ghosts") or 0) <= 0:
                continue
            n, robots = int(p.get("ghosts") or 0), int(p.get("robot_ghosts") or 0)
            if robots < n:
                mem.robot_wait.pop(p.get("id"), None)
                building.append(p)
                continue
            since = mem.robot_wait.get(p.get("id"))
            if since is None or n < since[1]:
                mem.robot_wait[p.get("id")] = (snap.minute, n)       # the robots are on it (or just built one)
            elif snap.minute - since[0] >= ROBOT_STALL_MIN:
                stalled.append(p)
        if stalled:
            p = stalled[0]
            key = f"helper:{p['id']}"
            if mem.ready(key, snap.minute):
                n = mem.bump(key)
                mem.hold(key, snap.minute, 2)
                mem.robot_wait[p["id"]] = (snap.minute, int(p.get("ghosts") or 0))
                return Action("helper_restart", "R6", goal and goal["id"],
                              f"the robots built none of {p.get('ghosts')} ghosts in {ROBOT_STALL_MIN:.0f} game "
                              f"minutes: the helper builds them", {"plan": p["id"], "attempt": n, "no_robots": True},
                              "build " + p["id"])
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
                if item in ("iron-plate", "copper-plate") + tuple(PLATES_PER) \
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
        recharted(snap, mem, goals)          # radar network: a template that found nothing charted may fit now
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
        add = mall_add_items(snap, m, mp)
        key = "expand:mall_add:" + "+".join(add)
        if add and mem.ready(key, snap.minute):
            # defence v2: firearm magazines (and anything else researched after the mall was planned) into the mall
            # row's free slots, so the helper takes them there instead of hand-crafting (tries per item set)
            n = mem.bump(key)
            mem.hold(key, snap.minute, EXPAND_RETRY_MIN)
            if n <= EXPAND_TRIES:
                return Action("expand", "R8", goal and goal["id"], f"the mall can make {', '.join(add)} now",
                              {"master": mid, "template": "mall_add", "after": mp["top_started"], "attempt": n},
                              "more mall items")
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

    # R11: the radar network. A radar next to the hub once the base has power, then one more on the base's edge as the
    # master grows (radars_wanted: one per RADAR_SPAN tiles of its span) and one more while an expansion found nothing
    # on the charted map (crude oil, an ore patch with room: explore_targets; the planner points it that way). The
    # Radar tech first (2.1 locks the recipe: queued at the end, once red science runs). Bounded by power: each radar
    # draws RADAR_KW all the time, so one only while the load with it stays under RADAR_HEADROOM of capacity; an
    # exploring radar that does not fit asks for power growth (R8), a growth radar just waits.
    def r11_radar(self, snap, mem, goal):
        mid = active_master(snap)
        if not mid:
            return None
        cap = float(snap.power.get("capacity_kw") or 0)
        if cap <= 0:
            return None                              # no power yet: nothing would run it
        m = snap.masters.get(mid) or {}
        mp = master_progress(snap, mid)
        rad = as_dict(m.get("radar"))
        have = max(int(rad.get("n") or 0), int(snap.census.get("radar", 0)))
        toward = explore_targets(mem)
        span = float(rad.get("span") or 0)
        if toward and RADAR_FULL in str(mem.failed.get("radar") or "") and span < mem.radar_span + RADAR_REGROW:
            # review: on a starter-sized base no edge spot is radar_spacing from the hub radar; the exploring radar
            # was retried (and asked R8 for power) every hour without a spot ever existing. It waits for the base
            # to grow (its edge moves out) instead
            toward = []
        want = radars_wanted(rad, toward)
        if have >= want:
            return None
        if any(s.get("template") == "radar" and not (s.get("row") or {}).get("built") for s in mp["stages"]):
            return None                              # the last radar is still being built
        if not snap.done(RADAR_TECH):
            cur = snap.research.get("current")
            queue = ([cur] if cur else []) + [t for t in snap.research.get("queue", []) if t != cur]
            if RADAR_TECH in queue or RED not in snap.automated_packs() or snap.labs_n() == 0 or \
                    not mem.ready("radar_research", snap.minute) or not self._tech_ready(snap, RADAR_TECH):
                return None
            mem.hold("radar_research", snap.minute, RADAR_RESEARCH_HOLD_MIN)
            return Action("research_set", "R11", goal and goal["id"], "a radar needs the Radar tech (2.1)",
                          {"queue": (queue[:5] + [RADAR_TECH]), "removed": []}, "research radar")
        use = snap.use_kw()
        if use + RADAR_KW > RADAR_HEADROOM * cap:
            if toward:
                mem.need_power = True                # R8 grows power; the exploring radar comes after
            return None
        key = "expand:radar"
        if not mem.ready(key, snap.minute):
            return None
        n = mem.bump(key)
        if n > EXPAND_TRIES:
            mem.attempts[key] = 0
            mem.hold(key, snap.minute, 60)
            return Action("note", "R11", goal and goal["id"],
                          f"no spot for radar {have + 1} ({mem.failed.get('radar') or 'no room'})", {"why": "radar"},
                          "radar does not fit")
        mem.hold(key, snap.minute, RADAR_RETRY_MIN)
        mem.radar_span = span
        why =("the base has power: a radar at the hub" if have == 0 and not toward else
               f"find {', '.join(t.replace('-', ' ') for t in toward)}: a radar toward it" if toward else
               f"the base grew to {float(rad.get('span') or 0):.0f} tiles: radar {have + 1} on its edge")
        return Action("expand", "R11", goal and goal["id"], why,
                      {"master": mid, "template": "radar", "after": mp["top_started"], "attempt": n,
                       "toward": toward, "headroom_kw": round(RADAR_HEADROOM * cap - use, 1)},
                      "place a radar")

    # R10: defence. Not in a peaceful game. An attack in the last DEFEND_WINDOW_MIN with no turret near it: research gun
    # turrets first (in front of the queue), then place a turret group facing the attack. Without attacks, a nest the
    # pollution cloud already reaches gets a group on the base's side facing it (threats are re-read every
    # THREAT_EVERY_MIN). One group per spot per DEFEND_COOLDOWN_MIN.
    # Defence v2 (doctrine RES-5): Gun turrets and Stone wall are researched before the first attack - once red
    # science runs (after the tech being researched), at once (in front) when a nest is in the cloud or within
    # NEST_NEAR_TILES of the base or biters attacked; turret groups get a wall arc once walls are researched (groups
    # placed before that are walled later); the threats check also defends nests at the cloud's edge (rank_threats).
    def r10_defense(self, snap, mem, goal):
        d = snap.defense
        if not d or d.get("peaceful") or snap.peaceful:      # (peaceful mode, or no nest on the map)
            return None
        recent = [as_dict(t) for t in as_list(d.get("attacks"))
                  if float(as_dict(t).get("age_s") or 1e9) <= DEFEND_WINDOW_MIN * 60 and
                  not int(as_dict(t).get("turrets_near") or 0)]
        near = d.get("nest_dist")
        urgent = (bool(recent) or int(d.get("polluted_nests") or 0) > 0 or
                  (near is not None and float(near) <= NEST_NEAR_TILES))
        red = RED in snap.automated_packs()
        if not d.get("researched"):
            return self._defense_research(snap, mem, urgent, red, recent)
        for t in recent:
            key = f"defend:{int(float(t['x']) // 24)}:{int(float(t['y']) // 24)}"
            if mem.ready(key, snap.minute):
                mem.hold(key, snap.minute, DEFEND_COOLDOWN_MIN)
                return Action("defend", "R10", "G0", f"biters destroyed a {t.get('what')} here",
                              {"x": t["x"], "y": t["y"], "toward_x": t["x"], "toward_y": t["y"], "n": 4,
                               "walls": bool(d.get("walls_researched"))},
                              "turrets where biters attacked")
        if not d.get("walls_researched"):
            a = self._defense_research(snap, mem, urgent, red, recent)
            if a is not None:
                return a
        groups = [as_dict(g) for g in as_list(d.get("unwalled")) if as_dict(g).get("x") is not None]
        if d.get("walls_researched") and groups and mem.ready("walls", snap.minute):
            mem.hold("walls", snap.minute, WALLS_EVERY_MIN)
            return Action("defend_walls", "R10", "G0", f"{len(groups)} turret group(s) without a wall",
                          {"groups": groups[:THREAT_GROUPS_MAX]}, "walls in front of the turrets")
        if mem.ready("threats", snap.minute):
            mem.hold("threats", snap.minute, THREAT_EVERY_MIN)
            return Action("defend_threats", "R10", "G0", "check nests the pollution reaches or is about to",
                          {"trend": float(d.get("pollution_trend") or 0), "pollution": float(d.get("pollution") or 0),
                           "max_groups": THREAT_GROUPS_MAX, "walls": bool(d.get("walls_researched"))},
                          "check nests near the base")
        return None

    # R12: the robot network (roadmap G11, after blue science). Once the robot mall (G11's template, R8) is built: the
    # roboport grid - roboports covering the master's built area with their construction areas, one network - grown a
    # stage at a time while the masters summary counts built-area points no construction area reaches (repeatable: the
    # base grows); the helper puts the robot mall's construction robots into a roboport (bootstrap: ROBOTS_PER_PORT per
    # roboport), carries passive provider chests into a storage chest and marks the mall chests inside the logistic
    # area for upgrade (the robots swap them: logistic chests replace hand-fetching), and later a few logistic robots.
    def r12_robots(self, snap, mem, goal):
        mid = active_master(snap)
        if not mid or not snap.done(ROBOT_TECH):
            return None
        m = snap.masters.get(mid) or {}
        mp = master_progress(snap, mid)
        stages = mp["stages"]
        mall_built = any(s.get("template") == "robots" and (s.get("row") or {}).get("built") for s in stages)
        cov = as_dict(m.get("robots"))
        building = any(s.get("template") == "roboports" and not (s.get("row") or {}).get("built") for s in stages)
        if mall_built and (int(cov.get("uncovered") or 0) > 0 or int(cov.get("mall_out") or 0) > 0) and \
                not building and \
                all(snap.done(t) for t in EXPAND_GATE["roboports"]):
            key = "expand:roboports"
            if mem.ready(key, snap.minute):
                n = mem.bump(key)
                mem.hold(key, snap.minute, ROBOPORTS_RETRY_MIN)
                if n > EXPAND_TRIES:                  # (no room: tried again in an hour, as R11's radar)
                    mem.attempts[key] = 0
                    mem.hold(key, snap.minute, 60)
                    return Action("note", "R12", goal and goal["id"],
                                  f"no room for more roboports ({mem.failed.get('roboports') or 'no spot'})",
                                  {"why": "roboports"}, "roboports do not fit")
                if n <= EXPAND_TRIES:
                    first = int(cov.get("n") or 0) == 0
                    return Action("expand", "R12", goal and goal["id"],
                                  "the robot mall is built: the first roboport and storage chests by the hub" if first
                                  else f"{cov.get('uncovered')} of {cov.get('points')} spots of the base outside the "
                                       f"robots' construction areas, {cov.get('mall_out') or 0} mall chests outside "
                                       f"the network: more roboports",
                                  {"master": mid, "template": "roboports", "after": mp["top_started"], "attempt": n},
                                  "plan roboports")
        return self.r12_robots_upkeep(snap, mem, goal)

    def r12_robots_upkeep(self, snap, mem, goal):
        """R12's bootstrap carries and the mall chests' upgrade (no planner job: they go on while one runs)."""
        r = snap.robots
        if not r or not snap.done(ROBOT_TECH) or int(r.get("roboports") or 0) <= 0:
            return None
        stock = as_dict(snap.ap.get("mall_stock"))
        ports = [as_list(p) for p in as_list(r.get("ports")) if len(as_list(p)) >= 2]
        stores = [as_list(p) for p in as_list(r.get("storages")) if len(as_list(p)) >= 2]
        have = int(r.get("construction") or 0)
        want = max(ROBOTS_MIN, min(ROBOTS_MAX, ROBOTS_PER_PORT * int(r.get("roboports") or 0)))
        if have < want and ports and float(stock.get("construction-robot") or 0) > 0 and \
                mem.ready("seed:construction-robot", snap.minute):
            mem.hold("seed:construction-robot", snap.minute, SEED_COOLDOWN_MIN)
            n = int(min(SEED_BATCH, want - have, float(stock["construction-robot"])))
            p = min(ports, key=lambda q: float(q[2]) if len(q) > 2 else 0.0)   # the emptiest roboport
            return Action("seed_robots", "R12", goal and goal["id"],
                          f"{have} construction robots in the network, {want} wanted: the helper brings {n} from the "
                          f"robot mall", {"item": "construction-robot", "count": n, "x": p[0], "y": p[1],
                                          "into": "roboport"}, "construction robots into a roboport")
        upgradable, providers = int(r.get("upgradable") or 0), int(r.get("providers") or 0)
        if upgradable > 0 and have > 0:
            if providers < upgradable and stores and float(stock.get("passive-provider-chest") or 0) > 0 and \
                    mem.ready("seed:passive-provider-chest", snap.minute):
                mem.hold("seed:passive-provider-chest", snap.minute, SEED_COOLDOWN_MIN)
                n = int(min(SEED_BATCH, upgradable - providers, float(stock["passive-provider-chest"])))
                return Action("seed_robots", "R12", goal and goal["id"],
                              f"{upgradable} mall chests in the logistic area: the helper brings {n} passive provider "
                              f"chests to the robots' storage", {"item": "passive-provider-chest", "count": n,
                                                                 "x": stores[0][0], "y": stores[0][1],
                                                                 "into": "storage-chest"},
                              "passive provider chests into storage")
            if providers > 0 and mem.ready("logistic_chests", snap.minute):
                mem.hold("logistic_chests", snap.minute, LOGISTIC_CHESTS_MIN)
                return Action("logistic_chests", "R12", goal and goal["id"],
                              f"{upgradable} mall chests in the logistic area become passive providers (the robots "
                              f"take what ghosts need straight from the mall)", {"max": min(upgradable, providers)},
                              "mall chests to passive providers")
        logistic = int(r.get("logistic") or 0)
        if snap.done("logistic-robotics") and have >= want and logistic < LOGISTIC_ROBOTS and ports and \
                float(stock.get("logistic-robot") or 0) > 0 and mem.ready("seed:logistic-robot", snap.minute):
            mem.hold("seed:logistic-robot", snap.minute, SEED_COOLDOWN_MIN)
            n = int(min(LOGISTIC_ROBOTS - logistic, float(stock["logistic-robot"])))
            return Action("seed_robots", "R12", goal and goal["id"], f"the helper brings {n} logistic robots",
                          {"item": "logistic-robot", "count": n, "x": ports[0][0], "y": ports[0][1],
                           "into": "roboport"}, "logistic robots into a roboport")
        return None

    def _defense_research(self, snap, mem, urgent, red, recent):
        """Gun turrets / Stone wall into the research queue: in front when urgent (a nest near or polluted, an
        attack), else right after the tech being researched once red science runs. None when queued already."""
        if not (urgent or red) or not mem.ready("defend_research", snap.minute):
            return None
        d = snap.defense
        have = {"gun-turret": bool(d.get("researched")), "stone-wall": bool(d.get("walls_researched"))}
        want = [t for t in DEFENSE_TECHS if not have.get(t) and not snap.done(t) and self._tech_ready(snap, t)]
        cur = snap.research.get("current")
        queue = ([cur] if cur else []) + [t for t in snap.research.get("queue", []) if t != cur]
        if not want:
            return None
        if urgent:
            # Automation stays first while it runs on the 10 hand-made packs (R1b): in front of it the labs would
            # spend those packs on the turrets and Automation would need another hand-made batch
            pin = ["automation"] if "automation" in queue and not snap.done("automation") else []
            if queue[:len(pin) + len(want)] == pin + want:
                return None
            new = pin + want + [t for t in queue if t not in want and t not in pin]
        else:
            if all(t in queue for t in want):
                return None
            new = queue[:1] + want + [t for t in queue[1:] if t not in want]
        mem.hold("defend_research", snap.minute, DEFEND_RESEARCH_HOLD_MIN)
        why = ("biters attacked and there are no turrets" if recent else
               "a nest is near the base or in the pollution cloud" if urgent else
               "red science runs: turrets and walls before the first attack")
        return Action("research_set", "R10", "G0", why, {"queue": new[:6], "removed": []},
                      "research " + " and ".join(t.replace("-", " ") for t in want))

    def _tech_ready(self, snap, tech) -> bool:
        """Its prerequisites are researched (unknown without the game's tech table: assume so)."""
        t = self.techs.get(tech)
        if not t:
            return True
        pre = t.get("prerequisites") or []
        pre = list(pre.keys()) if isinstance(pre, dict) else list(pre)
        return all(snap.done(p) for p in pre)

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


def mall_add_items(snap: Snapshot, m: dict, mp: dict) -> list:
    """Items the active master's built mall could add now (masters summary "mall_addable": {item: tech}, the tech
    researched), once the mall stage is built and no mall_add stage is still being built."""
    addable = as_dict(m.get("mall_addable"))
    if not addable or "mall" not in (m.get("templates") or {}):
        return []
    stages = mp.get("stages") or []
    if not any(s.get("template") == "mall" and (s.get("row") or {}).get("built") for s in stages):
        return []
    if any(s.get("template") == "mall_add" and not (s.get("row") or {}).get("built") for s in stages):
        return []
    return sorted(i for i, t in addable.items() if snap.done(str(t)))


def explore_targets(mem: Memory) -> list:
    """Resources an expansion did not find on the charted map (mem.failed errors: no crude oil, no ore patch with
    room): the next radar looks for them."""
    return sorted({EXPLORE_FOR[t] for t, err in mem.failed.items() if t in EXPLORE_FOR and EXPLORE_ERR.search(str(err))})


def radars_wanted(rad: dict, toward=()) -> int:
    """Radars the base should have: one, one more per RADAR_SPAN tiles of the master's span (masters summary "radar":
    {n, span}), one more while something has to be found; at most RADAR_MAX."""
    span = float(as_dict(rad).get("span") or 0)
    return min(RADAR_MAX, 1 + int(span // RADAR_SPAN) + (1 if toward else 0))


def recharted(snap: Snapshot, mem: Memory, goals: list | None = None) -> list:
    """Radar network: once the charted chunk count (observe "chart") grew by RECHART_MIN and RECHART_SHARE since the
    last look, expansions that found nothing on the charted map get their tries back and their goals unblocked (R8
    plans them again on the new map). -> the templates freed."""
    n = int(as_dict(snap.chart).get("chunks") or 0)
    if n <= 0:
        return []
    if mem.charted_seen <= 0:
        mem.charted_seen = n
        return []
    if n - mem.charted_seen < max(RECHART_MIN, RECHART_SHARE * mem.charted_seen):
        return []
    mem.charted_seen = n
    freed = [t for t, err in mem.failed.items() if t in EXPLORE_FOR and EXPLORE_ERR.search(str(err))]
    for t in freed:
        mem.failed.pop(t, None)
        mem.attempts.pop(f"expand:{t}", None)
        mem.cooldown.pop(f"expand:{t}", None)
        for g in goals or ():
            if any(EXPAND.get(name) == t for name in g.get("templates") or [g.get("template")]):
                mem.blocked.pop(g["id"], None)
    return freed


def forecast_margin(trend: float, pollution: float = 0.0) -> int:
    """Chunks outside the pollution cloud at which a nest is already defended (defence v2): FORECAST_CHUNKS, one more
    while the cloud grows (trend: its total's change per game minute), one more while it grows by more than
    FORECAST_FAST of its total a minute."""
    m = FORECAST_CHUNKS
    if trend > 0:
        m += FORECAST_GROWING
        if pollution > 0 and trend / pollution > FORECAST_FAST:
            m += 1
    return m


def rank_threats(threats, trend: float = 0.0, pollution: float = 0.0, max_groups: int | None = None) -> list:
    """Nests to put a turret group in front of (defense.lua defense_threats rows: x, y, pollution, cloud = chunks to
    the nearest polluted chunk, base = the nearest building, dist): in the cloud, or within forecast_margin chunks of
    it (the cloud reaches them as the base grows), with a building of the base near them. Nearest to the base first,
    each chunk the cloud still has to spread counting FORECAST_TILES_PER_CHUNK tiles farther; one group per 24-tile
    spot, at most max_groups. -> [{"nest": [x, y], "base": {x, y}, "cloud": chunks, "score"}]"""
    margin = forecast_margin(trend, pollution)
    cands = []
    for t in as_list(threats):
        t = as_dict(t)
        base = as_dict(t.get("base"))
        if base.get("x") is None or base.get("y") is None or t.get("x") is None:
            continue
        cloud = 0 if float(t.get("pollution") or 0) > 0 else t.get("cloud")
        if cloud is None or int(cloud) > margin:
            continue
        dist = t.get("dist")
        if dist is None:
            dist = ((float(t["x"]) - float(base["x"])) ** 2 + (float(t["y"]) - float(base["y"])) ** 2) ** 0.5
        cands.append({"nest": [float(t["x"]), float(t["y"])], "base": {"x": float(base["x"]), "y": float(base["y"])},
                      "cloud": int(cloud), "score": float(dist) + FORECAST_TILES_PER_CHUNK * int(cloud)})
    cands.sort(key=lambda c: c["score"])
    out = []
    for c in cands:
        if max_groups is not None and len(out) >= max_groups:
            break
        if any(abs(o["base"]["x"] - c["base"]["x"]) < 24 and abs(o["base"]["y"] - c["base"]["y"]) < 24 for o in out):
            continue
        out.append(c)
    return out


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
            **({"robots": {k: int(snap.robots.get(k) or 0) for k in ("roboports", "construction", "logistic")}}
               if snap.robots else {}),
            "goals": [{"id": g["id"], "done": g["done"], "ready": g["ready"], "shipped": g["available"]}
                      for g in (goals or [])]}
