"""A stand-in for the Claude Code CLI in installer tests (sandbox / CI): remembers `claude mcp add/remove` in a JSON
file instead of touching the real ~/.claude.json, and answers `claude mcp get` like the real CLI does.

    FAKE_CLAUDE_STATE=<file.json> python tests/fake_claude.py mcp add -s user factorio -- <python> <run_server.py>

Wrap it in a claude.cmd (Windows) or an executable `claude` shell script (macOS/Linux) and point CLAUDE_BIN at it.
"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_CLAUDE_STATE") or Path(__file__).with_name("fake_claude_state.json"))


def load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"servers": {}, "calls": []}


def main(argv):
    st = load()
    st["calls"].append(argv)
    rc = 0
    if argv[:1] == ["--help"]:
        print("Usage: claude [options]\n  --max-budget-usd <n>\n  --setting-sources <s>")
    elif argv[:2] == ["mcp", "get"] and len(argv) > 2:
        s = st["servers"].get(argv[2])
        if s:
            print(f"{argv[2]}:\n  Scope: User config\n  Type: stdio\n  Command: {s['command']}\n"
                  f"  Args: {' '.join(s['args'])}")
        else:
            print(f'No MCP server found with name: "{argv[2]}"', file=sys.stderr)
            rc = 1
    elif argv[:2] == ["mcp", "add"] and "--" in argv:
        head, tail = argv[:argv.index("--")], argv[argv.index("--") + 1:]
        name = [a for a in head[2:] if not a.startswith("-") and a not in ("user", "local", "project")][0]
        if name in st["servers"]:
            print(f"MCP server {name} already exists", file=sys.stderr)
            rc = 1
        else:
            st["servers"][name] = {"command": tail[0], "args": tail[1:]}
    elif argv[:2] == ["mcp", "remove"]:
        name = [a for a in argv[2:] if not a.startswith("-") and a not in ("user", "local", "project")][0]
        rc = 0 if st["servers"].pop(name, None) else 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
