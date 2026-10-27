# RCON test harness. Usage (from the repo root):  exec(open("testenv/t.py").read())
# Set SLOT before exec to target another instance (RCON port 27016+SLOT).
import sys, json, time
from pathlib import Path as _Path
# repo root = first folder holding factorio_claude/, searched upward from the exec-ing script, then the cwd
_TROOT = next((p for b in (_Path(globals().get("__file__", ".")).resolve(), _Path.cwd().resolve())
               for p in (b, *b.parents) if (p / "factorio_claude" / "rcon.py").is_file()), _Path.cwd())
sys.path.insert(0, str(_TROOT))
from factorio_claude import rcon
SLOT = globals().get("SLOT", 0)
rcon._client = rcon.Rcon("127.0.0.1", 27016 + SLOT, "testpw")
B = rcon.bridge
def show(name, f):
    try: r = f(); print(f"== {name}: {json.dumps(r)[:700]}")
    except Exception as e: print(f"!! {name}: {e}")
