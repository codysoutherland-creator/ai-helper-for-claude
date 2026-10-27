"""Windows entry point kept for the existing "Factorio (Claude)" desktop shortcut (pythonw launcher.pyw: no console
window). Everything lives in launcher.py, which is the cross-platform launcher.

    pythonw launcher.pyw            # what the shortcut does
    python launcher.pyw --dry-run   # print what it would do, start nothing
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from launcher import *  # noqa: E402,F401,F403 - re-exported for older callers and tests
from launcher import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
