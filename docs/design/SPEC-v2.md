# factorio-claude v2 spec — "one click, zero tokens for the simple stuff"

> Historical design brief (written for the coding agents that built v0.2). Kept for the protocol and module
> contracts; some details (paths, Windows-only launcher, version numbers) are out of date. See README.md and
> CONTRIBUTING.md for the current state.

User goals (the player):
1. Things that don't need an LLM must NOT use Claude (tokens/subscription). Spawning the helper, telling it to
   follow / chop wood / mine / build ghosts / refuel, generating standard blueprints, base health alerts → all done
   by the mod in Lua, triggered by **in-game buttons** (and free chat keywords).
2. One click to play. No command prompts, no manual linking, no "host multiplayer" requirement.
   The desktop shortcut launches Factorio + a hidden companion; companion exits when Factorio exits.
3. Claude (tokens) is only used when the player explicitly asks it something ("Ask Claude" box in the GUI, or an
   `@claude` chat message that isn't a free keyword command), and those calls should be lean.

Ground truth already verified in this engine (Factorio 2.1.23, Space Age) — don't re-litigate:
- Mods must declare `"factorio_version": "2.1"`.
- Inserter `direction` = side it picks up FROM; drops opposite.
- Ghost placement check: `build_check_type = script_ghost` plus explicit "existing ghost in footprint" check.
- Blueprint poles need explicit `wires`: `[[a, 5, b, 5], ...]` (5 = pole_copper). Blueprint `version` for 2.1.23 =
  `(2<<48)|(1<<32)|(23<<16)`. 16-way directions: north 0, east 4, south 8, west 12.
- Electric network stats: `output_counts` = production, `input_counts` = consumption (inverse of item stats).
  Item stats: input = produced. `get_flow_count` returns per-minute (items) / per-tick joules (electric).
- `helpers.recv_udp()` (call every tick) + `on_udp_packet_received {payload, source_port, player_index}` +
  `helpers.send_udp(port, data, for_player?)` work (tested headless, ~33 ms RTT). A 60 KB datagram was dropped →
  **chunk everything at ≤ 4000 bytes** in both directions. Requires launch flag `--enable-lua-udp 34500`.
  `player_index` is 0 on a server/single instance; only pass `for_player` to send_udp when it's > 0.
- `helpers.encode_string(s)` = deflate+base64 → blueprint string is `"0" .. helpers.encode_string(json)`;
  `LuaItemStack.import_stack(str)` puts it into a stack (e.g. `player.cursor_stack`).
- `player.add_custom_alert(entity, SignalID, message, show_on_map)` exists. `require("mod-gui")` exists in core lualib.
- Headless test server: `powershell -NoProfile -ExecutionPolicy Bypass -File testenv/restart.ps1` (copies
  `mod/claude-bridge` into `testenv/mods`, starts fresh from `testenv/write/test.zip`, RCON 27016 / `testpw`,
  UDP 34500, auto_pause off). State resets on every restart. Test map has NO players (headless) and uncharted
  chunks — chart first: `game.forces.player.chart(game.surfaces.nauvis, {{-300,-100},{300,300}})`.
  `testenv/t.py` is an RCON harness (`exec` it; gives `B(op, **args)` and `rcon.lua(code)`).
  NEVER kill factorio.exe by image name (the player's real game may be running) — only via restart.ps1.
- Python: `.venv\Scripts\python.exe` in the repo (MCP SDK 2.x: `from mcp.server.mcpserver import MCPServer`).
- `claude -p` headless needs `--tools ToolSearch` (MCP tools are deferred) and the MCP launcher `run_server.py`.

## Mod layout (mod/claude-bridge, version 0.2.0)

```
info.json        factorio_version 2.1, base >= 2.1.0
data.lua         custom-input "claude-toggle-gui" (key_sequence "CONTROL + SHIFT + C")
settings.lua     runtime-global: claude-foreman-enabled (bool, true), claude-foreman-interval (int seconds, 30, min 10),
                 claude-foreman-chat (bool, false)  — locale in locale/en/claude.cfg
control.lua      requires modules, calls events.finalize()
scripts/events.lua     tiny multi-handler dispatcher: events.on(defines_or_name, fn), events.on_nth_tick(n, fn),
                       events.on_init(fn), events.on_load(fn), events.on_configuration_changed(fn), events.finalize()
scripts/util.lua       shared helpers moved from the old control.lua (round, pos_of, pos_out, dist, first_player,
                       get_player, get_surface, get_force, parse_dir, dir_names, status_names, area_from,
                       contents_to_map, inv_contents, entity_at, entity_brief)
scripts/api.lua        `api.ops` registry; `api.register(name, fn)`; `api.call(op, args) -> table {ok=, result=|error=}`;
                       all existing observation/action/blueprint ops; the `/claude <json>` command (RCON path, unchanged)
scripts/transport.lua  UDP server (protocol below) + inbox for Claude-bound messages + reply history
scripts/agent.lua      the co-op helper character (existing task engine) + new tasks + quick actions
scripts/local_commands.lua  free chat keyword commands
scripts/foreman.lua    free rule-based base monitor
scripts/blueprint_gen.lua   Lua port of the generators; put blueprint in the player's cursor
scripts/gui.lua        top-left mod-gui button + panel
```

### Module contracts (exact names — other modules call these)

agent.lua
- `agent.get()` -> state table or nil (alive helper)
- `agent.spawn(player) -> ok:boolean, msg:string`   (near player; no free items)
- `agent.dismiss() -> msg`   (inventory → wooden chest at its position, then removed)
- `agent.queue(tasks, replace) -> count`; `agent.stop()`; `agent.status() -> table` (JSON-safe)
- `agent.quick(player, action, opts) -> msg:string` — actions:
  `"spawn"`, `"dismiss"`, `"follow"` (follow player until stopped — no time limit), `"stop"`,
  `"come"` (walk to player), `"wood"` (mine 10 trees), `"stone"`/`"iron"`/`"copper"`/`"coal"` (mine `opts.count` or 50
  of that resource, nearest to the player), `"build_ghosts"` (radius 32 around player), `"refuel"` (fill burner
  entities within 32 of player with fuel from helper inventory), `"give_all"` (hand all helper items to player),
  `"take_hand"` (move the player's cursor stack into the helper's inventory — immediate, no task).
  If no helper exists, every action except dismiss/stop auto-spawns it first.
- new task types: `refuel {x?,y?,radius}`, `give_all {player?}`; `follow` with `ticks=nil` = forever.
- Existing task engine semantics stay (auto-walk, halt on failure). Also: when halted, print a one-line reason to
  the player who issued the last quick action (free feedback).

foreman.lua
- `foreman.scan(force) -> report` and `foreman.get_report(force) -> report` (cached last scan), where
  `report = {tick, alerts = {{severity="high"|"medium"|"info", kind, text, count, surface, position}}, stats = {...}}`
- kinds: `power_shortage` (network satisfaction < 98% when demand > 0, using electric stats 5s window),
  `no_fuel` (burner entities), `depleted` (mining drills no_minable_resources), `no_power`/`low_power`,
  `research_idle` (labs exist and no current research), `output_full` (≥ 3 machines full_output),
  `starved` (≥ 5 machines no_ingredients — info).
- Runs on nth_tick per setting when enabled; adds native custom alerts (`add_custom_alert`) for high/medium alerts
  only when an alert is new (don't spam every scan); if claude-foreman-chat is on, prints new alerts to chat once.
- Registers api op `foreman_report` (returns the cached report; `args.rescan=true` forces a scan).
- Must be cheap: cap entity scans (types list, one find per scan), skip when no players connected.

blueprint_gen.lua
- `blueprint_gen.KINDS` = ordered list of `{id, title, params = {{name, title, type="int"|"choice", default, min, max, choices}}}`
  covering: smelter_column (count, furnace stone/steel/electric), assembler_line (count, recipe = choice of common
  intermediates incl. iron-gear-wheel, copper-cable, electronic-circuit, automation-science-pack,
  logistic-science-pack, inserter, transport-belt; machine assembling-machine-1/2/3), mining_outpost (rows, drill
  electric-mining-drill), solar_block (pairs, panel_rows, accumulator_rows).
  Layouts must match `factorio_claude/blueprints.py` exactly (same coordinates/directions/wires) — port, don't redesign.
- `blueprint_gen.build(kind, params) -> blueprint_string` and `blueprint_gen.to_cursor(player, kind, params) -> ok, err`
  (clears cursor, imports string into cursor_stack).
- api op `blueprint_generate {kind, params}` -> {blueprint}.

transport.lua (UDP + Claude inbox)
- Protocol (all text, `|`-separated header, ≤ 4000-byte datagrams):
  request part: `Q|<id>|<seq>|<total>|<chunk>` (seq 1-based). When all parts of `<id>` arrived, concat → JSON
  `{"op":..., "args":...}` → `api.call` → JSON string of `{ok,result|error}` → reply parts
  `A|<id>|<seq>|<total>|<chunk>` to `source_port` (and `for_player` if player_index > 0).
  Partial-request buffers live in `storage.udp_parts` and are dropped after 600 ticks.
- `helpers.recv_udp()` every tick (wrapped in pcall; UDP disabled = silent no-op).
- Inbox (Claude-bound messages): `transport.ask(player, text, source)` source = "gui"|"chat";
  ops: `inbox_pop` (returns+clears messages, and sets `storage.companion_seen_tick = game.tick`; acknowledged form
  `{ack, epoch?}` -> `{messages, epoch}` drops only ids <= ack so a poll lost to a UDP timeout is redelivered),
  `claude_reply {text, to?, rid?}` (rid = idempotency key for retried deliveries; stores in history, prints `[Claude] text` to chat, refreshes GUIs),
  `claude_status {status}` ("thinking"/"idle"/custom text).
- `transport.history(n)` -> last n `{who, text, tick}`; `transport.companion_online()` -> boolean (seen within 600 ticks).
- on_console_chat: messages starting with `@claude` / `claude:` / `claude,` → first `local_commands.try(player, text)`;
  if handled, print its reply (free, no inbox). Otherwise `transport.ask(player, text, "chat")`.
  If the companion is offline, tell the player once: "Claude companion isn't running — launch via the Factorio
  (Claude) shortcut. Free commands still work: @claude help".
- GUI refresh hook: `transport.on_change = function() end` (gui.lua overrides it).

local_commands.lua
- `local_commands.try(player, text) -> handled:boolean, reply:string`
- keywords (case-insensitive, first word; optional number): help, spawn|come|here, dismiss|bye|leave, follow,
  stop|halt, wood|chop|trees [n], stone [n], iron [n], copper [n], coal [n], build|ghosts, refuel|fuel,
  give|gimme, status|where, alerts|report (foreman summary), blueprint <kind> [count] (to cursor).
  Anything else → not handled (goes to Claude).

gui.lua
- mod-gui top button (sprite "entity/character" or text "Claude"), toggled also by custom-input claude-toggle-gui.
- Panel (draggable frame, ~360px) with a tabbed-pane: **Helper** | **Foreman** | **Blueprints** | **Ask Claude**.
  - Helper: status line (alive/pos/current task/halted reason), buttons for every quick action; number field for
    mine count; "Hand item in cursor to helper" = take_hand; small inventory summary.
  - Foreman: last report alerts list (severity colored), "Scan now" button, last scan time. Free.
  - Blueprints: kind dropdown, dynamic param widgets from KINDS, "Put in cursor" button. Free.
  - Ask Claude: companion online/offline indicator, multi-line text-box, "Send" button (uses tokens, label says so),
    status ("thinking…"), last ~8 history entries.
- All button names prefixed `claude_`; handle on_gui_click / on_gui_selection_state_changed / on_gui_confirmed;
  rebuild-safe after save/load and mod updates (on_configuration_changed destroys + rebuilds).
- Refresh panel every 60 ticks only while open (helper status) and on transport.on_change.

## Python side (factorio_claude/)

link.py  — `call(op, **args)` used by everything. UDP first (port from config `udp_port`, default 34500; bind
  ephemeral 127.0.0.1 socket; chunk protocol above; timeout ~4 s → "game not responding (not running, paused, or
  launched without the Claude shortcut)"), RCON fallback (existing rcon.py) only if UDP gets no reply and RCON
  config present. Thread-safe. Keep `rcon.bridge`/`rcon.lua` working (rcon.lua still RCON-only).
server.py — switch to link.call; keep tool set; keep compact JSON outputs.
companion.py — hidden background process:
  - single instance (lock: bind 127.0.0.1:34501 TCP listen socket; if taken, exit quietly)
  - every 1 s: `inbox_pop`; if messages → `claude_status thinking` → ask Claude → `claude_reply` → `claude_status idle`
  - lean Claude calls: model from config `chat_model` (default "sonnet"); short system prompt (no full SKILL.md);
    include a compact pre-fetched context in the prompt (player pos, helper status one-liner, foreman alerts top 5)
    so Claude needs fewer tool calls; rotate session after `chat_session_turns` (default 12) turns.
  - log to `logs/companion.log` (rotate ~1 MB); never pop a console window (subprocess CREATE_NO_WINDOW).
  - exits when no Factorio process (excluding testenv servers) has been seen for 60 s (after it was seen once, or
    5 min after start if never seen).
launcher.pyw — one-click: if Factorio isn't running, start it via
  `steam.exe -applaunch 427520 --enable-lua-udp 34500 --rcon-port 27015 --rcon-password <pw>`; start companion
  detached with pythonw (CREATE_NO_WINDOW | DETACHED_PROCESS) unless already running; exit.
install.py — idempotent setup (what I did by hand): venv deps present, junction mod into %APPDATA%\Factorio\mods,
  enable in mod-list.json (backup once), register MCP server user-scope (`claude mcp add ...` only if missing),
  desktop shortcut "Factorio (Claude)" → `.venv\Scripts\pythonw.exe launcher.pyw` (icon factorio.exe), delete the
  old "Claude Factorio Chat" shortcut. Print a short summary.
chat_daemon.py — delete (replaced by companion.py); chat-daemon.bat — delete.
config.json gets `udp_port`, `chat_model`, `chat_session_turns`.
