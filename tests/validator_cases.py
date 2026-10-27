"""Scenarios shared by tests/test_validate.py (offline) and tests/live_validator.py (built + run on a test slot).

Each case: entities (local coords), resources [(name, tx, ty, amount)], water [(tx, ty)], expect (for validate),
want (error kinds the validator must report; empty = must pass), engine extras (loaders/infinity chests used only
to feed/drain the layout in-engine), fuel (positions that get coal in-engine = manual fuel), ticks, and
`works(info, eng)` -> (bool, detail): did the layout actually do its job in the engine.
"""
from __future__ import annotations

E, N, S, W = 4, 0, 8, 12


def pe(name, x, y, d=0, recipe=None, **extra):
    return {"name": name, "x": x, "y": y, "direction": d, "recipe": recipe, "extra": extra or None}


def find(info, name, x, y):
    for v in info.values():
        if v.get("name") == name and v.get("pos") and abs(v["pos"][0] - x) < 1e-6 and abs(v["pos"][1] - y) < 1e-6:
            return v
    return {}


def chest_count(info, x, y, item=None):
    c = find(info, "wooden-chest", x, y).get("contents") or []
    return sum(i["count"] for i in c if item is None or i["name"] == item)


def ore(name, x0, y0, x1, y1, amount=5000):
    return [(name, x, y, amount) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def case(name, entities, want=(), resources=(), water=(), expect=None, fuel=(), extras=(), ticks=3600,
         works=None, engine=True, sink_chests=()):
    return {"name": name, "entities": entities, "want": set(want), "resources": list(resources),
            "water": list(water), "expect": expect or {}, "fuel": list(fuel), "extras": list(extras),
            "ticks": ticks, "works": works, "engine": engine}


# ---- 1. burner drill straight into a furnace, inserter into a chest -------------------------------------------
def _drill_furnace(fuel_furnace=True):
    ents = [pe("burner-mining-drill", 1, 3, N), pe("stone-furnace", 1, 1), pe("burner-inserter", 2.5, 0.5, W),
            pe("wooden-chest", 3.5, 0.5)]
    fuel = [(1, 3), (2.5, 0.5)] + ([(1, 1)] if fuel_furnace else [])
    expect = {"manual_fuel": True} if fuel_furnace else {"manual_fuel": [(1, 3), (2.5, 0.5)]}
    return ents, fuel, expect


def _plates_in_chest(x, y, at_least=1):
    def f(info, eng):
        n = chest_count(info, x, y, "iron-plate")
        return n >= at_least, f"{n} iron-plate in chest"
    return f


ents, fuel, ex = _drill_furnace()
C_DRILL_FURNACE = case("drill_furnace_ok", ents, (), ore("iron-ore", 0, 2, 1, 3), expect=ex, fuel=fuel,
                       works=_plates_in_chest(3.5, 0.5))
ents, fuel, ex = _drill_furnace(False)
C_NO_FUEL = case("furnace_no_fuel_path", ents, {"fuel"}, ore("iron-ore", 0, 2, 1, 3), expect=ex, fuel=fuel,
                 works=_plates_in_chest(3.5, 0.5))


# ---- 2. burner drill -> belt line -> inserter -> chest ----------------------------------------------------------
def _belt_line(variant="ok"):
    ents = [pe("burner-mining-drill", 1, 3, N)]
    for x in range(7):
        d = E
        if variant == "reversed_tile" and x == 3:
            d = W
        if variant == "reversed_line":
            d = W
        if variant == "broken" and x == 3:
            continue
        ents.append(pe("transport-belt", x + 0.5, 1.5, d))
    ins_dir = N if variant == "inserter_nothing" else W
    ents += [pe("burner-inserter", 7.5, 1.5, ins_dir), pe("wooden-chest", 8.5, 1.5)]
    return ents


def _ore_in_chest(info, eng):
    n = chest_count(info, 8.5, 1.5, "iron-ore")
    return n > 0, f"{n} iron-ore in chest"


_BL = dict(resources=ore("iron-ore", 0, 2, 1, 3), expect={"manual_fuel": True}, fuel=[(1, 3), (7.5, 1.5)],
           works=_ore_in_chest)
C_BELT_OK = case("belt_line_ok", _belt_line(), (), **_BL)
C_BROKEN = case("broken_belt", _belt_line("broken"), {"belt_dead_end", "inserter_starved"}, **_BL)
C_REV_TILE = case("reversed_belt_tile", _belt_line("reversed_tile"), {"belt_headon"}, **_BL)
C_REV_LINE = case("reversed_belt_line", _belt_line("reversed_line"), {"belt_dead_end", "inserter_starved"}, **_BL)
C_INS_NOTHING = case("inserter_facing_nothing", _belt_line("inserter_nothing"),
                     {"inserter_pickup", "inserter_drop", "belt_dead_end"}, **_BL)


def _headon_lines():
    ents = [pe("burner-mining-drill", 1, 3, N), pe("burner-mining-drill", 9, 3, N)]
    ents += [pe("transport-belt", x + 0.5, 1.5, E) for x in range(5)]
    ents += [pe("transport-belt", x + 0.5, 1.5, W) for x in range(5, 10)]
    ents += [pe("burner-inserter", 4.5, 0.5, S), pe("wooden-chest", 4.5, -0.5)]
    return ents


C_HEADON = case("headon_belts", _headon_lines(), {"belt_headon", "belt_dead_end"},
                ore("iron-ore", 0, 2, 1, 3) + ore("iron-ore", 8, 2, 9, 3), expect={"manual_fuel": True},
                fuel=[(1, 3), (9, 3), (4.5, 0.5)],
                works=lambda info, eng: (chest_count(info, 4.5, -0.5) > 0 and
                                         not find(info, "transport-belt", 5.5, 1.5).get("items"),
                                         f"chest {chest_count(info, 4.5, -0.5)}, items stuck on the right line: "
                                         f"{find(info, 'transport-belt', 5.5, 1.5).get('items')}"))
# works = both lines deliver; head-on, the right line's ore stays stuck on its belts


# ---- 3. drill off ore ------------------------------------------------------------------------------------------
C_OFF_ORE = case("drill_off_ore", [pe("burner-mining-drill", 1, 3, N), pe("wooden-chest", 0.5, 1.5)],
                 {"drill_resource"}, ore("iron-ore", 5, 5, 6, 6), expect={"manual_fuel": True}, fuel=[(1, 3)],
                 works=lambda info, eng: (chest_count(info, 0.5, 1.5) > 0, f"chest {chest_count(info, 0.5, 1.5)}"))


# ---- 4. drills -> mixed-lane belt -> burner furnaces -> output belt -> chest -----------------------------------
def _smelter_line(fuel_out=True):
    ents = [pe("burner-mining-drill", x, 2, N) for x in (1, 3, 5)]          # iron, drop on the right (south) lane
    ents.append(pe("burner-mining-drill", 1, -1, S))                       # coal, drop on the left (north) lane
    ents += [pe("transport-belt", x + 0.5, 0.5, E) for x in range(12)]
    for x in (7.5, 9.5):
        ents += [pe("burner-inserter", x, 1.5, N), pe("stone-furnace", x + 0.5, 3), pe("burner-inserter", x, 4.5, N)]
    ents += [pe("transport-belt", x + 0.5, 5.5, E) for x in range(7, 12)]
    ents += [pe("burner-inserter", 12.5, 5.5, W), pe("wooden-chest", 13.5, 5.5)]
    manual = [(1, 2), (3, 2), (5, 2), (1, -1), (12.5, 5.5)] + ([(7.5, 4.5), (9.5, 4.5)] if fuel_out else [])
    return ents, manual


_res = ore("iron-ore", 0, 1, 5, 2) + ore("coal", 0, -2, 1, -1)
ents, manual = _smelter_line()
# Unfuelled burner inserters still move ~14 items on their start-up energy buffer, then stall for good (checked
# in-engine over 4 more minutes), so "works" needs a steady-state amount: 2 min of 2 furnaces ~ 60+ plates.
C_SMELTER = case("smelter_line_ok", ents, (), _res, expect={"manual_fuel": manual}, fuel=manual, ticks=7200,
                 works=_plates_in_chest(13.5, 5.5, 30))
ents, manual = _smelter_line(False)
C_SMELTER_NOFUEL = case("smelter_output_inserters_unfuelled", ents, {"fuel"}, _res, expect={"manual_fuel": manual},
                        fuel=manual, ticks=7200, works=_plates_in_chest(13.5, 5.5, 30))


# ---- 5. steam power: offshore pump -> pipes -> boiler -> engine -> pole -> electric drill -> chest -------------
LAKE = [(x, y) for x in range(-5, 0) for y in range(-3, 6)]


def _steam(variant="ok"):
    ents = [pe("offshore-pump", 0.5, 1.5, W)]
    if variant == "offshore_off_shore":
        ents = [pe("offshore-pump", 0.5, 7.5, W)] + [pe("pipe", 1.5, y + 0.5) for y in range(2, 8)]
    if variant in ("ptg_ok", "ptg_wrong"):
        ents += [pe("pipe-to-ground", 1.5, 1.5, W), pe("pipe-to-ground", 6.5, 1.5, E if variant == "ptg_ok" else W)]
    else:
        ents += [pe("pipe", x + 0.5, 1.5) for x in range(1, 7) if not (variant == "pipe_gap" and x == 4)]
    ents += [pe("boiler", 8.5, 1, N), pe("steam-engine", 8.5, -2.5, N), pe("small-electric-pole", 11.5, -2.5)]
    dx = {"unpowered": 3, "two_networks": 10}.get(variant, 0)
    ents += [pe("electric-mining-drill", 14.5 + dx, -2.5, N), pe("wooden-chest", 14.5 + dx, -4.5)]
    if variant == "two_networks":
        ents.append(pe("small-electric-pole", 21.5, -2.5))
    res = ore("iron-ore", 13 + dx, -4, 15 + dx, -2)

    def works(info, eng, dx=dx):
        n = chest_count(info, 14.5 + dx, -4.5)
        return n > 0, f"{n} ore in chest; drill status {find(info, 'electric-mining-drill', 14.5 + dx, -2.5).get('status')}"
    return case("steam_" + variant, ents, (), res, LAKE, {"manual_fuel": True}, fuel=[(8.5, 1)], ticks=1800,
                works=works)


C_STEAM = _steam()
C_PIPE_GAP = dict(_steam("pipe_gap"), want={"fluid_source"})
C_PTG_OK = _steam("ptg_ok")
C_PTG_WRONG = dict(_steam("ptg_wrong"), want={"pipe_to_ground", "fluid_source"})
C_OFFSHORE = dict(_steam("offshore_off_shore"), want={"offshore"})
C_UNPOWERED = dict(_steam("unpowered"), want={"power_coverage"})
C_TWO_NETS = dict(_steam("two_networks"), want={"power_network"})


# ---- 6. full belt sideloaded onto one lane (overload) vs. curved (fine) ----------------------------------------
def _overload(sideload=True):
    ents = [pe("transport-belt", x + 0.5, 0.5, E) for x in range(6)]
    ents += [pe("transport-belt", 6.5, y + 0.5, N) for y in range(-6, 1 + (3 if sideload else 0))]
    extras = [{"name": "infinity-chest", "x": -2.5, "y": 0.5, "infinity": ["iron-plate"]},
              {"name": "loader", "x": -1, "y": 0.5, "direction": E, "ug": "output"},
              {"name": "loader", "x": 6.5, "y": -7, "direction": N, "ug": "input"},
              {"name": "wooden-chest", "x": 6.5, "y": -8.5}]
    expect = {"sources": [{"x": 0.5, "y": 0.5, "item": "iron-plate", "per_min": 900}],
              "sinks": [{"x": 6.5, "y": -6.5, "item": "iron-plate", "per_min": 900}],
              "rates": {"iron-plate": 900}}
    ticks = 1800

    def works(info, eng, name="overloaded_belt" if sideload else "curve_full_belt"):
        n = eng.contents(name, 6.5, -8.5)
        secs = getattr(eng, "elapsed", ticks) / 60
        rate = n / secs
        return rate > 15 * 0.8, f"{n} plates in {secs:.0f} s = {rate:.1f}/s (plan 15/s, belt fill-up included)"
    return case("overloaded_belt" if sideload else "curve_full_belt", ents,
                {"belt_overload", "rate"} if sideload else (), expect=expect, extras=extras, ticks=ticks, works=works)


C_OVERLOAD = _overload(True)
C_CURVE = _overload(False)


# ---- 7. underground belt pairing ------------------------------------------------------------------------------
def _ug(gap):
    ents = [pe("burner-mining-drill", 1, 3, N), pe("transport-belt", 0.5, 1.5, E),
            pe("underground-belt", 1.5, 1.5, E, type="input"),
            pe("underground-belt", 1.5 + gap, 1.5, E, type="output"),
            pe("transport-belt", 2.5 + gap, 1.5, E), pe("burner-inserter", 3.5 + gap, 1.5, W),
            pe("wooden-chest", 4.5 + gap, 1.5)]

    def works(info, eng):
        n = chest_count(info, 4.5 + gap, 1.5)
        return n > 0, f"{n} ore in chest"
    return case(f"underground_gap{gap}", ents, () if gap <= 5 else {"underground"}, ore("iron-ore", 0, 2, 1, 3),
                expect={"manual_fuel": True}, fuel=[(1, 3), (3.5 + gap, 1.5)], works=works)


C_UG5 = _ug(5)
C_UG6 = _ug(6)

LIVE = [C_DRILL_FURNACE, C_NO_FUEL, C_BELT_OK, C_BROKEN, C_REV_TILE, C_REV_LINE, C_INS_NOTHING, C_HEADON, C_OFF_ORE,
        C_SMELTER, C_SMELTER_NOFUEL, C_STEAM, C_PIPE_GAP, C_PTG_OK, C_PTG_WRONG, C_OFFSHORE, C_UNPOWERED, C_TWO_NETS,
        C_OVERLOAD, C_CURVE, C_UG5, C_UG6]


# ---- 8. blocks from factorio_claude/blueprints.py generators (known-good layouts) ------------------------------
def gen_entities(ents):
    """blueprints.py dicts -> PE dicts (string directions -> 16-way)."""
    dirs = {"north": N, "east": E, "south": S, "west": W}
    return [pe(e["name"], e["x"], e["y"], dirs.get(e.get("direction"), e.get("direction") or 0), e.get("recipe"))
            for e in ents]


def _eei(x, y):
    return {"name": "electric-energy-interface", "x": x, "y": y, "eei": 100000}


def _gen_cases():
    from factorio_claude import blueprints as bp
    out = []

    def counted(name, x, y, item, at_least):
        def works(info, eng):
            n = eng.contents(name, x, y, item)
            return n >= at_least, f"{n} {item} delivered"
        return works

    # electric smelter column: ore in at the bottom of the left belt, plates out at the top of the right belt
    ents = gen_entities(bp.smelter_column(2, furnace="electric-furnace"))
    extras = [{"name": "infinity-chest", "x": 0.5, "y": 8.5, "infinity": ["iron-ore"]},
              {"name": "loader", "x": 0.5, "y": 7, "direction": N, "ug": "output"},
              {"name": "loader", "x": 6.5, "y": -1, "direction": N, "ug": "input"},
              {"name": "wooden-chest", "x": 6.5, "y": -2.5}, _eei(-1, 3)]
    out.append(case("gen_smelter_column_electric", ents, (), extras=extras, ticks=3600,
                    expect={"ports": [{"kind": "belt_in", "item": "iron-ore", "x": 0.5, "y": 6.5, "direction": N},
                                      {"kind": "belt_out", "item": "iron-plate", "x": 6.5, "y": -0.5, "direction": N},
                                      {"kind": "pole", "x": 1.5, "y": 2.5}]},
                    works=counted("gen_smelter_column_electric", 6.5, -2.5, "iron-plate", 30)))
    # stone smelter column: ore (right lane) + coal (left lane) put on by burner inserters from infinity chests
    for fuelled in (True, False):
        name = "gen_smelter_column_stone" + ("" if fuelled else "_unfuelled_outputs")
        ents = gen_entities(bp.smelter_column(2)) + [pe("transport-belt", 0.5, y + 0.5, N) for y in (4, 5)]
        extras = [{"name": "infinity-chest", "x": -1.5, "y": y, "infinity": ["iron-ore"]} for y in (4.5, 5.5)]
        extras += [{"name": "burner-inserter", "x": -0.5, "y": y, "direction": W} for y in (4.5, 5.5)]
        extras += [{"name": "infinity-chest", "x": 2.5, "y": 5.5, "infinity": ["coal"]},
                   {"name": "burner-inserter", "x": 1.5, "y": 5.5, "direction": E},
                   {"name": "loader", "x": 5.5, "y": -1, "direction": N, "ug": "input"},
                   {"name": "wooden-chest", "x": 5.5, "y": -2.5}]
        outs = [(4.5, 0.5), (4.5, 2.5)]
        fuel = [(-0.5, 4.5), (-0.5, 5.5)] + (outs if fuelled else [])
        out.append(case(name, ents, () if fuelled else {"fuel"}, extras=extras, fuel=fuel, ticks=7200,
                        expect={"sources": [{"x": 0.5, "y": 5.5, "items": ["iron-ore", "coal"]}],
                                "sinks": [{"x": 5.5, "y": -0.5}], "manual_fuel": outs if fuelled else []},
                        works=counted(name, 5.5, -2.5, "iron-plate", 30)))
    # gear assembler line: plates in on the left belt, gears out on the right belt
    ents = gen_entities(bp.assembler_line(2, recipe="iron-gear-wheel"))
    extras = [{"name": "infinity-chest", "x": 0.5, "y": 8.5, "infinity": ["iron-plate"]},
              {"name": "loader", "x": 0.5, "y": 7, "direction": N, "ug": "output"},
              {"name": "loader", "x": 6.5, "y": -1, "direction": N, "ug": "input"},
              {"name": "wooden-chest", "x": 6.5, "y": -2.5}, _eei(-1, 2)]
    out.append(case("gen_assembler_line_gears", ents, (), extras=extras, ticks=3600,
                    expect={"sources": [{"x": 0.5, "y": 6.5, "direction": N, "item": "iron-plate"}],
                            "sinks": [{"x": 6.5, "y": -0.5}], "power": "external"},
                    works=counted("gen_assembler_line_gears", 6.5, -2.5, "iron-gear-wheel", 30)))
    # electric mining outpost on iron: the generator's poles sit east of the east drills only
    ents = gen_entities(bp.mining_outpost(4))
    extras = [{"name": "loader", "x": 3.5, "y": -1, "direction": N, "ug": "input"},
              {"name": "wooden-chest", "x": 3.5, "y": -2.5}, _eei(9, 2)]

    def outpost_works(info, eng):
        drills = [v for v in info.values() if v.get("name") == "electric-mining-drill"]
        idle = [v["pos"] for v in drills if v.get("net") is None]
        n = eng.contents("gen_mining_outpost", 3.5, -2.5)
        return not idle and n > 0, f"{n} ore delivered; drills without power: {idle}"
    out.append(case("gen_mining_outpost", ents, {"power_coverage"}, ore("iron-ore", -1, -1, 7, 12),
                    extras=extras, ticks=1800, expect={"sinks": [{"x": 3.5, "y": -0.5}], "power": "external"},
                    works=outpost_works))
    return out


GEN = _gen_cases()
LIVE += GEN


# ---- 9. splitter: one line split into two consumers; a belt running into a splitter's side ----------------------
def _splitter(side=False):
    if side:
        ents = [pe("burner-mining-drill", 1, 2, N)] + [pe("transport-belt", x + 0.5, 0.5, E) for x in range(4)]
        ents.append(pe("transport-belt", 4.5, 0.5, S))
        res = ore("iron-ore", 0, 1, 1, 2)
        fuel = [(1, 2)]
    else:
        ents = [pe("burner-mining-drill", 1, 3, N)] + [pe("transport-belt", x + 0.5, 1.5, E) for x in range(4)]
        res = ore("iron-ore", 0, 2, 1, 3)
        fuel = [(1, 3)]
    ents.append(pe("splitter", 4.5, 2, E))
    for y in (1.5, 2.5):
        ents += [pe("transport-belt", 5.5, y, E), pe("transport-belt", 6.5, y, E), pe("burner-inserter", 7.5, y, W),
                 pe("wooden-chest", 8.5, y)]
        fuel.append((7.5, y))

    def works(info, eng):
        a, b = chest_count(info, 8.5, 1.5), chest_count(info, 8.5, 2.5)
        return a > 0 and b > 0, f"chests {a} / {b}"
    return case("splitter_side" if side else "splitter_ok", ents,
                {"belt_into_side", "belt_dead_end", "inserter_starved"} if side else (), res,
                expect={"manual_fuel": True}, fuel=fuel, works=works)


C_SPLIT = _splitter()
C_SPLIT_SIDE = _splitter(True)
LIVE += [C_SPLIT, C_SPLIT_SIDE]
