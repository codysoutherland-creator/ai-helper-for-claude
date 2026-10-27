"""CI: load the mod (folder or release zip) into a headless Factorio server and talk to it over UDP and RCON.

    python tests/headless_mod_load.py --factorio <factorio/bin/x64/factorio> --mod <mod/claude-bridge | zip>
        [--expansions] [--udp-port 34590] [--rcon-port 27090]

Everything lives in a temp dir (own write-data, mods, save); RCON is bound to 127.0.0.1. Exit 0 when the mod loads
with no errors and answers `ping` over both links. Needs no login: the headless server is a free download.
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
sys.path.insert(0, str(ROOT))


def wait_for(log: Path, needles, timeout, proc):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        for n in needles:
            if n in text:
                return n, text
        if proc.poll() is not None:
            return None, text
        time.sleep(0.5)
    return None, log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factorio", required=True, type=Path)
    ap.add_argument("--mod", required=True, type=Path, help="mod folder or claude-bridge_<version>.zip")
    ap.add_argument("--expansions", action="store_true", help="also enable space-age, quality, elevated-rails")
    ap.add_argument("--udp-port", type=int, default=34590)
    ap.add_argument("--rcon-port", type=int, default=27090)
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="fc-headless-"))
    mods, write = work / "mods", work / "write"
    mods.mkdir()
    write.mkdir()
    if a.mod.is_dir():
        shutil.copytree(a.mod, mods / "claude-bridge")
    else:
        shutil.copy2(a.mod, mods / a.mod.name)
    entries = [{"name": "base", "enabled": True}, {"name": "claude-bridge", "enabled": True}]
    for m in ("space-age", "quality", "elevated-rails"):
        entries.append({"name": m, "enabled": a.expansions})
    (mods / "mod-list.json").write_text(json.dumps({"mods": entries}), encoding="utf-8")
    (work / "config.ini").write_text(f"[path]\nread-data=__PATH__executable__/../../data\n"
                                     f"write-data={write.as_posix()}\n", encoding="utf-8")
    (work / "server-settings.json").write_text(json.dumps({
        "name": "ci", "description": "ci", "visibility": {"public": False, "lan": False},
        "require_user_verification": False, "auto_pause": False, "autosave_interval": 0}), encoding="utf-8")
    base = [str(a.factorio), "--config", str(work / "config.ini"), "--mod-directory", str(mods)]
    save = write / "saves" / "ci.zip"
    r = subprocess.run(base + ["--create", str(save)], capture_output=True, text=True, timeout=600)
    print(r.stdout[-3000:])
    if r.returncode or not save.exists():
        print("FAIL: --create failed", r.stderr[-2000:])
        return 1
    log = work / "server.log"
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(base + ["--start-server", str(save), "--server-settings",
                                        str(work / "server-settings.json"), "--port", "34290",
                                        "--rcon-bind", f"127.0.0.1:{a.rcon_port}", "--rcon-password", "ci-only",
                                        "--enable-lua-udp", str(a.udp_port)], stdout=f, stderr=subprocess.STDOUT)
    rc = 1
    try:
        hit, text = wait_for(log, ["Starting RCON", "Error"], 300, proc)
        print(text[-4000:])
        if hit != "Starting RCON":
            print("FAIL: the server didn't start (see log above)")
            return 1
        if "claude-bridge" not in text:
            print("FAIL: claude-bridge is not in the loaded mod list")
            return 1
        os.environ.update(FACTORIO_UDP_PORT=str(a.udp_port), FACTORIO_RCON_PORT=str(a.rcon_port),
                          FACTORIO_RCON_PASSWORD="ci-only", FACTORIO_UDP_TIMEOUT="5")
        from factorio_claude import link, rcon
        udp = None
        for _ in range(20):  # the server needs a few ticks before the mod polls UDP
            try:
                udp = link._udp(json.dumps({"op": "ping", "args": {}}).encode(), a.udp_port, 5)
            except OSError:
                udp = None
            if udp:
                break
            time.sleep(1)
        print("udp ping:", udp)
        via_rcon = rcon.bridge("ping")
        print("rcon ping:", via_rcon)
        later = log.read_text(encoding="utf-8", errors="replace")
        errors = [ln for ln in later.splitlines() if "Error" in ln and "claude" in ln.lower()]
        ok = bool(udp and udp.get("ok")) and via_rcon is not None and not errors
        print("errors:", errors)
        print("PASS" if ok else "FAIL")
        rc = 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
