"""Capture a save for OFFLINE planning through the server's own export path, and prove offline == live.

    .venv\\Scripts\\python.exe testenv\\capture_live.py --save real_copy.zip [--slot 33] [--out DIR] [--no-compare]

Starts the save on a headless TEST slot (31-39, restart.ps1 -NoDebugOps: like a player's game), then exports exactly
what plan_starter_base reads: the player's spot (server._player_spot), the map (server._world: PLAN_RADIUS, charted
chunks only, as the tool does), the game data and the player's state (server._state). Written to --out (default
testenv/private/capture_<save stem>; a capture of a real save holds the player's map and name and must stay in the
gitignored testenv/private): map/, data.json, state.json, spot.json.

Then (unless --no-compare) it calls the MCP tool plan_starter_base on the running slot (plans into a temporary
folder) and master_options() on the capture, and prints both lists of master ids: they are identical when the
planner is deterministic and the capture is the tool's input (PLANNER-v3 section 17/18). The slot is stopped at the
end. Never touches the real game: test slots only, their own ports.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--save", required=True, help="save file in testenv/write")
ap.add_argument("--slot", type=int, default=33)
ap.add_argument("--name", default="capture")
ap.add_argument("--out", default=None)
ap.add_argument("--no-compare", action="store_true")
A = ap.parse_args()
assert 31 <= A.slot <= 39, "test slots 31-39 only"
OUT = Path(A.out) if A.out else ROOT / "testenv" / "private" / f"capture_{Path(A.save).stem}"
NAME = f"{A.name}_{A.slot}"
PLANS = Path(tempfile.mkdtemp(prefix="plans_capture_"))
os.environ.update({"FACTORIO_UDP_PORT": str(34500 + A.slot), "FACTORIO_RCON_PORT": str(27016 + A.slot),
                   "FACTORIO_RCON_PASSWORD": "testpw", "FACTORIO_RCON_HOST": "127.0.0.1", "FACTORIO_UDP_TIMEOUT": "20",
                   "FACTORIO_PLANS_DIR": str(PLANS),
                   "FACTORIO_SCRIPT_OUTPUT": str(ROOT / "testenv" / "inst" / NAME / "write" / "script-output")})
if A.save.startswith("seed_"):
    os.environ["FACTORIO_PLAN_UNCHARTED"] = "1"
assert 34500 + A.slot != 34500 and 27016 + A.slot != 27015
sys.path.insert(0, str(ROOT))
from factorio_claude import rcon  # noqa: E402
from factorio_claude import server as S  # noqa: E402
from factorio_claude.planner import master as M  # noqa: E402
from factorio_claude.planner.gamedata import GameData  # noqa: E402
from factorio_claude.planner.state import BaseState  # noqa: E402
from factorio_claude.planner.world import World  # noqa: E402


def restart(stop=False):
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "testenv" / "restart.ps1"),
            "-Name", NAME, "-Slot", str(A.slot)]
    args += ["-Stop"] if stop else ["-Save", A.save, "-NoDebugOps"]
    lf = ROOT / "testenv" / "inst" / f"{NAME}_restart.txt"
    lf.parent.mkdir(parents=True, exist_ok=True)
    with open(lf, "w") as fh:
        subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=300)
    return lf.read_text(errors="replace")


def connect(tries=90):
    rcon._client = rcon.Rcon("127.0.0.1", 27016 + A.slot, "testpw")
    for _ in range(tries):
        try:
            rcon.lua("rcon.print('ping')")
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("server did not come up")


def main():
    print(restart()[-200:])
    connect()
    cx, cy, surface = S._player_spot()
    world, gd = S._world(cx, cy, surface)
    st = S._state(cx, cy)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(world.dir, OUT / "map")
    shutil.copy2(gd.path, OUT / "data.json")
    st.save(OUT / "state.json")
    (OUT / "spot.json").write_text(json.dumps({"cx": cx, "cy": cy, "surface": surface, "radius": S.PLAN_RADIUS}),
                                   encoding="utf-8")
    print("captured", OUT, "spot", (cx, cy))
    if A.no_compare:
        return
    t0 = time.time()
    live = json.loads(S.plan_starter_base(""))
    live_ids = [m["master"] for m in live.get("masters") or []]
    print("live  plan_starter_base:", live_ids, "recommended", live.get("recommended"), round(time.time() - t0), "s")
    w2 = World.load(OUT / "map")
    gd2 = GameData.load(OUT / "data.json")
    st2 = BaseState.load(OUT / "state.json")
    off = M.master_options(w2, gd2, st2, {"near": (cx, cy)})
    off_ids = [mp.id for mp, _i in off]
    print("offline master_options:  ", off_ids, "recommended", next(mp.id for mp, i in off if i["recommended"]))
    same = live_ids == off_ids
    print("IDENTICAL" if same else "DIFFERENT")
    (OUT / "compare.json").write_text(json.dumps({"live": live_ids, "offline": off_ids, "identical": same,
                                                  "recommended_live": live.get("recommended")}), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    finally:
        print(restart(stop=True))
        shutil.rmtree(PLANS, ignore_errors=True)
