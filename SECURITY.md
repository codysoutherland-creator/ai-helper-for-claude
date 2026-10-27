# Security policy

## Reporting a problem

Please report security problems privately through GitHub's **Report a vulnerability** button on this repository's
Security tab (private vulnerability reporting), not in a public issue. Include what you found, how to reproduce it
and which version (`mod/claude-bridge/info.json`) you used. This is a hobby project maintained in spare time, so
there is no guaranteed response time, but reports are taken seriously.

Only the latest release is supported.

## Scope

Things that are in scope, for example:

- A player in a multiplayer game getting Claude (and so the host's Claude subscription) to do something the
  "Who can use Claude" setting or the in-game tool allowlist (`factorio_claude/policy.py`) should prevent.
- Chat text or game data that makes the in-game Claude run tools outside that allowlist, or read or write files.
- The local UDP link or RCON being reachable from other machines, or accepting ops it should refuse.
- The installers writing outside your user profile, needing admin rights, or fetching code over an unverified path.
- Secrets (the RCON password in `config.json`, Claude session ids) leaking into logs, files readable by other users,
  or the repository.

Out of scope: problems in Factorio, Claude Code or third-party packages themselves (report those upstream), and
anything that needs the attacker to already control your user account.

## How it is meant to work

- Everything listens on `127.0.0.1` only. RCON is off by default for new installs and bound to localhost when on.
- In-game Claude runs with a fixed tool allowlist, a per-call budget cap and no raw Lua, cheats or clipboard.
- `config.json`, `chat-session/`, `mcp.json` and `logs/` are created readable only by you (on macOS/Linux).
- If you turn RCON on (`"rcon": true`), the launcher has to pass the RCON password to Factorio on its command line
  (`--rcon-password`; Factorio has no other way to receive it). Other local users can see process command lines on
  macOS/Linux, so on a shared machine leave RCON off. The launcher never prints the password.
- Python dependencies are installed from a hash-locked `requirements.lock`.

Unofficial project; not affiliated with or endorsed by Anthropic.
