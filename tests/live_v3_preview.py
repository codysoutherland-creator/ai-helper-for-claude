"""Live check (not pytest): a planner v3 stage plan with its reserved zones goes through the mod's plan_preview on a
test slot: the zones are stored and drawn (faint outlines + labels, game and chart view), the plan validates.

    .venv\\Scripts\\python.exe tests\\live_v3_preview.py [--slot 35] [--seed 2002]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factorio_claude import rcon  # noqa: E402
from factorio_claude.planner import master as M  # noqa: E402
from factorio_claude.planner.state import BaseState  # noqa: E402
from factorio_claude.planner.testing import load_fixture  # noqa: E402


def restart(slot, save, stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", f"v3preview{slot}", "-Slot", str(slot)]
    args += ["-Stop"] if stop else ["-Save", save]
    with open(ROOT / "testenv" / "inst" / f"v3preview{slot}.txt", "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=180)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=35)
    ap.add_argument("--seed", type=int, default=2002)
    a = ap.parse_args()
    assert 31 <= a.slot <= 39
    w, gd = load_fixture(a.seed)
    mp = M.design_master(w, gd, None, {"near": (0, 0)}, "hub")
    plan = M.stage_plan(mp, 0, w, gd, BaseState.fresh_start())
    args = plan.preview_args()
    restart(a.slot, f"seed_{a.seed}.zip")
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + a.slot, "testpw")
    for _ in range(40):
        try:
            rcon.lua("rcon.print('ping')")
            break
        except Exception:
            time.sleep(1)
    rcon.lua("rcon.print('warm')")
    ok = True
    try:
        bare = dict(args, zones=[], id=plan.id + "-nozones")
        r0 = rcon.bridge("plan_preview", **bare)
        r = rcon.bridge("plan_preview", **args)
        print("plan_preview:", json.dumps(r)[:300])
        # every labelled zone: a fill + an outline in game and in chart view, and a label
        want = sum(5 if z[4] else 4 for z in args["zones"])
        print("objects with zones", r.get("objects"), "without", r0.get("objects"), "zone objects expected", want)
        if not r.get("drawn") or (r.get("objects") or 0) - (r0.get("objects") or 0) != want:
            ok = False
        log = (ROOT / "testenv" / "inst" / f"v3preview{a.slot}" / "server.log").read_text(errors="replace")
        if "Error" in log and "claude-bridge" in log.split("Error", 1)[1][:400]:
            ok = False
            print(log[-2000:])
    finally:
        restart(a.slot, "", stop=True)
    print("PASS" if ok else "FAIL", f"zones sent {len(args['zones'])}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
