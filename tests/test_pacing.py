"""Pacing of the autopilot's Claude calls (AUTOPILOT.md 4.5): rolling cap (the lower of the companion's and the mod's),
minimum real and game gaps, back-off doubling and reset, no call while paused / while a rule applies / during host
chat, and the capped fallback that never blocks the loop."""
from factorio_claude import autopilot as AP


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def pacer(**kw):
    c = Clock()
    return AP.Pacer(kw.get("cap", 6), kw.get("gap", 180), kw.get("game_gap", 300), kw.get("backoff_max", 1800),
                    clock=c), c


def test_rolling_cap_per_hour():
    p, c = pacer(cap=3, gap=0, game_gap=0)
    tick = 0
    for _ in range(3):
        assert p.allowed("no_rule", tick)[0]
        p.record("no_rule", tick)
        c.t += 60
        tick += 3600
    assert p.allowed("no_rule", tick) == (False, "capped")
    c.t += 3600 - 170            # the first call falls out of the window
    assert p.allowed("no_rule", tick)[0]


def test_mod_cap_wins_when_lower():
    p, _ = pacer(cap=6)
    assert p.cap(4) == 4 and p.cap(10) == 6 and p.cap(None) == 6 and p.cap(0) == 0
    assert p.allowed("milestone", 0, mod_cap=0) == (False, "capped")


def test_minimum_real_and_game_gaps():
    p, c = pacer(gap=180, game_gap=300)
    p.record("milestone", 0)
    c.t += 100
    assert p.allowed("no_rule", 10 ** 6) == (False, "gap")
    c.t += 100                           # 200 s real, but only 200 game-s
    assert p.allowed("no_rule", 200 * 60) == (False, "game_gap")
    assert p.allowed("no_rule", 301 * 60)[0]


def test_backoff_doubles_and_resets():
    p, c = pacer(gap=180, game_gap=0, backoff_max=1800)
    p.record("no_rule", 0)
    p.changed(False)
    p.changed(False)
    assert p.backoff == 4
    c.t += 500                            # 180 x 4 = 720 s
    assert p.allowed("no_rule", 0) == (False, "gap")
    assert p.allowed("milestone", 0)[0]  # milestones skip the back-off
    c.t += 300
    assert p.allowed("no_rule", 0)[0]
    for _ in range(10):
        p.changed(False)
    assert p.backoff * 180 <= 1800 + 1e-9    # capped at 30 min
    p.changed(True)
    assert p.backoff == 1


def test_never_while_paused_rule_or_chat():
    p, _ = pacer()
    assert p.allowed("review", 0, paused=True) == (False, "paused")
    assert p.allowed("review", 0, rule_applies=True) == (False, "rule")
    assert p.allowed("review", 0, chat_busy=True) == (False, "chat")


def test_capped_decision_falls_back_without_calling(tmp_path):
    from tests.test_autopilot import FakeDecider, FakeGame, make
    g = FakeGame()
    dec = FakeDecider()
    ap, clock = make(g, tmp_path, decider=dec, cfg={"autopilot_calls_per_hour": 2, "autopilot_min_gap_s": 0,
                                                    "autopilot_min_gap_game_s": 0})
    ap.tick()
    s = ap._snap
    opts = [{"id": "a"}, {"id": "b"}]
    sources = []
    for i in range(6):                     # 6 forced decisions in a few minutes: at most the cap reach Claude
        clock.t += 30
        d = ap._decide("no_rule", g.get(), s, opts, default="a")
        sources.append(d.source)
    assert sources.count("claude") == 2 and sources.count("capped") == 4
    assert len(dec.asked) == 2
    assert all(st["calls"]["cap"] == 2 for st in g.statuses[-1:])


def test_chat_has_priority(tmp_path):
    from tests.test_autopilot import FakeDecider, FakeGame, make
    g = FakeGame()
    dec = FakeDecider()
    ap, clock = make(g, tmp_path, decider=dec)
    ap.chat_busy = lambda: True
    ap.tick()
    d = ap._decide("review", g.get(), ap._snap, [{"id": "continue"}])
    assert d.source == "fallback" and d.error == "chat" and not dec.asked
