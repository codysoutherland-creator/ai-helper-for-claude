"""The knowledge base (AUTOPILOT.md 6): schema, every game fact checked against the game's own data (fixtures), the
research order, sizing against the rate solver, prompt budgets and the generated doctrine.md."""
import subprocess
import sys
from pathlib import Path

import pytest

from factorio_claude import knowledge as K
from factorio_claude.planner import rates
from factorio_claude.planner.testing import fixture_seeds, load_fixture

ROOT = Path(__file__).resolve().parent.parent


def test_schema_rules_have_ids_groups_and_audiences():
    d = K.load()
    ids = [r["id"] for r in d["rules"]]
    assert len(ids) == len(set(ids)), "rule ids are unique"
    for r in d["rules"]:
        assert r["group"] in K.GROUPS and r["id"].startswith(r["group"] + "-")
        assert r["kind"] in ("doctrine", "convention", "fact")
        assert set(r["applies"]) <= {"autopilot", "chat"} and r["applies"]
        assert len(r["text"]) < 400 and "\n" not in r["text"]
    for g in K.GROUPS:
        assert any(r["group"] == g for r in d["rules"]), g
    assert tuple(d["conventions"]["acquisition_order"]) == K.ACQUISITION_ORDER


@pytest.mark.parametrize("seed", fixture_seeds())
def test_every_game_fact_matches_the_game_data(seed):
    _, gd = load_fixture(seed)
    assert K.check(gd) == []


def test_check_catches_a_wrong_number():
    _, gd = load_fixture(1001)
    import copy
    bad = copy.deepcopy(gd.raw)
    bad["technologies"]["automation"]["unit_count"] = 11
    bad["recipes"]["iron-gear-wheel"]["energy"] = 1.0
    bad["entities"]["transport-belt"]["belt_items_per_s"] = 16
    issues = K.check(type("G", (), {"raw": bad})())
    assert any("automation" in i and "units" in i for i in issues)
    assert any("iron-gear-wheel" in i for i in issues)
    assert any("transport-belt" in i for i in issues)


def test_corrections_found_in_the_game_data_are_in_the_doctrine():
    f, c = K.facts(), K.conventions()
    assert f["recipe_inputs"]["small-electric-pole"] == {"wood": 1, "copper-cable": 2}
    assert "small-electric-pole" not in c["mall_v1"] and "lab" not in c["mall_v1"]
    assert f["unlocked_by"]["long-handed-inserter"] == "automation"
    assert f["unlocked_by"]["underground-belt"] == "logistics" and f["unlocked_by"]["splitter"] == "logistics"
    assert f["techs"]["electric-mining-drill"]["prereq"] == ["automation-science-pack"]
    assert f["trigger_techs"]["automation-science-pack"]["trigger"] == "craft-item"


def test_research_order_follows_prerequisites_and_packs():
    _, gd = load_fixture(1001)
    start = {"steam-power", "electronics", "automation-science-pack"}
    first = K.research_order(gd, researched=start)
    assert first[0] == "automation" and "electric-mining-drill" in first
    # nothing that needs green packs before green science is researched and made
    for t in K.research_order(gd, researched=start | {"automation", "logistics"}, limit=10):
        packs = [i[0] for i in gd.raw["technologies"][t]["ingredients"]]
        assert packs == ["automation-science-pack"], t
    later = K.research_order(gd, researched=start | {"automation", "electric-mining-drill", "logistics",
                                                     "logistic-science-pack", "steel-processing"},
                             packs_made={"automation-science-pack", "logistic-science-pack"}, limit=5)
    assert "automation-2" in later
    # a trigger tech is never queued for labs
    assert "oil-processing" not in K.research_order(gd, researched=set(gd.raw["technologies"]) - {"oil-processing"})


@pytest.mark.parametrize("spm", [15, 30, 60])
def test_sizing_matches_the_rate_solver(spm):
    _, gd = load_fixture(1001)
    s = K.sizing(spm)
    r = rates.solve({"automation-science-pack": spm, "logistic-science-pack": spm}, gd,
                    {"machines": {"crafting": "assembling-machine-1", "smelting": "stone-furnace"},
                     "assume_research": True, "mine": False, "surface": "nauvis"})

    def count(name):
        return sum(v["count"] for k, v in r.recipes.items() if k == name or k.startswith(name + "@"))
    for item in ("automation-science-pack", "logistic-science-pack", "iron-gear-wheel", "electronic-circuit",
                 "copper-cable"):
        assert count(item) == pytest.approx(s["machines"][item], rel=0.05), item
    assert count("iron-plate") * K.facts()["machine_out_per_min"]["stone-furnace"]["iron-plate"] == \
        pytest.approx(s["iron-plate_per_min"], rel=0.05)
    assert count("copper-plate") * 18.75 == pytest.approx(s["copper-plate_per_min"], rel=0.05)
    table = K.conventions()["sizing_red_green_am1"]
    if str(spm) in table:
        assert table[str(spm)]["iron-plate_per_min"] == pytest.approx(s["iron-plate_per_min"], rel=0.01)
        assert table[str(spm)]["automation-science-pack"] == pytest.approx(s["machines"]["automation-science-pack"],
                                                                           rel=0.01)


def test_prompt_budgets():
    full = K.prompt_text(K.AUTOPILOT_BUDGET, focus="G3")
    chat = K.prompt_text(K.CHAT_BUDGET, applies="chat")
    assert K.tokens(full) <= K.AUTOPILOT_BUDGET and K.tokens(chat) <= K.CHAT_BUDGET
    assert "[AUTO-1]" in chat and "[ORG-1]" in chat and "[SCALE-2]" in chat
    assert "[SAFE-2]" not in chat                    # autopilot-only rule
    assert "[SAFE-1]" in full and "30 red+green/min" in full
    assert full.index("[SAFE-1]") < full.index("[AUTO-1]")      # safety first: never cut by the budget
    oil = K.prompt_text(K.AUTOPILOT_BUDGET, focus="G9")
    assert "Oil: pumpjack 10 crude/s" in oil and K.tokens(oil) <= K.AUTOPILOT_BUDGET and "[RES-4]" in oil


def test_mall_items_and_goals():
    assert "underground-belt" not in K.mall_items(1)
    assert "underground-belt" in K.mall_items(1, researched=["logistics"])
    ids = [g["id"] for g in K.roadmap()]
    assert ids == [f"G{i}" for i in range(11)]
    for g in K.roadmap():
        assert all(n in ids for n in g["needs"]) and g["round"] in (1, 2, 3, 4)
    # round 3: past red and green the roadmap goes on to oil (and names blue science as the next, unshipped goal)
    g9 = K.goal("G9")
    assert g9["templates"] == ["oil", "oil_plant"] and g9["round"] == 3 and "plastics" in g9["techs"]
    assert K.goal("G10")["round"] > 3
    # the mall comes with the electric drill stage, not after green science
    assert {"metric": "master_stage_started", "min": 4} in K.goal("G2")["ready"]


def test_doctrine_md_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_doctrine_md.py"), "--check"], capture_output=True,
                       text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (ROOT / "skill" / "factorio" / "doctrine.md").read_text(encoding="utf-8") == \
        (ROOT / "factorio_claude" / "knowledge" / "doctrine.md").read_text(encoding="utf-8")


def test_verify_tool_reads_lua_prototypes(tmp_path):
    """tools/verify_doctrine.py --data: the tolerant Lua reader on a small synthetic prototype file."""
    base = tmp_path / "base" / "prototypes"
    base.mkdir(parents=True)
    (base / "technology.lua").write_text('''
local icon = "x.png"
data:extend
({
  { type = "technology", name = "automation", icon = icon, -- a comment
    effects = {{ type = "unlock-recipe", recipe = "assembling-machine-1" },
               { type = "unlock-recipe", recipe = "long-handed-inserter" }},
    prerequisites = {"automation-science-pack"},
    unit = { count = 10, ingredients = {{"automation-science-pack", 1}}, time = 10 }, order = util.foo("a") },
})''', encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import verify_doctrine as V
        raw = V.gd_from_lua(tmp_path)
    finally:
        sys.path.remove(str(ROOT / "tools"))
    t = raw["technologies"]["automation"]
    assert t["unit_count"] == 10 and t["unit_energy"] == 600 and t["prerequisites"] == ["automation-science-pack"]
    assert {e["recipe"] for e in t["effects"]} == {"assembling-machine-1", "long-handed-inserter"}


def test_research_ranking_is_doctrine_first_and_peaceful():
    """Round-2 judge: the autopilot researched military, gun turrets, walls, radar and lamps as 'cheap fillers' in a
    peaceful game. Useful techs first, weapons only with enemies around, lamps / radar never by default; a tech
    whose packs the base does not make is never offered (it would block the labs)."""
    gd = load_fixture(1001)[1]
    done = {n for n, t in gd.raw["technologies"].items() if t.get("researched")} | {
        "automation", "steam-power", "electronics", "automation-science-pack", "electric-mining-drill", "logistics",
        "logistic-science-pack"}
    red = {"automation-science-pack"}
    picks = K.research_order(gd, researched=done, packs_made=red, limit=20)
    assert picks[:2] == ["steel-processing", "fast-inserter"]
    assert not any(K.is_combat(t) for t in picks) and not set(picks) & {"lamp", "radar", "repair-pack"}
    assert any(K.is_combat(t) for t in K.research_order(gd, researched=done, packs_made=red, limit=20,
                                                         peaceful=False))
    assert all(set(K.tech_packs(gd, t)) <= red for t in picks)
    assert K.research_blocked(gd, ["automation-2", "steel-processing"], red) == ["automation-2"]
    for name in ("military", "military-2", "gun-turret", "stone-wall", "physical-projectile-damage-1"):
        assert K.is_combat(name), name
    for name in ("logistics", "steel-processing", "automation-2"):
        assert not K.is_combat(name), name
