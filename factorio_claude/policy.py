"""What the in-game chat Claude and the autopilot Claude may do.

The companion answers '@claude ...' chat and the Ask Claude box with `claude -p`. In multiplayer that text can come from
other players (and is prompt-injection material in any case), so chat mode gets an explicit tool allowlist, and the MCP
server it starts (env FACTORIO_CHAT_MODE=1) only registers those tools and refuses risky arguments:
no allow_large, blueprint mode only normal/forced, deconstruct radius <= 16, no clipboard writes.
The autopilot (env FACTORIO_AUTOPILOT_MODE=1, AUTOPILOT.md 4.4) gets less: look tools, research, and its own two tools
to return a decision; the planner and plan approval stay in code. Claude Code (the player at their own terminal) keeps
every tool.
"""
import os

CHAT_MODE_ENV = "FACTORIO_CHAT_MODE"
AUTOPILOT_MODE_ENV = "FACTORIO_AUTOPILOT_MODE"
AUTOPILOT_DECISION_ENV = "FACTORIO_AUTOPILOT_DECISION"   # file the autopilot_decide tool writes its answer to
MAX_CHAT_DECONSTRUCT_RADIUS = 16
CHAT_BLUEPRINT_MODES = ("normal", "forced")

# MCP tool names (server.py function names) the in-game chat Claude gets
CHAT_TOOLS = (
    # look
    "factorio_overview", "factorio_production", "factorio_power", "factorio_research", "factorio_problems",
    "factorio_inspect", "factorio_find", "factorio_resources", "factorio_map", "factorio_census",
    "factorio_foreman_report", "factorio_mark", "factorio_defense",
    # small, reversible actions (argument limits enforced by the server in chat mode)
    "factorio_place_ghosts", "factorio_clear_claude_ghosts", "factorio_set_recipe", "factorio_deconstruct",
    "factorio_queue_research", "factorio_defend",
    # helper character (free actions and its task queue)
    "factorio_quick_action", "factorio_agent_status", "factorio_agent_do", "factorio_agent_stop",
    # planner: proposes; the player approves in the Plans tab
    "plan_starter_base", "plan_preview", "plan_validate", "plan_list", "plan_helper", "world_summary",
    "plan_roadmap", "plan_next_stage",
    # blueprints (no clipboard in chat mode)
    "blueprint_generate", "blueprint_check", "blueprint_decode", "blueprint_place",
)

# Every tool server.py defines (in full mode) that chat mode does NOT get (kept in sync by tests/test_security.py)
NOT_IN_CHAT = (
    "factorio_player",          # other players' inventories and positions
    "factorio_say",             # impersonation / spam
    "factorio_instant_build", "factorio_agent_give", "factorio_agent_spawn",  # cheats (spawn: kit)
    "factorio_agent_remove",
    "factorio_run_lua",         # arbitrary code
    "factorio_chat_pop",        # would drain the companion's own inbox
    "plan_apply",               # the player approves plans themselves
    "clipboard_copy", "blueprint_encode", "blueprint_capture",  # host clipboard / file writes
    "factorio_autopilot", "factorio_autopilot_report",          # switching the autopilot: the host only
)

# Registered only in autopilot mode: how the autopilot Claude returns its choice to the companion
AUTOPILOT_ONLY = ("autopilot_decide", "autopilot_note")

# The autopilot Claude: look (read-only), research, and its decision tools. No plan_apply, cheats, raw Lua, say,
# deconstruct, helper orders or direct planner calls: Claude chooses among the options, code executes.
AUTOPILOT_TOOLS = (
    "factorio_overview", "factorio_production", "factorio_power", "factorio_research", "factorio_problems",
    "factorio_inspect", "factorio_find", "factorio_resources", "factorio_map", "factorio_census",
    "factorio_foreman_report", "factorio_agent_status", "plan_list", "plan_roadmap", "world_summary",
    "factorio_queue_research", "factorio_defense",
) + AUTOPILOT_ONLY

# never in the autopilot's list (tests/test_security.py)
AUTOPILOT_FORBIDDEN = (
    "plan_apply", "factorio_instant_build", "factorio_agent_give", "factorio_agent_spawn", "factorio_run_lua",
    "factorio_say", "factorio_deconstruct", "factorio_agent_remove", "plan_starter_base", "plan_next_stage",
    "factorio_autopilot", "factorio_agent_do", "factorio_place_ghosts", "blueprint_place", "clipboard_copy",
)


def chat_mode() -> bool:
    return os.environ.get(CHAT_MODE_ENV) == "1"


def autopilot_mode() -> bool:
    return os.environ.get(AUTOPILOT_MODE_ENV) == "1"


def mcp_names(names, server="factorio"):
    return [f"mcp__{server}__{n}" for n in names]
