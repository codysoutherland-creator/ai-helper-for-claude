# v3 contracts (M0 + M1) — exact interfaces shared by all agents

> Historical design brief (written for the coding agents that built v0.3). Still the reference for the export_map /
> export_data file formats and the planner module APIs; some paths are out of date.

Read SPEC-v3.md (goals), SPEC-v2.md (verified engine facts, test harness, UDP protocol) first. This file wins on
interfaces. If you must change an interface, keep it backward compatible and note it in your report.

## Hard rules
- The player's REAL game may be running (factorio.exe whose command line does NOT contain "testenv"), with the real
  companion (pythonw … factorio_claude.companion on UDP 34500 / lock 34501). Never kill/restart them, never send them
  ops, never write into %APPDATA%\Factorio (real script-output, saves, mod-list). Test only on your own headless slot.
  The real mod folder is a junction to mod/claude-bridge, so edits are fine — the running game already loaded the old code.
- Never use cheats/console in the real game. In test instances anything goes (`rcon.lua`, instant_build, game.speed).
- Every event handler path in the mod must be error-safe (an uncaught error crashes the player's game).
- Coordinates: tile grid, x → east, y → south. Entity positions are centers (1×1 at .5, 2×2 on integers, 3×3 at .5).
  Directions: 16-way defines (north 0, east 4, south 8, west 12). Inserter direction = pickup side.
- Respect research: a plan for the current game may only use entities/recipes whose recipe is enabled for the force
  (`options.assume_research` may relax this for "plan ahead" views, then mark such entities `requires_research`).
  On a fresh map underground belts and splitters are NOT unlocked (logistics tech) — routers must work without them.
- Python: `.venv\Scripts\python.exe` in the repo (numpy + scipy installed; add new deps to
  install.py `deps()` too). Pure functions + dataclasses; no global state; type hints welcome; keep modules focused.

## Files the mod writes (script-output)
Root: `<write-data>/script-output/claude/`. Real game: `%APPDATA%\Factorio\script-output\claude`; test slot N:
`testenv\inst\<name>\write\script-output\claude`. Python resolves it via `planner.paths.script_output()`:
env `FACTORIO_SCRIPT_OUTPUT` → config.json `script_output` → `%APPDATA%\Factorio\script-output`.

### export_map (mod op, owner: perception agent) — `scripts/export_map.lua`
`export_map {surface?, area?:{x0,y0,x1,y1}, include?:["tiles","resources","obstacles","entities"]}`
→ writes `claude/map/<surface>/<run_id>/c_<cx>_<cy>.json` per 32×32 chunk (charted chunks only, within area) and
`claude/map/<surface>/<run_id>/index.json` = {surface, tick, run_id, chunks:[[cx,cy],...], bbox}. Returns
{run_id, dir (relative to script-output), chunks, tick}. Large exports may be spread over several ticks (job state in
storage; op `export_status {run_id}` → {done, written, total}); never freeze the game > ~50 ms per tick.
Chunk JSON: {cx, cy,
  water: [row strings of 32 chars '1'/'0', y-major] or null if none,
  void: same for out-of-map/void tiles or null,
  resources: [[name, x, y, amount], ...],          (tile centers)
  obstacles: [[kind, name, x, y], ...]  kind ∈ tree|rock|cliff|other_neutral  (cliffs also give "orientation" as 5th),
  entities: [[name, x, y, dir, extra], ...]          force entities incl. ghosts as "ghost:<inner>", extra = {recipe?,tags?}
}

### export_data (mod op, owner: game-data agent) — `scripts/export_data.lua`
`export_data {}` → writes `claude/data.json`, returns {path, version_key}. Contains everything the planner needs:
entities (type, collision_box, tile_width/height, crafting_speed, crafting_categories, energy_usage_kw, energy source
type (burner/electric/void) + burner fuel categories, mining_speed, mining_drill_radius, resource_categories,
module_slots, allowed effects, fluidbox pipe connections per direction (relative positions + flow), belt_speed
(items/s per lane & total), inserter rotation_speed / extension / pickup+drop vectors, pole supply_area_distance +
max_wire_distance, offshore pump rate, boiler/engine stats, items_to_place_this, next_upgrade),
recipes (ingredients, products incl. probability/amount ranges, energy, categories, enabled for force, hidden,
hidden_from_player_crafting), items (stack_size, fuel_value, fuel_category, place_result, rocket capacity if any),
fluids, qualities (name, level, next, multipliers that matter), modules (effects, tier), beacons,
technologies (researched/enabled/queue), force bonuses (mining productivity, inserter stack bonus, laboratory speed,
worker robots), surfaces (name, planet, surface properties). Python: `planner.gamedata.GameData.load(path)`.

## Python planner package `factorio_claude/planner/`
| module | owner | public API |
|---|---|---|
| `paths.py` | perception | `script_output() -> Path` |
| `world.py` | perception | `World.load(map_dir) -> World`; `World.from_link(surface=None, area=None, link=None) -> World` (calls export_map, waits, loads). Methods: `bbox`, `is_water(x,y)`, `is_void(x,y)`, `resource_at(x,y) -> (name, amount)|None`, `patches(min_tiles=20) -> [Patch]`, `water_bodies(min_tiles=30) -> [WaterBody]`, `nearest_water(x,y) -> WaterBody`, `shoreline(body) -> [(x,y,dir_to_water)]`, `obstacles_in(rect)`, `entities_in(rect)`, `grid(rect, margin=0) -> Grid`. `Patch{name, tiles:set[(x,y)], amount, bbox, center}`; `WaterBody{tiles, bbox, center}` |
| `grid.py` | perception | `Grid(x0,y0,w,h)`: per-tile state BLOCKED (water/void/cliff/existing entity), CLEARABLE (tree/rock — may be deconstructed), RESOURCE layer, FREE; `copy()`, `occupy(tiles, tag)`, `free(x,y)`, `can_place(tiles, allow_clear=True)`, `free_rect(w,h,near,within)`, `to_ascii(rect)`. Used by router, placer and validator. |
| `gamedata.py` | game-data | `GameData.load(path)`, `GameData.from_link(link=None)`; `entity(name)`, `recipe(name)`, `item(name)`, `footprint(name, x, y, direction) -> set[(tx,ty)]` (handles rotation), `size(name, direction) -> (w,h)`, `enabled(recipe_name)`, `can_build(entity_name)` (its place-item recipe enabled or item obtainable), `fluid_connections(name, x, y, direction) -> [(tx,ty,dir)]`, `belt_throughput(name) -> items/s`, `inserter_throughput(name, ...)`, `pole(name) -> (supply_radius, wire_reach)`, `quality(name)` |
| `rates.py` | game-data | `solve(targets: dict[item->per_min], gd, opts) -> RateResult` with opts {machines:{category->entity}, modules, beacons, quality, allowed_recipes, raw_items (ores/water/crude: unbounded inputs), fuel:"coal"}. RateResult{recipes:{recipe:{machine, count (float), count_ceil, per_min}}, raw:{item:per_min}, power_kw, fuel_per_min, belts:{item:belts_needed by tier}, notes}. Linear program (scipy.optimize.linprog) so byproducts/multiple recipes work; quality handled as separate item variants "name@quality". |
| `blocks.py` | layout | Parametric verified building blocks returning `Block{name, entities:[PE], size:(w,h), ports:[Port], rates:{}, notes}` in LOCAL coords (top-left 0,0). `PE{name,x,y,direction=0,recipe=None,extra=None}`. `Port{kind: belt_in|belt_out|pipe_in|pipe_out|pole, item, x, y, direction, lane=None}` — x,y = the tile just OUTSIDE the block where a belt/pipe must connect, direction = flow direction. Blocks: `mining_outpost(world_patch_tiles, drill, belt, pole, output_side)` (covers the patch region given, drills only on ore, output belt port), `burner_mining_column(...)`, `smelter_array(n, furnace, fuel_mode: "mixed_lane"|"separate", output_side, inserters)`, `steam_power(engines, ... )` (offshore pump : boiler : engine ratios, port for coal belt + water-side placement handled by placer), `bus_start(lanes:[item])`, `coal_splitter`… everything must pass validate on its own. |
| `router.py` | routing | `route_belt(grid, gd, start: Port, goal: Port, belt="transport-belt", allow_underground=None) -> [PE] or raise NoRoute` (A* on tiles, turn penalty, avoids BLOCKED, may clear CLEARABLE at cost, uses undergrounds only if `gd.enabled("underground-belt")` or allow_underground True, side-loading onto an existing belt allowed when goal is a belt tile), `route_pipe(...)` (pipe-to-ground if enabled), `place_poles(grid, gd, entities, pole) -> [PE]` (cover every electric consumer, connect into one network, connect to given power source poles), `cross(...)` helpers. Marks occupied tiles in the grid it's given. |
| `placer.py` + `plan.py` | orchestrator | `plan_starter_base(world, gd, goal, variant) -> Plan`; `plan_variants(world, gd, goal) -> [Plan]` (2-3 genuinely different trade-offs: e.g. compact hub / expandable bus / resource-efficient electric), `Plan{id, title, variant, stage, summary, entities:[PE], deconstruct:[(x,y,name)], warnings, validation}`, `Plan.save(dir)/load`, stages: `stage="bootstrap"` (burner only, buildable from nothing with hand tools) and `stage="electric"`. Hub site selection: minimize weighted belt length patches→hub + water distance, on free ground. |
| `validate.py` | validator | `validate(entities:[PE], gd, world_or_grid, expect: dict|None) -> Report{ok, errors:[Issue], warnings:[Issue], stats}` with `Issue{kind, x, y, msg}`. Checks: overlaps + placeable terrain (resources required under drills, water under offshore pump), belt graph continuity & direction (every belt_out reaches a consumer, no dead ends unless declared sink, no head-on belts), inserter pickup/drop targets real (entity or belt), fluid connectivity (pipe graph incl. pipe-to-ground pairs), power coverage + single network connected to a generator, fuel reaches every burner entity (via inserter from a belt/lane carrying fuel, or declared manual), throughput (belt/inserter capacity vs planned rates when `expect` given), research (all buildable). |

## Mod plan ops (owner: plan-UI agent) — `scripts/plans.lua` + GUI
- `plan_preview {id, title, entities:[[name,x,y,dir,recipe?],...], deconstruct?, summary?, color?, surface?}` → draws
  an overlay (rendering: footprint rectangles colored by kind + belt arrows, chart mode too) and stores the plan
  (storage.plans[id], entities may be large — accept chunked delivery via the UDP protocol as usual).
  `plan_clear_preview {id?}`. `plan_apply {id}` → places ghosts with `tags = {claude_plan = id}` (skip blocked,
  report counts) + orders deconstruction for listed obstacles. `plan_list`, `plan_drop {id}`.
- `clear_claude_ghosts {plan?}` → destroys ghosts with tags.claude_plan (that plan or any) AND ghosts with
  last_user == nil (script-placed, i.e. v0.1/v0.2 Claude ghosts); never ghosts with a player last_user. Returns counts.
  (M0 agent implements this op + a GUI button; plan-UI agent reuses it.)
- GUI: a "Plans" section (new tab or inside Ask Claude): list of proposed variants with summary lines, buttons
  Preview / Approve / Reject per plan; Approve calls plan_apply and tells the companion (inbox message source="plan",
  text="approved <id>").

## Companion / Claude (owner: orchestrator agent, after M0 agent's cost gate exists)
- MCP tools: `plan_starter_base(goal_text, variants=3)` → runs World.from_link + GameData.from_link + plan_variants,
  sends previews, returns compact summaries (no entity dumps); `plan_preview`, `plan_apply(id)`, `plan_validate(id)`,
  `world_summary()` (patches, water, free areas — compact). Raw `factorio_place_ghosts` refuses > 50 ghosts unless
  `allow_large=True` (M0).
- Companion system prompt: for any build request bigger than a few entities, use the planner tools, never hand-place;
  present variants with trade-offs and let the player pick in the GUI; no markdown.

## Cost gate (M0 agent)
Companion classifies each message: "planning" (build/design/optimize/layout/base/setup… or > 200 chars) vs "chat".
Planning → `claude_confirm {request_id, estimate_text}` op → Ask tab shows "This looks like a planning job (≈ $0.05–0.30).
Run it?" [Run] [Cancel] (free); the click goes back through the inbox (source="confirm", text="run <id>"/"cancel <id>").
Every reply ends with a cost line in the GUI history (not in chat): "cost ≈ $0.02 · 4 s".
