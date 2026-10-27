"""Rate solver: a linear program over recipes, mining, pumps and steam power.

    from factorio_claude.planner.rates import solve
    r = solve({"automation-science-pack": 60}, gd, {"machines": {"crafting": "assembling-machine-1"}})
    r.recipes["iron-gear-wheel"]["count"]  # -> 1.0

Targets are items/min (fluids units/min); the pseudo-item "electricity" is a target in kW.
Quality variants are separate items "name@quality" (normal = plain name).

opts (all optional):
  machines     {recipe name | recipe category | resource category | "generator" | "boiler" | "offshore-pump": entity}
  modules      {recipe name | category | machine name | "*": [module item or "module@quality", ...]}
  beacons      {same keys: {"beacon": name, "count": beacons per machine, "modules": [...]}}
  quality      list of extra quality levels to model (levels in targets are always modelled)
  machine_quality  quality of crafting machines (speed multiplier), default "normal"
  surface      surface name: drop recipes/machines whose surface conditions fail there and limit
               default raw_items to that surface's resources (recommended: pass it)
  allowed_recipes  only these recipes (default: enabled for the force, or all with assume_research)
  assume_research  use every non-hidden recipe / machine regardless of research
  allow_recycling  include "recycling" category recipes (quality upcycling)
  raw_items    unbounded inputs (default: water + products of every resource, or of the resources
               that generate on opts.surface when given)
  mine         model drills/pumps for raw items: True (default), False, or a list of raw items
  fuel         burner fuel item (default "coal")
  power        "steam" to build the electricity supply into the LP (implied by an "electricity" target)
  mining_productivity  default: force bonus
  belts        belt entities to size flows against (default: every transport-belt type)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

from .gamedata import GameData

ELECTRICITY = "electricity"
EFFECTS = ("speed", "productivity", "consumption", "pollution", "quality")
BENEFICIAL = {"speed": 1, "productivity": 1, "quality": 1, "consumption": -1, "pollution": -1}
QMULT = {"speed": "module_speed_multiplier", "productivity": "module_productivity_multiplier",
         "quality": "module_quality_multiplier", "consumption": "module_consumption_multiplier",
         "pollution": "module_pollution_multiplier"}
RAW_COST = 1.0          # objective weight per raw item/min
FLUID_RAW_COST = 0.01   # per raw fluid unit/min (water, crude)
MACHINE_COST = 0.01     # per machine
MISSING_COST = 1e5      # per unit/min of an item nothing can make (reported in notes)
STUCK_COST = 1e8        # per unit/min of a producible item the LP still can't balance


@dataclass
class RateResult:
    recipes: dict = field(default_factory=dict)   # key -> {machine, count, count_ceil, per_min, quality, ...}
    raw: dict = field(default_factory=dict)       # item -> per_min taken from the world (mined/pumped/raw)
    power_kw: float = 0.0                         # electric consumption of the production chain
    fuel_per_min: float = 0.0                     # burner fuel items/min (opts.fuel)
    belts: dict = field(default_factory=dict)     # item -> {belt: belts needed}
    notes: list = field(default_factory=list)
    machines: dict = field(default_factory=dict)  # entity -> machines needed (ceil per recipe, summed)
    surplus: dict = field(default_factory=dict)   # byproducts not consumed, per_min
    missing: dict = field(default_factory=dict)   # items no allowed recipe/raw source could provide
    pollution_per_min: float = 0.0
    status: str = "ok"


def qname(item: str, quality: str = "normal") -> str:
    return item if quality == "normal" else f"{item}@{quality}"


def split_q(name: str) -> tuple[str, str]:
    item, _, q = name.partition("@")
    return item, (q or "normal")


@dataclass
class _Proc:
    key: str
    kind: str                 # recipe | mine | pump | boil | generate | raw
    machine: str | None
    quality: str
    seconds: float            # machine-seconds per operation (count = ops/min * seconds / 60)
    coeffs: dict              # item -> amount per operation (+ produced, - consumed)
    power_kw: float = 0.0     # per machine (electric)
    fuel: float = 0.0         # fuel items per operation
    pollution: float = 0.0    # per machine per minute
    effects: dict | None = None
    cost: float = 0.0
    recipe: str | None = None


def _expected(p: dict, productivity: float) -> float:
    if p.get("amount") is not None:
        base = float(p["amount"])
    else:
        base = (float(p.get("amount_min") or 0) + float(p.get("amount_max") or 0)) / 2
    prob = float(p.get("independent_probability", p.get("probability", 1)) or 0)
    sp = p.get("shared_probability")
    if isinstance(sp, dict):
        prob *= float(sp.get("max", 1)) - float(sp.get("min", 0))
    amount = base * prob + float(p.get("extra_count_fraction") or 0)
    ignored = min(amount, float(p.get("ignored_by_productivity") or 0))
    return ignored + (amount - ignored) * (1 + productivity)


class _Solver:
    def __init__(self, targets: dict, gd: GameData, opts: dict):
        self.gd, self.opts, self.targets = gd, opts, dict(targets)
        self.notes: list[str] = []
        self.fuel = opts.get("fuel", "coal")
        self.assume = bool(opts.get("assume_research"))
        self.power_lp = opts.get("power") == "steam" or ELECTRICITY in self.targets
        mp = opts.get("mining_productivity")
        self.mining_prod = (gd.bonuses.get("mining_drill_productivity_bonus") or 0) if mp is None else mp
        levels = {split_q(t)[1] for t in self.targets} | set(opts.get("quality") or [])
        self.qset = [q for q in gd.qualities_unlocked() if q in levels or q == "normal"]
        unknown = levels - set(self.qset)
        if unknown:
            self.notes.append(f"quality not unlocked/unknown: {sorted(unknown)}")
        self.procs: list[_Proc] = []

    # ---- configuration lookups -----------------------------------------------------------------
    def _lookup(self, table: dict, keys) -> object:
        for k in keys:
            if k in table:
                return table[k]
        return table.get("*")

    def _surface_ok(self, r: dict) -> bool:
        """Recipe/entity surface conditions vs opts.surface (unchecked when no surface is given)."""
        sname = self.opts.get("surface")
        if not sname:
            return True
        props = (self.gd.raw["surfaces"].get(sname) or {}).get("properties") or {}
        for c in r.get("surface_conditions") or []:
            v = props.get(c.get("property"))
            if v is None:
                continue
            if v < c.get("min", -math.inf) or v > c.get("max", math.inf):
                return False
        return True

    def _usable(self, entity: str) -> bool:
        return self.assume or self.gd.can_build(entity)

    def _machine_for(self, rn: str, r: dict) -> str | None:
        mtab = self.opts.get("machines") or {}
        cats = r["categories"]
        chosen = mtab.get(rn) or next((mtab[c] for c in cats if c in mtab), None)
        if chosen:
            e = self.gd.raw["entities"].get(chosen)
            if not e or not set(cats) & set(e.get("crafting_categories") or []):
                self.notes.append(f"{chosen} can't craft {rn}")
                return None
            return chosen
        cands = {m for c in cats for m in self.gd.machines_for(c) if self._surface_ok(self.gd.entity(m))}
        if not cands:
            return None
        ent = self.gd.raw["entities"]
        usable = [m for m in cands if self._usable(m)]
        if usable:  # fastest machine the player can build
            return max(usable, key=lambda m: (ent[m].get("crafting_speed") or 0, m))
        return min(cands, key=lambda m: (ent[m].get("crafting_speed") or 0, m))

    def _module_effects(self, mods: list, slots: int, allowed: dict, label: str) -> dict:
        eff = dict.fromkeys(EFFECTS, 0.0)
        mods = list(mods or [])
        if len(mods) > slots:
            self.notes.append(f"{label}: {len(mods)} modules but {slots} slots; extra ignored")
            mods = mods[:slots]
        for m in mods:
            name, q = split_q(m)
            it = self.gd.raw["items"].get(name)
            if not it or not it.get("module_effects"):
                self.notes.append(f"{label}: {m} is not a module")
                continue
            me = it["module_effects"]
            bad = [k for k, v in me.items() if v and BENEFICIAL.get(k, 1) * v > 0 and not allowed.get(k, True)]
            if bad:
                self.notes.append(f"{label}: {m} not allowed ({', '.join(bad)})")
                continue
            qd = self.gd.quality(q)
            for k, v in me.items():
                if k in eff and v and allowed.get(k, True):
                    good = BENEFICIAL.get(k, 1) * v > 0
                    eff[k] += v * ((qd.get(QMULT[k]) or qd.get("default_multiplier") or 1) if good else 1)
        return eff

    def _beacon_effects(self, spec: dict | None, allowed: dict, label: str) -> tuple[dict, float]:
        """(effects, beacon count) for `count` beacons with the given modules reaching one machine."""
        eff = dict.fromkeys(EFFECTS, 0.0)
        if not spec or not spec.get("count"):
            return eff, 0.0
        bname, _, bq = spec.get("beacon", "beacon").partition("@")
        b = self.gd.entity(bname)
        n = int(spec["count"])
        per = self._module_effects(spec.get("modules"), b.get("module_slots") or 0, b.get("allowed_effects") or {},
                                   f"{label} beacon")
        de = (b.get("distribution_effectivity") or 1) + \
            (b.get("distribution_effectivity_bonus_per_quality_level") or 0) * self.gd.quality(bq or "normal")["level"]
        prof = b.get("profile") or [1]
        mult = n * de * prof[min(n, len(prof)) - 1]
        for k in EFFECTS:
            if allowed.get(k, True):
                eff[k] = per[k] * mult
        return eff, float(n)

    def _effects(self, machine: str, keys: list, recipe: dict | None, label: str) -> tuple[dict, float]:
        e = self.gd.entity(machine)
        allowed = dict(e.get("allowed_effects") or {})
        if recipe is not None:
            for k, v in (recipe.get("allowed_effects") or {}).items():
                allowed[k] = allowed.get(k, True) and v
        eff = dict.fromkeys(EFFECTS, 0.0)
        for k, v in ((e.get("effect_receiver") or {}).get("base_effect") or {}).items():
            if k in eff and v:
                eff[k] += v
        er = e.get("effect_receiver") or {}
        if er.get("uses_module_effects", True) and e.get("module_slots"):
            mods = self._lookup(self.opts.get("modules") or {}, keys)
            me = self._module_effects(mods, e["module_slots"], allowed, label)
            for k in EFFECTS:
                eff[k] += me[k]
        beacons = 0.0
        if er.get("uses_beacon_effects", True):
            spec = self._lookup(self.opts.get("beacons") or {}, keys)
            if spec:
                be, beacons = self._beacon_effects(spec, allowed, label)
                for k in EFFECTS:
                    eff[k] += be[k]
        return eff, beacons

    @staticmethod
    def _clamp(eff: dict, max_prod: float) -> dict:
        out = dict(eff)
        out["speed"] = max(-0.8, out["speed"])
        out["consumption"] = max(-0.8, out["consumption"])
        out["pollution"] = max(-0.8, out["pollution"])
        out["productivity"] = min(max(0.0, out["productivity"]), max_prod)
        out["quality"] = max(0.0, out["quality"])
        return out

    def _energy(self, machine: str, eff: dict, seconds: float) -> tuple[float, float, float]:
        """(electric kW per machine, fuel items per operation, pollution per machine-minute)."""
        e = self.gd.entity(machine)
        src = e.get("energy_source") or {}
        usage = e.get("energy_usage_kw") or e.get("max_energy_usage_kw") or 0.0
        usage *= 1 + eff.get("consumption", 0)
        epj = sum((src.get("emissions_per_joule") or {}).get(k, 0) for k in ("pollution",))
        poll = usage * 1000 * 60 * epj * (1 + eff.get("pollution", 0))
        if src.get("type") == "electric":
            return usage + (src.get("drain_kw") or 0), 0.0, poll
        if src.get("type") == "burner":
            fv = self.gd.fuel_value(self.fuel)
            if fv <= 0:
                self.notes.append(f"fuel {self.fuel} has no fuel value")
                return 0.0, 0.0, poll
            return 0.0, usage * 1000 * seconds / (fv * (src.get("effectivity") or 1)), poll
        return 0.0, 0.0, poll

    # ---- process construction ------------------------------------------------------------------
    def _add_recipes(self):
        gd = self.gd
        allowed = self.opts.get("allowed_recipes")
        mq = gd.quality(self.opts.get("machine_quality", "normal"))
        for rn, r in gd.raw["recipes"].items():
            if allowed is not None:
                if rn not in allowed:
                    continue
            else:
                if r.get("hidden") or not (self.assume or r.get("enabled")):
                    continue
                if "recycling" in r["categories"] and not self.opts.get("allow_recycling"):
                    continue
                if "parameters" in r["categories"]:
                    continue
                if not self._surface_ok(r):
                    continue
            machine = self._machine_for(rn, r)
            if not machine:
                continue
            if not self._usable(machine):
                self.notes.append(f"{machine} (for {rn}) is not buildable yet")
            m = gd.entity(machine)
            keys = [rn, *r["categories"], machine]
            eff, beacons = self._effects(machine, keys, r, rn)
            prod_bonus = (r.get("productivity_bonus") or 0)
            eff["productivity"] += prod_bonus
            eff = self._clamp(eff, r.get("maximum_productivity") or 3.0)
            speed = (m.get("crafting_speed") or 1) * (mq.get("crafting_machine_speed_multiplier") or 1)
            seconds = r["energy"] / (speed * (1 + eff["speed"]))
            for q in self.qset:
                if q != "normal" and r.get("can_set_quality") is False:
                    continue
                coeffs: dict[str, float] = {}
                for ing in r["ingredients"]:
                    n = ing["name"] if ing.get("type") == "fluid" else qname(ing["name"], q)
                    coeffs[n] = coeffs.get(n, 0) - float(ing["amount"])
                roll = gd.quality_roll(q, eff["quality"])
                for p in r["products"]:
                    amt = _expected(p, eff["productivity"])
                    if p.get("type") == "fluid" or p.get("affected_by_quality") is False:
                        dist = {q if p.get("type") != "fluid" else "normal": 1.0}
                    else:
                        dist = roll
                    for pq, frac in dist.items():
                        n = p["name"] if p.get("type") == "fluid" else qname(p["name"], pq)
                        coeffs[n] = coeffs.get(n, 0) + amt * frac
                self._finish(_Proc(qname(rn, q), "recipe", machine, q, seconds, coeffs, effects=eff, recipe=rn),
                             r.get("emissions_multiplier") or 1, beacons)

    def _finish(self, p: _Proc, emissions_mult: float = 1.0, beacons: float = 0.0):
        kw, fuel, poll = self._energy(p.machine, p.effects or {}, p.seconds)
        p.power_kw, p.fuel, p.pollution = kw, fuel, poll * emissions_mult
        if fuel:
            p.coeffs[self.fuel] = p.coeffs.get(self.fuel, 0) - fuel
        if self.power_lp and kw:
            p.coeffs[ELECTRICITY] = p.coeffs.get(ELECTRICITY, 0) - kw * p.seconds / 60
        p.cost += MACHINE_COST * p.seconds / 60
        if beacons:
            p.effects = dict(p.effects or {}, beacons=beacons)
        self.procs.append(p)

    def _raw_items(self) -> list[str]:
        if self.opts.get("raw_items") is not None:
            return list(self.opts["raw_items"])
        out = {"water"}
        sname = self.opts.get("surface")
        on_surface = (self.gd.raw["surfaces"].get(sname) or {}).get("resources") if sname else None
        for rn, r in self.gd.raw["resources"].items():
            if on_surface is not None and rn not in on_surface:
                continue
            for p in r.get("products") or []:
                out.add(p["name"])
        return sorted(out)

    def _add_sources(self):
        gd, mtab = self.gd, self.opts.get("machines") or {}
        for item in self._raw_items():
            fluid = gd.is_fluid(item)
            unit_cost = (FLUID_RAW_COST if fluid else RAW_COST) * (0.1 if item == "water" else 1)
            res = gd.resource_for_item(item)
            rd = gd.raw["resources"].get(res) if res else None
            mine = self.opts.get("mine", True)
            if isinstance(mine, (list, tuple, set)):
                mine = item in mine
            if mine and item == "water":
                pump = mtab.get("offshore-pump", "offshore-pump")
                pe = gd.raw["entities"].get(pump)
                if pe and pe.get("pumping_speed_per_s"):
                    rate = pe["pumping_speed_per_s"] * 60
                    self._finish(_Proc(f"pump:{item}", "pump", pump, "normal", 60.0, {item: rate},
                                       cost=unit_cost * rate, effects={}))
                    continue
            if mine and rd and rd.get("category") and not fluid:
                cat = rd["category"]
                drill = mtab.get(cat) or mtab.get(res)
                if not drill:
                    drills = [d for d in gd.drills_for(cat) if self._surface_ok(gd.entity(d))]
                    usable = [d for d in drills if self._usable(d)]
                    drill = (usable or drills or [None])[-1]
                if drill:
                    self._add_mine(item, res, rd, drill, unit_cost)
                    continue
            self.procs.append(_Proc(f"raw:{item}", "raw", None, "normal", 0.0, {item: 1.0}, cost=unit_cost))
        if self.power_lp:
            self._add_steam()

    def _add_mine(self, item: str, res: str, rd: dict, drill: str, unit_cost: float):
        gd = self.gd
        d = gd.entity(drill)
        eff, beacons = self._effects(drill, [res, rd["category"], drill], None, f"mining {res}")
        if d.get("uses_force_mining_productivity_bonus", True):
            eff["productivity"] += self.mining_prod
        eff = self._clamp(eff, 1e9)
        seconds = rd.get("mining_time", 1) / ((d.get("mining_speed") or 1) * (1 + eff["speed"]))
        coeffs: dict[str, float] = {}
        roll = gd.quality_roll("normal", eff["quality"])
        for p in rd.get("products") or []:
            amt = _expected(p, eff["productivity"])
            for pq, frac in (roll if p.get("type") != "fluid" else {"normal": 1}).items():
                n = qname(p["name"], pq)
                coeffs[n] = coeffs.get(n, 0) + amt * frac
        if rd.get("required_fluid"):  # fluid_amount is per 10 mining operations
            f = rd["required_fluid"]
            coeffs[f] = coeffs.get(f, 0) - float(rd.get("fluid_amount") or 0) / 10
        main = coeffs.get(item, 1)
        self._finish(_Proc(f"mine:{item}", "mine", drill, "normal", seconds, coeffs, effects=eff,
                           cost=unit_cost * main), 1.0, beacons)

    def _add_steam(self):
        gd, mtab = self.gd, self.opts.get("machines") or {}
        boiler, engine = mtab.get("boiler", "boiler"), mtab.get("generator", "steam-engine")
        b, g = gd.entity(boiler), gd.entity(engine)
        steam = gd.fluid("steam")
        water_t = gd.fluid("water").get("default_temperature") or 15
        bkw = b.get("max_energy_usage_kw") or 0
        per_min = bkw * 1000 / (((b.get("target_temperature") or 165) - water_t) * steam["heat_capacity"]) * 60
        self._finish(_Proc(f"boil:{boiler}", "boil", boiler, "normal", 60.0,
                           {"water": -per_min, "steam": per_min}, effects={}))
        self._finish(_Proc(f"generate:{engine}", "generate", engine, "normal", 60.0,
                           {"steam": -(g.get("fluid_usage_per_s") or 0) * 60,
                            ELECTRICITY: g.get("max_energy_production_kw") or 0}, effects={}))

    # ---- LP ------------------------------------------------------------------------------------
    def run(self) -> RateResult:
        self._add_recipes()
        self._add_sources()
        items = sorted({i for p in self.procs for i in p.coeffs} | set(self.targets))
        idx = {i: k for k, i in enumerate(items)}
        n_p, n_i = len(self.procs), len(items)
        # variables: process ops/min..., then a "missing" slack per item
        A = np.zeros((n_i, n_p + n_i))
        for j, p in enumerate(self.procs):
            for i, v in p.coeffs.items():
                A[idx[i], j] += v
        A[np.arange(n_i), n_p + np.arange(n_i)] = 1.0
        made = {i for p in self.procs for i, v in p.coeffs.items() if v > 0}
        c = np.array([p.cost for p in self.procs] + [STUCK_COST if i in made else MISSING_COST for i in items])
        b = np.array([float(self.targets.get(i, 0)) for i in items])
        eq = np.array([i in self.targets for i in items])
        res = linprog(c, A_ub=-A[~eq], b_ub=-b[~eq], A_eq=A[eq], b_eq=b[eq],
                      bounds=[(0, None)] * (n_p + n_i), method="highs")
        out = RateResult(notes=self.notes)
        if res.status != 0:
            out.status = f"lp failed: {res.message}"
            return out
        x = [float(v) for v in res.x]
        net = A[:, :n_p] @ np.array(x[:n_p])
        for k, i in enumerate(items):
            if x[n_p + k] > 1e-7:
                out.missing[i] = x[n_p + k]
            surplus = net[k] + x[n_p + k] - b[k]
            if surplus > 1e-6 and i != ELECTRICITY:
                out.surplus[i] = float(surplus)
        if out.missing:
            out.status = "missing inputs"
            out.notes.append("no allowed recipe or raw source for: " + ", ".join(sorted(out.missing)))
        self._report(out, x[:n_p])
        return out

    def _report(self, out: RateResult, x):
        fuel_total = 0.0
        flows: dict[str, float] = {}
        for p, ops in zip(self.procs, x):
            if ops <= 1e-9:
                continue
            for i, v in p.coeffs.items():
                if v > 0:
                    flows[i] = flows.get(i, 0) + v * ops
            if p.kind == "raw":
                for i, v in p.coeffs.items():
                    out.raw[i] = out.raw.get(i, 0) + v * ops
                continue
            count = ops * p.seconds / 60
            if p.kind in ("mine", "pump"):
                for i, v in p.coeffs.items():
                    if v > 0:
                        out.raw[i] = out.raw.get(i, 0) + v * ops
            ceil = math.ceil(count - 1e-6)
            entry = {"machine": p.machine, "count": count, "count_ceil": ceil, "per_min": ops,
                     "quality": p.quality, "kind": p.kind,
                     "inputs": {i: -v * ops for i, v in p.coeffs.items() if v < 0 and i != ELECTRICITY},
                     "outputs": {i: v * ops for i, v in p.coeffs.items() if v > 0}}
            if p.recipe:
                entry["recipe"] = p.recipe
            if p.effects:
                entry["effects"] = {k: round(v, 6) for k, v in p.effects.items() if v}
            if p.power_kw:
                entry["power_kw"] = p.power_kw * count
                out.power_kw += p.power_kw * count
            if p.fuel:
                entry["fuel_per_min"] = p.fuel * ops
                fuel_total += p.fuel * ops
            out.pollution_per_min += p.pollution * count
            out.recipes[p.key] = entry
            out.machines[p.machine] = out.machines.get(p.machine, 0) + ceil
        out.fuel_per_min = fuel_total
        belts = self.opts.get("belts") or sorted(
            (n for n, e in self.gd.raw["entities"].items() if e.get("type") == "transport-belt"),
            key=lambda n: self.gd.raw["entities"][n]["belt_items_per_s"])
        for i, f in sorted(flows.items()):
            if i == ELECTRICITY or self.gd.is_fluid(i):
                continue
            out.belts[i] = {bn: float(f / 60 / self.gd.belt_throughput(bn)) for bn in belts}


def solve(targets: dict, gd: GameData, opts: dict | None = None) -> RateResult:
    """Solve for `targets` {item or "item@quality": per_min, "electricity": kW}. See module doc for opts."""
    return _Solver(targets, gd, dict(opts or {})).run()
