# Autopilot: "Let Claude play"

> Design brief for the pass after v0.5.0 (target v0.6.0). The host can turn on an autopilot in the bridge. Claude
> then plays the game by itself: it observes the base, picks the next goal from a tech and production roadmap,
> queues research, designs builds through the planner, approves its own plans, has the helper build them, checks
> them, fixes problems, and keeps going past the starter base. Requirements P1-P4 and acceptance criteria Q1-Q6 are
> binding. Game numbers in this document come from the installed 2.1 `data/base` prototypes, not from memory, and
> they become data with tests (section 6). Section 13 splits the work into at most three implementation rounds. The
> core is P1 working end to end, together with P3 and a solid P2.

## 1. Why

The owner's request, in his words: *"Using the bridge I'd like a feature where Claude just kind of plays the game
for itself. It should have a complete understanding of everything and how it works and go to town building the most
efficient successful base on its own. I think Claude spends way too much time trying to make everything in its own
inventory instead of automation, and also needs a better understanding of organization and scale in this game,
since they both play a part in how the factory gets laid out."*

Below is what the code does today (v0.5.0, checked during the survey) and what is missing:

- **Nothing plays by itself.** Every step waits for a player. Chat Claude answers one request at a time.
  `plan_apply` is refused in chat mode, and only the player's Approve button applies a plan. `plans.lua`
  `check_ready()` offers the next stage, and then nothing happens until a click.
- **The master plan stops at S5.** `master.STAGES` runs from 0 to 5. No stage exists after automated green
  science, and no mall zone is reserved, although PLANNER-v3 section 6.7 calls for one.
- **The helper hand-crafts first.** In `agent.lua` `new_planner` / `P.acquire` the order is: own inventory, then
  chests and furnaces within 150 tiles, then the player's inventory, then **recursive hand-crafting (depth 5, no
  size cap)**, then hand-smelting, chopping wood and hand-mining. Assembler outputs are never a source. Nothing
  prefers waiting for a machine that is producing the item. Craft time is not counted (`a.stats` holds only
  active, walking and mining ticks).
- **No organization or scale knowledge.** The prompts hold no rules about mall, bus, block sizing or headroom. On
  a real-save copy (7.8 h of play), iron ore travels about 170 tiles on several parallel belts. The labs sit in
  three places. There is no bus, no mall, and no room reserved for growth. The two power networks are split by one
  missing pole, which leaves 31 inserters and 4 labs unpowered. The research queue has been empty for a long time.

## 2. Requirements (binding)

| # | Requirement | In short |
|---|---|---|
| P1 | Autopilot | Only the host can toggle it, from the GUI, from chat (`@claude autopilot on\|off\|status`) or from an MCP tool. While it is on, Claude repeats a cycle with no further input: observe, pick a goal, research, plan, self-approve, build, verify, fix. It goes past S5. A status line is always visible. Chat gets short updates at milestones only. Pausing or switching off stops it cleanly, and it resumes from saved state. It never removes the player's builds (unless the removal setting is on), never cheats, and never builds where the player is working. Claude calls are paced. It works on Windows and macOS. |
| P2 | Game knowledge | A compact, verified Factorio 2.x knowledge base that both the autopilot and chat Claude load. It holds data and rules (doctrine, tech roadmap and unlocks, ratios, organization, scale), not just prose. |
| P3 | Less hand-crafting | Acquisition prefers automation: mall and producer outputs, then storage, then bounded hand-crafting for bootstrap only. Repeated needs get a mall. A hand-craft metric appears in status. |
| P4 | Organization and scale | After S5, expansion follows the doctrine: a bus with planned lanes, smelting columns sized to targets, a mall zone, power growth and science blocks, all on a tidy grid with reserved room. The master plan grows; it is not replaced. |

## 3. Architecture

The autopilot is split across the two existing processes, and each side does what it is good at.

```
 Factorio (mod, Lua)                               Companion (Python)                        claude -p
 ---------------------------------------------     -------------------------------------    -----------
 autopilot.lua  storage.autopilot (persisted)      autopilot.py
   toggle (host-only), status line           <-->    Observer  -> Snapshot (every ~10 game-s)
   keep-out ring (player builds/positions)   UDP     Director  -> deterministic rules R0-R9
   approve_queue -> plans.apply (Lua only)           Pacer     -> calls/hour cap, gaps, back-off
   counters (hand-craft, calls mirror)               Decider   -> ask_autopilot()  ----------->  doctrine +
 plans.lua   owner = "autopilot", mall_chests        Executor  -> planner in-process, ops   <---  snapshot +
 agent.lua   acquisition order, wait_collect,      knowledge/  doctrine.json, roadmap.json          options
             craft counters, t.autopilot tag       planner/    expand.py, blocks.mall_block         (decision)
 gui.lua / local_commands.lua  toggle, status      server.py   autopilot tools, owner arg
```

Principles:

1. **Deterministic first.** Observing, research queueing, known fixes, stage offers, sizing and placement are
   code. Claude is called only for decisions the rules cannot make (section 4.4). Most cycles cost no call at all.
2. **Approval happens in Lua, never through Claude.** `plan_apply` stays out of every Claude allowlist. The mod
   auto-applies plans whose `owner == "autopilot"` once the safety checks pass (section 5).
3. **The mod is the source of truth for on/off.** The companion runs the loop only while `autopilot_get` returns
   `on = true`. Nothing in the companion can turn it on: only the host can, through the GUI, chat or a full-mode
   MCP tool.
4. **One Claude process at a time per companion.** The autopilot and chat share the companion's worker lock. Chat
   from the host has priority. An autopilot decision waits, or is skipped if it has gone stale.

### 3.1 Mod side: `scripts/autopilot.lua` (new)

```lua
storage.autopilot = {
  on = false, by_index = nil, since_tick = 0, epoch = 0,   -- epoch bumps on every on/off (stale results dropped)
  phase = "off",               -- mirror of the companion phase (section 4.1), for the status line
  goal = nil, step = nil, next_step = nil, note = nil,      -- written by the companion via autopilot_status
  milestones = {},             -- ring of 50 {tick, text}
  approve_queue = {},          -- [plan_id] = {due = tick, deferred = n, reason = ...}
  keepout = {},                -- ring of 200 {x, y, tick} from player builds/mines
  baseline = nil,              -- set of unit_numbers present when autopilot was first turned on (section 5.3)
  owned = {},                  -- [unit_number] = plan_id for entities built from autopilot plans
  calls = {window = {}},       -- mirror of the companion's call log (ticks), shown in the GUI
  counters = {},               -- [hour] = {craft_ticks, crafted = {}, acquired = {source = n}, placed = n}
  mall_chests = {},            -- unit_numbers of mall output chests (from plan_preview mall_chests)
}
```

Ops (UDP, the same transport as the other ops):

| Op | Who | Does |
|---|---|---|
| `autopilot_get` | companion, MCP | State plus `game.tick_paused`, `game.speed`, the connected host, keep-out boxes and counters |
| `autopilot_set {on}` | host only, checked in Lua with `access.is_host` (the server console and RCON count as host) | Toggle. Bumps the epoch. Prints "Claude autopilot on/off (by X)" to everyone |
| `autopilot_status {goal, step, next_step, note, phase, calls}` | companion | Updates the status line. Cannot change `on` |
| `autopilot_milestone {text}` | companion | Appends to the ring. Prints one chat line, at most one per 2 game-min |
| `autopilot_keepout` | companion | Boxes, radius `claude-autopilot-keepout` (default 16), around each connected player and around every player `on_built_entity` / `on_player_mined_entity` / `on_player_rotated_entity` in the last 15 game-min |

Events: `on_built_entity` with `player_index` adds to the keep-out ring. `on_nth_tick(60)` processes the
approve queue. `on_research_finished` and `check_ready()` call `autopilot.on_proposed(p)`. `script_raised_built`
from the helper adds to `owned` when the ghost carries an autopilot plan tag.

Settings (runtime-global, in `settings.lua` and `locale/en`):

| Setting | Default | Meaning |
|---|---|---|
| `claude-autopilot-allow-removal` | off | The autopilot may remove entities that are not its own (that is, the player's) when a plan lists them |
| `claude-autopilot-keepout` | 16 | Keep-out radius in tiles |
| `claude-autopilot-grace-s` | 30 | Game seconds between "Autopilot builds X" and applying it (Reject in that window stops it) |
| `claude-autopilot-use-player-items` | off | The helper may take from the player's inventory while the autopilot is on |
| `claude-autopilot-calls-per-hour` | 6 | Fallback cap when the companion config has none (the companion's value wins when lower) |

### 3.2 Companion side: `factorio_claude/autopilot.py` (new)

`Autopilot(link, cfg, gd)` runs in its own thread, started by `companion.main` and ticked about every 2 s of real
time. Its parts:

- `Observer.snapshot() -> Snapshot`: research (done, current with progress, queue, available), production
  per minute over 10 game-min (ores, plates, gears, circuits, packs), power (per network: production, demand,
  satisfaction), problems and foreman alerts, the helper's status (task, halted, stats, craft share, shortages),
  `plan_list` (status, future, owner), the master roadmap (`M.stage_status`), inventory totals (helper, chests, mall
  chests), the keep-out list, and the census. Every free-text field (entity tags, chest labels, train-stop names,
  player names) is dropped before anything reaches a prompt.
- `Director.step(snap) -> Action | None`: the rule table in section 4.3. Pure functions, unit-tested.
- `Pacer`: the call budget and back-off (section 4.5).
- `Decider.ask(snap, options) -> Decision`: the Claude call (section 4.4).
- `Executor.run(action)`: calls the planner in-process (`plan_starter_base`, `plan_next_stage`, `expand.extend`,
  `plan_helper`), sends ops (`queue_research`, `agent_do`, `autopilot_status`), and records the outcome.
- `Journal`: `logs/autopilot.jsonl`, one line per observation summary (every 5 game-min), action, decision and Claude
  call (reason, duration, outcome). The live harness reads it.

State that must survive a companion restart lives in the mod (goal, step, milestones, owned set) or in
`plans/*.json` (masters). The companion keeps only caches and rebuilds them from `autopilot_get` and `plan_list`
after a restart or a save load.

## 4. Decision loop

### 4.1 State machine

```
              host toggles on                                       host toggles off / epoch change
   OFF ----------------------------> START ---------------------------------------------------> OFF
                                       | adopt or create master, baseline set
                                       v
   PAUSED <--- game paused / link ---- OBSERVE <-------------------------------------------+
     | resumes                         | snapshot                                         |
     +-------------------------------> DECIDE  (Director rule? -> ACT; none -> Claude?)    |
                                       |                                                  |
                                       v                                                  |
                                      ACT  (research / plan / fix / helper order)          |
                                       |                                                  |
                       plan proposed   v                                                  |
                                     WAIT_BUILD  (grace -> applied -> helper builds) ------+ built
                                       |                                                  |
                                       v                                                  |
                                     VERIFY  (rates vs plan, power, problems) -------------+ ok / fix queued
   STUCK  <--- no progress 5 game-min (any phase) ---> recovery ladder (4.6) ------------> OBSERVE
   HALTED <--- 3 failed recoveries or a fatal error: status line says why, chat once; host can toggle off/on
```

- **OFF.** Nothing runs. The companion only polls `autopilot_get` every 10 s.
- **START.** This runs once per `on`. If an autopilot-owned master exists, the autopilot continues it. If a
  master the player approved exists in `plans/` and its stages match the save (`plan_list`), the autopilot adopts
  it (`owner` becomes "autopilot" for its future stages only). Otherwise R3 plans a new one. START also records the
  baseline set (section 5.3).
- **PAUSED.** Entered when `game.tick_paused` is set, when the link is down (a paused game does not poll UDP), or
  when `game.speed` is 0. No calls, no actions. Pause timers do not count toward stuck time, which is measured in
  game ticks.
- **WAIT_BUILD.** This phase covers the grace period, the apply, and the helper building. Progress here is
  measured as ghosts remaining and helper activity. If the helper is halted with a reason, R6 handles it.
- **VERIFY.** For a finished stage the autopilot compares `stage_rates` (expected) with the measured production
  after 5 game-min. If the measured rate is below 70% of the expected one, it diagnoses the cause from problems
  (no input, no power, output full, no fuel) and queues a fix.
- **HALTED.** The autopilot stops acting but stays "on", so the host sees why. Toggling off and on clears it.

### 4.2 Goals and the roadmap

Goals come from `knowledge/roadmap.json`: an ordered list. Each entry has an id, a predicate on the Snapshot (done
when true), its prerequisites, and the planner template that achieves it. The Director works on the first goal
that is not done and whose prerequisites are met. Claude may reorder when the options are close (section 4.4).

| Goal | Done when | How |
|---|---|---|
| G0 restore | every power network that has consumers has a generator, satisfaction >= 0.9, no out-of-fuel burner, research queued | R1, R4, R5 |
| G1 starter base | master stages S0-S5 built and verified | R2, R3 (planner v3) |
| G2 mall v1 | mall stage built, every mall item has its chest stocked at least once | expand S6 (section 8.1) |
| G3 smelting 30 SPM | iron >= 225/min and copper >= 75/min measured, with room reserved for 2x | expand: column extension and second column |
| G4 power headroom | peak demand <= 66% of capacity | expand: boilers into the reserved slots, then a second block |
| G5 steel | steel-processing researched, steel >= 15/min | expand: steel column |
| G6 science 30 SPM | red >= 30/min and green >= 30/min measured, labs sized to the techs in queue | expand: second red/green block plus labs |
| G7 tier 2 research | automation-2, fast-inserter, advanced-material-processing, logistics-2 done | R1 research order |
| G8 science 60 SPM | 60 red + 60 green per min | expand, sized from doctrine |
| G9 oil and blue (round 3 stretch) | chemical science >= 10/min | expand: oil block (pumpjacks, refineries, chemical plants), engine, advanced circuit and blue blocks |

Research follows `doctrine.research_order`, filtered to techs that are available and either needed by an open
goal or cheap (<= 50 packs x unit time 15 s). It is checked against the game data (section 6.3). Trigger techs are
handled as actions, not as research: steam-power (craft 50 iron plates), electronics (craft 10 copper plates),
automation-science-pack (a player must craft a lab, see section 13 open decision D3) and oil-processing (a pumpjack
must pump crude).

### 4.3 Director rules (deterministic, free)

The Director evaluates the rules in order. The first rule that returns an action wins. Every action carries a
`reason` and the goal it serves.

| Rule | Trigger | Action |
|---|---|---|
| R0 safety | keep-out intersects a pending autopilot plan; player connected and building nearby | defer the plan (mod does it), status "waiting for you to finish nearby" |
| R1 research | queue empty or shorter than 2, labs present | queue the next techs (section 4.2), at most 3 at once |
| R2 next stage | an autopilot master has a stage with status "now" or "ready" | `plan_next_stage(owner="autopilot")` in-process; the mod applies it after the grace period |
| R3 starter | no master (autopilot-owned or adoptable) | `plan_starter_base(owner="autopilot")`, recommended variant, `allow_removal` from the setting; Claude picks the variant only when the planner returns two within 10% of score |
| R4 power split | a network with consumers and no generator, within 20 tiles of a generating network | a small autopilot plan: poles along the shortest gap between the two networks (pole placer, reach 7.5 for small poles) |
| R5 fuel | burners or boilers out of fuel | `plan_helper("round")`, or a coal belt fix when the master has one in a later stage (prefer moving to that stage) |
| R6 helper | halted with a known reason (no path, missing items, inventory full) | replan acquisition (section 7), `stash_plan`, or skip the ghost and report it; after 3 times for the same ghost: STUCK |
| R7 verify | a stage built more than 5 game-min ago and not verified | measure, then a fix or `verified` |
| R8 expand | every master stage is built and verified, and the next goal G2+ needs a template | `expand.extend(mp, goal)` (section 8), which appends a stage; then R2 |
| R9 mall pressure | hand-craft share > 40% over the last 30 game-min, or the same item short 3 times, once the mall exists | move G2 (or a mall extension with that item) to the front |

### 4.4 When Claude decides, and how

Claude is called only for these reasons, each a `reason` code in the journal:

| Reason | Example |
|---|---|
| `milestone` | a goal is done; choose the next goal among the open ones (the roadmap order is the default) |
| `variant` | two planner variants within 10% |
| `no_rule` | a problem no rule covers, or VERIFY found a shortfall with no known cause |
| `stuck` | step 3 of the recovery ladder (section 4.6) |
| `review` | every 60 game-min while progressing: a short "is this still the right plan" check (optional, off when the cap is below 4/h) |

**Prompt.** The companion builds it; no player text is in it.

```
SYSTEM: autopilot role (short), safety rules (never remove the player's builds, no cheats, keep-out),
        knowledge.prompt_text(budget=1500 tokens, focus=goal)   -- doctrine rules + the tables relevant to the goal
USER:   {"reason": "milestone", "goal_done": "G2", "snapshot": {... <= 2 KB ...},
         "options": [{"id": "G3", "what": "smelting to 30 SPM", "est_min": 25, "bill": {...}},
                     {"id": "G4", ...}, {"id": "G6", ...}],
         "recent": [last 5 journal actions], "calls_left_this_hour": 3}
        Answer with autopilot_decide(option, goal_text, why, status_line). You may look first (read-only tools).
```

**Tools.** `policy.AUTOPILOT_TOOLS` is a new list: the CHAT_TOOLS look tools, `factorio_queue_research`, plus two
new tools that are registered only in autopilot mode (`FACTORIO_AUTOPILOT_MODE=1`):

- `autopilot_decide(option, goal_text, why, status_line)` returns the decision to the companion through a file in
  the session directory, which the companion reads after the process exits;
- `autopilot_note(text)` records a milestone line.

The list deliberately leaves out several tools:

- `plan_apply`;
- every cheat (`instant_build`, `agent_give`, `agent_spawn` with a kit);
- `run_lua`, `factorio_say`, `factorio_deconstruct` and `factorio_agent_remove`;
- direct `plan_starter_base` / `plan_next_stage`: the companion runs the planner itself after the decision, so
  Claude chooses and code executes.

`tests/test_security.py` checks that AUTOPILOT_TOOLS is a subset of the allowed tools, that it contains none of
these, and that every tool server.py defines is listed in exactly one of the lists.

**Validation.** The decision must name one of the offered option ids. Otherwise, or on a timeout (default 180 s),
an error or a missing decision, the Director takes the default option (the first in roadmap order) and the journal
records `fallback`. The status line is truncated to 80 characters and stripped of control characters. Model and
turn limits come from the config: `autopilot_model` defaults to `chat_model`, and `autopilot_max_turns` is 12.

### 4.5 Pacing and back-off

The owner uses a subscription, so there is no money gate. Calls are still a shared, rate-limited resource, so they
are paced:

| Knob (companion config) | Default | Rule |
|---|---|---|
| `autopilot_calls_per_hour` | 6 | Hard cap in a rolling 60-min real-time window. The mod setting acts as a second cap, and the lower one wins |
| `autopilot_min_gap_s` | 180 | Minimum real seconds between two autopilot calls |
| `autopilot_min_gap_game_s` | 300 | Minimum game seconds between calls, so a game at speed 10 does not burn through the cap |
| back-off | x2 | After a call whose decision did not change the snapshot hash within 10 game-min, the next non-milestone call waits twice as long (up to 30 min). Any progress resets it |
| no call | always | Never while paused, never with `game.speed == 0`, never while a Director rule applies, never while a chat request from the host is running |

The call log (real and game time, reason) is mirrored to the mod (`autopilot_status.calls`), so the GUI shows
"Claude calls: 3 in the last hour (cap 6)". When the cap is hit, a decision falls back to the default option and the
journal records `capped`; the loop never blocks waiting for a call.

### 4.6 Failure handling and stuck detection

**Progress.** A progress score is computed every snapshot. Any of these counts as progress:

- research units completed went up;
- a tech was finished;
- an autopilot-owned entity was built;
- the number of ghosts left in an applied autopilot plan went down;
- a plan's status changed;
- a goal predicate's measured value went up (for example plates per minute towards G3, or mall chest stock).

Steady production alone is not progress.

**Stuck.** No progress for 5 game-min is STUCK. Q1 fails on more than 10 min with no progress, so detection runs
at half that. The recovery ladder:

1. re-run the Director with the "current plan failed" flag: replan the helper's acquisition, skip unbuildable ghosts
   and report them;
2. stop and restart the helper's build of the current plan (`agent_stop`, then `work_area` again from Lua);
3. ask Claude (`stuck`, with the journal of the last 10 actions and the problems list);
4. mark the goal `blocked` for 30 game-min and move to the next eligible goal.

After 3 full ladders without progress the autopilot goes to HALTED, with the reason on the status line and in one
chat line.

**Errors.** Planner exceptions (`MasterError`, validation failure) mark the template attempt failed. The autopilot
retries with the next variant or a shifted zone, at most 3 attempts per goal, and then marks the goal blocked. A
link error moves it to PAUSED. Python exceptions in the loop are logged with a traceback and the autopilot retries
after 60 s; 5 in 10 minutes means HALTED. An action is never retried blindly.

## 5. Self-approval and safety

### 5.1 Who owns a plan

- `plan_preview` gains `owner` ("player" by default, "autopilot") and `mall_chests`.
- The companion sets `owner = "autopilot"` only on planner calls made by the autopilot Executor.
- The MCP `owner` argument is honoured only in full mode (Claude Code at the host's terminal). It is refused in chat
  mode and ignored in autopilot mode, where Executor calls are in-process.
- Plans tab cards show "Autopilot" and the grace countdown.

### 5.2 Auto-apply in Lua

When an autopilot plan becomes "proposed" and the autopilot is on, the mod:

1. puts it in `approve_queue` with `due = tick + grace`;
2. posts "Autopilot builds <title> in 30 s. Reject to stop it." on the card and in chat (one line).

When the plan comes due, `autopilot.lua` checks, in order:

| Check | If it fails |
|---|---|
| autopilot still on, epoch unchanged | drop |
| plan still "proposed" (not rejected or superseded) | drop |
| plan bbox does not intersect the keep-out boxes | defer 60 s, `deferred += 1`; after 30 game-min, tell the player once: "Autopilot is waiting for you to leave (x, y)" |
| every removal is either an autopilot-owned entity, a master swap of an entity a plan built (the burner-to-electric swaps), or `claude-autopilot-allow-removal` is on | strip the offending removals; if the plan no longer validates without them, drop and tell the Director (`removal_blocked`) |
| no tile overlaps a player ghost or a player entity outside the listed removals | drop (`conflict`) |

If every check passes, it calls `plans.apply(id)` and `agent.work_area(nil, bbox, "build", title, fuel, id)`, the
same path as the GUI Approve, with the tasks tagged `t.autopilot = true`. Player plans are never auto-applied.

### 5.3 The player's builds

- **Baseline.** When the autopilot is first turned on, the mod records the set of unit_numbers of every
  non-resource entity on the surface. That set, plus anything built later by a player (`on_built_entity` with
  `player_index`), is "the player's". Entities built from a plan the player approved in the past count as the
  player's too, because he approved them. The exception is a master swap within an adopted master he approved,
  since those swaps were part of what he approved.
- **No removal.** The planner gets the player's set as fixed obstacles, so expansion plans route around them, and
  `allow_removal` is false unless the setting is on. The live harness checks the baseline set at the end (Q1).
- **Keep-out.** As in section 3.1. The helper also does not use `free_furnace_spot` or `stash_plan` inside a
  keep-out box.
- **No cheats.** No instant build, no given items, no `run_lua` from the autopilot. The helper builds from
  inventory, salvage, automation outputs and bounded crafting. Debug ops stay behind the startup setting. The live
  test may use setup cheats only (peaceful mode, chart radius), and the report lists them.
- **Host-only.** The `autopilot_set` op checks `access.is_host` in Lua. The GUI checkbox is disabled for others,
  with a tooltip. In chat, `autopilot on|off` from a non-host gets "Only the host can do that". The MCP
  `factorio_autopilot` tool exists only in full mode.

### 5.4 Stopping

- **Off.** The epoch bumps and `approve_queue` is cleared. Helper tasks with `t.autopilot` are cancelled after the
  current atomic action (one placement or one take). Ghosts of applied plans stay, and the player can Reject or
  clear them. Any Claude result that arrives later is dropped by epoch. Chat says "Autopilot off. It stopped at:
  <goal> / <step>."
- **Pause.** No code is needed: UDP is not polled while paused, so the companion goes to PAUSED.
- **Save and load.** `storage.autopilot` is saved with the map. After a load with `on = true`, the status line
  says "Autopilot on, waiting for the companion" until the companion's first `autopilot_status`, and then it
  resumes at OBSERVE. Masters are reloaded from `plans/`.
- **Companion not running.** The status line shows "companion offline" after 30 game-s without an
  `autopilot_status`.

## 6. Knowledge base (P2)

### 6.1 Where it lives

```
factorio_claude/knowledge/
  __init__.py        load(), rules(), roadmap(), research_order(gd, done), ratios(gd), sizing(gd, spm, tier),
                     lane_plan(), mall_items(tier), footprints(), prompt_text(budget, focus), check(gd) -> [issues]
  doctrine.json      rules with ids, layout and scale conventions, mall lists, acquisition order, hand-craft policy,
                     throughput tables, headroom, spacing, bus lane plan
  roadmap.json       goals G0-G9 (section 4.2): predicate, prerequisites, template, target
  doctrine.md        generated by tools/gen_doctrine_md.py (human/skill readable); a test checks it is up to date
skill/factorio/doctrine.md   copy of the generated file, linked from SKILL.md
```

### 6.2 Format

Rules are short, imperative and have ids, so prompts, logs and tests can refer to them:

```json
{"id": "AUTO-1", "kind": "doctrine", "text": "Hand-craft only to bootstrap; every item needed more than ~50 times gets an assembler.",
 "applies": ["autopilot", "chat"], "weight": 1}
```

Rule groups:

- AUTO: automation first.
- ACQ: acquisition order.
- MALL: what goes in it and when.
- RES: research.
- ORG: organization, meaning the bus, columns, one-way flows and spacing.
- SCALE: sizing, headroom, and when to expand vs rebuild.
- PWR: power.
- SAFE: safety.

Numbers are data, not prose: `belt_items_per_s`, `underground_max_distance`, `inserter_items_per_s`,
`machine_out_per_min`, `column_cap_per_yellow_lane`, `steam`, `solar`, `headroom`, `spacing`, `bus_lane_plan`,
`mall_v1` / `mall_v2`, `handcraft`, `acquisition_order`, `research_order`, `trigger_techs`, `footprints`. The
researched draft (base 2.1.24) supplies the first version.

Corrections already found while writing this document, checked in the game data:

- small electric poles need wood (1 wood + 2 cables gives 2 poles), so they are not in mall v1;
- long-handed inserters come with Automation, together with assembling-machine-1;
- underground belts and splitters come with Logistics;
- electric-mining-drill needs automation-science-pack and 25 red packs, not Automation;
- assembling-machine-1 cannot run fluid recipes.

### 6.3 Verified, not remembered

Anything that is a game fact is derived from, or checked against, the planner's game data (`GameData`, dumped from
the running game, and the `testenv/fixtures/*/data.json` used in tests).

- `knowledge.check(gd)` recomputes each `machine_out_per_min` (recipe energy, crafting speed, result amount), each
  tech cost on the research order (count, unit ingredients, time), each unlock (effects), the furnaces-per-lane caps
  and the steam ratio, and returns every mismatch.
- `tests/test_knowledge.py` runs it on the fixture data and fails on any mismatch.
- `tools/verify_doctrine.py --data <factorio>/data/base` runs the same check against the installed prototypes. It
  is a developer tool, skipped in CI when the game is absent.
- Facts that are conventions (lane order, mall contents, spacing) are marked `"kind": "convention"`, and the prompt
  presents them as rules of thumb.

### 6.4 How it is used

- **Planner.** `blocks.mall_block` reads `mall_items`. `expand.py` reads `sizing`, `lane_plan`, `footprints`,
  `spacing` and `headroom`. `quality.py` reads the belt and inserter caps for throughput checks. The planner's
  `rates.solve` stays the source of truth for exact counts; the doctrine provides targets, caps and layout rules.
- **Autopilot prompt.** `prompt_text(budget=1500, focus=goal)`: every rule plus the tables for the current goal (for
  example sizing for G3, mall lists for G2).
- **Chat prompt.** `SYSTEM` gets `prompt_text(budget=400)`: the AUTO, ACQ, ORG and SCALE rules only. A test checks
  both prompt budgets (by characters/4).
- **Skill.** SKILL.md links to `doctrine.md`.

## 7. Helper acquisition order and the mall-first rule (P3)

### 7.1 New order in `agent.lua` `new_planner` / `P.acquire(item, n)`

| # | Source | Notes |
|---|---|---|
| 1 | own inventory | unchanged |
| 2 | the plan's `take_from` chests and the mall chests (`storage.autopilot.mall_chests`) | mall first, nearest first |
| 3 | producer outputs: assembler `crafter_output` and furnace output, within SOURCE_RADIUS | new: `assembling-machine` added to the find_entities types |
| 4 | other storage chests within SOURCE_RADIUS | unchanged; never a keep-out box's chest while the player is there |
| 5 | player inventory | only with `claude-helper-use-player-items`, player connected, and either the autopilot off or `claude-autopilot-use-player-items` on |
| 6 | wait_collect | new: if an assembler with this recipe exists and is working (or a mall chest is filling), queue a `wait_collect` task: walk there, take the output, wait up to 120 game-s per visit, and build other ghosts in between |
| 7 | short craft from intermediates already held | for example belts from gears and plates held: allowed if the craft takes <= 10 s |
| 8 | bounded hand-craft | allowed only (a) in bootstrap (no mall built yet and no working assembler for the recipe), or (b) when the whole job is <= `handcraft.max_job_s` (60 s) and the 10-min craft total stays under `loop_alarm_s_per_10min` (120 s) |
| 9 | hand-smelt, chop, mine | raw bootstrap only (unchanged code, after crafting is refused) |

When step 8 is refused and nothing else applies, the helper records a shortage (`stats.short[item] += n`) and builds
other ghosts. The Director's R9 turns repeated shortages into mall work.

### 7.2 Metrics (in the helper, persisted)

- `stats.craft_ticks`: added in `tick()` while a craft task runs.
- `stats.crafted[item]`: top-level items crafted.
- `stats.acquired[source][item]`, where source is `inv`, `mall`, `producer`, `chest`, `player`, `wait`, `craft`,
  `smelt` or `mine`. It counts the item as delivered for placing; intermediates are not counted, but their craft
  time is.
- `stats.placed`: entities placed.
- Hourly buckets go to `storage.autopilot.counters[hour]`.
- **Hand-craft share** = (`craft` + `smelt` items) / all acquired items placed, per hour and over the last 60
  game-min.

`agent.status()` and `base_state` expose these, and the status line shows "hand 18%".

### 7.3 Bootstrap exceptions

These are expected and reported, not failures:

- the 10 Automation packs;
- the first lab, which a player must craft (D3);
- items needed before the mall stage exists;
- wood for small poles.

## 8. Mall and post-S5 expansion (P3, P4)

### 8.1 Mall block: `blocks.mall_block(gd, items, *, mirrored=False)`

The layout is a classic single-row mall, gated by Automation (assembling-machine-1 and long-handed inserters):

```
 y0   C  C  C  C  ...      output chests (wooden, limited to 2 stacks via the inventory bar)
 y1   i  .  i  .  ...      output inserters (one per machine)
 y2-4 [AM][AM][AM] ...     assembling-machine-1 row, 1-tile gaps for small poles
 y5   i  L  .  i  L  .     inserter (plate belt) + long-handed inserter (intermediate belt) per machine
 y6   = iron | iron =      belt A: iron plate on both lanes
 y7   = gear | circuit =   belt B: gears and circuits, made at the head of the block
```

- **Head.** One gear AM1 feeds belt B's near lane. One copper cable AM1 feeds one circuit AM1 by direct insertion,
  which feeds belt B's far lane. Copper enters at the head only (a 1-belt tap). That gives about 40 circuits/min
  and 60 gears/min, far more than a mall uses.
- **v1 items.** Each is fed only by iron, gears and circuits: transport belt, burner inserter, inserter, pipe,
  electric mining drill, assembling-machine-1, gear (a chest of its own) and circuit (a chest of its own).
- **Direct-insertion pairs.** These are second-tier items: belt to underground belt and to splitter, after
  Logistics; pipe to steam engine.
- **Left out of v1.** Lab, boiler and stone furnace need inputs the row does not carry. Small poles need wood.
  These stay helper-crafted (each short) or are mall v2 work.
- **Footprint.** About 4 tiles per machine along the belt and 8 tiles across, plus a 2-tile walkway. For 12
  machines with the head, about 56 x 10, and the zone reserves 64 x 12.
- **Recipes and limits.** Recipes are set by the plan (`factorio_set_recipe` semantics in `plans.apply`). Chest
  bars are set when the helper builds the chest. `p.mall_chests` lists the output chests for the acquisition step.

### 8.2 Master growth: `factorio_claude/planner/expand.py`

`extend(mp, world, gd, state, goal) -> MasterPlan` appends one `StageSpec` (S6, S7 and so on) and its zones to the
same master:

- **Same grid.** New zones snap to the master's 32-tile grid. Blocks keep 3 tiles minimum and 6-10 preferred
  between them. The master's occupied tiles, its reserved zones, the player's baseline set and the keep-out are
  hard obstacles.
- **Bus.** The first expansion that needs plates beyond the S3/S5 science blocks (G2 mall, G6 science) lays a
  plate trunk from the smelting columns' outputs. Lanes follow `lane_plan`: iron x4, copper x4, a gap, then
  circuits, steel, gears and so on. The trunk reserves the full width of the plan even when only the first belts are
  placed. Belts run straight along one axis. Consumers branch perpendicular with splitter and underground (after
  Logistics) or side-loading (before). Branches never cross the trunk at grade.
- **Sizing.** `knowledge.sizing(gd, target_spm, tier)` gives furnaces, drills, assemblers, boilers and labs. Blocks
  are built for the current target and reserve room for 2x, with 4x for smelting columns and the trunk. Example
  (red+green, assembler 1, stone furnaces, from the draft): 30 SPM needs 5 red + 6 green AM1, 12 iron + 4 copper
  furnaces, 225 + 75 plates/min and about 3.3-3.9 MW.
- **Expand vs rebuild.** Expansion is the default: fill reserved extension rows first, then a second column or
  block in a new zone. A player's spaghetti is never rebuilt. If an old block is badly placed, the autopilot builds
  the new organized block elsewhere and leaves the old one running.

The stage templates, in roadmap order:

| Stage template | Goal | Gate |
|---|---|---|
| `mall` | G2 | automation |
| `smelt_extend` (rows) / `smelt_column` (new column per metal) | G3, G8 | — (electric drills after S4) |
| `power_grow` | G4 | — |
| `steel_column` | G5 | steel-processing |
| `science_block` x n plus `lab_rows` | G6, G8 | logistic-science-pack |
| `trunk` | part of the first stage that needs it | — |
| `oil`, `blue` | G9, round 3 stretch | oil-processing, advanced-circuit, chemical-science-pack |

**Checks.** `check_monotone` and `check_all` are extended to any number of stages: no later stage removes or
overlaps an earlier one except planned swaps. New quality gates:

- `SPAGHETTI`: belt tiles / Manhattan distance <= 1.3 per net;
- `TURNS`: at most 4 per net;
- `GRID`: block origins on the grid;
- `TRUNK_STRAIGHT`;
- `RESERVE`: reserved width kept.

**Persistence.** The master's version is bumped, earlier stage files are unchanged, and new stages are saved as
`plans/<mid>-s<k>.json` and sent to the mod as future stages, as today.

**Adopted masters.** A master from an older save, such as the real copy with S2 applied, is extended the same way.
If its S0-S5 are still future, the autopilot first finishes them.

## 9. GUI, status line and chat

- **Toggle.** A new row at the top of the Helper tab: a "Let Claude play" checkbox (host only, disabled with a
  tooltip for others) with the phase, the goal, Claude calls this hour / cap, the hand-craft share, and buttons
  "Show log" (the last 20 milestones) and "Stop now". The TABS list does not change (it is append-only).
- **Status line.** A label in the mod-gui button flow next to `claude_coords`, visible to everyone while the
  autopilot is on, refreshed every 30 ticks: `Autopilot: <goal> > <next step> | 3/6 calls | hand 18%`. In PAUSED,
  STUCK, HALTED or deferral states it shows that state and the reason instead of the next step. Players can hide
  it with a per-user setting.
- **Chat.** `local_commands.lua` handles these for free (no Claude call):
  - `@claude autopilot on`, `off`, `status`;
  - the phrase "let claude play" as an alias for on.

  Milestones (goal done, tech tier unlocked, stage built, HALTED) print one line each, at most one line per 2
  game-min. Routine steps go to the status line only.
- **MCP** (full mode only):
  - `factorio_autopilot(on: bool | None)` returns the state and toggles it;
  - `factorio_autopilot_report(hours=3)` returns the journal summary and metrics.

## 10. Metrics

Every metric is recorded in the journal (every 5 game-min) and shown in `factorio_autopilot_report`:

- research: techs done (with time), current, packs consumed per min by type;
- science per minute and plates per minute (iron, copper, steel), gears and circuits;
- power: capacity, peak demand, satisfaction, and the network count;
- census of entities by type and of autopilot-owned entities;
- decisions: Director actions and Claude decisions with their reasons;
- Claude calls per hour (and fallbacks or caps);
- stuck periods (start, length, resolution);
- errors;
- hand-craft seconds and share per hour, items by source;
- helper time split (walking, building, crafting, mining, waiting).

## 11. Test plan (Q1-Q6)

All live runs use test slots 31-39 through `testenv/restart.ps1` only. Test companions use the slot's ports and a
config under `testenv/`. The owner's game, its ports and the Factorio user folder are never touched. Real-copy data,
plan copies, journals and renders stay in `testenv/private/` (gitignored).

**Unit tests (Q6, pytest)**

- `test_knowledge.py`: schema; `check(gd)` empty on the fixture data; research order costs and unlocks; sizing at
  15/30/60 SPM matching `rates.solve` within 5%; prompt budgets; `doctrine.md` up to date.
- `test_autopilot.py`: the state machine on scripted snapshots (off, start, adopt, observe, paused, stuck, halted,
  epoch drop); rules R0-R9 on synthetic snapshots; the Director never asks Claude when a rule applies; decision
  validation and fallback; journal format.
- `test_pacing.py`: rolling cap, minimum real and game gaps, back-off doubling and reset, no call while paused, cap
  fallback, using fake_claude.py.
- `test_acquire_order.py`: a Lua-free model of the order table (or `tests/headless_mod_load.py` with a stubbed
  surface); the hand-craft caps; bootstrap exceptions; wait_collect chosen over craft when a producer exists.
- `test_expand.py`: `extend` on the seed 1001/2002/3003 masters for each template: monotone, zones disjoint,
  baseline obstacles respected, the new quality gates pass, mall zone present, trunk straight with reserved width.
- `test_blocks.py`: `mall_block` validates, and its rates and lane assignments are right.
- `test_security.py`: AUTOPILOT_TOOLS rules (section 4.4); the `owner` argument refused in chat mode; the
  autopilot prompt contains no chat text and no free-text fields.

**Live (harness `tests/live_autopilot.py --save <zip> | --fixture seed_1001 --slot 3x --hours 3 --speed 10`)**

The harness:

1. starts a player-like server: `-NoDebugOps` for measured runs, the server console acts as host (no player
   input during the run; if a test player is connected, it stands idle away from the base);
2. starts a test companion with a testenv config and a test `PLANS_DIR`;
3. turns the autopilot on through the GUI click path, then sends no other input;
4. samples every 5 game-min;
5. renders `render_plan` / map frames every 30 game-min;
6. checks the baseline set at the end;
7. writes `report.json` and `report.md` into `testenv/private/autopilot/runs/<run>/`.

| Criterion | Measured |
|---|---|
| Q1 real copy | At least 3 game-hours, or until a plateau (no new goal in 45 game-min). Pass: automated red+green (green >= 10/min measured) and at least 2 new techs researched from automated packs (pack production statistics: no character crafts after S3). No stuck period over 10 game-min. Every baseline unit_number still present unless the removal setting is on. 0 cheats. Claude calls per hour within the cap. Setup cheats listed. Plan copies of the adopted master go in the test PLANS_DIR. |
| Q2 fresh map | The same on seed_1001 (or a new synthetic fixture) from a near-empty start: freeplay items plus a burner start |
| Q3 hand-craft | Share per game-hour. Pass: after the mall is built, share < 25% in the last hour, and no crafting stretch longer than 60 game-s once the mall exists. Report seconds by item. |
| Q4 layout | Look at the renders at each 30-min mark and record the review in the run report: bus or blocks, columns, reserved room, proportional belts. SPAGHETTI and TURNS gates pass for all autopilot nets. Old player spaghetti is excluded but shown. |
| Q5 toggle | A scripted live test (`tests/live_autopilot_toggle.py`): GUI on/off by the host; a non-host GUI and chat attempt is refused; chat on/off/status; save, restart from the save, and the autopilot resumes; off cancels the grace queue and autopilot tasks within 2 game-s; the status line text matches the companion phase; pacing (6 forced decisions in 10 min, at most the cap reach claude). |
| Q6 regression | pytest green; `acceptance.py --planner v3` on seeds 1001/2002/3003 passes as before; `luac -p` clean; CI green on Linux, macOS and Windows after push |

Real `claude -p` calls are used only in the Q1/Q2 runs (and one Q5 smoke). Every other test uses fake_claude.py.
Each report gives the call count.

## 12. Files touched (plan)

- **Mod:**
  - `scripts/autopilot.lua` (new);
  - `control.lua`, `settings.lua`, `locale/en/*.cfg`;
  - `plans.lua` (owner, mall_chests, the on_proposed hook, the card);
  - `gui.lua` (Helper row, status label);
  - `local_commands.lua`;
  - `agent.lua` (acquisition order, producer sources, wait_collect, craft caps and counters, `t.autopilot`,
    keep-out-aware spot picking);
  - `base_state.lua`;
  - `info.json` and `changelog.txt` to 0.6.0.
- **Python:**
  - `autopilot.py` and `autopilot_rules.py` (new);
  - `knowledge/` (new);
  - `planner/expand.py` (new);
  - `planner/blocks.py` (`mall_block`, trunk);
  - `planner/master.py` (mall zone reservation for new masters, any number of stages);
  - `planner/quality.py` (new gates);
  - `policy.py` (AUTOPILOT_TOOLS, `AUTOPILOT_MODE_ENV`);
  - `server.py` (`owner` argument, the autopilot tools, `extend` in `plan_next_stage`);
  - `companion.py` (thread, autopilot MCP config, doctrine in SYSTEM, config keys);
  - `config.example.json`.
- **Docs:**
  - README (an autopilot section);
  - CONTRIBUTING (verified facts);
  - SKILL.md and doctrine.md;
  - this file (as-built notes per round).
- **Tests:** as in section 11.

## 13. Implementation rounds

| Round | Content | Exit criteria |
|---|---|---|
| **1. Loop end to end (P1 core, P2 data, P3 core)** | `knowledge/` with doctrine.json, roadmap.json, `check(gd)`, prompt_text, generated doctrine.md; `autopilot.lua` (state, ops, host-only toggle, keep-out, baseline, approve queue, status line, settings); GUI row and chat commands; the companion `autopilot.py` (Observer, Director R0-R7, Pacer, Decider with AUTOPILOT_TOOLS, Journal); `owner` on plans; agent acquisition order, producer sources, craft caps, counters, wait_collect; `live_autopilot.py` | Q5; Q6; a 1-game-hour smoke run on seed_1001 reaching S3 with no stuck period and no human input; a short dry run on the real copy that fixes power, queues research and adopts the master |
| **2. Mall and growth (P3 mall, P4)** | `mall_block`; `expand.py` with mall, smelt_extend/column, power_grow, steel_column, science_block, trunk; the new quality gates; R8 and R9; the mall chests in acquisition; VERIFY with fixes | Q1 and Q2 (3 game-hours each); Q3; first Q4 review |
| **3. Tune and polish** | Fixes from the Q1/Q2 reports; pacing tuning; layout fixes from the Q4 review; optional review calls; mall v2; oil and blue templates as a stretch; README, CONTRIBUTING, SKILL; version 0.6.0 | Q1-Q6 all pass in a final run; CI green |

### 13.1 Open decisions for the owner

- **D1. Grace period.** 30 game-s by default. Is a shorter grace fine when no player is online?
- **D2. Player inventory.** Should the helper use the player's inventory while the autopilot is on? The default
  is off, so the autopilot does not take the player's things.
- **D3. The first lab.** The automation-science-pack trigger needs a lab crafted by a player. On a fresh map the
  autopilot cannot do that, so it asks the host once ("craft one lab to unlock red science"). The alternative is
  that the run counts this one step as a setup action and the report says so.
- **D4. Variant on fresh maps.** Hub or on-site? The default is the planner's recommendation, with Claude
  deciding only within 10%.
- **D5. Older plans.** Should the autopilot adopt plans the player approved in the past (the default), or always
  start its own master next to the existing base?

Risks:

- Long helper walks on large maps dominate build time; wait_collect makes this worse while there is no mall.
- AM1 mall throughput is fine, but the plate tap may starve the science blocks at low smelting levels. The VERIFY
  step must catch this.
- Throughput of `claude -p` sessions on a subscription (rate limits). The pacing cap and fallback keep the loop
  running without calls.
- Adopted masters whose later stages no longer fit the save (the player built there). Then `extend` replans the
  remaining stages around the baseline obstacles.

## 14. As built: round 1

What exists after round 1 (all of section 13's round 1 except the parts listed under "not yet"):

- **Knowledge** (`factorio_claude/knowledge/`): `doctrine.json` (25 rules in the eight groups, game facts, conventions),
  `roadmap.json` (G0-G9, each with the round that ships its template), `check(gd)` (every game fact recomputed from
  the game data; empty on the fixtures), `research_order`, `sizing` (matches `rates.solve` within 5% at 15/30/60
  SPM), `prompt_text` (1,500 / 400 token budgets), a Lua-free model of the helper's acquisition order
  (`acquire_model`), generated `doctrine.md` (also `skill/factorio/doctrine.md`), `tools/verify_doctrine.py` (base
  2.1.24 Lua prototypes: no mismatch).
- **Mod**: `scripts/autopilot.lua` (state, host-only toggle, epoch, keep-out ring, baseline with names, owned set from
  `script_raised_revive` tags, approve queue with the checks of 5.2, status line, milestones, one-call
  `autopilot_observe`, `autopilot_power_gaps` incl. labs/assemblers no pole reaches, `autopilot_pole_spots`,
  `autopilot_baseline_check`); `plans.lua` (`owner`, `mall_chests`, the proposed hook); `agent.lua` (acquisition order
  mall > plan > producer surplus > chest > player (gated) > wait at a mall machine > crafting with the caps of 7.1 >
  smelt/mine, `acquire` and `wait_collect` tasks, hand-craft counters in 10-minute buckets, autopilot tags inherited
  by every sub-task, keep-out-aware spots); GUI row and status line; chat `autopilot on|off|status`, `let claude play`;
  five map settings and one per-user setting.
- **Companion**: `autopilot_rules.py` (Snapshot, goals, rules R0-R8, stuck ladder) and `autopilot.py` (loop, Pacer,
  Decider with `AUTOPILOT_TOOLS`, Executor running the planner in-process, Journal, `report`); the chat prompt carries
  the doctrine (400 tokens); one Claude process at a time (`CLAUDE_LOCK`); `FACTORIO_CONFIG` for test companions.
- **Decisions made while building** (deviations from the text above, all on the safe side):
  - a master swap is allowed only for entities of a stage that was *applied* (approved); rows of stages that were
    only proposed do not count (the real-save copy's adopted master listed the player's chests in a never-approved
    S0);
  - when a stage keeps being refused (removal_blocked / conflict) after two re-checks, the autopilot leaves that
    master (drops its unbuilt stages; what is built stays) and plans a new base next to the player's (ORG-6); when no
    starter fits, it says so once and keeps the base running and researching;
  - the variant question goes to Claude only when two masters score within 10%; a milestone question only when two
    or more shipped goals are open (else the roadmap order needs no call);
  - with no open goal the autopilot idles (a plateau) instead of climbing the stuck ladder;
  - "companion offline" on the status line after about 3 real minutes (a planner run takes one or two).
- **Not yet (round 2)**: the mall block and `expand.py` (G2-G6, G8), so R8 reports a plateau after S5 and R9 is
  absent; the hand-craft share stays high until a mall exists (round 1 runs: 0.75-0.8 during the starter base);
  VERIFY records shortfalls and their likely cause but does not plan fixes beyond R4/R5/R6; version 0.6.0 and the
  changelog entry.

Round 1 measurements (all on test slots, `testenv/private/autopilot/runs/`):

| run | setup | result |
|---|---|---|
| seed_1001, 90 game min, real Claude | `--player-lab` (D3) + peaceful | S0..S3 built, red science automated at 80 min (21 packs/min), Automation from 10 hand-made packs, then electric-mining-drill, logistics and logistic-science-pack from automated packs; 0 stuck periods, 0 errors, 2 Claude calls (variant 8 s, review 7 s) |
| real-save copy, 60 game min, no Claude calls | peaceful | the split power grid joined and two unpowered labs connected (35 machines without power -> 0), 25 burners refuelled, Automation researched (the queue was empty), the player's master adopted then left (its S0 would take apart the player's belts), a new starter base planned next to it and S0, S0b built; baseline 1,113 entities, 0 missing, 0 swaps |
| seed_1001 again, 75 game min, real Claude, final code | `--player-lab` + peaceful | S3 under construction at 75 min with red packs already at 19/min, Automation done, Electric mining drill researching; 2 Claude calls (variant 7 s, milestone 10 s) and 1 decision paced out (gap: fallback to the default); 0 stuck, 0 errors |
| toggle (Q5) | `sec_players.zip`, debug ops | 21/21 checks: host-only GUI and chat, `let claude play`, off cancels the grace queue and the helper's autopilot tasks within 2 game s, save and reload keep it on, a companion resumes and its phase and goal reach the status line |

## 15. As built: round 2

Round 1's verification failed Q1-Q4 and Q6. What changed:

- **No more stalls while planning.** Planner actions (starter base, next stage, adopt, expansions) run on a worker
  thread (`autopilot.PlannerJob`); the loop keeps observing, researching, refuelling and watching the helper. While a
  job runs on a sped-up game (`game.speed > 1`, a test server) the mod holds the game at speed 1
  (`autopilot_hold`, released when the job ends, when the autopilot is switched off, or after 6 game minutes).
- **Stuck ladder within 10 game minutes.** Rungs at 5, 6.5, 8 and 9.5 game minutes without progress (replan,
  restart the helper, ask Claude, block the goal - only when another goal can go on). Hand-mining and hand-smelting
  are no longer counted as progress (round 1 reset the clock that way for 30 minutes); entities placed, ghosts left,
  research units, techs and plan states are. Stuck detection also runs while a build or a planner job is pending.
- **Research.** R1 keeps three techs queued, ranked: the current goal's techs, the doctrine order, then every other
  useful tech cheapest first. Red techs may wait in the queue before red is automated; a tech needing any other pack
  the base does not make is never queued, and R1c takes such a tech (queued by Claude, say) out again
  (`autopilot_research` op). Weapons, walls, turrets and armour are skipped while the game is peaceful (peaceful mode
  or no spawners); lamps, radar and repair packs are never fillers.
- **Goals.** A goal is never done before its needs; once done it stays done (latched: one milestone line each, no
  G0/G1 flicker); a rate goal needs two observations in a row; power headroom needs a real load (500 kW+); G3 counts
  furnace capacity; G2 is ready once the electric-drill stage is being built.
- **Verify fixes.** A shortfall with cause `no_fuel` queues a refuel round over the stage, `no_power` makes power
  growth the next expansion, `full_output` is recorded as surplus (headroom, not a fault), anything else is checked
  again 10 game minutes later.
- **Swaps.** An autopilot stage's removals are no longer marked for deconstruction when it is approved (a marked
  machine stops working: round 2 runs lost all iron and copper for 20 game minutes when S4 marked every burner
  drill). The helper first gathers the replacements, builds the rest of the stage (its steam units and poles), then
  takes the old machines apart one by one and puts each replacement in right away; the ghosts over the old machines
  wait (deferred) until their ground is free. Player-approved plans keep the marks (the player sees them).
- **Expansions (`planner/expand.py`).** `extend(mp, world, gd, template)` appends a stage to the same master:
  - `mall` (planned once the green-science stage starts; the stage after it, or index k + 0.7 when later starter
    stages are still waiting, labelled e.g. S4m; research goes on with green science while it is built): a straight
    plate bus (iron row, copper row, 96 tiles, corridor reserved for 8 lanes) fed at its head by two smelting cells
    (electric drills on the ore, a smelting column whose two plate belts merge onto one belt at its top, coal from an
    inserter tap on a starter-base coal belt), and the mall row beside it, fed by inserter taps on the bus (a
    long-handed inserter reaches the copper belt over the iron one). Mall row (`blocks.mall_block`): a cable, circuit
    and gear head, then inserters, electric drills, assemblers, splitters, belts, undergrounds, pipes and steam
    engines, each into a chest limited to 1-2 stacks, and a plate depot at the end of the iron belt. Its chests and
    machines are tagged on the ghosts and registered by the mod when built; they are the helper's first source
    wherever they stand, and the helper only waits at a mall machine that is working.
  - `power_grow`: the power block's reserved units, re-derived from the master's built boilers.
  - `steel`: a 2-pair steel column beside the bus (iron from a bus tap, coal from a starter coal belt), all steel into
    a depot chest.
  - `science`: a second red + green science block with 10 labs (the starter base's engine-checked block, +30 red
    and +30 green per minute) beside the bus, its four plate inputs tapped from the bus, plus an extra iron cell on
    any iron patch whose plates side-load onto the bus iron belt.
  Taps only take from a belt that carries nothing but their item, and use fast inserters once researched. Every stage is validated as the whole base after
  it and checked monotone (the bus corridor is a zone later blocks may tap from); a failure leaves the master
  unchanged. R8 asks for the next template the roadmap allows (mall when S5 starts, power growth when use passes 66%
  of capacity or VERIFY saw machines without power, steel after steel processing, the second science block for G8
  once green runs at 30/min); three failures block the goal for an hour with one milestone line. Oil and blue
  science stay round 3.
- **Starter-base rates.** A rate limited by the plate supply rather than by machines is promised at 90%
  (`master.SUPPLY_EFF`): acceptance seed 3003 hub S3 measured 13.2 red/min where the supply allows exactly 15.
- **Version 0.6.0.**

## 16. As built: round 3

Round 3's verification passed Q1, Q2, Q5 and Q6 but failed Q3 (hand-craft share 0.79 / 0.92 in the last hour; the
mall supplied 9 and 0 items) and Q4 (long feeder belts, nested loops on the real copy), and both judges asked for the
roadmap to go past red and green. What changed:

- **Why the mall did not supply.** Three causes, all fixed:
  - its iron fed the gear machine at the head of belt A first, and a depot chest at the end of belt A drained
    everything that passed, so the gear machine never backed up: iron became a chest of gears while the belt and
    inserter machines downstream starved (live: 226 gears, 4 iron plates, 0-14 belts). `mall_block` has no depot any
    more (`depot=False`): belt A backs up, each machine takes what it needs, the taps only move what the row uses.
    The belt chest holds 4 stacks;
  - steel and the second science block tapped the same bus as the mall and took the plates first. The mall now has a
    dedicated supply (below) and, when it hangs off a bus, taps its upstream third; steel and science tap downstream
    of the mall's taps (`mall_tap_rows`, `bus_slice`);
  - the helper took "not working" for "not making": a machine fed by one inserter waits for ingredients between crafts
    most of the time (status `item_ingredient_shortage`) and still makes ~50 belts a minute. A mall machine now counts
    as making its item when it is crafting or finished a craft in the last 3 game minutes (`products_finished`,
    `agent.mall_machine_active`), and the helper waits there (`wait_collect`) instead of crafting.
- **The hand-craft budget had a hole.** The helper halved a refused batch until it fell under the 10 s "short craft"
  bar, which was always allowed: 40 belts at a time, 145-375 crafting seconds per 10 game minutes next to a running
  mall. Once a mall exists every craft now counts against the 120 s per 10 min budget, an item a mall machine makes
  gets at most a 10 s top-up, and bootstrap items (assemblers) stop being exempt once the mall makes them. Coal
  fetched as fuel (refuel rounds, start fuel) is counted apart (`stats.fuel`), not as a built item.
- **The mall comes earlier and smaller.** G2 is ready when the electric-drill stage (S4) starts and its research is
  done (was: when S5 starts); the stage is S4m, before green science, so S5 and everything after it are built from the
  mall. It no longer brings a bus and smelting cells (650-915 entities, all hand-made before any of it worked, which is
  why the real copy's mall came online at 660 of 665 game minutes): it taps the starter base's own iron (3 inserters)
  and copper lines near the hub (feeds <= 80 tiles; ~225 of the starter base's 450 iron/min are surplus), about 220-260
  entities. A bus is used when the master already has one, and laid with cells only as a last resort.
- **The bus comes with steel.** `_new_bus` / `_on_bus`: the first block that needs plates beyond the starter base
  (steel, or the second science block) lays the bus (96 tiles, was 128) with a smelting cell per metal at the ore when
  the cell's feed is <= 64 belt tiles (`CELL_FEED_MAX`; iron cells 12 drills / 12 furnaces, 225/min), else a tap on the
  starter line. The science block's extra iron cell has the same limit.
- **Feeds are proportional.** Every belt `expand` routes is checked (`route_shape`, `proportional`): at most 1.4x the
  taxicab distance + 12 tiles and 6 turns; a longer one counts as no route and the next tap or spot is tried. When
  nothing fits that way, one relaxed pass (2x + 16, 10 turns) runs before the template fails (doctrine ORG-8).
- **Past red and green: oil (G9).** `expand.oil` puts up to 4 pumpjacks on the crude-oil wells nearest the base, each
  output facing the base (gate: Oil gathering, Fluid handling); pumping crude finishes the Oil processing trigger tech
  (engine-checked, X5). `expand.oil_plant` (gate: Oil processing, Plastics) places `blocks.oil_plant_block` - one
  refinery on basic oil processing and one chemical plant on plastic (1 refinery keeps ~0.45 plastic plants busy),
  plastic into a depot chest - near a coal belt (coal by a tap), and pipes crude from every pumpjack into the
  refinery's crude input. Engine check X4: the block makes plastic from crude and coal. The validator now honours a
  recipe's `fluidbox_index` (basic oil processing takes crude in the refinery's 2nd input and gives petroleum from its
  3rd output; it used to assume the 1st of each). G10 (blue science) is on the roadmap as round 4, unshipped: the
  status line names it as the next step.
- **Done, not stuck.** A goal whose templates are all built, whose master stages are built and whose techs are not
  being researched is only watched (`goal_actionable`). With nothing actionable and no research running, the
  autopilot says once why ("Done for this version: next would be G10 ...", or "Paused at G9: no crude oil within reach
  ...") and goes to phase `done`: no stuck ladder, no review calls (round 3: G8 stayed the goal on a finished base and
  the ladder ran replan, restart, ask Claude and block goal).
- **Mall row.** No splitter machine (never needed in 3-hour runs; first on the circuit belt, it took 250 circuits and
  200 belts to fill its chest while inserters ran short). Order: belts, undergrounds, long-handed / plain / fast
  inserters (the inserter group ahead of every other circuit user), drills, assemblers, pipes, steam engines, iron
  chests. The belt machine has two inputs and two outputs into two 4-stack chests (~100 belts/min). Engine check X1:
  every chest full in 30 game minutes (800 belts, 100 inserters, 50 drills, 50 assemblers; the inserter, drill and assembler machines take iron and gears through two inserters). Depot chests (bus ends,
  steel, plastic) are limited (2-4 stacks): an unlimited depot keeps its belt flowing and its starter taps running
  flat out (smoke run: red science fell from 30 to 3/min while the depots filled).
- **Medium poles** in expansions once the master makes steel and they are researched (7x7 supply, no wood): about
  half the poles, the one bulk item the mall cannot make.
- **Mall build is bootstrap.** The mall stage's own build may hand-craft freely (`mall_build` on the plan); a half
  built mall used to put its own build under the budget.
- **Doctrine.** New and changed rules: AUTO-2 (every craft counts), MALL-3 (mall with the electric drills, belt A
  backs up), ORG-7 (mall gets the first pick), ORG-8 (short, straight feeds; no lone feeder from a far patch), RES-4
  (the oil path); oil facts (refinery and plant speeds, the three oil recipes, pumpjack 10 crude/s at 100% yield)
  checked against the game data by `check()`; the autopilot prompt budget is 2,000 tokens with the safety rules first.
