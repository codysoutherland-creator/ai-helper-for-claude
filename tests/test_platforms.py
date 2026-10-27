"""Cross-platform paths, game/Claude discovery, the launcher's game command and the installer steps (all sandboxed:
tmp dirs, env overrides and a fake claude CLI; nothing touches a real profile or game)."""
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from factorio_claude import companion, platforms

ROOT = Path(__file__).resolve().parent.parent


def _os(monkeypatch, name):
    monkeypatch.setattr(platforms, "IS_WIN", name == "win")
    monkeypatch.setattr(platforms, "IS_MAC", name == "mac")
    monkeypatch.setattr(platforms, "IS_LINUX", name == "linux")


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- paths

def test_user_data_dir_per_os(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORIO_USER_DIR", raising=False)
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    _os(monkeypatch, "mac")
    assert platforms.user_data_dir({}) == tmp_path / "Library" / "Application Support" / "factorio"
    assert platforms.mods_dir({}) == tmp_path / "Library" / "Application Support" / "factorio" / "mods"
    _os(monkeypatch, "linux")
    assert platforms.script_output_dir({}) == tmp_path / ".factorio" / "script-output"
    _os(monkeypatch, "win")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert platforms.user_data_dir({}) == tmp_path / "Roaming" / "Factorio"
    # config, then env win
    assert platforms.user_data_dir({"factorio_user_dir": str(tmp_path / "c")}) == tmp_path / "c"
    monkeypatch.setenv("FACTORIO_USER_DIR", str(tmp_path / "e"))
    assert platforms.user_data_dir({"factorio_user_dir": str(tmp_path / "c")}) == tmp_path / "e"


def test_default_install_dir_per_os(monkeypatch, tmp_path):
    monkeypatch.delenv("AIHELPER_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    _os(monkeypatch, "mac")
    assert platforms.default_install_dir() == tmp_path / "Library" / "Application Support" / "ai-helper-for-claude"
    _os(monkeypatch, "linux")
    assert platforms.default_install_dir() == tmp_path / ".local" / "share" / "ai-helper-for-claude"
    monkeypatch.setenv("AIHELPER_HOME", str(tmp_path / "x"))
    assert platforms.default_install_dir() == tmp_path / "x"


def test_venv_python_layouts(monkeypatch, tmp_path):
    _os(monkeypatch, "linux")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert platforms.venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"
    assert platforms.venv_python(tmp_path, gui=True) == tmp_path / ".venv" / "bin" / "python"  # no pythonw on POSIX
    _os(monkeypatch, "win")
    s = tmp_path / ".venv" / "Scripts"
    s.mkdir()
    (s / "python.exe").write_text("")
    assert platforms.venv_python(tmp_path) == s / "python.exe"
    assert platforms.venv_python(tmp_path, gui=True) == s / "python.exe"
    (s / "pythonw.exe").write_text("")
    assert platforms.venv_python(tmp_path, gui=True) == s / "pythonw.exe"


def test_find_factorio_env_config_and_kind(monkeypatch, tmp_path):
    steam = tmp_path / "Steam" / "steamapps" / "common" / "Factorio" / "factorio.app" / "Contents" / "MacOS"
    steam.mkdir(parents=True)
    (steam / "factorio").write_text("")
    alone = tmp_path / "factorio.app" / "Contents" / "MacOS"
    alone.mkdir(parents=True)
    (alone / "factorio").write_text("")
    monkeypatch.setenv("FACTORIO_BIN", str(alone / "factorio"))
    assert platforms.find_factorio({}) == (alone / "factorio", "standalone")
    monkeypatch.delenv("FACTORIO_BIN")
    assert platforms.find_factorio({"factorio_path": str(steam / "factorio")}) == (steam / "factorio", "steam")
    assert platforms.mac_app_bundle(steam / "factorio").name == "factorio.app"


def test_mac_steam_library_layout(monkeypatch, tmp_path):
    _os(monkeypatch, "mac")
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    root = tmp_path / "Library" / "Application Support" / "Steam"
    other = tmp_path / "ExtLib"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n "1"\n {{\n "path" "{other}"\n }}\n}}')
    exe = other / "steamapps" / "common" / "Factorio" / "factorio.app" / "Contents" / "MacOS" / "factorio"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.delenv("FACTORIO_BIN", raising=False)
    assert platforms.find_factorio({}) == (exe, "steam")


def test_find_claude_order(monkeypatch, tmp_path):
    fake = tmp_path / "claude-x"
    fake.write_text("")
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    assert platforms.find_claude({}) == fake
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "missing"))
    assert platforms.find_claude({}) is None  # an explicit override is never second-guessed
    monkeypatch.delenv("CLAUDE_BIN")
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None)
    monkeypatch.setattr(platforms, "claude_candidates", lambda: [])
    assert platforms.find_claude({"claude_path": str(fake)}) == fake
    assert platforms.find_claude({}) is None


def test_merge_path_prepends_missing_entries(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/a", "/b"]))
    platforms.merge_path(os.pathsep.join(["/c", "/a"]))
    assert os.environ["PATH"].split(os.pathsep) == ["/c", "/a", "/b"]


def test_companion_sees_the_mac_and_linux_process_name(monkeypatch):
    class P:
        def __init__(self, name, cmd, exe=None):
            self.info = {"name": name, "cmdline": cmd, "exe": exe}

    procs = [P("factorio", ["/Applications/factorio.app/Contents/MacOS/factorio", "--enable-lua-udp", "34500"]),
             P("factorio.exe", [r"C:\Games\Factorio\bin\x64\factorio.exe"]),
             P("factorio", ["/x/testenv/inst/a/factorio", "--start-server"]),  # a test server: never counts
             P("python", ["python", "factorio.py"])]
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.delenv("FACTORIO_GAME_MATCH", raising=False)
    got = companion.factorio_cmdlines()
    assert len(got) == 2 and "--enable-lua-udp" in got[0]


# ---------------------------------------------------------------- launcher

@pytest.fixture()
def lau():
    return _load("launcher_mod_under_test", "launcher.py")


CFG = {"udp_port": 34500, "port": 27015, "password": "pw", "rcon": False}


def test_game_cmd_standalone_runs_the_binary(lau, tmp_path):
    exe = tmp_path / "factorio"
    cmd, env, cwd = lau.game_cmd(CFG, exe=exe, kind="standalone", steam=None)
    assert cmd == [str(exe), "--enable-lua-udp", "34500"] and env is None and cwd == tmp_path


def test_game_cmd_steam_copy(lau, tmp_path):
    exe, steam = tmp_path / "factorio", tmp_path / "steam_osx"
    cmd, env, _ = lau.game_cmd(CFG, exe=exe, kind="steam", steam=steam, steam_is_running=True)
    assert cmd[0] == str(exe) and env["SteamAppId"] == "427520"
    cmd, env, _ = lau.game_cmd(CFG, exe=exe, kind="steam", steam=steam, steam_is_running=False)
    assert cmd[:3] == [str(steam), "-applaunch", "427520"] and cmd[3:] == ["--enable-lua-udp", "34500"]
    # no Steam client found at all: try the binary anyway
    cmd, env, _ = lau.game_cmd(CFG, exe=exe, kind="steam", steam=None, steam_is_running=False)
    assert cmd[0] == str(exe)


def test_game_cmd_nothing_found(lau, monkeypatch):
    monkeypatch.setattr(lau.platforms, "find_factorio", lambda cfg=None: (None, None))
    assert lau.game_cmd(CFG, steam=None) == (None, None, None)
    cmd, _, _ = lau.game_cmd(CFG, exe=None, steam=Path("/x/steam"))  # Steam but no game found: Steam installs it
    assert cmd[1:3] == ["-applaunch", "427520"]


def test_launcher_pyw_reexports_launcher():
    pyw = _load("launcher_pyw_under_test", "launcher.pyw")
    assert callable(pyw.game_flags) and callable(pyw.run)


# ---------------------------------------------------------------- installer (sandboxed)

@pytest.fixture()
def inst(monkeypatch, tmp_path):
    mod = _load("install_platform_test", "install.py")
    monkeypatch.setattr(mod, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(mod, "MODS", tmp_path / "factorio" / "mods")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dot-claude"))
    monkeypatch.setenv("AIHELPER_SKIP_RUNNING_CHECK", "1")
    mod.results.clear()
    return mod


def test_mod_link_create_and_idempotent(inst, tmp_path):
    inst.junction()
    link = tmp_path / "factorio" / "mods" / "claude-bridge"
    assert (link / "info.json").exists() and inst.results["mod_link"]["status"] == "changed"
    inst.junction()
    assert inst.results["mod_link"]["message"] == "mod link: ok"
    if inst.is_link(link):
        inst.remove_link(link)
    else:
        shutil.rmtree(link)
    assert (ROOT / "mod" / "claude-bridge" / "info.json").exists()  # the source is never touched


def test_mod_link_replaces_a_broken_link(inst, tmp_path):
    mods = tmp_path / "factorio" / "mods"
    mods.mkdir(parents=True)
    gone = tmp_path / "moved-away"
    gone.mkdir()
    try:
        inst.create_mod_link(gone, mods / "claude-bridge")
    except OSError:
        pytest.skip("no link support here")
    if not inst.is_link(mods / "claude-bridge"):
        pytest.skip("fell back to a copy")
    shutil.rmtree(gone)
    inst.junction()
    assert inst._same(mods / "claude-bridge", ROOT / "mod" / "claude-bridge")
    inst.remove_link(mods / "claude-bridge")


def test_mod_link_leaves_a_real_folder_alone(inst, tmp_path):
    real = tmp_path / "factorio" / "mods" / "claude-bridge"
    real.mkdir(parents=True)
    (real / "mine.txt").write_text("x")
    inst.junction()
    assert (real / "mine.txt").exists() and inst.results["mod_link"]["status"] == "warning"


def test_mod_list_created_and_enabled(inst, tmp_path):
    inst.mod_list()
    data = json.loads((tmp_path / "factorio" / "mods" / "mod-list.json").read_text())
    assert {"name": "claude-bridge", "enabled": True} in data["mods"]
    (tmp_path / "factorio" / "mods" / "mod-list.json").write_text(json.dumps(
        {"mods": [{"name": "base", "enabled": True}, {"name": "claude-bridge", "enabled": False}]}))
    inst.mod_list()
    data = json.loads((tmp_path / "factorio" / "mods" / "mod-list.json").read_text())
    assert {"name": "claude-bridge", "enabled": True} in data["mods"] and len(data["mods"]) == 2
    assert (tmp_path / "factorio" / "mods" / "mod-list.json.bak-claude").exists()


def test_mod_list_waits_while_factorio_runs(inst, tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "factorio_running", lambda: True)
    mods = tmp_path / "factorio" / "mods"
    mods.mkdir(parents=True)
    (mods / "mod-list.json").write_text('{"mods": [{"name": "base", "enabled": true}]}')
    inst.mod_list()
    assert "claude-bridge" not in (mods / "mod-list.json").read_text()
    assert inst.results["mod_list"]["status"] == "warning"


def test_missing_factorio_user_dir_asks_for_a_first_launch(inst, monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "MODS", None)
    monkeypatch.setenv("FACTORIO_USER_DIR", str(tmp_path / "never-started"))
    inst.junction()
    inst.mod_list()
    assert inst.results["needs_factorio_once"] and not (tmp_path / "never-started").exists()


def test_skill_install_respects_user_edits(inst, tmp_path):
    inst.skill()
    dest = tmp_path / "dot-claude" / "skills" / "factorio" / "SKILL.md"
    text = dest.read_text(encoding="utf-8")
    assert inst.MARKER in text and str(ROOT) in text and "{{INSTALL_DIR}}" not in text
    dest.write_text("my own skill", encoding="utf-8")
    inst.skill()
    assert dest.read_text(encoding="utf-8") == "my own skill"
    assert (dest.parent / "SKILL.md.new").exists()


def _fake_claude(tmp_path):
    state = tmp_path / "fake_state.json"
    if os.name == "nt":
        w = tmp_path / "claude.cmd"
        w.write_text(f'@echo off\r\n"{sys.executable}" "{ROOT / "tests" / "fake_claude.py"}" %*\r\n')
    else:
        w = tmp_path / "claude"
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_claude.py"}" "$@"\n')
        w.chmod(0o755)
    return w, state


def test_mcp_register_and_replace_stale(inst, tmp_path, monkeypatch):
    claude, state = _fake_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", str(claude))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    monkeypatch.delenv("AIHELPER_NO_MCP", raising=False)
    state.write_text(json.dumps({"servers": {"factorio": {"command": "/old/python", "args": ["/old/run_server.py"]}},
                                 "calls": []}))
    inst.mcp()
    s = json.loads(state.read_text())["servers"]["factorio"]
    assert Path(s["args"][0]) == ROOT / "run_server.py" and inst.results["mcp"]["message"].startswith(
        "mcp: re-registered")
    inst.mcp()
    assert inst.results["mcp"]["message"] == "mcp: factorio already registered"


def test_mcp_without_claude_warns_and_continues(inst, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "nothing-here"))
    monkeypatch.delenv("AIHELPER_NO_MCP", raising=False)
    inst.mcp()
    assert inst.results["claude_missing"] and inst.results["mcp"]["status"] == "warning"


def test_mac_app_bundle(inst, tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "factorio_exe", lambda: None)
    (tmp_path / "config.json").write_text(json.dumps({"extra_path": "/opt/homebrew/bin:/usr/bin:/bin"}))
    app = inst.make_mac_app(tmp_path / "Applications")
    with open(app / "Contents" / "Info.plist", "rb") as f:
        info = plistlib.load(f)
    assert app.name == "Factorio (Claude).app"
    assert info["CFBundleExecutable"] == "launch" and info["CFBundlePackageType"] == "APPL" and info["LSUIElement"]
    script = (app / "Contents" / "MacOS" / "launch").read_text()
    assert script.startswith("#!/bin/bash\n") and "export PATH=/opt/homebrew/bin:/usr/bin:/bin" in script
    assert "launcher.py" in script
    if os.name != "nt":
        assert os.access(app / "Contents" / "MacOS" / "launch", os.X_OK)
        assert subprocess.run(["/bin/bash", "-n", str(app / "Contents" / "MacOS" / "launch")]).returncode == 0


def test_launch_script_quotes_paths_with_spaces(inst, monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"extra_path": "/a b/bin:/usr/bin"}))
    s = inst.launch_script()
    assert "export PATH='/a b/bin:/usr/bin'" in s
