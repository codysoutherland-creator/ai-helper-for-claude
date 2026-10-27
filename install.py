"""Setup for AI Helper (for Claude) on Windows, macOS and Linux. Safe to re-run (that is also how you update/repair).
The one-line installers (install.sh / install.ps1) run it with the app's venv python; from a checkout:

    .venv/bin/python install.py            (macOS / Linux)
    .venv\\Scripts\\python.exe install.py    (Windows)
    ... install.py --check                 report what is set up, change nothing
    ... install.py --uninstall             undo the steps below (leaves this folder, your config and logs)

Steps:
- Python deps into the venv from requirements.lock (exact versions, --require-hashes; uv when available, else pip)
- config.json (only you can read it) with a random RCON password; RCON stays off unless "rcon": true. On macOS/Linux
  also the paths a Finder/desktop start can't find on its own (factorio_path, claude_path, extra_path)
- links mod/claude-bridge into Factorio's mods folder (Windows: directory junction; macOS/Linux: symlink; else a copy)
  and enables it in mod-list.json (one backup: mod-list.json.bak-claude). Factorio must not be running.
- copies the Claude Code skill to ~/.claude/skills/factorio (only over a copy this installer made)
- registers the `factorio` MCP server for Claude Code (user scope), if Claude Code is installed
- creates the launcher: Windows desktop shortcut "Factorio (Claude)" (pythonw launcher.pyw), macOS app
  "Factorio (Claude).app" (/Applications if writable, else ~/Applications), Linux menu entry

Sandbox / test overrides: FACTORIO_USER_DIR (Factorio's user folder), CLAUDE_CONFIG_DIR (instead of ~/.claude),
CLAUDE_BIN, FACTORIO_BIN, AIHELPER_NO_SHORTCUT=1, AIHELPER_NO_MCP=1, AIHELPER_NO_DEPS=1, AIHELPER_APPS_DIR (macOS app /
Linux .desktop folder), AIHELPER_DESKTOP_DIR (Windows shortcut folder), AIHELPER_UV (uv binary),
AIHELPER_SKIP_RUNNING_CHECK=1 (don't look for a running Factorio), AIHELPER_RELINK=1 (replace a mod link that points
at another copy), AIHELPER_SUMMARY (where to write the JSON summary; default logs/install-summary.json).
"""
import argparse
import hashlib
import json
import os
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from factorio_claude import platforms  # noqa: E402 - standard library only

MOD_NAME = "claude-bridge"
MOD_SRC = ROOT / "mod" / MOD_NAME
CONFIG = ROOT / "config.json"
LOCK = ROOT / "requirements.lock"
SKILL_SRC = ROOT / "skill" / "factorio" / "SKILL.md"
MARKER = "managed by ai-helper-for-claude"
COPY_MARKER = ".aihelper-copy"
MODS = None  # Factorio's mods folder; resolved lazily (tests may set it)
SHORTCUT = "Factorio (Claude).lnk"
OLD_SHORTCUTS = ["Claude Factorio Chat.lnk"]
APP_NAME = "Factorio (Claude).app"
APP_ID = "io.github.codysoutherland-creator.aihelper"
DESKTOP_FILE = "factorio-claude.desktop"
DEPS = {"mcp": "mcp", "psutil": "psutil", "numpy": "numpy", "scipy": "scipy"}  # scipy: planner LP + ndimage
if os.name == "nt":
    DEPS["win32com"] = "pywin32"

summary = []
results = {}


def note(msg, step=None, status=None):
    """Print one line and record it. status: ok | changed | skipped | warning | failed (default from the text)."""
    summary.append(msg)
    print(msg)
    if step:
        if status is None:
            status = "failed" if "FAILED" in msg else "warning" if "WARNING" in msg else "ok"
        results[step] = {"status": status, "message": msg}


def env_on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _mods() -> Path:
    return Path(MODS) if MODS else platforms.mods_dir(_cfg())


def _cfg() -> dict:
    return platforms.read_config(CONFIG)


def venv_py() -> Path:
    return platforms.venv_python(ROOT)


# ---------------------------------------------------------------- deps

def find_uv():
    for p in (os.environ.get("AIHELPER_UV"), shutil.which("uv"),
              Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")):
        if p and Path(p).is_file():
            return str(p)
    return None


def _lock_hash() -> str:
    return hashlib.sha256(LOCK.read_bytes()).hexdigest()


def deps():
    if env_on("AIHELPER_NO_DEPS"):
        note("deps: skipped (AIHELPER_NO_DEPS)", "deps", "skipped")
        return
    py = str(venv_py())
    stamp = ROOT / ".venv" / ".aihelper-lock-sha256"
    missing = [pkg for mod, pkg in DEPS.items()
               if subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode]
    current = stamp.exists() and stamp.read_text(encoding="utf-8").strip() == _lock_hash()
    if not missing and (current or not stamp.parent.exists()):
        note("deps: ok", "deps")
        return
    uv = find_uv()
    print("deps: installing the pinned Python packages (requirements.lock; the first time takes a minute)...")
    # pinned + hash-checked: a new or tampered release is never picked up silently
    if uv:
        cmd = [uv, "pip", "install", "-q", "--python", py, "--require-hashes", "-r", str(LOCK)]
    else:
        if subprocess.run([py, "-m", "pip", "--version"], capture_output=True).returncode:
            subprocess.run([py, "-m", "ensurepip", "--upgrade"], check=True, capture_output=True)
        cmd = [py, "-m", "pip", "install", "-q", "--require-hashes", "-r", str(LOCK)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"installing requirements.lock failed: {(r.stderr or r.stdout).strip()[-800:]}")
    if stamp.parent.exists():
        stamp.write_text(_lock_hash(), encoding="utf-8")
    if Path(sys.executable).resolve() == Path(py).resolve():
        # packages installed just now (pywin32 adds itself via a .pth file) become importable in this process too
        import importlib
        import site
        import sysconfig
        site.addsitedir(sysconfig.get_paths()["purelib"])
        importlib.invalidate_caches()
    note(f"deps: installed from requirements.lock{' (' + ', '.join(missing) + ' were missing)' if missing else ''}"
         f" via {'uv' if uv else 'pip'}", "deps", "changed")


# ---------------------------------------------------------------- config

def write_private(path, text):
    """Write a file only the current user can read (0600 on macOS/Linux)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


def config():
    """config.json with a random RCON password (secrets) if it has none. New installs keep RCON off ("rcon": false;
    the UDP link does everything except raw Lua). An existing config keeps its values. Values are never printed."""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.exists() else {}
    except ValueError:
        note("config: WARNING config.json is not valid JSON; left as it is", "config")
        return
    changed = []
    if not CONFIG.exists():
        data.update({"host": "127.0.0.1", "port": 27015, "udp_port": 34500, "rcon": False})
        changed.append("created")
    if not data.get("password"):
        data["password"] = secrets.token_urlsafe(24)
        changed.append("generated an RCON password")
    if changed:
        write_private(CONFIG, json.dumps(data, indent=2))
        note(f"config: {', '.join(changed)} (RCON {'on' if data.get('rcon', True) else 'off'})", "config", "changed")
    else:
        if os.name != "nt":
            os.chmod(CONFIG, 0o600)
        note("config: ok", "config")


def node_dir():
    w = shutil.which("node") or platforms.login_shell_lookup("node")
    return str(Path(w).parent) if w else None


def found_paths():
    """macOS/Linux: remember where the game and Claude Code are, plus this shell's PATH, in config.json. An app started
    from Finder or a desktop menu gets a bare PATH and would find neither (npm-installed claude also needs node)."""
    if os.name == "nt":
        game, _ = platforms.find_factorio(_cfg())
        note(f"paths: Factorio {'found at ' + str(game) if game else 'NOT found (set factorio_path in config.json)'}",
             "paths", "ok" if game else "warning")
        return
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        note("paths: WARNING no readable config.json", "paths")
        return
    found = []
    game, kind = platforms.find_factorio(data)
    if game and data.get("factorio_path") != str(game):
        data["factorio_path"] = str(game)
    if game:
        found.append(f"Factorio ({kind})")
    claude = platforms.find_claude(data, ask_login=True)
    if claude:
        data["claude_path"] = str(claude)
        found.append("Claude Code")
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for extra in (str(claude.parent) if claude else None, node_dir()):
        if extra and extra not in dirs:
            dirs.insert(0, extra)
    data["extra_path"] = os.pathsep.join(dict.fromkeys(dirs))
    write_private(CONFIG, json.dumps(data, indent=2))
    missing = [x for x, ok in (("Factorio", game), ("Claude Code", claude)) if not ok]
    msg = f"paths: found {', '.join(found) or 'nothing'}"
    if missing:
        msg += f"; WARNING not found: {', '.join(missing)}"
    note(msg, "paths")


# ---------------------------------------------------------------- the mod

def make_junction(src, link):
    """Directory junction (no admin rights needed) via the stdlib - no cmd.exe re-parsing of the paths."""
    import _winapi
    _winapi.CreateJunction(str(Path(src).resolve()), str(link))


def is_link(p: Path) -> bool:
    p = Path(p)
    return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())


def remove_link(p: Path):
    """Remove a symlink/junction itself, never what it points to."""
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)  # Windows directory junction / directory symlink


def _same(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def create_mod_link(src: Path, link: Path) -> str:
    """-> 'junction' | 'symlink' | 'copy'."""
    if os.name == "nt":
        try:
            make_junction(src, link)
            return "junction"
        except OSError:
            pass
    else:
        try:
            os.symlink(Path(src).resolve(), link, target_is_directory=True)
            return "symlink"
        except OSError:
            pass
    shutil.copytree(src, link)
    (link / COPY_MARKER).write_text("copied by install.py; re-run the installer to update it\n", encoding="utf-8")
    return "copy"


def user_dir_missing() -> bool:
    return MODS is None and not platforms.user_data_dir(_cfg()).exists()


def first_launch_hint() -> str:
    return (f"Factorio's user folder {platforms.user_data_dir(_cfg())} doesn't exist yet: start Factorio once, quit "
            "it, then run the installer again")


def junction():
    """Link mod/claude-bridge into Factorio's mods folder (the step keeps its old name)."""
    if user_dir_missing():
        results["needs_factorio_once"] = True
        note(f"mod link: WARNING {first_launch_hint()}", "mod_link")
        return
    mods = _mods()
    mods.mkdir(parents=True, exist_ok=True)
    link = mods / MOD_NAME
    results["mod_link_path"] = str(link)
    if is_link(link):
        if link.exists() and _same(link, MOD_SRC):
            note("mod link: ok", "mod_link")
            return
        if link.exists() and not env_on("AIHELPER_RELINK"):
            note(f"mod link: WARNING {link} points at another copy ({link.resolve()}); left as it is "
                 "(AIHELPER_RELINK=1 replaces it)", "mod_link")
            return
        remove_link(link)  # broken (moved install) or replacing on request
    elif link.exists():
        if (link / COPY_MARKER).exists():
            shutil.rmtree(link)  # our own copy: refresh it
        else:
            note(f"mod link: WARNING {link} is a real folder, not a link to {MOD_SRC}; left as it is", "mod_link")
            return
    how = create_mod_link(MOD_SRC, link)
    results["mod_link_kind"] = how
    note(f"mod link: created {link} ({how})", "mod_link", "changed")
    zips = sorted(p.name for p in mods.glob(f"{MOD_NAME}_*.zip"))
    if zips:
        note(f"mod link: WARNING also found {', '.join(zips)} in the mods folder; remove it so Factorio loads the "
             "linked copy", "mod_zip")


def factorio_running() -> bool:
    if env_on("AIHELPER_SKIP_RUNNING_CHECK"):
        return False
    try:
        from factorio_claude import companion
        return companion.factorio_running()
    except Exception:  # noqa: BLE001 - psutil missing etc.: don't block the install on it
        return False


def mod_list():
    if user_dir_missing():
        results["needs_factorio_once"] = True
        note(f"mod-list: WARNING {first_launch_hint()}", "mod_list")
        return
    mods = _mods()
    path = mods / "mod-list.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"mods": [
            {"name": "base", "enabled": True}]}
    except ValueError:
        note("mod-list: WARNING mod-list.json is not valid JSON; enable the mod in Factorio's Mods menu", "mod_list")
        return
    entry = next((m for m in data["mods"] if m.get("name") == MOD_NAME), None)
    if entry and entry.get("enabled"):
        note(f"mod-list: {MOD_NAME} enabled", "mod_list")
        return
    if factorio_running():
        # Factorio rewrites mod-list.json when it quits, which would undo this
        note("mod-list: WARNING Factorio is running; quit it and run the installer again (or enable "
             "\"AI Helper (for Claude)\" in the game's Mods menu)", "mod_list")
        return
    backup = mods / "mod-list.json.bak-claude"
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    if entry:
        entry["enabled"] = True
    else:
        data["mods"].append({"name": MOD_NAME, "enabled": True})
    mods.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    note(f"mod-list: enabled {MOD_NAME} (backup: mod-list.json.bak-claude)", "mod_list", "changed")


# ---------------------------------------------------------------- Claude Code: skill + MCP server

def skill_dest() -> Path:
    return platforms.claude_config_dir() / "skills" / "factorio" / "SKILL.md"


def skill_text() -> str:
    return SKILL_SRC.read_text(encoding="utf-8").replace("{{INSTALL_DIR}}", str(ROOT))


def skill():
    if not SKILL_SRC.exists():
        note("skill: WARNING skill/factorio/SKILL.md is missing from this copy", "skill")
        return
    dest, text = skill_dest(), skill_text()
    if dest.exists():
        old = dest.read_text(encoding="utf-8", errors="replace")
        if old == text:
            note("skill: ok", "skill")
            return
        if MARKER not in old:
            dest.with_name("SKILL.md.new").write_text(text, encoding="utf-8")
            note(f"skill: WARNING {dest} was written by someone else; left it, the new one is SKILL.md.new", "skill")
            return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    note(f"skill: installed {dest}", "skill", "changed")


def claude_exe():
    p = platforms.find_claude(_cfg(), ask_login=True)
    return str(p) if p else None


def _norm(s) -> str:
    s = str(s).replace("\\", "/")
    return s.lower() if os.name == "nt" else s


def mcp_registration(exe):
    """-> (registered?, points at this install?)"""
    r = subprocess.run([exe, "mcp", "get", "factorio"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    if r.returncode != 0 or "run_server.py" not in r.stdout:
        return r.returncode == 0, False
    text = _norm(r.stdout)
    return True, _norm(ROOT / "run_server.py") in text and _norm(venv_py()) in text


def mcp_add_cmd(exe):
    return [exe, "mcp", "add", "-s", "user", "factorio", "--", str(venv_py()), str(ROOT / "run_server.py")]


def mcp():
    if env_on("AIHELPER_NO_MCP"):
        note("mcp: skipped (AIHELPER_NO_MCP)", "mcp", "skipped")
        return
    exe = claude_exe()
    if not exe:
        results["claude_missing"] = True
        note("mcp: WARNING Claude Code not found; install it (https://claude.com/claude-code), log in once, then run "
             "the installer again. The free in-game buttons work without it.", "mcp")
        return
    registered, ours = mcp_registration(exe)
    if registered and ours:
        note("mcp: factorio already registered", "mcp")
        return
    if registered:  # another install / an old venv layout: replace it
        subprocess.run([exe, "mcp", "remove", "-s", "user", "factorio"], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    r = subprocess.run(mcp_add_cmd(exe), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, timeout=120)
    if r.returncode:
        raise RuntimeError(f"`claude mcp add` failed: {(r.stderr or r.stdout).strip()[-400:]}")
    note(f"mcp: {'re-registered' if registered else 'registered'} factorio (user scope)", "mcp", "changed")


# ---------------------------------------------------------------- launcher shortcut / app

def factorio_exe():
    """The game binary (shortcut icon), or None."""
    game, _ = platforms.find_factorio(_cfg())
    return game


def apps_dir() -> Path:
    env = os.environ.get("AIHELPER_APPS_DIR")
    if env:
        return Path(env).expanduser()
    if platforms.IS_MAC:
        system = Path("/Applications")
        return system if system.is_dir() and os.access(system, os.W_OK) else Path.home() / "Applications"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"


def launch_script() -> str:
    """bash script the macOS app / Linux menu entry runs: absolute paths, the install-time PATH."""
    path = _cfg().get("extra_path") or os.environ.get("PATH", "/usr/bin:/bin")
    logs = ROOT / "logs"
    q = shlex.quote
    return ("#!/bin/bash\n"
            f"# {MARKER}: created by install.py; run the installer again to refresh it.\n"
            f"export PATH={q(path)}\n"
            "export PYTHONUTF8=1\n"
            f"mkdir -p {q(str(logs))}\n"
            f"exec {q(str(venv_py()))} {q(str(ROOT / 'launcher.py'))} >>{q(str(logs / 'app.log'))} 2>&1\n")


def mac_icon(resources: Path):
    """Copy factorio.app's icon into our bundle -> icon file name, or None."""
    game = factorio_exe()
    bundle = platforms.mac_app_bundle(game) if game else None
    if not bundle:
        return None
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            name = plistlib.load(f).get("CFBundleIconFile")
        if not name:
            return None
        src = bundle / "Contents" / "Resources" / (name if name.endswith(".icns") else name + ".icns")
        if src.exists():
            shutil.copy2(src, resources / "icon.icns")
            return "icon"
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return None


def make_mac_app(dest_dir: Path) -> Path:
    app = dest_dir / APP_NAME
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": "Factorio (Claude)", "CFBundleDisplayName": "Factorio (Claude)",
        "CFBundleIdentifier": APP_ID, "CFBundlePackageType": "APPL", "CFBundleExecutable": "launch",
        "CFBundleShortVersionString": mod_version(), "CFBundleVersion": mod_version(),
        "CFBundleInfoDictionaryVersion": "6.0", "LSUIElement": True, "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }
    icon = mac_icon(contents / "Resources")
    if icon:
        info["CFBundleIconFile"] = icon
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    exe = contents / "MacOS" / "launch"
    exe.write_text(launch_script(), encoding="utf-8")
    os.chmod(exe, 0o755)
    os.utime(app)  # Finder picks up the new bundle / icon
    return app


def make_linux_entry(dest_dir: Path) -> Path:
    script = ROOT / "launch-factorio-claude.sh"
    script.write_text(launch_script(), encoding="utf-8")
    os.chmod(script, 0o755)
    dest_dir.mkdir(parents=True, exist_ok=True)
    game = factorio_exe()
    icon = None
    if game:
        for cand in (Path(game).parents[2] / "data" / "core" / "graphics" / "factorio.png",
                     Path(game).parents[2] / "data" / "core" / "graphics" / "factorio-icon.png"):
            if cand.exists():
                icon = cand
                break
    entry = dest_dir / DESKTOP_FILE
    entry.write_text("[Desktop Entry]\nType=Application\nName=Factorio (Claude)\n"
                     f"Comment=Factorio with the AI Helper companion ({MARKER})\n"
                     f'Exec="{script}"\nIcon={icon or "applications-games"}\nTerminal=false\nCategories=Game;\n',
                     encoding="utf-8")
    os.chmod(entry, 0o755)
    return entry


def windows_desktop() -> Path:
    env = os.environ.get("AIHELPER_DESKTOP_DIR")
    if env:
        return Path(env).expanduser()
    import win32com.client
    return Path(win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop"))


def shortcuts():
    if env_on("AIHELPER_NO_SHORTCUT"):
        note("shortcut: skipped (AIHELPER_NO_SHORTCUT)", "shortcut", "skipped")
        return
    if platforms.IS_MAC:
        app = make_mac_app(apps_dir())
        results["app_path"] = str(app)
        note(f"app: {app}", "shortcut", "changed")
        return
    if not platforms.IS_WIN:
        entry = make_linux_entry(apps_dir())
        results["app_path"] = str(entry)
        note(f"menu entry: {entry}", "shortcut", "changed")
        return
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = windows_desktop()
    desktop.mkdir(parents=True, exist_ok=True)
    lnk = shell.CreateShortcut(str(desktop / SHORTCUT))
    lnk.TargetPath = str(platforms.venv_python(ROOT, gui=True))
    lnk.Arguments = f'"{ROOT / "launcher.pyw"}"'
    lnk.WorkingDirectory = str(ROOT)
    lnk.Description = "Factorio with Claude (starts the game and the hidden Claude companion)"
    icon = factorio_exe()
    if icon:
        lnk.IconLocation = f"{icon},0"
    lnk.Save()
    results["app_path"] = str(desktop / SHORTCUT)
    note(f"shortcut: {desktop / SHORTCUT} -> pythonw launcher.pyw", "shortcut", "changed")
    for old in OLD_SHORTCUTS:
        p = desktop / old
        if p.exists():
            p.unlink()
            note(f"shortcut: removed old {old}")


def launcher_path():
    """Where this install's shortcut / app / menu entry is (whether or not it exists)."""
    if platforms.IS_MAC:
        return apps_dir() / APP_NAME
    if platforms.IS_WIN:
        try:
            return windows_desktop() / SHORTCUT
        except Exception:  # noqa: BLE001
            return None
    return apps_dir() / DESKTOP_FILE


def mod_version() -> str:
    try:
        return json.loads((MOD_SRC / "info.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "0"


# ---------------------------------------------------------------- --check / --uninstall

def check() -> int:
    """Report the state of every step; change nothing. -> 0 when everything needed is in place."""
    rows = []

    def row(name, ok, detail, needed=True):
        rows.append((name, "ok" if ok else ("PROBLEM" if needed else "note"), detail))

    py = venv_py()
    missing = [pkg for mod, pkg in DEPS.items()
               if subprocess.run([str(py), "-c", f"import {mod}"], capture_output=True).returncode]
    row("python", not missing, f"{py}" + (f" (missing: {', '.join(missing)})" if missing else ""))
    cfg = _cfg()
    row("config", bool(cfg.get("password")), str(CONFIG) if CONFIG.exists() else "config.json missing")
    if os.name != "nt" and CONFIG.exists():
        mode = CONFIG.stat().st_mode & 0o777
        row("config perms", mode == 0o600, oct(mode))
    ud = platforms.user_data_dir(cfg)
    row("factorio user dir", ud.exists() or MODS is not None, str(ud))
    link = _mods() / MOD_NAME
    row("mod link", link.exists() and (_same(link, MOD_SRC) or (link / COPY_MARKER).exists()), str(link))
    try:
        ml = json.loads((_mods() / "mod-list.json").read_text(encoding="utf-8-sig"))
        on = any(m.get("name") == MOD_NAME and m.get("enabled") for m in ml.get("mods", []))
    except (OSError, ValueError):
        on = False
    row("mod-list", on, f"{MOD_NAME} {'enabled' if on else 'not enabled'}")
    game, kind = platforms.find_factorio(cfg)
    row("factorio", bool(game), f"{game} ({kind})" if game else "not found (set factorio_path in config.json)")
    row("skill", skill_dest().exists(), str(skill_dest()))
    exe = claude_exe()
    row("claude", bool(exe), exe or "Claude Code not found")
    if exe and not env_on("AIHELPER_NO_MCP"):
        try:
            registered, ours = mcp_registration(exe)
        except (OSError, subprocess.SubprocessError):
            registered = ours = False
        row("mcp", ours, "registered" if ours else ("registered for another copy" if registered else "not registered"))
    lp = launcher_path()
    row("launcher", bool(lp and lp.exists()), str(lp), needed=not env_on("AIHELPER_NO_SHORTCUT"))
    width = max(len(r[0]) for r in rows)
    for name, state, detail in rows:
        print(f"{name.ljust(width)}  {state:7}  {detail}")
    bad = [r for r in rows if r[1] == "PROBLEM"]
    print(f"\n{'Everything is set up.' if not bad else str(len(bad)) + ' problem(s); run the installer again.'}")
    return 1 if bad else 0


def uninstall() -> int:
    """Undo the install steps (only what this install made). Leaves ROOT (config, logs, blueprints) in place."""
    lp = launcher_path()
    if lp and lp.exists():
        if platforms.IS_MAC:
            try:
                with open(lp / "Contents" / "Info.plist", "rb") as f:
                    ours = plistlib.load(f).get("CFBundleIdentifier") == APP_ID
            except (OSError, ValueError, plistlib.InvalidFileException):
                ours = False
            if ours:
                shutil.rmtree(lp)
                note(f"app: removed {lp}", "shortcut", "changed")
        else:
            lp.unlink()
            note(f"shortcut: removed {lp}", "shortcut", "changed")
    exe = claude_exe()
    if exe and not env_on("AIHELPER_NO_MCP"):
        registered, ours = mcp_registration(exe)
        if ours:
            subprocess.run([exe, "mcp", "remove", "-s", "user", "factorio"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=120)
            note("mcp: removed factorio", "mcp", "changed")
        elif registered:
            note("mcp: factorio is registered for another copy; left it", "mcp")
    dest = skill_dest()
    if dest.exists() and MARKER in dest.read_text(encoding="utf-8", errors="replace"):
        dest.unlink()
        dest.with_name("SKILL.md.new").unlink(missing_ok=True)
        try:
            dest.parent.rmdir()
        except OSError:
            pass
        note(f"skill: removed {dest}", "skill", "changed")
    mods = _mods()
    link = mods / MOD_NAME
    if factorio_running():
        note("mod: WARNING Factorio is running; quit it and run --uninstall again to remove the mod", "mod_link")
    else:
        if is_link(link) and (not link.exists() or _same(link, MOD_SRC)):
            remove_link(link)
            note(f"mod link: removed {link}", "mod_link", "changed")
        elif link.is_dir() and (link / COPY_MARKER).exists():
            shutil.rmtree(link)
            note(f"mod copy: removed {link}", "mod_link", "changed")
        path = mods / "mod-list.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            kept = [m for m in data.get("mods", []) if m.get("name") != MOD_NAME]
            if len(kept) != len(data.get("mods", [])):
                data["mods"] = kept
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                note(f"mod-list: removed {MOD_NAME}", "mod_list", "changed")
        except (OSError, ValueError):
            pass
    print(f"\nUninstalled. This folder is still here (with your config, logs and blueprints): {ROOT}\n"
          "Delete it to remove the app completely.")
    return 1 if any("FAILED" in s for s in summary) else 0


# ---------------------------------------------------------------- main

def write_summary(mode):
    path = Path(os.environ.get("AIHELPER_SUMMARY") or ROOT / "logs" / "install-summary.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"mode": mode, "platform": sys.platform, "root": str(ROOT), "mods_dir": str(_mods()),
                "python": str(venv_py()), "skill": str(skill_dest()), **results}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"(summary not written: {e})")


def next_steps():
    lines = ["", "Done."]
    if results.get("needs_factorio_once"):
        lines.append("Next: start Factorio once (so it creates its user folder), quit it, then run the installer "
                     "again.")
    if results.get("claude_missing"):
        lines.append("Claude Code wasn't found: install it and log in (run `claude` once in a terminal), then run the "
                     "installer again so Ask Claude works.")
    app = results.get("app_path")
    if platforms.IS_MAC:
        lines.append(f"To play: open \"Factorio (Claude)\" ({app or apps_dir() / APP_NAME}); Spotlight and Launchpad "
                     "find it too. It starts Factorio with the Claude link and the hidden companion.")
    elif platforms.IS_WIN:
        lines.append("To play: double-click 'Factorio (Claude)' on the desktop.")
    else:
        lines.append(f"To play: start \"Factorio (Claude)\" from your applications menu ({app or 'not created'}).")
    lines.append("In Factorio, check that \"AI Helper (for Claude)\" is enabled under Mods, then load or start a game.")
    print("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install, check or remove AI Helper (for Claude).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="report what is set up; change nothing")
    g.add_argument("--uninstall", action="store_true", help="remove the mod link, MCP registration, skill and app")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if a.uninstall:
        rc = uninstall()
        write_summary("uninstall")
        return rc
    if Path(sys.executable).resolve() != venv_py().resolve():
        print(f"note: run this with {venv_py()} (continuing)")
    for step in (deps, config, found_paths, junction, mod_list, skill, mcp, shortcuts):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            note(f"{step.__name__}: FAILED {e}", step.__name__, "failed")
    write_summary("install")
    next_steps()
    return 1 if any("FAILED" in s for s in summary) else 0


if __name__ == "__main__":
    sys.exit(main())
