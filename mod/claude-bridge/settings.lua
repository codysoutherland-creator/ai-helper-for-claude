data:extend{
  {type = "bool-setting", name = "claude-foreman-enabled", setting_type = "runtime-global",
    default_value = true, order = "a"},
  {type = "int-setting", name = "claude-foreman-interval", setting_type = "runtime-global",
    default_value = 30, minimum_value = 10, maximum_value = 3600, order = "b"},
  {type = "bool-setting", name = "claude-foreman-chat", setting_type = "runtime-global",
    default_value = false, order = "c"},
  {type = "bool-setting", name = "claude-helper-use-player-items", setting_type = "runtime-global",
    default_value = true, order = "d"},
  {type = "string-setting", name = "claude-who-can-ask", setting_type = "runtime-global",
    default_value = "host-only", allowed_values = {"host-only", "admins", "everyone"}, order = "aa"},
  {type = "bool-setting", name = "claude-show-coords", setting_type = "runtime-per-user",
    default_value = true, order = "e"},
  -- test/cheat ops over UDP/RCON (chat_simulate, confirm_simulate, gui_click, gui_dump, agent_give, instant_build,
  -- big deconstruct areas). Off for players; the headless test harness turns it on.
  {type = "bool-setting", name = "claude-debug-ops", setting_type = "startup",
    default_value = false, order = "z"},
}
