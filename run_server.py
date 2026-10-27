"""Launcher for the factorio MCP server that works from any working directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from factorio_claude.server import main  # noqa: E402

main()
