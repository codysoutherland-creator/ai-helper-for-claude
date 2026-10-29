"""Planner v2 (docs/design/PLANNER-v2.md): cost/ETA model, state, existing-build clusters, on-site blocks, quality
gates, variants, rebuild decisions and summaries, offline on the fixture maps (seed_4004 = the sprawl map)."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "testenv"))

from factorio_claude.planner import blocks as B  # noqa: E402
from factorio_claude.planner import cost, existing, quality, staged  # noqa: E402
from factorio_claude.planner.gamedata import rotate  # noqa: E402
from factorio_claude.planner.plan import Plan, world_without  # noqa: E402
from factorio_claude.planner.state import BaseState  # noqa: E402
from factorio_claude.planner.testing import fixture_seeds, load_fixture, load_state  # noqa: E402
from factorio_claude.planner.validate import validate  # noqa: E402

SPRAWL = fixture_seeds("sprawl")
need_sprawl = pytest.mark.skipif(not SPRAWL, reason="no sprawl fixture (testenv/make_fixtures.py sprawl 4004)")
PRIVATE = Path(__file__).resolve().parent.parent / "testenv" / "private" / "real_1028"
_cache: dict = {}


def gd0():
    return load_fixture(fixture_seeds()[0])[1]


def options(seed, goal=None, state="fixture", n=4):
    key = (seed, goal, state, n)
    if key not in _cache:
        world, gd = load_fixture(seed)
        st = load_state(seed) if state == "fixture" else state
        _cache[key] = staged.plan_options(world, gd, st, goal, variants=n)
    return _cache[key]


# ---- cost ------------------------------------------------------------------------------------------------------
def test_constants_pinned():
    assert (cost.WALK, cost.PLACE_S, cost.H_DEFAULT, cost.PLATE_COAL) == (8.9, 0.4, 8.5, 0.072)
    assert cost.PRIORITY == {"iron-ore": 1.5, "copper-ore": 1.0, "coal": 0.8, "stone": 0.3}
    assert cost.fe_equiv({"iron-ore": 10, "copper-ore": 10, "stone": 10, "coal": 10}) == 23.0


def test_bill_items_first_and_start_fuel():
    gd = gd0()
    b = cost.bill({"burner-mining-drill": 2, "stone-furnace": 1, "transport-belt": 3, "boiler": 1}, gd)
    assert b["burner-mining-drill"] == 2 and b["transport-belt"] == 3
    assert b["coal"] == 3 * cost.START_FUEL + cost.BOILER_FUEL      # burners 5 each, boiler 20, belts none
    c = cost.cover({"burner-mining-drill": 2}, {"burner-mining-drill": 1, "iron-plate": 100, "stone": 100}, gd)
    assert c.ok and c.used["burner-mining-drill"] == 1                # the drill in stock first, one crafted
    assert c.used["iron-plate"] == 9 and c.used["stone"] == 5 and c.craft_s > 0


def test_cover_expands_to_raw():
    gd = gd0()
    c = cost.cover({"burner-mining-drill": 1}, {}, gd)
    assert c.short["iron-ore"] == 9 and c.short["stone"] == 5
    assert c.short["coal"] == pytest.approx(9 * cost.PLATE_COAL)
    assert cost.raw_of({"stone-brick": 10}, gd)["stone"] == 20


def test_simulate_hand_checked():
    """Two steps from an empty stock: the helper gathers 8.5 raw/min; the first needs 9 iron + 5 stone + 5 coal."""
    gd = gd0()
    s1 = cost.Step("one drill", {"burner-mining-drill": 1, "coal": 5}, (0, 0), 1, {"iron-plate": 15}, {"coal": 2.25})
    e = cost.simulate([s1], {}, gd, H=8.5, start=(0, 0), target={"iron-plate": 15})
    # gather ~19.6 raw units (9 + 5 + 5 + 0.65) at 8.5/min -> ~2.5 min, + 4 s craft + 0.5 min warm-up
    assert 2.5 <= e["first_plates"] <= 4.0, e
    assert e["target"] is not None and e["target"] >= e["first_plates"]
    with_stock = cost.simulate([s1], {"burner-mining-drill": 1, "coal": 50}, gd, H=8.5, start=(0, 0))
    assert with_stock["first_plates"] <= 1.0
    never = cost.simulate([cost.Step("x", {"space-science-pack": 1}, None, 1, {"iron-plate": 1})], {}, gd)
    assert never["first_plates"] is None and never["complete"] is None


def _agenda(stock, coal_at=(0.0, 0.0), order=(1, 0)):
    """One burner pair 100 tiles east of a coal drill; all items in stock; coal mined at `coal_at` (None: nowhere)."""
    from factorio_claude.planner import agenda as AG
    items_pair = {"burner-mining-drill": 1, "stone-furnace": 1, "burner-inserter": 1, "wooden-chest": 1}
    items_coal = {"burner-mining-drill": 1, "wooden-chest": 1}
    units = [AG.Unit("coal 1", (0.0, 0.0), "coal", "coal", 15.0, 2.25, 50, 8, 1, 0),
             AG.Unit("iron 1", (100.0, 0.0), "pair", "iron-plate", 15.0, 3.4, 85, 14, 3, 1)]
    sites = [AG.Site("coal drills", (0.0, 0.0), items_coal, 2, [0]), AG.Site("iron pairs", (100.0, 0.0), items_pair, 4, [1])]
    s = dict(stock)
    for k, v in list(items_pair.items()) + list(items_coal.items()):
        s[k] = s.get(k, 0) + v
    return AG.Agenda(stock=s, start=(50.0, 0.0), sites=sites, units=units, target={"iron-plate": 15.0},
                     patch=(lambda raw, near: coal_at if raw == "coal" else None), centre=(50.0, 0.0),
                     order=list(order))


def test_agenda_coal_has_to_be_carried():
    """The ETA model counts coal only once the helper has carried it into a burner: from nothing it walks to the coal
    patch and mines the start fuel first; with coal in hand the first plate comes sooner; with no coal anywhere there
    is none. The round keeps the pair running from the coal drill's chest (hand-mining only starter amounts)."""
    from factorio_claude.planner import agenda as AG
    gd = gd0()
    none = AG.simulate(_agenda({}), gd)
    hand = AG.simulate(_agenda({"coal": 200}), gd)
    assert none["first_plates"] is not None and hand["first_plates"] is not None
    # 14 start coal mined by hand (2 s each) + the walks: noticeably later than with coal in the inventory
    assert none["first_plates"] >= hand["first_plates"] + 14 * AG.MINE_S["coal"] / 60
    assert none["sustained"] is not None and none["sustained"] >= 0.9
    assert none["gathered"].get("coal", 0) < 85 + 50         # never mined a full fill of everything by hand
    assert none["trips"] >= 1
    nowhere = AG.simulate(_agenda({}, coal_at=None), gd)
    assert nowhere["first_plates"] is None


def test_agenda_build_order_matters():
    """Plates first vs coal drill first: both are simulated; with the coal drill first the pair waits for it."""
    from factorio_claude.planner import agenda as AG
    gd = gd0()
    def ag(order):
        a = _agenda({"coal": 30, "iron-plate": 20}, order=order)
        a.stock["burner-mining-drill"] -= 1           # the coal drill is crafted first: stone mined at the coal patch
        a.patch = lambda raw, near: (0.0, 0.0) if raw in ("coal", "stone") else None
        return a
    plates_first = AG.simulate(ag((1, 0)), gd)
    coal_first = AG.simulate(ag((0, 1)), gd)
    assert plates_first["first_plates"] < coal_first["first_plates"]
    assert plates_first["timeline"][0][1].startswith("build iron")


def test_helper_rate_measured_or_default():
    assert cost.helper_rate(None) == cost.H_DEFAULT
    assert cost.helper_rate({"mined": {"iron-ore": 10}, "mining_ticks": 600}) == cost.H_DEFAULT  # < 3 min of samples
    assert cost.helper_rate({"mined": {"iron-ore": 100}, "mining_ticks": 3600 * 4, "walking_ticks": 3600}) == 20.0


# ---- state -----------------------------------------------------------------------------------------------------
def test_state_from_base_state_and_roundtrip(tmp_path):
    d = {"tick": 5, "surface": "nauvis", "player": {"pos": {"x": 1, "y": 2}, "main": {"iron-plate": 10}},
         "helper": {"pos": {"x": 5, "y": 5}, "main": {"coal": 4}, "stats": {"mined": {"coal": 4}}},
         "chests": [{"name": "wooden-chest", "x": 3.5, "y": 2.5, "contents": {"iron-plate": 7}},
                    {"name": "wooden-chest", "x": 300.5, "y": 2.5, "contents": {"iron-plate": 1000}}],
         "machines": [{"name": "stone-furnace", "type": "furnace", "x": 2, "y": 4, "status": "no_fuel",
                       "output": {"iron-plate": 3}}],
         "production": {"per_min": {"iron-plate": 11.8}}, "research": {"done": ["steam-power"]}}
    st = BaseState.from_dict(d)
    assert st.player_pos == (1.0, 2.0) and st.helper_pos == (5.0, 5.0)
    s = st.stock()
    assert s["iron-plate"] == 10 + 7 + 3 and s["coal"] == 4     # far chest not counted, furnace output counted
    st.save(tmp_path / "state.json")
    st2 = BaseState.load(tmp_path / "state.json")
    assert st2.stock() == s and st2.research_done == ["steam-power"]
    fresh = BaseState.fresh_start()
    assert fresh.fresh and fresh.stock()["iron-plate"] == 8


def test_state_player_items_only_when_the_helper_may_take_them():
    d = {"player": {"pos": {"x": 0, "y": 0}, "main": {"stone": 50}, "connected": False},
         "helper": {"pos": {"x": 1, "y": 1}, "main": {"coal": 3}}}
    assert BaseState.from_dict(d).stock() == {"coal": 3}             # offline player: agent.lua take_player can't
    d["player"]["connected"] = True
    assert BaseState.from_dict(d).stock() == {"stone": 50, "coal": 3}
    d["settings"] = {"use_player_items": False}
    assert BaseState.from_dict(d).stock() == {"coal": 3}             # the mod setting is off


def test_state_from_link_falls_back_to_old_ops():
    def call(op, **a):
        if op == "player":
            return {"pos": {"x": 3, "y": 4}, "inventory": {"stone": 5}, "surface": "nauvis"}
        if op == "agent_status":
            return {"alive": True, "pos": {"x": 0, "y": 0}, "inventory": {"coal": 2}}
        raise RuntimeError("unknown op " + op)
    st = BaseState.from_link(call, 0, 0)
    assert st.source == "ops" and st.stock() == {"stone": 5, "coal": 2}


# ---- on-site blocks ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("side", ["north", "east", "south", "west"])
def test_burner_pair_row_geometry(side):
    gd = gd0()
    b = B.burner_pair_row(3, gd, output_side=side)
    ents = b.entities
    by = {}
    for e in ents:
        for t in gd.footprint(e.name, e.x, e.y, e.direction):
            assert t not in by, "overlap"
            by[t] = e
    drills = [e for e in ents if e.name == "burner-mining-drill"]
    assert len(drills) == 3 and all((e.extra or {}).get("fuel") == "manual" for e in ents if "chest" not in e.name)
    for d in drills:  # every drill drops into a furnace
        vx, vy = rotate(*gd.entity(d.name)["vector_to_place_result"], d.direction)
        assert by[(math.floor(d.x + vx), math.floor(d.y + vy))].name == "stone-furnace"
    for i in (e for e in ents if e.name == "burner-inserter"):  # every inserter: furnace -> chest
        px, py = rotate(*gd.entity(i.name)["inserter"]["pickup"], i.direction)
        dx, dy = rotate(*gd.entity(i.name)["inserter"]["drop"], i.direction)
        assert by[(math.floor(i.x + px), math.floor(i.y + py))].name == "stone-furnace"
        assert by[(math.floor(i.x + dx), math.floor(i.y + dy))].name == "wooden-chest"
    assert b.rates["iron-plate"] == pytest.approx(45.0) and b.rates["coal"] < 0


def test_drill_to_chest_and_validation():
    gd = gd0()
    from validator_util import make_grid
    for res in ("coal", "stone"):
        b = B.drill_to_chest(2, gd, resource=res)
        ore = [(res, x, y, 5000) for e in b.entities if e.name == "burner-mining-drill"
               for x in range(int(e.x) - 1, int(e.x) + 1) for y in range(int(e.y) - 1, int(e.y) + 1)]
        rep = validate(b.entities, gd, make_grid(ore), {})
        assert rep.ok, rep.summary()
    assert B.drill_to_chest(2, gd, resource="coal").rates["coal"] == pytest.approx(30 - 4.5)


# ---- existing builds ---------------------------------------------------------------------------------------------
@need_sprawl
def test_clusters_on_sprawl_fixture():
    world, gd = load_fixture(SPRAWL[0])
    cl = existing.clusters(world, gd, load_state(SPRAWL[0]))
    kinds = {c.kind: c for c in cl}
    fr = kinds["furnace_row"]
    assert fr.counts == {"burner-inserter": 14, "stone-furnace": 14} and fr.status.startswith("idle")
    assert kinds["labs"].status == "idle_no_power" and kinds["drill_line"].counts == {"burner-mining-drill": 8}
    belts = [c for c in cl if c.kind == "belt_line"]
    assert max(c.belt_tiles for c in belts) >= 60          # the long belt from nowhere
    assert fr.items(gd)["stone-furnace"] == 14 and fr.removal_s() > 0
    assert all(not e.name.startswith("ghost:") for c in cl for e in c.ents)
    assert existing.summarize(cl, gd)["idle"]


def test_salvage_without_contents_and_fuel_takes():
    """Fuel inside an idle machine can be taken out without removing it: it never counts towards a removal, and S0
    takes it only from idle machines (never a working one), nearest the helper first, up to the need."""
    E = existing.Ent
    idle = E("stone-furnace", "furnace", 1, 1, 0, (0, 0, 2, 2), unit=11, status="idle_no_input",
             items={"coal": 30}, fuel={"coal": 30})
    busy = E("stone-furnace", "furnace", 5, 1, 0, (4, 0, 6, 2), unit=12, status="working", items={"coal": 20},
             fuel={"coal": 20})
    far = E("stone-furnace", "furnace", 90, 1, 0, (89, 0, 91, 2), unit=13, status="idle_no_input",
            items={"coal": 40}, fuel={"coal": 40})
    c = existing.Cluster(0, "furnace_row", [idle, busy])
    c2 = existing.Cluster(1, "furnace_row", [far])
    gd = gd0()
    assert c.items(gd, contents=False) == {"stone-furnace": 2} and c.items(gd)["coal"] == 50
    world, gd = load_fixture(fixture_seeds()[0])
    pl = staged.PlannerV2(world, gd, BaseState(helper_pos=(0.0, 0.0), helper_inv={}))
    pl.clusters = [c, c2]
    rows = pl.takes(need=25)
    assert [r[3] for r in rows] == [11] and rows[0][4:6] == ["coal", 30]
    assert [r[3] for r in pl.takes(need=60)] == [11, 13]
    assert pl.takes(removing_ids={0, 1}, need=60) == []


def test_preview_args_carry_the_helper_work():
    p = Plan("qui-boot-1", "Quick start", "quick", "bootstrap", "x", [], stats={
        "eta": {"first_plates": 2}, "takes": [["stone-furnace", 1, 2, 7, "coal", 10, 3]], "coal_from": [[5.5, 6.5]],
        "build_order": [[0, 0, 8, 6], [20, 0, 24, 3]], "refuel_existing": [["burner-mining-drill", 3, 4, 9, 50]]})
    a = p.preview_args()
    assert a["takes"] == [["stone-furnace", 1, 2, 7, "coal", 10]] and a["coal_from"] == [[5.5, 6.5]]
    assert a["build_order"] == [[0, 0, 8, 6], [20, 0, 24, 3]] and a["refuel_existing"][0][4] == 50


def test_groups_join_rows_of_one_block():
    E = existing.Ent
    a = existing.Cluster(0, "furnace_row", [E("stone-furnace", "furnace", 1, 1, 0, (0, 0, 2, 2))])
    b = existing.Cluster(1, "furnace_row", [E("stone-furnace", "furnace", 1, 8, 0, (0, 7, 2, 9))])
    c = existing.Cluster(2, "furnace_row", [E("stone-furnace", "furnace", 60, 1, 0, (59, 0, 61, 2))])
    g = existing.groups([a, b, c], gap=10)
    assert len(g) == 1 and sorted(g[0].members) == [0, 1] and g[0].id >= existing.GROUP_BASE
    assert g[0].rows()[0][4] == g[0].id


# ---- quality gates -----------------------------------------------------------------------------------------------
def _plan(**stats):
    return Plan("t", "t", "quick", "bootstrap", "", [], stats=stats)


def test_quality_gates():
    codes = lambda p, **k: {(i.code, i.level) for i in quality.assess(p, **k)}  # noqa: E731
    assert codes(_plan(eta={"first_plates": 5})) == set()
    assert ("SLOW_START", "soft") in codes(_plan(eta={"first_plates": 20}))
    assert ("FIRST_STAGE_TOO_BIG", "hard") in codes(_plan(eta={"first_plates": 50}))
    assert ("FIRST_STAGE_TOO_BIG", "hard") in codes(_plan(eta={"first_plates": None}))
    assert ("LONG_BELT", "soft") in codes(_plan(longest_run=40))
    assert ("LONG_BELT", "hard") in codes(_plan(longest_run=70))
    assert ("LONG_BELT", "soft") in codes(_plan(longest_run=120, **{"class": "longterm"}))
    assert ("SMELTING_FAR_FROM_IRON", "hard") in codes(_plan(smelt_dist=190))
    assert ("BILL_UNREACHABLE", "soft") in codes(_plan(bill={"short": {"iron-ore": 1800}}))
    assert ("WATER_IGNORED", "soft") in codes(_plan(water={"near": 30, "plant": 120}))
    p = _plan(clusters=[{"id": 3, "action": "replace", "reason": ""}])
    p.removes = [["stone-furnace", 1, 1, 7, 3]]
    assert ("REMOVAL_UNJUSTIFIED", "hard") in codes(p)
    p.stats["clusters"][0]["reason"] = "far and idle"
    assert ("REMOVAL_UNJUSTIFIED", "hard") not in codes(p)
    assert ("REMOVES_TOO_MUCH", "hard") in codes(p, base_entities=1)
    assert ("REMOVES_TOO_MUCH", "hard") not in codes(p, base_entities=1, rebuild_words=True)
    # the coal round: can't keep up / keeps the helper busy; biters next to a site or a skipped site
    assert ("FUEL_SHORT", "soft") in codes(_plan(eta={"first_plates": 5, "target": 9, "sustained": 0.8}))
    assert ("FUEL_SHORT", "hard") in codes(_plan(eta={"first_plates": 5, "target": 9, "sustained": 0.4}))
    assert ("FUEL_SHORT", "soft") in codes(_plan(eta={"first_plates": 5, "target": None}, target={"iron-plate": 60}))
    assert ("HELPER_BUSY", "soft") in codes(_plan(round={"helper_share": 0.8}))
    assert ("NESTS_NEAR", "soft") in codes(_plan(nest_sites=[{"what": "stone drills", "nest": 20, "at": [1, 2]}]))
    assert ("NESTS_NEAR", "soft") in codes(_plan(skipped=["the stone patch has biter nests"]))


def test_score_hand_checked():
    p = _plan(eta={"first_plates": 4, "target": 10}, belt_tiles=50, stage2_min=12, gates=[
        {"code": "SLOW_START", "level": "soft", "text": ""}])
    p.removes = [["stone-furnace", 0, 0, 1, 1], ["transport-belt", 0, 0, 2, 1]]
    # 4 + 10 + 0.02*50 + 0.5*12 + 5*1 soft + 0.05 furnace + 0.01 belt
    assert staged.score(p) == round(4 + 10 + 1 + 6 + 5 + 0.05 + 0.01, 1)
    assert staged.score(_plan(eta={"first_plates": None, "target": None})) == 2 * staged.NONE_MIN


# ---- variants on the fresh fixtures ------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", fixture_seeds())
def test_fresh_maps_quick_and_balanced(seed):
    world, gd = load_fixture(seed)
    plans = options(seed, None, None, 2)
    assert [p.variant for p in plans] == ["quick", "balanced"] or {p.variant for p in plans} == {"quick", "balanced"}
    for p in plans:
        s = p.stats
        assert p.validation["ok"] and validate(p.entities, gd, world, {}).ok
        assert s["belt_tiles"] == 0 and s["longest_run"] == 0             # bootstrap: no ore belts at all
        assert s["eta"]["first_plates"] is not None and s["eta"]["first_plates"] <= 20
        assert s["build_now"] and not quality.hard([quality.Issue(**g) for g in s["gates"]])
        drills = [e for e in p.entities if e.name == "burner-mining-drill"]
        assert drills and all(world.resource_at(e.x, e.y) for e in drills)
        for k in ("First plates", "Needs", "Coal:", "Sites:", "Belts", "Removes", "Trade-off"):
            assert k in p.summary, (k, p.summary)
        # every site of the plan is on the card (balanced spans 3-4 sites, not just the iron one)
        for row in s["site_rows"]:
            assert f"[gps={round(row['at'][0])},{round(row['at'][1])}]" in p.summary
        # the mod gets the build order, the coal chests for its round, and the fuel model's numbers are sane
        a = p.preview_args()
        assert len(a["build_order"]) == len(s["site_rows"]) and a["coal_from"]
        assert s["eta"]["sustained"] is None or s["eta"]["sustained"] >= 0.9
        assert s["eta"]["target"] is None or s["eta"]["target"] >= s["eta"]["first_plates"]
        b = p.brief()
        assert {"eta", "bill", "belts", "removes", "score", "gates", "tradeoff"} <= set(b)
    q = next(p for p in plans if p.variant == "quick")
    bal = next(p for p in plans if p.variant == "balanced")
    assert q.stats["units"]["iron-ore"] == 4 and "copper-plate" in bal.stats["per_min"]
    assert bal.stats["stages"][1]["name"] == "power + research"


def test_aliases_and_keys():
    assert staged.ALIASES["compact"] == "quick" and staged.ALIASES["expandable"] == "longterm"
    world, gd = load_fixture(fixture_seeds()[0])
    plans = staged.plan_options(world, gd, None, None, keys=["compact"])
    assert [p.variant for p in plans] == ["quick"]


def test_plan_roundtrip_with_removes(tmp_path):
    p = Plan("reb-boot-1", "Rebuild", "rebuild", "bootstrap", "Line one. Line two.", [],
             stats={"eta": {"first_plates": 3}, "belt_tiles": 0}, removes=[["stone-furnace", 1, 1, 42, 3]])
    q = Plan.load(p.save(tmp_path))
    assert q.removes == [["stone-furnace", 1, 1, 42, 3]]
    a = q.preview_args()
    assert a["removes"] == [["stone-furnace", 1, 1, 42]] and a["facts"]["removes"] == 1
    assert a["facts"]["first_plates_min"] == 3


# ---- sprawl map: geometry, gates, rebuild --------------------------------------------------------------------------
@need_sprawl
def test_sprawl_variants_quality_and_geometry():
    seed = SPRAWL[0]
    world, gd = load_fixture(seed)
    plans = options(seed, "starter base")
    now = [p for p in plans if p.stats["build_now"]]
    assert {p.variant for p in now} >= {"quick", "balanced"}
    iron = staged.PlannerV2(world, gd, load_state(seed)).iron
    for p in now:
        st = p.stats
        assert st["longest_run"] <= quality.BELT_CAPS["bootstrap"][0]
        assert not [g for g in st["gates"] if g["level"] == "hard"]
        assert p.validation["ok"]
        # iron smelting sits on the iron patch (its rows' centre within a few tiles of the ore)
        from factorio_claude.planner.onsite import dist_point_patch
        assert dist_point_patch(tuple(st["sites"]["iron-ore"]), iron)[1] <= 6
        assert st["smelt_dist"] == 0
        # nothing of the plan stands on an existing entity that it does not remove
        w = world_without(world, p.removes)
        occupied = set()
        for me in w.entities:
            occupied |= {(x, y) for x in range(me.box[0], me.box[2]) for y in range(me.box[1], me.box[3])}
        for e in p.entities:
            assert not (gd.footprint(e.name, e.x, e.y, e.direction) & occupied)
    lt = [p for p in plans if p.variant == "longterm"]
    if lt:  # the hub would need 400-tile ore belts: never offered (not even as a later stage); grow on site instead
        g = lt[0]
        # (patches 200-330 tiles apart: the hub is not even tried, its ore belts would exceed the hard cap)
        assert g.stats.get("grow") and "longer than 160 tiles" in g.stats["hub_check"]["why_not"][0]
        assert "central hub is not offered" in g.summary
        assert g.stats["units"]["iron-ore"] > next(p for p in plans if p.variant == "quick").stats["units"]["iron-ore"]
        assert all(not s.get("belt_tiles") for s in g.stats["stages"])     # no stage carries the hub's belts


@need_sprawl
def test_sprawl_rebuild_when_stock_is_low():
    """With nothing in stock, the idle burner drills in the middle are worth taking apart: the rebuild variant lists
    exactly their entities, says why, scores better than its keep version and stays within the removal caps."""
    seed = SPRAWL[0]
    world, gd = load_fixture(seed)
    st = load_state(seed)
    st.player_inv = {}
    plans = staged.plan_options(world, gd, st, "starter base", keys=["quick", "balanced", "rebuild"])
    reb = next((p for p in plans if p.variant == "rebuild"), None)
    assert reb is not None, [p.variant for p in plans]
    base = next(p for p in plans if p.variant == reb.stats["base_variant"])
    assert reb.stats["score"] < base.stats["score"]
    rs = reb.stats["removes_summary"]
    assert rs["clusters"] and all(c["reason"] for c in rs["clusters"])
    assert reb.stats["net_gain"]["eta_first_plates_min"] > 0
    cl = {c["id"]: c for c in reb.stats["clusters"]}
    for r in reb.removes:
        assert cl[r[4]]["action"] in ("replace", "reuse")
        assert any(e.name == r[0] and float(e.x) == r[1] and float(e.y) == r[2] for e in world.entities)
    # the card lists every removal with its reason (no "and 1 more"), and says what it is compared with
    assert f"Removes exactly {len(reb.removes)} of your entities" in reb.summary
    for c in rs["clusters"]:
        assert c["reason"] in reb.summary and c["gps"] in reb.summary
    assert "more, see the plan" not in reb.summary and "scores ~" not in reb.summary
    assert reb.validation["ok"]
    machines = sum(1 for e in world.entities if not e.name.startswith("ghost:") and "belt" not in e.name
                   and "pole" not in e.name)
    assert sum(1 for r in reb.removes if "belt" not in r[0]) <= 0.5 * machines
    # never offered when removals are switched off
    assert all(p.variant != "rebuild" for p in staged.plan_options(world, gd, st, None, allow_removal=False,
                                                                   keys=["quick", "balanced", "rebuild"]))


@need_sprawl
def test_idle_review_is_per_variant():
    """Clusters are shared between variants: a plan that removes nothing must never tell Claude (brief s0.idle_builds)
    that it replaces something just because the rebuild variant chose to."""
    seed = SPRAWL[0]
    world, gd = load_fixture(seed)
    st = load_state(seed)
    st.player_inv = {}
    plans = staged.plan_options(world, gd, st, "starter base", keys=["quick", "balanced", "rebuild", "longterm"])
    assert any(p.variant == "rebuild" for p in plans)
    for p in plans:
        if p.removes:
            continue
        for row in p.stats.get("idle_review") or []:
            assert row["action"] in ("keep", "reuse"), (p.variant, row)
        brief = p.brief() if hasattr(p, "brief") else None
        for row in ((brief or {}).get("s0") or {}).get("idle_builds") or []:
            assert row["action"] != "replace", (p.variant, row)


@need_sprawl
def test_sprawl_review_with_good_stock():
    """With the fixture's stock (100 plates) only the 8 idle burner drills off any patch are worth taking apart (their
    drills save crafting 72 iron for Balanced, and the iron patch is far); the idle furnace row, the labs and the long
    belt are kept. The keep cards point at the Rebuild card instead of contradicting it."""
    plans = options(SPRAWL[0], "starter base")
    reb = next((p for p in plans if p.variant == "rebuild"), None)
    q = next(p for p in plans if p.variant == "quick")
    assert q.stats["reviewed"] >= 1
    if reb is None:
        assert "checked" in q.summary.lower()
        return
    assert {r[0] for r in reb.removes} <= {"burner-mining-drill", "transport-belt"}
    assert "Rebuild" in q.summary and "would save only" not in q.summary
    assert reb.stats["net_gain"]["score_min"] >= staged.REMOVE_GAIN_MIN


@need_sprawl
def test_render_plan_png(tmp_path):
    from render_plan import render
    world, gd = load_fixture(SPRAWL[0])
    st = load_state(SPRAWL[0])
    st.player_inv = {}
    reb = next(p for p in staged.plan_options(world, gd, st, None, keys=["rebuild"]) if p.variant == "rebuild")
    out = tmp_path / "r.png"
    render(world, out, [reb], gd, rect=(-60, -40, 140, 140), scale=2)
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 1000
    render(world, tmp_path / "f.png", [reb], gd, rect=(-60.5, -40.2, 139.7, 140.1), scale=1)   # float rect
    assert (tmp_path / "f.png").read_bytes()[:4] == b"\x89PNG"


# ---- round 3: stage 2 placed, variants that differ, honest cards, reuse in place -----------------------------------
def test_steam_power_hand_fed_and_lab_row():
    gd = gd0()
    b = B.steam_power(1, 1, gd, feed="manual", water_side="west", pole="small-electric-pole")
    names = [e.name for e in b.entities]
    assert "transport-belt" not in names and "burner-inserter" not in names and names.count("steam-engine") == 1
    assert next(e for e in b.entities if e.name == "boiler").extra == {"fuel": "manual"}
    assert {q.kind for q in b.ports} == {"pipe_in", "pole"}
    belt_fed = B.steam_power(1, 1, gd, water_side="west", pole="small-electric-pole")
    assert b.size[1] < belt_fed.size[1]                         # no inserter row, no belt row
    lr = B.lab_row(2, gd)
    assert [e.name for e in lr.entities] == ["lab", "lab"] and lr.size == (7, 3) and lr.rates["power_kw"] == 120


def test_place_free_keeps_off_ore_and_obstacles():
    from factorio_claude.planner.onsite import place_free
    world, gd = load_fixture(fixture_seeds()[0])
    grid = world.grid((-60, -60, 60, 60))
    b = place_free(grid, gd, B.lab_row(2, gd), (-44, -34), radius=40)   # target: the iron patch of seed 1001
    assert b is not None
    for e in b.placed():
        for t in gd.footprint(e.name, e.x, e.y, e.direction):
            assert not grid.resource(*t) and grid.state_at(*t) != 2


def _stage2_cases():
    out = [(seed, None, 2) for seed in fixture_seeds()]
    return out + [(seed, "starter base", 4) for seed in SPRAWL]


@pytest.mark.parametrize("seed,goal,n", _stage2_cases())
def test_stage2_is_placed_on_top_of_stage1(seed, goal, n, tmp_path):
    """Stage 2 (power + research) is a placed, validated plan of its own: a hand-fed steam engine at the water, poles
    to the labs (the player's idle ones where they stand, else 2 new ones by the iron), copper pairs when stage 1 has
    none; it never conflicts with stage 1, keeps stage 1's burners in its coal round, and the card gives its bill and
    when red science research can start."""
    world, gd = load_fixture(seed)
    plans = options(seed, goal, None if goal is None else "fixture", n)
    G = B._geo(gd)
    for p in plans:
        s2 = p.next_stage
        assert s2 is not None, (p.variant, p.stats["stages"])
        st = s2.stats
        assert s2.id == p.id + "-s2" and st["parent"] == p.id and st["class"] == "stage2"
        gone = p.removes + [[e.name, e.x, e.y] for e in p.entities if (e.extra or {}).get("existing")]
        # (s2.expect(): entities a trigger tech still locks - stage 1's plates unlock them - count as researched)
        assert validate(p.entities + s2.entities, gd, world_without(world, gone), s2.expect()).ok
        names = {e.name for e in s2.entities}
        assert {"offshore-pump", "boiler", "steam-engine", "small-electric-pole"} <= names
        assert next(e for e in s2.entities if e.name == "boiler").extra == {"fuel": "manual"}
        assert not [e for e in s2.entities if "belt" in e.name]
        poles = [e for e in s2.entities if e.name == "small-electric-pole"]
        labs = ([B.PE("lab", e.x, e.y) for e in world.entities if e.name == "lab"] if st["labs"]["reused"]
                else [e for e in s2.entities if e.name == "lab"])
        assert labs and all(any(B.powered_by(G, q, lab) for q in poles) for lab in labs)
        if p.variant == "quick" and "copper-ore" not in p.stats.get("units", {}):
            assert st["copper_pairs"] >= 1 and "burner-mining-drill" in names
        e2 = st["eta"]
        assert e2["start"] <= e2["power_on"] <= e2["science_start"] <= e2["automation"]
        assert p.stats["science_min"] == e2["science_start"]
        assert "Stage 2 (placed and checked" in p.summary and "red science research from" in p.summary
        # red science's trigger (craft a lab) only counts the player's own crafting: the card says so when it is due
        if any(u["tech"] == "automation-science-pack" for u in st["research_steps"]):
            assert "craft one lab by hand" in p.summary
        # the mod side: stage 1's hand-fed burners stay in the coal round, stage 1's chests are its material source
        a = s2.preview_args()
        manual = [e for e in p.entities if (e.extra or {}).get("fuel") == "manual"]
        assert len(a["keep_fuelled"]) == len(manual) and a["coal_from"] and a["take_from"]
        assert len(p.preview_args()["reserve"]) == len(s2.entities)   # stage 1 keeps stage 2's ground free
        assert Plan.load(s2.save(tmp_path)).entities == s2.entities
        assert not [g for g in st["gates"] if g["level"] == "hard"]


@pytest.mark.parametrize("seed", fixture_seeds()[:1] + SPRAWL)
def test_variants_are_different_builds(seed):
    """R4: no two cards are the same build; the long-term card is either a hub that passes the gates (short belts,
    smelting by the iron) or twice the on-site smelting - never a hub with 150-400 tile ore belts."""
    plans = options(seed, "starter base")
    # (a Rebuild may place the same rows as its base and differ in what it takes apart for their parts)
    builds = {p.variant: (frozenset((e.name, e.x, e.y) for e in p.entities), frozenset(tuple(r[:3]) for r in p.removes))
              for p in plans}
    assert len(set(builds.values())) == len(builds), list(builds)
    lt = next((p for p in plans if p.variant == "longterm"), None)
    if lt is not None:
        if lt.stats.get("grow"):
            assert lt.stats["belt_tiles"] == 0 and lt.stats["units"]["iron-ore"] >= 8
        else:
            assert lt.stats["longest_run"] <= quality.BELT_CAPS["longterm"][1]
            assert lt.stats["smelt_dist"] <= quality.SMELT_FAR["longterm"]


@need_sprawl
def test_cards_say_what_keeps_the_burners_going():
    plans = options(SPRAWL[0], "starter base")
    for p in plans:
        fu = p.stats["bill"]["fuel"]
        assert 0 < fu["start"] < fu["first_fill"]
        assert f"Coal: {fu['start']} to start the burners" in p.summary
        assert "Without the helper the burners run dry" in p.summary      # it depends on the helper: said so
        if p.deconstruct:
            assert f"Clears {len(p.deconstruct)} trees/rocks" in p.summary
        assert p.stats["eta"]["target"] is None or p.stats["eta"]["target"] >= p.stats["eta"]["first_plates"]


@need_sprawl
def test_labs_are_never_removal_candidates():
    world, gd = load_fixture(SPRAWL[0])
    st = load_state(SPRAWL[0])
    st.player_inv = {}
    pl = staged.PlannerV2(world, gd, st, "tidy up my old stuff")
    spec = pl.spec("balanced")
    keep = pl.build(spec, with_s2=False)
    assert keep.next_stage is None
    cands = pl.candidates(spec, keep)
    assert cands and not [c for c in cands if any(e.type == "lab" for e in c.ents)]


@pytest.mark.skipif(not (PRIVATE / "state.json").exists(), reason="private real-save fixture not present")
def test_private_reuse_in_place():
    """The player's 6 burner drills on copper that drop onto a 12-tile belt: as a conversion only the belt is removed;
    the drills stay (listed as existing: no ghost, no bill) and get a furnace, inserter and chest in front."""
    from factorio_claude.planner.gamedata import GameData
    from factorio_claude.planner.world import World
    world, gd = World.load(PRIVATE / "map"), GameData.load(PRIVATE / "data.json")
    st = BaseState.load(PRIVATE / "state.json")
    pl = staged.PlannerV2(world, gd, st, "starter base, tidy up")
    convs = pl.conversions()
    cv = next(v for v in convs.values() if v.res == "copper-ore")
    assert cv.n == 6 and len(cv.belts) == 12 and {r[0] for r in cv.belts} == {"transport-belt"}
    p = pl.build(pl.spec("balanced"), converting=[cv])
    assert p.validation["ok"] and {r[0] for r in p.removes} == {"transport-belt"}
    old = [e for e in p.entities if (e.extra or {}).get("existing")]
    assert len(old) == 6 and all(e.name == "burner-mining-drill" for e in old)
    assert p.stats["units"]["copper-ore"] == 6 and p.stats["bill"]["need"].get("burner-mining-drill", 0) < 11
    furnaces = [e for e in p.entities if e.name == "stone-furnace"]
    for d in old:   # each drill drops into a planned furnace
        vx, vy = rotate(*gd.entity(d.name)["vector_to_place_result"], d.direction)
        px, py = math.floor(d.x + vx), math.floor(d.y + vy)
        assert any((px, py) in gd.footprint(f.name, f.x, f.y, f.direction) for f in furnaces)


# ---- private real-save copy (skipped when absent; never committed) ---------------------------------------------------
@pytest.mark.skipif(not (PRIVATE / "state.json").exists(), reason="private real-save fixture not present")
def test_private_real_copy():
    from factorio_claude.planner.gamedata import GameData
    from factorio_claude.planner.world import World
    world, gd = World.load(PRIVATE / "map"), GameData.load(PRIVATE / "data.json")
    st = BaseState.load(PRIVATE / "state.json")
    plans = staged.plan_options(world, gd, st, "starter base, tidy up my old stuff", variants=4)
    by = {p.variant: p for p in plans}
    for p in plans:
        if p.stats["build_now"]:
            assert p.stats["longest_run"] <= quality.BELT_CAPS["bootstrap"][1]
            assert p.stats["eta"]["first_plates"] is not None and "Needs" in p.summary
    assert by["quick"].stats["eta"]["first_plates"] <= 10
    assert "rebuild" in by and by["rebuild"].removes and by["rebuild"].stats["removes_summary"]["clusters"][0]["reason"]
    # labs are never taken apart for stage 1: stage 2 powers the player's 4 labs where they stand
    assert not [r for r in by["rebuild"].removes if r[0] == "lab"]
    for p in plans:
        s2 = p.next_stage
        assert s2 is not None and s2.stats["labs"]["reused"] == 4 and s2.stats["labs"]["new"] == 0
        assert s2.validation["ok"] and s2.stats["eta"]["science_start"] is not None
    # the drill line on copper that drops onto a belt was checked as a conversion where it stands, too
    assert any(r.get("action") == "reuse" for r in by["quick"].stats["review"]) or         any(c.get("action") == "reuse" for c in by["rebuild"].stats["removes_summary"]["clusters"])
    # the hub has 290-tile ore belts on this map: never offered; long-term = grow on site, a different build
    lt = by["longterm"]
    assert lt.stats.get("grow") and lt.stats["hub_check"]["longest_run"] > 160
    assert len(lt.entities) > len(by["quick"].entities) and lt.stats["belt_tiles"] == 0
    # S0: coal out of the idle machines, never out of a working one
    takes = by["quick"].stats["takes"]
    assert takes and all(r[4] in ("coal", "wood") for r in takes)
    working = {(m["name"], m["x"], m["y"]) for m in st.machines if m.get("status") == "working"}
    assert not {(r[0], r[1], r[2]) for r in takes} & working
    json.dumps([p.brief() for p in plans])
