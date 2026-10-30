"""What the in-game chat Claude may do.

The companion answers '@claude ...' chat and the Ask Claude box with `claude -p`. In multiplayer that text can come from
other players (and is prompt-injection material in any case), so chat mode gets an explicit tool allowlist, and the MCP
server it starts (env FACTORIO_CHAT_MODE=1) only registers those tools and refuses risky arguments:
no allow_large, blueprint mode only normal/forced, deconstruct radius <= 16, no clipboard writes.
Claude Code (the player at their own terminal) keeps every tool.
"""
import os

CHAT_MODE_ENV = "FACTORIO_CHAT_MODE"
MAX_CHAT_DECONSTRUCT_RADIUS = 16
CHAT_BLUEPRINT_MODES = ("normal", "forced")

# MCP tool names (server.py function names) the in-game chat Claude gets
CHAT_TOOLS = (
    # look
    "factorio_overview", "factorio_production", "factorio_power", "factorio_research", "factorio_problems",
    "factorio_inspect", "factorio_find", "factorio_resources", "factorio_map", "factorio_census",
    "factorio_foreman_report", "factorio_mark",
    # small, reversible actions (argument limits enforced by the server in chat mode)
    "factorio_place_ghosts", "factorio_clear_claude_ghosts", "factorio_set_recipe", "factorio_deconstruct",
    "factorio_queue_research",
    # helper character (free actions and its task queue)
    "factorio_quick_action", "factorio_agent_status", "factorio_agent_do", "factorio_agent_stop",
    # planner: proposes; the player approves in the Plans tab
    "plan_starter_base", "plan_preview", "plan_validate", "plan_list", "plan_helper", "world_summary",
    "plan_roadmap", "plan_next_stage",
    # blueprints (no clipboard in chat mode)
    "blueprint_generate", "blueprint_check", "blueprint_decode", "blueprint_place",
)

# Every tool server.py defines that chat mode does NOT get (kept in sync by tests/test_security.py)
NOT_IN_CHAT = (
    "factorio_player",          # other players' inventories and positions
    "factorio_say",             # impersonation / spam
    "factorio_instant_build", "factorio_agent_give", "factorio_agent_spawn",  # cheats (spawn: kit)
    "factorio_agent_remove",
    "factorio_run_lua",         # arbitrary code
    "factorio_chat_pop",        # would drain the companion's own inbox
    "plan_apply",               # the player approves plans themselves
    "clipboard_copy", "blueprint_encode", "blueprint_capture",  # host clipboard / file writes
)


def chat_mode() -> bool:
    return os.environ.get(CHAT_MODE_ENV) == "1"


def mcp_names(names, server="factorio"):
    return [f"mcp__{server}__{n}" for n in names]
