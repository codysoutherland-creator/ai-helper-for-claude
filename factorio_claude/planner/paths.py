"""Where the mod's files land: <write-data>/script-output (the mod writes under its claude/ subfolder)."""
import json
import os
from pathlib import Path

from .. import platforms

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"


def script_output() -> Path:
    """env FACTORIO_SCRIPT_OUTPUT -> config.json "script_output" -> <Factorio user dir>/script-output
    (env FACTORIO_USER_DIR / config "factorio_user_dir" / the OS default, see platforms.user_data_dir)."""
    env = os.environ.get("FACTORIO_SCRIPT_OUTPUT")
    if env:
        return Path(env)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("script_output"):
        return Path(cfg["script_output"])
    return platforms.script_output_dir(cfg)
