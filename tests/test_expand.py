"""Master growth past the starter base (docs/design/AUTOPILOT.md 8, planner/expand.py): the mall, the plate bus with its
smelting cells (laid by steel or science), power growth, steel, a second science block and oil, appended to the starter
masters of the fixture maps. Offline: fixture maps only (the stages are validated by the planner's validator against
the map; the blocks themselves are engine-checked by tests/live_expand_blocks.py)."""
import copy
import math
from functools import lru_cache

import pytest

from factorio_claude.planner import blocks as B
from factorio_claude.planner import expand as X
from factorio_claude.planner import master as M
from factorio_claude.planner.blocks import PE, Port
from factorio_claude.planner.state import BaseState
from factorio_claude.planner.testing import load_fixture, load_state
from factorio_claude.planner.validate import validate

RESEARCH = ("automation", "logistics", "steel-processing", "electric-mining-drill", "logistic-science-pack",
            "steam-power", "electronics", "automation-science-pack")
OIL_RESEARCH = ("fluid-handling", "oil-gathering", "oil-processing", "plastics")
# synthetic crude-oil wells (test setup: no fixture has crude oil within 224 tiles of spawn)
WELLS_1001 = [(50, -60), (56, -55), (47, -51), (59, -63), (53, -67)]


@lru_cache(maxsize=None)
def _base(seed, variant):
    w, gd = load_fixture(seed)
    st = load_state(seed) or BaseState.fresh_start()
    return M.design_master(w, gd, st, {"near": st.pos or (0, 0)}, variant)


def setup(seed, variant, extra_research=()):
    w, gd = load_fixture(seed, cached=False)
    for t in RESEARCH + tuple(extra_research):
        gd.raw["technologies"][t]["researched"] = True
    for r in gd.raw["recipes"].values():      # the research above unlocks these (the fixture force has none)
        r["enabled"] = True
    return w, gd, copy.deepcopy(_base(seed, variant))


def add_crude(w, wells):
    if "crude-oil" not in w.res_names:
        w.res_names.append("crude-oil")
    k = w.res_names.index("crude-oil")
    for x, y in wells:
        w.res[y - w.y0, x - w.x0] = k
        w.amount[y - w.y0, x - w.x0] = 300000


def _bus(mp):
    return [e for e in mp.ents if (e.extra or {}).get("role") == "bus"]


def _stage(mp, k):
    return [e for e in mp.ents if (e.extra or {}).get("stage") == k]


def _zones_disjoint(mp, k):
    zones = [z for z in mp.zones if z.first_stage == k]
    old = [z for z in mp.zones if z.first_stage != k]
    for z in zones:
        for o in old:
            assert not (z.rect[0] < o.rect[2] and o.rect[0] < z.rect[2] and z.rect[1] < o.rect[3] and
                        o.rect[1] < z.rect[3]), (z.id, o.id)
    return zones


@pytest.mark.parametrize("seed,variant", [(1001, "hub"), (2002, "onsite")])
def test_mall_taps_the_starter_plate_lines_and_comes_before_green(seed, variant):
    """Round 3: the mall is a small row with its own short feeds from the starter base's iron and copper lines (no new
    smelting to build first: round 2/3's bus + cells version was 650-915 entities, built by hand before any of it
    worked), placed right after the electric-drill stage so green science is built from it."""
    w, gd, mp = setup(seed, variant)
    n_before = len(mp.ents)
    info = X.extend(mp, w, gd, "mall", after=4)
    k = info["stage"]
    assert k == 4.7 and M.slabel(k) == "S4m" and info["key"] == "x_mall"
    assert info["entities"] < 350
    # the starter base's stages are untouched; the new stage only adds
    assert len([e for e in mp.ents if (e.extra or {}).get("stage") != k]) == n_before
    assert not M.check_monotone(mp, gd)
    assert M.validate_stage(mp, gd, w, k).ok
    assert M.validate_stage(mp, gd, w, 5).ok            # S5 still valid on top of it
    assert not _bus(mp)                                 # no bus for the mall: it comes with steel / science
    feeds = info["feeds"]
    assert {f["item"] for f in feeds} == {"iron-plate", "copper-plate"} and all(f.get("tap") for f in feeds)
    assert all(f["belt_tiles"] <= X.MALL_FEED_MAX for f in feeds)
    # three tap inserters on the iron line (a basic inserter moves ~50 plates/min)
    taps = [e for e in _stage(mp, k) if (e.extra or {}).get("role") == "tap" and (e.extra or {}).get("item") ==
            "iron-plate"]
    assert len(taps) == X.MALL_TAPS["iron-plate"]
    assert len(info["chests"]) >= 10 and "inserter" in info["items"] and "transport-belt" in info["items"]
    plan = M.stage_plan(mp, k, w, gd, validation={"ok": True})
    assert plan.stats.get("mall_chests")
    # building the mall itself is bootstrap hand-crafting (the mod passes it to the helper's material planner)
    assert plan.stats.get("mall_build") and plan.preview_args(show=False).get("mall_build") is True
    rows = plan.rows()
    marked = [r for r in rows if len(r) > 5 and isinstance(r[5], dict) and r[5].get("mall")]
    assert any(r[5].get("bar") for r in marked) and any(r[5]["mall"] == "machine" for r in marked)
    zones = _zones_disjoint(mp, k)
    assert any(z.id == "x_mall" for z in zones)


def test_feeds_are_proportional():
    """No snaking (doctrine ORG-8): every feed the expansions route is at most 1.4x (relaxed: 2x) its end points'
    taxicab distance + slack, with few turns; the shape check itself."""
    s = X.route_shape([PE("transport-belt", x + 0.5, 0.5, 4) for x in range(10)], Port("belt_out", "x", 0, 0, 4),
                      Port("belt_in", "x", 9, 0, 4))
    assert s == {"tiles": 10, "manhattan": 9, "ratio": 1.11, "turns": 0} and X.proportional(s)
    loop = [PE("transport-belt", x + 0.5, 0.5, 4) for x in range(10)] + \
        [PE("transport-belt", 9.5, y + 0.5, 8) for y in range(1, 20)] + \
        [PE("transport-belt", x + 0.5, 19.5, 12) for x in range(9, -1, -1)] + \
        [PE("transport-belt", 0.5, y + 0.5, 0) for y in range(18, 1, -1)]
    s = X.route_shape(loop, Port("belt_out", "x", 0, 0, 4), Port("belt_in", "x", 0, 2, 0))
    assert not X.proportional(s) and not X.proportional(s, X.SHAPES[1])
    w, gd, mp = setup(1001, "hub")
    info = X.extend(mp, w, gd, "mall", after=4)
    assert info["feed_belt_tiles"] < 150
    for f in info["feeds"]:
        assert f["belt_tiles"] < 100, f


def test_power_grow_fills_the_reserved_units():
    w, gd, mp = setup(1001, "hub")
    boilers = sum(1 for e in mp.ents if e.name == "boiler")
    info = X.extend(mp, w, gd, "power_grow")
    assert info["units"] and len(info["units"]) == 2
    k = info["stage"]
    new = _stage(mp, k)
    assert sum(1 for e in new if e.name == "boiler") == 2 and sum(1 for e in new if e.name == "steam-engine") == 4
    assert sum(1 for e in mp.ents if e.name == "boiler") == boilers + 2
    assert all((e.extra or {}).get("zone") == "power" for e in new)
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    with pytest.raises(X.ExpandError):          # the reserved room holds 6 units: the next growth needs a new block
        X.extend(mp, w, gd, "power_grow")


def test_steel_lays_the_plate_bus_with_its_smelting():
    """Steel is the first block that needs plates beyond the starter base: it lays the plate bus (two straight belts,
    a corridor reserved for 8 lanes) fed by a smelting cell per metal within reach (else a tap on the starter line),
    and hangs off it; coal from a tap on a starter coal belt."""
    w, gd, mp = setup(3003, "onsite")
    X.extend(mp, w, gd, "mall", after=4)
    info = X.extend(mp, w, gd, "steel")
    k = info["stage"]
    assert M.slabel(k) == "S6" and info["chests"]
    bus = _bus(mp)
    by = {}
    for e in bus:
        by.setdefault(e.extra["item"], []).append(e)
    assert set(by) == {"iron-plate", "copper-plate"}
    for item, belts in by.items():
        assert len({b.direction for b in belts}) == 1
        assert len({b.x for b in belts}) == 1 or len({b.y for b in belts}) == 1
        assert len(belts) == X.BUS_LEN
    assert any(z.kind == "bus" for z in _zones_disjoint(mp, k))
    items = [f["item"] for f in info["feeds"]]
    assert items.count("iron-plate") >= 2 and "coal" in items and "copper-plate" in items
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    # taps on the bus only take from belts that carry nothing but their item
    taps = [e for e in _stage(mp, k) if (e.extra or {}).get("role") == "tap"]
    assert taps
    # cells are near: no lone feeder belt longer than CELL_FEED_MAX from a far patch
    for f in info["feeds"]:
        if f.get("cell"):
            assert f["belt_tiles"] <= 2 * X.CELL_FEED_MAX + X.CELL_COAL_MAX


def test_keepout_and_failures_leave_the_master_unchanged():
    w, gd, mp = setup(1001, "hub")
    before = (len(mp.ents), len(mp.zones), len(mp.stages))
    # a keep-out box over the whole map: nothing fits
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "mall", after=4, keepout=[(-500, -500, 500, 500)])
    assert (len(mp.ents), len(mp.zones), len(mp.stages)) == before
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "nonsense")
    with pytest.raises(X.ExpandError):          # no crude oil on the map
        X.extend(mp, w, gd, "oil")
    assert (len(mp.ents), len(mp.zones), len(mp.stages)) == before


def test_mall_block_layout():
    """The mall row on its own: iron only ever on belt A's north lane (side-loaded), gears dropped on its south
    lane, circuits on belt B, every machine with its inputs; direct-insertion neighbours; validates with external
    power and plate ports. Belt A ends at the last machine (no depot: it backs up), belts get a 4-stack chest."""
    _w, gd = load_fixture(1001)
    blk = B.mall_block(gd, None)
    rep = validate(blk.entities, gd, None, {"ports": blk.ports, "power": "external", "assume_research": True})
    assert rep.ok, rep.summary()
    items = blk.info["items"]
    # belts first: the helper's biggest demand (1700+ belts in 3.5 h runs) gets gears before the chests upstream fill;
    # then the inserter group ahead of every other circuit user; no splitter (never needed, it hogged circuits)
    assert items[:5] == ["transport-belt", "underground-belt", "long-handed-inserter", "inserter", "fast-inserter"] \
        and "iron-chest" in items and "splitter" not in items
    roles = [((e.extra or {}).get("role"), (e.extra or {}).get("unit")) for e in blk.entities]
    assert roles.count(("mall_out", "transport-belt")) == 2 and roles.count(("mall_in", "transport-belt")) == 2
    am = {e.recipe: e for e in blk.entities if (e.extra or {}).get("role") in ("mall_am", "head_am")}
    assert set(items) <= set(am) and {"iron-gear-wheel", "electronic-circuit", "copper-cable"} <= set(am)
    links = [e for e in blk.entities if (e.extra or {}).get("role") == "mall_link"]
    assert {e.extra["unit"] for e in links} == {"underground-belt", "steam-engine", "fast-inserter",
                                                "long-handed-inserter"}
    no_logistics = B.mall_block(gd, ["inserter", "transport-belt", "pipe"])
    assert "underground-belt" not in no_logistics.info["items"]
    bars = {e.extra["unit"]: e.extra["bar"] for e in blk.entities if (e.extra or {}).get("role") == "mall_chest"}
    assert bars["transport-belt"] == 4 and all(1 <= b <= 2 for i, b in bars.items() if i != "transport-belt")
    assert not [e for e in blk.entities if (e.extra or {}).get("role") in ("depot", "depot_in")]
    assert [e for e in B.mall_block(gd, None, depot=True).entities if (e.extra or {}).get("role") == "depot"]


def test_second_science_block_and_the_mall_keeps_its_first_pick():
    """The science block hangs off the bus downstream of everything the mall takes (the mall keeps the first pick);
    on a master whose mall sits on the bus (a master grown in another order), the mall's taps are in the bus's
    upstream third and the science taps after them."""
    w, gd, mp = setup(2002, "hub")
    k_steel = X.extend(mp, w, gd, "steel")["stage"]          # lays the bus
    info_m = X.extend(mp, w, gd, "mall", after=k_steel)
    bus = _bus(mp)
    rows = X.mall_tap_rows(mp, bus)
    assert rows and max(rows) < X.BUS_LEN // 3 + 2
    info = X.extend(mp, w, gd, "science")
    k = info["stage"]
    new = _stage(mp, k)
    names = [e.name for e in new]
    assert names.count("lab") == 10 and sum(1 for e in new if e.recipe == "automation-science-pack") == M.N_RED
    assert sum(1 for e in new if e.recipe == "logistic-science-pack") == B.GREEN_N
    assert {f["item"] for f in info["feeds"] if f.get("tap")} >= {"iron-plate", "copper-plate"}
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    sci_rows = []
    tiles = {(math.floor(e.x), math.floor(e.y)): int(e.extra.get("row", 0)) for e in bus
             if e.name == "transport-belt"}
    for e in new:
        if (e.extra or {}).get("role") == "tap" and "inserter" in e.name:
            dx, dy = X.DVEC[int(e.direction or 0)]
            r = 2 if e.name == "long-handed-inserter" else 1
            t = (math.floor(e.x) + r * dx, math.floor(e.y) + r * dy)
            if t in tiles:
                sci_rows.append(tiles[t])
    assert sci_rows and min(sci_rows) > max(rows)
    assert info_m["entities"] < 400


def test_oil_pumpjacks_then_the_plant():
    """G9: pumpjacks on the wells nearest the base (their outputs kept free), then the refinery + plastic plant near a
    coal belt with a crude pipe from every pumpjack; the base validates with the plant (crude reaches the refinery's
    2nd input, petroleum leaves its 3rd output into the chemical plant)."""
    w, gd, mp = setup(1001, "hub", OIL_RESEARCH)
    add_crude(w, WELLS_1001)
    info = X.extend(mp, w, gd, "oil")
    k = info["stage"]
    jacks = [e for e in _stage(mp, k) if e.name == "pumpjack"]
    assert 1 <= len(jacks) <= X.OIL_WELLS
    for j in jacks:
        assert (math.floor(j.x), math.floor(j.y)) in WELLS_1001
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "oil")               # once per master
    info = X.extend(mp, w, gd, "oil_plant")
    k2 = info["stage"]
    new = _stage(mp, k2)
    ref = [e for e in new if e.name == "oil-refinery"]
    assert len(ref) == 1 and ref[0].recipe == "basic-oil-processing"
    assert [e.recipe for e in new if e.name == "chemical-plant"] == ["plastic-bar"]
    assert any(e.name in ("pipe", "pipe-to-ground") and (e.extra or {}).get("role") == "crude_pipe" for e in new)
    assert info["chests"] and any(f["item"] == "coal" for f in info["feeds"])
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k2).ok
    assert M.slabel(k2) == M.slabel(k + 1)


def test_oil_plant_block_and_the_refinerys_fluid_boxes():
    """The block on its own validates; crude piped into the refinery's 1st input instead (the wrong box: basic oil
    processing takes crude in its 2nd, fluidbox_index 2) does not."""
    _w, gd = load_fixture(1001)
    blk = B.oil_plant_block(gd)
    opts = {"power": "external", "assume_research": True}
    rep = validate(blk.entities, gd, None, dict(opts, ports=blk.ports))
    assert rep.ok, rep.summary()
    coal, crude = blk.ports
    wrong = Port("pipe_in", "crude-oil", crude.x - 2, crude.y, crude.direction)
    rep = validate(blk.entities, gd, None, dict(opts, ports=[coal, wrong]))
    assert not rep.ok and any(i.kind == "fluid_source" for i in rep.errors)


def test_medium_poles_once_the_master_makes_steel():
    """After the steel stage the expansions use medium poles (7x7 supply, no wood): about half as many poles, the
    hand-made item the mall cannot make."""
    w, gd, mp = setup(3003, "onsite")
    assert X.pole_for(mp, gd) == "small-electric-pole"
    X.extend(mp, w, gd, "mall", after=4)
    X.extend(mp, w, gd, "steel")
    assert X.pole_for(mp, gd) == "medium-electric-pole"
    info = X.extend(mp, w, gd, "science")
    new = _stage(mp, info["stage"])
    assert any(e.name == "medium-electric-pole" for e in new)
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, info["stage"]).ok


def test_urgent_power_follows_the_power_blocks_own_units():
    """Power growth asked for early goes right after the stage being built (S4p) - but never before a starter stage
    that adds a unit of its own (the units are chained: water and coal pass through the boilers before them)."""
    w, gd, mp = setup(1001, "hub")
    assert M.slabel(4.6) == "S4p" and M.slabel(4.7) == "S4m"
    info = X.extend(mp, w, gd, "power_grow", after=2)       # S4 and S5 add boilers: after them
    assert info["stage"] == 6.0
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, 6.0).ok


@pytest.mark.parametrize("seed,variant", [(3003, "onsite")])     # 2002: no free iron patch near its bus
def test_iron_grow_adds_a_cell_onto_the_bus_and_repeats(seed, variant):
    """iron_grow (the autopilot's answer to an iron bottleneck): a smelting cell whose plates side-load onto the
    bus's iron belt; repeatable (a second one finds another patch or spot); with no bus yet, it lays the bus."""
    w, gd, mp = setup(seed, variant)
    X.extend(mp, w, gd, "steel")                             # lays the bus
    n_bus = len(_bus(mp))
    info = X.extend(mp, w, gd, "iron_grow")
    k = info["stage"]
    new = _stage(mp, k)
    assert len(_bus(mp)) == n_bus                            # onto the existing bus, no new one
    assert sum(1 for e in new if e.name == "electric-mining-drill") >= 4
    assert sum(1 for e in new if e.name == "stone-furnace") >= 4
    assert [f["item"] for f in info["feeds"]] == ["iron-plate"]
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    assert "iron_grow" in X.templates_built(mp)
    try:
        k2 = X.extend(mp, w, gd, "iron_grow")["stage"]       # repeatable when there is room
        assert k2 > k and M.validate_stage(mp, gd, w, k2).ok
    except X.ExpandError:
        pass                                                 # no second patch in reach is a fair answer


def test_grow_without_a_bus_lays_one():
    w, gd, mp = setup(2002, "hub")
    info = X.extend(mp, w, gd, "copper_grow")
    assert _bus(mp) and M.validate_stage(mp, gd, w, info["stage"]).ok


@pytest.mark.parametrize("seed,variant", [(1001, "hub"), (2002, "onsite")])
def test_mall_add_puts_magazines_into_a_built_mall(seed, variant):
    """Defence v2: a mall planned before Gun turrets was researched gets the firearm-magazine machine later, in its
    reserved room at the row's east end (nothing of the row moves; belt A runs on to it); a mall planned after it has
    the magazine slot from the start."""
    w, gd, mp = setup(seed, variant)
    info = X.extend(mp, w, gd, "mall", after=4)
    assert "firearm-magazine" not in info["items"]
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "mall_add", researched=list(RESEARCH))           # nothing new researched
    n_before = len(mp.ents)
    add = X.extend(mp, w, gd, "mall_add", researched=list(RESEARCH) + ["gun-turret", "fast-inserter"])
    k = add["stage"]
    # (2002 onsite: a pole the mall needed stands in the empty fast-inserter slot - that item waits)
    assert "firearm-magazine" in add["items"] and add["key"] == "x_malladd"
    assert set(add["items"]) == ({"firearm-magazine", "fast-inserter"} if seed == 1001 else {"firearm-magazine"})
    assert len(mp.ents) == n_before + add["entities"] and add["entities"] < 40
    assert set(add["items"]) <= X.mall_items(mp)
    assert not M.check_monotone(mp, gd) and M.validate_stage(mp, gd, w, k).ok
    assert sorted(mp.stages[[s.index for s in mp.stages].index(k)].gate) == sorted(B.MALL_GATE[i] for i in add["items"])
    new = _stage(mp, k)
    chests = [e for e in new if (e.extra or {}).get("role") == "mall_chest"]
    assert {e.extra["unit"] for e in chests} == set(add["items"])
    assert all((e.extra or {}).get("zone", "").startswith("x_mall") for e in new)
    plan = M.stage_plan(mp, k, w, gd, validation={"ok": True})
    assert any(len(r) > 5 and isinstance(r[5], dict) and r[5].get("mall") == "machine" for r in plan.rows())
    with pytest.raises(X.ExpandError):                                      # all in: nothing more to add
        X.extend(mp, w, gd, "mall_add", researched=list(RESEARCH) + ["gun-turret", "fast-inserter"])
    # planned after Gun turrets: the magazine slot is part of the mall itself
    w2, gd2, mp2 = setup(seed, variant, extra_research=("gun-turret",))
    assert "firearm-magazine" in X.extend(mp2, w2, gd2, "mall", after=4)["items"]


# ---- radar network ------------------------------------------------------------------------------------------------------
def _radars(mp):
    return [e for e in mp.ents if e.name == "radar"]


def _on_ore(w, gd, e):
    return any(w.resource_at(x, y) for x, y in gd.footprint(e.name, e.x, e.y, 0))


@pytest.mark.parametrize("seed,variant", [(1001, "hub"), (2002, "onsite"), (3003, "hub"), (4004, "hub")])
def test_first_radar_next_to_the_hub_powered_and_valid(seed, variant):
    """Radar network: the first radar stands next to the hub, off the ore, joined to the master's network by a short
    pole chain (or none), and the base validates with it (its 300 kW within the generators' capacity). 2.1 locks the
    radar recipe behind the Radar tech: that is the stage's gate."""
    w, gd, mp = setup(seed, variant, ("radar",))
    info = X.extend(mp, w, gd, "radar")
    k = info["stage"]
    assert info["key"] == "x_radar" and "radar" in X.templates_built(mp)
    r = _radars(mp)
    assert len(r) == 1 and (r[0].extra or {}).get("stage") == k and [r[0].x, r[0].y] == info["radar"]
    hub = mp.hub
    assert max(abs(r[0].x - hub[0]), abs(r[0].y - hub[1])) <= X.RADAR_HUB_R + 1
    assert not _on_ore(w, gd, r[0])
    assert info["entities"] <= 1 + X.RADAR_POLES_MAX
    assert all(e.name == "radar" or gd.entity(e.name)["type"] == "electric-pole" for e in _stage(mp, k))
    rep = M.validate_stage(mp, gd, w, k)
    assert rep.ok and not [i for i in rep.errors if i.kind.startswith("power")]
    assert not M.check_monotone(mp, gd)
    assert mp.stages[[s.index for s in mp.stages].index(k)].gate == ["radar"]
    _zones_disjoint(mp, k)
    plan = M.stage_plan(mp, k, w, gd, validation={"ok": True})        # (what R2 sends to the game)
    assert any(r[0] == "radar" for r in plan.rows())


def test_radar_data_from_the_export():
    """2.1: the radar recipe is locked (the Radar tech, 20 red packs); 300 kW; the planner's spacing comes from the
    area a radar keeps charted (7x7 chunks: max_distance_of_nearby_sector_revealed 3 when the export has it)."""
    _w, gd = load_fixture(1001)
    assert gd.raw["recipes"]["radar"]["enabled"] is False
    assert {"type": "unlock-recipe", "recipe": "radar"}.items() <= \
        next(e for e in gd.raw["technologies"]["radar"]["effects"]).items()
    assert X.radar_kw(gd) == 300 and X.radar_reach(gd) == (3, 14) and X.radar_spacing(gd) == 168


def test_second_radar_waits_for_the_base_to_grow():
    """The squares two radars keep charted must not overlap much: on a starter base every spot of its edge is within
    radar_spacing of the hub's radar, so a second one does not fit yet (the master is unchanged)."""
    w, gd, mp = setup(1001, "hub", ("radar",))
    X.extend(mp, w, gd, "radar")
    n = len(mp.ents)
    with pytest.raises(X.ExpandError, match="within 168 tiles") as err:
        X.extend(mp, w, gd, "radar")
    assert len(mp.ents) == n
    from factorio_claude.autopilot_rules import RADAR_FULL
    assert RADAR_FULL in str(err.value)[:120]       # (R11 reads it from mem.failed: the exploring radar waits)


@pytest.mark.parametrize("seed,variant", [(1001, "hub"), (3003, "hub")])
def test_next_radar_on_the_edge_toward_uncharted_ground(seed, variant, monkeypatch):
    """Later radars go on a ring just outside the base's built bounding box, toward the octant the game's chunk survey
    finds least charted, at least radar_spacing from every other radar; each validates (powered). (The fixture bases
    are starter-sized: the overlap allowed is raised so a second radar fits on their edge.)"""
    monkeypatch.setattr(X, "RADAR_OVERLAP", 0.7)
    gap = X.radar_spacing(load_fixture(seed)[1])
    for want in (2, 4):                         # south, west (open on both fixtures)
        w, gd, mp = setup(seed, variant, ("radar",))
        X.extend(mp, w, gd, "radar")
        oct_ = [{"dir": i, "charted": 0 if i == want else 9, "total": 9} for i in range(8)]
        bbox = M._ents_rect(gd, mp.ents)
        info = X.extend(mp, w, gd, "radar", hints={"octants": oct_})
        assert info["dir"] == want and info["key"].startswith("x_radar_")
        a, b = _radars(mp)
        assert max(abs(a.x - b.x), abs(a.y - b.y)) >= gap
        # on the ring: outside the bounding box, within the farthest ring offset of it
        out = max(bbox[0] - b.x, b.x - bbox[2], bbox[1] - b.y, b.y - bbox[3])
        assert 0 < out <= max(X.RADAR_EDGE) + 3
        assert not _on_ore(w, gd, b) and M.validate_stage(mp, gd, w, info["stage"]).ok
        assert not M.check_monotone(mp, gd)


def test_next_radar_toward_the_exports_uncharted_chunks(monkeypatch):
    """Without the game's survey the export's own charted mask decides: chunks east of the base left uncharted (a
    charted-only export has nothing there) pull the radar east."""
    monkeypatch.setattr(X, "RADAR_OVERLAP", 0.7)
    w, gd, mp = setup(3003, "hub", ("radar",))
    X.extend(mp, w, gd, "radar")
    x1 = M._ents_rect(gd, mp.ents)[2]
    j = x1 + 20 - w.x0
    w.known[:, j:] = False
    info = X.extend(mp, w, gd, "radar")
    assert info["dir"] in (7, 0, 1)            # east-ish


def test_next_radar_toward_ore_the_autopilot_needs(monkeypatch):
    """hints["toward"]: a radar looks toward the nearest known patch of each resource the autopilot needs (crude oil
    north-west of the base here) when the survey shows no uncharted direction."""
    monkeypatch.setattr(X, "RADAR_OVERLAP", 0.7)
    w, gd, mp = setup(3003, "hub", ("radar",))
    bx0, by0, _bx1, _by1 = M._ents_rect(gd, mp.ents)
    add_crude(w, [(bx0 - 40 + i, by0 - 40 + (i % 3)) for i in range(0, 10, 2)])
    X.extend(mp, w, gd, "radar")
    flat = [{"dir": i, "charted": 9, "total": 9} for i in range(8)]
    info = X.extend(mp, w, gd, "radar", hints={"octants": flat, "toward": ["crude-oil"]})
    assert info["dir"] in (4, 5, 6)            # west / north-west / north


def test_radar_scores_rank_explore_and_wanted_ore():
    w, gd = load_fixture(1001)
    survey = [{"dir": i, "charted": 1 if i == 2 else 10, "total": 10} for i in range(8)]
    ranked = X.radar_scores(w, gd, (0, 0), [(100, 0), (0, 100), (-100, 0)], {"octants": survey})
    assert ranked[0][1] == (0, 100)                         # south: least charted
    # a resource the charted map has no patch of counts the unexplored share once more (it has to be found)
    a = X.radar_scores(w, gd, (0, 0), [(0, 100)], {"octants": survey})[0][0]
    b = X.radar_scores(w, gd, (0, 0), [(0, 100)], {"octants": survey, "toward": ["no-such-ore"]})[0][0]
    assert b > a
    assert X.octant(1, 0) == 0 and X.octant(0, 1) == 2 and X.octant(-1, 0) == 4 and X.octant(0, -1) == 6


def test_oil_that_finds_no_crude_on_the_charted_map_asks_for_a_radar_then_fits_once_charted():
    """Use what radar reveals: the oil template's error on a map without charted crude is one the autopilot reads as
    "explore for crude oil" (R11 points the next radar that way); once the wells are on the charted map the same
    template fits (the planner reads the charted chunks fresh on every expansion)."""
    from factorio_claude import autopilot_rules as R
    w, gd, mp = setup(1001, "hub", OIL_RESEARCH)
    with pytest.raises(X.ExpandError) as err:
        X.extend(mp, w, gd, "oil")
    assert R.explore_targets(R.Memory(failed={"oil": str(err.value)})) == ["crude-oil"]
    add_crude(w, WELLS_1001)                    # (what a radar charting those chunks puts in the next export)
    info = X.extend(mp, w, gd, "oil")
    assert M.validate_stage(mp, gd, w, info["stage"]).ok


# ---- blue science (roadmap G10, round 4) -----------------------------------------------------------------------------
BLUE_RESEARCH = ("automation-2", "engine", "advanced-circuit", "sulfur-processing", "chemical-science-pack")
WELLS_2002 = [(36, 16), (42, 21), (33, 25), (45, 13)]     # synthetic crude-oil wells near the 2002 hub master


def test_blue_block_layout_rates_and_names():
    """blocks.blue_block on its own validates (iron, plastic, copper, steel, water and petroleum at its ports, packs
    out of its belt port); assemblers 2 for the packs, engines and advanced circuits: ~13.5 packs/min, the engines'
    rate. Every recipe and gate tech is a 2.1 name from the data export (Advanced circuit, not 1.1's Advanced
    electronics). The block itself is engine-checked by tests/live_expand_blocks.py X6 (13.3 packs/min)."""
    _w, gd = load_fixture(1001, cached=False)        # (enabling recipes must not leak into the cached fixture)
    for r in gd.raw["recipes"].values():
        r["enabled"] = True
    blk = B.blue_block(gd)
    rep = validate(blk.entities, gd, None, {"power": "external", "assume_research": True, "ports": blk.ports})
    assert rep.ok, rep.summary()
    assert blk.rates["chemical-science-pack"] == 13.5 and blk.info["capacity"]["chemical-science-pack"] == 15.0
    # half a sulfur per pack, the sulfur recipe 30 water + 30 petroleum -> 2 (review: the block once said twice that)
    sulfur = gd.raw["recipes"]["sulfur"]
    per_sulfur = {i["name"]: i["amount"] / sulfur["products"][0]["amount"] for i in sulfur["ingredients"]}
    assert blk.rates["petroleum-gas"] == -0.5 * per_sulfur["petroleum-gas"] * 13.5
    assert blk.rates["water"] == -0.5 * per_sulfur["water"] * 13.5
    names = {e.name for e in blk.entities}
    assert "assembling-machine-2" in names and "chemical-plant" in names and not blk.requires
    assert {e.recipe for e in blk.entities if e.recipe} == {"copper-cable", "advanced-circuit", "electronic-circuit",
                                                           "iron-gear-wheel", "pipe", "engine-unit", "sulfur",
                                                           "chemical-science-pack"}
    for e in blk.entities:
        if e.recipe:
            assert e.recipe in gd.raw["recipes"]
    for t in X.GATES["blue"]:
        assert t in gd.raw["technologies"]
    assert "advanced-electronics" not in gd.raw["technologies"]
    # the sulfur plant's water and petroleum ports are its 1st and 2nd input (the recipe's order; engine-checked)
    plant = next(e for e in blk.entities if e.recipe == "sulfur")
    conns = [c for c in gd.fluid_connections_full(plant.name, plant.x, plant.y, plant.direction)
             if c["production_type"] == "input"]
    by = {p.item: (p.x, p.y) for p in blk.ports}
    assert tuple(conns[0]["target_tile"]) == by["water"] and tuple(conns[1]["target_tile"]) == by["petroleum-gas"]
    # petroleum on the wrong input: water and petroleum swapped is not a valid block
    wrong = [Port(p.kind, {"water": "petroleum-gas", "petroleum-gas": "water"}.get(p.item, p.item), p.x, p.y,
                  p.direction) for p in blk.ports]
    rep = validate(blk.entities, gd, None, {"power": "external", "assume_research": True, "ports": wrong})
    assert not rep.ok and any(i.kind in ("fluid_source", "fluid_mix") for i in rep.errors)


def test_labs_pass_packs_on_to_labs():
    """The blue stage's lab row: an inserter takes packs out of a lab only into another lab (validator; the engine
    does the same, live_expand_blocks X7)."""
    _w, gd = load_fixture(1001)
    ents = [PE("lab", 1.5, 1.5), PE("inserter", 1.5, 3.5, 8, None, {}), PE("iron-chest", 1.5, 4.5),
            PE("inserter", 3.5, 1.5, 12), PE("lab", 5.5, 1.5), PE("inserter", 7.5, 1.5, 12), PE("lab", 9.5, 1.5),
            PE("medium-electric-pole", 3.5, 3.5), PE("medium-electric-pole", 7.5, 3.5)]
    opts = {"power": "external", "assume_research": True,
            "containers": {(1.5, 4.5): ["automation-science-pack", "logistic-science-pack"]}}
    rep = validate(ents, gd, None, opts)
    assert rep.ok, rep.summary()
    # out of a lab onto a belt: not possible
    bad = ents[:4] + ents[-2:] + [PE("transport-belt", 4.5, 1.5, 4), PE("transport-belt", 5.5, 1.5, 4)]
    rep = validate(bad, gd, None, opts)
    assert any(i.kind == "inserter_pickup" for i in rep.errors)


def test_blue_science_off_the_bus_with_sulfur_from_the_oil_plant():
    """G10: the blue block beside the plate bus (iron and copper from taps downstream of the mall's), steel out of the
    steel column's depot and plastic out of the oil plant's depot (an inserter and a belt each), petroleum piped from
    the oil plant's header, water from an offshore pump of its own, packs on a belt to a row of new labs chained from
    one of the master's labs. The base validates with it, the master stays monotone (the depot inserters, the header
    pipe and the lab row belong to the zones they stand in), and the rate check gets ~13.5 packs/min."""
    w, gd, mp = setup(2002, "hub", OIL_RESEARCH + BLUE_RESEARCH)
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "blue")            # no plate bus, no oil plant yet
    X.extend(mp, w, gd, "mall", after=4)
    X.extend(mp, w, gd, "steel")
    X.extend(mp, w, gd, "science")
    add_crude(w, WELLS_2002)
    X.extend(mp, w, gd, "oil")
    X.extend(mp, w, gd, "oil_plant")
    n0 = len(mp.ents)
    info = X.extend(mp, w, gd, "blue")
    k = info["stage"]
    assert M.slabel(k) == "S10" and len(mp.ents) > n0 and info["key"] == "x_blue"
    new = _stage(mp, k)
    recipes = [e.recipe for e in new if e.recipe]
    assert recipes.count("chemical-science-pack") == B.BLUE_CHEM and recipes.count("sulfur") == 1
    roles = [(e.extra or {}).get("role") for e in new]
    assert roles.count("depot_out") == 2 and "petroleum_pipe" in roles and "water_pipe" in roles
    assert sum(1 for e in new if e.name == "offshore-pump") == 1
    assert X.LAB_ROW_MIN <= sum(1 for e in new if e.name == "lab") <= X.LAB_ROW and "lab_chain" in roles
    items = {f["item"] for f in info["feeds"]}
    assert items == {"iron-plate", "copper-plate", "steel-plate", "plastic-bar", "chemical-science-pack"}
    rep = M.validate_stage(mp, gd, w, k)
    assert rep.ok, rep.summary()
    assert not M.check_monotone(mp, gd)
    # the first chain inserter takes from one of the master's labs (red + green from its spine inserter)
    first = next(e for e in new if (e.extra or {}).get("role") == "lab_chain" and e.extra.get("unit") == 0)
    dx, dy = X.DVEC[first.direction]
    old = [e for e in mp.ents if e.name == "lab" and (e.extra or {}).get("stage", 0) < k]
    assert any(abs(e.x - (first.x + 2 * dx)) < 0.01 and abs(e.y - (first.y + 2 * dy)) < 0.01 for e in old)
    assert X.blue_rate(mp, gd, k) == {"chemical-science-pack": 13.5} and not X.blue_rate(mp, gd, k - 1)
    with pytest.raises(X.ExpandError):
        X.extend(mp, w, gd, "blue")            # once per master


def test_long_straight_pipes_become_pipe_to_ground_pairs():
    """The blue stage's petroleum and water lines: straight stretches as pipe-to-ground pairs back to back (at most
    the pipe-to-ground's reach apart), the run's first and last pipe plain; the line validates as one fluid network
    (engine-checked: live_expand_blocks X8)."""
    _w, gd = load_fixture(1001, cached=False)        # (enabling recipes must not leak into the cached fixture)
    for r in gd.raw["recipes"].values():
        r["enabled"] = True
    route = [PE("pipe", x + 0.5, 0.5) for x in range(30)] + [PE("pipe", 29.5, y + 0.5) for y in range(1, 4)]
    ents, under = X._ptg_compress(gd, route)
    ptg = [e for e in ents if e.name == "pipe-to-ground"]
    assert len(ents) < 12 and len(ptg) % 2 == 0 and ents[0].name == "pipe" and len(under) > 15
    md = gd.entity("pipe-to-ground")["max_underground_distance"]
    for a, b in zip(ptg[::2], ptg[1::2]):
        assert a.direction == X.WEST and b.direction == X.EAST and 2 <= b.x - a.x <= md
    rep = validate(ents, gd, None, {"assume_research": True,
                                    "ports": [Port("pipe_in", "water", -1, 0, X.EAST),
                                              Port("pipe_out", "water", 29, 4, X.SOUTH)]})
    assert rep.ok and not any(i.kind == "pipe_to_ground" for i in rep.errors + rep.warnings), rep.summary()


# ---- the robot network (roadmap G11, round 5) ------------------------------------------------------------------------
ROBOT_RESEARCH = ("battery", "advanced-oil-processing", "lubricant", "electric-engine", "robotics",
                  "construction-robotics", "logistic-robotics")


def test_robot_block_layout_rates_and_names():
    """blocks.robot_block on its own validates (iron, steel, copper, plastic at its belt ports, water and crude oil
    at its pipe ports): four belts with one plate lane and one intermediate lane each, an oil refinery on ADVANCED
    oil processing (lubricant needs heavy oil) and the battery / electric engine / frame chain; every recipe and gate
    tech is a 2.1 name from the data export. Its shape is two rects (the row and the fluid section above its frame
    machines): every entity stands in one. Engine-checked: tests/live_robots.py RB1 (2.29 frames/min; every product,
    logistic robots last)."""
    _w, gd = load_fixture(1001, cached=False)        # (enabling recipes must not leak into the cached fixture)
    for r in gd.raw["recipes"].values():
        r["enabled"] = True
    blk = B.robot_block(gd)
    rep = validate(blk.entities, gd, None, {"power": "external", "assume_research": True, "ports": blk.ports})
    assert rep.ok, rep.summary()
    assert not [i for i in rep.warnings if i.kind.startswith("fluid") and "light-oil" not in i.msg]
    assert blk.rates["flying-robot-frame"] == 2.25 and not blk.requires
    recipes = {e.recipe for e in blk.entities if e.recipe}
    assert {"battery", "sulfuric-acid", "sulfur", "lubricant", "advanced-oil-processing", "electric-engine-unit",
            "flying-robot-frame", "construction-robot", "logistic-robot", "roboport", "storage-chest",
            "passive-provider-chest", "steel-chest"} <= recipes
    for rc in recipes:
        assert rc in gd.raw["recipes"]
    for t in X.GATES["robots"] + X.GATES["roboports"]:
        assert t in gd.raw["technologies"]
    chests = {e.extra["unit"] for e in blk.entities if (e.extra or {}).get("role") == "mall_chest"}
    assert chests == set(B.ROBOT_ITEMS)
    assert sum(1 for e in blk.entities if e.name == "storage-tank") == 2
    # electric engines need an assembler 2 (crafting with fluid)
    assert next(e for e in blk.entities if e.recipe == "electric-engine-unit").name == "assembling-machine-2"
    # the shape: every entity's tiles inside one of the two rects; the ports just outside them
    shape = blk.info["shape"]
    for e in blk.entities:
        ts = gd.footprint(e.name, e.x, e.y, e.direction)
        assert any(all(r[0] <= x < r[2] and r[1] <= y < r[3] for x, y in ts) for r in shape), e
    for p in blk.ports:
        assert not any(r[0] <= p.x < r[2] and r[1] <= p.y < r[3] for r in shape), p
    # the refinery takes water at its first input and crude at its second (the recipe's order; engine-checked)
    ref = next(e for e in blk.entities if e.recipe == "advanced-oil-processing")
    ins = [tuple(c["target_tile"]) for c in gd.fluid_connections_full(ref.name, ref.x, ref.y, ref.direction)
           if c["production_type"] == "input"]
    water = {(math.floor(e.x), math.floor(e.y)) for e in blk.entities if (e.extra or {}).get("unit") == "water"}
    assert ins[0] in water


@lru_cache(maxsize=None)
def _blue_base(seed=2002, variant="hub"):
    """The 2002 hub master grown to blue science (mall, steel, science, oil, oil plant, blue), once per session."""
    w, gd, mp = setup(seed, variant, OIL_RESEARCH + BLUE_RESEARCH + ROBOT_RESEARCH)
    X.extend(mp, w, gd, "mall", after=4)
    for tpl in ("steel", "science"):
        X.extend(mp, w, gd, tpl)
    add_crude(w, WELLS_2002)
    for tpl in ("oil", "oil_plant", "blue"):
        X.extend(mp, w, gd, tpl)
    return w, gd, mp


@lru_cache(maxsize=None)
def _robot_base():
    w, gd, mp = _blue_base()
    mp = copy.deepcopy(mp)
    info = X.extend(mp, w, gd, "robots")
    return w, gd, mp, info


def test_robot_mall_off_the_bus_with_crude_from_the_oil_plant():
    """G11: the robot mall beside the plate bus (iron and copper from bus taps), steel and plastic out of their
    depots or - the blue stage took the plastic chest's free sides - a tap on the blue stage's plastic feed, crude oil
    branched off the oil plant's crude pipes to its own refinery, water from an offshore pump of its own. The base
    validates with it, the master stays monotone, its product chests and machines are the helper's mall sources."""
    w, gd, mp, info = _robot_base()
    k = info["stage"]
    assert info["key"] == "x_robots" and M.slabel(k) == "S11" and "robots" in X.templates_built(mp)
    assert {f["item"] for f in info["feeds"]} == {"iron-plate", "copper-plate", "steel-plate", "plastic-bar"}
    new = _stage(mp, k)
    roles = [(e.extra or {}).get("role") for e in new]
    assert "robot_crude" in roles and "water_pipe" in roles and sum(1 for e in new if e.name == "offshore-pump") == 1
    assert sum(1 for e in new if e.name == "oil-refinery") == 1
    rep = M.validate_stage(mp, gd, w, k)
    assert rep.ok, rep.summary()
    assert not M.check_monotone(mp, gd)
    assert sorted(mp.stages[[s.index for s in mp.stages].index(k)].gate) == ["construction-robotics",
                                                                             "logistic-robotics"]
    plan = M.stage_plan(mp, k, w, gd, validation={"ok": True})
    rows = [r for r in plan.rows() if len(r) > 5 and isinstance(r[5], dict) and r[5].get("mall")]
    assert {r[5]["mall"] for r in rows} == {"chest", "machine"}
    assert sum(1 for r in rows if r[5]["mall"] == "chest") == len(B.ROBOT_ITEMS)
    assert len(plan.stats["mall_chests"]) == len(B.ROBOT_ITEMS)
    with pytest.raises(X.ExpandError):
        X.extend(copy.deepcopy(mp), w, gd, "robots")          # once per master


def test_robot_mall_needs_the_oil_plant_and_the_steel_depot():
    w, gd, mp = setup(2002, "hub", OIL_RESEARCH + BLUE_RESEARCH + ROBOT_RESEARCH)
    with pytest.raises(X.ExpandError, match="no plate bus"):
        X.extend(mp, w, gd, "robots")


def _linked(ports, link):
    """The roboports form one network: each logistic area overlaps another's (graph connected)."""
    seen, todo = {0}, [0]
    while todo:
        i = todo.pop()
        for j, q in enumerate(ports):
            if j not in seen and max(abs(ports[i][0] - q[0]), abs(ports[i][1] - q[1])) <= link:
                seen.add(j)
                todo.append(j)
    return len(seen) == len(ports)


def test_roboport_grid_covers_the_base_as_one_network():
    """The roboport stages: the first next to the hub with two storage chests beside it, then the spots linked to the
    network that cover the most of the base still outside the construction areas, until all of it is covered (then
    the template says so). Every stage validates (powered), the master stays monotone, the roboports form ONE network
    (neighbouring logistic areas overlap)."""
    w, gd, mp, _info = _robot_base()
    mp = copy.deepcopy(mp)
    assert X.roboport_coverage(mp, gd)["uncovered"] > 0 and X.roboport_radii(gd) == (25, 55)
    stages = []
    for _ in range(6):
        try:
            stages.append(X.extend(mp, w, gd, "roboports"))
        except X.ExpandError as e:
            assert "cover the whole base" in str(e)
            break
    assert len(stages) >= 1 and stages[0]["key"] == "x_roboports" and len(stages[0]["storage"]) == X.HUB_STORAGE
    assert all(not s["storage"] for s in stages[1:])
    first = stages[0]["roboports"][0]
    for c in stages[0]["storage"]:
        assert max(abs(c[0] - first[0]), abs(c[1] - first[1])) <= 4
    hub = mp.hub
    assert abs(first[0] - hub[0]) + abs(first[1] - hub[1]) <= 2 * X.HUB_R
    for s in stages:
        assert len(s["roboports"]) <= X.ROBOPORTS_PER_STAGE
        assert M.validate_stage(mp, gd, w, s["stage"]).ok
        assert mp.stages[[t.index for t in mp.stages].index(s["stage"])].gate == ["construction-robotics"]
    assert not M.check_monotone(mp, gd)
    ports = X.roboports_of(mp)
    assert _linked(ports, 2 * X.ROBOPORT_LOGISTIC_R - X.ROBOPORT_LINK)
    cov = X.roboport_coverage(mp, gd)
    assert cov["uncovered"] == 0 and cov["n"] == len(ports)
    # every mall chest (the mall's and the robot mall's) inside the network's logistic area: they become the passive
    # providers the robots take from; the first roboport stands by the mall
    assert cov["mall_out"] == 0
    malls = [e for e in mp.ents if (e.extra or {}).get("role") == "mall_chest"]
    assert any(max(abs(c.x - first[0]), abs(c.y - first[1])) <= X.ROBOPORT_LOGISTIC_R for c in malls)
    # no roboport on ore, all on free ground of the master (no overlap: monotone above)
    for e in mp.ents:
        if e.name == "roboport":
            assert not any(w.resource_at(x, y) for x, y in gd.footprint(e.name, e.x, e.y, 0))


def test_roboport_coverage_grows_with_the_base():
    """R12 asks for more roboports while built-area points lie outside every construction area: a block the base adds
    120 tiles east of the network is such a point (repeatable as the base grows)."""
    w, gd = load_fixture(1001)

    class _MP:
        ents = [PE("roboport", 2.0, 2.0, 0), PE("assembling-machine-1", 30.5, 0.5, 0)]
    cov = X.roboport_coverage(_MP, gd)
    assert cov["n"] == 1 and cov["points"] >= 2 and cov["uncovered"] == 0
    _MP.ents = _MP.ents + [PE("assembling-machine-1", 122.5, 4.5, 0)]
    grown = X.roboport_coverage(_MP, gd)
    assert grown["points"] == cov["points"] + 1 and grown["uncovered"] == 1   # (its 3x3 is in one 8x8 cell)
    # (the map's roboports count too: one the player built covers the new block)
    w2 = copy.copy(w)
    from factorio_claude.planner.world import MapEntity
    w2.entities = [MapEntity("roboport", 110.0, 2.0, 0, {}, None)]
    assert X.roboport_coverage(_MP, gd, w2)["uncovered"] == 0


def test_a_mall_chest_the_robots_upgraded_is_still_the_masters():
    """autopilot_logistic_chests turns mall chests into passive providers on the map: the planner counts such a chest
    as the master's own (world_without_master), so later stages still validate."""
    w, gd, mp, info = _robot_base()
    chest = next(e for e in mp.ents if (e.extra or {}).get("role") == "mall_chest" and e.name == "iron-chest")
    from factorio_claude.planner.world import MapEntity
    w2 = copy.copy(w)
    w2.entities = list(w.entities) + [MapEntity("passive-provider-chest", chest.x, chest.y, 0, {}, None)]
    w2._chunk_ent = {}
    kept = M.world_without_master(w2, mp)
    assert not any(e.name == "passive-provider-chest" for e in kept.entities)
