"""Planner v3 (docs/design/PLANNER-v3.md): blocks, the master plan's stage invariants, research gating, cards.
Offline: fixture maps only (the private real-copy test is skipped when its capture is absent)."""
import copy
import math
from functools import lru_cache
from pathlib import Path

import pytest

from factorio_claude.planner import blocks as B
from factorio_claude.planner import master as M
from factorio_claude.planner.blocks import PE
from factorio_claude.planner.gamedata import rotate
from factorio_claude.planner.state import BaseState
from factorio_claude.planner.testing import load_fixture, load_state
from factorio_claude.planner.validate import validate
from factorio_claude.planner.world import MapEntity, World

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "testenv" / "private" / "v3r2" / "real"


def gd1001():
    return load_fixture(1001)[1]


@lru_cache(maxsize=None)
def master(seed, variant):
    w, gd = load_fixture(seed)
    st = load_state(seed) or BaseState.fresh_start()
    return M.design_master(w, gd, st, {"near": st.pos or (0, 0)}, variant)


CASES = [(2002, "hub"), (2002, "onsite"), (3003, "onsite"), (4004, "hub")]


# ---- validator regression (PLANNER-v3 7.3) ---------------------------------------------------------------------------
def _world_with(ents):
    w = World("nauvis", 0, "t", -32, -32, 64, 64)
    w.known[:] = True
    for e in ents:
        cx, cy = math.floor(e.x) // 32, math.floor(e.y) // 32
        w._chunk_ent.setdefault((cx, cy), []).append(len(w.entities))
        w.entities.append(e)
    return w


def test_existing_inserter_dropping_on_a_planned_pole_is_no_link():
    """An existing burner inserter whose drop point is a planned small pole moves nothing into the plan (the real
    copy lost every stage 2 to this false positive); a planned chest there still is an existing_link."""
    gd = gd1001()
    ins = MapEntity("burner-inserter", 0.5, 0.5, 4, {}, (0, 0, 1, 1))   # picks from the east, drops west on (-1, 0)
    w = _world_with([ins])
    rep = validate([PE("small-electric-pole", -0.5, 0.5)], gd, w, {})
    assert not [i for i in rep.errors if i.kind == "existing_link"], rep.summary()
    rep = validate([PE("wooden-chest", -0.5, 0.5)], gd, w, {})
    assert [i for i in rep.errors if i.kind == "existing_link"], rep.summary()


# ---- blocks (section 6) ------------------------------------------------------------------------------------------------
def _drop(gd, e):
    vx, vy = rotate(*gd.entity(e.name)["vector_to_place_result"], e.direction)
    return e.x + vx, e.y + vy


def test_mining_field_burner_slot_matches_the_electric_slot():
    """M2: in every east slot the burner drill and its burner inserter lie inside the 3x3 the electric drill takes
    later, the burner drill drops on the same belt column and lane as the electric drill (prototype drop vectors),
    the inserter picks from the corridor; poles sit on the gap rows, 7 apart (wire reach 7.5)."""
    gd = gd1001()
    blk = B.mining_field(8, gd, resource="iron-ore")
    by = {}
    for e in blk.entities:
        x = e.extra or {}
        by.setdefault((x.get("role"), x.get("unit")), []).append(e)
    for k in (1, 3, 5, 7):
        db, ib, de = by[("drill_b", k)][0], by[("ins_b", k)][0], by[("drill_e", k)][0]
        slot = gd.footprint(de.name, de.x, de.y, de.direction)
        assert gd.footprint(db.name, db.x, db.y, db.direction) | gd.footprint(ib.name, ib.x, ib.y, ib.direction) <= slot
        bx, ex = _drop(gd, db)[0], _drop(gd, de)[0]
        assert math.floor(bx) == math.floor(ex) == B.FIELD_BELT_X
        assert (bx - B.FIELD_BELT_X >= 0.5) == (ex - B.FIELD_BELT_X >= 0.5)     # same lane
        assert ib.direction == 4 and ib.x + 1 == 8.5                           # picks from the corridor at x=8
    for k in (0, 2, 4, 6):
        assert ("drill_b", k) not in by                                        # west slots: electric only
    poles = sorted(e.y for e in blk.entities if (e.extra or {}).get("role") == "field_pole")
    assert all(b - a == B.FIELD_PERIOD for a, b in zip(poles, poles[1:])) and B.FIELD_PERIOD <= 7.5
    kinds = [p.kind for p in blk.ports]
    assert kinds == ["belt_out", "belt_in", "belt_out"]


def test_mining_field_burner_phase_validates():
    """The burner phase of a field (east burner slots, corridor fed with coal at the tail) validates: every burner
    drill has a fuel path through its burner inserter."""
    gd = gd1001()
    blk = B.mining_field(4, gd, resource="iron-ore")
    ents = [e for e in blk.entities if (e.extra or {}).get("role") in ("drill_b", "ins_b", "ore_belt", "corr")]
    res = {(x, y): "iron-ore" for x in range(-1, 10) for y in range(-1, blk.size[1] + 1)}
    from validator_util import make_grid
    g = make_grid([("iron-ore", x, y) for (x, y) in res], rect=(-20, -20, 30, 40))
    ore_out, coal_in, coal_out = blk.ports
    rep = validate(ents, gd, g, {"ports": [ore_out, coal_out, B.Port("belt_in", "coal", coal_in.x, coal_in.y, 0)]})
    assert rep.ok, rep.summary(12)
    assert not [i for i in rep.errors + rep.warnings if i.kind == "fuel"]


def test_smelter_column_merge_lanes_and_first_pairs():
    """The column's merge puts ore on the left lane, coal on the right; built bottom-first, the first two pairs
    validate with burner input inserters fed from the middle belt and buffer chests for the plates (the burner
    inserters are hand-fed: one that moves ore refuels only when its furnace asks for coal). Every input and
    output inserter tile also holds the electric inserter that replaces it in place, and a pole of the in-inserter
    column powers the input inserters of both sides."""
    gd = gd1001()
    blk = B.smelter_column(3, gd, ore="iron-ore")
    keep = [e for e in blk.entities if (e.extra or {}).get("role") in ("in_b", "furnace", "out_b", "buffer")
            and (e.extra or {}).get("unit") in (1, 2)]
    keep += [e for e in blk.entities if (e.extra or {}).get("role") == "merge" or
             ((e.extra or {}).get("role") == "mid" and (e.extra or {}).get("row") >= 2)]
    for e in keep:
        if (e.extra or {}).get("role") in ("out_b", "in_b"):
            e.extra = dict(e.extra, fuel="manual")
    ore, coal = blk.ports[2], blk.ports[3]
    rep = validate(keep, gd, None, {"sources": [ore, coal]})
    assert rep.ok, rep.summary(12)
    assert ore.lane == "left" and coal.lane == "right"
    by = {}
    for e in blk.entities:
        x = e.extra or {}
        by.setdefault((x.get("role"), x.get("unit"), x.get("side")), []).append(e)
    for i in range(3):
        for side in ("W", "E"):
            for b, el in (("in_b", "in_e"), ("out_b", "out_e")):
                eb, ee = by[(b, i, side)][0], by[(el, i, side)][0]
                assert (eb.name, ee.name) == ("burner-inserter", "inserter")
                assert (eb.x, eb.y, eb.direction) == (ee.x, ee.y, ee.direction)
    centre = [e for e in blk.entities if (e.extra or {}).get("role") == "col_pole" and (e.extra or {}).get("side") == "C"]
    ins_e = [e for e in blk.entities if (e.extra or {}).get("role") == "in_e"]
    assert all(any(B.powered_by(gd, p, e) for p in centre) for e in ins_e)


def test_power_block_ratio_and_blackstart_geometry():
    """1 pump : 20 boilers : 40 engines from the prototypes (PLANNER-v2 said 1:200:400); the block feeds every
    boiler with a burner inserter from the coal belt (no electric inserter: it black-starts)."""
    gd = gd1001()
    pump = gd.entity("offshore-pump")["pumping_speed_per_s"]
    boiler_steam = gd.entity("boiler")["max_energy_usage_kw"] * 1000 / (200 * (165 - 15))   # water/s per boiler
    engine = gd.entity("steam-engine")["fluid_usage_per_s"]
    assert round(pump / boiler_steam) == 20 and round(boiler_steam / engine) == 2
    blk = B.power_block(4, gd)
    names = {e.name for e in blk.entities}
    assert "inserter" not in names and "burner-inserter" in names
    assert sum(1 for e in blk.entities if e.name == "steam-engine") == 8
    for coal_end in ("water", "far"):
        b = B.power_block(2, gd, coal_end=coal_end)
        assert b.ports[1].kind == "belt_in"
        m = B.mirror_y(b)
        assert {e.direction for e in m.entities if e.name == "boiler"} == {8}


def test_red_block_rate():
    """5 red assemblers (AM1, speed 0.5, 5 s) make 0.5 packs/s; one gear AM1 (1 gear/s) covers them; the gear AM
    has two input inserters (an inserter moves ~0.83/s, the gear AM needs 1 iron/s)."""
    gd = gd1001()
    am = gd.entity("assembling-machine-1")["crafting_speed"]
    red = gd.recipe("automation-science-pack")["energy"]
    assert M.N_RED * am / red == pytest.approx(M.TARGET_RED)
    blk = B.science_block(gd, n_red_res=M.N_RED_RES)
    assert sum(1 for e in blk.entities if (e.extra or {}).get("role") == "gear_in") * \
        gd.inserter_throughput("inserter") >= 2 * M.N_RED * am / red


# ---- the master plan (sections 4, 5, 7) -----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed,variant", CASES)
def test_master_every_stage_validates_and_is_monotone(seed, variant):
    """B1: every stage of the master plan validates as a closed base, nothing of an early stage stands where a later
    one builds (except the planned swaps), every removal is a whitelisted swap."""
    w, gd = load_fixture(seed)
    mp = master(seed, variant)
    chk = M.check_all(mp, gd, w)
    assert not chk["monotone"], chk["monotone"][:3]
    for k, st in chk["stages"].items():
        assert st["ok"], (k, st["errors"][:3])
        if k >= 2:
            assert st["selfrun"]["ok"] and not st["selfrun"]["needs_helper"], (k, st["selfrun"]["issues"])


@pytest.mark.parametrize("seed,variant", CASES[:2])
def test_stages_are_subsets_plus_planned_swaps(seed, variant):
    """stage_entities(k) = stage_entities(j) for j < k, minus the entities a swap of a stage in (j, k] removes,
    plus new_in for the stages in between."""
    mp = master(seed, variant)
    idx = [s.index for s in mp.stages]
    key = lambda e: (e.name, e.x, e.y, e.direction)   # noqa: E731
    for j, k in zip(idx, idx[1:]):
        a = {key(e) for e in M.stage_entities(mp, j)}
        b = {key(e) for e in M.stage_entities(mp, k)}
        gone = {key(e) for e in M.removed_in(mp, k)}
        new = {key(e) for e in M.new_in(mp, k)}
        assert b == (a - gone) | new
        assert gone <= a


@pytest.mark.parametrize("seed,variant", CASES[:2])
def test_swap_pairs_and_counts(seed, variant):
    """Every upgrade swap takes out exactly a burner drill + its burner inserter and puts an electric drill whose
    3x3 covers both, in S4; replace swaps are burner inserter -> inserter on the same tile in S2; the counts in
    swap_counts are the B4 census reference."""
    w, gd = load_fixture(seed)
    mp = master(seed, variant)
    ids = {e.extra["id"]: e for e in mp.ents}
    up = [s for s in mp.swaps if s.kind == "upgrade"]
    assert up
    for s in up:
        outs = sorted(ids[i].name for i in s.out)
        assert outs == ["burner-inserter", "burner-mining-drill"] and s.stage == 4
        de = ids[s.into[0]]
        assert de.name == "electric-mining-drill"
        cover = gd.footprint(de.name, de.x, de.y, de.direction)
        for i in s.out:
            o = ids[i]
            assert gd.footprint(o.name, o.x, o.y, o.direction) <= cover
    for s in mp.swaps:
        if s.kind == "replace":
            a, b = ids[s.out[0]], ids[s.into[0]]
            if a.name == "transport-belt":       # on-site S5: a plate-belt tile becomes one half of a splitter
                assert b.name == "splitter" and s.stage == 5
                assert gd.footprint(a.name, a.x, a.y, a.direction) <= gd.footprint(b.name, b.x, b.y, b.direction)
                continue
            assert (a.name, b.name) == ("burner-inserter", "inserter") and (a.x, a.y) == (b.x, b.y) and s.stage == 2
    sc = M.swap_counts(mp)
    assert sc["S4"]["upgrade"]["out"]["burner-mining-drill"] == len(up)
    # field poles stand on the gap rows reserved from S0: nothing else ever uses those tiles
    field_poles = [e for e in mp.ents if e.extra.get("role") == "field_pole"]
    assert field_poles and all(e.extra["stage"] == 4 for e in field_poles)


def test_planted_conflict_is_caught():
    """A stage-1 furnace inside the science zone (first built in S2/S3) and a furnace the plan removes are both
    STAGE_CONFLICT."""
    w, gd = load_fixture(2002)
    mp = copy.deepcopy(master(2002, "hub"))
    z = mp.zone("science")
    x, y = z.rect[0] + 4, z.rect[1] + 4
    mp.ents.append(PE("stone-furnace", x + 0.0, y + 0.0, 0, None, {"id": "planted", "stage": 1, "zone": "smelt_x",
                                                                    "role": "furnace"}))
    assert any(i["code"] == "STAGE_CONFLICT" for i in M.check_monotone(mp, gd))
    mp2 = copy.deepcopy(master(2002, "hub"))
    f = next(e for e in mp2.ents if e.extra.get("role") == "furnace")
    f.extra["until"] = 4
    issues = M.check_monotone(mp2, gd)
    assert any("without a planned swap" in i["text"] for i in issues)


def test_research_gating_follows_the_save():
    """Stage gates come from the save's research state: on a fresh fixture Automation is not done (S3 waits),
    the trigger techs are listed as triggers, the player's lab craft is called out; with Automation researched
    the gate opens."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    s3 = M.stage_of(mp, 3)
    # the red pack recipe itself needs the trigger tech (a lab crafted by the player), then Automation (10 packs)
    assert s3.gate == ["automation-science-pack", "automation"] and not M.gate_open(gd, s3.gate)
    rp = {r["tech"]: r for r in mp.research_path}
    assert rp["steam-power"]["source"] == "trigger" and rp["automation"]["packs"] == 10
    assert "PLAYER" in rp["automation-science-pack"]["trigger"]
    gd2 = copy.deepcopy(gd)
    gd2.raw["technologies"]["automation"]["researched"] = True
    assert not M.gate_open(gd2, s3.gate)
    gd2.raw["technologies"]["automation-science-pack"]["researched"] = True
    assert M.gate_open(gd2, s3.gate)
    line = M.roadmap_line(mp, {})
    assert "S3 red science" in line and "automation-science-pack" not in line
    st = M.stage_status(mp, gd, w)
    assert st[0] == "now" and st[3] == "waiting"


def test_card_and_brief():
    """Cards are at most 5 lines (title + trade-off, the current stage in numbers, the roadmap, removals, one
    warning); the roadmap line names every stage with its research gate; the brief has the roadmap rows."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    st = BaseState.fresh_start()
    eta = M.roadmap_eta(mp, gd, st)
    status = M.stage_status(mp, gd, w)
    c = M.card(mp, gd, eta, status, 0, recommended=True, why="over Smelt at each patch: 60 fewer belt tiles")
    lines = c.split("\n")
    assert len(lines) <= M.CARD_LINES and lines[0].startswith(mp.title + " (recommended over")
    # the roadmap: every stage with the minutes from now until it is built
    assert lines[2].startswith("Roadmap: S0") and "S5 green science ~" in lines[2]
    assert all(f"{M.slabel(s.index)} {M.short_title(s)} ~{M.fmt_n(eta[s.index]['eta']['complete'])}m" in lines[2]
               for s in mp.stages)
    assert all(len(x) <= 260 for x in lines)            # scannable: no wall of text
    assert ("Runs without the helper from S2." in lines[3]) and "ready in ~" in lines[1]
    # plain words: no option names or tech abbreviations; the hand-made packs are said out loud
    for word in ("allow_removal", "EMD", "LSP", "[", "=false"):
        assert word not in c, word
    assert M.D1_TEXT in lines[3]
    assert lines[3].startswith("Keeps everything of yours")     # nothing of the player's is touched by default
    b = M.brief(mp, gd, eta, status, recommended=True)
    assert [r["index"] for r in b["roadmap"]] == [s.index for s in mp.stages]
    assert [r["stage"] for r in b["roadmap"]] == [M.slabel(s.index) for s in mp.stages]
    row = b["roadmap"][0]
    for k in ("title", "status", "gate", "bill", "eta", "places", "removes", "swaps", "plan_id", "self_running",
              "needs_helper"):
        assert k in row
    assert b["self_running_from"] == "S2"
    assert all(eta[s.index]["eta"]["complete"] is not None for s in mp.stages)
    times = [eta[s.index]["eta"]["complete"] for s in mp.stages]
    assert times == sorted(times)
    # the card's "ready in" is the roadmap row's complete (one figure, minutes from now)
    assert f"ready in ~{M.fmt_n(eta[0]['eta']['complete'])} min" in lines[1]
    for s in mp.stages:
        e = eta[s.index]["eta"]
        assert e["build"] == pytest.approx(e["complete"] - e["start"], abs=0.11)


def test_eta_counts_built_stages_as_done():
    """Round-1 finding: offered after S1 was built, S2's card said 'done in ~32 min' (the whole roadmap again)
    while its own build took 13 min. Stages that stand already cost nothing and produce from minute 0: the next
    stage's complete is its own build time from now."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    st = BaseState.fresh_start()
    full = M.roadmap_eta(mp, gd, st)
    done = {s.index: "done" for s in mp.stages if s.index < 2}
    done[2] = "now"
    eta = M.roadmap_eta(mp, gd, st, status=done)
    for k in done:
        if done[k] == "done":
            assert eta[k]["done"] and eta[k]["eta"]["complete"] == 0
    assert eta[2]["eta"]["start"] < 1.0
    assert eta[2]["eta"]["complete"] < full[2]["eta"]["complete"] - 5
    assert eta[2]["eta"]["complete"] == pytest.approx(eta[2]["eta"]["build"] + eta[2]["eta"]["start"], abs=0.11)


def _world_of(mp, k, gd, drop=(), base=None):
    """A World holding the master's base after stage k as built entities (on a copy of `base`, else empty)."""
    from factorio_claude.planner.world import MapEntity
    ents = []
    for e in M.stage_entities(mp, k):
        if e.name in drop:
            continue
        x0, y0, x1, y1 = M._ents_rect(gd, [e])
        ents.append(MapEntity(e.name, e.x, e.y, e.direction, {}, (x0, y0, x1, y1)))
    if base is not None:
        w = copy.copy(base)
        w.entities = list(base.entities)
        w._chunk_ent = {c: list(v) for c, v in base._chunk_ent.items()}
    else:
        w = World("nauvis", 0, "t", -400, -400, 800, 800)
        w.known[:] = True
    for e in ents:
        cx, cy = math.floor(e.x) // 32, math.floor(e.y) // 32
        w._chunk_ent.setdefault((cx, cy), []).append(len(w.entities))
        w.entities.append(e)
    return w


def test_stage_status_counts_retired_entities_as_built():
    """Round-1 bug: after S0b retired S0's coal chest and its inserter, S0 counted as not built and
    plan_next_stage offered S0 again forever. A stage is done when what it places for good stands; what a later
    stage (that is built) retired counts as done."""
    w4, gd = load_fixture(4004)
    mp = master(4004, "hub")
    conn = next(s.index for s in mp.stages if s.key.startswith("connect_"))
    first = int(conn)
    w = _world_of(mp, conn, gd)
    st = M.stage_status(mp, gd, w)
    assert st[first] == "done" and st[conn] == "done"
    nxt = next(k for k, v in st.items() if v in ("now", "ready", "waiting"))
    assert nxt > conn
    # S0 built, S0b not: S0 done, S0b now
    w0 = _world_of(mp, first, gd)
    st0 = M.stage_status(mp, gd, w0)
    assert st0[first] == "done" and st0[conn] == "now"


def test_column_inserters_become_electric_at_s2():
    """M4 / B3 root cause (round 1): a burner inserter that moves ore AND coal only refuels when its furnace asks
    for coal and ran dry holding ore. From S2 every smelting-column inserter is electric (same tile, replace swap);
    S0/S1 keep burner ones in the helper's round (hand-fed), and selfrun flags any burner inserter that moves ore
    or plates at S2+."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    for k in (2, 3, 4, 5):
        assert not [e for e in M.stage_entities(mp, k) if e.extra.get("role") in ("in_b", "out_b")]
    ins = [e for e in M.stage_entities(mp, 0) if e.extra.get("role") == "in_b"]
    assert ins and all(e.extra.get("fuel") == "manual" for e in ins)
    ids = {e.extra["id"]: e for e in mp.ents}
    rep_in = [s for s in mp.swaps if s.kind == "replace" and ids[s.out[0]].extra["role"] == "in_b"]
    assert len(rep_in) == len(ins) + sum(1 for e in M.new_in(mp, 1) if e.extra.get("role") == "in_b")
    assert all(s.stage == 2 and ids[s.into[0]].name == "inserter" for s in rep_in)
    bad = copy.deepcopy(mp)
    e = next(e for e in bad.ents if e.extra.get("role") == "in_b")
    e.extra["until"] = None
    bad.swaps = [s for s in bad.swaps if e.extra["id"] not in s.out]
    sr = M.selfrun(bad, gd, 3)
    assert not sr["ok"] and any("still move ore" in i for i in sr["issues"])


def test_coal_fields_are_primed_not_hand_fed():
    """A self-feeding coal field runs on the coal it mines once it has a few (engine check E10: 20 min, no
    help), but one built while the helper carried no coal never starts (round-2 live finding: the copper line died
    with it). Its burners are 'primed': start fuel and the helper's round restarts them if they ever stop; they are
    not hand-fed, so they do not make a stage 'needs helper'."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    k = mp.stages[0].index
    ents = M.stage_entities(mp, k)
    coal = [e for e in ents if e.extra.get("coal_field") and e.extra.get("role") in ("drill_b", "ins_b")]
    assert coal and all(e.extra.get("prime") and e.extra.get("fuel") != "manual" for e in coal)
    plan = M.stage_plan(mp, k, w, gd, BaseState.fresh_start())
    rows = {(r[1], r[2]): r for r in plan.rows()}
    assert all(rows[(e.x, e.y)][-1].get("prime") for e in coal if e.extra.get("stage") == k)
    later = M.stage_plan(mp, 2, w, gd, BaseState.fresh_start())
    assert any(r[1:3] == [e.x, e.y] for r in later.stats["keep_fuelled"] for e in coal)
    assert not M.selfrun(mp, gd, 2)["needs_helper"]


def test_boiler_coal_keeps_burners_for_a_black_start():
    """The boilers' coal field keeps its burner drills for good and adds electric drills in its free slots at S4:
    electric coal drills alone cannot restart the steam engines after a power dip (round-2 live finding: two S5 runs
    stopped for good). The other coal fields' burners are upgraded in place as before."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    cb = [e for e in mp.ents if e.extra.get("zone") == "coal_boiler"]
    burners = [e for e in cb if e.name == "burner-mining-drill"]
    assert burners and all(e.extra.get("until") is None for e in burners)
    assert any(e.name == "electric-mining-drill" and e.extra.get("stage") == 4 for e in cb)
    for k in (4, 5):
        alive = [e for e in M.stage_entities(mp, k) if e.extra.get("zone") == "coal_boiler"]
        assert sum(1 for e in alive if e.name == "burner-mining-drill") == len(burners)
        assert M.coal_budget(mp, gd, k)["boiler"]["ok"]
    others = [e for e in mp.ents if e.extra.get("zone") in ("coal_iron", "coal_copper") and e.name == "burner-mining-drill"]
    assert others and all(e.extra.get("until") == 4 for e in others)


def test_green_block_is_laid_out_and_validates():
    """M3 / B5: S5 builds a real green block (round 1 had only a label): 6 logistic-science-pack AM1 fed by an
    inserter AM and a belt AM (separate lanes of one belt), which get circuits (from a cable AM, 1:1 here, room for
    3:2 kept) and gears; S5 validates and its rates give 30 green/min next to 30 red/min."""
    w, gd = load_fixture(2002)
    for variant in ("hub", "onsite"):
        mp = master(2002, variant)
        new5 = M.new_in(mp, 5)
        rec = {}
        for e in new5:
            if e.name == "assembling-machine-1":
                rec[e.recipe] = rec.get(e.recipe, 0) + 1
        assert rec == {"logistic-science-pack": 6, "inserter": 1, "transport-belt": 1, "electronic-circuit": 1,
                       "copper-cable": 1, "iron-gear-wheel": 1}, rec
        chk = M.check_all(mp, gd, w)
        assert chk["stages"][5]["ok"], chk["stages"][5]["errors"][:3]
        r5 = M.stage_rates(mp, gd, 5)
        assert r5["logistic-science-pack"] == pytest.approx(30, abs=0.5)
        assert r5["automation-science-pack"] == pytest.approx(30, abs=0.5)
        assert sum(1 for e in M.stage_entities(mp, 5) if e.name == "lab") == 16
    am = gd.entity("assembling-machine-1")["crafting_speed"]
    assert B.GREEN_N * am / gd.recipe("logistic-science-pack")["energy"] == pytest.approx(M.TARGET_GREEN)


def test_design_is_deterministic():
    """The same map gives the same master plan (no time budget: offline and live see the same plans)."""
    w, gd = load_fixture(2002)
    st = load_state(2002) or BaseState.fresh_start()
    a = M.design_master(w, gd, st, {"near": st.pos or (0, 0)}, "onsite")
    b = M.design_master(w, gd, st, {"near": st.pos or (0, 0)}, "onsite")
    assert a.id == b.id and len(a.ents) == len(b.ents)
    assert [(e.name, e.x, e.y) for e in a.ents] == [(e.name, e.x, e.y) for e in b.ents]


def test_mixed_field_sends_ore_and_coal_on_one_belt():
    """Round 3 (hub): the field's ore (both lanes) side-loads onto the LEFT lane of one belt and the coal that has
    passed the corridor onto its RIGHT lane, so one trunk carries both to a column whose middle belt takes it
    straight (no merge there). The burner phase of such a field validates with coal fed at the tail."""
    gd = gd1001()
    blk = B.mining_field(8, gd, resource="iron-ore", mixed=True)
    out, coal_in = blk.ports
    assert (out.kind, out.x, out.y, out.direction) == ("belt_out", 6, -1, 0) and coal_in.kind == "belt_in"
    belts = {(math.floor(e.x), math.floor(e.y)): e for e in blk.entities if e.name == "transport-belt"}
    mid = belts[(6, 0)]                                   # the mixed belt's first tile, fed from behind (straightener)
    assert mid.direction == 0 and belts[(6, 1)].direction == 0
    assert belts[(5, 0)].direction == 4 and belts[(7, 0)].direction == 12   # ore from the west, coal from the east
    assert belts[(4, 1)].extra["role"] == "fmerge" and belts[(8, 1)].extra["role"] == "fmerge"
    ents = [e for e in blk.entities if (e.extra or {}).get("role") in ("drill_b", "ins_b", "ore_belt", "corr",
                                                                        "fmerge")]
    from validator_util import make_grid
    g = make_grid([("iron-ore", x, y) for x in range(-1, 10) for y in range(-1, blk.size[1] + 1)],
                  rect=(-20, -20, 30, 40))
    rep = validate(ents, gd, g, {"ports": [out, B.Port("belt_in", "coal", coal_in.x, coal_in.y, 0)]})
    assert rep.ok, rep.summary(12)
    assert not [i for i in rep.errors + rep.warnings if i.kind == "fuel"]
    col = B.smelter_column(3, gd, ore="iron-ore", mixed_in=True)
    assert [p.kind for p in col.ports] == ["belt_out", "belt_out", "belt_in"]
    assert not [e for e in col.entities if (e.extra or {}).get("role") == "merge"]
    p = col.ports[2]
    assert (p.x, p.y, p.direction) == (5, 2 * 3 + 2, 0)
    assert any(e.name == "transport-belt" and (e.x, e.y) == (5.5, 2 * 3 + 1.5) for e in col.entities)


@pytest.mark.parametrize("seed,variant", CASES)
def test_nothing_is_torn_out_but_in_place_swaps(seed, variant):
    """B1 read strictly (round 3): an entity is only ever removed by an in-place swap - burner drill + its burner
    inserter -> electric drill (S4), burner inserter -> inserter (S2), a plate-belt tile -> half of a splitter
    (on-site S5), a temporary chest (+ its inserter) -> a belt on the same tile. No belt, field corridor, coal leg,
    drill, furnace or pole is taken up again (round 2's hub took up 209-558 tiles of burner-phase coal belt)."""
    w, gd = load_fixture(seed)
    mp = master(seed, variant)
    ids = {e.extra["id"]: e for e in mp.ents}
    for e in mp.ents:
        x = e.extra
        if x.get("until") is None:
            continue
        s = next(s for s in mp.swaps if x["id"] in s.out)
        assert s.kind in M.SWAP_ROLES[x["role"]], (e.name, x["role"], s.kind)
        new = set().union(*[gd.footprint(ids[i].name, ids[i].x, ids[i].y, ids[i].direction) for i in s.into]) \
            if s.into else set()
        assert gd.footprint(e.name, e.x, e.y, e.direction) <= new, (e.name, x["role"], s.slot)
    assert not [r for r in mp.routes if r.get("until") is not None]
    if variant == "hub":
        assert set(mp.stats["chains"].values()) == {"mixed"}
        assert {r["what"] for r in mp.routes} >= {"iron ore + coal", "copper ore + coal"}


def test_a_tear_out_is_a_stage_conflict():
    """check_monotone rejects a removal that is not in place: a coal-leg belt retired at S4 with nothing put on its
    tile, and a furnace removed at all."""
    w, gd = load_fixture(2002)
    mp = copy.deepcopy(master(2002, "hub"))
    leg = next(e for e in mp.ents if e.extra.get("route") == "coal to the copper field")
    leg.extra["until"] = 4
    mp.swaps.append(M.Swap("route:leg", "retire", 4, [leg.extra["id"]], []))
    issues = M.check_monotone(mp, gd)
    assert any("may not be removed" in i["text"] for i in issues), issues[:3]


def test_onsite_feeds_green_with_a_splitter_and_keeps_plate_chests():
    """Round 3 (on-site): one plate belt per metal goes to the science block; at S5 a splitter replaces one of its
    tiles and sends plates on to the green block (round 2 ran a second belt from the far column, 131 tiles on the
    real-save copy); the columns' other side keeps its plate chests for good (plates for building)."""
    w, gd = load_fixture(2002)
    mp = master(2002, "onsite")
    spl = [e for e in mp.ents if e.name == "splitter"]
    assert len(spl) == 2 and all(e.extra["stage"] == 5 for e in spl)
    assert {r["what"] for r in mp.routes} >= {"iron plates to green", "copper plates to green"}
    for zid, side_b in (("smelt_iron", "E"), ("smelt_copper", "W")):
        chests = [e for e in mp.ents if e.extra.get("zone") == zid and e.extra.get("role") == "buffer"
                  and e.extra.get("side") == side_b]
        assert chests and all(e.extra.get("until") is None for e in chests)
        assert not [e for e in mp.ents if e.extra.get("zone") == zid and e.extra.get("role") == "plate"
                    and e.extra.get("side") == side_b]
    r5 = M.stage_rates(mp, gd, 5)
    assert r5["logistic-science-pack"] == pytest.approx(30, abs=0.5)


def test_labs_use_what_the_assemblers_make():
    """Round 3: S3 builds 10 labs (30 red/min on techs up to 20 s per unit; round 2's 5 labs fell to 11-12/min on
    30 s techs), S5 16 (30 red + 30 green on techs up to 30 s); the brief says what the labs use per research speed
    and how much of the plate capacity the science uses."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    assert sum(1 for e in M.stage_entities(mp, 3) if e.name == "lab") == 10
    sn = M.science_note(mp, gd)
    # S3: the burner phase's iron (side A of the column, ~30/min) caps red at 15/min; the 10 labs use it all
    assert sn["after_S3"]["labs"] == 10 and sn["after_S3"]["red_per_min"] == pytest.approx(15)
    assert sn["after_S3"]["labs_use_per_unit_time"]["30s"] == pytest.approx(15)
    r4 = M.stage_rates(mp, gd, 4)
    assert r4["automation-science-pack"] == pytest.approx(30)        # S4's electric drills: the full 30/min
    assert 10 * 60 / 20 >= r4["automation-science-pack"]             # 10 labs use 30/min on techs up to 20 s
    assert sn["end"]["labs"] == 16 and sn["end"]["labs_use_per_unit_time"]["30s"]["green"] == pytest.approx(30)
    assert sn["plates"]["science_uses_iron"] == pytest.approx(225) and sn["plates"]["iron_per_min"] == 450


def test_removal_is_opt_in():
    """Round 3: the rebuild review never takes the player's builds apart or coal out of their machines unless they
    allowed it; what it would gain is listed (could_remove), and the card says so in plain words."""
    w, gd = load_fixture(4004)
    st = load_state(4004) or BaseState.fresh_start()
    mp = copy.deepcopy(master(4004, "hub"))
    rv = M.review(mp, w, gd, st)
    assert rv["removes"] == [] and rv["takes"] == [] and not rv["allowed"] and mp.removes == []
    rv2 = M.review(copy.deepcopy(master(4004, "hub")), w, gd, st, allow=True)
    assert rv2["allowed"]
    # what it would have taken apart is exactly what it takes apart once allowed
    assert [r["cluster"] for r in rv2["removes"]] == [r["cluster"] for r in rv["could_remove"]]
    assert sum(r[5] for r in rv2["takes"]) == rv["could_take_coal"]
    import inspect
    from factorio_claude import server
    assert inspect.signature(server.plan_starter_base).parameters["allow_removal"].default is False
    assert inspect.signature(M.master_options).parameters["allow_removal"].default is False


def test_roadmap_rows_and_brief_say_the_exception():
    """The Plans-tab roadmap row of a later stage carries its build time, bill and in-place replacements; the brief
    names the one place packs are hand-made (Automation, decision D1)."""
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    eta = M.roadmap_eta(mp, gd, BaseState.fresh_start())
    lab = M.roadmap_label(mp, 4, eta)
    assert lab.startswith("S4 Electric drills") and "min to build" in lab and "needs" in lab
    assert "replaces in place" in lab and "burner mining drill" in lab
    b = M.brief(mp, gd, eta, M.stage_status(mp, gd, w))
    assert b["exceptions"] == [M.D1_TEXT] and "science" in b and b["removal_allowed"] is False


def test_connector_stage_on_far_apart_ore():
    """seed 4004 (ore 180-300 tiles apart): a coal leg over LONG_RUN tiles becomes its own stage right after the
    chain's first stage; until then that chain's burners are hand-fed (helper round) from a coal chest the
    connector stage retires; from S2 on everything runs with the helper idle."""
    w, gd = load_fixture(4004)
    mp = master(4004, "hub")
    keys = [s.key for s in mp.stages]
    assert "connect_iron" in keys or "connect_copper" in keys
    assert any("far apart" in x for x in mp.warnings)
    conn = next(s for s in mp.stages if s.key.startswith("connect_"))
    assert any(e.extra.get("role") == "coal_chest" and e.extra.get("until") == conn.index for e in mp.ents)
    # the connector's coal leg takes the place of the chest and its inserter (it leaves the coal field straight)
    for s in mp.swaps:
        if s.slot.endswith("/out"):
            assert s.kind == "retire" and len(s.into) == 2
    sr = M.selfrun(mp, gd, int(conn.index))
    assert sr["needs_helper"] and any("until " + M.slabel(conn.index) in i for i in sr["issues"])
    # after the connector the coal reaches every burner by belt; until S2 only the smelting columns' burner
    # inserters stay in the helper's round (they move ore/plates and cannot refuel themselves reliably)
    after = M.selfrun(mp, gd, conn.index)
    assert not any("until " + M.slabel(conn.index) in i for i in after["issues"])
    assert all("burner inserters of the smelting columns" in i for i in after["issues"])
    assert not M.selfrun(mp, gd, 2)["needs_helper"]


def test_selfrun_catches_a_starved_chain():
    """Coal budget per chain: removing the iron line's coal drills leaves its burners without supply."""
    w, gd = load_fixture(2002)
    mp = copy.deepcopy(master(2002, "hub"))
    mp.ents = [e for e in mp.ents if not (e.extra.get("zone") == "coal_iron" and "drill" in e.name)]
    sr = M.selfrun(mp, gd, 3)
    assert not sr["ok"] and any("coal chain iron" in i for i in sr["issues"])


def test_master_roundtrip_and_stage_plan(tmp_path):
    w, gd = load_fixture(2002)
    mp = master(2002, "hub")
    p = mp.save(tmp_path)
    mp2 = M.MasterPlan.load(p)
    assert len(mp2.ents) == len(mp.ents) and mp2.stages[0].key == "burner_iron"
    plan = M.stage_plan(mp2, 4, w, gd, BaseState.fresh_start())
    assert plan.id.endswith("-s4") and plan.validation["ok"]
    assert len(plan.removes) == sum(len(s.out) for s in mp.swaps if s.stage == 4)
    assert plan.stats["reserved"] and plan.stats["connect_existing"]
    args = plan.preview_args()
    assert args["removes"] and args["reserve"]


@pytest.mark.skipif(not (PRIVATE / "map" / "index.json").exists(), reason="private real-copy capture not present")
def test_private_real_copy_master():
    """The owner's real-save copy (private capture, made through the live tool path): every offered master plan
    validates in every stage, is monotone, and its belts stay proportional (round 1: 824-1150 tiles, coal legs
    around the lake)."""
    import json as _json
    from factorio_claude.planner.gamedata import GameData
    w, gd = World.load(PRIVATE / "map"), GameData.load(PRIVATE / "data.json")
    st = BaseState.load(PRIVATE / "state.json")
    spot = _json.loads((PRIVATE / "spot.json").read_text()) if (PRIVATE / "spot.json").exists() else None
    near = (spot["cx"], spot["cy"]) if spot else st.pos
    opts = M.master_options(w, gd, st, {"near": near})
    assert len(opts) >= 2
    for mp, info in opts:
        assert info["check"]["ok"] and not info["check"]["monotone"]
        assert sum(r["tiles"] for r in mp.routes) < 700
