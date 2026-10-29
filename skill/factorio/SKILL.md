---
name: factorio
description: Play Factorio 2.x alongside the player through the `factorio` MCP server — starter-base planner (verified layouts shown in the in-game Plans tab), base analysis ("check my base"), the AI co-op character, and blueprints. Use when the user mentions Factorio, their base/factory, blueprints, "foreman", "patrol", "check my base", "starter base", or asks you to build/fix/mine something in-game. Args - "foreman" (one analysis pass, on request), "coop", "blueprint <what>", "plan <what>".
---

<!-- managed by ai-helper-for-claude: the installer overwrites this file on update. To customize it, remove this line
     (then the installer leaves it alone and writes SKILL.md.new instead). -->

# Factorio co-pilot (AI Helper for Claude)

Install folder: `{{INSTALL_DIR}}` (mod in `mod/claude-bridge`, MCP server `factorio_claude/server.py`, hidden chat
companion `factorio_claude/companion.py`, one-click `launcher.py`, blueprint library `blueprints/`, foreman log
`foreman-notes.md`). Setup/repair: run the one-line installer again, or `install.py` with the folder's venv python
(`install.py --check` shows what is set up).

## What's free in-game (don't spend tokens on these)

The player starts the game with the **Factorio (Claude)** shortcut/app: it starts the game and a hidden companion
(single-player is fine, no hosting). The mod does the simple stuff itself, no LLM:
- **Claude panel** (top-left button / Ctrl+Shift+C): *Helper* tab (spawn, follow, come, stop, dismiss, wood, mine
  stone/iron/copper/coal, build ghosts, refuel, give all, hand it the cursor item, Clear Claude's ghosts), *Foreman*
  tab (rule-based alerts every 30 s + map alerts), *Blueprints* tab (standard layouts straight into the cursor), *Ask
  Claude* tab (tokens, with a cost gate: build/design requests get "≈ $X, run it?" Run / Cancel first), *Plans* tab
  (proposed plans: Preview / Approve / Adjust / Reject; approved: Helper: build, Helper: refuel, Remove its ghosts).
  Approving places tagged ghosts and starts the helper building them (free).
- **Chat keywords**: `@claude help`, `@claude wood 20`, `@claude follow`, `@claude alerts`,
  `@claude blueprint smelter 8`, … Any other `@claude …` message (or the Ask Claude box) goes to the companion,
  which makes a lean `claude -p` call (short prompt, pre-fetched context).
- From here, `factorio_quick_action` runs the same free helper actions and `factorio_foreman_report` returns the
  mod's alert list. Use them instead of hand-built task lists or manual patrols for those.

## Connection

Tools talk to the live game over UDP on localhost (the launcher starts Factorio with `--enable-lua-udp 34500`),
falling back to RCON when it is enabled. "game not responding" = Factorio isn't running, is paused (single-player
menu), or was started without the launcher → ask the player to use the **Factorio (Claude)** shortcut/app.
"unknown op" / "empty response" = the save doesn't have the mod (AI Helper for Claude, internal name claude-bridge)
enabled. `factorio_run_lua` needs RCON (config `"rcon": true`).

## Planner first (any build bigger than a few entities)

Deterministic code does the layout; you pick, explain and orchestrate. Never hand-place a base.
- Starter base / "set me up": `plan_starter_base(goal_text, variants=3[, allow_removal=True])` — one call: exports
  the map, reads what the player and the helper actually carry (and chests, machines, production, research), and
  plans up to 4 validated variants, each a different build: **quick** (burner drills straight into furnaces on the
  iron patch: iron plates in minutes, no belts; copper comes with its stage 2), **balanced** (iron + copper pairs on
  their patches from the start), **rebuild** (only when taking apart or converting specific old builds scores clearly
  better: it lists exactly what it removes and why - e.g. an idle drill line whose belt goes nowhere is turned into
  pairs where it stands, only its belt removed; labs are never taken apart; the player's words "tidy up" / "reuse my
  old stuff" lower that bar) and **longterm** (the central hub only where its belts are short; on a map with far-apart
  patches "grow on site": twice the smelting on the patches - never a hub with 150+ tile ore belts). Every variant
  carries a placed and checked **stage 2** (`stages[1]`, its own plan id): a hand-fed steam engine at the water, poles
  to the player's idle labs (powered where they stand) or 2 new labs by the iron, copper pairs when stage 1 has none,
  with its bill and when red science research can start (`eta.science_start`, `eta.automation`). Each plan returns
  `eta` (minutes to first plates / full rate, from a model of what the helper really does: gathering, building site by
  site, carrying coal), `bill` (needs / have / salvage / short, and `fuel`: coal to start the burners, what they burn
  per minute, and where the coal comes from), belts (total, longest), `removes`, `gates` (quality warnings) and `card`
  (the Plans tab text). In your reply give, per variant, the time to first plates, the bill against what they have,
  how the coal keeps coming (the helper carries it from the coal drills: how far, how much of its time - and that the
  burners stop if the helper is away for ~20 min), what it removes or takes coal out of (if anything), and its stage 2
  (what, bill, when red science research can start); recommend one (`recommended`) and say why; be honest when a
  variant is slow or keeps the helper busy. They Preview and Approve in the Plans tab; Approve starts the helper: it
  takes the listed coal, dismantles the listed builds, builds only the plan's ghosts (never the player's own) and
  then keeps the hand-fed burners fuelled from the plan's coal drills every few minutes until it gets another order
  (`plan_helper(id, "round")` restarts that). When stage 1 runs and the player wants the next step, show its stage 2
  with `plan_preview(<stages[1].id>)`; they approve it in the Plans tab (it is re-checked against the map then; its
  coal round also keeps stage 1's burners going). Red science packs are hand-crafted (offer the helper:
  factorio_agent_do craft + insert into the labs).
- `world_summary` = patches, water, free areas in one call. `plan_preview(id)` re-shows one, `plan_validate(id)`
  re-checks it against the current map, `plan_list` lists them.
- `plan_apply(id)` only when the player explicitly names the plan to build (normally they approve in the GUI).
  `plan_helper(id, what="build"|"refuel"|"round")` = the free helper buttons of an approved plan.
- Raw `factorio_place_ghosts` / `blueprint_place` refuse more than 50 ghosts unless `allow_large=True` (only if the
  player insists). Pass `plan="<short id>"` when placing a few ghosts by hand, so `factorio_clear_claude_ghosts` can
  remove them.

## Ground rules

- Coordinates: tiles, x → right, **y → down**. Entity positions are centers: 1x1 at `.5`, 2x2 on whole numbers, 3x3 at `.5`.
- Directions: north/east/south/west. **Inserters: direction = side they pick up from**; they drop on the opposite side.
  To move items east, the inserter faces west.
- Look before building: `factorio_map` (ASCII) + `factorio_inspect` around the target. Ghosts never go on occupied tiles.
- Prefer non-cheat actions: ghosts/blueprints (the player or robots build them) and the agent character (uses real
  items, walks, mines, crafts). `factorio_instant_build`, `factorio_agent_give`, `factorio_run_lua` are cheats/escape
  hatches — only when the player asks. `run_lua` also disables achievements for the save.
- Don't deconstruct or rebuild the player's stuff without asking, unless they set you loose on it. Ghosts are cheap
  to suggest.
- Keep in-game chat (`factorio_say`) short. Big explanations go in the terminal.

## Mode: foreman (one analysis pass, when the player asks)

Routine monitoring is free in-game (the mod's foreman). Use this mode only when the player wants analysis
("check my base", "why is X slow", "what should I build next"); don't set up recurring loops for it.
Goal: explain problems, fix the obvious ones, and tell the player about the rest. One pass ≈ a handful of tool calls.

1. `factorio_foreman_report` (free alert list) + `factorio_overview` — power, research, top production (10m), grouped problems.
2. Read `foreman-notes.md` in the install folder (last passes) to compare trends. Things worth flagging:
   - power: consumption ≥ ~90% of production, or accumulators draining → brownout risk
   - `no_fuel` / `no_minable_resources` (ore patch running dry) / `no_power` / `low_power`
   - `no_ingredients` in clusters → an upstream starvation; trace it (inspect the input belt / supplier)
   - `full_output` in clusters → backed-up output, consumer missing or belt blocked
   - research idle (`current` null) → suggest/queue next tech if the player said that's OK
   - production of a key item dropping vs last pass (iron/copper plates, circuits, science)
3. For each real issue, dig one level (`factorio_problems` with a radius, `factorio_inspect` the sample position).
4. Safe auto-fixes (do them): queue research if the queue is empty and the player has okayed it;
   `factorio_quick_action` `refuel` for burner stuff (the helper uses fuel it carries); place ghosts for an obviously
   missing belt piece. Anything else → suggest.
5. Append a dated entry to `foreman-notes.md` (5–10 lines: key rates, power headroom, issues, actions taken).
6. If something needs the player, `factorio_say` a one-liner in game. Report a short summary in the terminal.

## Mode: co-op (Claude's character)

- One-shot helper actions: `factorio_quick_action` (spawn, follow, come, wood, stone/iron/copper/coal + count,
  build_ghosts, refuel, give_all, take_hand, stop, dismiss) — same as the free in-game buttons.
- Multi-step jobs: `factorio_agent_spawn` (no kit unless the player says so), then `factorio_agent_do` with a task
  list; poll `factorio_agent_status`. Tasks auto-walk to targets. On failure the queue halts with a reason — re-plan
  from there.
- Useful patterns:
  - gather: `{type:"mine", entity_type:"tree", count:10}`, `{type:"mine", name:"iron-ore", count:50}`, `{type:"mine", name:"stone", count:20}`
  - bootstrap: mine stone → `craft stone-furnace` → `place` near ore/coal → `insert` coal + ore → later `take` plates
  - helper builder: the player stamps a blueprint as ghosts → give the agent the items (chest + `take`) → `build_ghosts`
  - hand items over: `{type:"give_player", item:"iron-plate", count:50}`
- Crafting uses the character's real inventory and recipes the player has unlocked.
- The character is orange and labeled "Claude"; it can die to biters. `factorio_agent_remove` drops its inventory in a chest.

## Mode: blueprint

- The player can put the standard layouts in their cursor for free (Blueprints tab / `@claude blueprint …`); use the
  tools for custom variants or when they ask you.
- Proven generators: `blueprint_generate` kinds `smelter_column`, `assembler_line`, `mining_outpost`, `solar_block`
  (belts flow north; poles auto-wired). Outposts must be stamped onto ore.
- Custom designs: build an entity list → `blueprint_encode` → **always** `blueprint_check` (overlaps / bad names) →
  hand over. Electric poles in the list are wired automatically.
- Learn the player's style: `blueprint_capture` an area they built, `blueprint_decode` it, adapt.
- Delivery: strings are saved in `blueprints/` and copied to the clipboard (the player presses Ctrl+V in game), or
  `blueprint_place` stamps small ones as ghosts directly (mode `forced` clears trees; > 50 entities is refused
  unless `allow_large=True` — use the planner for bases).
- Ratios to remember: 1 yellow belt = 15/s; stone/steel furnace 0.3125/0.625 plate/s → 48/24 furnaces per yellow belt;
  green circuit 3 copper cable : 2 cable assemblers per 3 circuit assemblers (asm2); 1 boiler : 2 steam engines; 1 offshore pump : 20 boilers.
