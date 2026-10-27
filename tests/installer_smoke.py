"""Checks a sandboxed run of install.sh / install.ps1 (CI and local testing; never against a real profile).

    python tests/installer_smoke.py --app <AIHELPER_HOME> --user-dir <FACTORIO_USER_DIR> --claude-dir <~/.claude>
        [--fake-state <FAKE_CLAUDE_STATE>] [--apps-dir <AIHELPER_APPS_DIR or AIHELPER_DESKTOP_DIR>] [--no-claude]

Exit 0 when everything the installer should have done is in place.
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, type=Path)
    ap.add_argument("--user-dir", required=True, type=Path)
    ap.add_argument("--claude-dir", required=True, type=Path)
    ap.add_argument("--fake-state", type=Path)
    ap.add_argument("--apps-dir", type=Path)
    ap.add_argument("--no-claude", action="store_true", help="the run had no claude CLI: expect the warning path")
    a = ap.parse_args()
    app = a.app.resolve()
    win = os.name == "nt"

    py = app / ".venv" / ("Scripts/python.exe" if win else "bin/python")
    check(py.exists(), f"venv python {py}")
    r = subprocess.run([str(py), "-c", "import factorio_claude.server, numpy, scipy, psutil"], cwd=app,
                       capture_output=True, text=True)
    check(r.returncode == 0, "venv imports factorio_claude.server, numpy, scipy, psutil " + r.stderr[-300:])
    check((app / "MANIFEST").exists(), "MANIFEST written")
    cfg_path = app / "config.json"
    check(cfg_path.exists(), "config.json created")
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        check(len(cfg.get("password", "")) >= 30 and cfg.get("rcon") is False, "config: random password, RCON off")
        if not win:
            check(cfg_path.stat().st_mode & 0o777 == 0o600, "config.json is 0600")
            check(bool(cfg.get("extra_path")), "config: extra_path saved")

    link = a.user_dir / "mods" / "claude-bridge"
    check(link.exists() and link.resolve() == (app / "mod" / "claude-bridge").resolve(),
          f"mod link {link} -> {app / 'mod' / 'claude-bridge'}")
    check(link.is_symlink() or (hasattr(link, "is_junction") and link.is_junction()), "mod link is a link")
    ml = json.loads((a.user_dir / "mods" / "mod-list.json").read_text(encoding="utf-8"))
    check(any(m.get("name") == "claude-bridge" and m.get("enabled") for m in ml["mods"]), "mod-list: enabled")
    check((a.user_dir / "mods" / "mod-list.json.bak-claude").exists(), "mod-list backup")

    skill = a.claude_dir / "skills" / "factorio" / "SKILL.md"
    text = skill.read_text(encoding="utf-8") if skill.exists() else ""
    check("managed by ai-helper-for-claude" in text and str(app) in text and "{{INSTALL_DIR}}" not in text,
          f"skill installed at {skill}")

    if a.fake_state:
        st = json.loads(a.fake_state.read_text(encoding="utf-8")) if a.fake_state.exists() else {"servers": {}}
        s = st["servers"].get("factorio")
        check(bool(s) and Path(s["args"][0]).resolve() == (app / "run_server.py").resolve()
              and Path(s["command"]).resolve() == py.resolve(), f"mcp registered: {s}")
    summary = json.loads((app / "logs" / "install-summary.json").read_text(encoding="utf-8"))
    bad = {k: v for k, v in summary.items() if isinstance(v, dict) and v.get("status") == "failed"}
    check(not bad, f"no failed steps {bad or ''}")
    if a.no_claude:
        check(summary.get("claude_missing") is True, "claude missing -> warning path")

    if a.apps_dir:
        if sys.platform == "darwin":
            bundle = a.apps_dir / "Factorio (Claude).app"
            info = bundle / "Contents" / "Info.plist"
            check(info.exists(), f"app bundle {bundle}")
            if info.exists():
                with open(info, "rb") as f:
                    plist = plistlib.load(f)
                check(plist.get("CFBundleExecutable") == "launch" and plist.get("CFBundlePackageType") == "APPL",
                      "Info.plist")
            exe = bundle / "Contents" / "MacOS" / "launch"
            check(exe.exists() and os.access(exe, os.X_OK), "launch script is executable")
            r = subprocess.run(["/bin/bash", "-n", str(exe)], capture_output=True, text=True)
            check(r.returncode == 0, "launch script parses " + r.stderr)
        elif win:
            check((a.apps_dir / "Factorio (Claude).lnk").exists(), "desktop shortcut")
        else:
            check((a.apps_dir / "factorio-claude.desktop").exists(), "menu entry")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
