"""Plan: a verified, approvable build (SPEC-v3 M1) + its plain-English summary.

    plan = placer.plan_starter_base(world, gd, goal, "compact")
    plan.save(plans_dir); Plan.load(path)
    link.call("plan_preview", **plan.preview_args())

Entities are PE (blocks.PE) in map coordinates. `deconstruct` = trees/rocks [(x, y, name)] the build needs removed.
`validation` = validate.Report.to_dict() of exactly these entities (a Plan is only ever returned when it passed).
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .blocks import PE

# items the helper/player gets from mining + smelting (material lists stop here)
BASE_ITEMS = ("iron-plate", "copper-plate", "steel-plate", "stone", "stone-brick", "wood", "coal", "iron-ore",
              "copper-ore")
HAND_MINING_S = 2.0      # character mining speed 0.5, ore mining time 1
WALK_TILES_S = 8.9       # character running speed (tiles/s)
PLACE_S = 0.4            # per entity: reach, place, small walks between neighbours
FURNACE_S = 3.2          # stone furnace: one plate per 3.2 s


@dataclass
class Plan:
    id: str
    title: str
    variant: str
    stage: str                       # "bootstrap" | "electric"
    summary: str                     # plain sentences (no markdown), shown in game
    entities: list                   # [PE]
    deconstruct: list = field(default_factory=list)   # [(x, y, name)]
    warnings: list = field(default_factory=list)      # plain strings
    validation: dict = field(default_factory=dict)    # validate.Report.to_dict()
    stats: dict = field(default_factory=dict)         # numbers behind the summary (rates, counts, materials ...)
    surface: str = "nauvis"
    created: float = field(default_factory=time.time)
    removes: list = field(default_factory=list)       # existing entities to dismantle: [name, x, y, unit, cluster]

    # ---- (de)serialisation ---------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["entities"] = [asdict(e) for e in self.entities]
        d["deconstruct"] = [list(t) for t in self.deconstruct]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        d = dict(d)
        d["entities"] = [PE(**e) for e in d.get("entities") or []]
        d["deconstruct"] = [tuple(t) for t in d.get("deconstruct") or []]
        d["removes"] = [list(r) for r in d.get("removes") or []]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, directory) -> Path:
        p = Path(directory)
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"{self.id}.json"
        f.write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8")
        return f

    @classmethod
    def load(cls, path) -> "Plan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- game-facing forms ---------------------------------------------------------------------------------
    def rows(self) -> list:
        """plan_preview entity rows [name, x, y, dir, recipe?, extra?]: extra = the underground half ("input" /
        "output"), or {type?, fuel: "manual"} for burners fuelled by hand (the helper's plan refuel finds them)."""
        out = []
        for e in self.entities:
            row = [e.name, e.x, e.y, int(e.direction or 0)]
            ex = e.extra if isinstance(e.extra, dict) else {}
            ug, manual = ex.get("type"), ex.get("fuel") == "manual"
            extra = dict({"fuel": "manual"}, **({"type": ug} if ug else {})) if manual else ug
            if e.recipe or extra:
                row.append(e.recipe)
            if extra:
                row.append(extra)
            out.append(row)
        return out

    def preview_args(self, group=None, show=True, color=None) -> dict:
        """plan_preview op arguments (plans.lua): summary as one line per sentence, validation as counts."""
        v = self.validation or {}
        args = {"id": self.id, "title": self.title, "entities": self.rows(),
                "deconstruct": [[x, y, n] for x, y, n in self.deconstruct],
                "summary": summary_lines(self.summary),
                "surface": self.surface, "variant": self.variant, "stage": self.stage, "warnings": self.warnings,
                "validation": {"ok": bool(v.get("ok", True)), "errors": len(v.get("errors") or []),
                               "warnings": len(v.get("warnings") or [])}, "show": show}
        if self.removes:
            args["removes"] = [[r[0], r[1], r[2], r[3] if len(r) > 3 else None] for r in self.removes]
        s = self.stats or {}
        if s.get("takes"):      # S0: coal out of listed idle machines [name, x, y, unit, item, count]
            args["takes"] = [list(r[:6]) for r in s["takes"]]
        if s.get("coal_from"):  # the plan's coal chests: its logistics round takes coal there first
            args["coal_from"] = [list(p) for p in s["coal_from"]]
        if s.get("refuel_existing"):  # S0 repairs: existing burners the helper refuels [name, x, y, unit, n]
            args["refuel_existing"] = [list(r) for r in s["refuel_existing"]]
        if s.get("build_order"):      # site boxes [x0, y0, x1, y1] the helper builds one after the other
            args["build_order"] = [list(b) for b in s["build_order"]]
        if s.get("keep_fuelled"):     # stage 2: the previous stage's hand-fed burners stay in the coal round
            args["keep_fuelled"] = [[r[1], r[2], r[3]] for r in s["keep_fuelled"]]
        if s.get("reserve"):          # ground of the next stage: the helper builds no smelter of its own there
            args["reserve"] = [[float(v) for v in b] for b in s["reserve"]]
        if s.get("take_from"):        # stage 2: chests whose plates the helper uses first (stage 1's output)
            args["take_from"] = [list(p) for p in s["take_from"]]
        facts = self.facts()
        if facts:
            args["facts"] = facts
        if group:
            args["group"] = group
        if color:
            args["color"] = color
        return args

    def expect(self) -> dict:
        """validate() expectations this plan was checked with (declared sinks, research assumption)."""
        from .blocks import Port
        ex = {"ports": [Port(*p) for p in self.stats.get("ports") or []]}
        if self.stats.get("assume_research"):
            ex["assume_research"] = True
        return ex

    def revalidate(self, gd, world):
        """validate() against a fresh map export; the plan's own ghosts / already built copies and the existing
        entities it removes don't count."""
        from .validate import validate
        return validate(self.entities, gd, world_without_plan(world, self), self.expect())

    def ghosts(self) -> list:
        """place_ghosts form (fallback when the plan-UI ops are missing)."""
        out = []
        for e in self.entities:
            g = {"name": e.name, "x": e.x, "y": e.y, "direction": int(e.direction or 0)}
            if e.recipe:
                g["recipe"] = e.recipe
            if isinstance(e.extra, dict) and e.extra.get("type"):
                g["type"] = e.extra["type"]
            out.append(g)
        return out

    def facts(self) -> dict:
        """Short numbers for the Plans tab facts line (planner v2 plans)."""
        s = self.stats
        if "eta" not in s:
            return {}
        eta = s.get("eta") or {}
        return {"stage": s.get("stage_label") or self.stage, "first_plates_min": eta.get("first_plates"),
                "belt_tiles": s.get("belt_tiles", 0), "removes": len(self.removes),
                "build_now": bool(s.get("build_now", True)), "score": s.get("score")}

    def brief(self) -> dict:
        """Compact form for Claude (no entity dump)."""
        s = self.stats
        out = {"id": self.id, "title": self.title, "variant": self.variant, "stage": self.stage,
               "summary": self.summary, "entities": len(self.entities), "clear": len(self.deconstruct),
               "per_min": s.get("per_min"), "footprint": s.get("footprint"), "belts": s.get("belts"),
               "power_kw": s.get("power_kw"), "build_min": s.get("build_min"),
               "needs_research": s.get("needs_research") or [], "warnings": self.warnings[:4]}
        if "eta" in s:  # planner v2 (PLANNER-v2.md 12.2)
            for k in ("topology", "recommended", "eta", "bill", "footprint", "net_gain", "score", "gates", "tradeoff",
                      "build_now"):
                if k in s:
                    out[k] = s[k]
            keep = ("name", "now", "placed", "id", "bill_raw", "eta", "research", "unlocks", "after",  # what: on the card
                    "ready_min", "after_stage1_min")
            out["stages"] = [{k: v for k, v in st.items() if k in keep and v not in (None, [], {})}
                             for st in s.get("stages") or []]
            out["belts"] = {"total": s.get("belt_tiles", 0), "longest": s.get("longest_run", 0),
                            "over_cap": [g["text"] for g in s.get("gates") or [] if g["code"] == "LONG_BELT"]}
            rm = s.get("removes_summary")
            out["removes"] = rm or {"count_by_name": {}, "clusters": []}
            takes = s.get("takes") or []
            out["s0"] = {  # helper-only first steps on the player's existing builds (nothing taken apart)
                "coal_from_idle_machines": {"machines": len({(r[1], r[2]) for r in takes}),
                                            "coal": sum(r[5] for r in takes if r[4] == "coal")},
                "repairs": [{k: r.get(k) for k in ("label", "gps")} for r in s.get("repairs") or []],
                "idle_builds": [{k: r.get(k) for k in ("label", "gps", "action")} for r in
                                (s.get("idle_review") or [])[:3]]}
            rd = s.get("round") or {}
            out["logistics"] = {"build_order": s.get("build_order_names"), "site_spread_tiles": rd.get("tiles"),
                                "helper_share": rd.get("helper_share")}
            if s.get("reviewed"):  # old builds checked for replacement (best gains first)
                out["review"] = {"checked": s["reviewed"], "best": [
                    {k: r.get(k) for k in ("label", "gps", "status", "gain_min", "needed_min", "verdict")}
                    for r in (s.get("review") or [])[:2]]}
            out.pop("build_min", None)
        return out


def summary_lines(text: str) -> list:
    """Plans tab lines: planner v2 cards are already one line per row (newlines); v1 summaries: one per sentence."""
    if "\n" in (text or ""):
        return [ln.strip() for ln in text.split("\n") if ln.strip()]
    return sentences(text)


def sentences(text: str) -> list:
    """Split plain text into sentences (one GUI line each); [gps=..] tags and decimals stay intact."""
    return [t for t in re.split(r"(?<=[.!?])\s+(?=[A-Z])", (text or "").strip()) if t]


def world_without(world, removes):
    """Copy of a World without the existing entities listed in `removes` ([name, x, y, ...] rows): the world a
    plan that dismantles them is validated against."""
    gone = {(r[0], float(r[1]), float(r[2])) for r in removes or []}
    if not gone:
        return world
    keep, remap = [], {}
    for i, e in enumerate(world.entities):
        if not e.name.startswith("ghost:") and (e.name, float(e.x), float(e.y)) in gone:
            continue
        remap[i] = len(keep)
        keep.append(e)
    w = copy.copy(world)
    w.entities = keep
    w._chunk_ent = {c: [remap[i] for i in idx if i in remap] for c, idx in world._chunk_ent.items()}
    return w


def world_without_plan(world, plan):
    """Copy of a World without the force entities that belong to `plan`: ghosts tagged with its id and entities
    standing exactly where the plan has the same entity (already built)."""
    mine = {(e.name, float(e.x), float(e.y)) for e in plan.entities}
    gone = {(r[0], float(r[1]), float(r[2])) for r in getattr(plan, "removes", None) or []}
    keep, remap = [], {}
    for i, e in enumerate(world.entities):
        extra = e.extra if isinstance(e.extra, dict) else {}
        tags = extra.get("tags") if isinstance(extra.get("tags"), dict) else {}
        name = e.name[6:] if e.name.startswith("ghost:") else e.name
        if tags.get("claude_plan") == plan.id or (name, float(e.x), float(e.y)) in mine:
            continue
        if not e.name.startswith("ghost:") and (e.name, float(e.x), float(e.y)) in gone:
            continue
        remap[i] = len(keep)
        keep.append(e)
    w = copy.copy(world)
    w.entities = keep
    w._chunk_ent = {c: [remap[i] for i in idx if i in remap] for c, idx in world._chunk_ent.items()}
    return w


# ---- materials + build time ----------------------------------------------------------------------------------
def entity_counts(entities) -> dict:
    out = {}
    for e in entities:
        out[e.name] = out.get(e.name, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _recipe_for(gd, item):
    r = gd.raw["recipes"].get(item)
    if r and not r.get("hidden") and r.get("ingredients"):
        return r
    return None


def raw_materials(gd, counts: dict) -> tuple[dict, float, dict]:
    """Expand entity items into base materials. -> (base items {name: n}, hand-craft seconds, crafted {item: n})."""
    need, base, crafted, secs = dict(counts), {}, {}, 0.0
    for _ in range(12):  # recipe depth
        nxt = {}
        for item, n in need.items():
            r = None if item in BASE_ITEMS else _recipe_for(gd, item)
            if r is None:
                base[item] = base.get(item, 0) + n
                continue
            prod = next((p.get("amount", 1) for p in r.get("products") or [] if p.get("name") == item), 1) or 1
            crafts = math.ceil(n / prod)
            crafted[item] = crafted.get(item, 0) + crafts * prod
            secs += crafts * float(r.get("energy") or 0.5)
            for ing in r["ingredients"]:
                nxt[ing["name"]] = nxt.get(ing["name"], 0) + crafts * ing.get("amount", 1)
        if not nxt:
            break
        need = nxt
    return dict(sorted(base.items(), key=lambda kv: -kv[1])), secs, crafted


def build_minutes(base: dict, craft_s: float, n_entities: int, belt_tiles: int) -> dict:
    """Rough helper build time: {'scratch': min if it mines + smelts everything, 'stocked': min with materials
    in a chest}. Smelting runs in one furnace per plate type in parallel with mining."""
    plates = {k: base.get(k, 0) for k in ("iron-plate", "copper-plate")}
    ore = sum(plates.values()) + base.get("stone", 0) + 2 * base.get("stone-brick", 0) + base.get("coal", 0)
    ore += sum(plates.values()) * 0.072  # coal burnt per plate in a stone furnace
    smelt = max(list(plates.values()) + [base.get("stone-brick", 0)]) * FURNACE_S
    place = n_entities * PLACE_S + 2 * belt_tiles / WALK_TILES_S
    scratch = max(ore * HAND_MINING_S, smelt) + craft_s + place
    stocked = craft_s + place
    return {"scratch": round(scratch / 60), "stocked": max(1, round(stocked / 60))}


def fmt_n(v) -> str:
    return f"{v:,.0f}" if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")


def nice(name: str) -> str:
    return name.replace("-", " ")
