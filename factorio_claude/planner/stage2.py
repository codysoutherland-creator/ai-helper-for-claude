"""Stage 2 of a planner v2 variant (PLANNER-v2.md section 10, round 3): power and research, PLACED and validated on
top of stage 1 (not an estimate any more).

    s2 = plan_stage2(pl, spec, lay, s1_eta, s1_per_min, removes, left)     # Plan | None (pl.reasons says why)

Contents, in the order the helper builds them:
  1. copper burner pairs on the copper patch when stage 1 has none (Quick start smelts iron only);
  2. a small hand-fed steam plant (offshore pump, 1 boiler, 1-2 steam engines; no fuel belt: the helper's coal round
     keeps the boiler filled) on the shore nearest the labs;
  3. labs: the player's idle labs are powered WHERE THEY STAND when a pole line can reach them (they cost 36 iron +
     15 copper each to replace; poles cost 0.5 copper + 0.5 wood); otherwise 2 new labs next to the iron pairs;
  4. small poles from the plant to the labs (router.place_poles).
Red science is hand-crafted (the player, or the helper on request) and put into the labs; nothing in stage 2 needs
research that stage 1's output does not unlock by itself (2.x trigger techs: steam power = 50 iron plates, electronics
= 10 copper plates, red science = craft a lab).

The ETA is an estimate built on stage 1's planned (and simulated) rates: materials come from what stage 1 leaves in
stock, then from stage 1's output (iron and copper plates collected from its chests), the rest is hand-gathered by the
helper. It reports when the power comes on, when red science research can start and when Automation and Electric
mining drill are researched (the stage-3 prerequisites). The player asks for stage 2 once stage 1 runs; it is then
re-validated against the map as it is (plan_apply / Approve).
"""
from __future__ import annotations

import math

from . import blocks as B
from . import cost
from .onsite import dist_point_patch, place_free, place_rows
from .placer import _amounts, _occupy, place_power
from .plan import Plan, entity_counts, world_without
from .router import NoRoute, place_poles
from .validate import validate

NEW_LABS = 2                 # labs built when the player has none that a pole line can reach
LAB_REUSE_MAX = 220          # existing idle labs farther than this from any shore are not powered in place
COPPER_PAIRS = 2             # stage 2 copper pairs when stage 1 has none
SCIENCE_TECHS = ("automation", "electric-mining-drill")   # stage 3 needs both (assemblers, electric drills)
UNIT_TIME_S = 10             # research unit time when the data has none (2.x: 10 s for red-science techs)
PACK_CRAFT_S = 5.5           # one red pack by hand: 1 gear (0.5 s) + the pack (5 s)
# 2.x trigger techs stage 2 waits for. Furnace output counts (stage 1's pairs unlock steam power and electronics by
# themselves); a lab crafted by the helper does NOT count for red science (verified in 2.1: only a player's crafting
# triggers it), so the player crafts one lab by hand (10 gears, 10 circuits, 4 belts)
TRIGGERS = {"steam-power": "smelting 50 iron plates (stage 1 does it)",
            "electronics": "smelting 10 copper plates (the copper pairs do it)",
            "automation-science-pack": "you crafting one lab by hand (the helper's crafting does not count)"}
WOOD_PER_MIN = 60            # the helper chopping trees (0.8 s per wood plus walking between trees)
ENGINE_KW = 900
BOILER_KW = 1800
BOILER_COAL_FULL = 27.0      # coal/min of a boiler at full load (1.8 MW / 4 MJ x 60)
SHORE_KEY_LABS = 1.0         # shore choice: distance to the labs ...
SHORE_KEY_IRON = 0.25        # ... plus a little toward the iron site (stage 3's science cell goes there)


def _gps(p) -> str:
    return f"[gps={round(p[0])},{round(p[1])}]"


def _lab_ents(pl, removed_ids):
    """The player's labs that stage 2 can power where they stand: idle (no power) and not being removed."""
    out = []
    for c in pl.clusters:
        if c.id in removed_ids:
            continue
        for e in c.ents:
            if e.type == "lab":
                out.append(e)
    return out


def research_left(gd, techs=SCIENCE_TECHS) -> list:
    """[(tech, units, unit_s)] of the stage-3 prerequisite techs not researched yet."""
    out = []
    for t in techs:
        tech = gd.raw.get("technologies", {}).get(t) or {}
        if tech and not tech.get("researched"):
            out.append((t, int(tech.get("unit_count") or 10), float(tech.get("unit_time") or UNIT_TIME_S)))
    return out


def triggers_left(gd) -> list:
    """Trigger techs stage 2's entities wait for, with what unlocks them."""
    out = []
    for t, how in TRIGGERS.items():
        tech = gd.raw.get("technologies", {}).get(t) or {}
        if tech and not tech.get("researched"):
            out.append({"tech": t, "unlocked_by": how})
    return out


def plan_stage2(pl, spec, lay, s1_eta: dict, s1_per_min: dict, removes=(), left=None, removed_ids=()):
    """Place stage 2 for variant `spec` on top of stage-1 layout `lay` -> Plan | None."""
    gd = pl.gd
    for n in ("offshore-pump", "boiler", "steam-engine", "small-electric-pole", "lab"):
        if n not in gd.raw.get("entities", {}):
            pl.reasons.append(f"stage 2: no {n} in the game data")
            return None
    world = world_without(pl.world, removes) if removes else pl.world
    k = tuple(lay.sites.get("iron-ore") or pl.iron_target)
    labs_old = _lab_ents(pl, set(removed_ids))
    labs_working = [e for e in labs_old if e.status == "working"]
    idle_labs = [e for e in labs_old if e.status != "working"]
    reuse = []
    if idle_labs:
        cx = sum(e.x for e in idle_labs) / len(idle_labs)
        cy = sum(e.y for e in idle_labs) / len(idle_labs)
        nw = world.nearest_water_tile(cx, cy)
        if nw is not None and math.hypot(nw[0][0] - cx, nw[0][1] - cy) <= LAB_REUSE_MAX:
            reuse = idle_labs
    # the grid: stage 1's rect, the labs, the shore near them, the copper patch
    pts = [k, (pl.rect[0], pl.rect[1]), (pl.rect[2], pl.rect[3])]
    pts += [(e.x, e.y) for e in reuse]
    centre0 = ((sum(e.x for e in reuse) / len(reuse), sum(e.y for e in reuse) / len(reuse)) if reuse else k)
    nw0 = world.nearest_water_tile(*centre0)
    if nw0 is not None:
        pts.append(tuple(nw0[0]))
    x0, y0, x1, y1 = world.bbox
    m = 30
    rect = (max(x0, int(min(p[0] for p in pts)) - m), max(y0, int(min(p[1] for p in pts)) - m),
            min(x1, int(max(p[0] for p in pts)) + m), min(y1, int(max(p[1] for p in pts)) + m))
    grid = world.grid(rect)
    s1_ents = list(lay.ents)
    _occupy(grid, gd, s1_ents, "s1")
    amount = _amounts(world, grid)
    ents2, sites, order = [], [], []

    def box(es):
        xs = [e.x for e in es]
        ys = [e.y for e in es]
        return [math.floor(min(xs)) - 2, math.floor(min(ys)) - 2, math.ceil(max(xs)) + 2, math.ceil(max(ys)) + 2]

    # 1. copper pairs (Quick start has none)
    cu_pairs = 0
    if not lay.units.get("copper-ore") and pl.copper is not None and "copper-ore" not in pl.skipped:
        n = max(1, round(COPPER_PAIRS * pl.scale))
        tgt = pl._away_from_nests(pl.copper, dist_point_patch(k, pl.copper)[0])
        if pl.nest_dist(tgt) >= 40:
            chest = pl._chest()
            rows = place_rows(grid, gd, lambda q: B.burner_pair_row(q, gd, ore="copper-ore", chest=chest), pl.copper,
                              n, tgt, amount=amount, tag="v2s2:copper")
            if rows:
                es = [e for r in rows for e in r.placed()]
                cu_pairs = sum(1 for e in es if e.name == "burner-mining-drill")
                ents2 += es
                sites.append({"what": "copper pairs", "n": cu_pairs, "at": _centre(es)})
                order.append(box(es))
    # 2. labs: the player's idle ones where they stand, else new ones next to the iron pairs
    new_labs = []
    if reuse:
        consumers = [B.PE(e.name, e.x, e.y, e.dir or 0) for e in reuse]
    elif labs_working:
        consumers = []
    else:
        lb = place_free(grid, gd, B.lab_row(NEW_LABS, gd), k, radius=45, margin=1)
        if lb is None:
            pl.reasons.append("stage 2: no room for labs next to the iron pairs")
            return None
        new_labs = lb.placed()
        _occupy(grid, gd, new_labs, "v2s2:labs")
        consumers = list(new_labs)
    if not consumers:   # labs already powered: stage 2 is only the copper pairs (if any)
        if not ents2:
            return None
        plant = None
        poles = []
        power_kw = 0.0
    else:
        centre = (sum(e.x for e in consumers) / len(consumers), sum(e.y for e in consumers) / len(consumers))
        lab_kw = gd.entity("lab").get("energy_usage_kw") or 60
        power_kw = lab_kw * len(consumers)
        engines = 1 if power_kw * 1.25 <= ENGINE_KW else 2
        pole = "small-electric-pole"

        def key(x, y):
            return SHORE_KEY_LABS * math.hypot(x - centre[0], y - centre[1]) + \
                SHORE_KEY_IRON * math.hypot(x - k[0], y - k[1])
        got = place_power(world, gd, grid, centre, 1, engines, pole, feed="manual", max_tries=60, key=key)
        if got is None:
            pl.reasons.append("stage 2: no room for a steam engine on the shore near the labs")
            return None
        sb, pump, pipe = got
        plant_ents = sb.placed() + [pump] + list(pipe)
        src = [e for e in plant_ents if gd.entity(e.name).get("type") == "electric-pole"]
        try:
            poles = place_poles(grid, gd, consumers, pole, sources=src, tag="v2s2:pole")
        except NoRoute as e:
            pl.reasons.append(f"stage 2: pole line: {e}")
            return None
        plant = (pump.x, pump.y)
        ents2 += plant_ents
        sites.append({"what": "steam power", "n": 1, "at": [round(pump.x, 1), round(pump.y, 1)]})
        order.append(box(plant_ents))
        ents2 += poles
        if poles:
            order.append(box(poles))
        if new_labs:
            ents2 += new_labs
            sites.append({"what": "labs", "n": len(new_labs), "at": _centre(new_labs)})
            order.append(box(new_labs))
    # entities a trigger tech still locks (steam power: 50 iron plates ...) are unlocked by stage 1's own output
    # before stage 2 is built: checked as if researched, listed as research steps
    trig = triggers_left(gd)
    expect = {"assume_research": True} if trig else {}
    rep = validate(s1_ents + ents2, gd, world, expect)
    if not rep.ok:
        pl.reasons.append("stage 2 validation: " + "; ".join(f"{i.kind}: {i.msg}" for i in rep.errors[:3]))
        return None
    # ---- bill + ETA (estimate on stage 1's rates) ----------------------------------------------------------------
    need = cost.bill(ents2, gd, fuel=False)
    boilers = [e for e in ents2 if e.name == "boiler"]
    start_coal = 5 * len(boilers) + 13 * cu_pairs
    stock = dict(left or {})
    cov = cost.cover(need, stock, gd)
    eta = _eta(pl, lay, s1_eta, s1_per_min, ents2, cu_pairs, consumers, plant, k, stock)
    research = research_left(gd)
    burn = (power_kw / BOILER_KW) * BOILER_COAL_FULL if boilers else 0.0
    lab_at = _centre(consumers) if consumers else None
    d_water = None
    if consumers:
        nwc = world.nearest_water_tile(*lab_at)
        d_water = round(math.hypot(nwc[0][0] - lab_at[0], nwc[0][1] - lab_at[1])) if nwc else None
    stats = {
        "class": "stage2", "stage_label": "stage 2 (power + research)", "stage_index": 2,
        "per_min": {"copper-plate": 15.0 * cu_pairs} if cu_pairs else {}, "counts": entity_counts(ents2),
        "entities": len(ents2), "belts": 0, "belt_tiles": 0, "longest_run": 0, "routes": [],
        "power_kw": round(power_kw, 1), "power_sizing": {"boilers": len(boilers), "load_kw": power_kw},
        "water": {"plant": round(math.hypot(plant[0] - lab_at[0], plant[1] - lab_at[1])) if plant else None,
                  "near": d_water, "pipe": sum(1 for e in ents2 if e.name == "pipe")},
        "boiler_coal_per_min": round(burn, 1),
        "bill": {"need": {k2: round(v) for k2, v in need.items()}, "need_raw": _round(cost.raw_of(need, gd)),
                 "have": {k2: round(v) for k2, v in cov.used.items()}, "short": _round(cov.short),
                 "craft_s": round(cov.craft_s), "start_coal": start_coal},
        "eta": eta, "sites": sites, "build_order": order,
        "labs": {"reused": len(reuse), "new": len(new_labs), "already_powered": len(labs_working),
                 "at": [round(lab_at[0], 1), round(lab_at[1], 1)] if lab_at else None},
        "plant": [round(plant[0], 1), round(plant[1], 1)] if plant else None,
        "poles": sum(1 for e in ents2 if e.name == "small-electric-pole"),
        "copper_pairs": cu_pairs, "research": [r[0] for r in research],
        "research_packs": sum(r[1] for r in research), "research_steps": trig, "assume_research": bool(trig),
        "needs_research": sorted({e.name for e in ents2 if not gd.can_build(e.name)}),
        "validation_s1_s2": rep.ok,
    }
    p = Plan("s2", "stage 2", spec.variant, "power", "", ents2, [], [], rep.to_dict(), stats,
             pl.world.surface or "nauvis")
    # the trees/rocks in its way (agent clears them before placing)
    tiles = set()
    for e in ents2:
        tiles |= gd.footprint(e.name, e.x, e.y, e.direction)
    p.deconstruct = list(grid.to_clear(tiles))
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    stats["bbox"] = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
    return p


def _centre(es):
    return [round(sum(e.x for e in es) / len(es), 1), round(sum(e.y for e in es) / len(es), 1)]


def _round(d):
    return {k: round(v) if v >= 1 else round(v, 2) for k, v in d.items() if v > 1e-6}


def _eta(pl, lay, s1_eta, s1_per_min, ents2, cu_pairs, consumers, plant, k, stock) -> dict:
    """Minutes from now (closed form, see the module doc). None when stage 1 never completes."""
    gd = pl.gd
    t = s1_eta.get("complete")
    out = {"start": t, "copper_plates": None, "power_on": None, "science_start": None,
           "assumes": "estimate from stage 1's planned rates; the helper gathers what they don't cover; the player "
                      "or the helper hand-crafts the red packs"}
    if t is None:
        return out
    sus = s1_eta.get("sustained") or 1.0
    rate = {"iron-ore": (s1_per_min.get("iron-plate") or 0) * sus,
            "copper-ore": (s1_per_min.get("copper-plate") or 0) * sus,
            "stone": s1_per_min.get("stone") or 0,
            "coal": max(0.0, s1_per_min.get("coal") or 0), "wood": WOOD_PER_MIN}
    if rate["copper-ore"]:
        out["copper_plates"] = s1_eta.get("first_plates")
    # the helper keeps stage 1's burners fed meanwhile: only the rest of its time goes into stage 2
    avail = max(0.5, 1.0 - float(s1_eta.get("helper_share") or 0.0))
    H = pl.H * avail
    stock = dict(stock)
    steps = []
    cu = [e for e in ents2 if (e.extra or {}).get("fuel") == "manual" and e.name != "boiler"] + \
        [e for e in ents2 if "chest" in e.name]
    if cu_pairs:
        steps.append(("copper", cu))
    rest = [e for e in ents2 if e not in cu]
    if rest:
        steps.append(("power", rest))
    pos = lay.sites.get("iron-ore") or k
    for name, es in steps:
        need = cost.bill(es, gd, fuel=False)
        c = cost.cover(need, stock, gd)
        wait = 0.0
        for raw, v in c.short.items():
            r = rate.get(raw, 0.0)
            if r <= 0.05:            # nothing makes it: the helper mines (and smelts) it by hand
                r = H
            wait = max(wait, v / r)
        site = _centre(es)
        walk = 2 * cost.walk_s(pos, site) / 60          # collect plates at the pairs, go and build, come back
        t += wait + (c.craft_s / 60 + cost.PLACE_S * len(es) / 60 + walk) / avail
        pos = site
        stock = {kk: vv for kk, vv in c.left.items() if vv > 0}
        if name == "copper":
            rate["copper-ore"] += 15.0 * cu_pairs
            out["copper_plates"] = round(t + 0.5, 1)
        else:
            out["power_on"] = round(t + 0.2, 1)
    if out["power_on"] is not None and consumers:
        labs = max(1, len(consumers))
        lab_pos = _centre(consumers)
        t_r = out["power_on"] + (PACK_CRAFT_S + cost.walk_s(pos, lab_pos)) / 60
        out["science_start"] = round(t_r, 1)
        for tech, units, unit_s in research_left(gd):
            dur = max(units * unit_s / labs, units * PACK_CRAFT_S) / 60
            t_r += dur
            out[tech] = round(t_r, 1)
    return out
