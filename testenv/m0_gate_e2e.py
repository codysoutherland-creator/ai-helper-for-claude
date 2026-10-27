# Cost gate end-to-end on a test slot: starts a companion (fake claude, no tokens) pointed at the slot, sends a chat
# request through the server console, answers the in-game confirmation, checks the reply + cost line.
# Usage: .venv\Scripts\python.exe testenv\m0_gate_e2e.py [slot]
import os
import subprocess
import sys
import time
from pathlib import Path

SLOT = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ROOT = Path(__file__).resolve().parents[1]
exec(open(ROOT / "testenv" / "t.py").read())
WORK = ROOT / "testenv" / "inst" / f"companion{SLOT}"
WORK.mkdir(parents=True, exist_ok=True)
(WORK / "companion.log").unlink(missing_ok=True)
fails = []


def check(name, cond, info=""):
    print(("ok  " if cond else "FAIL") + " " + name + ("" if cond else f"  {info}"))
    if not cond:
        fails.append(name)


def wait(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    return None


def history():
    return B("claude_history", n=50)


env = dict(os.environ, FACTORIO_UDP_PORT=str(34500 + SLOT), FACTORIO_RCON_PORT=str(27016 + SLOT),
           FACTORIO_RCON_PASSWORD="testpw", FACTORIO_COMPANION_LOCK_PORT=str(34700 + SLOT),
           FACTORIO_CHAT_DIR=str(WORK), FACTORIO_COMPANION_LOG=str(WORK / "companion.log"),
           FACTORIO_SCRIPT_OUTPUT=str(ROOT / "testenv" / "inst" / "m0" / "write" / "script-output"),
           FACTORIO_FAKE_CLAUDE="## Result\\n**Done** - furnaces at [gps=1,2]\\n- step one\\n- step two")
proc = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "factorio_claude.companion",
                         "--no-exit-check"], cwd=ROOT, env=env)
try:
    check("companion online", wait(lambda: history()["online"]))
    B("claude_confirm_drop")
    # chat: a cheap question runs right away (no confirmation) and gets a cost line
    rcon._client.command("@claude how much iron do I make?")
    h = wait(lambda: (lambda x: x if x["history"][-1]["who"] == "Claude" else None)(history()))
    last = h and h["history"][-1]
    check("chat answered without confirm", last and not h["confirms"], h)
    check("markdown stripped", last and last["text"] == "Result. Done - furnaces at [gps=1,2]. Step one. Step two", last)
    check("cost line", last and last.get("cost", "").startswith("cost ≈ $0.01 · "), last)

    # planning: held for confirmation
    n0 = len(history()["history"])
    rcon._client.command("@claude build me a starter base")
    h = wait(lambda: (lambda x: x if x["confirms"] else None)(history()))
    check("planning request held", h and len(h["confirms"]) == 1 and "planning job" in h["confirms"][0]["text"], h)
    time.sleep(2)
    check("no answer before confirm", history()["history"][-1]["who"] != "Claude")
    rid = h["confirms"][0]["id"]
    B("confirm_simulate", request_id=rid, run=True)
    h = wait(lambda: (lambda x: x if x["history"][-1]["who"] == "Claude" and len(x["history"]) > n0 + 2 else None)(history()))
    check("runs after Run", h and h["history"][-1].get("cost"), h)

    # cancel: nothing sent to Claude
    rcon._client.command("@claude design a mall please")
    h = wait(lambda: (lambda x: x if x["confirms"] else None)(history()))
    rid = h["confirms"][0]["id"]
    n1 = len(history()["history"])
    B("confirm_simulate", request_id=rid, run=False)
    time.sleep(4)
    hist = history()["history"]
    check("cancel -> no Claude answer", hist[-1]["text"].startswith("Cancelled") and len(hist) == n1 + 2, hist[-3:])
    log = (WORK / "companion.log").read_text(encoding="utf-8")
    check("companion log: 2 claude turns only", log.count("<< ") == 2, log[-1500:])
finally:
    proc.terminate()
    proc.wait(10)

print("\nFAILED: " + ", ".join(fails) if fails else "\ncost gate end-to-end passed")
sys.exit(1 if fails else 0)
