> **Unofficial — not affiliated with or endorsed by Anthropic. Claude is a trademark of Anthropic.**

Claude as a Factorio 2.1 co-pilot: an in-game helper mod plus a small companion app and an MCP server for Claude Code.

**You need:** Factorio 2.1 (installed and started once) and Claude Code (installed and logged in; the in-game chat
runs through your own Claude subscription). Quit Factorio, then paste one line.

**macOS / Linux** (Terminal):

```sh
curl -fsSL https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.sh | bash
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.ps1 | iex
```

No admin password, no Xcode Command Line Tools, no git and no system Python needed. Running the same line again
updates. Afterwards start the game with **Factorio (Claude)** (macOS: in Applications; Windows: desktop shortcut).

`claude-bridge_<version>.zip` is the Factorio mod on its own (the installer links it for you; you only need the zip
for a manual install). `SHA256SUMS` lists its checksum. See the README for details and uninstalling.
