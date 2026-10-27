# factorio-claude v3 — "Claude the base architect"

> Historical design brief (goals and milestones for v0.3). See README.md for what is implemented.

## What the player wants (2026-10-26)
- Spawn into a **new game**, ask Claude "set me up with a starter base", and get a complete, verified, working
  starter base: mines → smelting hub → bus, power, coal/fuel logistics. Repeatable at every stage of growth.
- Early: simple tasks (already mostly free buttons). Later: **completely re-optimize or analyze the base in any facet**,
  including **other planets**, with **multiple Claude helpers on different planets** at the same time.
- "Optimized" = **ask each time**: the planner produces 2-3 variants with clear trade-offs; the player picks in-game.
- Cost: **show an estimate and wait for confirmation** before expensive Claude jobs.
- Today's 749 unverified Claude ghosts: **remove them all** (keep the player's own ghosts).

## Lesson from the first real session
Claude hand-placed 749 ghosts across 4 replies, ~$1.20 API-equivalent, nothing verified, missed a lake next to the
player, furnaces without fuel feed, belts crossing the bus. An LLM placing entities one by one is the wrong tool.
**Principle: deterministic code does perception, math, layout, routing and verification (free, exact); Claude does
intent, choosing between variants, explaining trade-offs, and orchestration (few cheap calls).**

## Architecture

```
Factorio mod (Lua)                         Python engine (companion process)                 Claude
- map/state export -> script-output files  - world model + spatial index                      - turns request into goal +
- game-data export (prototypes, research)  - rate solver (LP) incl. quality/modules/beacons    constraints, runs planner,
- plan preview overlay + Approve/Reject    - layout library + placement solver                compares variants, explains
- tagged ghost placement / clear by tag    - belt/pipe router (A*, undergrounds), pole placer
- helper characters (one per surface)      - validator (static) + simulator harness (tests)
```

Big data moves through files (`helpers.write_file` → `%APPDATA%\Factorio\script-output\claude\…`, read by the companion),
not chat-sized UDP messages, so 1000×1000-tile areas are fine. UDP stays the control channel.

## Milestones

### M0 — quick fixes, before the relaunch (small)
- "Clear Claude's ghosts" button + op: removes ghosts placed by script (`last_user == nil`) or tagged by a Claude plan;
  never touches ghosts a player placed. Every future Claude ghost carries `tags.claude_plan = <plan id>`.
- No markdown in game chat: the mod strips `**`/headings/bullet syntax; the companion prompt forbids it.
- Cost gate: the companion classifies each request (chat vs planning job); planning jobs show "≈ $X, run it?" with
  Confirm / Cancel in the Ask Claude tab (free), and every reply shows its actual cost line.
- In-game Claude must not hand-place large builds any more: > 50 ghosts per reply requires the planner (M1) or explicit
  confirmation.

### M1 — Starter-base autopilot (the big first test)
1. **Perception**: mod exports charted chunks (tiles/water, resources with amounts, cliffs, trees/rocks, all force
   entities, belts/pipes), plus force state (research, unlocked recipes, bonuses, inventories). Python world model with
   queries: patches (outline, richness, center), water bodies, free rectangles, distances, existing networks.
2. **Game data**: export all prototypes the planner needs (recipes, items, fluids, machines + speeds/energy, modules,
   beacons, qualities, belts/inserters throughput, poles, technologies) on demand; cached by game/mod version.
3. **Rate solver**: LP over recipes for any target (items/min, SPM), with machine choice, modules, beacons, quality tiers,
   productivity research, mining productivity, fuel and power; reports machine counts, belts, ore/min, power, pollution.
4. **Layout library** (parametric, verified): mining outposts sized to patch, smelting arrays (burner + electric),
   steam power (offshore 1 : boilers 20 : engines 40 ratios), main bus with lane plan, science/mall blocks later.
5. **Placement solver + router**: choose hub location (minimize weighted belt length to patches + water), place
   modules on free ground (avoid cliffs/water/existing), A* belt and pipe routing with undergrounds and bus crossings,
   automatic pole coverage, tree/rock clearing via deconstruction.
6. **Validator** (blocks placement on failure): belt continuity + direction, inserter pickup/drop targets, fluid
   connectivity, power coverage, throughput vs belt/inserter limits, fuel reaches every burner, everything unlocked and
   buildable at current progress, no overlaps.
7. **Staged build orders**: Stage 1 burner bootstrap the helper can do from nothing (mine, craft, place);
   Stage 2 electric + automation; each stage is its own approvable plan.
8. **In-game preview & approval**: coloured overlay of the plan (rendering), summary (rates, footprint, materials,
   power, est. build time), variants side by side (2-3 per "ask each time"); buttons Approve / Adjust / Reject.
   Approve → tagged ghosts; helper(s) + bots build with the new fetch/craft logic.
9. **Claude tools**: `plan_starter_base`, `plan_variants`, `plan_preview`, `plan_approve`… Claude never places raw
   entities for big builds.
10. **Acceptance test (automated, headless, 3 map seeds)**: autopilot from a fresh map → validator passes → instant
    build on the test server → run at `game.speed` 10+ for N in-game minutes → measured production within 10% of the
    plan, no stalled machines. Cost per run recorded (target: well under today's $0.70 for the whole base).

### M2 — Analyze & re-optimize any base
Bottleneck analysis from real flow statistics vs. design, upgrade proposals (belts, machines, modules, beacons,
quality), rebuild/upgrade plans as tagged ghost diffs + upgrade planners, "why is X low?" answered from data.

### M3 — Planets & multiple helpers
`storage.agents` keyed by id/surface; GUI helper picker; one helper per planet/platform working in parallel; planner
aware of planet resources, recipes, surface conditions (Vulcanus, Fulgora, Gleba, Aquilo, space platforms).

### M4 — Late game
Quality loops/upcycling, module + beacon optimization, trains, rocket/space logistics, megabase-scale planning.
