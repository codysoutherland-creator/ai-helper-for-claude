"""M0: markdown stripping, cost-gate classification/triage, cost line, fake claude, >50-ghost refusal."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factorio_claude import blueprints as bp  # noqa: E402
from factorio_claude import companion as c  # noqa: E402
from factorio_claude import server  # noqa: E402

# (markdown in, plain text out) - the Lua util.strip_markdown is tested with the same cases in-game
MD_CASES = [
    ("**Done.** Furnaces at [gps=-40,20] need coal.", "Done. Furnaces at [gps=-40,20] need coal."),
    ("## Plan\n- place *8* furnaces\n- add `coal`", "Plan. Place 8 furnaces. Add coal"),
    ("1. Mine iron\n2) Smelt it\n3. Profit", "Mine iron. Smelt it. Profit"),
    ("# Heading\n\n---\nText with __bold__ and _italic_.", "Heading. Text with bold and italic."),
    ("See [the wiki](https://wiki.factorio.com) and [item=iron-plate] x5", "See the wiki and [item=iron-plate] x5"),
    ("> quoted line\n* star bullet", "Quoted line. Star bullet"),
    ("plain sentence, nothing to do", "Plain sentence, nothing to do"),
    ("iron_ore stays, 2*3 stays", "Iron_ore stays, 2*3 stays"),
]


def test_plain_text():
    for md, want in MD_CASES:
        assert c.plain_text(md) == want, (md, c.plain_text(md))


def test_classify():
    for t in ["set me up with a starter base", "Build a smelting column at the iron patch", "optimize my bus",
              "design a mall", "x" * 201, "can you lay out a mining outpost?", "please build me a bus",
              "expand my smelting"]:
        assert c.classify(t) == "planning", t
    for t in ["how much iron do I make?", "what's the helper doing", "thanks!", "why is power low?",
              "check my base", "how is my base doing?", "what should I build next?", "why is my factory slow",
              "is my power ok", "run", "can you check my base?"]:
        assert c.classify(t) == "chat", t


def test_cost_line():
    assert c.cost_line({"cost_usd": 0.0234}, 4.2) == "cost ≈ $0.02 · 4 s"
    assert c.cost_line({}, 1.0) == "cost n/a · 1 s"


def test_gate_triage_run_cancel_expire():
    gate = c.CostGate()
    msgs = [{"id": 1, "player": "alice", "text": "build me a starter base", "source": "gui"},
            {"id": 2, "player": "alice", "text": "how much copper?", "source": "gui"},
            {"id": 3, "player": "bob", "text": "optimize my smelting", "source": "chat"}]
    run, hold, expired = c.triage(msgs, gate)
    assert [m["id"] for m in run] == [2] and not expired
    assert {p: [m["id"] for m in ms] for p, ms in hold} == {"alice": [1], "bob": [3]}
    rid_alice = gate.hold(hold[0][1])
    rid_bob = gate.hold(hold[1][1])
    run, hold, expired = c.triage([{"player": "alice", "text": f"run {rid_alice}", "source": "confirm"},
                                   {"player": "bob", "text": f"cancel {rid_bob}", "source": "confirm"}], gate)
    assert [m["id"] for m in run] == [1] and not hold and not expired and not gate.pending
    # unknown / expired id -> the player is told to ask again
    stale = c.CostGate(ttl=60)
    rid = stale.hold([msgs[0]], now=-1000)
    run, hold, expired = c.triage([{"player": "alice", "text": f"run {rid}", "source": "confirm"}], stale)
    assert run == [] and expired == ["alice"]
    # gate off: everything runs directly
    run, hold, _ = c.triage(msgs, c.CostGate(), enabled=False)
    assert len(run) == 3 and not hold


def test_fake_claude(monkeypatch):
    monkeypatch.setenv("FACTORIO_FAKE_CLAUDE", "**hi**\\n- one")
    text, info = c.ask_claude("question", {"id": None, "turns": 0}, {})
    assert text == "**hi**\n- one" and info["fake"] and info["cost_usd"] == 0.0123
    monkeypatch.setenv("FACTORIO_FAKE_CLAUDE", "1")
    assert c.ask_claude("q1\nctx", {"id": None, "turns": 0}, {})[0] == "(fake) you said: q1"


def test_ghost_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_call", lambda op, **a: calls.append((op, a)) or "ok")
    ghosts = [{"name": "transport-belt", "x": i + 0.5, "y": 0.5} for i in range(51)]
    r = server.factorio_place_ghosts(ghosts)
    assert r.startswith("REFUSED") and "planner" in r and not calls
    assert server.factorio_place_ghosts(ghosts[:50], plan="p1") == "ok"
    assert calls[-1] == ("place_ghosts", {"ghosts": ghosts[:50], "surface": None, "plan": "p1"})
    assert server.factorio_place_ghosts(ghosts, allow_large=True) == "ok"

    big = bp.encode(bp.make_blueprint([{"name": "wooden-chest", "x": i + 0.5, "y": 0.5} for i in range(60)], "big"))
    small = bp.encode(bp.make_blueprint([{"name": "wooden-chest", "x": i + 0.5, "y": 0.5} for i in range(5)], "s"))
    assert server.blueprint_entity_count(big) == 60 and server.blueprint_entity_count("garbage") == 0
    assert server.blueprint_place(big, 0, 0).startswith("REFUSED")
    assert server.blueprint_place(small, 0, 0, plan="x") == "ok" and calls[-1][1]["plan"] == "x"
    assert server.blueprint_place(big, 0, 0, allow_large=True) == "ok"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
