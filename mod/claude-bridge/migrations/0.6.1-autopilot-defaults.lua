-- 0.6.1 changed two autopilot defaults, but a save keeps the values it already had: move saves still on the old
-- defaults to the new ones (once per save). A value the player changed from the old default stays as it is.
local function move(name, old, new)
  local s = settings.global[name]
  if s and s.value == old then settings.global[name] = {value = new} end
end
move("claude-autopilot-allow-removal", false, true)
move("claude-autopilot-keepout", 16, 8)
