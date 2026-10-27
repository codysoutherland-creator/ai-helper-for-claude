# AI Helper (for Claude)

> **Unofficial — not affiliated with or endorsed by Anthropic. Claude is a trademark of Anthropic.**

Claude as a Factorio 2.1 co-pilot. The simple stuff (helper character, base alerts, standard blueprints, building and
fuelling approved plans) runs inside the mod for free. Claude (tokens) is only used when you ask it something.
Layouts are designed by deterministic code (the planner), not by Claude placing entities one by one.

## Requirements

- **Factorio 2.1** (Steam or the standalone factorio.com version), **started once** so it has created its user folder.
- **Claude Code**, installed and logged in (run `claude` once in a terminal). The in-game chat runs through your own
  Claude subscription and counts toward your usage.
- macOS 11 or newer, Windows 10/11, or Linux. No admin password, no Xcode Command Line Tools, no git and no Python
  install needed: the installer brings its own private Python.

## Install

Quit Factorio first, then paste one line.

**macOS / Linux** (Terminal):

```sh
curl -fsSL https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.sh | bash
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.ps1 | iex
```

What it does (all for your user only):

1. Downloads the latest release to `~/Library/Application Support/ai-helper-for-claude` (macOS),
   `~/.local/share/ai-helper-for-claude` (Linux) or `%LOCALAPPDATA%\ai-helper-for-claude` (Windows).
2. Installs [uv](https://docs.astral.sh/uv/) to `~/.local/bin` if you don't have it, and uses it to make a private
   Python 3.14 environment with pinned, hash-checked packages.
3. Links the mod into Factorio's mods folder (a symlink on macOS/Linux, a directory junction on Windows) and enables
   it in `mod-list.json` (a backup is kept as `mod-list.json.bak-claude`).
4. Creates `config.json` with a random password (only you can read it).
5. Registers the `factorio` MCP server with Claude Code and copies the Claude Code skill to `~/.claude/skills/factorio`.
6. Creates the launcher: **Factorio (Claude).app** in `/Applications` (or `~/Applications`) on macOS, a
   **Factorio (Claude)** desktop shortcut on Windows, a menu entry on Linux.

**Updating:** run the same line again. Your config, logs and blueprints stay. Saves keep working: the mod's internal
name (`claude-bridge`) never changes. On Windows, quit Factorio and wait a minute (the companion stops by itself)
before updating.

## Playing

Open **Factorio (Claude)** (macOS: from Applications, Launchpad or Spotlight; Windows: the desktop shortcut). It
starts Factorio with the right launch flags and a hidden companion that answers Claude questions. Load any save
(single-player is fine, no hosting needed) with *AI Helper (for Claude)* enabled in Mods. The companion quits by itself
about a minute after Factorio closes. Everything talks over localhost only (UDP 34500, optional RCON 27015 bound to
127.0.0.1), so no firewall or network prompts.

In game, click the **Claude** button (top-left) or press **Ctrl+Shift+C**:

| Tab | Cost | What |
|---|---|---|
| Helper | free | spawn / follow / come / stop / dismiss, chop wood, mine stone/iron/copper/coal (count box), build ghosts near you, refuel burners, give me everything, hand it the item in my cursor; status + inventory; **Clear Claude's ghosts** (removes ghosts Claude placed, never yours; asks first) |
| Foreman | free | rule-based base check every 30 s (power shortage, no fuel, depleted drills, no power, research idle, backed-up output, starved machines) with map alerts; "Scan now" |
| Blueprints | free | smelter column, assembler line, mining outpost, solar block, with options; "Put blueprint in cursor" |
| Ask Claude | tokens | type a question; the answer appears in the panel and in chat, with a cost line ("cost ≈ $0.03 · 48 s"). Bigger jobs ask first (see Cost gate) |
| Plans | free | plans Claude proposed: summary, Preview (coloured overlay, also on the map), map pin, Approve, Adjust (asks Claude, tokens), Reject. Approved plans: Helper: build, Helper: refuel, Remove its ghosts, Dismiss |

Chat works too: `@claude help` lists the free keyword commands (`@claude wood 20`, `@claude follow`,
`@claude blueprint smelter 8`, ...). Any other `@claude ...` message goes to Claude.
`@claude reset` starts a fresh Claude conversation.

### Starter base (new game)

1. Ask: `@claude set me up with a starter base` (or type it in the Ask Claude tab).
2. Cost gate: "This looks like a planning job (≈ $0.05–0.30). Run it?" Click **Run** (or `@claude run`). About
   $0.03 and under a minute.
3. Claude runs the planner once. It reads the map and proposes 2-3 variants (Compact hub / Balanced hub / Expandable
   bus) in the **Plans** tab. Every variant has already passed the validator.
4. **Preview** a variant (overlay in the world and on the map; another Preview hides it), then **Approve** one.
   Approving places tagged ghosts, marks trees/rocks in the way for removal, and starts the helper (spawned if needed).
   The helper builds everything. It takes materials from nearby chests, then your inventory (mod setting), and
   crafts, smelts or mines the rest. It chops trees in the way, then fills the hand-fed burners with coal.
   The other variants on the same ground are greyed out ("Replaced by ..."). Use Remove its ghosts to switch.
5. The burner (bootstrap) base: burner drills and the furnaces' output inserters have no coal feed (2.1 has no burner
   leech), so they run about 22 minutes on 50 coal each. Press **Helper: refuel** on the plan about every 20 minutes.
   The helper tops them up. If it has no coal, it gathers some first. Furnaces get their coal by belt.
6. Later, ask for the electric stage (`@claude plan the electric stage`). This adds steam power, electric drills and
   inserters, which need no hand fuel after the first few coal in the boilers.

### Cost gate

The companion classifies each message. A build/design request (build, design, set up, plan, expand, optimize,
lay out, ...) that is not phrased as a question, or anything over 200 characters, is a planning job: it gets the
in-game "≈ $X, run it?" with Run / Cancel (free). Questions and status checks ("check my base", "why is my factory
slow", "what should I build next?") go straight through. `@claude run` / `@claude cancel` answer a pending
confirmation from chat; with none pending they are answered locally, never sent to Claude.
`config.json`: `cost_gate` (true), `planning_estimate` ("≈ $0.05–0.30").

In-game Claude never hand-places big builds. Raw ghost placement (`factorio_place_ghosts`, `blueprint_place`) refuses
more than 50 ghosts unless `allow_large=True`. Every Claude ghost carries `tags.claude_plan`, so **Clear Claude's
ghosts** can remove exactly those.

Foreman settings (on/off, interval, also post alerts in chat) and "Helper: may take building materials from my
inventory": Settings → Mod settings → Map.

In Claude Code you can still say "check my base", "set me up with a starter base", etc. (MCP server `factorio`).

## Pieces

| Piece | Where | What |
|---|---|---|
| `claude-bridge` mod (0.4.0) | `mod/claude-bridge` (linked into Factorio's `mods` folder: `%APPDATA%\Factorio`, `~/Library/Application Support/factorio`, `~/.factorio`) | GUI, free helper/foreman/blueprints, plan overlay + tagged ghost placement (`scripts/plans.lua`), map/game-data export to script-output, UDP + `/claude <json>` RCON API, Claude inbox |
| planner | `factorio_claude/planner/` | world model (`world.py`, `grid.py`), game data (`gamedata.py`), rate solver (`rates.py`), verified blocks (`blocks.py`), A* belt/pipe router + pole placer (`router.py`), starter-base placer (`placer.py`, `plan.py`), static validator (`validate.py`) |
| launcher | `launcher.py` (Windows shortcut: `pythonw launcher.pyw`; macOS: `Factorio (Claude).app`; Linux: menu entry) | starts Factorio with `--enable-lua-udp 34500 ...` (the game binary directly when Steam is already running or for a standalone copy, else `steam -applaunch 427520`) + companion |
| platform paths | `factorio_claude/platforms.py` | Factorio user dir, Steam/Factorio/Claude Code discovery, venv python, detached processes, dialogs, clipboard per OS |
| installers | `install.sh` (macOS/Linux), `install.ps1` (Windows) -> `install.py` | one-line bootstrap, then the cross-platform setup (`--check`, `--uninstall`) |
| companion | `factorio_claude/companion.py` | hidden; polls the inbox, cost gate, answers with lean `claude -p` calls (`chat_model`, short planner-first prompt, pre-fetched context); log `logs/companion.log` |
| link | `factorio_claude/link.py` | UDP client (chunked), RCON fallback |
| MCP server `factorio` | `factorio_claude/server.py` via `run_server.py` (user scope) | observe, act, blueprints, agent, quick actions, foreman report; planner tools `plan_starter_base`, `world_summary`, `plan_preview`, `plan_validate`, `plan_apply`, `plan_helper`, `plan_list` |
| Skill | `~/.claude/skills/factorio/SKILL.md` | playbooks for Claude Code |

How a plan is made: the mod exports the charted map (`export_map`: water, resources, trees/rocks/cliffs, your
entities) and the game data (`export_data`: prototypes, recipes, research) as files to script-output. The planner
picks ore/coal patches and a hub site, places mines, a smelting hub with mixed ore/coal lanes and a bus, routes the
belts (A*, no undergrounds until logistics is researched), adds steam power and poles for the electric stage, and
runs the validator. The validator checks belts, lanes, inserters, fuel (including what burner inserters burn
themselves), power, fluids, terrain and research, and checks that nothing touches your existing belts. Anything that
fails is dropped and the next candidate is tried. Plans are saved in `plans/` and shown in the Plans tab.

`config.json` (created by the installer with a random password; `config.example.json` shows the starting keys):
`udp_port` (34500), `chat_model` (`sonnet`), `chat_session_turns` (12, then a fresh session),
`cost_gate`, `planning_estimate`, optional `chat_effort`; `host`/`port`/`password` = RCON, used only for
`factorio_run_lua` from Claude Code. RCON is opt-in: `"rcon": true` (new installs get `false` and a random password;
configs from before this key keep RCON on). When on, the launcher binds it to `127.0.0.1` only (`--rcon-bind`).
Limits: `rate_limit_per_hour` (20 questions per non-host player), `max_requests_per_session` (200 Claude calls per
companion run), `chat_max_budget_usd` (1.0 per answer), `chat_setting_sources` (`project`), optional
`allowed_players` (list of names; overrides the in-game setting).

## Multiplayer and safety

Every question to Claude runs on the host's own Claude subscription, so by default only the host can use Claude.
The map setting **Who can use Claude** (Settings > Mod settings > Map) picks host only (default), host and admins,
or everyone. Players it doesn't allow get a free one-line explanation instead of a Claude call, and can't press the
helper buttons or chat commands either. Running a held (expensive) job and approving a plan always need the host or
an admin, even with "everyone".

The in-game Claude gets a fixed list of tools (look, mark, small ghost builds, the helper, the planner). It has no
raw Lua, cheats, clipboard, `plan_apply` or other players' inventories; those stay with Claude Code at your own
terminal. Chat text reaches Claude as untrusted data, and each player has a separate Claude conversation.

`logs/companion.log` keeps a short (truncated) copy of each question and answer, which includes other players'
chat. `config.json` and `chat-session/` are readable only by you.

The mod's test and cheat ops (simulated chat and clicks, instant build, give items) need the startup setting
"Debug: enable test and cheat ops", which is off by default. The test harness turns it on for its own servers.

## Privacy: what is sent to Claude

AI Helper has no server of its own, no telemetry and no analytics. Nothing is sent to the author of this project.

- **Free features** (helper, foreman, blueprints, building approved plans) run entirely inside the mod. Nothing
  leaves your computer.
- **When you ask Claude something** (the Ask Claude tab, an `@claude ...` message that isn't a free keyword, a plan
  "Adjust", or a planning job you confirmed), the companion runs your locally installed Claude Code (`claude -p`)
  under your own Claude account. That request contains: the message text, the sender's in-game name and role
  (host/admin/guest) and position, the host's in-game name, and a short game-state summary (helper position, task and
  inventory, the top foreman alerts). While answering, Claude can call the allowlisted game tools, so their results
  (map excerpts around your base, entity and production data, research state, plan summaries) are sent too.
  In multiplayer this includes other players' messages when they are allowed to ask Claude.
- What Anthropic does with that data is governed by your Claude account's terms and privacy settings, the same as any
  other Claude Code session.
- **Locally**, `logs/companion.log` keeps a truncated copy of each question and answer, and `chat-session/` keeps the
  Claude Code session ids. Both stay in the install folder; delete them whenever you like.
- **The installers** download only from GitHub (this project), astral.sh / GitHub (uv and its Python build) and PyPI
  (the pinned, hash-checked Python packages).

## Setup, repair and uninstall

- Repair or update: run the one-line installer again. From a checkout: `install.py` with the venv's python
  (`.venv/bin/python install.py` or `.venv\Scripts\python.exe install.py`); it is idempotent.
- `install.py --check` lists what is set up (venv, config, mod link, mod-list, skill, MCP, launcher) and changes nothing.
- `install.py --uninstall` removes the launcher, the MCP registration, the skill (only the installer's own copy), the
  mod link and the mod-list entry. The install folder (config, logs, blueprints) stays; delete it to finish.
  By hand: `claude mcp remove -s user factorio`, delete `~/.claude/skills/factorio`, the mod link in Factorio's
  `mods` folder (restore `mod-list.json.bak-claude` if you like), the app/shortcut and the install folder.
- `launcher.py --dry-run` shows what the launcher would start.

## Troubleshooting

- **"Claude Code not found"**: install Claude Code, log in once (`claude` in a terminal), then run the installer again.
  The free in-game buttons work without it.
- **The mod isn't listed in Factorio**: start Factorio once (it creates its user folder), quit, run the installer
  again. A portable or custom Factorio data folder: set `FACTORIO_USER_DIR` (or `"factorio_user_dir"` in
  `config.json`) and run the installer again.
- **"Factorio is running" during install**: Factorio rewrites `mod-list.json` when it quits, so quit it first.
- **Factorio isn't found by the launcher**: put the game binary into `config.json` as `"factorio_path"`
  (macOS: `.../factorio.app/Contents/MacOS/factorio`).
- **macOS, Steam copy**: the launcher starts the binary inside `factorio.app` while Steam runs; if Steam isn't running
  it goes through `steam_osx -applaunch`, and Steam may ask once whether to allow the launch options.
- **macOS Gatekeeper**: `Factorio (Claude).app` is a small script bundle created on your Mac by the installer (not
  downloaded), so macOS doesn't quarantine it.
- **Claude doesn't answer**: see `logs/companion.log` and `logs/launcher.log` in the install folder (macOS app starts
  also log to `logs/app.log`).
- Factorio must be 2.1 (the mod declares `factorio_version` 2.1).

## Contributing and security

Development setup, the test suite and the headless test harness are described in [CONTRIBUTING.md](CONTRIBUTING.md).
Design history is in [docs/design](docs/design). To report a security problem, see [SECURITY.md](SECURITY.md).

## Support

If you enjoy this, donation links will appear here. (Not set up yet.)

## License

MIT, see [LICENSE](LICENSE). Unofficial; not affiliated with or endorsed by Anthropic. Claude is a trademark of
Anthropic. Factorio is a trademark of Wube Software.
