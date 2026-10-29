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
    seed = fixture_seeds()[0]
    world, gd = load_fixture(seed)
    game = FakeGame()
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
