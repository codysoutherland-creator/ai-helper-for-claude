"""Factorio MCP server: lets Claude observe and act in a live Factorio 2.x game.

Talks to the claude-bridge mod over UDP (game launched with --enable-lua-udp, i.e. the Factorio (Claude) shortcut;
single-player is fine), falling back to RCON. See link.py.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import blueprints as bp
from . import platforms, policy
from .link import LinkError, call
from .rcon import RconError, lua

LIBRARY = Path(os.environ.get("FACTORIO_BLUEPRINT_DIR") or Path(__file__).resolve().parent.parent / "blueprints")
LIBRARY_KEEP = 200  # newest saved blueprint strings kept in the library
# In-game chat (companion) mode: only the chat allowlist is registered and risky arguments are refused (policy.py).
CHAT_MODE = policy.chat_mode()

INSTRUCTIONS = """Tools for playing Factorio 2.x alongside the user.
Coordinates are map tiles (x right, y DOWN). Entity positions are centers: 1x1 at .5, 2x2 at whole numbers, 3x3 at .5.
The player has free in-game buttons/keywords for simple helper actions (factorio_quick_action does the same);
the mod's foreman watches the base for free (factorio_foreman_report). Start with `factorio_overview`. Use `factorio_map` to see layouts before building. Prefer ghosts/blueprints
(robots or the user build them) or the agent character; `factorio_instant_build`/`factorio_agent_give` are cheats -
only use them when the user asks. Agent tasks run asynchronously in-game: queue them, then poll `factorio_agent_status`.
Builds bigger than a few entities go through the planner: `plan_starter_base` plans, verifies and shows 2-3 variants
in the game's Plans tab; the player approves one there. Never hand-place large builds with ghost tools."""

mcp = MCPServer("factorio", instructions=INSTRUCTIONS)
REGISTERED = []  # tool names registered in this process


def tool(fn):
    """@mcp.tool(), except that chat mode only registers the tools in policy.CHAT_TOOLS."""
    if CHAT_MODE and fn.__name__ not in policy.CHAT_TOOLS:
        return fn
    REGISTERED.append(fn.__name__)
    return mcp.tool()(fn)


def chat_refusal(what: str) -> str:
    return f"REFUSED: {what} is not available when Claude is asked from in-game chat (ask from Claude Code instead)."


def _call(op, **args) -> str:
    try:
        return json.dumps(call(op, **args), separators=(",", ":"))
    except LinkError as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- observe

@tool
def factorio_overview(surface: str | None = None) -> str:
    """One-call snapshot: players, research, power networks, top production (10m), and grouped machine problems."""
    try:
        out = {
            "game": call("ping"),
            "research": call("research", surface=surface),
            "power": call("power", surface=surface),
            "production_10m": call("production", surface=surface, window="10m", limit=12),
            "problems": call("problems", surface=surface, samples=2),
        }
        return json.dumps(out, separators=(",", ":"))
    except LinkError as e:
        return f"ERROR: {e}"


@tool
def factorio_production(window: str = "10m", items: list[str] | None = None, limit: int = 25,
                        surface: str | None = None) -> str:
    """Item/fluid production and consumption rates per minute. window: 5s|1m|10m|1h. items: optional filter."""
    return _call("production", window=window, items=items, limit=limit, surface=surface)


@tool
def factorio_power(surface: str | None = None) -> str:
    """Each electric network: production vs consumption (MW), producers, top consumers."""
    return _call("power", surface=surface)


@tool
def factorio_research(available: bool = False) -> str:
    """Current research, progress, queue. available=True also lists researchable techs."""
    return _call("research", available=available)


@tool
def factorio_problems(x: float | None = None, y: float | None = None, radius: float | None = None,
                      types: list[str] | None = None, samples: int = 3, surface: str | None = None) -> str:
    """Machines not working, grouped by (entity, status) with sample positions. Omit x/y/radius for whole surface."""
    return _call("problems", x=x, y=y, radius=radius, types=types, samples=samples, surface=surface)


@tool
def factorio_inspect(x: float, y: float, radius: float | None = None, name: str | None = None,
                     type: str | None = None, limit: int = 60, surface: str | None = None) -> str:
    """Detailed state of the entity at x,y (no radius) or all entities in a radius: recipe, status, inventories,
    belt lane contents, inserter pickup/drop, fuel, power."""
    return _call("inspect", x=x, y=y, radius=radius, name=name, type=type, limit=limit, surface=surface)


@tool
def factorio_find(name: str | None = None, type: str | None = None, x: float | None = None,
                  y: float | None = None, max_distance: float = 200, limit: int = 20,
                  surface: str | None = None) -> str:
    """Find entities by prototype name or type, nearest-first from x,y (defaults: whole surface, unsorted)."""
    return _call("find", name=name, type=type, x=x, y=y, max_distance=max_distance, limit=limit, surface=surface)


@tool
def factorio_resources(x: float | None = None, y: float | None = None, radius: float = 64,
                       surface: str | None = None) -> str:
    """Ore/oil patches around a point (default: near the player): total amount, tile count, bounds, center."""
    return _call("resources", x=x, y=y, radius=radius, surface=surface)


@tool
def factorio_map(x: float | None = None, y: float | None = None, radius: float = 16,
                 surface: str | None = None) -> str:
    """ASCII top-down map around x,y (default: player), max 100x100. Returns rows + legend.
    Belts are ^>v<, ores lowercase, water ~, ghosts ?."""
    try:
        r = call("map", x=x, y=y, radius=radius, surface=surface)
    except LinkError as e:
        return f"ERROR: {e}"
    ox, oy = r["origin"]["x"], r["origin"]["y"]
    header = f"origin x={ox} y={oy} (top-left tile), {r['width']}x{r['height']}\n"
    ruler = "     " + "".join(str((ox + i) % 10) for i in range(r["width"]))
    body = "\n".join(f"{oy + i:>4} {row}" for i, row in enumerate(r["rows"]))
    legend = "\n".join(f"  {k} = {v}" for k, v in sorted(r["legend"].items()))
    return header + ruler + "\n" + body + "\nlegend:\n" + legend


@tool
def factorio_census(x: float | None = None, y: float | None = None, radius: float | None = None,
                    surface: str | None = None) -> str:
    """Count of the player force's entities by name (whole surface, or within radius of x,y)."""
    return _call("census", x=x, y=y, radius=radius, surface=surface)


@tool
def factorio_player(player: str | None = None) -> str:
    """A player's position, inventory, cursor item and the entity they're hovering (great for 'this thing here')."""
    return _call("player", player=player)


# ---------------------------------------------------------------- act

@tool
def factorio_say(text: str) -> str:
    """Post a chat message in-game as [Claude]."""
    return _call("say", text=text)


@tool
def factorio_mark(x: float, y: float, text: str = "", seconds: float = 60, surface: str | None = None) -> str:
    """Point at a spot for the player: a blinking ring + label in the world AND on the map for `seconds`, plus a
    clickable map pin in chat. Use it whenever you talk about a location (players can't see coordinates).
    In chat text, write locations as [gps=X,Y] - Factorio turns that into a clickable pin."""
    return _call("mark", x=x, y=y, text=text, seconds=seconds, surface=surface)


MAX_GHOSTS = 50


def too_many(n: int, allow_large: bool) -> str | None:
    """Refusal text when a hand-placed build is too big (Claude must use the planner), else None."""
    if CHAT_MODE and allow_large:
        return chat_refusal("allow_large")
    if n <= MAX_GHOSTS or allow_large:
        return None
    return (f"REFUSED: {n} ghosts is more than {MAX_GHOSTS}. Large builds must go through the planner "
            "(plan_starter_base: verified variants shown in the Plans tab; the player approves in-game), which verifies "
            "belts, fuel, power and terrain." + ("" if CHAT_MODE else " Only if the player explicitly asked for exactly "
                                                  "this build, call again with allow_large=True."))


def blueprint_entity_count(blueprint: str) -> int:
    """Entities in a blueprint string (active blueprint of a book); 0 if it can't be decoded offline."""
    try:
        d = bp.decode(blueprint)
    except Exception:
        return 0
    if "blueprint_book" in d:
        book = d["blueprint_book"]
        items = book.get("blueprints") or []
        idx = book.get("active_index", 0)
        d = next((b for b in items if b.get("index") == idx), items[0] if items else {})
    return len((d.get("blueprint") or {}).get("entities") or [])


@tool
def factorio_place_ghosts(ghosts: list[dict[str, Any]], surface: str | None = None, plan: str | None = None,
                          allow_large: bool = False) -> str:
    """Place entity ghosts for robots/players to build. ghosts: [{name, x, y, direction?: north|east|south|west,
    recipe?}]. Returns placed count + blocked ones. plan: short id tagged on every ghost (default "manual") so
    factorio_clear_claude_ghosts can remove exactly this build. More than 50 ghosts is refused (use the planner)
    unless allow_large=True because the player explicitly asked."""
    refusal = too_many(len(ghosts or []), allow_large)
    if refusal:
        return refusal
    return _call("place_ghosts", ghosts=ghosts, surface=surface, plan=plan)


@tool
def factorio_clear_claude_ghosts(plan: str | None = None, dry_run: bool = False, surface: str | None = None) -> str:
    """Remove ghosts Claude placed (script-placed, or tagged with `plan` when given) on all surfaces (or `surface`).
    Never touches ghosts a player placed. dry_run=True only counts. Returns removed / kept counts."""
    return _call("clear_claude_ghosts", plan=plan, dry_run=dry_run or None, surface=surface)


@tool
def factorio_set_recipe(x: float, y: float, recipe: str) -> str:
    """Set the recipe of the assembler/chemical plant at x,y (directly, no character needed)."""
    return _call("set_recipe", x=x, y=y, recipe=recipe)


@tool
def factorio_deconstruct(x: float, y: float, radius: float = 1, name: str | None = None,
                         type: str | None = None) -> str:
    """Mark entities for deconstruction (robots remove them)."""
    if CHAT_MODE and not (0 <= float(radius) <= policy.MAX_CHAT_DECONSTRUCT_RADIUS):
        return chat_refusal(f"a deconstruct radius over {policy.MAX_CHAT_DECONSTRUCT_RADIUS}")
    return _call("deconstruct", x=x, y=y, radius=radius, name=name, type=type)


@tool
def factorio_queue_research(tech: str) -> str:
    """Add a technology to the research queue."""
    return _call("queue_research", tech=tech)


@tool
def factorio_instant_build(x: float, y: float, radius: float = 16) -> str:
    """CHEAT: instantly build all ghosts in the area without items. Only when the user explicitly asks."""
    return _call("instant_build", x=x, y=y, radius=radius)


@tool
def factorio_run_lua(code: str) -> str:
    """Escape hatch: run raw Lua via /silent-command. Return data with rcon.print(...).
    RCON only (opt-in: config "rcon": true; Claude Code only, never the in-game chat). Disables achievements on that
    save. Prefer the structured tools."""
    if CHAT_MODE:
        return chat_refusal("factorio_run_lua")
    try:
        return lua(code) or "(no output)"
    except RconError as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- blueprints

def _save(name: str, s: str) -> str:
    safe = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in str(name))[:60] or "blueprint"
    try:
        LIBRARY.mkdir(parents=True, exist_ok=True)
        path = LIBRARY / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.txt"
        path.write_text(s, encoding="utf-8")
        old = sorted(LIBRARY.glob("*.txt"), key=lambda p: p.stat().st_mtime)[:-LIBRARY_KEEP]
        for p in old:  # keep the library bounded
            p.unlink(missing_ok=True)
        return str(path)
    except OSError as e:
        return f"(not saved: {e})"


def _clip(s: str) -> bool:
    """Put text on the OS clipboard. Never in chat mode: chat text must not reach the host's clipboard."""
    if CHAT_MODE:
        return False
    cmd = platforms.clipboard_cmd()
    if not cmd:
        return False
    data = s.encode("ascii", errors="replace") if os.name == "nt" else s.encode("utf-8")
    try:
        subprocess.run(cmd, input=data, check=True, timeout=5)
        return True
    except Exception:
        return False


@tool
def blueprint_generate(kind: str, params: dict[str, Any] | None = None, label: str | None = None,
                       copy: bool = True) -> str:
    """Generate a proven layout as a blueprint string. kind:
    - smelter_column {count, furnace: stone-furnace|steel-furnace|electric-furnace, belt, inserter}
    - assembler_line {count, recipe, machine, belt, inserter, second_input}
    - mining_outpost {rows, drill, belt}
    - solar_block {pairs, panel_rows, accumulator_rows}
    Saves to the library, copies to clipboard (copy=True), returns the string."""
    gen = bp.GENERATORS.get(kind)
    if not gen:
        return f"ERROR: unknown kind; options: {list(bp.GENERATORS)}"
    ents = gen(**(params or {}))
    s = bp.encode(bp.make_blueprint(ents, label or kind.replace("_", " ")))
    path = _save(label or kind, s)
    copied = copy and _clip(s)
    return json.dumps({"entities": len(ents), "saved": path, "clipboard": copied, "blueprint": s})


@tool
def blueprint_encode(entities: list[dict[str, Any]], label: str = "Claude blueprint", copy: bool = True) -> str:
    """Build a blueprint string from your own entity list: [{name, x, y, direction?, recipe?, ...}].
    Positions are entity centers. Run blueprint_check on the result before handing it over."""
    s = bp.encode(bp.make_blueprint(entities, label))
    path = _save(label, s)
    copied = copy and _clip(s)
    return json.dumps({"entities": len(entities), "saved": path, "clipboard": copied, "blueprint": s})


@tool
def blueprint_decode(blueprint: str) -> str:
    """Decode a blueprint string to its JSON (offline; doesn't need the game)."""
    try:
        return json.dumps(bp.decode(blueprint), separators=(",", ":"))[:60000]
    except Exception as e:
        return f"ERROR: {e}"


@tool
def blueprint_check(blueprint: str) -> str:
    """Validate a blueprint in-engine: entity list, unknown entity names, and overlapping footprints."""
    return _call("blueprint_inspect", blueprint=blueprint)


@tool
def blueprint_place(blueprint: str, x: float, y: float, direction: str = "north",
                    mode: str = "forced", surface: str | None = None, plan: str | None = None,
                    allow_large: bool = False) -> str:
    """Stamp a blueprint into the world as ghosts centered at x,y. mode: normal (all-or-nothing) |
    forced (skip blocked, clear trees) | superforced. plan: id tagged on the ghosts (default "manual").
    Blueprints with more than 50 entities are refused (use the planner) unless allow_large=True because the
    player explicitly asked."""
    if CHAT_MODE and mode not in policy.CHAT_BLUEPRINT_MODES:
        return chat_refusal(f"mode={mode!r}")
    n = blueprint_entity_count(blueprint)
    if CHAT_MODE and n == 0:
        return chat_refusal("a blueprint whose size can't be checked")
    refusal = too_many(n, allow_large)
    if refusal:
        return refusal
    return _call("blueprint_build", blueprint=blueprint, x=x, y=y, direction=direction, mode=mode, surface=surface,
                 plan=plan)


@tool
def blueprint_capture(x: float, y: float, radius: float = 8, label: str | None = None, copy: bool = True) -> str:
    """Turn an existing build in the world into a blueprint string (to study, tweak, or reuse)."""
    try:
        r = call("blueprint_capture", x=x, y=y, radius=radius, label=label)
    except LinkError as e:
        return f"ERROR: {e}"
    r["saved"] = _save(label or "capture", r["blueprint"])
    r["clipboard"] = copy and _clip(r["blueprint"])
    return json.dumps(r)


@tool
def clipboard_copy(text: str) -> str:
    """Copy text (e.g. a blueprint string) to the clipboard so the user can paste it in-game."""
    if CHAT_MODE:
        return chat_refusal("clipboard_copy")
    return "copied" if _clip(text) else "ERROR: clipboard copy failed"


# ---------------------------------------------------------------- planner (M1)
# Deterministic planning in Python: export the map + game data, plan validated variants, show them in-game. The
# player approves in the Plans tab (plan_apply places tagged ghosts). Plans are kept in plans/<id>.json.
# FACTORIO_PLANS_DIR: tests keep their plans out of the real plans/ folder
PLANS = Path(os.environ.get("FACTORIO_PLANS_DIR") or Path(__file__).resolve().parent.parent / "plans")
PLAN_RADIUS = 200  # tiles around the player exported for planning


def _plan_path(pid: str) -> Path:
    safe = "".join(c for c in str(pid) if c.isalnum() or c in "-_")
    return PLANS / f"{safe}.json"


def _load_plan(pid: str):
    from .planner.plan import Plan
    p = _plan_path(pid)
    if not p.exists():
        raise LinkError(f"no plan {pid!r} (known: {', '.join(sorted(f.stem for f in PLANS.glob('*.json'))[-8:])})")
    return Plan.load(p)


def _player_spot(x=None, y=None):
    """(x, y, surface) of the request: given coordinates, else the first player's position, else spawn."""
    if x is not None and y is not None:
        return float(x), float(y), None
    try:
        p = call("player")
        return float(p["pos"]["x"]), float(p["pos"]["y"]), p.get("surface")
    except (LinkError, KeyError, TypeError):
        return 0.0, 0.0, None


def _world(cx, cy, surface=None, radius=PLAN_RADIUS):
    from .planner.gamedata import GameData
    from .planner.world import World
    r = int(radius)
    world = World.from_link(surface=surface, area=(cx - r, cy - r, cx + r, cy + r), link=call, charted=_charted())
    return world, GameData.from_link(link=_LinkShim)


def _charted():
    """Export charted chunks only (default); FACTORIO_PLAN_UNCHARTED=1 (headless tests: nothing gets charted)."""
    return False if os.environ.get("FACTORIO_PLAN_UNCHARTED") == "1" else None


class _LinkShim:  # GameData.from_link wants an object with .call
    call = staticmethod(call)


def _send_preview(plan, group=None, show=True) -> dict:
    try:
        return call("plan_preview", **plan.preview_args(group=group, show=show))
    except LinkError as e:
        return {"error": str(e)}


def _jdump(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=str)


@tool
def plan_starter_base(goal_text: str = "", variants: int = 3, stage: str | None = None, x: float | None = None,
                      y: float | None = None) -> str:
    """Plan a complete starter base near the player (or x,y): mines on iron/copper/stone/coal -> smelting hub ->
    plate bus, coal feed, and steam power in the "electric" stage. The planner exports the map, places, routes and
    VERIFIES everything (deterministic, free), then shows the variants in the game's Plans tab where the player
    previews and approves one. stage: "bootstrap" (burner, buildable from nothing) | "electric" | omitted = what
    research allows. goal_text: the player's words (e.g. "bigger", "electric"). variants: 1-3 (compact / balanced /
    expandable). Returns compact summaries - explain the trade-offs in plain sentences and let the player pick."""
    from .planner import placer
    cx, cy, surface = _player_spot(x, y)
    t0 = time.monotonic()
    try:
        world, gd = _world(cx, cy, surface)
    except (LinkError, TimeoutError, OSError) as e:
        return f"ERROR: map export failed: {e}"
    goal = placer.parse_goal(goal_text)
    goal["near"] = (cx, cy)
    if stage:
        goal["stage"] = stage
    names = placer.DEFAULT_VARIANTS[:max(1, min(3, int(variants or 3)))]
    try:
        plans = placer.plan_variants(world, gd, goal, names)
    except placer.PlanError as e:
        return _jdump({"error": str(e), "why": e.reasons[-6:]})
    group = f"g{int(time.time()) % 100000}"
    out = []
    for i, p in enumerate(plans):
        p.save(PLANS)
        shown = _send_preview(p, group=group, show=(i == 0))
        b = p.brief()
        if "error" in shown:
            b["preview_error"] = shown["error"]
        out.append(b)
    return _jdump({"plans": out, "seconds": round(time.monotonic() - t0, 1), "group": group,
                   "skipped": plans[0].stats.get("skipped_variants") or [],
                   "next": "The player compares them in the Plans tab (Preview / Approve / Reject). Do not call "
                           "plan_apply unless the player explicitly tells you which plan to build."})


@tool
def plan_preview(plan_id: str, show: bool = True) -> str:
    """(Re)send a saved plan's overlay and summary to the game's Plans tab (show=False only lists it)."""
    try:
        p = _load_plan(plan_id)
    except LinkError as e:
        return f"ERROR: {e}"
    return _jdump({"plan": p.id, "preview": _send_preview(p, show=show)})


@tool
def plan_validate(plan_id: str) -> str:
    """Re-check a saved plan against the map as it is NOW (fresh export of its area): terrain, overlaps with new
    buildings, belts, fuel, power. Its own ghosts / already built parts don't count as obstacles."""
    from .planner.gamedata import GameData
    from .planner.world import World
    try:
        p = _load_plan(plan_id)
        x0, y0, x1, y1 = p.stats.get("bbox") or (-50, -50, 50, 50)
        world = World.from_link(surface=p.surface, area=(x0 - 8, y0 - 8, x1 + 8, y1 + 8), link=call,
                                charted=_charted())
        gd = GameData.from_link(link=_LinkShim)
    except (LinkError, TimeoutError, OSError) as e:
        return f"ERROR: {e}"
    rep = p.revalidate(gd, world)
    return _jdump({"plan": p.id, "ok": rep.ok, "summary": rep.summary(6)})


@tool
def plan_apply(plan_id: str) -> str:
    """Place a plan's ghosts (tagged with its id) after re-validating it against the current map. Only when the
    player explicitly asked to build this plan - normally they approve in the Plans tab themselves."""
    if CHAT_MODE:
        return chat_refusal("plan_apply")
    from .planner.gamedata import GameData
    from .planner.world import World
    try:
        p = _load_plan(plan_id)
        x0, y0, x1, y1 = p.stats.get("bbox") or (-50, -50, 50, 50)
        world = World.from_link(surface=p.surface, area=(x0 - 8, y0 - 8, x1 + 8, y1 + 8), link=call,
                                charted=_charted())
        rep = p.revalidate(GameData.from_link(link=_LinkShim), world)
        if not rep.ok:
            return _jdump({"plan": p.id, "applied": False, "why": rep.summary(6),
                           "next": "the map changed; plan again (plan_starter_base)"})
        try:
            call("plan_get", id=p.id)
        except LinkError:  # the game doesn't know it (other save / dropped): send it again, hidden
            _send_preview(p, show=False)
        return _jdump({"plan": p.id, "applied": call("plan_apply", id=p.id)})
    except (LinkError, TimeoutError, OSError) as e:
        return f"ERROR: {e}"


@tool
def plan_helper(plan_id: str, what: str = "build") -> str:
    """Free helper work on an approved plan (same as its Plans tab buttons): what="build" builds its ghosts (the helper
    fetches/crafts/mines materials, then fills the hand-fed burners with coal); what="refuel" only tops up the hand-fed
    burner drills/inserters (they run ~22 min on 50 coal). Spawns the helper if needed."""
    try:
        return _jdump(call("plan_helper", id=plan_id, what=what))
    except LinkError as e:
        return f"ERROR: {e}"


@tool
def plan_list() -> str:
    """Plans the game knows (status proposed/applying/applied) - compact."""
    try:
        rows = call("plan_list")
    except LinkError as e:
        return f"ERROR: {e}"
    return _jdump([{k: r.get(k) for k in ("id", "title", "status", "entities", "result")} for r in (rows or [])])


@tool
def world_summary(x: float | None = None, y: float | None = None, radius: int = 150) -> str:
    """Compact overview around the player (or x,y) from a fresh map export: nearest ore patches (amount, size,
    center, distance), water bodies, existing buildings, and which planner stage research allows."""
    from .planner import placer
    cx, cy, surface = _player_spot(x, y)
    try:
        world, gd = _world(cx, cy, surface, radius)
    except (LinkError, TimeoutError, OSError) as e:
        return f"ERROR: {e}"
    patches = {}
    for p in world.patches(min_tiles=20):
        d = round(((p.center[0] - cx) ** 2 + (p.center[1] - cy) ** 2) ** 0.5)
        row = {"center": [round(p.center[0]), round(p.center[1])], "tiles": p.size, "amount": p.amount, "dist": d}
        patches.setdefault(p.name, []).append(row)
    for k in patches:
        patches[k] = sorted(patches[k], key=lambda r: r["dist"])[:3]
    water = []
    for b in world.water_bodies(30)[:4]:
        water.append({"center": [round(b.center[0]), round(b.center[1])], "tiles": b.size})
    nw = world.nearest_water_tile(cx, cy)
    return _jdump({"at": [round(cx), round(cy)], "surface": world.surface, "patches": patches, "water": water,
                   "nearest_water": list(nw[0]) if nw else None, "buildings": len(world.entities),
                   "planner_stage": placer.default_stage(gd)})


# ---------------------------------------------------------------- agent character

@tool
def factorio_agent_spawn(player: str | None = None, kit: bool = False) -> str:
    """Spawn Claude's own character next to the player (orange, labeled 'Claude'). kit=True gives a small
    starter kit (cheat-ish; ask first)."""
    if CHAT_MODE and kit:
        return chat_refusal("kit=True")
    return _call("agent_spawn", player=player, kit=kit)


@tool
def factorio_agent_do(tasks: list[dict[str, Any]], replace: bool = False) -> str:
    """Queue tasks for Claude's character (run in order, in real game time; poll factorio_agent_status).
    Out-of-reach targets are walked to automatically. On failure the queue halts so you can re-plan.
    Task types:
      {type:"walk", x, y, radius?}
      {type:"mine", x?, y?, name?, entity_type?, count?}  - by position, or nearest by name/type
          e.g. {type:"mine", entity_type:"tree", count:10}, {type:"mine", name:"iron-ore", count:50}
      {type:"craft", recipe, count}
      {type:"place", name, x, y, direction?, recipe?}     - uses items from its inventory
      {type:"build_ghosts", x?, y?, radius?, limit?}      - builds nearby ghosts it has items for
      {type:"insert", x, y, item, count?}                 - e.g. fuel into a furnace
      {type:"take", x, y, item?, count?}                  - from chest/machine output
      {type:"give_player", item, count?, player?}
      {type:"recipe", x, y, recipe}
      {type:"follow", player?, distance?, ticks?}
      {type:"say", text}  {type:"wait", ticks}
    replace=True clears the existing queue first."""
    return _call("agent_tasks", tasks=tasks, replace=replace)


@tool
def factorio_agent_status() -> str:
    """Claude character: position, inventory, crafting queue, current task, queue, halted reason, recent log."""
    return _call("agent_status")


@tool
def factorio_agent_stop() -> str:
    """Stop the character and clear its task queue."""
    return _call("agent_stop")


@tool
def factorio_agent_remove(drop_items: bool = True) -> str:
    """Remove Claude's character (inventory goes into a wooden chest at its position)."""
    return _call("agent_remove", drop_items=drop_items)


@tool
def factorio_agent_give(item: str, count: int = 1) -> str:
    """CHEAT: spawn items into Claude's character inventory. Only when the user says it's fine."""
    return _call("agent_give", item=item, count=count)


@tool
def factorio_quick_action(action: str, player: str | None = None, count: int | None = None) -> str:
    """Free one-shot helper command, same as the in-game buttons (auto-spawns the helper). action:
    spawn | dismiss | follow (until stopped) | stop | come | wood (10 trees) | stone|iron|copper|coal (mine count,
    default 50, nearest to the player) | build_ghosts (r=32 around player) | refuel (burners r=32) |
    give_all (helper's items -> player) | take_hand (player's cursor stack -> helper)."""
    return _call("agent_quick", action=action, player=player, count=count)


@tool
def factorio_foreman_report(rescan: bool = False) -> str:
    """The mod's free rule-based base monitor: alerts (power_shortage, no_fuel, depleted, no_power/low_power,
    research_idle, output_full, starved) with severity, positions and counts, plus stats. rescan=True scans now."""
    return _call("foreman_report", rescan=rescan or None)


# ---------------------------------------------------------------- chat

@tool
def factorio_chat_pop() -> str:
    """Return (and clear) messages players addressed to Claude ('@claude ...' chat or the Ask Claude box).
    Normally the companion process answers these; only use this when it isn't running."""
    return _call("inbox_pop")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
