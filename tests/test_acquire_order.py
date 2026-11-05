"""The helper's acquisition order and hand-craft caps (AUTOPILOT.md 7.1, P3): the Lua tables in agent.lua match the
doctrine, and the Lua-free model (knowledge.acquire_model, the same rules as agent.lua's material planner) prefers
automation, caps hand-crafting once a mall exists and keeps the bootstrap exceptions."""
import re
from pathlib import Path

from factorio_claude import knowledge as K

AGENT = Path(__file__).resolve().parent.parent / "mod" / "claude-bridge" / "scripts" / "agent.lua"


def lua_table(name):
    src = AGENT.read_text(encoding="utf-8")
    m = re.search(r"local " + name + r" = \{(.*?)\}\n", src, re.S)
    assert m, name
    return m.group(1)


def test_lua_order_and_caps_match_the_doctrine():
    order = re.findall(r'"([a-z_]+)"', lua_table("ACQ_ORDER"))
    assert tuple(order) == K.ACQUISITION_ORDER == tuple(K.conventions()["acquisition_order"])
    caps = dict((k, float(v)) for k, v in re.findall(r"(\w+) = (\d+)", lua_table("HANDCRAFT")))
    hc = K.handcraft()
    assert caps == {"max_job_s": hc["max_job_s"], "loop_alarm_s_per_10min": hc["loop_alarm_s_per_10min"],
                    "short_craft_s": hc["short_craft_s"]}
    boot = set(re.findall(r'\["([a-z0-9-]+)"\] = true|(\b[a-z]+) = true', lua_table("BOOTSTRAP_ITEMS")))
    boot = {a or b for a, b in boot}
    assert boot == set(hc["bootstrap_items"])


def test_sources_come_before_crafting_in_order():
    got = K.acquire_model("transport-belt", 100, have={"transport-belt": 10},
                          sources=[("chest", 20), ("producer", 5), ("mall", 30)], player=50, mall=True)
    assert got == [("inventory", 10), ("mall", 30), ("producer", 5), ("chest", 20), ("player", 35)]


def test_wait_for_the_mall_instead_of_crafting():
    got = K.acquire_model("inserter", 40, sources=[("mall", 10)], mall_maker=True, mall=True)
    assert got == [("mall", 10), ("wait", 30)]
    assert all(src not in ("handcraft", "short_craft") for src, _ in got)


def test_bootstrap_crafts_freely_before_a_mall_exists():
    assert K.acquire_model("electric-mining-drill", 30, craft_s_per_item=6.0, mall=False) == [("handcraft", 30)]


def test_caps_once_a_mall_exists():
    # 30 drills x ~6 s = 180 s: over 60 s per job; halved to a batch that fits, the rest is a shortage
    got = K.acquire_model("electric-mining-drill", 30, craft_s_per_item=6.0, mall=True)
    crafted = sum(n for s, n in got if s == "handcraft")
    assert crafted * 6.0 <= K.handcraft()["max_job_s"] and ("short", 30 - crafted) in got
    # the 10-minute loop alarm: 100 s already crafted -> only a 20 s job fits
    got = K.acquire_model("inserter", 100, craft_s_per_item=1.0, mall=True, recent_s=100)
    assert sum(n for s, n in got if s == "handcraft") <= 20
    # a short craft (<= 10 s) is fine while the 10-minute budget lasts ...
    assert K.acquire_model("iron-gear-wheel", 10, craft_s_per_item=0.5, mall=True, recent_s=0) == \
        [("short_craft", 10)]
    # ... but counts against it: round 3's helper halved a refused batch until it fell under the short-craft bar and
    # crafted 145-375 s per 10 game minutes next to a running mall
    assert K.acquire_model("iron-gear-wheel", 10, craft_s_per_item=0.5, mall=True, recent_s=500) == [("short", 10)]
    got = K.acquire_model("transport-belt", 400, craft_s_per_item=0.25, mall=True, recent_s=110)
    assert sum(n for s, n in got if s in ("handcraft", "short_craft")) * 0.25 <= 10


def test_bootstrap_items_are_always_allowed():
    assert K.acquire_model("automation-science-pack", 10, craft_s_per_item=5.5, mall=True, recent_s=500) == \
        [("handcraft", 10)]
    assert K.craft_allowed("inserter", 300, mall=True, bootstrap=True)
    # ... until a mall machine makes them: then assemblers come from the mall's assembler machine
    assert K.craft_allowed("assembling-machine-1", 30, mall=False, mall_item=True)          # no mall yet
    assert not K.craft_allowed("assembling-machine-1", 30, mall=True, mall_item=True)
    assert K.acquire_model("assembling-machine-1", 5, craft_s_per_item=6.0, mall=True, mall_maker=True) == \
        [("wait", 5)]


def test_mall_items_get_only_a_short_top_up():
    """An item a mall machine is set to make, while that machine is idle (no plates yet): at most a 10 s craft, never
    the bulk (the helper waits at the mall when the machine runs)."""
    got = K.acquire_model("transport-belt", 200, craft_s_per_item=0.25, mall=True, mall_item=True)
    made = sum(n for s, n in got if s in ("handcraft", "short_craft"))
    assert made * 0.25 <= K.handcraft()["short_craft_s"] and ("short", 200 - made) in got
    # a non-mall item (a lab) may still be crafted within the 60 s job limit
    assert K.acquire_model("lab", 4, craft_s_per_item=6.0, mall=True) == [("handcraft", 4)]


def test_raw_materials_are_mined_last():
    assert K.acquire_model("stone", 50, craft_s_per_item=0, raw=True) == [("smelt_mine", 50)]
    assert K.acquire_model("coal", 5, sources=[("chest", 5)], craft_s_per_item=0, raw=True) == [("chest", 5)]


def test_agent_lua_wires_the_order():
    """The planner code itself: sources ranked mall > plan > producer > chest, assemblers only as surplus, the player
    gated by the autopilot setting, wait_collect before crafting, craft_ok at the top level."""
    src = AGENT.read_text(encoding="utf-8")
    assert "SRC_RANK = {mall = 1, plan = 2, producer = 3, chest = 4}" in src
    assert "defines.entity_status.full_output" in src and "claude-autopilot-use-player-items" in src
    acquire = src[src.index("function P.acquire(item, n, depth)"):src.index("function P.resource_near")]
    assert acquire.index("for _, s in ipairs(srcs)") < acquire.index("if pinv then") < \
        acquire.index('type = "wait_collect"') < acquire.index("handcraftable(r)") < acquire.index("smelt_recipe(")
    assert "P.craft_ok(item, P.craft_s - s0)" in acquire
    # round 3: a mall machine counts as making its item between crafts (products_finished), and the hand-craft budget
    # covers short crafts too; fuel is kept out of the built-items share
    assert "mall_machine_active(e)" in src and "products_finished" in src
    ok = src[src.index("function P.craft_ok(item, secs)"):src.index("local function set(tbl, k, v)")]
    assert ok.index("loop_alarm_s_per_10min") < ok.index("short_craft_s")
    assert "new_planner(c, c.position, player, t.coal_from, {fuel = true})" in src and '"^fuel_(.+)$"' in src
