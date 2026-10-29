# Starter-base planner v2: state-aware, distance-aware, rebuild-aware

> Design brief for the next planner pass (after v0.4.0). It replaces the "one hub template, three sizes" planner
> described in SPEC-v3.md. Requirements R1-R7 and acceptance criteria A1-A6 are binding; everything else is a
> proposal that the implementation may refine, as long as the numbers in the tables below stay the source of truth
> (they become constants in `factorio_claude/planner/cost.py` and `quality.py`, with tests).

## 1. Why

On a **sprawled map with pre-existing builds** (four ore patches roughly 100-250 tiles apart in different
directions, a lake off-centre, and the middle of the map covered by earlier hand-built and older-Claude builds), the
v1 planner produced an approved "expandable, electric" plan with about 600 ghosts: about 550 belts, 26 electric
drills, 9 steam engines and 5 boilers. The smelting hub sat 190-255 tiles from the iron patch, single belt runs were up
to 223 tiles, and the first iron plate needed about 1,250 raw iron (the whole plan about 2,300). The player had about
135 iron, 80 stone and almost no coal, no power, and electric mining drills were not even researched. At the measured
helper rate that is more than 3 hours of gathering. Every plan validated, and the three acceptance seeds passed,
because validation proves connectivity, not quality, and those seeds have ore next to spawn and nothing built.

Root causes found by reproducing the plan offline (line numbers refer to v0.4.0):

| # | Cause | Where |
|---|---|---|
| 1 | The only layout is a central hub; every ore and every coal feed gets its own belt to it. There is no on-site smelting, no belt-length cap or report of the longest run. | `placer.py` header, `build_hub`, `_route`, `validate.py`, `describe` |
| 2 | Hub site cost counts flows, not volume (iron 2, copper 2, stone 2, coal 3-4), so the weighted median lands at x = copper, y = coal. Distances ignore water and existing builds, so the cheapest sites then fail as "walled off" and the search falls back far away. | `hub_sites` |
| 3 | The final pick ignores site cost: fewest belts + 0.1 x entities among up to 3 successful sites. | `_run` (`want_sites`, final `min`) |
| 4 | Steam power targets the water nearest the player, not the hub; about 80 poles crossed the map. | `_run` (water tile) |
| 5 | Existing builds are hard walls and are never reused, repaired or removed. The reachability pre-check in `_route` uses `state != BLOCKED` labels and would reject underground crossings even when undergrounds are allowed (latent bug). | `world.py` grid, `_route` |
| 6 | No player state: fixed furnace counts per variant, the word "electric" in the goal overrides the research-based stage, `assume_research` only warns, and `build_minutes` assumes 30 ore/min of hand mining and ignores inventories. | `VARIANTS`, `parse_goal`, `default_stage`, `plan.build_minutes` |
| 7 | Each resource's patch is chosen by distance to the player, never jointly or relative to the smelting site. | `pick_patches` |

What an expert does instead, from the same state: put burner drills straight into stone furnaces **on** the iron patch
(no belts), fuel them from a coal drill into a chest, collect plates, and grow from there; move power with poles
(about 0.07 Cu per tile) rather than ore with belts (1.5 Fe per tile); use whatever is already built as either a cheap
first step (refuel the idle furnaces) or as salvage (picking an entity up refunds it in full).

## 2. Goals and non-goals

"Optimized starter base" means: **from the player's actual state, the fastest sustainable path to plates, then to
automation science, sized to materials obtainable soon, with sensible geometry and a clear upgrade path.**

Non-goals for this pass: trains, modules, other planets, multiple helpers, mall design, oil. The existing hub engine
(`placer.Planner`) stays and is reused for the Long-term variant and for central topologies; v2 wraps it rather than
rewriting it.

Principles kept from SPEC-v3: deterministic code does perception, math, layout, routing and verification; Claude
explains and lets the player choose; every ghost a plan places is tagged; the player approves in the Plans tab.

## 3. Game constants (2.x, normal quality)

Recipe costs come from `gamedata` at runtime; the values below are for the model, for tests and for this document.
"Fe" means raw iron plates, "Cu" raw copper plates.

| Thing | Rate / power | Raw cost |
|---|---|---|
| Hand mining (character) | 0.5 ore/s theoretical; **planner uses the measured helper rate**, default 8.5 raw units/min end to end (mine, walk, smelt) | - |
| Burner mining drill | 0.25 ore/s, 150 kW = 0.0375 coal/s | 9 Fe + 5 stone |
| Stone furnace | 0.3125 plates/s, 90 kW = 0.0225 coal/s | 5 stone |
| Burner pair (drill into furnace) | 15 plates/min, 3.6 coal/min | 9 Fe + 10 stone |
| Coal burner drill | 15 coal/min gross, 12.75 net of its own fuel = fuel for ~3.5 pairs | 9 Fe + 5 stone |
| Electric mining drill | 0.5 ore/s, 90 kW | 23 Fe + 4.5 Cu |
| Transport belt | 15 items/s (7.5 per lane) | 1.5 Fe per tile |
| Underground belt pair | spans 4 tiles | ~17.5 Fe |
| Pipe / pipe-to-ground pair | - / spans 10 | 1 Fe / 15 Fe |
| Small electric pole | ~7 tiles of line | 0.5 Cu + 0.5 wood |
| Offshore pump | 1200 water/s, no power | 7 Fe (+ circuits per recipe) |
| Boiler | 1.8 MW, 0.45 coal/s, 6 water/s -> 60 steam/s | 4 Fe + 5 stone |
| Steam engine | 0.9 MW, 30 steam/s | 31 Fe |
| Burner inserter / inserter | - | 3 Fe / 4 Fe + 1.5 Cu |
| Assembling machine 1 | speed 0.5 | 22 Fe + 4.5 Cu |
| Lab | - | 36 Fe + 15 Cu |
| Automation science pack | 5 s | 2 Fe + 1 Cu |
| Coal | 4 MJ | stone furnace: 0.072 coal per plate |

2.x power ratio is 1 pump : 200 boilers : 400 engines (the 1.1 ratio 1:20:40 is obsolete); boiler : engine stays 1:2.
`steam_size` and any doc that assumes 1:20:40 must be checked.

## 4. R1: state-aware planning

### 4.1 Data inputs

New mod op **`base_state {x, y, radius}`** (radius capped at 300; read-only; no belt lane data). Returns:

```
{
  tick, surface,
  player:  {pos, main: {item: n}},            -- first connected player on the force; no name is returned
  helper:  {pos, main: {item: n}, stats: {mined: {item: n}, active_ticks, mining_ticks, walking_ticks}} | null,
  chests:  [{unit, name, x, y, contents: {item: n}}],                     -- containers of the force in range
  machines:[{unit, name, type, x, y, dir, status, fuel: n, fuel_item, input: {item: n}, output: {item: n},
             recipe?, network?: id, burner: bool}],   -- furnaces, drills, boilers, engines, pumps, labs,
                                                      -- assemblers, inserters (burner ones only fuel/status)
  belts:   {count, segments: [[x1, y1, x2, y2, n_tiles, items_on_it]]},  -- straight runs, merged in Lua
  poles:   {count, networks: {id: {poles, producers_kw, consumers_kw}}},
  production: {window: "10m", per_min: {item: n}},   -- force flow statistics, output side
  research: {done: [names], current, queue}          -- done list is also in export_data (GameData)
}
```

- Map export (`export_map.lua`) gains `unit` (unit_number) on force entities so the Python world, `base_state` and
  removal rows can be joined. It does **not** export `last_user` names. "Existing build" means any non-ghost entity of
  the player's force, whoever placed it.
- The helper keeps the counters in `stats` (`agent.lua`: mined items, ticks spent mining, walking and building) so the
  hand-gather rate can be measured per session.
- Python: new `planner/state.py` with a `BaseState` dataclass, `BaseState.from_link(call, cx, cy, r)` and
  `BaseState.load(path)` / `.save(path)`. Fixtures gain an optional `state.json` (synthetic for public fixtures; the
  real-save-derived one lives only under `testenv/private/`). A missing state means "fresh freeplay start": 8 iron
  plates, 1 burner drill, 1 stone furnace, 1 wood, no production, research as in `data.json`.

### 4.2 Stock, salvage and the material bill

All accounting is done in **items first, raw second**, so a belt already in an inventory covers a belt in the plan.

- `stock` = player main inventory + helper inventory + chests within 60 tiles of the plan's sites or of the player +
  plates sitting in furnace output slots in range. (The helper already takes from the player and from chests:
  `take_player`, the material planner in `agent.lua`.)
- `salvage(removes)` = for each entity the plan removes: its own item (pickup refunds in full) plus fuel, input,
  output and chest contents plus items on removed belts.
- `bill(plan)` = item counts of the plan's new entities + fuel to start them (5 coal per burner, 20 per boiler).
- `net = bill - stock - salvage`, item by item. The remainder is expanded to raw with `raw_materials` and reported as
  **ore equivalents**: `iron-ore, copper-ore, stone, coal, wood`, where a plate costs 1 ore + 0.072 coal + smelting
  time, a brick 2 stone.
- `bill_fe_equiv = Fe + 0.8 Cu + 0.3 stone + 0.2 coal` (scoring only).

`planner/cost.py`: `bill(entities, gd)`, `available(state, removes)`, `shortfall(bill, available)`.

### 4.3 ETA model (discrete simulation)

The ETA is not one division. `cost.simulate(steps, state, gd)` runs in 30 s game-time steps up to 180 min.

State: `stock` (items + raw), `rates` (items/min from working machines: existing working ones from `base_state`,
the plan's ones once placed), `coal_balance`, `helper` (busy until t, position).

A plan is an ordered list of **build steps** (section 9.2): each with entities, start fuel, the site position,
production it adds when done and coal it burns.

```
t = 0
for step in steps:                                   # in payback order (9.2)
    while not covered(step.net_items(stock)):
        stock += rates * dt                          # running machines, existing + placed
        if helper idle: stock[most_short_raw] += H * dt   # H = measured helper rate, default 8.5/min
        consume coal for running burners; if coal < 0: scale burner rates by coal supply / demand
        t += dt
    helper busy for: craft_s(step) + PLACE_S * n_entities + walk(helper.pos -> step.site) / WALK + dismantle_s(step)
    stock -= step.items; rates += step.rates after 0.5 min warm-up
    record first time iron-plate (then copper) rate > 0 from the plan  -> eta_first_plates
    record first time plan rates >= stage target                       -> eta_target
eta_complete = t when the last step is done
```

Constants: `WALK = 8.9 tiles/s`, `PLACE_S = 0.4 s`, entity mining time from prototypes (+0.3 s reach overhead),
`H` = `helper.stats` mined ore per minute of mining+walking time when there are at least 3 minutes of samples, else
8.5. The player's own hand work is **not** counted (the player may be busy elsewhere); the summary says so. Measured
`production` is shown as a sanity number but not added (it already contains the helper's hand mining).
`plan.build_minutes` and `HAND_MINING_S` are replaced by this model.

Every Plan gets `stats["bill"] = {need, have, salvage, short}`, `stats["eta"] = {first_plates, target, complete,
assumes: "helper only, player not counted"}` (minutes, `null` when > 180).

### 4.4 R1 gates on the first stage

| Gate | Condition | Effect |
|---|---|---|
| `SLOW_START` (loud) | first stage `eta.first_plates > 15` min | red first summary line: "first plates in about N min: needs X more iron than you have" |
| `BILL_UNREACHABLE` | stage-1 `short` Fe > 10 x current plate rate per min + 60 | same loud warning |
| hard | first stage `eta.first_plates > 45` min | not offered as a first stage; the planner re-stages (moves the expensive part to a later stage) and offers the cheaper first stage instead |

## 5. R2: topology choice (hub vs on-site vs hybrid)

### 5.1 Inputs

- Patches: all patches of each resource within the export with at least `MIN_AMOUNT` x drills of ore, not only the
  nearest. Candidate patch sets are chosen **jointly** (section 6.2), not per resource by distance to the player.
- Distance fields: `walk_distance` over the grid where water, cliffs and unremovable entities are impassable, ore is
  passable, and removable existing builds are passable at their removal cost (section 8.4). Rocks and trees are
  passable (they get cleared).
- Consumer site K: where plates are used (science, mall). Default: next to the iron smelting, because iron is about
  2/3 of starter volume.

### 5.2 Algorithm

Per stage:

1. **Bootstrap (burner) stage: always on-site.** Burner pairs on each ore patch that gets smelted; no ore belts at all.
   The only belts allowed are a collection belt along a pair row (at most 2n + 2 tiles) and a coal loop. Output goes
   to a chest. Copper is only included in the first stage when science or copper items are in the stage target.
2. **Electric stage: per ore, compare three transports** for moving ore or plates from patch P to K:
   - `belt_ore`: drills -> belt -> smelting at K. One-time cost `1.5 x d(P, K) x lanes`.
   - `onsite_belt_plates`: smelt at P, belt plates to K. Same belt cost; furnaces need coal at P.
   - `onsite_ferry`: smelt at P into a chest; the helper's logistics round (section 10.1) carries plates to K. Zero
     belt cost; helper time `v / CARRY x 2 d / WALK` per minute (CARRY = 1000 plates per trip).
   Take `belt_ore` if `d <= 40`; else `onsite_belt_plates` if `d <= 100` and its belt Fe is below 15% of the stage
   bill; else `onsite_ferry`.
3. The resulting shape is named:
   - **CENTRAL**: iron and copper both use `belt_ore` to one site (both within 40 tiles). The v1 hub engine places it,
     with the site from section 6.
   - **HYBRID**: iron smelted at its patch (K there), at least one other ore belted, at least one ferried.
   - **ON-SITE**: every ore smelted at its patch, plates ferried to K.

On the sprawled map this yields ON-SITE for stages 1-2, which is what an expert would do; on the three old seeds
(iron, copper, stone and coal within 30-80 tiles of each other) it yields CENTRAL or HYBRID, so their layouts barely
change.

### 5.3 Belt-length caps (enforced in the router and in quality gates)

`_route(..., max_len)` stops the A* search at `max_len` (cheaper than finding and rejecting a long route). Every route
is recorded in `stats["routes"] = [{what, from, to, tiles}]` and every plan reports `belt_tiles`, `longest_run`,
`belt_fe = 1.5 x belt_tiles`.

| Stage | Soft warning | Hard (stage not offered as built-now) |
|---|---|---|
| Bootstrap | any run > 30 tiles; total belts > 60 | any run > 60 tiles |
| Electric | ore run > 20 tiles (furnaces belong at the patch); plate/coal run > 60; total > 150 | any run > 100 tiles |
| Long-term (hub) | any run > 100 | any run > 160 |

## 6. R3: smelting/hub site and power placement

### 6.1 Site cost

For a candidate site s (the centre of the smelting inputs; on-site sites are candidates along each patch edge):

```
C(s) =  sum over transported flows r:  c_belt * lanes_r * p_r * D_r(s)
      + c_pole * D_power(s)                       # pole line from the power plant
      + P_clutter(s) + P_ore(s) + P_cover(s)
```

- `D_r(s)`: walk distance (section 5.1) from s to the nearest edge tile of the chosen patch of r; for coal,
  distance to where coal actually joins (the ore belt corridor or the burner rows), not to the hub centre.
- `c_belt = 1.5` Fe per tile, `lanes_r = max(1, ceil(v_r / 450))` (v_r in items/min, one lane = 450/min),
  `c_pole = 0.07`.
- Priority `p_r`: iron 1.5, copper 1.0, coal 0.8, stone 0.3. Together with lanes this ranks iron highest (largest
  volume, the dominant plate), then copper, then fuel coal, and stone low (small, can be ferried).
- `P_clutter`: if the footprint overlaps an existing build, +infinity for variants that keep everything, otherwise
  the removal cost of the overlapped clusters (section 8.4).
- `P_ore`: 0.5 per footprint tile on ore (off ore preferred, edges acceptable).
- `P_cover`: 0 when the footprint fits, else infinity (`_Sum` integral-image check as today). The bus is no longer part
  of the rectangle that must be free: it is reserved as a corridor and routed.

Ranking and final pick: candidates are ranked by `C(s)`; the planner tries the best `want_sites` (default 4) and picks
among the successful layouts by the **variant score** (section 9.3), never by belt count alone. This fixes causes 2 and
3.

### 6.2 Patch choice

For each candidate site, choose the patch per resource minimising `p_r x D_r(s)` subject to `amount >= MIN_AMOUNT x
drills`. Distant large patches can win over a near tiny one; the player's position only breaks ties.

### 6.3 Clutter push detection

Compute the best site three times: `s_free` on a world without existing builds, `s_wall` with them as walls, `s_rm`
with them removable. If `|s_wall - s_free| > 30` tiles or `C(s_wall) > 1.3 x C(s_free)`, emit `CLUTTER_PUSH` ("existing
builds push the smelting N tiles away from iron") and hand `s_rm` with the overlapped clusters to the rebuild analysis
(section 8). The keep-everything variant never moves silently: the warning is shown.

### 6.4 Power

- The steam plant is placed at the shoreline tile minimising `1.5 x coal_belt_len + 1.0 x pipe_len + 0.07 x
  pole_len_to_consumers`, over shorelines within 120 tiles of the consumers' centroid (not of the player).
- Size: `boilers = ceil(load_kw x 1.25 / 1800)`, `engines = 2 x boilers`, one pump. Load is the planned electric
  consumers plus existing unpowered labs/assemblers the plan powers.
- Power always travels by poles. Existing poles and powered networks are reused when within 10 tiles (connect, not
  rebuild).
- Boiler coal: a coal belt only when the coal patch edge is within 60 tiles of the plant; else a boiler-side chest fed
  by the helper's logistics round (27 coal/min per boiler is within one helper's capacity for 1-2 boilers) and a
  warning that a coal belt is a later step.

## 7. Layout blocks (new, in `blocks.py`)

| Block | Shape | Validates as |
|---|---|---|
| `burner_pair_row(n, side)` | n burner drills on ore, a stone furnace in front of each (direct insertion), a burner inserter out of each furnace onto a collector belt, collector ends in a wooden chest (iron chest when no wood is obtainable) | drill -> furnace -> inserter -> belt -> chest; existing validator case `drill_furnace_ok` extended |
| `coal_to_chest(n)` | n burner drills on coal dropping straight into chests (pairs facing each other refuel each other as an option) | drill -> chest |
| `stone_to_chest(n)` | same for stone | drill -> chest |
| `onsite_column(drills, furnaces)` | electric drills both sides of a short belt (<= 20 tiles) into a furnace column at the patch edge, ore on one lane, coal on the other, output inserters onto a plate belt ending in chests | reuses `drill_column` + `smelter_array` |
| `science_cell(labs, asm)` | assemblers for gears and red packs feeding labs, input chests for plates | existing assembler checks |

Hand-fed burners (pair drills, burner inserters, furnaces without a coal lane) are listed in the plan's `fuel_at`, as
today, and the helper's refuel covers them. The `machine_output` validator error does not apply because each furnace
has an output inserter.

Sizing of a pair row from stock: `n = min(fit_on_patch_edge, floor(avail_Fe / 9) + salvaged_drills,
floor(avail_stone / 10) + salvaged_furnaces...)`, then the ETA model decides whether gathering for more pairs before
starting is worth it (it usually is not: a pair repays its iron in about 36 s of output).

## 8. R7: existing builds - keep, repair or replace

The planner may propose dismantling part of what the player built when a specific replacement scores meaningfully
better. It never proposes replacing the whole base by default, and it always offers a variant that keeps everything
that works.

### 8.1 Clusters

`planner/existing.py: clusters(world, state)` segments non-ghost force entities:

1. Connected components with an 8-neighbour, 1-tile-gap adjacency; belts are split at junctions and into straight
   runs; poles join the cluster whose entities they power, else a "power line" cluster.
2. Each component is classified by composition: `furnace_row` (>= 2 furnaces), `drill_line`, `belt_line`,
   `power_plant` (pump/boiler/engine), `power_line`, `labs`, `assembly`, `storage`, `mixed`.
3. Status per cluster from `base_state`: `working` (most machines working), `idle_no_fuel`, `idle_no_input`,
   `idle_no_power`, `output_full`, `disconnected`, with the reason counts and the positions of up to 5 samples.
4. Per cluster: `items` (salvage value), `fe_equiv`, `rate_now` (items/min it produces now), `rate_fixed` (what it
   would produce once repaired), `belt_tiles`, `dist_to_patch` for the ore it uses.

**Never in a cluster, never removed:** ghosts of any kind (player ghosts and Claude ghosts of other plans are
obstacles the new plan must avoid), characters, vehicles, rail, and containers holding more than 200 items (they are
`keep` with a note "empty it first to allow moving").

### 8.2 Actions per cluster

| Action | What it means | Cost | Gain |
|---|---|---|---|
| `keep` | leave as is; the plan routes around it | 0 | `rate_now` |
| `repair` | helper tasks only: refuel, feed ore/plates into input slots from a chest, connect a pole, empty a full output | coal/ore/poles needed + helper time | `rate_fixed - rate_now` |
| `reuse` | keep it and plug the new plan into it (e.g. new burner pairs feed the existing iron belt into the existing furnaces) | the connecting entities | treated as part of the plan's production |
| `replace` | dismantle the cluster, credit salvage, build the plan's own version at a better spot | dismantle time (mining time per entity + walking) | the plan's score with this cluster removed vs kept |

`repair` and `reuse` steps form **stage 0** (section 10).

### 8.3 Deciding

For each variant the planner first builds the **keep** version (all clusters `keep`, idle ones `repair` if cheaper than
the plan's first build step). Then, for each candidate cluster c (at most 6, those with the highest `fe_equiv` among
clusters that are idle, block a better site (`CLUTTER_PUSH`), block a route, or have a run over the belt cap):

```
gain(c) = score(keep version) - score(version with c replaced)      # minutes; salvage credited in the bill,
                                                                    # dismantle time charged to the helper
```

A cluster is proposed for replacement only if:

- `gain(c) >= max(3 min, 10% of the keep score)`;
- and, if the cluster is `working`, `gain(c) >= 2 x` that threshold;
- and the union of replaced clusters stays at most 50% of the base's non-belt entities and at most 50% of its belt
  tiles (unless the goal text explicitly says rebuild everything, for example "rebuild my base from scratch").

Greedy: take clusters by gain, re-score after each, stop when no candidate passes. Each kept removal records a one-line
reason, for example: "16 idle stone furnaces 180 tiles from iron: picking them up gives 80 stone for burner pairs on
the iron patch; first plates 12 min sooner, 210 fewer belt tiles".

### 8.4 Removal cost (also used by the site cost)

`removal_cost(c) = sum(mining_time + 0.3 s) + walk(cluster) / WALK` helper seconds, converted to score minutes; no
material cost (full refund). For site-cost purposes, `P_clutter = 1.5 x removal_cost_minutes x (Fe rate of the stage)`
so the site search can compare "walk around it" with "remove it" in the same unit.

### 8.5 Plan representation

`Plan` gains:

- `removes: [{name, x, y, unit, cluster}]`: exactly the entities to dismantle (unit_number when known; name and
  position are the fallback key).
- `stats["clusters"]: [{id, label, kind, status, action, reason, count_by_name, gps, gain_min}]`.
- `stats["net_gain"] = {eta_first_plates_min, eta_target_min, belt_tiles, fe}` vs the keep version of the same variant.

`world_without(world, removes)` produces the world the replacement is validated against (the validator must pass
on it), and `world_without_plan` keeps working for revalidation after the removals happened.

### 8.6 Mod side: preview, approve, dismantle, cancel

**Preview** (`plans.lua`): rows in `removes` are drawn as red outlined boxes with a red X, game and map view, legend
entry "To dismantle (yours)", separate from "To clear (trees/rocks)". The card lists the count by name and the clusters
with `[gps]` pins and the net gain.

**Approve** (`plans.apply`, host/admin only as today):

1. For each `removes` row: find the entity at exactly (x, y) with the same name, same force, not a ghost (and the same
   unit_number when given). Skip it and report it as `changed` otherwise. Then `order_deconstruction(force)` and record
   `p.removing[unit] = row`. Nothing else is marked. Cap: 2000 rows per plan.
2. Place every replacement ghost whose footprint does not overlap a marked entity. Ghosts that overlap go to
   `p.deferred`. (If the engine accepts a `manual_ghost` placement over an entity marked for deconstruction, the
   implementation may place them at once; this must be checked in the engine first and recorded in CONTRIBUTING.md's
   verified facts. The deferred path stays as the fallback.)
3. The helper's queue becomes `dismantle {plan}` then `build_ghosts` then refuel, as one work order.

**Helper task `dismantle {plan}`** (`agent.lua`): walk to the nearest entity in `p.removing` that is still valid and
marked, `entity.mine{inventory = helper}` (items and contents go to the helper; when its inventory is full, it puts
items into the plan's first chest ghost once built, else gives them to the player, else drops a wooden chest and
reports). After each removal, call `plans.place_deferred(p)` for ghosts whose footprint is now free.

**Events**: `on_player_mined_entity`, `on_robot_mined_entity`, `on_entity_died`, `script_raised_destroy` drop the unit
from `p.removing` (the player may dismantle it himself); `on_cancelled_deconstruction` on a listed unit drops it and
keeps its deferred ghosts unplaced (reported as "kept by you").

**Reject / Dismiss / `plan_drop` / `clear_claude_ghosts {plan}`** before completion: `cancel_deconstruction` on every
unit still in `p.removing` that is valid and marked; deferred ghosts are discarded. Ghosts already placed follow
today's rules (Dismiss keeps them, `clear_claude_ghosts {plan}` removes them). Player ghosts are never touched: only
ghosts tagged with the plan id are ever placed or removed.

A plan with `removing` or `deferred` entries stays in `storage.plans` (status `applying`) until both are empty; today
applied plans can disappear from storage, which also made the incident hard to reconstruct.

`plan_info` / `plan_get` return `removes` counts: `{listed, marked, removed, changed, cancelled}`.

The in-game Claude still cannot deconstruct large areas on request (companion prompt rule); removals only happen
through an approved plan in the Plans tab.

## 9. R4: variants and scoring

### 9.1 Variants

All variants share the same analysis (state, clusters, distance fields) and differ in target, topology limits and
whether removals are allowed. Old keys stay as aliases: `compact` -> `quick`, `balanced` -> `balanced`,
`expandable` -> `longterm`.

| Variant | Stages it contains | Target | Topology | Removals |
|---|---|---|---|---|
| **Quick start** (`quick`) | S0 repair + S1 burner pairs | plates flowing in <= 10 min; ~60 Fe/min at S1 end when stock allows | on-site only | none |
| **Balanced** (`balanced`) | S0 + S1 + S2 (power, research, labs) + S3 (electric/science) as stages | 60 Fe/min + 30 Cu/min, then 0.1-0.2 red/s | from section 5.2, electric ore runs <= 20 | none |
| **Long-term hub** (`longterm`) | S0 + S1 + later central hub with bus (v1 engine, fixed weights) | 120 Fe/min + 60 Cu/min, bus with spare lanes | CENTRAL or HYBRID only; on an ON-SITE map it is offered as "later stage" with its own ETA | allowed if justified |
| **Rebuild** (`<base>+rebuild`) | the best of Quick/Balanced with the justified removals of section 8.3 | as its base | as its base | the listed clusters |

- Quick start and Balanced always come in a keep-everything form. The Rebuild variant appears only when section 8.3
  finds at least one cluster to replace; its card says exactly what it removes and why.
- At most 4 cards. Default `variants=3` shows Quick, Balanced and either Rebuild (if it exists) or Long-term; the tool
  argument can ask for all 4.
- Goal words: "electric" no longer forces the electric stage. It selects Balanced as recommended and makes its
  electric stage the highlighted one; if research or the bill is missing, the stage shows "after research X" or
  "about N min after stage 1" instead of being planned as if available (`assume_research` only in tests).
  "bigger"/"small" scale the target by 1.5/0.5.

### 9.2 Build steps and payback order

Each stage is broken into steps with a site and a production gain. Steps are ordered by payback =
`(plates/min gained) / (Fe-equivalent net cost + 1)`, highest first, with dependencies (a furnace column before its
drills, power before electric consumers). Typical order: refuel idle furnaces with a feed (cost: coal only) > burner
pair rows > coal drills > stone drills > power > labs > assemblers > electric columns.

### 9.3 Score (lower is better, minutes)

```
score = eta.first_plates + eta.target
      + 0.02 * belt_tiles
      + 5  * (soft gates)
      + 10 * (hard gates)                        # usually the plan is not offered at all
      + 3  * (1 - reuse_fraction)                # share of existing working machines kept or reused
      - 2  * expand_room                         # 0..1: free column width beside each column, room on the patch
      + removal_penalty                          # 0.05 min per removed entity, +3 if a working cluster is removed
```

Variants are listed sorted by score; the recommended one is the lowest score unless the goal asks for long-term.
Tests pin the score function with a hand-checked case.

## 10. R5: stage progression

| Stage | Contents | Offered as "build now" when |
|---|---|---|
| **S0 fix** | helper tasks only: refuel idle burners, feed ore into idle furnaces from a nearby belt/chest/patch, connect stray poles, power idle labs if a network exists. No ghosts unless a 1-3 entity connection is needed. | there are idle clusters whose repair costs less than the first build step |
| **S1 burner** | pair rows on iron (and copper if the target needs it), coal to chests, stone to chest if stone is short | always (sized to stock) |
| **S2 power + research** | steam plant (section 6.4), poles to S1 sites and to labs, labs (reused or new), red science hand-crafted by the helper or by assemblers when researched; research queue for `electric-mining-drill`, `automation`, `logistics` | simulated ETA(S2) <= 20 min after S1 starts producing |
| **S3 electric + science** | electric drill columns replace the burner pair rows **in place** (the drills move off, furnaces stay or extend into a column), science cell at the iron site | the needed techs are researched or will be within the S2 research ETA |

- Each stage is planned on a world with the previous stages present as existing builds (`connect_existing=True`).
  S3's replacement of S1 pairs is expressed as `removes` of the S1 drills (it is the same mechanism as section 8,
  scoped to the plan's own previous stage, so it needs no extra justification beyond "upgrade").
- Each stage keeps at least 70% of the previous stage's non-belt entities and leaves one free column width beside
  every column.
- Stage content is filtered by `gd.buildable`; a missing technology becomes a research step with its own ETA
  (`packs x time / labs`), never a silent `assume_research`.
- Plans get `parent` (previous stage plan id) and `stage_index`. In the Plans tab, a variant is one card with its
  stages as numbered lines; Approve builds the stage marked "now"; the next stage is planned again from the then
  current state when the player asks (the old later stages are only estimates).

### 10.1 Helper logistics round

ON-SITE and HYBRID topologies need items carried between sites. New helper task `round {plan}`: when idle, every 5
game minutes: refuel the plan's hand-fed burners, empty plate chests at smelting sites into the chest at the consumer
site K, carry coal from coal chests to boiler chests. It is a loop like `follow`, stopped by any new order. Its time
share is charged in the ETA model and shown ("the helper spends about 20% of its time carrying plates").

## 11. R6: quality gates

New `planner/quality.py: assess(plan, state, world) -> [Issue(code, level, text)]`, separate from the validator (which
stays the correctness check). Hard issues stop the plan from being offered as a build-now stage (it may be shown as
"later"); soft ones are warnings shown in red and add to the score.

| Code | Level | Condition |
|---|---|---|
| `SLOW_START` | soft (loud) | first stage first plates > 15 min |
| `FIRST_STAGE_TOO_BIG` | hard | first stage first plates > 45 min |
| `BILL_UNREACHABLE` | soft (loud) | short Fe > 10 x plate rate/min + 60 |
| `LONG_BELT` | soft / hard | section 5.3 table |
| `TOO_MANY_BELTS` | soft | totals in section 5.3 |
| `SMELTING_FAR_FROM_IRON` | hard | iron smelting > 40 tiles from the iron patch edge (S1/S2), > 80 (Long-term) |
| `WATER_IGNORED` | soft | water within 60 tiles of the consumers but the steam plant is more than 1.5 x that distance away, or a pipe run > 30 |
| `OVERSIZED_POWER` | soft | boilers > ceil(load x 1.25 / 1800 kW) + 1 |
| `RESEARCH_MISSING` | hard for the stage | an entity of the stage is not buildable and no research step precedes it |
| `CLUTTER_PUSH` | soft | section 6.3 |
| `IDLE_FIXABLE_IGNORED` | soft | an idle cluster is cheaper to repair than the first build step and no S0 step uses it |
| `OVERLAP_EXISTING` | hard | overlap with an existing entity that is not in `removes` (the validator catches it too) |
| `REMOVAL_UNJUSTIFIED` | hard | a `removes` entry with no cluster reason or a gain below the threshold |
| `REMOVES_TOO_MUCH` | hard | removal caps of section 8.3 exceeded without the explicit goal words |

A plan validates **and** passes quality, or it is not shown as build-now. `testenv/acceptance.py` checks both.

## 12. Summaries (game and Claude)

### 12.1 Plans tab card (max 8 lines, plain text)

```
Quick start - burner pairs on site. First plates in ~6 min, 60 iron/min after ~14 min.
Needs 72 iron, 80 stone, 30 coal. You have 135 / 83 / 16, short 14 coal (helper mines ~2 min).
Belts 14 tiles (longest 12). Footprint 18x10 at the iron patch [gps=..]. No power.
Removes: nothing.
Trade-off: plates soonest and cheapest; copper and science come in the next stage.
[red] Iron patch is 160 tiles from your current furnaces: plates are collected in a chest there.
```

A Rebuild card replaces the "Removes" line with: `Removes 16 stone furnaces, 120 belts (old iron line [gps=..]) and 8
burner drills [gps=..]: first plates 12 min sooner, 210 fewer belt tiles. Items come back to you.` The facts line
(`gui.lua`) shows `stage, eta.first_plates, belt_tiles, removes count`. Stored with the plan in `plans.lua`
(`plan_preview` gains `facts`, `removes_summary`).

### 12.2 For Claude (`Plan.brief()`)

```
{id, title, variant, stage, topology, recommended,
 eta: {first_plates, target, complete, assumes},
 bill: {need: {...}, have: {...}, salvage: {...}, short: {...}},
 per_min: {...}, belts: {total, longest, over_cap}, footprint, power_kw,
 removes: {count_by_name, clusters: [{label, gps, reason, gain_min}]},
 net_gain, score, gates: [{code, level, text}], tradeoff, needs_research, warnings}
```

The companion prompt, the `plan_starter_base` docstring and `skill/factorio/SKILL.md` require Claude's reply to name,
for each variant, the time to first plates, the bill against what the player has, and what (if anything) it removes.

### 12.3 MCP

- `plan_starter_base` reads `base_state` around the player (radius = plan radius) and passes it to the planner; new
  optional `variants` up to 4 and `allow_removal` (default true; false hides Rebuild).
- `world_summary` adds a short state block: stock totals, production, idle clusters with reasons.
- `plan_helper(what="build")` on a plan with removes queues dismantle first (no separate call needed);
  `what="round"` starts the logistics round.

## 13. Fixtures and tools

- **`seed_4004` "sprawl"** (public, synthetic, `make_fixtures.py sprawl 4004`): a fresh map with ore autoplace
  turned off; a script on a test slot creates iron (~2,000 tiles) 180 tiles from spawn, copper 150 tiles in another
  direction, coal 90 tiles, stone 200 tiles; a lake off-centre (`set_tiles`) 70 tiles from spawn; and scripted
  clutter in the centre: an idle row of 12-16 stone furnaces fed by a 60+ tile belt from nowhere, 6-8 unfuelled burner
  drills off any patch, 2 unpowered labs, scattered belts and a pole line. A synthetic `state.json` (about 100 iron
  plates, 60 stone, 20 coal, 1 burner drill, no production, fresh research + steam power). Saves to
  `testenv/write/seed_4004.zip`, fixture folder `testenv/fixtures/seed_4004/` with `map/`, `data.json`, `state.json`,
  `screen.json`; README regenerated.
- **Private fixture** derived from the real-save copy: `testenv/private/real_1028/` (map export + state), never
  committed; tests that use it skip when it is absent.
- **`testenv/render_plan.py`** (publishable): renders a World + Plan offline to PNG with numpy + zlib (no PIL): terrain,
  water, ore by colour, existing builds grey, plan entities by kind, removals red, route lengths labelled. Used for
  the visual judge.

## 14. Acceptance criteria

All run on test slots 31-39 only, via `testenv/restart.ps1`; a real game running on the same machine, its default ports and the Factorio user folder
are never touched. Real-copy runs first execute `clear_claude_ghosts {plan="exp-elec-9c45"}` so the copy matches the
current real state. Real-copy artifacts stay in `testenv/private/`.

**A1 - real copy, quality and real ETA.** On the real-save copy:
- every proposed variant's first stage has every belt run within the section 5.3 cap, iron smelting within the section
  11 distance of the iron patch, and shows bill and ETA (card and brief);
- the Quick start variant, built by the helper with **only** the player's and helper's real inventory plus what it can
  mine and craft (server started with `-NoDebugOps`: no instant build, no given items; approval through the
  `plan_apply` op; `game.speed` up to 10), produces its first plates within about 10 in-game minutes;
- the measured time to first plates is within 50% of `eta.first_plates` (`|measured - predicted| <= 0.5 x predicted`).

**A2 - real copy, reuse.** When repairing the idle existing furnaces (fuel + feed) is cheaper than the first build
step, a variant or the S0 pre-step does it (gate `IDLE_FIXABLE_IGNORED` absent), and after the helper runs it those
furnaces report `working`.

**A3 - old seeds.** Seeds 1001, 2002 and 3003 still pass `testenv/acceptance.py` (plan, apply, build, run at speed 10,
measured rates within 10% of the plan) for every offered variant and stage.

**A4 - sprawl fixture.** `seed_4004` passes: validation, quality gates (no hard issue on any build-now stage), measured
rates within 10% in `acceptance.py`, the belt-length gate, and the visual judge: `render_plan.py` PNG per variant is
produced and reviewed (no run over the cap, smelting next to iron, no plan entity on clutter that is not in `removes`).
The automated geometric checks are part of the test; the visual review is recorded in the PR.

**A5 - suites.** `pytest` green offline (new unit tests for state, cost/ETA, topology, site cost, clusters and removal
decision, quality, summaries, the variant-name updates in `test_planner.py` and `test_planner_tools.py` with the alias
justification); CI green on Linux, macOS and Windows after push; `luac -p` clean.

**A6 - real copy, rebuild.** On the real-save copy:
- at least one variant proposes replacing a poorly placed existing cluster, with a stated justification and net gain;
- approving it marks exactly the listed entities (census of force entities by unit_number before and after: removed
  set == listed set minus `changed`, nothing else removed, player ghosts unchanged in count and position);
- the helper dismantles them, the salvage ends in the helper's or player's inventory (item delta matches the
  computed salvage within 5%), then the replacement is built (including formerly deferred ghosts);
- a separate Reject (and Dismiss) test after Approve but before dismantling finishes cancels every remaining mark
  (`to_be_deconstructed()` false for all listed units) and touches nothing else.

## 15. Implementation plan

One pass, in this order; each step lands with its tests and keeps the old suites green.

| Phase | Content | Requirements |
|---|---|---|
| P0 | `cost.py` (bill, available, simulate), `quality.py` gates, `_route(max_len)` + route stats, site-cost weights and final pick by score, power at the shoreline near consumers, the `_route` reachability fix | R1 (offline), R3, R6 |
| P1 | `base_state` op, `unit` in the map export, helper stats, `state.py`, state in `plan_starter_base`, synthetic `state.json` for the old seeds | R1 |
| P2 | `burner_pair_row`, `coal_to_chest`, `stone_to_chest`, `onsite_column`; `topology.py`; Quick start variant; `seed_4004` fixture and `render_plan.py` | R2, R4 |
| P3 | `existing.py` clusters, S0 repair steps, keep/repair/replace decision, `removes` in Plan, Rebuild variant | R3, R7 |
| P4 | mod: removal preview, approve marking, deferred ghosts, `dismantle` and `round` helper tasks, cancel on Reject/Dismiss/drop, plans kept in storage while pending | R7 |
| P5 | `staged.py` stage chain S0-S3, Balanced and Long-term variants, summaries (card facts, brief, prompt, SKILL.md) | R4, R5 |
| P6 | acceptance A1-A6, CI, changelog and version bump | all |

Risks and checks to do early: whether `manual_ghost` placement is allowed over entities marked for deconstruction;
the helper's real walk/mine/place rates (calibrate `H`, `WALK`, `PLACE_S` on a test slot before trusting A1's 50%
bound); research costs read from game data, not hard-coded; burner inserter fuel use in pair rows (hand-fed list).

## 16. Implementation status (round 1)

Implemented (Python: `cost.py`, `state.py`, `existing.py`, `onsite.py`, `quality.py`, `staged.py`; mod:
`base_state.lua`, `plans.lua`, `agent.lua`, `gui.lua`):

- R1: `base_state` op (player/helper inventories, chests, machine status/fuel/contents, production, research, helper
  work counters) with a fallback to the older ops; items-first bill, salvage, shortfall and the discrete ETA
  simulation (`cost.simulate`, start fuel burns inside the machines, the helper gathers the scarcest raw first and
  keeps the burners fed). Gates `SLOW_START`, `FIRST_STAGE_TOO_BIG`, `BILL_UNREACHABLE`.
- R2: stage 1 is always on-site; the electric-stage transport choice per ore (`belt_ore` / `onsite_belt_plates` /
  `onsite_ferry`) is computed and reported as the topology (CENTRAL / HYBRID / ON-SITE).
- R3: v1 hub site cost now weighs priority x lanes per resource, steam power uses the water nearest the consumers,
  the final pick penalises single runs over 100 tiles, and the reachability pre-check no longer rejects underground
  crossings. Route lengths are reported (`routes`, `longest_run`).
- R4: variants `quick`, `balanced`, `rebuild`, `longterm` (aliases compact/expandable), score of 9.3 plus half the
  stage-2 minutes, cards with bill + ETA + belts + removals + trade-off (one line each in the Plans tab), the brief
  for Claude, companion prompt and SKILL.md.
- R6: `quality.assess` (all codes of section 11 except `OVERSIZED_POWER`/`WATER_IGNORED`, which only fire on plans
  that carry power sizing / water stats, i.e. none of the stage-1 plans yet).
- R7: clusters, same-kind groups (two rows of one block), useful-salvage candidate ranking, greedy keep/replace with
  the 8.3 thresholds and caps (the player's words "tidy up", "reuse my old stuff", "dismantle" lower the bar to a
  0.5 min gain; "rebuild" lifts the caps), reasons, `removes` rows, red preview, approve marks exactly the listed
  entities, the helper's `dismantle` task, cancel on reject/dismiss/drop/clear, counts in `plan_get`.

Deviations, decided while implementing:

- `burner_pair_row` ends every pair in its own chest (drill -> furnace -> burner inserter -> chest) instead of a
  collector belt: no belts at all, cheaper (2 wood or 8 iron per pair instead of ~3 iron of belt plus end
  inserters), and it is the already engine-verified `drill_furnace_ok` case (14-15 plates/min per pair measured).
- Stage 2 (steam power + labs + research) is estimated (bill, minutes after stage 1, labs reused or moved), not
  placed yet; stage 3 (electric columns at the patch) is not modelled yet.
- 2.1 accepts a manual_ghost placement over an entity marked for deconstruction, so replacement ghosts are placed
  at approve; the deferred path only covers what the engine refuses.

Measured in round 1: on the real-save copy the stage-1 variants have no belts and first plates in ~3.5 min
(model); with the default thresholds no old build is worth taking apart there (best: 7 idle furnaces in the
middle, ~2 min) and the card says so; with "tidy up" the rebuild variant takes exactly those 7 furnaces apart,
which `tests/live_rebuild.py` verified end to end on a test slot (marks, reject, dismantle, salvage, player ghosts).

## 17. Round 2: fuel logistics, honest ETA, player ghosts

Round 1's burner plans starved in a real build: the ETA model counted coal 95 tiles away (and coal the helper "would
gather") as already inside the burners, and the helper never carried coal. Approve also built the player's own
ghosts. Fixed:

- **Only the plan's ghosts.** `build_ghosts {plan}` builds ghosts tagged `claude_plan == plan` only (GUI Approve,
  Helper: build and `plan_helper` all pass the plan id); the player's ghosts and other plans' ghosts are never built.
- **Coal logistics in the mod.** `build_ghosts {plan, order, fuel, fuel_at}` builds site by site in the plan's
  `build_order` (gathering each site's materials, then - right before placing, so the helper's own hand-smelting
  can't burn it - the coal its burners start on), puts start fuel into each new burner (coal only: drill 8,
  furnace 5, inserter 1, ~3.5 min), and tops up the burners already built before it moves on to the next site.
  Then the helper runs the plan's logistics **round** (section 10.1): every 2.5 min, when any hand-fed burner is
  below a third of a fill (fills: drill 50, furnace 30, inserter 5 coal, ~20 min), it fetches coal - the starter
  amounts first (the plan's coal chests, other chests, the player, else hand-mining), then once the full top-up from
  already mined coal only (chests, the player; never hand-mining full stacks) - allocates starters first and the
  rest in plan order, and delivers nearest first (coal only; a slot holding wood is left until it has burnt). Any
  new order ends the round; `plan_helper(id, "round")` restarts it. A refuel run no longer stops after the first
  entity it can't fill. Coal drills are sized from the burn rate plus one spare when a site is more than 100 tiles
  from the coal patch (the chests must fill faster than the burners use coal while their fills ramp up). The material planner looks for ore up to 400 tiles from the build site (was 200: plans
  span far-apart patches), smelts next to the ore it mines (not at the build centre, up to 200 tiles away), and a
  mining trip whose walk gets stuck (nests, water) tries another part of the patch instead of dropping the whole
  order. A long walk whose path request fails (the script pathfinder gives up on long detours, e.g. around a lake)
  goes in legs of ~28 tiles instead of walking straight into the obstacle; the helper also steps aside when it
  stands on the ghost it is building.
- **S0 reuse of the player's builds.** Idle machines (no input, no power, blocked output) of the base give their fuel
  to the new burners (`takes`: exactly the listed entities, shown on the card), without taking anything apart. Fuel
  and other contents never justify a removal (they can be taken out). S0 repairs refuel idle burner drills that drop
  into a furnace or chest on ore, and furnaces holding at least 20 ore; every other idle build gets a one-line verdict
  (`idle_review`: no power -> stage 2, nothing feeds it and the ore is N tiles away, output blocked).
- **ETA model = what the helper does** (`planner/agenda.py`): takes, dismantling, per-site gathering (walk to the
  patch, hand-mining 2 s per unit, plates smelted in one furnace near the plan centre in growing chunks, wood),
  hand-crafting, placing with start fuel, a top-up between sites, then the round (coal drills fill their chests;
  burners run only while they hold fuel). The build order (plates first or coal drills first) is chosen by this
  model. Outputs: first plates, full rate (the time from which every 5-minute average stays at >= 95 % of the target:
  held, not a peak on start fuel), completion, `sustained` (output over the 20 min after the build is complete /
  target) and the helper's share of time on the round at steady state. New gates: `FUEL_SHORT` (sustained < 90 %,
  hard < 50 %, or full rate never reached), `HELPER_BUSY` (> 60 % of the helper's time), `NESTS_NEAR` (a site within
  120 tiles of biter nests, or the helper hand-mining next to them; copper/stone sites on an infested patch are left
  out and said so).
- **Stock is what the helper can take**: the player's inventory counts only when the player is connected and the
  `claude-helper-use-player-items` setting is on (`base_state` reports both).
- **Rebuild decisions** use the gated score (removal bookkeeping gates excluded while deciding) and salvage without
  contents. The keep cards point at the Rebuild card when one exists ("The Rebuild (balanced) card takes apart X
  instead, ~N min better than balanced without it") and otherwise say what was checked and the gain it fell short of.
- **Long-term hub re-staged** (section 4.4): when the hub fails a hard gate its card is the quick start now plus the
  hub as a later stage (bill, belt tiles, longest run, the gates it fails).
- Cards list every site with its size and `[gps]`, the coal for the first fill and where it comes from, the round
  (site spread, helper share) and the stage 2/3 path (power + research, then electric drills and furnace columns at
  each patch with red science assemblers, with the research it waits for). Stages 2-3 are still estimates, not placed
  plans; `testenv/acceptance.py` says so and skips `--stages electric` for v2.
- `testenv/acceptance.py --no-cheat`: player-like server, the real helper builds and fuels (no revive, no coal handed
  out), first plates measured against the ETA. `tests/live_onsite.py`: the same for one variant of any save (real
  copy included), with the player-ghost census, removal census and burner fuel at the end.

Measured in round 2 (test slots, no debug ops, the real helper from Approve on):

| Map / variant | First plates measured vs model | Plates/min vs plan | Other |
|---|---|---|---|
| real copy, quick start | 1.52 vs 1.5 min | 60.1 / 60 | 112/112 player ghosts kept, every burner fuelled after 25 min |
| real copy, rebuild (balanced) | 2.91 vs 3.4 min | 90.1 / 90 | removed exactly the 18 listed entities |
| seed 1001, quick / balanced, empty inventory | 8.19 vs 9.0 / 11.05 vs 11.3 min | 100 % | `acceptance.py --no-cheat` |
| seed 3003, quick / balanced, empty inventory | 12.25 vs 12.3 / 13.84 vs 13.7 min | 100 % | `acceptance.py --no-cheat` |
| sprawl 4004, quick / rebuild / balanced, fixture state | 14.65 vs 13.0 / 9.17 vs 8.3 / 15.12 vs 14.1 min | 99.6-100 % | `acceptance.py --no-cheat`, all entities built |

Cheat-mode `acceptance.py` (built by revive, burners seeded, the helper's refuel checked) passes every variant on
seeds 1001 (via the MCP tool), 2002, 3003 and 4004 at 100 % of the planned rates.

`tests/live_rebuild.py` on the real copy: approve marks exactly the listed entities, Reject cancels every mark,
Dismiss in the middle of the dismantling cancels the remaining marks and leaves everything else alone, the dismantle
removes exactly the listed entities, the salvage ends in the helper's inventory, player ghosts unchanged.

## 18. Round 3: stage 2 placed, variants that differ, reuse in place

Round 2's review (three judges, the owner's real-save copy and the sprawl fixture) found the stage-1 builds right but
the offer thin: Long-term hub was the Quick start again with a 1,226-tile hub as a "later stage", stages 2-3 were
one-line estimates, the coal line hid that the burners depend on the helper, and a Rebuild removed the player's labs
with a reason that had no destination. Changed:

- **Stage 2 is placed and validated** (`planner/stage2.py`, section 10): for every variant, on top of its stage 1, a
  hand-fed steam engine (`blocks.steam_power(feed="manual")`: offshore pump, 1 boiler, 1-2 engines, no fuel belt; the
  helper's coal round fills the boiler) on the shore chosen by distance to the labs (plus a quarter of the distance to
  the iron site), small poles (`router.place_poles`) to the labs - the player's idle labs are powered **where they
  stand** when a shore is within 220 tiles of them (a lab costs 36 iron + 15 copper, a pole 0.5 copper + 0.5 wood),
  otherwise 2 new labs (`blocks.lab_row`, packs by hand: lab extra `input=manual`) next to the iron pairs - and copper
  pairs when stage 1 has none (Quick start). Stage 1 + stage 2 are validated together against the map (trigger techs
  that stage 1's own plates unlock - steam power, electronics, red science - count as researched and are listed as
  research steps). Stage 2 is a Plan of its own (`<stage 1 id>-s2`, saved with stage 1, shown with `plan_preview` when
  the player asks after stage 1 runs, re-validated on Approve); its coal round also keeps stage 1's burners going
  (`keep_fuelled` in the mod's fuel list: a new round replaces the old one) and the helper takes its materials from
  stage 1's plate chests first (`take_from`). Its ETA (an estimate built on stage 1's planned rates, the helper
  gathering the rest) gives when the power comes on, when red science research can start and when Automation and
  Electric mining drill are done (the stage-3 prerequisites); the score adds half of the red-science start time, so
  the recommendation weighs the path to automation science, not only the first plates.
- **Variants are different builds** (section 9.1): Quick start = iron pairs now, copper with stage 2; Balanced = iron
  and copper pairs (and a stone drill) now; Long-term = the central hub only when it passes the gates (longest run <=
  160, smelting <= 80 tiles from iron, a first stage the player can afford), otherwise **grow on site**: twice the
  smelting (8 iron + 4 copper pairs, 2 stone drills) in rows that leave room for stage 3's columns. A hub that fails
  is never offered, not even as a later stage; the card says it was checked and why not ("1,226 belt tiles, longest
  run 290"). The full-rate ETA is measured against the variant's target (more output, e.g. a converted line, is a
  bonus, not a cost).
- **Reuse in place** (R7, `PlannerV2.conversions`): an idle burner-drill line of the player's on ore that drops onto a
  belt nothing smelts from can become burner pairs where it stands: only its belt is removed, a stone furnace + burner
  inserter + chest (coal/stone: a chest) go in front of each drill (the canonical pair block rotated to the drill and
  anchored on it; drills with less than 600 ore under them are left alone). The drills are listed in the plan with
  `extra.existing` (no ghost - the mod counts them "already there" - no bill, but in the coal round). The removal
  decision tries every candidate both ways and keeps the better one (a conversion wins ties within 0.5 min, it keeps
  the player's drills); when the removal wins, its reason says the conversion was checked and by how much it lost.
  The helper does not fuel a drill whose drop target is still a ghost (the ore would land where its furnace goes).
- **Removals have a destination**: labs are never removal candidates (stage 2 powers them where they stand); every
  removal is listed on the card with its reason (no "and 1 more"); reasons say what changes in the player's terms
  (first plates / full rate sooner, a warning it avoids such as hand-mining next to biter nests) instead of score
  minutes; the keep cards compare the Rebuild card's first plates with their own.
- **Honest coal line**: "Coal: N to start the burners, then they burn ~X/min; K coal drills fill chests at ~Y/min; the
  helper carries the coal to the burners every 2.5 min (sites up to D tiles apart, ~S% of its time). Without the
  helper the burners run dry in ~20 min." The first full fill stays in the bill (`fuel.first_fill`) but is no longer
  presented as what the burners need to start. The helper's share is measured over the whole simulated run after the
  build (a 10-minute window could miss every trip and say 0 %). Trees/rocks to clear are on the card.
- **Player inventory rule, one statement**: the ETA counts the player's inventory only while the player is connected
  and the mod setting allows it, which is exactly when the helper takes from it (`agent.lua`); `eta.assumes` and the
  card say so.
- `testenv/render_plan.py`: a plan's placed stage 2 is drawn with cyan outlines, and `render_sheet()` writes one PNG
  with a zoomed panel (10 px per tile) per site, so the pair rows, the steam plant and the labs can be judged.
- New in-engine checks: `tests/live_onsite.py --stage2-minutes M` (approve the variant's stage 2 after stage 1 ran,
  the helper builds it; labs powered, research progresses, boiler and stage-1 burners fuelled, stage-1 rate held),
  `tests/live_convert.py` (a conversion built by the helper: exactly the listed belts removed, the player's drills
  kept and producing).
- Found in-engine while checking stage 2: (1) a script-placed pole ghost auto-connects to the nearest pole or pole
  ghost, including one of the player's ghosts the helper never builds, and then skips the direct link - on the real
  copy the labs stayed unpowered behind such a ghost; the mod now wires every pair of a plan's poles within reach
  when the plan is applied. (2) A lab crafted by the helper does not trigger the red-science tech (only a player's
  crafting does); when that tech is still locked, stage 2's card tells the player to craft one lab by hand.

Measured in round 3 (test slots 31-37, `-NoDebugOps`, the real helper builds everything from Approve on; stage 2
approved after stage 1 had run 15-35 minutes):

| Map / variant | Stage 1: first plates measured vs model, plates/min | Stage 2: built after approve (measured / model), result |
|---|---|---|
| real copy, quick | 1.52 vs 1.5 min, 60.0 / 60 | 27 entities, labs powered where they stand, Automation researched; stage 1 held 60.1/min |
| sprawl 4004, quick | 14.7 vs 13.0 min, 60.1 / 60 | 22 entities in 8.0 / 5.5 min, Automation researched, stage 1 held 59.8/min |
| seed 1001, quick | 8.17 vs 9.0 min, 60.0 / 60 | 26 entities (2 new labs) in 5.0 / 6.2 min, Automation researched |
| seed 2002, balanced | 10.16 vs 11.2 min, 90.1 / 90 | 11 entities in 3.0 / 3.3 min, labs powered |
| seed 2002, long-term (grow) | 15.9 vs 18.8 min, 180.1 / 180 | 11 entities, Automation researched, stage 1 held 180.3/min |
| seed 3003, balanced | 13.87 vs 13.7 min, 88.4-90 / 90 | 24 entities, Automation researched (after the `reserve` fix) |

`tests/live_convert.py` on the real copy: the 6 idle copper drills turned into pairs where they stand - exactly the 12
listed belts removed, the drills kept, 90.1 / 90 copper plates/min. `tests/live_rebuild.py` on the real copy (tidy-up
goal): marks, reject, dismiss and dismantle exactly the listed 20 / 14 entities, no labs, 112/112 player ghosts.
Planning time: real copy ~11 s, sprawl 0.8 s (the hub is not tried when its ore belts would exceed 160 tiles).
- Also found in-engine: on seed 3003 the helper's own hand-smelting furnace (built while gathering for stage 1) stood
  where stage 2's labs go, so stage 2 failed re-validation. Stage 1 now carries stage 2's footprint as `reserve`
  boxes and the helper never builds a smelter of its own there.
