# Contributing

Thanks for helping. Issues and pull requests are welcome. Please keep changes focused, add or update tests, and never
commit personal data (logs, saves, `config.json`, chat sessions, user paths). `.gitignore` already excludes them.

## Layout

| Path | What |
|---|---|
| `mod/claude-bridge/` | The Factorio 2.1 mod (Lua). The internal name `claude-bridge` must not change (save compatibility). |
| `factorio_claude/` | Python: `link.py` (UDP + RCON client), `server.py` (MCP server `factorio`), `companion.py` (in-game chat daemon that runs `claude -p`), `policy.py` (in-game tool allowlist), `platforms.py` (per-OS paths), `blueprints.py`, `planner/` (deterministic starter-base planner) |
| `run_server.py`, `launcher.py`, `launcher.pyw` | Entry points: MCP server, one-click launcher, Windows shim |
| `install.py`, `install.sh`, `install.ps1` | Cross-platform setup and the one-line bootstraps |
| `skill/factorio/SKILL.md` | Generic Claude Code skill; the installer copies it to `~/.claude/skills/factorio/` |
| `tests/` | pytest suite (`test_*.py`, offline) and live in-engine checks (`live_*.py`, not collected by pytest) |
| `testenv/` | Headless test harness (Windows) and the planner fixtures |
| `tools/build_mod_zip.py` | Release zip builder |
| `docs/design/` | Historical design briefs (UDP protocol, module and planner contracts) |

Architecture: the mod talks to the Python side over UDP on localhost (`--enable-lua-udp`, chunked `Q|id|seq|total|...`
/ `A|...` datagrams, see `docs/design/SPEC-v2.md`). RCON is an optional fallback. The MCP server and the companion
both use `link.call(op, **args)`. The planner is pure Python (numpy/scipy) and never calls Claude.

## Setup

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements.lock --require-hashes   # or: install.py with AIHELPER_NO_MCP=1 AIHELPER_NO_SHORTCUT=1
uv pip install --python .venv pytest
```

`requirements.in` holds the direct dependencies; regenerate the lock with
`uv pip compile requirements.in --universal --generate-hashes --python-version 3.14 -o requirements.lock`.

## Tests

```sh
.venv/bin/python -m pytest -q tests          # Windows: .venv\Scripts\python.exe -m pytest -q tests
```

The offline suite (planner, router, validator, fixtures, companion, security rules, platform paths) needs no game and
takes about a minute and a half. `tests/conftest.py` points the link at ports nothing listens on, so a test can
never reach a running game.

CI (`.github/workflows`): pytest on Linux, macOS and Windows; `luac5.2 -p` over the mod; shellcheck; installer smoke
tests in a sandbox home with a fake `claude` CLI (`tests/fake_claude.py`, `tests/installer_smoke.py`); the mod
loaded into the free Linux headless server (`mod-load.yml`); tagged releases (`v*`) build
`claude-bridge_<version>.zip` with `tools/build_mod_zip.py`, which checks that the tag, `info.json` and the top
`changelog.txt` entry agree.

### Installer sandboxing

Never run the installer against your real profile while testing. These overrides keep it inside a sandbox:
`AIHELPER_HOME` (install folder), `AIHELPER_SOURCE` (local folder or archive instead of the download; also how to
install while the repository is private), `AIHELPER_REF`, `FACTORIO_USER_DIR`, `CLAUDE_CONFIG_DIR`, `CLAUDE_BIN`,
`FACTORIO_BIN` / `FACTORIO_EXE`, `AIHELPER_APPS_DIR`, `AIHELPER_DESKTOP_DIR`, `AIHELPER_NO_SHORTCUT`,
`AIHELPER_NO_MCP`, `AIHELPER_NO_DEPS`, `AIHELPER_SKIP_RUNNING_CHECK`. On CI also point `HOME` (macOS/Linux) or
`USERPROFILE` / `APPDATA` / `LOCALAPPDATA` (Windows) at the sandbox.

## Headless test harness (Windows)

`testenv/restart.ps1` starts an isolated headless server with the current mod code. It never touches a running
game, your Factorio user folder or the default ports.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File testenv\restart.ps1 -Name mytest -Slot 21 [-Save seed_1001.zip] [-NoDebugOps]
powershell -NoProfile -ExecutionPolicy Bypass -File testenv\restart.ps1 -Name mytest -Slot 21 -Stop
```

- Slot N gives RCON `127.0.0.1:27016+N` (password `testpw`), UDP `34500+N` and game port `127.0.0.1:34199+N` (loopback only). Use slot 1 or
  higher when you also play: the real game uses UDP 34500 and RCON 27015.
- The Factorio binary comes from `FACTORIO_EXE` (or `FACTORIO_BIN`), else the Steam / standalone discovery in
  `factorio_claude/platforms.py`.
- Each instance lives in `testenv/inst/<name>/` (copied save, mods, `server.log`) and is reset on every restart.
  Test servers get the mod's startup setting `claude-debug-ops=true` (simulated chat/clicks, instant build, give
  items) through a generated `mod-settings.dat`; `-NoDebugOps` starts a player-like server.
- Saves are not in the repository. Create the fixture maps once:
  `.venv\Scripts\python.exe testenv\make_fixtures.py create 1001 2002 3003` (writes `testenv/write/seed_<n>.zip`),
  then copy `seed_1001.zip` to `testenv/write/test.zip` for the default save.
- Point the Python side at an instance with `FACTORIO_UDP_PORT=34500+N FACTORIO_RCON_PORT=27016+N
  FACTORIO_RCON_PASSWORD=testpw FACTORIO_SCRIPT_OUTPUT=testenv\inst\<name>\write\script-output`, and keep test
  plans out of `plans/` with `FACTORIO_PLANS_DIR`.
- `testenv/t.py` is an RCON scratch harness: from the repo root, set `SLOT = N` and `exec(open("testenv/t.py").read())`
  to get `B(op, **args)` and `rcon.lua(code)`.

In-engine checks (each on its own slot):

- `testenv/udp_test.py --port 3450N`: UDP protocol. `testenv/mcp_test.py`: the MCP server over stdio.
- `testenv/m0_test.py N`, `testenv/m0_gate_e2e.py N`: ghost tagging, markdown stripping, the cost gate end to end
  (fake `claude`, no tokens).
- `testenv/security_test.py N [--no-debug]`: multiplayer rules, debug-op gating, deconstruct and UDP buffer caps
  (needs a save with a connected player).
- `testenv/acceptance.py`: planner acceptance on the 3 fixture seeds (plan, apply, build, run at speed 10, compare
  measured output with the plan). About an hour; `--variants 0 --stages bootstrap` for a quick run. `--no-cheat`:
  player-like server, the real helper builds and fuels the plan from nothing, first plates measured against the ETA.
- `tests/live_stages.py --save <zip> [--slot 32] [--to 5] [--player-lab]`: planner v3 stage by stage with the real
  helper on a `-NoDebugOps` server through the shipped tool path (plan_starter_base, the mod's own "stage ready"
  offer, plan_next_stage, plan_apply, plan_helper build): measured build minutes vs the offer's "ready in", 30 game
  minutes with the helper stopped after S2 and after S3 (plates, packs, fuel, power per 3-minute window), research
  from the assemblers' packs, an entity census by unit_number around the electric-drill swap (S4), green packs/min
  after S5. `--player-lab` marks the red-science trigger done on a fresh map (a headless server has no player to
  craft the lab); results in `testenv/inst/live_stages_<tag>.json`.
- `testenv/acceptance.py --planner v3 [--to 5]`: every master plan offered on the fixture seeds, built stage by stage
  with test cheats (instant build, research and start fuel by script), then run untouched: plates, red and green
  packs per minute against the plan, no burner out of fuel.
- `tests/live_onsite.py --save <zip> --variant quick`: one planner v2 variant built by the real helper on a
  `-NoDebugOps` server (Approve via `plan_apply`, then `plan_helper build`): first plates vs the ETA, plate rate,
  burner fuel, player ghosts and the force's entities before/after.
- `tests/live_*.py`: planner, router, validator, Plans tab (`live_planui.py --client` starts a graphical client).
- `tests/live_rebuild.py [--save seed_4004.zip]`: planner v2 rebuilds end to end (approve marks exactly the listed
  entities, reject cancels the marks, the helper dismantles exactly them and keeps the salvage, player ghosts stay).
- `tests/live_onsite.py ... --stage2-minutes M`: after stage 1, the variant's placed stage 2 is approved and built by
  the helper (labs powered and researching, boiler and stage-1 burners fuelled, stage-1 rate held).
- `tests/live_convert.py --save <zip>`: a planner v2 conversion (the player's idle drill line turned into pairs where
  it stands) built by the helper: exactly the listed belts removed, the drills kept and producing.
- `testenv/render_plan.py <map_dir> <out.png> --plan plan.json`: offline PNG of a map and plans (removals in red).
- `testenv/make_fixtures.py screen|build|sprawl|readme`: rebuild `testenv/fixtures/` (planner unit-test inputs; `sprawl 4004` = the
  synthetic planner-v2 map with far-apart ore and old builds in the middle).

Companion test overrides: `FACTORIO_COMPANION_LOCK_PORT`, `FACTORIO_GAME_MATCH`, `FACTORIO_EXIT_AFTER`,
`FACTORIO_CHAT_DIR`, `FACTORIO_PLANS_DIR`, `FACTORIO_FAKE_CLAUDE`, `--no-exit-check`.

## Verified Factorio 2.1 facts

Checked in this engine; please don't "fix" code that relies on them:

- Mods declare `"factorio_version": "2.1"`.
- Inserter `direction` = the side it picks up from (it drops on the opposite side).
- Electric poles in blueprints need explicit `wires`.
- Directions are 16-way (north 0, east 4, south 8, west 12).
- Recipes have `recipe.categories` (plural) in 2.1.
- The first `/sc` command over RCON after start is swallowed; send a throwaway one first.
- Headless servers need `auto_pause: false` in the server settings, or nothing runs without a player.
- UDP datagrams above ~4 KB may be dropped, so everything is chunked.
- `can_place_entity{build_check_type = manual_ghost}` accepts a ghost over an entity of the force that is marked for
  deconstruction (not over an unmarked one; `manual` refuses both). Planner v2 rebuild plans therefore place their
  replacement ghosts at once on approve; the deferred path in `plans.lua` only covers what the engine refuses.
- Burner pair (burner drill dropping straight into a stone furnace, burner inserter into a chest): 14-15 plates/min
  per pair; a burner drill into a chest: 15 coal or stone/min (`tests/live_validator.py v2_*`).
- Reviving a ghost under a standing character fails (`revive()` returns nil): the helper steps off the ghost first.
- A tree whose position is just outside a ghost's tiles can still block it with its collision box: the helper clears
  trees whose box overlaps the ghost, not only those standing on its tiles.
- A small steam plant needs no fuel belt: one hand-fed boiler and 1-2 steam engines power a few labs; the helper's
  coal round keeps the boiler filled (`tests/live_onsite.py --stage2-minutes`).
- A burner drill whose output tile is still a ghost drops one ore on the ground and waits; a furnace can still be
  built there afterwards (the item does not block it). The helper's refuel skips such drills until their furnace or
  chest is built (`tests/live_convert.py`).
- Research triggers: furnace output counts (stage-1 pairs unlock steam power and electronics by themselves), but a lab
  crafted by the helper (a character without a player) does not trigger red science; the player has to craft one.
- A script-placed pole ghost auto-connects to whatever pole or pole ghost is nearest, including a player's ghost the
  helper never builds, and skips the direct link to a pole it already reaches through that ghost: a planned line can
  stay cut. `plans.lua` therefore wires every pair of the plan's poles within reach explicitly after applying.
- Planner v3 blocks (`tests/live_v3_blocks.py`, engine checks E1-E7 of PLANNER-v3.md): a burner drill in the
  belt-side columns and top rows of an east 3x3 electric-drill slot drops on the same belt lane (the right one) as
  the electric drill of that slot; burner drills and their burner inserters both built EMPTY run on coal the
  inserter takes from a belt (the inserter fuels itself from the coal it carries); ore side-loaded from the west and
  coal from the east onto the smelter column's middle belt stay on separate lanes; a burner output inserter moves
  ~65 plates per coal; an electric-drill ghost fits (`manual_ghost`) over a burner drill + burner inserter marked
  for deconstruction, an inserter ghost over a marked burner inserter, a belt ghost over a marked chest; small poles
  on the field's gap rows (pitch 7) power every drill from one source; a boiler row with empty boilers and empty
  burner inserters starts from coal on its belt alone (black start).
- A burner inserter that moves ore AND coal into a stone furnace from a mixed belt refuels itself only when the
  furnace asks for coal; with the furnace's fuel slot kept full it burns its own fuel on ore swings and stops for
  good, sometimes holding ore (real-save copy, round 1 of planner v3). Smelting-column inserters are therefore
  electric from the steam-power stage on (same tiles, swapped in place); burner inserters only move coal.
- Planner v3 green block (`tests/live_v3_blocks.py green`, E8): fed with plates at its two ports it makes
  0.496 logistic science packs/s (plan 0.5), all on one lane of the spine (red uses the other); the smelting
  column's electric phase (E9: electric input and output inserters, poles at x 1, 4 and 9 every 4 rows) runs every
  furnace from one pole source.
- Planner v3 round 3 (`tests/live_v3_blocks.py mixed_chain split_branch`): a mining field's head can side-load its
  ore (both field lanes) onto the left lane and the coal that passed its corridor onto the right lane of ONE belt;
  that belt feeds a smelting column's middle belt straight and every furnace gets ore and coal from it (E11: trunk
  lanes ore | coal only, 287 plates from 300 ore). A splitter that replaces one tile of a plate belt (that tile is
  one half, the lateral tile the other) sends plates both along the line and into a branch starting in front of
  its other half (E12).
- A character's hand craft whose product does not fit its inventory waits for ever (round 3, real-save copy: the
  helper took apart 17 plate chests full of plates at S5 and stopped crafting the last 12 belts). The helper now
  stores its surplus in chests of its own before it fetches or crafts, and gives up a craft that waits on a full
  inventory for 10 s.
- Planner v2 ETA model vs the real helper (`tests/live_onsite.py`, `acceptance.py --no-cheat`, no debug ops): first
  plates 1.52 min measured vs 1.5 predicted on a real-save copy (quick start, 60 plates/min held), 8.2-14.7 min vs
  9.0-13.0 predicted from an empty inventory on the fixture seeds.

## Rules for changes

- Every mod event handler must be error-safe: an uncaught Lua error ends the player's game.
- Free features stay free: nothing that can be done in Lua or deterministic Python should call Claude.
- In-game Claude only gets the tools in `factorio_claude/policy.py`. Widening that list needs a good reason.
- No console commands or cheats in real games. Test servers may use them.
- Keep `mod/claude-bridge/changelog.txt` in Factorio's format and bump `info.json` together with it.
