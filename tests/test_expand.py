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
