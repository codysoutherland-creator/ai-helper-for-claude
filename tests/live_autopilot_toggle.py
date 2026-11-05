"""Q5: the autopilot switch in the engine (AUTOPILOT.md 11). Not collected by pytest.

    .venv\\Scripts\\python.exe tests\\live_autopilot_toggle.py [--slot 35]

Uses testenv/write/sec_players.zip (a save with a player, see testenv/security_test.py) on a debug-ops test server
(simulated chat and GUI clicks). Checks: chat status/on/off; a non-host's GUI click and chat are refused; the host's
GUI checkbox switches it on and off; "let claude play"; switching off cancels the grace queue and the helper's
autopilot tasks within 2 game seconds; the state survives a save and a restart from it; the status line says
"waiting for the companion" until a companion reports, then the companion's goal; the phase written by the companion
matches its journal. Results: testenv/inst/live_autopilot_toggle_<slot>.json.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--slot", type=int, default=35)
ap.add_argument("--save", default="sec_players.zip")
A = ap.parse_args()
assert 31 <= A.slot <= 39
NAME = f"aptoggle_{A.slot}"
INST = ROOT / "testenv" / "inst" / NAME
OUT = ROOT / "testenv" / "inst" / f"live_autopilot_toggle_{A.slot}.json"
UDP, RCON = 34500 + A.slot, 27016 + A.slot
RUN = ROOT / "testenv" / "inst" / f"{NAME}_run"
RUN.mkdir(parents=True, exist_ok=True)
os.environ.update({"FACTORIO_UDP_PORT": str(UDP), "FACTORIO_RCON_PORT": str(RCON), "FACTORIO_RCON_PASSWORD": "testpw",
                   "FACTORIO_UDP_TIMEOUT": "10", "FACTORIO_CONFIG": str(RUN / "harness_config.json"),
                   "FACTORIO_PLANS_DIR": str(RUN / "plans")})
(RUN / "harness_config.json").write_text(json.dumps({"udp_port": UDP, "rcon": True, "password": "testpw",
                                                     "port": RCON}), encoding="utf-8")
sys.path.insert(0, str(ROOT))
from factorio_claude import rcon  # noqa: E402

REP = {"checks": [], "fails": []}
B = None


def check(name, cond, info=""):
    print(("ok  " if cond else "FAIL") + " " + name + ("" if cond else f"  {str(info)[:400]}"), flush=True)
    REP["checks"].append({"name": name, "ok": bool(cond), "info": str(info)[:600]})
    if not cond:
        REP["fails"].append(name)


def restart(save, debug=True, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", NAME, "-Slot", str(A.slot)]
    args += ["-Stop"] if stop else ["-Save", save] + ([] if debug else ["-NoDebugOps"])
    lf = RUN / "restart.txt"
    with open(lf, "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=300)
    return lf.read_text(errors="replace")


def connect():
    global B
    rcon._client = rcon.Rcon("127.0.0.1", RCON, "testpw")
    for _ in range(90):
        try:
            rcon.lua("rcon.print(1)")
            rcon.lua("rcon.print(1)")
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    B = rcon.bridge


def lua(code):
    return rcon.lua(code).strip()


def wait_ticks(n):
    t0 = int(lua("rcon.print(game.tick)"))
    while int(lua("rcon.print(game.tick)")) < t0 + n:
        time.sleep(0.2)


def open_panel(reopen=False):
    """Open the Claude panel of player 1 (a click on the Claude button toggles it); reopen = close it first."""
    if reopen and B("gui_dump", player=1).get("panel"):
        B("gui_click", player=1, name="claude_toggle")
    if not B("gui_dump", player=1).get("panel"):
        B("gui_click", player=1, name="claude_toggle")


def chat(text, player=1):
    return B("chat_simulate", player=player, text=text)


def get():
    return B("autopilot_get")


def main():
    print(restart(A.save)[-150:])
    connect()
    lua("game.speed = 1 rcon.print(1)")
    ping = B("ping")
    p1 = ping["players"][0]["name"]
    r = chat("@claude autopilot status")
    check("chat status (off)", r["route"] == "local" and "off" in (r.get("reply") or "").lower(), r)
    # a guest (someone else is the host) may talk to Claude (policy everyone) but not switch the autopilot
    B("access_test", policy="everyone", host_index=99)
    r = chat("@claude autopilot on")
    check("guest chat on refused", "Only the host" in (r.get("reply") or "") and not get()["on"], r)
    r = chat("let claude play")
    check("guest 'let claude play' refused", not get()["on"], r)
    open_panel(reopen=True)
    r = B("gui_click", player=1, name="claude_ap_toggle")
    check("guest GUI checkbox disabled", r.get("disabled") is True and not get()["on"], r)
    B("access_test", policy="host-only")          # back to normal: player 1 is the host
    open_panel(reopen=True)                          # rebuilt: the checkbox is enabled for the host
    r = B("gui_click", player=1, name="claude_ap_toggle")
    g = get()
    check("host GUI checkbox on", g["on"] and g["epoch"] == 1 and g["by"] == p1, (r, g))
    dump = B("gui_dump", player=1)
    s = json.dumps(dump)
    check("GUI shows it on", '"claude_ap_toggle"' in s and "Let Claude play" in s, s[:300])
    check("status line while no companion", (g.get("status_line") or "").startswith("Autopilot: on, waiting for "
                                                                                   "the companion"), g.get("status_line"))
    # self-approval queue + autopilot helper tasks, then off cancels both within 2 game seconds
    B("plan_preview", id="aptoggle-poles", title="Toggle test poles", owner="autopilot", show=False,
      entities=[["small-electric-pole", 3.5, 10.5, 0]])
    g = get()
    check("autopilot plan queued (grace)", any(q["id"] == "aptoggle-poles" for q in g["queue"]), g["queue"])
    B("agent_quick", action="spawn", x=0, y=0)
    B("agent_tasks", tasks=[{"type": "wait", "ticks": 36000, "autopilot": True}, {"type": "wait", "ticks": 36000,
                                                                                   "autopilot": True}])
    r = B("gui_click", player=1, name="claude_ap_toggle")
    wait_ticks(120)
    g = get()
    st = B("agent_status")
    check("off: grace queue empty", not g["on"] and not g["queue"], g)
    # the player's own order (the save's helper follows player 1) stays; every autopilot task is gone
    check("off: helper's autopilot tasks cancelled", not st.get("queue") and
          not (st.get("current") or {}).get("autopilot"), st)
    wait_ticks(60 * 35)
    pl = next((p for p in B("plan_list") if p["id"] == "aptoggle-poles"), {})
    check("off: queued plan never applied", pl.get("status") == "proposed", pl)
    # chat on / off
    r = chat("@claude autopilot on")
    check("host chat on", get()["on"] and get()["epoch"] == 3, r)
    r = chat("@claude autopilot off")
    check("host chat off", not get()["on"], r)
    r = chat("let claude play")
    g = get()
    check("'let claude play' switches on", g["on"] and g["epoch"] == 5, r)
    check("the earlier autopilot plan is queued again on", any(q["id"] == "aptoggle-poles" for q in g["queue"]),
          g["queue"])
    # save, restart from the save: still on, same epoch, waiting for the companion
    lua('game.server_save("aptoggle") rcon.print(1)')
    time.sleep(3)
    src = INST / "write" / "saves" / "aptoggle.zip"
    for _ in range(30):
        if src.exists():
            break
        time.sleep(1)
    dst = ROOT / "testenv" / "write" / f"aptoggle_{A.slot}.zip"
    shutil.copy2(src, dst)
    print(restart(dst.name)[-150:])
    connect()
    g = get()
    check("survives save + load (on, epoch)", g["on"] and g["epoch"] == 5, g)
    check("after load: waiting for the companion", "waiting for the companion" in (g.get("status_line") or ""),
          g.get("status_line"))
    # a companion (fake Claude) picks it up: the status line shows its goal, its phase matches its journal
    comp = start_companion()
    try:
        ok = False
        for _ in range(60):
            time.sleep(2)
            g = get()
            if g.get("goal") and "waiting for the companion" not in (g.get("status_line") or ""):
                ok = True
                break
        check("companion resumes: status line shows the goal", ok, g.get("status_line"))
        rows = [json.loads(x) for x in (RUN / "autopilot.jsonl").read_text(encoding="utf-8").splitlines()]
        check("companion journal starts at the saved epoch", any(r["kind"] == "start" and r.get("epoch") == 5
                                                                for r in rows), rows[:3])
        check("phase written by the companion", g.get("phase") in ("observe", "decide", "act", "wait_build", "stuck",
                                                                     "start", "planning"), g.get("phase"))
        r = chat("@claude autopilot off")
        time.sleep(4)
        rows = [json.loads(x) for x in (RUN / "autopilot.jsonl").read_text(encoding="utf-8").splitlines()]
        check("companion sees off", any(r["kind"] == "off" for r in rows), rows[-3:])
    finally:
        comp.terminate()
        comp.wait(15)
    dst.unlink(missing_ok=True)


def start_companion():
    work = RUN / "companion"
    work.mkdir(exist_ok=True)
    (work / "config.json").write_text(json.dumps({"udp_port": UDP, "rcon": False, "password": ""}), encoding="utf-8")
    (RUN / "autopilot.jsonl").unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FACTORIO_")}
    env.update({"FACTORIO_CONFIG": str(work / "config.json"), "FACTORIO_UDP_PORT": str(UDP),
                "FACTORIO_COMPANION_LOCK_PORT": str(34600 + A.slot), "FACTORIO_COMPANION_LOG": str(RUN / "companion.log"),
                "FACTORIO_CHAT_DIR": str(work / "chat"), "FACTORIO_PLANS_DIR": str(RUN / "plans"),
                "FACTORIO_SCRIPT_OUTPUT": str(INST / "write" / "script-output"), "FACTORIO_PLAN_UNCHARTED": "1",
                "FACTORIO_AUTOPILOT_JOURNAL": str(RUN / "autopilot.jsonl"), "FACTORIO_FAKE_CLAUDE": "1"})
    py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return subprocess.Popen([str(py), "-m", "factorio_claude.companion", "--no-exit-check"], cwd=ROOT, env=env,
                            stdout=open(RUN / "companion.out", "w"), stderr=subprocess.STDOUT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        REP["error"] = traceback.format_exc()
        print(REP["error"])
    finally:
        OUT.write_text(json.dumps(REP, indent=1), encoding="utf-8")
        print(restart(None, stop=True))
        print("FAILS:", REP["fails"] or "none")
