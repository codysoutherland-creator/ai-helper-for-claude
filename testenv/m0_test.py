# M0 in-game checks on a test slot (default 1): ghost tagging + clear_claude_ghosts, markdown stripping in
# claude_reply, the claude_confirm round-trip. Usage: .venv\Scripts\python.exe testenv\m0_test.py [slot]
import sys
from pathlib import Path

SLOT = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ROOT = Path(__file__).resolve().parents[1]
exec(open(ROOT / "testenv" / "t.py").read())
sys.path.insert(0, str(ROOT / "tests"))
from test_m0 import MD_CASES  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("ok  " if cond else "FAIL") + " " + name + ("" if cond else f"  {info}"))
    if not cond:
        fails.append(name)


rcon.lua("game.forces.player.chart(game.surfaces.nauvis, {{-200,-200},{200,200}})")
# find open ground near spawn (no water / entities) for 3 rows of chests
rcon.lua("game.surfaces.nauvis.request_to_generate_chunks({0,0}, 4) game.surfaces.nauvis.force_generate_chunk_requests()")
spot = rcon.lua("""
local s = game.surfaces.nauvis
for r = 0, 120, 8 do for _, d in ipairs({{1,0},{0,1},{-1,0},{0,-1}}) do
  local x, y = d[1]*r, d[2]*r
  local area = {{x-1, y-1}, {x+80, y+8}}
  if s.count_entities_filtered{area=area} == 0 and s.count_tiles_filtered{area=area, collision_mask="water_tile"} == 0 then
    rcon.print(x .. "," .. y) return
  end
end end""")
x0, y0 = map(int, spot.strip().split(","))
print("test area at", x0, y0)

# 1) place_ghosts: default tag "manual", explicit plan tag
r = B("place_ghosts", ghosts=[{"name": "wooden-chest", "x": x0 + i + 0.5, "y": y0 + 0.5} for i in range(5)])
check("place_ghosts manual", r["placed"] == 5, r)
r = B("place_ghosts", plan="p1", ghosts=[{"name": "wooden-chest", "x": x0 + i + 0.5, "y": y0 + 2.5} for i in range(4)])
check("place_ghosts plan p1", r["placed"] == 4, r)
tags = rcon.lua(f"""local out = {{}}
for _, g in pairs(game.surfaces.nauvis.find_entities_filtered{{type="entity-ghost", area={{{{{x0},{y0}}},{{{x0+10},{y0+4}}}}}}}) do
  out[#out+1] = (g.tags and g.tags.claude_plan or "nil") end
table.sort(out) rcon.print(table.concat(out, ","))""").strip()
check("ghost tags", tags == "manual,manual,manual,manual,manual,p1,p1,p1,p1", tags)

# 2) blueprint_build: tagged with plan, existing blueprint tags kept
from factorio_claude import blueprints as bpm  # noqa: E402
bpd = bpm.make_blueprint([{"name": "wooden-chest", "x": i + 0.5, "y": 0.5, "tags": {"mine": i}} for i in range(3)], "t")
r = B("blueprint_build", blueprint=bpm.encode(bpd), x=x0 + 20, y=y0 + 4, plan="bp7")
check("blueprint_build", r["ghosts"] == 3, r)
tags = rcon.lua(f"""local out = {{}}
for _, g in pairs(game.surfaces.nauvis.find_entities_filtered{{type="entity-ghost", area={{{{{x0+15},{y0+2}}},{{{x0+30},{y0+6}}}}}}}) do
  out[#out+1] = (g.tags and (g.tags.claude_plan .. ":" .. tostring(g.tags.mine)) or "nil") end
table.sort(out) rcon.print(table.concat(out, ","))""").strip()
check("blueprint ghost tags (plan + own tags kept)", tags == "bp7:0,bp7:1,bp7:2", tags)

# 3) untagged script ghosts (like v0.1/v0.2 Claude ghosts) + "player" ghosts.
# Headless test map has no players, so a player-placed ghost (last_user = player) can't be created here; if a
# player exists, use it.
has_player = rcon.lua("rcon.print(#game.players)").strip() != "0"
rcon.lua(f"""local s = game.surfaces.nauvis
for i = 0, 5 do s.create_entity{{name="entity-ghost", inner_name="wooden-chest", position={{{x0}+i+0.5, {y0}+6.5}}, force="player"}} end
local p = game.players[1]
if p then for i = 0, 2 do
  local g = s.create_entity{{name="entity-ghost", inner_name="wooden-chest", position={{{x0}+40+i+0.5, {y0}+0.5}}, force="player"}}
  g.last_user = p end end""")
print("player ghosts:", "yes" if has_player else "no players on this map (skipped)")
nplayer = 3 if has_player else 0

d = B("clear_claude_ghosts", plan="p1", dry_run=True)
check("dry run plan p1", d["removed"] == 4 and d["dry_run"] and d["kept_other"] == 5 + 3 + 6, d)
d = B("clear_claude_ghosts", plan="p1")
check("clear plan p1", d["removed"] == 4 and d["by_plan"] == {"p1": 4}, d)
d = B("clear_claude_ghosts", dry_run=True)
check("dry run all", d["removed"] == 5 + 3 + 6 and d["kept_player"] == nplayer, d)
d = B("clear_claude_ghosts")
check("clear all", d["removed"] == 14 and d["tagged"] == 8 and d["untagged"] == 6 and
      d["by_plan"] == {"manual": 5, "bp7": 3} and d["kept_player"] == nplayer, d)
left = int(rcon.lua("rcon.print(game.surfaces.nauvis.count_entities_filtered{type='entity-ghost'})"))
check("only player ghosts left", left == nplayer, left)

# 4) markdown stripping in claude_reply (same cases as the Python plain_text)
for md, want in MD_CASES:
    B("claude_reply", text=md)
    got = B("claude_history", n=1)["history"][-1]["text"]
    check(f"strip {md[:30]!r}", got == want, f"got {got!r} want {want!r}")
B("claude_reply", text="**x**", cost="cost ≈ $0.02 · 4 s")
h = B("claude_history", n=1)["history"][-1]
check("cost kept in history", h.get("cost") == "cost ≈ $0.02 · 4 s" and h["text"] == "X", h)

# 5) confirm round-trip (mod side)
B("inbox_pop")
B("claude_confirm", request_id="abc123", estimate_text="This looks like a **planning** job (≈ $0.05–0.30). Run it?")
h = B("claude_history", n=1)
check("confirm pending", [c["id"] for c in h["confirms"]] == ["abc123"] and "**" not in h["confirms"][0]["text"], h)
r = B("confirm_simulate", request_id="abc123", run=True)
check("confirm run", r["ok"], r)
msgs = B("inbox_pop")
check("inbox carries run", any(m["source"] == "confirm" and m["text"] == "run abc123" for m in msgs), msgs)
check("confirm cleared", not B("claude_history", n=1)["confirms"])
r = B("confirm_simulate", request_id="abc123", run=False)
check("second answer refused", not r["ok"], r)
B("claude_confirm", request_id="zz9", estimate_text="Run it?")
B("confirm_simulate", request_id="zz9", run=False)
msgs = B("inbox_pop")
check("inbox carries cancel", [m["text"] for m in msgs] == ["cancel zz9"], msgs)

print("\nFAILED: " + ", ".join(fails) if fails else "\nall M0 in-game checks passed")
sys.exit(1 if fails else 0)
