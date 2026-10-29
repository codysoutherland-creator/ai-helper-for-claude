# Planner test fixtures

Fresh vanilla Space Age maps (Factorio 2.1.23, default map settings, no player), one folder per seed:
`seed_<n>/map/` = export_map of spawn +-200 tiles (400x400, `charted=false`, headless servers don't chart),
`seed_<n>/data.json` = export_data of the fresh force, `seed_<n>/screen.json` = in-game counts within
150 tiles of spawn (resource tiles / amount / nearest distance, water). Saves: `testenv/write/seed_<n>.zip`.

Load in tests: `from factorio_claude.planner.testing import load_fixture, fixture_seeds`;
`world, gd = load_fixture(1001)` (cached, read-only; `cached=False` for a private copy).
Start a live instance from the same map: `testenv/restart.ps1 -Name <name> -Slot <slot> -Save seed_<n>.zip`.
Rebuild: `.venv\Scripts\python.exe testenv\make_fixtures.py create|build <seeds>` then `readme`.

## seed 1001

Spawn (0, 0); exported bbox (-224, -224, 224, 224), tick 83, 7342 obstacles, 0 force entities.

| resource | patches | total amount | nearest patch: tiles / amount / center / closest tile to spawn |
|---|---|---|---|
| coal | 1 | 201,573 | 243 / 201,573 / (17, 50) / 42 |
| copper-ore | 1 | 393,494 | 394 / 393,494 / (-61, -43) / 64 |
| iron-ore | 2 | 1,090,935 | 586 / 300,059 / (-44, -34) / 38 |
| stone | 2 | 222,481 | 250 / 189,318 / (-8, -51) / 42 |

Water: 1695 tiles in 1 bodies (>=30 tiles); largest 1695 tiles; nearest body to spawn: 1695 tiles centered (76, 9), nearest tile 57 from spawn.

## seed 2002

Spawn (0, 0); exported bbox (-224, -224, 224, 224), tick 61, 4546 obstacles, 0 force entities.

| resource | patches | total amount | nearest patch: tiles / amount / center / closest tile to spawn |
|---|---|---|---|
| coal | 1 | 322,163 | 385 / 322,163 / (6, -64) / 53 |
| copper-ore | 1 | 283,735 | 395 / 283,735 / (-54, 5) / 45 |
| crude-oil | 1 | 1,457,785 | 3 / 1,457,785 / (-190, -169) / 252 |
| iron-ore | 2 | 1,194,396 | 604 / 305,361 / (2, 47) / 33 |
| stone | 2 | 1,099,362 | 200 / 179,984 / (-10, 56) / 49 |

Water: 2727 tiles in 2 bodies (>=30 tiles); largest 2470 tiles; nearest body to spawn: 2470 tiles centered (67, 28), nearest tile 38 from spawn.

## seed 3003

Spawn (0, 0); exported bbox (-224, -224, 224, 224), tick 62, 2294 obstacles, 0 force entities.

| resource | patches | total amount | nearest patch: tiles / amount / center / closest tile to spawn |
|---|---|---|---|
| coal | 3 | 893,976 | 336 / 324,170 / (26, -48) / 44 |
| copper-ore | 2 | 168,903 | 229 / 97,533 / (23, 80) / 72 |
| iron-ore | 1 | 242,082 | 612 / 242,082 / (-58, 75) / 76 |
| stone | 1 | 173,525 | 237 / 173,525 / (-42, 2) / 34 |

Water: 2164 tiles in 1 bodies (>=30 tiles); largest 2164 tiles; nearest body to spawn: 2164 tiles centered (70, 38), nearest tile 53 from spawn.

## seed 4004 (sprawl, synthetic)

Planner v2 regression map (`make_fixtures.py sprawl 4004`): natural ore and water removed around spawn, then iron ~180 tiles, copper ~150, coal ~90 and stone ~200 tiles from spawn in different directions, one lake ~70 tiles south-east, and old builds in the middle (an idle row of 14 stone furnaces fed by an 83-tile belt from nowhere, 8 burner drills off any ore, 2 unpowered labs, stray belts, a pole line). `state.json`: synthetic inventory (100 iron plates, 60 stone, 20 coal, 10 wood, 1 burner drill), machine status from the game, steam power researched. Not in `fixture_seeds()` (default kind "fresh"); use `fixture_seeds("sprawl")` / `load_state(4004)`.

Spawn (0, 0); exported bbox (-224, -224, 224, 224), tick 147, 6052 obstacles, 155 force entities.

| resource | patches | total amount | nearest patch: tiles / amount / center / closest tile to spawn |
|---|---|---|---|
| coal | 1 | 716,400 | 796 / 716,400 / (7, -90) / 74 |
| copper-ore | 1 | 1,107,000 | 1230 / 1,107,000 / (-149, 9) / 129 |
| iron-ore | 1 | 2,198,900 | 1999 / 2,198,900 / (128, 127) / 151 |
| stone | 1 | 484,000 | 605 / 484,000 / (-140, -142) / 184 |

Water: 728 tiles in 1 bodies (>=30 tiles); largest 728 tiles; nearest body to spawn: 728 tiles centered (49, 50), nearest tile 53 from spawn.
