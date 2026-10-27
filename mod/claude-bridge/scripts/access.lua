-- Who may use Claude in this game. In multiplayer anyone can type '@claude ...' or click the panel buttons, and every
-- question is paid for by the host's Claude subscription, so by default only the host can ask, run held jobs, approve
-- plans or drive the shared helper. Mod setting "claude-who-can-ask" (runtime-global): host-only | admins | everyone.
--   host      = single-player, or player #1 of a hosted game (the one who created the save), or the server console
--   trusted   = host or a game admin
--   can_ask   = per the setting (free read-only things - help, status, alerts, blueprints to your own cursor - stay open)
--   can_confirm = can_ask and trusted: only the host/admins can release a held (expensive) job or approve a plan

local access = {}

local SETTING = "claude-who-can-ask"

function access.debug_ops()
  local s = settings.startup["claude-debug-ops"]
  return s ~= nil and s.value == true
end

function access.policy()
  local s = settings.global[SETTING]
  local v = s and s.value
  if v == "admins" or v == "everyone" then return v end
  return "host-only"
end

-- test hook (only honoured with the claude-debug-ops startup setting): pretend another player index is the host
local function host_index()
  if access.debug_ops() and storage.access_test_host_index then return storage.access_test_host_index end
  return 1
end

-- player: LuaPlayer or nil (nil = server console / RCON, which is the host's own machine)
function access.is_host(player)
  if player == nil then return true end
  if not player.valid then return false end
  if not game.is_multiplayer() and not (access.debug_ops() and storage.access_test_host_index) then return true end
  return player.index == host_index()
end

function access.is_trusted(player)
  if access.is_host(player) then return true end
  return player.valid and player.admin == true
end

function access.can_ask(player)
  local pol = access.policy()
  if pol == "everyone" then return true end
  if pol == "admins" then return access.is_trusted(player) end
  return access.is_host(player)
end

function access.can_confirm(player)
  return access.can_ask(player) and access.is_trusted(player)
end

function access.role(player)
  if player == nil then return "server" end
  if access.is_host(player) then return "host" end
  if player.valid and player.admin then return "admin" end
  return "player"
end

function access.denied_text()
  local pol = access.policy()
  if pol == "admins" then return "Only the host and admins can use Claude in this game." end
  return "Only the host can use Claude in this game (mod setting: Who can use Claude)."
end

-- print the free refusal line to that player (never more than once per second per player)
function access.deny(player, text)
  if not (player and player.valid) then return end
  storage.access_denied_tick = storage.access_denied_tick or {}
  local last = storage.access_denied_tick[player.index]
  if last and game.tick - last < 60 then return end
  storage.access_denied_tick[player.index] = game.tick
  player.print({"", "[color=0.85,0.47,0.34][Claude][/color] ", text or access.denied_text()})
end

return access
