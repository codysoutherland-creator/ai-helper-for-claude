"""Quality gates for planner v2 (PLANNER-v2.md section 11). The validator proves a plan is CORRECT (connected,
fuelled, powered); these gates judge whether it is SENSIBLE for the player's actual state.

    issues = assess(plan, state)       # [Issue(code, level, text)]; level "hard" = not offered as build-now
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

SLOW_START_MIN = 15          # first plates later than this: loud warning
FIRST_STAGE_MAX_MIN = 45     # ... later than this: not a first stage
# belt caps per stage: (soft single run, hard single run, soft total)
BELT_CAPS = {"bootstrap": (30, 60, 60), "electric": (60, 100, 150), "longterm": (100, 160, 10 ** 9)}
ORE_RUN_SOFT = {"electric": 20}  # electric: ore belts belong next to the patch
SMELT_FAR = {"bootstrap": 40, "electric": 40, "longterm": 80}
WATER_NEAR = 60
PIPE_MAX = 30
MAX_REMOVE_SHARE = 0.5
FUEL_SUSTAIN = 0.9           # measured (simulated) output after full rate / plan rate: below = the round can't keep up
FUEL_SUSTAIN_HARD = 0.5
HELPER_BUSY = 0.6            # the coal round takes more of the helper's time than this: warning


@dataclass
class Issue:
    code: str
    level: str      # soft | hard
    text: str

    def to_dict(self):
        return asdict(self)


def _plate_rate(state) -> float:
    if state is None:
        return 0.0
    return float((state.production or {}).get("iron-plate", 0.0))


def assess(plan, state=None, *, base_entities=None, base_belts=None, rebuild_words=False) -> list[Issue]:
    """Gates on a v2 Plan (plan.stats as written by staged.py; v1 plans get the belt/distance gates)."""
    s = plan.stats
    out = []
    cls = s.get("class") or ("bootstrap" if plan.stage == "bootstrap" else "electric")
    eta = s.get("eta") or {}
    first = eta.get("first_plates")
    if s.get("eta") is not None and cls != "stage2":    # first-stage gates (a stage 2 is built after stage 1 runs)
        if first is None or first > FIRST_STAGE_MAX_MIN:
            out.append(Issue("FIRST_STAGE_TOO_BIG", "hard", "first plates take more than "
                             f"{FIRST_STAGE_MAX_MIN} min from what you have: not a good first step"))
        elif first > SLOW_START_MIN:
            out.append(Issue("SLOW_START", "soft", f"first plates in about {round(first)} min"))
        sus = eta.get("sustained")
        if sus is not None and sus < FUEL_SUSTAIN_HARD:
            out.append(Issue("FUEL_SHORT", "hard", f"the helper's coal round keeps only {round(100 * sus)}% of the "
                             "burners running"))
        elif sus is not None and sus < FUEL_SUSTAIN:
            out.append(Issue("FUEL_SHORT", "soft", f"the helper's coal round keeps about {round(100 * sus)}% of "
                             "the burners running"))
        elif first is not None and eta.get("target") is None and s.get("target"):
            out.append(Issue("FUEL_SHORT", "soft", "it does not reach its full rate within 2 hours"))
    share = (s.get("round") or {}).get("helper_share")
    if share is not None and share > HELPER_BUSY:
        out.append(Issue("HELPER_BUSY", "soft", f"the helper spends about {round(100 * share)}% of its time carrying "
                         "coal between the sites"))
    for txt in s.get("skipped") or []:
        out.append(Issue("NESTS_NEAR", "soft", txt))
    for n in s.get("nest_sites") or []:
        out.append(Issue("NESTS_NEAR", "soft", f"biter nests {n['nest']} tiles from the {n['what']} "
                         f"[gps={round(n['at'][0])},{round(n['at'][1])}]: pollution draws attacks, clear them or "
                         "defend"))
        break
    short = (s.get("bill") or {}).get("short") or {}
    fe_short = short.get("iron-ore", 0)
    if fe_short > 10 * _plate_rate(state) + 60 and cls != "stage2":   # stage 2 is paid from stage 1's output
        out.append(Issue("BILL_UNREACHABLE", "soft", f"needs about {round(fe_short)} more iron than you have "
                         f"(you make {round(_plate_rate(state), 1)} plates/min)"))
    soft, hard, total = BELT_CAPS.get(cls, BELT_CAPS["electric"])
    longest = s.get("longest_run") or 0
    belts = s.get("belt_tiles", s.get("belts", 0)) or 0
    if longest > hard:
        out.append(Issue("LONG_BELT", "hard", f"a belt run of {longest} tiles (cap {hard} for this stage)"))
    elif longest > soft:
        out.append(Issue("LONG_BELT", "soft", f"a belt run of {longest} tiles"))
    for r in s.get("routes") or []:
        if r.get("kind") == "ore" and cls in ORE_RUN_SOFT and r.get("tiles", 0) > ORE_RUN_SOFT[cls] and longest <= soft:
            out.append(Issue("LONG_BELT", "soft", f"{r.get('what', 'ore')} belt of {r['tiles']} tiles: furnaces "
                             "belong next to the patch"))
            break
    if belts > total:
        out.append(Issue("TOO_MANY_BELTS", "soft", f"{belts} belt tiles"))
    d = s.get("smelt_dist")
    if d is not None and d > SMELT_FAR.get(cls, 40):
        out.append(Issue("SMELTING_FAR_FROM_IRON", "hard", f"iron smelting is {round(d)} tiles from the iron ore"))
    w = s.get("water") or {}
    if w.get("plant") is not None and w.get("near") is not None:
        if w["near"] <= WATER_NEAR and w["plant"] > 1.5 * max(w["near"], 10):
            out.append(Issue("WATER_IGNORED", "soft", f"water {round(w['near'])} tiles away but the steam plant "
                             f"is {round(w['plant'])} tiles away"))
    if (w.get("pipe") or 0) > PIPE_MAX:
        out.append(Issue("WATER_IGNORED", "soft", f"a water pipe of {w['pipe']} tiles"))
    p = s.get("power_sizing") or {}
    if p.get("boilers") and p.get("load_kw") is not None:
        if p["boilers"] > math.ceil(p["load_kw"] * 1.25 / 1800) + 1:
            out.append(Issue("OVERSIZED_POWER", "soft", f"{p['boilers']} boilers for {round(p['load_kw'])} kW"))
    if s.get("needs_research") and not s.get("research_steps") and cls != "longterm":
        out.append(Issue("RESEARCH_MISSING", "hard", "needs research first: " +
                         ", ".join(n.replace('-', ' ') for n in s["needs_research"][:4])))
    if s.get("clutter_push"):
        out.append(Issue("CLUTTER_PUSH", "soft", s["clutter_push"]))
    for c in s.get("idle_fixable_ignored") or []:
        out.append(Issue("IDLE_FIXABLE_IGNORED", "soft", f"{c} could be repaired first"))
    for o in s.get("overlap_existing") or []:
        out.append(Issue("OVERLAP_EXISTING", "hard", o))
    removes = getattr(plan, "removes", None) or []
    if removes:
        cl = {c["id"]: c for c in s.get("clusters") or [] if c.get("action") in ("replace", "reuse")}
        for r in removes:
            c = cl.get(r[4] if len(r) > 4 else None)
            if c is None or not c.get("reason"):
                out.append(Issue("REMOVAL_UNJUSTIFIED", "hard", f"{r[0]} at ({r[1]}, {r[2]}) has no stated reason"))
                break
        if not rebuild_words and base_entities:
            n_ent = sum(1 for r in removes if "belt" not in r[0])
            n_belt = sum(1 for r in removes if "belt" in r[0])
            if n_ent > MAX_REMOVE_SHARE * base_entities or (base_belts and n_belt > MAX_REMOVE_SHARE * base_belts):
                out.append(Issue("REMOVES_TOO_MUCH", "hard", "removes more than half of your base"))
    return out


def hard(issues) -> bool:
    return any(i.level == "hard" for i in issues)


def counts(issues) -> tuple:
    return sum(1 for i in issues if i.level == "soft"), sum(1 for i in issues if i.level == "hard")
