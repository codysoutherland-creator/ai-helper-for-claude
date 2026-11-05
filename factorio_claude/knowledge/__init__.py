"""Factorio 2.x knowledge base for the autopilot, the in-game chat Claude and the skill (AUTOPILOT.md section 6).

    from factorio_claude import knowledge as K
    K.rules(applies="chat")                  # doctrine rules with ids (AUTO-1, ORG-3, ...)
    K.prompt_text(budget=400, applies="chat")  # compact text for a system prompt (budget in tokens, ~chars/4)
    K.check(gd)                               # [] when every game fact matches this game's data, else the mismatches
    K.research_order(gd)                      # the next techs worth queueing, in doctrine order
    K.sizing(30)                              # machines and plates for 30 red + 30 green per minute

Data lives in doctrine.json (rules, game facts, conventions) and roadmap.json (goals G0-G9). Game facts are verified
against the game's own export (testenv/fixtures/*/data.json, or a live export) by check(); conventions are rules of
thumb and are presented as such. doctrine.md is generated from the JSON (tools/gen_doctrine_md.py).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = ("AUTO", "ACQ", "MALL", "RES", "ORG", "SCALE", "PWR", "SAFE")
CHAT_GROUPS = ("AUTO", "ACQ", "ORG", "SCALE")      # what the chat prompt gets (section 6.4)
# the chat prompt's 400 tokens hold one or two rules per group, most important first
CHAT_RULES = ("AUTO-1", "AUTO-4", "ACQ-2", "ORG-1", "ORG-2", "ORG-6", "SCALE-2", "SCALE-4", "ORG-3", "AUTO-3")
AUTOPILOT_BUDGET = 2000          # round 3: +oil, feeds and mall rules (was 1500)
CHAT_BUDGET = 400
ACQUISITION_ORDER = ("inventory", "mall", "producer", "chest", "player", "wait", "short_craft", "handcraft",
                     "smelt_mine")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads((HERE / "doctrine.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def roadmap() -> list:
    return json.loads((HERE / "roadmap.json").read_text(encoding="utf-8"))["goals"]


def goal(gid: str) -> dict | None:
    return next((g for g in roadmap() if g["id"] == gid), None)


def facts() -> dict:
    return load()["facts"]


def conventions() -> dict:
    return load()["conventions"]


def rules(applies: str | None = None, groups=None) -> list:
    out = []
    for r in load()["rules"]:
        if applies and applies not in r.get("applies", ()):
            continue
        if groups and r["group"] not in groups:
            continue
        out.append(r)
    return out


def rule(rid: str) -> dict | None:
    return next((r for r in load()["rules"] if r["id"] == rid), None)


def handcraft() -> dict:
    return conventions()["handcraft"]


def mall_items(tier: int = 1, researched=()) -> list:
    """Mall items for tier 1 (plus the direct-insertion ones whose tech is researched) or tier 2."""
    c = conventions()
    items = list(c["mall_v1"])
    done = set(researched or ())
    items += [i for i, t in c["mall_v1_after"].items() if t in done]
    if tier >= 2:
        items += c["mall_v2"]
    return items


def lane_plan() -> list:
    return [list(x) for x in conventions()["bus_lane_plan"]]


def footprints() -> dict:
    return conventions()["footprints"]


def tokens(text: str) -> int:
    """Rough token count (characters / 4), the unit of the prompt budgets."""
    return math.ceil(len(text) / 4)


# ---- the helper's acquisition order (a Lua-free model of agent.lua's material planner, AUTOPILOT.md 7.1) ---------
def craft_allowed(item: str, job_s: float, mall: bool, recent_s: float = 0.0, bootstrap: bool = False,
                  mall_item: bool = False) -> bool:
    """Is a top-level hand-craft job of `job_s` crafting seconds fine? (agent.lua P.craft_ok). Once a mall exists
    every job counts against the 10-minute budget, short ones too; an item a mall machine makes (mall_item) only
    gets a short top-up, and is no bootstrap item any more."""
    hc = handcraft()
    if bootstrap or (item in hc["bootstrap_items"] and not mall_item):
        return True
    if not mall:
        return True                      # bootstrap: no mall yet
    if recent_s + job_s > hc["loop_alarm_s_per_10min"]:
        return False
    if mall_item:
        return job_s <= hc["short_craft_s"]
    return job_s <= hc["max_job_s"]


def acquire_model(item: str, n: int, have: dict | None = None, sources=(), player: int = 0, mall_maker=False,
                  craft_s_per_item: float = 0.5, mall: bool = False, recent_s: float = 0.0, bootstrap=False,
                  raw: bool = False, mall_item: bool | None = None) -> list:
    """Where `n` of `item` come from, in order: [(source, count)]. sources: [(kind, count)] with kind in mall | plan |
    producer | chest (sorted mall > plan > producer > chest like the helper does); player: what the player's inventory
    holds (only when allowed); mall_maker: a mall machine makes it and is active (crafting, or finished a craft in the
    last 3 game minutes): wait there instead of crafting; mall_item: a mall machine is set to make it (default: same
    as mall_maker); raw: the item can be mined/smelted. A craft over the budget is refused: ("short", rest)."""
    mall_item = bool(mall_maker) if mall_item is None else mall_item
    rank = {"mall": 1, "plan": 2, "producer": 3, "chest": 4}
    out = []
    k = min(n, (have or {}).get(item, 0))
    if k:
        out.append(("inventory", k))
        n -= k
    for kind, cnt in sorted(sources, key=lambda s: rank[s[0]]):
        if n <= 0:
            break
        k = min(n, cnt)
        if k:
            out.append((kind, k))
            n -= k
    if n > 0 and player:
        k = min(n, player)
        out.append(("player", k))
        n -= k
    if n > 0 and mall_maker and not bootstrap:
        out.append(("wait", n))
        n = 0
    if n > 0 and craft_s_per_item > 0:
        job = n * craft_s_per_item
        if craft_allowed(item, job, mall, recent_s, bootstrap, mall_item):
            out.append(("short_craft" if job <= handcraft()["short_craft_s"] else "handcraft", n))
            n = 0
        else:
            # the helper halves the batch until a job fits the budget
            m = n
            while m > 0 and not craft_allowed(item, m * craft_s_per_item, mall, recent_s, bootstrap, mall_item):
                m //= 2
            if m > 0:
                out.append(("handcraft", m))
            out.append(("short", n - m))
            n = 0
    if n > 0 and raw:
        out.append(("smelt_mine", n))
        n = 0
    if n > 0:
        out.append(("short", n))
    return out


# ---- research ------------------------------------------------------------------------------------------------------
def _tech(gd, name) -> dict:
    return ((gd.raw if gd is not None else {}).get("technologies") or {}).get(name) or {}


def _list(v) -> list:
    return list(v) if isinstance(v, list) else (list(v.keys()) if isinstance(v, dict) else [])


def tech_packs(gd, name) -> list:
    """Science packs a tech's units cost (empty for a trigger tech)."""
    t = _tech(gd, name)
    return [i[0] if isinstance(i, list) else i.get("name") for i in (t.get("ingredients") or [])]


def is_combat(name: str) -> bool:
    """A weapons / defence / armour tech (doctrine conventions.research_skip_peaceful, by name prefix)."""
    return any(name == p or name.startswith(p + "-") for p in conventions()["research_skip_peaceful"])


def research_order(gd, researched=None, packs_made=None, needed=(), limit=3, peaceful=True, low_value=False) -> list:
    """Techs to queue next, best first: available (prerequisites done, not a trigger tech), not done, and only with
    science packs the base makes (packs_made: pack items, default red only, plus green once logistic-science-pack is
    researched - a tech that needs a pack nobody makes blocks the labs). Ranked: techs an open goal needs
    (`needed`), then the doctrine order, then every other useful tech cheapest first; low-value ones (lamps, radar,
    repair packs) only with low_value=True; weapons and armour only when enemies are around (peaceful=False)."""
    techs = (gd.raw.get("technologies") or {}) if gd is not None else {}
    done = set(researched) if researched is not None else {n for n, t in techs.items() if t.get("researched")}
    if packs_made is None:
        packs_made = {"automation-science-pack"}
        if "logistic-science-pack" in done:
            packs_made.add("logistic-science-pack")
    low = set(conventions()["research_low_value"])

    def ok(name):
        t = techs.get(name)
        if not t or name in done or not t.get("enabled", True) or t.get("research_trigger"):
            return False
        if any(p not in done for p in _list(t.get("prerequisites"))):
            return False
        if peaceful and is_combat(name):
            return False
        packs = [i[0] if isinstance(i, list) else i.get("name") for i in (t.get("ingredients") or [])]
        return bool(packs) and all(p in packs_made for p in packs)

    def cost(n):
        t = techs[n]
        return int(t.get("unit_count") or 0) * (t.get("unit_energy") or 0)

    out = []
    for name in list(needed) + conventions()["research_order"]:
        if name not in out and ok(name):
            out.append(name)
    if len(out) < limit:   # every other useful tech the packs allow keeps the labs busy (cheapest first)
        rest = [n for n in techs if n not in out and ok(n)]
        out += sorted((n for n in rest if n not in low), key=lambda n: (cost(n), n))
        if low_value:
            out += sorted((n for n in rest if n in low), key=lambda n: (cost(n), n))
    return out[:limit]


def research_blocked(gd, queue, packs_made) -> list:
    """Queued techs whose science packs the base does not make (they would block the labs)."""
    return [t for t in queue if t and any(p not in packs_made for p in tech_packs(gd, t))]


# ---- scale ---------------------------------------------------------------------------------------------------------
def sizing(spm: float) -> dict:
    """Red + green at `spm` packs/min each with assembling machine 1 and stone furnaces (exact machine counts) from
    the doctrine's machine rates, plus the headroom the doctrine reserves."""
    f = facts()
    am1 = f["machine_out_per_min"]["assembling-machine-1"]
    sf = f["machine_out_per_min"]["stone-furnace"]
    red, green = spm / am1["automation-science-pack"], spm / am1["logistic-science-pack"]
    gears = spm * (1 + 1 + 0.5)                     # red 1, inserter 1, belt 1/2 per green pack
    circuits = spm * 1.0                            # one per inserter
    cable = circuits * 3
    iron = spm * 2 + spm * (1 + 2 + 1) + spm * 1.5  # red: gear 2; inserter: plate + gear 2 + circuit 1; belt 1.5
    copper = spm * 1 + cable / 2
    out = {"spm": spm,
           "machines": {"automation-science-pack": round(red, 2), "logistic-science-pack": round(green, 2),
                        "iron-gear-wheel": round(gears / am1["iron-gear-wheel"], 2),
                        "electronic-circuit": round(circuits / am1["electronic-circuit"], 2),
                        "copper-cable": round(cable / am1["copper-cable"], 2),
                        "inserter": round(spm / am1["inserter"], 2),
                        "transport-belt": round(spm / am1["transport-belt"], 2)},
           "iron-plate_per_min": round(iron, 1), "copper-plate_per_min": round(copper, 1),
           "furnaces": {"iron": round(iron / sf["iron-plate"], 1), "copper": round(copper / sf["copper-plate"], 1)},
           "drills": {"iron": round(iron / f["mining_out_per_min"]["electric-mining-drill"], 1),
                      "copper": round(copper / f["mining_out_per_min"]["electric-mining-drill"], 1)}}
    h = conventions()["headroom"]
    out["build_for_spm"] = spm * h["build"]
    out["reserve_for_spm"] = spm * h["reserve_max"]
    return out


def labs_for(spm: float, unit_s: float) -> float:
    return spm * unit_s / 60.0


# ---- prompts -------------------------------------------------------------------------------------------------------
FOCUS_TABLES = {
    "G0": ("steam",), "G1": ("machine_out_per_min",), "G2": ("mall",), "G3": ("sizing", "column_cap_per_yellow_lane"),
    "G4": ("steam",), "G5": ("machine_out_per_min",), "G6": ("sizing",), "G7": ("research",), "G8": ("sizing",),
    "G9": ("research", "oil"), "G10": ("research", "oil"),
}


def _table_text(name: str) -> str:
    f, c = facts(), conventions()
    if name == "steam":
        s = f["steam"]
        return (f"Steam: 1 offshore pump : {s['boilers_per_offshore_pump']} boilers : "
                f"{s['boilers_per_offshore_pump'] * s['engines_per_boiler']} engines; engine {s['engine_kw']} kW; "
                f"boiler {s['boiler_coal_per_min']} coal/min.")
    if name == "machine_out_per_min":
        am1 = f["machine_out_per_min"]["assembling-machine-1"]
        return "Assembler 1 output/min: " + ", ".join(f"{k} {v:g}" for k, v in am1.items()) + \
               "; stone furnace 18.75 plates/min; electric drill 30 ore/min, burner drill 15."
    if name == "column_cap_per_yellow_lane":
        return "Per yellow lane (450/min): " + ", ".join(f"{v} {k}" for k, v in f["column_cap_per_yellow_lane"].items())
    if name == "sizing":
        s = sizing(30)
        m = s["machines"]
        return (f"30 red+green/min (AM1): {m['automation-science-pack']:g} red + {m['logistic-science-pack']:g} green "
                f"AM1, {s['iron-plate_per_min']:g} iron + {s['copper-plate_per_min']:g} copper/min, "
                f"{s['furnaces']['iron']:g} + {s['furnaces']['copper']:g} stone furnaces; build for 2x, reserve 4x.")
    if name == "mall":
        return "Mall v1: " + ", ".join(c["mall_v1"]) + f"; chests limited to {c['mall_chest_limit_stacks']} stacks."
    if name == "research":
        return "Research order: " + ", ".join(c["research_order"][:12]) + "."
    if name == "oil":
        o = f["oil"]
        return (f"Oil: pumpjack {o['pumpjack_crude_per_s_at_100pct']} crude/s at 100% yield; basic oil processing "
                f"100 crude -> 45 petroleum per 5 s ({o['refinery_petroleum_per_s_basic']}/s per refinery); plastic "
                f"20 petroleum + 1 coal -> 2 per 1 s in a chemical plant ({o['plastic_plants_per_basic_refinery']} "
                f"plants per basic refinery); sulfur 30 water + 30 petroleum -> 2 per 1 s.")
    return ""


def prompt_text(budget: int = AUTOPILOT_BUDGET, focus: str | None = None, applies: str = "autopilot",
                groups=None) -> str:
    """Doctrine for a system prompt within `budget` tokens: the rules (chat: AUTO/ACQ/ORG/SCALE only), then the tables
    for the focus goal. Conventions are marked as rules of thumb."""
    if groups is None and applies == "chat":
        groups = CHAT_GROUPS
    lines = ["Factorio doctrine (rule ids in brackets; (c) = rule of thumb, not a game fact):"]
    chosen = rules(applies, groups)
    if applies == "chat":
        rank = {rid: i for i, rid in enumerate(CHAT_RULES)}
        chosen = sorted((r for r in chosen if r["id"] in rank), key=lambda r: rank[r["id"]])
    else:   # safety first: it must never be cut by the budget
        chosen = [r for r in chosen if r["group"] == "SAFE"] + [r for r in chosen if r["group"] != "SAFE"]
    for r in chosen:
        lines.append(f"[{r['id']}]{' (c)' if r.get('kind') == 'convention' else ''} {r['text']}")
    tables = list(FOCUS_TABLES.get(focus or "", ())) if applies != "chat" else []
    if applies != "chat":
        for t in ("machine_out_per_min", "steam", "sizing", "research"):
            if t not in tables:
                tables.append(t)
    out, used = [], 0
    for line in lines + [_table_text(t) for t in tables]:
        if not line:
            continue
        n = tokens(line + "\n")
        if used + n > budget:
            continue      # a later, shorter line may still fit
        out.append(line)
        used += n
    return "\n".join(out)


# ---- verification against the game's data ----------------------------------------------------------------------------
def _ents(gd) -> dict:
    return gd.raw.get("entities") or {}


def _recipe(gd, name) -> dict:
    return (gd.raw.get("recipes") or {}).get(name) or {}


def _product(rec: dict, item: str) -> float:
    for p in rec.get("products") or []:
        if p.get("name") == item:
            return float(p.get("amount") or p.get("amount_max") or 1)
    return 0.0


def _close(a, b, tol=0.01) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


def check(gd) -> list:
    """Every game fact in doctrine.json recomputed from this game's data. -> list of mismatch strings (empty = ok)."""
    f, issues = facts(), []
    ents = _ents(gd)

    def bad(msg):
        issues.append(msg)

    for name, v in f["belt_items_per_s"].items():
        e = ents.get(name)
        if not e:
            bad(f"belt {name}: not in the game data")
        elif not _close(e.get("belt_items_per_s") or (e.get("belt_speed", 0) * 480), v):
            bad(f"belt {name}: {v}/s in the doctrine, {e.get('belt_items_per_s')} in the game")
    for name, v in f["underground_max_distance"].items():
        e = ents.get(name)
        if not e or int(e.get("max_underground_distance") or -1) != v:
            bad(f"underground {name}: {v} in the doctrine, {e and e.get('max_underground_distance')} in the game")
    for name, v in f["crafting_speed"].items():
        e = ents.get(name)
        if not e or not _close(e.get("crafting_speed") or 0, v):
            bad(f"crafting speed {name}: {v} in the doctrine, {e and e.get('crafting_speed')} in the game")
    res = gd.raw.get("resources") or {}
    mt = float((res.get("iron-ore") or {}).get("mining_time") or 1)
    for name, v in f["mining_out_per_min"].items():
        e = ents.get(name)
        got = (e or {}).get("mining_speed", 0) * 60 / mt
        if not e or not _close(got, v):
            bad(f"drill {name}: {v}/min in the doctrine, {got} in the game")
    for machine, table in f["machine_out_per_min"].items():
        speed = (ents.get(machine) or {}).get("crafting_speed")
        if not speed:
            bad(f"machine {machine}: not in the game data")
            continue
        for item, v in table.items():
            rec = _recipe(gd, item)
            if not rec:
                bad(f"recipe {item}: not in the game data")
                continue
            got = 60.0 / float(rec["energy"]) * float(speed) * _product(rec, item)
            if not _close(got, v):
                bad(f"{machine} {item}: {v}/min in the doctrine, {round(got, 3)} in the game")
    lane = f["belt_items_per_s"]["transport-belt"] / 2 * 60
    for name, v in f["column_cap_per_yellow_lane"].items():
        if name == "electric-mining-drill":
            got = lane / f["mining_out_per_min"][name]
        else:
            got = lane / f["machine_out_per_min"][name]["iron-plate"]
        if not _close(got, v):
            bad(f"lane cap {name}: {v} in the doctrine, {got} computed")
    s = f["steam"]
    boiler, engine, pump = ents.get("boiler") or {}, ents.get("steam-engine") or {}, ents.get("offshore-pump") or {}
    coal = ((gd.raw.get("items") or {}).get("coal") or {}).get("fuel_value") or 0
    if not _close(boiler.get("max_energy_usage_kw") or 0, s["boiler_kw"]):
        bad(f"boiler kW: {s['boiler_kw']} in the doctrine, {boiler.get('max_energy_usage_kw')} in the game")
    if not _close(engine.get("max_energy_production_kw") or 0, s["engine_kw"]):
        bad(f"engine kW: {s['engine_kw']} in the doctrine, {engine.get('max_energy_production_kw')} in the game")
    if engine and boiler and not _close(boiler["max_energy_usage_kw"] / engine["max_energy_production_kw"],
                                        s["engines_per_boiler"]):
        bad("engines per boiler do not match the game")
    # water 15 -> 165 C at 0.2 kJ/unit/C = 30 kJ per unit of steam
    if pump and boiler and not _close(pump.get("pumping_speed_per_s", 0) / (boiler["max_energy_usage_kw"] / 30.0),
                                      s["boilers_per_offshore_pump"]):
        bad("boilers per offshore pump do not match the game")
    if coal and not _close(s["boiler_kw"] * 1000 / coal * 60, s["boiler_coal_per_min"]):
        bad(f"boiler coal/min: {s['boiler_coal_per_min']} in the doctrine, {s['boiler_kw'] * 1000 / coal * 60}")
    oil = f.get("oil") or {}
    for name, v in (oil.get("crafting_speed") or {}).items():
        e = ents.get(name)
        if not e or not _close(e.get("crafting_speed") or 0, v):
            bad(f"crafting speed {name}: {v} in the doctrine, {e and e.get('crafting_speed')} in the game")
    for name, r in (oil.get("recipes") or {}).items():
        rec = _recipe(gd, name)
        if not rec:
            bad(f"recipe {name}: not in the game data")
            continue
        if not _close(rec.get("energy") or 0, r["s"]):
            bad(f"recipe {name}: {r['s']} s in the doctrine, {rec.get('energy')} in the game")
        have_in = {i["name"]: i["amount"] for i in rec.get("ingredients") or []}
        have_out = {p["name"]: p.get("amount") for p in rec.get("products") or []}
        if have_in != r["in"] or have_out != r["out"]:
            bad(f"recipe {name}: {r['in']} -> {r['out']} in the doctrine, {have_in} -> {have_out} in the game")
    pj, crude = ents.get("pumpjack") or {}, res.get("crude-oil") or {}
    if pj and crude:
        amt = sum(float(p.get("amount") or p.get("amount_min") or 0) for p in crude.get("results") or []) or 10.0
        got = float(pj.get("mining_speed") or 0) * amt / float(crude.get("mining_time") or 1)
        if not _close(got, oil.get("pumpjack_crude_per_s_at_100pct", 0)):
            bad(f"pumpjack: {oil.get('pumpjack_crude_per_s_at_100pct')}/s at 100% in the doctrine, {got} in the game")
    bo = (oil.get("recipes") or {}).get("basic-oil-processing")
    if bo and not _close(bo["out"]["petroleum-gas"] / bo["s"] * oil["crafting_speed"]["oil-refinery"],
                         oil.get("refinery_petroleum_per_s_basic", 0)):
        bad("refinery petroleum/s (basic oil processing) does not follow from the recipe")
    pb = (oil.get("recipes") or {}).get("plastic-bar")
    if bo and pb and not _close(oil["refinery_petroleum_per_s_basic"] /
                                (pb["in"]["petroleum-gas"] / pb["s"] * oil["crafting_speed"]["chemical-plant"]),
                                oil.get("plastic_plants_per_basic_refinery", 0)):
        bad("plastic plants per basic refinery does not follow from the recipes")
    techs = gd.raw.get("technologies") or {}
    for name, t in f["techs"].items():
        g = techs.get(name)
        if not g:
            bad(f"tech {name}: not in the game data")
            continue
        if int(g.get("unit_count") or 0) != t["count"]:
            bad(f"tech {name}: {t['count']} units in the doctrine, {g.get('unit_count')} in the game")
        if not _close((g.get("unit_energy") or 0) / 60.0, t["unit_s"]):
            bad(f"tech {name}: {t['unit_s']} s per unit in the doctrine, {(g.get('unit_energy') or 0) / 60} in the game")
        packs = sorted(i[0] if isinstance(i, list) else i.get("name") for i in (g.get("ingredients") or []))
        if packs != sorted(t["packs"]):
            bad(f"tech {name}: packs {t['packs']} in the doctrine, {packs} in the game")
        unl = {e.get("recipe") for e in (g.get("effects") or []) if e.get("type") == "unlock-recipe"}
        for r in t.get("unlocks", []):
            if r not in unl:
                bad(f"tech {name}: does not unlock {r} in the game")
        for p in t.get("prereq", []):
            if p not in _list(g.get("prerequisites")):
                bad(f"tech {name}: {p} is not a prerequisite in the game")
    for name, t in f["trigger_techs"].items():
        g = techs.get(name)
        trig = (g or {}).get("research_trigger") or {}
        if not g or trig.get("type") != t["trigger"]:
            bad(f"trigger tech {name}: {t['trigger']} in the doctrine, {trig.get('type')} in the game")
    for item, tech in f["unlocked_by"].items():
        g = techs.get(tech) or {}
        unl = {e.get("recipe") for e in (g.get("effects") or []) if e.get("type") == "unlock-recipe"}
        if item not in unl:
            bad(f"{item}: not unlocked by {tech} in the game")
    for item, ings in f["recipe_inputs"].items():
        rec = _recipe(gd, item)
        have = {i["name"]: i["amount"] for i in rec.get("ingredients") or []}
        if have != ings:
            bad(f"recipe {item}: {ings} in the doctrine, {have} in the game")
    for name in conventions()["research_order"]:
        if name not in techs:
            bad(f"research_order: {name} is not a tech in the game")
    for item in mall_items(2, researched=list(conventions()["mall_v1_after"].values())):
        if not _recipe(gd, item):
            bad(f"mall item {item}: no recipe in the game")
    return issues
