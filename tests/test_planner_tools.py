"""server.py planner tools and the companion's Plans-tab handling, offline (fixture map, fake game link)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude import companion, server  # noqa: E402
from factorio_claude.planner.plan import Plan  # noqa: E402
from factorio_claude.planner.testing import fixture_seeds, load_fixture  # noqa: E402


class FakeGame:
    def __init__(self):
        self.calls = []

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if op == "player":
            return {"name": "alice", "surface": "nauvis", "pos": {"x": 0.5, "y": -2.5}}
        if op == "plan_preview":
            return {"id": args["id"], "entities": len(args["entities"]), "objects": 10}
        if op == "agent_status":
            return {"alive": True}
        if op == "agent_tasks":
            return {"queued": len(args["tasks"])}
        raise server.LinkError(f"unknown op {op}")


def test_plan_starter_base_tool(tmp_path, monkeypatch):
    """Planner v2 (the fallback when no master plan fits, and FACTORIO_PLANNER=v2): unchanged behaviour."""
    seed = fixture_seeds()[0]
    world, gd = load_fixture(seed)
    game = FakeGame()
    monkeypatch.setenv("FACTORIO_PLANNER", "v2")
    monkeypatch.setattr(server, "call", game)
    monkeypatch.setattr(server, "PLANS", tmp_path)
    seen = {}

    def fake_world(cx, cy, surface=None, radius=server.PLAN_RADIUS):
        seen["at"] = (cx, cy, surface)
        return world, gd
    monkeypatch.setattr(server, "_world", fake_world)
    out = json.loads(server.plan_starter_base("start me off", variants=2))
    assert seen["at"] == (0.5, -2.5, "nauvis")  # planned around the player
    # planner v2: "compact" became "quick" (alias kept in staged.ALIASES); the recommended plan comes first
    assert sorted(p["variant"] for p in out["plans"]) == ["balanced", "quick"]
    assert out["recommended"] == out["plans"][0]["id"]
    assert out["state"]["source"] == "ops"  # this fake game has no base_state op: the older ops were used
    for p in out["plans"]:
        assert p["eta"]["first_plates"] is not None and p["bill"]["need"] and "card" in p
        assert "First plates" in p["card"] and "Needs" in p["card"] and "Removes" in p["card"]
    previews = [a for op, a in game.calls if op == "plan_preview"]
    assert [a["show"] for a in previews] == [True, False]  # first one drawn, the other listed
    assert all(a["group"] == out["group"] and a["validation"]["ok"] for a in previews)
    for p in out["plans"]:
        assert (tmp_path / f"{p['id']}.json").exists()
        assert "entities" in p and isinstance(p["entities"], int) and "card" in p
    # compact: no entity dumps (round 3: ~1.5 KB more per variant for its placed stage 2 - its bill, research, what
    # it unlocks and when red science research can start - which Claude has to tell the player)
    assert len(json.dumps(out)) < 9500
    for p in out["plans"]:   # each variant's stage 2 is saved under its own id, ready for plan_preview later
        s2 = next((s for s in p["stages"] if s.get("placed")), None)
        if s2 is not None:
            assert (tmp_path / f"{s2['id']}.json").exists()
    # re-preview from disk
    r = json.loads(server.plan_preview(out["plans"][0]["id"]))
    assert r["preview"]["entities"] == out["plans"][0]["entities"]
    assert server.plan_preview("nope").startswith("ERROR")


def test_plan_starter_base_masters(tmp_path, monkeypatch):
    """Planner v3: master plans with their roadmap; the current stage of each is previewed (the recommended one
    shown), masters and stage plans are saved; plan_roadmap reads the master back."""
    world, gd = load_fixture(2002)
    game = FakeGame()
    monkeypatch.delenv("FACTORIO_PLANNER", raising=False)
    monkeypatch.setattr(server, "call", game)
    monkeypatch.setattr(server, "PLANS", tmp_path)
    monkeypatch.setattr(server, "_world", lambda cx, cy, surface=None, radius=server.PLAN_RADIUS: (world, gd))
    monkeypatch.setattr(server, "_master_world", lambda mp: (world, gd))
    out = json.loads(server.plan_starter_base("start me off"))
    assert out["masters"] and out["recommended"] == out["masters"][0]["master"]
    m = out["masters"][0]
    assert m["recommended"] and m["roadmap"][0]["key"] == "burner_iron" and m["roadmap"][0]["status"] == "now"
    assert m["checked"]["stages_valid"] and m["checked"]["monotone"]
    assert len(m["card"].split("\n")) <= 5 and m["card"].split("\n")[2].startswith("Roadmap: S0")
    previews = [a for op, a in game.calls if op == "plan_preview"]
    assert [a["show"] for a in previews] == [True] + [False] * (len(previews) - 1)
    assert previews[0]["zones"] and previews[0]["validation"]["ok"]
    assert (tmp_path / f"{m['master']}.master.json").exists() and (tmp_path / f"{m['now_plan']}.json").exists()
    assert "roadmap" in out["next"] and "recommended" in out["next"]
    r = json.loads(server.plan_roadmap(m["master"]))
    assert r["master"] == m["master"] and len(r["roadmap"]) == len(m["roadmap"])
    assert server.plan_roadmap("nope").startswith("ERROR")


def _built_world(mp, k, gd, base):
    """A copy of the fixture World `base` with the master's base after stage k standing as built entities."""
    import copy
    import math
    from factorio_claude.planner import master as M
    from factorio_claude.planner.world import MapEntity
    w = copy.copy(base)
    w.entities = list(base.entities)
    w._chunk_ent = {c: list(v) for c, v in base._chunk_ent.items()}
    for e in M.stage_entities(mp, k):
        x0, y0, x1, y1 = M._ents_rect(gd, [e])
        cx, cy = math.floor(e.x) // 32, math.floor(e.y) // 32
        w._chunk_ent.setdefault((cx, cy), []).append(len(w.entities))
        w.entities.append(MapEntity(e.name, e.x, e.y, e.direction, {}, (x0, y0, x1, y1)))
    return w


def test_master_roadmap_in_the_game_and_next_stage(tmp_path, monkeypatch):
    """M5: every later stage of every master is sent to the game as a waiting ("future") plan - not drawn - with its
    research gate and the stage it follows (the mod offers it when both are met). plan_next_stage advances past
    a built stage (round 1: it re-offered S0 forever) and plan_apply of a stage re-checks the whole base after
    that stage (round 1: a connector stage was refused as 'fed by nothing')."""
    from factorio_claude.planner import master as M
    from factorio_claude.planner.gamedata import GameData
    from factorio_claude.planner.world import World
    world, gd = load_fixture(4004)
    game = FakeGame()
    monkeypatch.delenv("FACTORIO_PLANNER", raising=False)
    monkeypatch.setattr(server, "call", game)
    monkeypatch.setattr(server, "PLANS", tmp_path)
    monkeypatch.setattr(server, "_world", lambda cx, cy, surface=None, radius=server.PLAN_RADIUS: (world, gd))
    out = json.loads(server.plan_starter_base(""))
    rec = next(m for m in out["masters"] if m["recommended"])
    previews = [a for op, a in game.calls if op == "plan_preview"]
    mine = [a for a in previews if (a.get("future") or {}).get("master") == rec["master"]]
    assert len(mine) == len(rec["roadmap"]) - 1 and all(a["show"] is False for a in mine)
    assert mine[0]["future"]["after"] == rec["now_plan"]
    assert all(b["future"]["after"] == a["id"] for a, b in zip(mine, mine[1:]))
    red = next(a for a in mine if a["id"].endswith("-s3"))
    assert red["future"]["gate"] == ["automation-science-pack", "automation"] and red["master"] == rec["master"]
    assert "wire_with" not in next(a for a in mine if a["id"].endswith("-s2")) and red["wire_with"]  # S2 has the first poles
    mp = M.MasterPlan.load(tmp_path / f"{rec['master']}.master.json")
    k0, k1 = mp.stages[0].index, mp.stages[1].index
    w1 = _built_world(mp, k0, gd, world)
    monkeypatch.setattr(server, "_world", lambda cx, cy, surface=None, radius=server.PLAN_RADIUS: (w1, gd))
    monkeypatch.setattr(server, "_master_world", lambda mp: (w1, gd))
    nx = json.loads(server.plan_next_stage(rec["master"], show=False))
    assert nx["stage"] == M.slabel(k1) and nx["valid"], nx
    monkeypatch.setattr(World, "from_link", classmethod(lambda cls, **kw: w1))
    monkeypatch.setattr(GameData, "from_link", classmethod(lambda cls, **kw: gd))
    game.calls.clear()
    game_apply = lambda op, **a: ({"placed": 1} if op == "plan_apply" else game(op, **a))  # noqa: E731
    monkeypatch.setattr(server, "call", game_apply)
    r = json.loads(server.plan_apply(nx["id"]))
    assert r.get("applied") == {"placed": 1}, r


def test_plan_path_is_sanitised():
    assert server._plan_path("../../evil id").name == "evilid.json"


def test_companion_plan_event(tmp_path, monkeypatch):
    """The mod starts the helper on approve; the companion only confirms (never queues a second build)."""
    monkeypatch.setattr(companion, "PLANS_DIR", tmp_path)
    Plan("com-boot-1a2b", "Compact hub (bootstrap)", "compact", "bootstrap", "s", [],
         stats={"bbox": [-10, -20, 30, 40]}).save(tmp_path)
    building = type("G", (FakeGame,), {"__call__": lambda s, op, **a: (s.calls.append((op, a)) or
                                                                       {"alive": True, "current": {"type": "build_ghosts"}, "queue": ["refuel"]})})()
    monkeypatch.setattr(companion.link, "call", building)
    text = companion.plan_event({"source": "plan", "text": "approved com-boot-1a2b", "player": "alice"})
    assert "Compact hub (bootstrap)" in text and "helper is building" in text
    assert [op for op, _a in building.calls] == ["agent_status"]
    assert companion.plan_event({"source": "plan", "text": "rejected x"}) is None
    # planner v3: the mod offered a waiting stage by itself (research done, previous stage built): a free note
    Plan("mhub-1-s3", "Central hub: S3 Automate red science", "hub", "red", "s", [],
         stats={"eta": {"build": 7.4}}).save(tmp_path)
    text = companion.plan_event({"source": "plan", "text": "stage_ready mhub-1-s3", "player": "server"})
    assert "S3 Automate red science" in text and "Plans tab" in text and "7 min" in text
    # helper following the player / not spawned: point at the plan card's build button
    for st in ({"alive": False}, {"alive": True, "current": {"type": "follow"}, "queue": []}):
        g = type("G", (FakeGame,), {"__call__": lambda s, op, st=st, **a: st})()
        monkeypatch.setattr(companion.link, "call", g)
        text = companion.plan_event({"source": "plan", "text": "approved com-boot-1a2b"})
        assert "Helper: build" in text


def test_companion_prompt_is_planner_first():
    s = companion.SYSTEM
    assert "plan_starter_base" in s and "Plans tab" in s and "never call plan_apply" in s
    assert "No markdown" in s
    # planner v2: honest trade-offs per variant (first plates, bill vs stock, removals) and a recommendation
    flat = " ".join(s.split())
    assert "time to first plates" in flat and "takes apart" in flat and "recommend" in flat
