# In-engine checks for the multiplayer/security rules (mod 0.4.0) on a TEST slot.
#   restart.ps1 -Name sec<N> -Slot <N>               then  python testenv/security_test.py <N>
#   restart.ps1 -Name sec<M> -Slot <M> -NoDebugOps   then  python testenv/security_test.py <M> --no-debug
# Needs a save that has a player (a --create'd save has none): copy an autosave from an instance a GUI client joined
# into testenv/write (e.g. sec_players.zip) and pass -Save. Uses the access_test debug op to make player 1 a "guest".
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from factorio_claude import rcon  # noqa: E402

SLOT = int(sys.argv[1]) if len(sys.argv) > 1 else 21
NO_DEBUG = "--no-debug" in sys.argv
if SLOT < 1:
    sys.exit("use a test slot (1+); slot 0 shares the real game's UDP port")
rcon._client = rcon.Rcon("127.0.0.1", 27016 + SLOT, "testpw")
B = rcon.bridge
fails = []


def check(name, cond, info=""):
    print(("ok  " if cond else "FAIL") + " " + name + ("" if cond else f"  {info}"))
    if not cond:
        fails.append(name)


def lua(code):
    return rcon.lua(code).strip()


def err(op, **args):
    try:
        return "NO ERROR: " + json.dumps(B(op, **args))[:200]
    except rcon.RconError as e:
        return str(e)


GUEST = {"host_index": 99}  # access_test: someone else is the host, player 1 is a guest


def set_policy(v, host_index=None):
    r = B("access_test", policy=v, **({} if host_index is None else {"host_index": host_index}))
    assert r["policy"] == v, r
    return r


ping = B("ping")
p1 = ping["players"][0]
name = p1["name"]
check("ping: who_can_ask default host-only", ping.get("who_can_ask") == "host-only", ping)
check("ping: multiplayer flag + roles", ping.get("multiplayer") is True and p1.get("role") == "host"
      and p1.get("index") == 1, ping)
check("ping: debug_ops flag", ping.get("debug_ops") is (not NO_DEBUG), ping)
# claude_history is a normal op; count history to see whether a question was queued
hist_n = lambda: len(B("claude_history", n=50)["history"])  # noqa: E731

if NO_DEBUG:
    for op in ("chat_simulate", "confirm_simulate", "gui_click", "gui_dump", "agent_give", "instant_build",
               "access_test"):
        check(f"{op} disabled without debug ops", "test/cheat op" in err(op, player=name, text="x", name="x",
                                                                          item="coal", request_id="x", x=0, y=0),
              err(op, player=name))
    check("deconstruct radius 100 refused", "too large" in err("deconstruct", x=0, y=0, radius=100),
          err("deconstruct", x=0, y=0, radius=100))
    check("deconstruct radius 10 ok", "NO ERROR" in err("deconstruct", x=5000, y=5000, radius=10))
    check("deconstruct huge area refused", "too large" in err("deconstruct", area=[[-500, -500], [500, 500]]))
else:
    B("claude_confirm_drop")
    lua('game.players[1].admin = true rcon.print("ok")')
    set_policy("host-only")  # back to normal: player 1 is the host
    n0 = hist_n()
    r = B("chat_simulate", player=name, text="@claude what is my iron output?")
    check("host: chat goes to Claude", r.get("route") == "claude" and hist_n() == n0 + 1, r)

    # player 1 as a guest (host is someone else), not admin
    lua('game.players[1].admin = false rcon.print("ok")')
    roles = set_policy("host-only", 99)["roles"][name]
    check("guest role", roles["role"] == "player" and not roles["can_ask"] and not roles["can_confirm"], roles)
    n0 = hist_n()
    r = B("chat_simulate", player=name, text="@claude build me a base")
    check("host-only: guest chat denied", r.get("route") == "denied" and hist_n() == n0, r)
    r = B("chat_simulate", player=name, text="@claude wood")
    check("host-only: guest free helper command denied", r.get("route") == "denied", r)
    if not B("gui_dump", player=name).get("panel"):
        B("gui_click", player=name, name="claude_toggle")
    try:
        B("gui_click", player=name, name="claude_act_spawn")
        st = B("gui_dump", player=name)["state"]
        check("host-only: guest helper button denied", "Only the host" in str(st.get("msg")), st.get("msg"))
    except rcon.RconError as e:
        check("helper button reachable", False, e)
    B("plan_preview", id="sectest", title="Security test plan", entities=[], show=False)
    try:
        B("gui_click", player=name, name="claude_tab_plans")
        B("gui_click", player=name, name="claude_plan_approve|sectest")
        st = B("gui_dump", player=name)["state"]
        check("host-only: guest can't approve a plan", "approve plans" in str(st.get("plan_msg"))
              and B("plan_get", id="sectest").get("status") == "proposed", st.get("plan_msg"))
    except rcon.RconError as e:
        check("plans tab reachable", False, e)
    B("plan_drop", id="sectest")
    B("claude_confirm", request_id="sec1", to=name, estimate_text="Run it?")
    r = B("confirm_simulate", player=name, request_id="sec1", run=True)
    check("host-only: guest can't run a held job", r.get("ok") is False and "Only the host" in r.get("msg", ""), r)

    # admins: a guest admin may ask and confirm; a plain guest may not
    set_policy("admins", 99)
    lua('game.players[1].admin = true rcon.print("ok")')
    n0 = hist_n()
    r = B("chat_simulate", player=name, text="@claude what is my iron output?")
    check("admins: admin may ask", r.get("route") == "claude" and hist_n() == n0 + 1, r)
    r = B("confirm_simulate", player=name, request_id="sec1", run=False)
    check("admins: admin may cancel a held job", r.get("ok") is True, r)
    lua('game.players[1].admin = false rcon.print("ok")')
    r = B("chat_simulate", player=name, text="@claude what is my iron output?")
    check("admins: non-admin guest denied", r.get("route") == "denied", r)

    # everyone: a guest may ask, but never run a held job
    set_policy("everyone", 99)
    n0 = hist_n()
    r = B("chat_simulate", player=name, text="@claude what is my iron output?")
    check("everyone: guest may ask", r.get("route") == "claude" and hist_n() == n0 + 1, r)
    B("claude_confirm", request_id="sec2", to=name, estimate_text="Run it?")
    conf = [c for c in B("claude_history")["confirms"] if c["id"] == "sec2"]
    check("everyone: a guest's held job is addressed to the host (to=nil)", conf and conf[0].get("to") is None, conf)
    r = B("confirm_simulate", player=name, request_id="sec2", run=True)
    check("everyone: guest can't run a held job", r.get("ok") is False, r)
    r = B("chat_simulate", player=name, text="@claude run")
    check("everyone: guest '@claude run' refused", r.get("route") == "local" and "Only the host" in str(r.get("reply")), r)
    # inbox entries carry player_index + role from the game
    msgs = B("inbox_pop", ack=0)["messages"]
    last = msgs[-1] if msgs else {}
    check("inbox entry has player_index + role", last.get("player_index") == 1 and last.get("role") == "player", last)

    # back to normal
    set_policy("host-only")
    lua('game.players[1].admin = true rcon.print("ok")')
    B("claude_confirm_drop")
    r = B("chat_simulate", player=name, text="@claude what is my iron output?")
    check("restored: host may ask", r.get("route") == "claude", r)
    B("inbox_pop", ack=10 ** 6)

# UDP request buffers are capped (16 open, <= 256 parts each)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 0))
for i in range(40):
    s.sendto(f"Q|junk{i}|1|200|".encode() + b"x" * 3000, ("127.0.0.1", 34500 + SLOT))
s.sendto(b"Q|toobig|1|5000|x", ("127.0.0.1", 34500 + SLOT))
import time  # noqa: E402
time.sleep(1.5)
if NO_DEBUG:
    print("(udp buffer checks need the access_test debug op)")
else:
    udp = B("access_test", policy="host-only")["udp_open"]
    udp = list(udp.values()) if isinstance(udp, dict) else udp
    check("udp: at most 16 incomplete requests buffered", 0 < len(udp) <= 16, len(udp))
    check("udp: newest kept", any("junk39" in u["key"] for u in udp), [u["key"] for u in udp])
    check("udp: request with > 256 parts ignored", not any("toobig" in u["key"] for u in udp))

print("\nFAILED: " + ", ".join(fails) if fails else "\nsecurity checks passed")
sys.exit(1 if fails else 0)
