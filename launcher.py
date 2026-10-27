"""One-click start for Windows, macOS and Linux. The "Factorio (Claude)" desktop shortcut (Windows: pythonw
launcher.pyw), app (macOS: Factorio (Claude).app) or menu entry (Linux) runs this. It starts Factorio with the
UDP/RCON launch flags unless it's already running, then starts the hidden companion (factorio_claude/companion.py)
unless it's already running, and exits.

    python launcher.py              # what the shortcut / app does
    python launcher.py --dry-run    # print what it would do, start nothing

How the game is started (the binary comes from env FACTORIO_BIN, config "factorio_path" (saved by install.py), the
Steam libraries, then the standard install places):
- Steam copy, Steam running: the game binary directly, with SteamAppId/SteamGameId set (no Steam relaunch, no
  "launch with custom arguments?" prompt). Steam not running: `steam -applaunch 427520 <flags>`.
- Standalone (factorio.com) copy: the binary directly.
- macOS: the binary inside factorio.app (`open -a` would drop the flags when the app already runs); when Steam must
  start first, `steam_osx -applaunch` (Steam may ask once whether to allow the custom arguments).
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from factorio_claude import companion, link, platforms, rcon  # noqa: E402

FACTORIO_APPID = platforms.FACTORIO_APPID
LOG = ROOT / "logs" / "launcher.log"
IDOK = platforms.IDOK


def out(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    if sys.stdout is not None:
        try:
            print(msg)
        except OSError:
            pass


def popup(msg, kind="error", okcancel=False):
    """Message box (no console when started from a shortcut / app). Returns IDOK, 2 (Cancel) or None."""
    return platforms.popup(msg, kind=kind, okcancel=okcancel)


NO_LINK = ("Factorio is already running, but it was started without the Claude link (e.g. from the Steam library), "
           "so Claude can't talk to it.\n\nSave and quit Factorio, then click OK and I'll start it again with the "
           "link.\nCancel leaves it as it is (the free in-game buttons still work).")
NO_GAME = ("Couldn't find Factorio. Install Factorio 2.1 (Steam or factorio.com) and start it once, or put the "
           "game's path into config.json as \"factorio_path\".")


def unlinked_game(cmdlines):
    """True if Factorio runs but none of its instances has the UDP flag (unreadable command lines count as linked)."""
    return bool(cmdlines) and all(cl and "--enable-lua-udp" not in cl for cl in cmdlines)


def wait_for_quit(timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not companion.factorio_running():
            return True
        time.sleep(1)
    return False


# kept for older callers / tests
def steam_exe():
    p = platforms.find_steam()
    return str(p) if p else None


def steam_running():
    return platforms.steam_running()


def game_flags(cfg):
    """Launch flags: the local UDP link, plus RCON only when enabled (opt-in) and then bound to 127.0.0.1 only -
    `--rcon-port` would listen on every network interface."""
    flags = ["--enable-lua-udp", str(int(cfg["udp_port"]))]
    if rcon.rcon_enabled(cfg):
        flags += ["--rcon-bind", f"127.0.0.1:{int(cfg['port'])}", "--rcon-password", cfg["password"]]
    return flags


def steam_env():
    return dict(os.environ, SteamAppId=FACTORIO_APPID, SteamGameId=FACTORIO_APPID)


LOOKUP = object()


def game_cmd(cfg, exe=LOOKUP, kind=None, steam=LOOKUP, steam_is_running=None):
    """-> (cmd, env or None, cwd or None), or (None, None, None) when neither Factorio nor Steam is found.
    The keyword arguments are for tests (None = not installed); normally everything is looked up."""
    if exe is LOOKUP:
        exe, kind = platforms.find_factorio(cfg)
    if steam is LOOKUP:
        steam = platforms.find_steam()
    flags = game_flags(cfg)
    if exe and kind != "steam":
        return [str(exe)] + flags, None, Path(exe).parent
    if exe:  # the Steam copy
        running = platforms.steam_running() if steam_is_running is None else steam_is_running
        if running or not steam:
            # with Steam up the game runs directly (SteamAppId: no relaunch); without a Steam client at all, try anyway
            return [str(exe)] + flags, steam_env(), Path(exe).parent
    if steam:
        return [str(steam), "-applaunch", FACTORIO_APPID] + flags, None, Path(steam).parent
    return None, None, None


def companion_cmd():
    return [str(platforms.venv_python(ROOT, gui=True)), "-m", "factorio_claude.companion"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be started, start nothing")
    a = ap.parse_args(argv)
    dry = a.dry_run
    cfg = link.config()
    platforms.merge_path(cfg.get("extra_path"))  # PATH saved at install time (a Finder-started app has a bare one)

    running = companion.factorio_cmdlines()
    if unlinked_game(running):
        out("Factorio is running without --enable-lua-udp (not started by this launcher).")
        # never kill it (unsaved progress): ask the player to quit, then relaunch with the flags
        while not dry and running:
            if popup(NO_LINK, "warning", okcancel=True) != IDOK:
                out("left the unlinked game running")
                break
            if wait_for_quit(20):
                running = []
    if running:
        if not unlinked_game(running):
            out("Factorio is already running - not starting it.")
    else:
        cmd, env, cwd = game_cmd(cfg)
        if not cmd:
            out("Factorio not found.")
            if not dry:
                popup(NO_GAME)
            return 1
        shown = [c if c != cfg.get("password") else "<rcon password>" for c in cmd]
        out(("would start: " if dry else "starting: ") + platforms.cmdline(shown))
        if not dry:
            platforms.spawn_detached(cmd, cwd=cwd, env=env, console=True)

    if companion.companion_running():
        out(f"Companion already running (lock port {companion.lock_port()}).")
    else:
        cmd = companion_cmd()
        out(("would start companion: " if dry else "starting companion: ") + platforms.cmdline(cmd))
        if not dry:
            platforms.spawn_detached(cmd, cwd=ROOT, env=os.environ.copy())
    if not companion.claude_available():
        out(f"Claude Code CLI not found ({companion.claude_exe()}).")
        if not dry:
            popup("Claude Code isn't installed (or wasn't found), so 'Ask Claude' won't work. The free in-game "
                  "buttons still do. Install Claude Code, then run the installer again.", "warning")
    return 0


def run():
    """Entry point for launcher.pyw and the macOS/Linux app: errors become a message box (there is no console)."""
    try:
        return main()
    except Exception as e:  # noqa: BLE001 - no console, so say it in a box
        out(f"launcher failed: {e!r}")
        popup(f"Factorio (Claude) launcher failed:\n{e}\n\nSee {LOG}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
