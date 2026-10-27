"""Where things are on this computer (Windows, macOS, Linux). Standard library only, safe to import from install.py
before any dependency is installed. Everything is resolved lazily, so tests and sandboxes can set the env first.

Env overrides (testing / unusual setups):
    FACTORIO_USER_DIR   Factorio's user data dir (mods/, mod-list.json, script-output/); config "factorio_user_dir"
    FACTORIO_BIN        the Factorio game binary; config "factorio_path"
    CLAUDE_BIN          the Claude Code CLI; config "claude_path"
    AIHELPER_HOME       where the one-line installers put this app (default: default_install_dir())
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
APP_NAME = "ai-helper-for-claude"
DISPLAY_NAME = "Factorio (Claude)"
FACTORIO_APPID = "427520"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# process names of the game client (Windows: factorio.exe; macOS app bundle and Linux: factorio)
FACTORIO_PROCESS_NAMES = {"factorio.exe", "factorio"}
STEAM_PROCESS_NAMES = {"steam.exe", "steam_osx", "steam"}


def home() -> Path:
    return Path.home()


def read_config(path: Path | None = None) -> dict:
    """config.json as a dict ({} when missing or broken). utf-8-sig: PowerShell-written files may carry a BOM."""
    try:
        data = json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- Factorio user data

def default_user_data_dir() -> Path:
    """Factorio's write-data dir for this OS (https://wiki.factorio.com/Application_directory)."""
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or home() / "AppData" / "Roaming") / "Factorio"
    if IS_MAC:
        return home() / "Library" / "Application Support" / "factorio"
    return home() / ".factorio"


def user_data_dir(cfg: dict | None = None) -> Path:
    """env FACTORIO_USER_DIR -> config "factorio_user_dir" -> the OS default."""
    env = os.environ.get("FACTORIO_USER_DIR")
    if env:
        return Path(env).expanduser()
    cfg = read_config() if cfg is None else cfg
    if cfg.get("factorio_user_dir"):
        return Path(cfg["factorio_user_dir"]).expanduser()
    return default_user_data_dir()


def mods_dir(cfg: dict | None = None) -> Path:
    return user_data_dir(cfg) / "mods"


def script_output_dir(cfg: dict | None = None) -> Path:
    return user_data_dir(cfg) / "script-output"


# ---------------------------------------------------------------- this app

def default_install_dir() -> Path:
    """Where the one-line installers put the app (never under Documents/Desktop: macOS privacy prompts)."""
    env = os.environ.get("AIHELPER_HOME")
    if env:
        return Path(env).expanduser()
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or home() / "AppData" / "Local") / APP_NAME
    if IS_MAC:
        return home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local" / "share") / APP_NAME


def venv_python(root: Path = ROOT, gui: bool = False) -> Path:
    """The venv's python. gui=True on Windows: pythonw.exe (no console window) when present.
    Falls back to the running interpreter (or its python.exe twin) when there is no venv."""
    venv = Path(root) / ".venv"
    if IS_WIN:
        pyw, py = venv / "Scripts" / "pythonw.exe", venv / "Scripts" / "python.exe"
        if gui and pyw.exists():
            return pyw
        if py.exists():
            return py
    else:
        py = venv / "bin" / "python"
        if py.exists():
            return py
    exe = Path(sys.executable)
    if IS_WIN and not gui and exe.name.lower() == "pythonw.exe":
        return exe.with_name("python.exe")
    if IS_WIN and gui and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        return exe.with_name("pythonw.exe")
    return exe


def claude_config_dir() -> Path:
    """~/.claude (or CLAUDE_CONFIG_DIR): where Claude Code keeps user skills."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else home() / ".claude"


def cmdline(cmd) -> str:
    """A command list as this OS's shell would show it (for logs only)."""
    import shlex
    return subprocess.list2cmdline([str(c) for c in cmd]) if IS_WIN else shlex.join(str(c) for c in cmd)


# ---------------------------------------------------------------- Steam + Factorio

def _vdf_paths(vdf: Path) -> list[Path]:
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]


def steam_roots() -> list[Path]:
    """Steam install dirs (each holds steamapps/)."""
    roots = []
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
        roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    elif IS_MAC:
        roots.append(home() / "Library" / "Application Support" / "Steam")
    else:
        roots += [home() / ".local" / "share" / "Steam", home() / ".steam" / "steam",
                  home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"]
    out = []
    for r in roots:
        if r.exists() and all(r.resolve() != o.resolve() for o in out):
            out.append(r)
    return out


def steam_libraries() -> list[Path]:
    libs = []
    for root in steam_roots():
        for lib in [root] + _vdf_paths(root / "steamapps" / "libraryfolders.vdf"):
            if lib not in libs:
                libs.append(lib)
    return libs


def _factorio_in_library(lib: Path) -> Path:
    game = Path(lib) / "steamapps" / "common" / "Factorio"
    if IS_WIN:
        return game / "bin" / "x64" / "factorio.exe"
    if IS_MAC:
        return game / "factorio.app" / "Contents" / "MacOS" / "factorio"
    return game / "bin" / "x64" / "factorio"


def factorio_kind(path) -> str:
    """'steam' for a binary inside a Steam library, else 'standalone'."""
    return "steam" if "steamapps" in str(path).replace("\\", "/").lower().split("/") else "standalone"


def factorio_candidates() -> list[Path]:
    """Every place the game binary may be, most likely first (not checked for existence)."""
    c = [_factorio_in_library(lib) for lib in steam_libraries()]
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramW6432"):
            if os.environ.get(env):
                c.append(Path(os.environ[env]) / "Factorio" / "bin" / "x64" / "factorio.exe")
    elif IS_MAC:
        c += [Path("/Applications/factorio.app/Contents/MacOS/factorio"),
              home() / "Applications" / "factorio.app" / "Contents" / "MacOS" / "factorio"]
    else:
        c += [home() / "factorio" / "bin" / "x64" / "factorio", Path("/opt/factorio/bin/x64/factorio")]
    return c


def find_factorio(cfg: dict | None = None):
    """-> (binary Path, 'steam'|'standalone') or (None, None). env FACTORIO_BIN (or FACTORIO_EXE) -> config
    "factorio_path" -> Steam libraries -> standalone install locations."""
    cfg = read_config() if cfg is None else cfg
    for p in (os.environ.get("FACTORIO_BIN"), os.environ.get("FACTORIO_EXE"), cfg.get("factorio_path")):
        if p and Path(p).expanduser().is_file():
            p = Path(p).expanduser()
            return p, factorio_kind(p)
    for p in factorio_candidates():
        if p.is_file():
            return p, factorio_kind(p)
    return None, None


def mac_app_bundle(binary) -> Path | None:
    """factorio.app for a .../factorio.app/Contents/MacOS/factorio binary."""
    p = Path(binary)
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def find_steam() -> Path | None:
    """The Steam client binary (steam.exe / steam_osx / steam)."""
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                p = Path(winreg.QueryValueEx(k, "SteamExe")[0])
                if p.exists():
                    return p
        except OSError:
            pass
        p = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steam.exe"
        return p if p.exists() else None
    if IS_MAC:
        for app in (Path("/Applications/Steam.app"), home() / "Applications" / "Steam.app"):
            p = app / "Contents" / "MacOS" / "steam_osx"
            if p.exists():
                return p
        return None
    w = shutil.which("steam")
    return Path(w) if w else None


def steam_running() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    return any((p.info["name"] or "").lower() in STEAM_PROCESS_NAMES for p in psutil.process_iter(["name"]))


def is_factorio_process(name: str | None, exe: str | None = None) -> bool:
    if (name or "").lower() in FACTORIO_PROCESS_NAMES:
        return True
    return bool(exe) and Path(exe).name.lower() in FACTORIO_PROCESS_NAMES


# ---------------------------------------------------------------- Claude Code CLI

def claude_candidates() -> list[Path]:
    h = home()
    if IS_WIN:
        c = [h / ".local" / "bin" / "claude.exe"]
        if os.environ.get("APPDATA"):
            c.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")
        return c
    return [h / ".local" / "bin" / "claude", h / ".claude" / "local" / "claude", Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"), h / ".npm-global" / "bin" / "claude"]


def login_shell_lookup(command: str, timeout: float = 10) -> str | None:
    """Ask the user's login shell (macOS/Linux) for `command -v <command>`: a GUI-started process has a bare PATH."""
    if IS_WIN:
        return None
    shell = os.environ.get("SHELL") or ("/bin/zsh" if IS_MAC else "/bin/bash")
    if not Path(shell).exists():
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", command):
        return None
    for flags in ("-lc", "-ilc"):
        try:
            r = subprocess.run([shell, flags, f"command -v {command}"], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("/")]
        if r.returncode == 0 and lines and Path(lines[-1]).is_file():
            return lines[-1]
    return None


def find_claude(cfg: dict | None = None, ask_login: bool = False) -> Path | None:
    """env CLAUDE_BIN -> PATH -> config "claude_path" -> the usual install places [-> the login shell's PATH]."""
    env = os.environ.get("CLAUDE_BIN")
    if env:
        return Path(env).expanduser() if Path(env).expanduser().is_file() else None
    w = shutil.which("claude")
    if w:
        return Path(w)
    cfg = read_config() if cfg is None else cfg
    if cfg.get("claude_path") and Path(cfg["claude_path"]).expanduser().is_file():
        return Path(cfg["claude_path"]).expanduser()
    for p in claude_candidates():
        if p.is_file():
            return p
    if ask_login:
        found = login_shell_lookup("claude")
        if found:
            return Path(found)
    return None


def merge_path(extra: str | None):
    """Prepend saved PATH entries (from install time) that this process lacks, e.g. when started from Finder."""
    if not extra:
        return
    have = os.environ.get("PATH", "").split(os.pathsep)
    add = [p for p in str(extra).split(os.pathsep) if p and p not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + [p for p in have if p])


# ---------------------------------------------------------------- processes and UI

def spawn_detached(cmd, cwd=None, env=None, console=False):
    """Start a process that outlives us. Windows: detached, own process group, no console window unless console.
    macOS/Linux: its own session (start_new_session), stdio to /dev/null."""
    kw = {"cwd": str(cwd) if cwd else None, "env": env, "close_fds": True, "stdin": subprocess.DEVNULL,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if not console:
            flags |= subprocess.CREATE_NO_WINDOW
        return subprocess.Popen([str(c) for c in cmd], creationflags=flags, **kw)
    return subprocess.Popen([str(c) for c in cmd], start_new_session=True, **kw)


IDOK = 1


def popup(msg: str, title: str = DISPLAY_NAME, kind: str = "error", okcancel: bool = False):
    """A message box (GUI launches have no console). kind: error | warning | info.
    Returns IDOK (1) for OK, 2 for Cancel, None if no box could be shown."""
    try:
        if IS_WIN:
            import ctypes
            icon = {"error": 0x10, "warning": 0x30}.get(kind, 0x40)
            return ctypes.windll.user32.MessageBoxW(None, msg, title, icon | (0x01 if okcancel else 0) | 0x40000)
        if IS_MAC:
            buttons = '{"Cancel", "OK"}' if okcancel else '{"OK"}'
            icon = {"error": "stop", "warning": "caution"}.get(kind, "note")
            script = ("on run argv\n"
                      f'display dialog (item 1 of argv) with title (item 2 of argv) buttons {buttons} '
                      f'default button "OK" with icon {icon}\n'
                      "end run")
            r = subprocess.run(["/usr/bin/osascript", "-", msg, title], input=script, text=True,
                               capture_output=True, timeout=3600)
            return IDOK if r.returncode == 0 else 2
        for tool in ("zenity", "kdialog"):
            if shutil.which(tool):
                if tool == "zenity":
                    args = ["--question" if okcancel else f"--{'error' if kind == 'error' else kind}",
                            "--title", title, "--text", msg]
                else:
                    args = ["--title", title, "--yesno" if okcancel else "--msgbox", msg]
                r = subprocess.run([tool] + args, capture_output=True, timeout=3600)
                return IDOK if r.returncode == 0 else 2
    except Exception:  # noqa: BLE001 - a missing dialog must never break the launcher
        return None
    return None


def clipboard_cmd() -> list | None:
    """The OS clipboard command (reads stdin), or None."""
    if IS_WIN:
        return ["clip"]
    if IS_MAC:
        return ["/usr/bin/pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "-ib"]
    return None
