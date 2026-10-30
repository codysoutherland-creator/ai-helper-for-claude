# Starter-base planner v3: master plan first, stages derived from it

> Design brief for the planner pass after v0.4.1. It keeps v2's state-aware bill/ETA, rebuild review and Plans-tab
> workflow (`PLANNER-v2.md`), but changes what is planned. v2 plans one stage at a time on whatever space is left.
> v3 designs **one end-state starter base first** (through automated red + green science), then derives every stage
> as a subset or an in-place upgrade of it. Requirements M1-M7 and acceptance criteria B1-B6 are binding. The
> numbers in section 3 come from the installed 2.1 base data and become constants with tests. Section 15 scopes the
> work into at most three implementation rounds. The core (M1-M4) is in rounds A and B and is not optional.

## 1. Why

v0.4.1 builds fast first plates (1.5 min measured on a real-save copy), but three reviewers judged the base itself:

- **Islands.** Every patch gets its own burner-pair island (drill -> furnace -> chest). Nothing connects iron, copper,
  coal and power. The helper carries coal and plates 100-250 tiles, and without it the burners run dry in about
  20 minutes.
- **No path to automation.** Red science is never automated (tests put packs into labs). Stage 3 is a text estimate,
  no electric drill is ever placed, and the stage-2 boiler is hand-fed.
- **Stages are not a plan.** Each stage is placed on leftover space. The "Long-term" variant is a bigger Quick start
  with no answer to where iron and copper meet. Later stages could collide with earlier ones, and v2 only patches
  that with `reserve` boxes.
- **Inconsistent review.** The rebuild variant appears only with "tidy" wording (a 0.5 min threshold instead of
  3 min / 10%).
- **Cards are walls of text.** Stage-2 ETAs are unverified, and offline vs live ETAs differ (1.1 vs 1.5 min).

The owner's requirement for this pass, in his words: *a base builder that starts with the burner miners knowing it
needs to upgrade to electric and ultimately put down electric poles too; same with red science: if you know you have
to automate gears and green chips you account for that in your building even if you haven't unlocked it yet; the
builder should be pre-planning on a plan.*

## 2. Requirements (binding)

| # | Requirement | In short |
|---|---|---|
| M1 | Master plan first | One end-state layout (automated red + green science, optional small mall) is designed before anything is placed. Stages are derived from it. Nothing from an early stage stands where a later stage needs space, unless it is a planned in-place swap. |
| M2 | Burner -> electric in place | Mining fields use the electric-drill grid from day one. Burner drills sit in electric slots and drop onto the lanes the electric drills will use. The electric stage removes the burners and places electric drills in the same slots. Pole positions, belt corridors and the steam power block (sized for the end state, with room to grow) are reserved from the start. |
| M3 | Science pre-planned | Gear + red block, cable/circuit + inserter + belt + green block, and labs are laid out while still locked. They activate when the save's real research state unlocks them. |
| M4 | Connected, self-running | In the end state, iron, copper, coal, power and science are linked by belts and poles. Coal reaches every burner and boiler by belt, so the base runs with the helper idle. Plate lines meet at the science blocks. Belt runs are proportional; far-apart ore is reported with the cheapest connection. |
| M5 | Stages are a roadmap | The whole roadmap is shown: per stage a bill, ETA, gate (research), what it unlocks and what it replaces. The current stage is ghosted and later stages are drawn as faint reserved outlines. When a gate opens, the next stage is offered automatically. |
| M6 | Keep v2's good parts | State-aware bill/ETA, rebuild review with one threshold that is always run, Reject cancels marks, player ghosts never touched. Short cards. |
| M7 | Meaningful variants | Each variant is a full master plan, and the variants differ in topology or reuse, not only in size. |

### 2.1 What the 2.x tech tree changes in the owner's stage list

The owner's example order ("S2 steam power + electric drill swap + poles, S3 red science") is impossible in 2.1:
**electric-mining-drill costs 25 red packs** and **assembling-machine-1 is unlocked by Automation (10 red packs)**.
Red packs must therefore exist before either. The roadmap is ordered by research gates (section 7), and the card says
why ("Electric drills need research: 25 red packs. They come right after red science is automated.").

| Owner's stage | v3 stage(s) |
|---|---|
| S0 burner iron + coal | S0 (same) |
| S1 burner copper + stone furnaces | S1 (same) |
| S2 steam power + electric swap + poles | S2 power + poles + labs (the swap moves to S4) |
| S3 automated red science | S3 (same) |
| S4 automated green science | S5 (S4 is the electric swap) |

## 3. Game constants (2.1 base data, normal quality)

Checked against `data/base` of 2.1.23: technology, recipe, entity, mining-drill, trees and freeplay files. The
planner reads recipe and tech costs from `GameData` at runtime. The table is for this document and the tests.

| Thing | Value |
|---|---|
| Freeplay start | 8 iron plates, 1 wood, 1 burner drill, 1 stone furnace (+8 iron plates in crash debris) |
| Unlocked at start | burner drill, stone furnace, gear, transport belt, burner inserter, wooden/iron chest |
| Burner drill | 2x2, 0.25 ore/s, 150 kW = 0.0375 coal/s (one 50-coal stack lasts ~22 min), mines only its own 2x2. Drop vector (-0.35, -1.3) when facing north |
| Electric drill | 3x3, 0.5 ore/s, 90 kW, mines 5x5. Drop vector (0, -1.85). Cost 3 circuits + 5 gears + 10 plates = 23 Fe + 4.5 Cu. Same fast-replace group as the burner drill but a different size and no `next_upgrade`, so the swap is remove + place |
| Stone furnace | 2x2, 0.3125 plates/s, 90 kW = 0.0225 coal/s, 5 stone |
| Burner inserter / inserter | ~0.78 / ~0.84 items/s chest to chest. Same fast-replace group, so they swap in place. A burner inserter refuels itself from fuel it carries, but it cannot take fuel from the entity it picks from (no `allow_burner_leech`) |
| Belt | 15 items/s, 7.5 per lane, 1.5 Fe per tile |
| Small pole | wire reach 7.5, supply 5x5. Recipe 1 wood + 2 cables -> 2 poles. A tree gives 4 wood |
| Boiler | 3x2, 1.8 MW, 0.45 coal/s at full load, 60 water -> 60 steam/s |
| Steam engine | 3x5, 900 kW, 30 steam/s |
| Offshore pump | 1200 water/s, no power |
| Pump : boiler : engine | **1 : 20 : 40.** This corrects PLANNER-v2 section 3, which said 1:200:400 |
| AM1 / lab | 3x3, speed 0.5, 75 kW / 3x3, speed 1, 60 kW |
| Recipes (time at speed 1) | gear 0.5 s; cable 0.5 s -> 2; circuit 0.5 s (3 cable + 1 Fe); inserter 0.5 s; belt 0.5 s -> 2; red 5 s (1 Cu + 1 gear); green 6 s (1 inserter + 1 belt) |

Tech chain (all verified in `technology.lua`):

| Tech | Cost | Unlocks |
|---|---|---|
| steam-power | trigger: craft 50 iron plates (furnace output counts, verified) | pipe, offshore pump, boiler, steam engine |
| electronics | trigger: craft 10 copper plates | cable, circuit, inserter, lab, small pole |
| automation-science-pack | trigger: craft 1 lab. It must be a **player's** craft; a lab crafted by the helper does not count (verified) | red pack |
| automation | 10 red x 10 s | AM1, long-handed inserter |
| electric-mining-drill | 25 red x 10 s | electric drill |
| logistics | 20 red x 15 s | underground belt, splitter (optional in the starter) |
| logistic-science-pack | 75 red x 5 s | green pack |

Medium poles (steel + green research) and splitters (logistics) are not assumed: v3 layouts need neither.

## 4. Master-plan data model (M1)

New module `factorio_claude/planner/master.py`.

```python
@dataclass
class Zone:                      # a reserved rectangle of the end state
    id: str                      # "mine_iron", "smelt_iron", "power", "red", "green", "labs", "mall",
    kind: str                    # "corr_coal_3", "trunk_iron", ...
    rect: tuple                  # (x1, y1, x2, y2) tiles, half-open
    first_stage: int             # the first stage that places anything inside it
    label: str                   # "Red science (S3, after Automation)"

@dataclass
class Swap:                      # a planned in-place change between two stages
    slot: str                    # "mine_iron/w3" (field, side, index) or a tile key
    kind: str                    # "upgrade" | "replace" | "retire"
    stage: int                   # the stage that performs it
    out: list[str]               # entity ids removed (salvaged)
    into: list[str]              # entity ids placed (empty for "retire")

@dataclass
class StageSpec:
    index: int                   # 0..5
    key: str                     # "burner_iron", "burner_copper", "power_labs", "red", "electric", "green"
    title: str                   # "Automate red science"
    gate: list[str]              # techs that must be researched before it is offered
    after: int | None            # stage that must be complete first
    unlocks: list[str]           # research it enables or speeds up, for the roadmap line
    research: list[dict]         # research steps it queues: {tech, packs, source: "hand"|"assemblers"}

@dataclass
class MasterPlan:
    id: str                      # "m-<hash>"; stage plans are "<id>-s<k>"
    variant: str                 # "hub" | "onsite" | "reuse"
    hub: tuple | None            # meeting point of the plate lines (section 9)
    zones: list[Zone]
    ents: list[PlannedEntity]    # the end state plus every entity that is ever placed
    swaps: list[Swap]
    stages: list[StageSpec]
    rates: dict                  # end-state rates per item/s (rates.solve) and per stage
    power: dict                  # {peak_kw, boilers_end, engines_end, boilers_reserved}
    routes: list[dict]           # {what, from, to, tiles, fe} for every belt run
    research_path: list[dict]    # ordered techs with gate stage, packs, ETA
    removes: list[dict]          # clusters of the player's builds removed, with stage and reason (section 11)
```

Every `PlannedEntity` gets these `extra` fields:

- `stage`: the first stage in which it exists.
- `until`: the stage that removes it, or `None` if it lasts to the end state.
- `slot`: the swap slot it belongs to, if any.
- `zone`: the id of the zone it sits in.

Rules:

- `stage_entities(mp, k)` = entities with `stage <= k < (until or inf)`. This is exactly the base after stage k is
  built.
- `new_in(mp, k)` = entities with `stage == k`. These become the ghosts of stage k.
- `removed_in(mp, k)` = entities with `until == k`. These become stage k's `removes` rows (cluster `"upgrade"`).
- Every entity with `until` set must appear in exactly one `Swap.out`. Every `Swap.into` entity has
  `stage == Swap.stage`.

The master plan is saved as `plans/<id>.master.json` next to the stage plans. Each stage plan stays an ordinary
`Plan`, so `plan_preview`, `plan_apply`, the validator, quality gates, the agenda and the mod keep working. A stage
plan carries `stats.master = {id, stage, stages: [...]}`, `stats.reserved` (section 12.3) and `stats.gate`.

## 5. End-state design (M1-M4)

`design_end_state(pl, variant) -> MasterPlan` runs once per variant, in this order. Any failed step makes the variant
unavailable, with a reason shown on the "checked, not offered" line.

1. **Size.** `rates.solve({automation-science-pack: 0.5/s, logistic-science-pack: 0.5/s}, assume_research=True)`
   gives science needs of 3.75 Fe/s and 1.25 Cu/s. Add building headroom: iron = one full ore lane (24 furnaces, 7.5/s),
   copper 8-12 furnaces. That means 15 electric iron slots (8 is the science minimum), 6 copper, 4-5 coal and 2 stone,
   plus 6 red AM1 and 10 green-block AM1 (section 6.6) and up to 10 labs.
2. **Hub and connections** (section 9): choose where smelting happens and where the plate lines meet, by weighted
   walk distance.
3. **Reserve zones** on one occupancy grid (`world.grid`), in this order, each placed at its lowest-cost free spot:
   mining fields at the patch edge nearest the hub, smelting columns, red block, green block, labs, power block
   (section 6.4, at the shore that minimises coal belt + pipe + pole cost), the mall (optional, reserved only), then
   belt trunks (`router.route_belt` with `max_len`) and pole corridors. Clusters of the player's builds inside a
   zone are handled by section 11.
4. **Fill zones** with the block generators of section 6, at end-state size, plus every early-stage entity in its
   final slot (burner drills, burner inserters, corridor coal belts) tagged with `stage` and `until`.
5. **Validate the end state** with `validate(..., assume_research=True)`. Then validate every stage subset
   (section 7.3) and run `check_monotone` (section 7.4). A master plan that fails any of these is not offered.

### 5.1 Expansion room

Every field leaves one free double-row pitch (8 tiles) beyond its last row, inside its zone. The power block reserves
10 boilers / 20 engines, but only 3 / 6 are built by S5. The smelting column has one spare 4-tile segment at its end.
`expand_room` in the score (v2 section 9.3) is measured on these reserves.

## 6. Blocks (in `blocks.py`)

Each block returns entities together with slot ids, so the swap list and the pole positions are data, not geometry
guessed later. All geometry is described for one orientation; the generators rotate it.

### 6.1 `mining_field(patch, n_slots, *, rows)`: one lattice, two phases (M2)

```
 corridor  west slot    belt  east slot   corridor
   x=b-4   b-3 b-2 b-1   b    b+1 b+2 b+3   b+4
 y   C     BI  [BD BD]   v    [BD BD] BI    C      <- slot rows y..y+2 (burner phase)
 y+1 C     .   [BD BD]   v    [BD BD] .     C
 y+2 C     .   .   .     v    .   .   .     C
 y+3 C     .   .   P     v    P?  .   .     C      <- gap row: small pole at b-1 (or b+1, alternating)
 y+4 ...   next slot pair (period 7 along the belt)
```

- **Electric phase (end state).** One ore belt at x=b. West slots cover x b-3..b-1 and face east; east slots cover
  b+1..b+3 and face west. The period along the belt is 7 (slot 3 + slot 3 + 1 gap row). One small pole per gap row
  at the belt-adjacent tile powers 4 drills, and the 7-tile pole spacing is inside the 7.5 wire reach. West drills
  drop on the west lane, east drills on the east lane, with at most 15 drills per lane. Double rows sit at a pitch of
  8: 7 tiles of field plus a 1-tile corridor at b-4 / b+4. Corridor ore is still mined by the 5x5 areas.
- **Burner phase.** In each slot the 2x2 burner drill takes the two belt-side columns of the top two rows
  (b-2..b-1, y..y+1 for a west slot) and faces the belt. Its drop point is (b + 0.3, y + 0.65), on the west lane of
  the same belt tile column the electric drill drops onto (b + 0.35), so the lane loading matches. A burner inserter
  at (b-3, y) picks coal from the corridor belt at b-4 and drops it into the drill (an inserter feeding a drill moves
  only fuel). It refuels itself from the coal it carries.
- **Corridors.** One coal corridor serves the facing slots of two neighbouring double rows, so K double rows need
  K+1 corridors. Without splitters, one coal belt snakes through all corridors with U-turns at the far field edge.
- **Coal field.** Uses the same lattice. Its burner drills drop coal on its ore belt, which runs first through its own
  corridors (self-feed; net 0.2125 coal/s per burner coal drill), then out to the other consumers (section 8).
- **Ore left for later.** A burner mines 4 of the slot's 9 tiles. The rest of the ore stays for the electric drill.
- **Swap per slot (S4).** `Swap(kind="upgrade", out=[burner drill, burner inserter], into=[electric drill])`. The
  electric drill's 3x3 covers exactly the tiles the burner drill and burner inserter used. Corridor belts get
  `Swap(kind="retire")` in S4. Gap-row poles are `stage=4` (or `2` where S2 already powers something nearby). Their
  tiles are reserved from S0, and nothing else may use them.
- **Stage tags.** Slots `0..n_burner-1` hold burner drills from S0 (iron, coal) or S1 (copper, stone).
  `n_burner = ceil(furnaces_supplied / 0.8)`, capped by stock (v2 section 7 sizing). The remaining slots stay empty
  until S4. Electric slots never exceed the reserved count.

### 6.2 `smelter_column(n_furnaces, *, mirrored=True)`

```
plate | out-ins | furnace(2) | in-ins | ORE/COAL | in-ins | furnace(2) | out-ins | plate      (11 wide)
```

- The field belt side-loads onto the middle belt's ore lane, so both field lanes merge into one lane. The coal belt
  side-loads onto the other lane. 7.5 ore/s per lane feeds 24 furnaces (12 per side, 24 tiles long). Coal use is
  0.54/s per 24 furnaces.
- **Input inserters are burner inserters forever.** They refuel from the coal they move and survive a blackout.
- **Output inserters:** burner inserters in S0/S1, pre-fuelled with coal (about 33-40 swings per coal at
  0.31 plates/s per furnace, so 20 coal lasts more than 30 min; checked live in round A). From S2 they are
  fast-replaced by electric inserters: `Swap(kind="replace")`, same tile. A pole on the out-inserter column every
  4 tiles (2 furnaces) is reserved from S0 and placed in S2.
- **Furnaces are stage-tagged in pairs from the head of the column.** The S0 iron column holds
  `0.8 x n_burner_iron` furnaces; S4 extends it toward 24 in the same column (no move).

### 6.3 Plate lines

Each smelting column ends in a plate belt (one per side, merged by side-loading into one belt, both lanes). Iron and
copper plate belts run to the science area (section 9.3). Before S3 they end in a chest at the science area, so the
helper and the player can take plates (a "plate buffer"). In S3 the chest is retired and the belt continues into the
red block.

### 6.4 `power_block(boilers_end, boilers_reserved)` (M2, M4)

- The offshore pump sits on the shore. A water pipe runs to a row of boilers whose water ports chain sideways. Each
  boiler feeds 2 engines in series (3x5 + 3x5) on its steam output. After every second boiler column comes a 1-tile
  gap with a pipe at the boiler row.
- One small pole per gap at the engine1/engine2 joint powers 4 engines, at a pitch of 7.
- A coal belt runs along the boiler row with one burner inserter per boiler. There are no electric inserters, so the
  block can black-start.
- Footprint: 7 tiles per 2 boilers, about 14 deep (belt 1, inserter 1, boiler 2, engines 10). The reserve is
  10 boilers / 20 engines (about 35x15 plus the pump). Build 1-2 boilers / 2-4 engines in S2 and reach 3 / 6 by S4-S5.
- Peak load at S5: 15 drills 1.35 MW, about 16 AM1 1.3 MW, 10 labs 0.6 MW, inserters about 0.25 MW, so 3.4-3.9 MW.
  `boilers = ceil(peak_kw x 1.1 / 1800)`. The quality gate `OVERSIZED_POWER` counts built boilers, not reserved ones.

### 6.5 `red_block(n_red=5, n_gear=1)` (M3)

```
mixed input belt (Cu lane + gear lane) | ins | AM1 | ins | science belt (red lane)        (7 wide)
```

- At full speed the ratio is 10 red AM1 to 1 gear AM1 (red 10 s/pack, gear 1 s in AM1). The starter builds 5:1
  (0.5 red/s, gear AM1 half idle) and reserves room for 10:1.
- The gear AM1 takes iron from the iron plate belt and puts gears onto the gear lane. Copper comes in on the other
  lane from the copper plate belt. Poles sit on free tiles of both inserter columns, 1 per 2 AM per column.
- Plates needed: 1.0 Fe/s + 0.5 Cu/s.

### 6.6 `green_block()` (M3, 0.5 green/s)

- 6 green AM1 (12 s each), 1 inserter AM1, 1 belt AM1, 1 gear AM1 (0.75 gear/s), 1 circuit AM1 and 1 cable AM1
  (1.5 cable/s, inserting directly into the circuit AM1). The inserter and belt AMs output onto one mixed belt
  (inserter lane + belt lane) that feeds the 6-AM row, using the same 7-wide geometry as the red block.
- The 3 cable : 2 circuit interleave (cable-circuit-cable-circuit-cable) is only needed above about 0.67 circuits/s.
  The zone reserves room for it. The S5 build places 1:1.
- Plates needed: 2.75 Fe/s + 0.75 Cu/s. The zone is about 12x35.

### 6.7 `lab_rows(n_end=10)` and the optional mall

- Lab rows are 9 wide: lab | inserter | science belt (red lane, green lane) | inserter | lab, 5 labs per side,
  15 tiles long. 2-5 labs are built in S2 (or the player's idle labs are reused, section 11). The rest are reserved.
- Labs needed = pack rate x unit time = 0.5/s x 10-15 s, so 5-8.
- The **mall** (gears, belts, inserters, burner drills, furnaces, poles from the plate belts) is a reserved zone
  only. Building it is out of scope for v3 and can be offered later as its own stage.

## 7. Stages: derivation, gating, validation (M1, M3, M5)

### 7.1 Stage templates

| # | Key / title | Places (tags) | Removes (swaps) | Gate (all researched) | After | Research it queues |
|---|---|---|---|---|---|---|
| S0 | `burner_iron` "Burner iron + coal" | iron and coal burner slots, iron smelter head (burner in/out inserters), coal corridors and coal belt to the iron column and field, plate buffer chest, stone burner slots if stone is short | player clusters scheduled for S0 (section 11) | none | - | (steam-power trigger fires from plates) |
| S1 | `burner_copper` "Burner copper" | copper burner slots + copper smelter head, coal belt branch, plate buffer | - | none | S0 | (electronics trigger) |
| S2 | `power_labs` "Steam power + labs" | pump, 1-2 boilers, 2-4 engines, boiler coal belt, pole trunk from the power block to the smelter columns and labs, first labs | out-inserters of the smelter columns -> electric (`replace`) | steam-power, electronics | S1 | automation-science-pack trigger (player crafts 1 lab, if locked); automation with 10 hand-crafted packs (D1) |
| S3 | `red` "Automate red science" | red block, plate belts extended into it, science belt to the labs, its poles | plate buffer chests (`retire`) | automation | S2 | electric-mining-drill (25), then logistic-science-pack (75); logistics (20) optional |
| S4 | `electric` "Electric drills" | electric drills in every burner slot + empty slots up to the end count, gap-row poles, boilers 3 / engines 6, furnaces extended toward the end count | burner drills + their burner inserters (`upgrade`), corridor coal belts (`retire`) | electric-mining-drill | S3 | - |
| S5 | `green` "Automate green science" | green block, green lane to the labs, labs up to the end count | - | logistic-science-pack | S3 | (next: whatever the player picks) |

- **Ordering.** The order above holds whenever the gates are the vanilla ones. `derive_stages` sorts stages by
  (gate ETA, template order). A save where electric-mining-drill is already researched but automation is not gets
  S4 before S3, and the card says so.
- **Skipping.** A stage whose entities already exist (e.g. a save with copper smelting that the reuse variant keeps)
  becomes a "done" row on the roadmap. A stage whose gate is already met and whose predecessor is done is "ready".
  The first stage that is not done is "now".
- **Connector stages.** When a trunk (section 9.2) is longer than 120 tiles, it is split into its own stage between
  the stages that need it ("S1b Connect the coal belt to iron, 143 iron"). Until then the helper's round bridges the
  gap. The card says this explicitly ("until S1b the helper carries coal to the iron field, ~15% of its time"). The
  self-running check (section 8.2) only counts from the stage that completes the connection.

### 7.2 Research gating from the live tech state

- `base_state.research` and `export_data` already return `done`. Round A adds `recipes_enabled` (names of enabled
  recipes of the force) and the tech's trigger type. The planner never assumes a tech: `stage.gate` uses
  `force.technologies[t].researched`, and the bill of every stage is checked against `recipes_enabled` plus the
  recipes its gate unlocks. Fixtures without the field fall back to `data.json`.
- **Trigger techs** are shown as research steps with their trigger ("crafts 50 iron plates: the S0 furnaces do this
  in ~3 min"). automation-science-pack needs the **player** to craft one lab. If it is locked and the player is not
  connected, S2's card says "craft one lab by hand to unlock red science" and the stage stays "waiting on you".
- **Research ETA** = `packs x unit_time / labs_working` + pack production time (hand: 5 s per pack + gears;
  assemblers: rate from the red block). The roadmap shows each gate's ETA from now.
- **Queueing research** is a normal player action (the research screen), not a cheat. On Approve of a stage, the mod
  adds that stage's `research` entries to the force's queue if the queue is empty or the player allows it (setting
  `claude-helper-queue-research`, default on). The card says what it queues.

### 7.3 Stage validation

For every k:

- `validate(stage_entities(mp, k) + existing builds kept at k, world_without(removes_upto_k), assume_research=gates_upto_k)`
  must pass with no errors.
- Machines whose inputs only arrive in a later stage are exempt from "starved" checks only if they are `stage > k`,
  which never happens by construction. So a stage-k base must be **closed**: every belt ends in a consumer or a
  buffer chest, every burner has a fuel path (section 8.2), and every electric entity has a pole path to a generator
  of stage <= k.

**Validator fix (round A, first commit).** In `validate.py` `_Validator._items` (the `existing_link` branch around
lines 689-695), the drop target of an existing inserter is counted even when it has no item inventory. On the
real-save copy an existing burner inserter "reaches into a planned small pole", and every variant loses its stage 2.
Only targets with an item inventory (containers, machines, belts, ground-item targets for drills) should count.
This needs a regression test.

### 7.4 `check_monotone(mp)`: the M1 check, in code (B1, B4)

Hard gate `STAGE_CONFLICT` in `quality.py`. For every pair of stages j < k:

1. Footprints of `stage_entities(mp, j)` and `new_in(mp, k)` are disjoint, except where the stage-j entity is in a
   `Swap.out` with `Swap.stage == k` (or earlier).
2. Every entity with `until = k` is in a swap of stage k, and the swap kind is allowed for that entity type:
   `upgrade` only burner drill and its burner inserter -> electric drill in the same slot; `replace` only burner
   inserter -> inserter on the same tile; `retire` only belts in corridors/buffer chests/coal belts to fields.
   Anything else (a furnace, a boiler, a lab, a pole) may never be removed by the master plan.
   **Round 3 (strict, as built):** every removal is in place. `upgrade` as above; `replace` burner inserter ->
   inserter, or a plate-belt tile -> one half of a splitter (on-site S5); `retire` only a temporary chest (plate
   buffer, the connector stage's coal chest) and its inserter, and only where a belt of the same stage takes that very
   tile. A belt, corridor, coal leg, drill, furnace, boiler, lab or pole is never taken up again (section 18).
3. No entity of stage j lies inside a zone whose `first_stage > j`, unless it persists into that stage (it is part of
   the zone's own end state) or it is a whitelisted swap.
4. Reserved pole tiles, gap rows and corridor tiles are free of everything not tagged for them.
5. The number of `upgrade` swaps equals the number of burner slots ever placed. The number of electric drills in
   S4's `into` equals that count; the extra electric drills in empty slots are counted separately. These counts are
   written to `stats.swap_counts` and are the B4 reference.

A unit test plants a conflict (a stage-1 furnace in the stage-3 red zone) and expects `STAGE_CONFLICT`.

## 8. Coal, power and self-running (M4)

### 8.1 Coal distribution

- The coal field's belt runs: own corridors -> iron field corridors -> iron smelter coal lane -> copper field
  corridors -> copper smelter coal lane -> boiler row. Without splitters, branches are side-loads or the lane is
  passed through ("pass-through" coal lane on the smelter's middle belt, ending in the next consumer). The router
  builds this as one chain ordered by distance (`route_belt` per leg, `max_len` caps).
- **Coal budget per stage** = sum of burner drills x 0.0375 + furnaces x 0.0225 + boilers x 0.45 x load factor
  (load from the stage's consumers / built capacity) + inserters (negligible, counted at 0.001/s). Supply = coal
  burner slots x 0.25 (gross, their own burn included in demand), or electric coal drills x 0.5 from S4.
  Burner-phase peak is about 2 coal/s (16 burner drills 0.6, 20 furnaces 0.45, 2 boilers 0.9), so about 10 burner
  coal slots. After S4, 4-5 electric coal drills cover it.
- The number of coal slots per stage = `ceil(demand x 1.15 / per_slot)`. Coal slots are tagged into the earliest
  stage whose demand needs them.
- **Order matters.** A belt consumer that is first on the chain can starve the rest when supply is short. The budget
  check therefore runs per leg, not only in total, and the agenda simulation (8.3) is the judge.

### 8.2 Self-running check (offline, every stage >= the connection stage)

`selfrun(mp, k)` returns `{ok, issues, rates}`:

1. Every burner entity of stage k has a fuel path: a burner inserter (or the furnace input inserter) picking from a
   belt that carries coal, a burner coal drill dropping onto its own corridor chain, or (S0/S1 smelter output
   inserters only) pre-fuel with a lifetime of at least 60 min at the planned rate. The helper round is **not** a
   fuel path. A stage that relies on the round is marked `needs_helper` and is not "self-running".
2. Coal budget per leg >= demand x 1.1.
3. Power: built generator capacity >= peak consumer load of stage k x 1.1, and every consumer has a pole path.
4. Lanes: ore per lane <= 7.5/s, smelter input <= lane capacity, no plate belt ends without a consumer or buffer.
5. `agenda.simulate` with the helper idle for 30 min after the stage completes: plates/min, red/min and power
   satisfaction at the start and end, any machine that stops for fuel. `ok` means end rates >= 95% of start rates
   and no burner is out of fuel.

Gate `NOT_SELF_RUNNING` (hard for S2 and later, soft for S0/S1 while a connector stage is pending).

### 8.3 Agenda extensions (ETA)

`agenda.py` gains:

- self-fed units (on a coal chain) that do not join the round;
- belts between sites (items arrive after length / 1.875 tiles/s);
- `research` events (time from packs and labs);
- `assembler` units (recipe rates at speed 0.5, input-limited by belt supply);
- swap steps (remove = mining time per entity, salvage to stock, then place);
- tree chopping for pole wood (1 wood per 2 poles, 4 wood per tree, ~0.5 s per tree plus walk to the nearest tree
  cluster).

Each stage gets `eta = {start, first_output, complete, gate_open}` relative to now and to the previous stage.

## 9. Hub vs on-site, and where the plate lines meet (M4)

### 9.1 Facts that decide it

- Ore to plate is 1:1, so belting ore to a hub moves the same volume as belting plates from on-site smelting.
- Burner-phase fields need coal **at the field** until S4. Furnaces need coal for good.
- Power is close to free to move (about 7 wood + 14 cables per 100 tiles of poles, against 150 Fe for 100 belt
  tiles).

### 9.2 Algorithm

For each candidate smelting arrangement A (central: all smelting at one site s; on-site: each ore smelted at its
patch; mixed: per ore), compute the belt tiles it needs with real walk distance `D` (`placer.walk_distance` over
the grid, water and unremovable builds blocked):

```
belt(A) = sum over ores r of:   D(field_r -> smelter_r)            # ore belt (0 if on-site)
                              + D(smelter_r -> meet)               # plate belt
                              + D(coal -> field_r)   [until S4]    # burner-phase coal to the field
                              + D(coal -> smelter_r)               # furnace coal
        + D(coal -> boilers) + D(meet -> labs)
cost(A) = 1.5 x belt(A) + 0.07 x pole_tiles(A) + clutter(A) + 0.5 x ore_tiles_covered(A)
```

- A coal leg that is retired in S4 counts at 0.6, since its belts are salvaged.
- Legs that share a corridor count once.
- The weighted centroid (iron 3, copper 1, coal 1.5, stone 0.2) seeds the search for `s` and the meeting point.
  Up to 6 candidates are then evaluated with the full cost.
- **Meeting point.** Science (red, green, labs) sits between the iron and copper plate belt ends, offset toward
  iron (iron carries 3x the volume). Both plate belts enter the red block from one side.
- **Caps.** Any single run over 120 tiles becomes a connector stage (7.1) and a card warning with its iron cost. A
  total starter belt over 400 tiles gets the `FAR_APART` warning: "Ore is far apart: the cheapest connection is
  N belt tiles (M iron). Iron is X tiles from coal." The plan is still offered (M4 says to report it, not hide it).
  Over 900 tiles, the variant is offered only as "connect later" with the S0-S2 helper round shown as the bridge.
- Stone is a build input, not a running input. It may stay as burner drills into a chest on the stone patch, emptied
  by the helper, and is exempt from M4.

### 9.3 Expected outcomes

- **Real-save copy** (about 6 h in): coal under the player, copper about 30 tiles from coal, a lake about 25 tiles
  from coal, iron about 95 tiles from coal, stone about 110. The hub goes next to coal/copper/lake. One iron ore
  trunk of about 95-110 tiles is needed, plus a burner-phase coal leg to the iron field of the same length (retired
  in S4), or the reuse variant's existing iron trunk.
- **seed_4004** (sprawl): iron, copper and coal are 180-300 tiles from each other. `FAR_APART` is expected. The hub is
  at iron (3x volume), with coal and copper trunks as connector stages and the helper bridging until they are built.
- **Seeds 1001/2002/3003**: patches within 30-80 tiles give a compact central layout with no connector stage.

**As built (round 3, `testenv/private/r3/offline.py`; the real-copy capture made by `testenv/capture_live.py`).**
The expectation above about connector stages was wrong: a coal leg longer than 120 tiles exists on most maps.

| Map | Recommended | Belt tiles (other variant) | Connector stages | Notes |
|---|---|---|---|---|
| real-save copy | hub `mhub-80c9c1` | 382 (on-site 517) | S0b (coal to iron, 166 tiles) | the hub sits between iron and copper; iron trunk 88, copper trunk 65 |
| seed_4004 | on-site | 972 (hub 1060) | S0b, S1b | `FAR_APART`: one patch of each ore in range, 150-300 tiles apart |
| seed_1001 | hub | 380 (on-site 455) | S1b | |
| seed_2002 | on-site | 411 (hub 351) | S0b, S1b | on-site: red science 20 min sooner |
| seed_3003 | hub | 452 (on-site 578) | S0b, S1b | |

## 10. Variants (M7)

Each variant is a complete master plan with its own roadmap. At most 3 cards; the recommended one is the lowest
score (v2 section 9.3, with `eta.first_plates` of S0 plus the S3 "red automated" ETA in place of `eta.target`).

| Key | Title | Topology | Reuse | Typical trade-off |
|---|---|---|---|---|
| `hub` | Central hub | smelting for iron and copper in one block with the science; each ore field sends ore + coal on ONE belt (round 3) | removes clutter in zones (section 11) | fewest belts; the first plates wait for the iron field's trunk |
| `onsite` | Smelt at each patch | smelter column at each patch edge; one plate belt per metal to the science, split for green at S5 (round 3) | same | first plates soonest; coal must reach each patch |
| `reuse` | Build on what you have | the arrangement that keeps the most of the player's working or repairable clusters (trunks, furnace rows, labs, pole lines), repaired and wired into the master plan | maximal: clusters become zone content (`extra.existing`) | cheapest bill and fastest early stages, less regular layout |

- Old keys stay as aliases with the justification recorded in the changelog: `quick`/`compact` -> `onsite`,
  `balanced`/`longterm`/`expandable` -> `hub`. `rebuild` is no longer a variant: every variant runs the rebuild
  review (section 11).
- Two variants whose master plans have the same topology and differ by less than 10% in belt tiles and S3 ETA are
  merged; the card says what the other one would have been.
- `reuse` is offered only when it keeps at least 10% of the player's non-belt entities or a trunk of 40+ tiles.

## 11. Rebuild review (M6)

- **Always run, one threshold.** Remove the `tidy_words` path (`REMOVE_GAIN_ASKED`). A cluster is proposed for
  removal when `gain >= max(REMOVE_GAIN_MIN=3 min, REMOVE_GAIN_FRAC=10% of the keep score)`, x2 if working. The
  caps (50% of non-belt entities, 50% of belt tiles) are lifted only by explicit rebuild words ("rebuild from
  scratch").
- **Zones first.** A cluster of the player's that overlaps a reserved zone is resolved in this order:
  1. Move the zone if a placement within +10% of the zone's cost exists.
  2. Adopt the cluster into the zone if it matches its content and slot grid (`reuse`).
  3. Otherwise propose its removal **in the stage that first needs the space**, with the reason "stands where the red
     block goes (S3)".

  Zone removals are justified by M1 and do not need the gain threshold, but they count toward the caps and are
  listed on the card.
- Clusters outside every zone go through the normal gain test. Labs are never removal candidates (they are moved
  into the lab zone only if powering them where they stand is impossible, and then with a reason).
- Player ghosts and other plans' ghosts are obstacles, never touched. Reject / Dismiss / drop cancel every remaining
  mark (unchanged from v2).
- Every removal row carries `{cluster, stage, reason}`. The removal happens in that stage's plan, not all at once in
  S0.

## 12. Presentation (M5, M6)

### 12.1 Card (Plans tab, max 4 lines + 1 warning line)

```
Central hub - everything within 40 tiles of the coal; iron comes on one 102-tile belt.
Now S0: first plates ~6 min | 60 iron/min | needs 140 Fe, 60 stone (salvage covers) | belts 118
Roadmap: S0 iron+coal (now) > S1 copper > S2 power+labs > S3 red [Automation] > S4 electric [EMD] > S5 green [LSP]
Removes 7 idle furnaces in S3 (stand where the red block goes). Keeps your 4 labs.
[red] Iron is 95 tiles from coal: S1b coal belt to iron (143 Fe); the helper carries coal until then.
```

- Line 1: title - 1-line trade-off.
- Line 2: the current stage and 3-4 numbers.
- Line 3: the roadmap with gates.
- Line 4: removals/reuse.
- Line 5 (only if any): the loudest warning.

Details (full bill, per-site lists, research path) live in a "Details" expander, not on the card.

### 12.2 Roadmap row (Plans tab, per master)

One row per stage: `S3 Automate red science | waiting: Automation (10 packs, ~2 min) | bill 160 Fe 75 Cu | ~9 min
build | replaces: plate chests`. Status icons: done / now / ready / waiting (gate) / later. Clicking a row previews
that stage (ghosts for "now"/"ready", outlines for the rest).

### 12.3 Reserved-zone rendering (in game, rendering only)

- `stats.reserved = [{id, kind, rect, stage, label}]` (at most 64 zones) is sent with every stage plan.
- `plans.lua` draws each zone as a filled rectangle (alpha 0.08, colour per kind), a 1 px outline and a label
  "S3 Red science", in game view and chart view, with the legend entry "Reserved: later stages". The **next** stage's
  entities are also drawn as faint per-entity outlines (cap 400). No ghosts are placed for future stages.
- The render objects are destroyed on Reject, Dismiss, drop and when the master is replaced. A "Show roadmap"
  checkbox toggles them.
- `testenv/render_plan.py` gains `render_roadmap(world, master, out)`: one panel for the end state (all zones, entity
  colours by stage) plus one panel per stage (that stage solid, earlier stages grey, later zones as outlines, swaps
  marked with a yellow X).

### 12.4 Next-stage auto offers

- Mod: `storage.masters[id] = {stages = [{plan_id, gate, after, status}], current}`. Stage plans after the current
  one are stored with `status = "future"` (not ghosted).
- `plans.check_ready(force)` runs from a new `on_research_finished` handler and from `on_built_entity` /
  `script_raised_built` for a plan's ghosts (stage completion = all of the plan's ghosts built and all removals done).
  When a future stage's gate techs are researched and its `after` stage is complete, its status becomes `ready`.
  This triggers:
  - a Plans-tab banner "Stage 3 ready: automate red science" with Preview / Approve;
  - a one-line player print;
  - a companion chat event (`stage_ready {master, stage}`) so Claude can say it.
- Python: `plan_next_stage(master_id)` re-derives the stage from the **current** state. The geometry stays the
  master's; the bill, ETA, takes and removals are recomputed; the subset is re-validated against the current world.
  If the player has built inside the stage's zone, the review of section 11 runs for that zone only and the offer
  says so.
- New MCP tools: `plan_roadmap(master_id)` (the brief of 12.5) and `plan_next_stage(master_id)`.
  `plan_starter_base` returns masters (one per variant) with their "now" stage plan.

### 12.5 Brief for Claude

```
{master: id, variant, title, tradeoff, recommended, topology, far_apart,
 roadmap: [{stage, key, title, status, gate: [techs], gate_eta_min, bill: {need, have, short},
            eta: {start, first_output, complete}, places: {name: n}, removes: {name: n, reason},
            swaps: {upgrade: n, replace: n, retire: n}, unlocks, self_running: bool, needs_helper: bool}],
 end_state: {per_s: {...}, power_mw, belts: {total, longest}, zones: [{kind, label, stage}]},
 research_path: [{tech, packs, source, stage, eta_min}],
 removes: [{label, gps, stage, reason}], gates: [{code, level, text}]}
```

The companion prompt and `skill/factorio/SKILL.md` require Claude to present the roadmap: the current stage (time to
first output, bill vs stock), the next gate and what it unlocks, and what is removed and when.

## 13. Open decisions for the owner

| # | Question | Default until answered |
|---|---|---|
| D1 | Automation (10 red packs) must be researched before any assembler exists. Is it acceptable for the helper to hand-craft those 10 packs from mined materials and put them in a lab, as a player would? | Yes. "No inserting science packs" is read as "no cheated packs". B2 counts the **next** tech (electric-mining-drill) from assembler-made packs. Round 3: this is an **open exception to B2's wording**, not a pass: every card and brief says it ("Automation's 10 red packs are hand-made and put in a lab; every later pack comes from the assemblers"). |
| D2 | Should the mod queue research on Approve? | Yes, behind the setting `claude-helper-queue-research` (default on). |
| D3 | On a far-apart map, is a connector stage bridged by the helper round acceptable for S0-S1? | Yes, shown loudly on the card. Every stage from S2 on must be self-running. |
| D4 | Round 3: may the planner touch the player's builds by default? | No for whole builds and their coal: `allow_removal` is off by default (the card lists what allowing it would gain). Yes, listed on the card, for the few tiles of an IDLE belt line a new belt crosses (without them the real-save copy's routes grow from 382 to 568 belt tiles). |

## 14. Acceptance (B1-B6) and test plan

All live runs use test slots 31-39 via `testenv/restart.ps1` only. The real game, its ports and the Factorio user
folder are never touched. Real-copy data and renders stay in `testenv/private/` (gitignored).

**B1 - master plan and stage validity** (offline, real copy + seed_4004):

- `render_roadmap` PNGs show the end state with reserved zones. Each one is reviewed and the review is recorded in
  the PR (without real-copy images).
- Every stage validates (7.3).
- `check_monotone` passes, with no removal other than whitelisted swaps.

`tests/test_master.py` asserts these on 1001-4004. A private test on the real copy is skipped when absent.

**B2 - real build, no cheats** (real copy, `-NoDebugOps`, `game.speed <= 10`):

- The helper builds S0..S3 from only the helper's (and connected player's) inventory, salvage, mining and crafting.
  There is no instant build and no given items. Approve goes through `plan_apply` and `plan_next_stage`.
- Automation is researched with 10 hand-crafted packs (D1). electric-mining-drill is then researched from packs made
  by the S3 assemblers. This is measured by lab consumption and the force's `item_production_statistics` for
  automation-science-pack, with no pack crafted by a character after S3 starts.
- ETA per stage measured vs `eta.complete`, within 50%.

Harness: `tests/live_stages.py --save <zip> --variant hub --to 3`.

**B3 - self-running** (live, after S2 and again after S3): the helper is set idle (no round, no orders) for 30 in-game
minutes. Report at start and end:

- plates/min (iron, copper);
- red/min (after S3);
- power satisfaction;
- the count of burner entities with no fuel and of boilers below 5 coal.

Pass: end >= 95% of start, 0 out-of-fuel burners. Offline `selfrun` (8.2) predicts the same numbers, and the report
compares them.

**B4 - swap census** (live S4 on a slot; research is set with a test cheat to reach S4 quickly, and the report says
so):

- Before/after census by unit_number: removed set == the master's `upgrade` + `retire` outs (counts from
  `stats.swap_counts`).
- The burner drills end up in the helper's inventory (or the plan's chest).
- Every electric drill's position == its slot centre.
- Every pole on a reserved pole tile.
- Player ghosts unchanged.

**B5 - green science**: S5 validates on the real copy and the seeds, and `rates.solve` matches the block's rated
output (0.5 green/s) within 5%. If reached live, measure it.

**B6 - regression**:

- `acceptance.py` passes on seeds 1001/2002/3003 for every offered stage up to S3 (cheat mode) and S0-S2
  (`--no-cheat`).
- pytest is green; any test changed for the variant rename keeps its old assertion under the alias.
- CI is green on Linux/macOS/Windows after push; `luac -p` is clean.

Unit tests to add:

- `test_master.py`: data model invariants, `stage_entities`, monotone check + planted conflict, swap counts, pole
  tiles == gap tiles, gates follow fixture research, connector stage split.
- `test_blocks.py`: `mining_field` both phases (burner drop point lane == electric drop lane, computed from the
  prototypes' drop vectors), `smelter_column`, `power_block` ratios, `red_block`/`green_block` rates.
- `test_validate.py`: the pole/inserter `existing_link` regression.
- `test_selfrun.py`: coal budget per leg, a starved-leg case, pre-fuel lifetime.
- `test_cards.py`: card <= 5 lines, roadmap line format, brief schema.

Engine checks to run first (round A, `tests/live_blocks.py`), each recorded in CONTRIBUTING's verified facts:

- E1: a burner drill in the belt-side columns of a 3x3 slot drops on the same belt tile and lane as the electric
  drill.
- E2: a burner inserter moving coal from a belt into a burner drill keeps itself fuelled.
- E3: field belt side-load + coal side-load into the mirrored smelter column.
- E4: burner output inserter lifetime per coal.
- E5: a 3x3 electric-drill ghost placed (`manual_ghost`) over a marked burner drill + burner inserter.
- E6: gap-row poles wire at pitch 7 after the swap.
- E7: a boiler row fed by burner inserters from a belt black-starts.

## 15. Implementation rounds

| Round | Content | Exit criteria |
|---|---|---|
| **A - master plan offline (M1-M4 core)** | validator fix (7.3); engine checks E1-E7; `master.py` data model; blocks `mining_field`, `smelter_column`, `power_block`, `red_block`, `lab_rows` (green block zone reserved, filled in round C); hub/on-site choice and routes (9); `derive_stages` S0-S4 with research gating (`recipes_enabled` in `base_state`); stage validation and `check_monotone`; coal chains + `selfrun`; agenda extensions; `render_roadmap`; `hub` variant on real copy, 4004 and old seeds | B1 for `hub` (S0-S4); pytest green; renders reviewed |
| **B - live: mod, stages, swaps** | mod: master storage, future stages, reserved rendering, roadmap rows, `on_research_finished`, stage-completion tracking, ready banner + companion event, swap execution in `build_ghosts` (dismantle -> place over marked), research queue on Approve; MCP `plan_roadmap` / `plan_next_stage`; cards 12.1; `live_stages.py`; ETA calibration of `WALK`, `PLACE_S`, `H` from the stage runs (closes the 1.1 vs 1.5 min gap) | B2, B3, B4 on the real copy (S0-S3, swap test); B3 on 4004 after S2 |
| **C - green, variants, polish** | `green_block` + S5; `onsite` and `reuse` variants with merge rule; rebuild review on zones with one threshold; brief + companion prompt + SKILL.md; acceptance.py stage support; docs, changelog, version bump | B5, B6, M7; CI green |

Risks:

- The burner-phase coal chain on far-apart maps (connector stages may dominate the bill).
- AM1 throughput at speed 0.5 vs belt-fed inputs (measure in round B).
- The research-queue setting.
- The render-object count on large zones (zones drawn as rectangles, not per tile).

## 16. Round A as built (implementation notes)

Code: `planner/master.py` (data model, designer, stage views, checks, ETA, review, cards, brief), new blocks in
`planner/blocks.py` (`mining_field`, `smelter_column`, `power_block` + `mirror_y`, `science_block`), `router.py`
(per-tile cost for negotiated routing), `validate.py` (the existing-link fix of 7.3), `render_plan.py`
(`render_roadmap`, `draw_master`), `server.py` (`plan_starter_base` returns masters; `plan_roadmap`,
`plan_next_stage`; v2 stays as the fallback and behind `FACTORIO_PLANNER=v2`), `plans.lua` (reserved zones drawn
faint with labels, from `stats.reserved`). Tests: `tests/test_master.py`; live: `tests/live_v3_blocks.py` (E1-E7),
`tests/live_master.py` (a master built stage by stage with test cheats and run untouched), `tests/live_v3_preview.py`.

Where the build differs from sections 4-12, and why:

- **Field geometry.** Burner slots are the EAST slots and the coal corridor is at x=8 flowing toward the head, so the
  coal leaves the field right of the ore, in the order the smelter column takes them (no crossing). Only east slots
  have a burner phase; west slots are electric-only. Coal fields feed their own corridor at the head and send the
  coal out at the tail.
- **Coal chains.** One small coal field per chain (iron, copper, boilers) instead of one snaking belt: no splitters,
  no taps. The field corridors stay in the end state (they carry the coal on to the furnaces), so no corridor is
  retired. The boiler chain's coal field is burner in S2-S3 and electric from S4.
- **Connector stages** S0b/S1b exist when a coal leg is longer than 120 tiles; until then a burner inserter and a chest
  at the coal field's tail collect coal for the helper's round, and the line's burners are `fuel=manual`.
- **Smelter columns** are built from the input end (bottom-first): S0 needs a short middle belt; the plate belts come
  with S3 (side A, to red) and S5 (side B, to green; buffer chests until then).
- **Gates.** S3's gate is `automation-science-pack` + `automation` (the red-pack recipe itself needs the trigger tech;
  found by the live build).
- **Routing** is negotiated congestion routing (PathFinder) over all belts at once, with an endgame that lays the
  contested belts strictly in sequence; placement is committed only when every open port stays reachable
  (`routable()`); up to 12 placement alternatives (`ATTEMPTS`) within 60 s per variant. A mirrored hub core swaps its
  columns' ore and coal input ports so the field pairs never have to cross.
- **Rebuild review** (section 11) already runs in round A for S0: coal out of idle machines, and idle clusters outside
  the zones taken apart when their parts make S0 at least max(3 min, 10%) sooner. Zones never cover the player's
  builds (they are obstacles), so the zone-move/adopt rules of section 11 are not needed yet.
- **ETA** is one `cost.simulate` run over the whole roadmap (salvage first, sites in order, research steps), not the
  agenda extensions of 8.3.
- **Self-running** is the static check of 8.2 (fuel paths from the validator, coal budget per chain, power, lanes)
  plus the live run in `tests/live_master.py`; the agenda simulation of 8.2 point 5 is not built.

Measured (live, test slots, cheats only for setup):

| Check | Result |
|---|---|
| E1-E7 (`live_v3_blocks.py`, seed 1001) | all pass; a burner output inserter moves ~65 plates per coal (`COAL_PER_SWING` = 1/65) |
| seed 2002 hub, S0-S3 built, 10 min untouched | iron 55-60/min (plan 60), copper 52-60 (plan 60), no burner out of fuel |
| seed 2002 hub, S0-S4, 16 min untouched, plate chests emptied, labs kept researching | iron 412/min first half (plan 450), copper 181 (plan 187.5), red 29.8/min (plan 30), 5/5 labs researching, no burner out of fuel |
| seed 4004 hub (far apart, S0b + S1b), S0-S4 | iron 338/304, copper 134/143, red 21/27 per min, 5/5 labs, no burner out of fuel |
| real-save copy hub, S0-S4 | iron 354/300, copper 158/130, red 23/26 per min, 5/5 labs, no burner out of fuel |

Plate rates in the second half of an S4 run are consumption-limited: side A feeds only the red block and side B fills
its buffer chests until the green block exists.

Open after round A: `onsite` fails on the real copy and seed 1001 (crossing or no column spot); on the real copy the
hub's coal legs and the boiler coal loop around the lake (230-270 tiles) because the gap between hub and lake is
crowded; S5 (green) is a reserved zone only; the mod has no master storage, stage tracking or ready banner yet
(`plan_next_stage` is the manual way); B2-B4 with the real helper are round B.

## 17. Round 2 (as built)

Round 1's verification found that the base did not run itself, that green science was only a label, that the tool
path could not advance the roadmap, and that the belts wound around the map. Each finding, what changed, and how it is
checked:

| Round-1 finding | Change | Checked by |
|---|---|---|
| Smelter input burner inserters ran dry holding ore (copper 0/min, red 0/min after S3) | Every smelting-column inserter is a burner/electric pair on one tile (`in_b`/`in_e`, `out_b`/`out_e`); S2 swaps all of them (`replace`), a third pole column (x 4) powers the input inserters of both sides. S0/S1 burner inserters are in the helper's round (hand-fed). `selfrun` flags any burner inserter that moves ore or plates from S2 on | E9 (electric column), `test_column_inserters_become_electric_at_s2`, B3 live |
| A coal field built while the helper carried no coal never started (found in round 2) | Coal-field burners carry `prime`: the helper gives them start fuel for every batch it places (not once per site) and its round restarts a coal field that ever stops | E10 (self-feed loop, 20 min), `test_coal_fields_are_primed_not_hand_fed`, B3 live |
| Electric coal drills fed the boilers: no power, no coal (S5 stalled in two live runs) | The boilers' coal field keeps its burner drills for good; its free slots get electric drills at S4 on top. Coal is sized on the expected load (drills at `DRILL_DUTY` 0.5) | static `selfrun` S2-S5 on every fixture and the real copy, B3/B5 live |
| No green block (S5 = label, did not validate) | `science_block` holds the green block (section 6.6, without long-handed inserters): cable -> circuit -> inserter <- gear -> belt stacked with direct insertion, inserters and belts on separate lanes of one belt, 6 green AM1. Hub: fixed links with two underground pairs (S5 has Logistics); onsite: two S5 routes with undergrounds allowed | E8 (0.496 packs/s), `test_green_block_is_laid_out_and_validates`, acceptance v3, B5 live |
| `plan_next_stage` re-offered S0 forever | `stage_status`: an entity a later stage retired counts as built once that stage is | `test_stage_status_counts_retired_entities_as_built`, B2 live |
| MCP `plan_apply` refused S0b/S1b ("fed by nothing") | A master stage is re-validated as the whole base after that stage (`validate_stage`) | `test_master_roadmap_in_the_game_and_next_stage`, B2 live |
| No automatic "Stage N ready" | Every later stage is sent to the game as a waiting plan (`future`: master, gate, the stage it follows). The mod offers it (status proposed, a print, a companion event `stage_ready`) on `on_research_finished` and every 10 s once its gate is open and the previous stage's ghosts and removals are done. Plans tab: a Roadmap section with what each stage waits for; approving a stage drops the other masters' waiting stages | B2 live (every stage offered by the mod before `plan_next_stage`) |
| Belts wound around the lake (real copy 824-1150 tiles) | Hub variant smelts at the coal: per ore a *split* chain (furnace coal straight from the coal patch; a small burner-only coal field feeds the ore field's corridor at its head until S4, then field corridor, leg and that coal field retire) when the coal is nearer the core than the field, else the round-1 sequential chain. The core spot is scored by real walking distance from its ports. Routes may cut through the player's idle belt lines (`CUT_COST` 8 per tile; the crossed tiles and the upstream tile are removed in the route's stage). Placement penalises straight crossings of planned pairs. The boiler coal belt is negotiated with a provisional steam block, then shore spots near the coal are tried with the belt routed for real and the shortest wins. A master whose belt winds over twice its straight distance (and 80+ tiles longer) is not offered while another variant does without; `design_master` tries further layouts first | B1 below |
| Offline and live variants differed | No time budget (`MAX_ATTEMPTS` 6 layouts, deterministic); the capture used offline is taken through the server's own export | master ids identical offline and live (round 2 did not ship its capture script; round 3: `testenv/capture_live.py`, section 18) |
| ETA fields disagreed (card 32 vs roadmap 10) | `roadmap_eta(status=...)`: stages that stand cost nothing and produce from minute 0; every figure is minutes from now, `eta.build` = the stage's own time; the card's "ready in" is the roadmap row's `complete` | `test_eta_counts_built_stages_as_done`, B2 live |
| Rebuild review dismantled 89-218 entities without asking | Card and brief say what S0 takes apart (`takes_apart`); `plan_starter_base(allow_removal=false)` keeps everything (listed as `could_remove` with its gain); the companion prompt and the skill ask before approving | `test_card_and_brief` |
| Helper stopped with a full inventory after the S4 swap | It stores the salvage it does not need in a chest of its own on free, unplanned ground and goes on | B2 live (S5 built) |

Other changes: `place_ghosts` keeps the half of underground belts; plan apply places a ghost over an entity it is
taking apart (a cut belt tile); `mining_field` blocks are as tall as their entities (a one-pair field's pole stuck out
and a later zone overlapped it); a port tile is reserved with its lane; open ports are forgotten once their belt is
laid; tiles the player's inserters and drills reach are blocked for the plan; `plan_next_stage` / `plan_roadmap`
export the whole master's extent (on 4004 a field lies beyond 200 tiles of the hub and its stage never counted as
built).

New tests: `tests/test_master.py` (green block, column inserters, retired-entity status, ETA from now, determinism,
head-fed field retirement, priming), `tests/test_planner_tools.py` (waiting stages sent to the game, next stage and
MCP apply through the master), `tests/live_v3_blocks.py` E8-E10, `tests/live_stages.py` (B2-B5 with the real helper),
`testenv/acceptance.py --planner v3 --to 5`.

Late round-2 calibration: the offer's gather rate is now the helper's pure hand-mining rate x 0.6 (`GATHER_SHARE`,
`helper_gather_rate`) instead of its life average (which counted every building walk: 4.4/min vs 28/min while
mining), and the stock counts the master's own plate buffers and coal chests. Real-save copy, two final runs:
every stage S0-S5 within 50 % of its "ready in" (ratios 0.55-1.18; S4 27 min offered, 22-24 measured).

## 18. Round 3 (as built)

Round 2's verification and three reviews found: the recommended real-copy plan sprawled (on-site, 647 belt tiles,
two plate belts per metal); the hub took up 209-558 tiles of burner-phase coal belt at S4 (`check_monotone` let it
pass because nothing reused the tiles); the player's builds were taken apart by default (89 entities on the real
copy) while the report said otherwise; 5 labs used only 11-12 of the 30 red packs/min on 30 s techs; Automation's
hand-made packs were filed as a pass; the helper's message promised a hand-feeding round the base no longer needed;
the capture script behind "offline == live" was missing; `acceptance.py` defaulted to slot 11 and a paid Claude call.

| Finding | Change | Checked by |
|---|---|---|
| Hub tore out its burner-phase coal belts | `mining_field(mixed=True)`: the field's head side-loads its ore onto the left lane and the coal that passed the corridor onto the right lane of ONE trunk; `smelter_column(mixed_in=True)` takes it straight. The coal leg out to the field is the furnaces' coal for good, so nothing retires; split chains are no longer used | E11 live (trunk lanes ore / coal only); `test_mixed_field_sends_ore_and_coal_on_one_belt` |
| `check_monotone` allowed tear-outs | Only in-place swaps: `SWAP_ROLES` lost corridor / route / field-belt / drill retires; every removed entity's tiles must be covered by an entity of the same stage; a `replace` must put the listed entity (inserter, or a splitter for a plate-belt tile). The connector's coal chest and inserter are covered by the leg, which now always leaves the coal field straight (`_route_net`) | `test_nothing_is_torn_out_but_in_place_swaps` (4 masters), `test_a_tear_out_is_a_stage_conflict`, independent overlap check on all 10 final masters (0 overlaps) |
| On-site sprawl (a second plate belt per metal from the far column) | One plate belt per metal (side A); at S5 a splitter replaces one of its tiles and a branch goes to the green block (`_branch_green`); side B keeps its plate chests for good (plates for building) | E12 live; `test_onsite_feeds_green_with_a_splitter_and_keeps_plate_chests`; real copy 647 -> 517 tiles, 4004 1253 -> 972 |
| Sprawl recommended | `score` = 0.5 x first plates + 0.5 x red science + 0.15 per belt tile (was ~0.045); the card names why the recommended plan wins ("135 fewer belt tiles, red science 5 min later, first plates 12 min later") | section 9.3 table: real copy recommends the 382-tile hub |
| Far-apart maps | The warning says when there is no closer patch ("no closer iron or copper patch in the scanned area") | seed_4004: one patch of each ore in range |
| Removal on by default | `allow_removal=False` everywhere (tool, `master_options`, `review`): no idle build taken apart, no coal taken out of machines; the card says what allowing it would gain. A new belt may still cut a few tiles of an IDLE belt line where it crosses (decision D4, listed on the card) | `test_removal_is_opt_in` |
| 5 labs too few | 10 labs at S3, 16 at S5; `science_note` in the brief says what the labs use per research speed; red at S3 is honestly 15/min (side A of the burner-phase iron column, ~30 iron/min) and 30/min from S4 | `test_labs_use_what_the_assemblers_make`; live S3 idle 16-19 red/min, S5 idle 30 red + 30 green |
| D1 hidden in a decision | `D1_TEXT` on every card and in `brief.exceptions` | `test_card_and_brief`, `test_roadmap_rows_and_brief_say_the_exception` |
| Helper message / hand-fed boilers | `plans.fuel_at` marks primed coal-field burners and leaves a v3 stage's belt-fed boilers out; the message counts the hand-fed burners or says it only restarts coal fields | live: "keeps the 16 hand-fed burners fuelled ..." (S0), "checks every few minutes that the coal fields keep running" (S5) |
| S0 burner inserter out of fuel | Column burner inserters get 10 coal per fill (`COLUMN_INS_FILL`, ~35 min) | live: no burner out of fuel after any stage |
| Cards hard to scan | Plain words (no option names, no EMD/LSP); roadmap with minutes from now per stage; the Plans-tab roadmap rows carry build time, bill and in-place replacements (`roadmap_label`) | `test_card_and_brief` |
| Not reproducible | `testenv/capture_live.py`: exports through the server's own path and compares `plan_starter_base` live with `master_options` on the capture | real copy: `['mhub-80c9c1', 'mons-477731']` IDENTICAL |
| ETA from a fresh helper | `HELPER_FRESH` = 0.6 x 28/min when no mining is measured yet (was the v2 life average 8.5); `ROUTE_TILE_S` = 1 s per belt-run tile (a 166-tile leg took 7.7 min against 4.5 predicted) | live run 3 below |
| Stalled S5 | The helper stores surplus (incl. plates above 600 / 400) in up to 4 chests of its own before it fetches or crafts; a hand craft waiting on a full inventory gives up after 10 s | live: runs 1-2 stalled at 24 ghosts, run 3 built S5 in 8.7 min |
| Unsafe acceptance defaults | `--slot 31` (asserted 31-39), `--claude-seed 0` | - |

Measured on the real-save copy (slot 32, `-NoDebugOps`, game.speed 10, recommended hub `mhub-80c9c1`, nothing of the
player's taken apart, research queued as a player would; run 3 = the final code):

| Stage | Offered | Measured | Ratio |
|---|---|---|---|
| S0 burner iron + coal | 34.5 | 24.4 | 0.71 |
| S0b coal belt to iron | 7.0 | 7.7 | 1.10 |
| S1 burner copper | 13.0 | 8.1 | 0.62 |
| S2 steam power + labs | 11.5 | 6.4 | 0.55 |
| S3 red science | 9.0 | 7.4 | 0.82 |
| S4 electric drills | 34.5 | 21.7 | 0.63 |
| S5 green science | 11.5 | 8.7 | 0.76 |

- Every stage after S0 was offered by the mod itself before `plan_next_stage`. Automation from the 10 hand-made
  packs (D1, the one exception) in 3 game minutes; everything after from assembler packs.
- Helper idle 30 game minutes after S2: iron 75 -> 60, copper 74 -> 60 per minute (the first 15 minutes drain the
  ore the 88/65-tile trunks hold; the second half is the planned 60/min in every window), 0 burners out of fuel,
  0 boilers low, 0 without power.
- After S3 (research kept queued as a player would): iron 56-60, copper 56/min, red 16-19/min (plan 15, iron-limited
  until S4), 524 red packs, 0 out of fuel, 0 without power. Red stopped only when every red-only tech was done.
- After S5: red 29.9-30.3, green 29.9-30.5 per minute from minute 6 on, 16 labs working, 4 boilers / 8 engines
  (3.2 of 7.2 MW), 0 out of fuel, 0 without power; one boiler (the last on the coal belt) held under 5 coal at the
  end sample while working. Iron 221 / copper 75 per minute: what the science uses (225 / 75), the rest backs up.
- S4 census by unit number: exactly the 12 planned burner drills and 12 burner inserters removed, 33 electric drills
  at their slots, 50 poles all on the master's positions, 0 belts removed, 12 burner drills back in the helper's
  inventory.
