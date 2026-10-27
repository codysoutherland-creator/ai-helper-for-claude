-- Free chat keyword commands (no tokens): "@claude wood 20", "@claude follow", "@claude blueprint smelter 8".
-- Only short command-like messages are handled; anything with other words ("iron is low, why?") goes to Claude.

local agent = require("scripts.agent")
local foreman = require("scripts.foreman")
local blueprint_gen = require("scripts.blueprint_gen")
local transport = require("scripts.transport")
local util = require("scripts.util")

local local_commands = {}

-- keyword -> agent.quick action
local actions = {
  spawn = "spawn", come = "come", here = "come",
  dismiss = "dismiss", bye = "dismiss", leave = "dismiss",
  follow = "follow", stop = "stop", halt = "stop",
  wood = "wood", chop = "wood", trees = "wood",
  stone = "stone", iron = "iron", copper = "copper", coal = "coal",
  build = "build_ghosts", ghosts = "build_ghosts",
  refuel = "refuel", fuel = "refuel",
  give = "give_all", gimme = "give_all",
  take = "take_hand", hand = "take_hand",
}

-- words allowed around a keyword without making it a question for Claude
local filler = {}
for w in ("please pls plz now me us some the a an ore ores tree trees wood logs stone rock rocks to here over " ..
    "more all you are is it up everything stuff items go get mine chop gather collect x and thanks thx ghost " ..
    "ghosts burners things my your back plates"):gmatch("%S+") do filler[w] = true end
local lead = {mine = true, get = true, gather = true, collect = true, go = true, please = true, pls = true}

-- blueprint kind aliases (first word match against KINDS ids also works)
local kind_alias = {smelter = "smelter_column", smelting = "smelter_column", furnaces = "smelter_column",
  assembler = "assembler_line", assembly = "assembler_line", assemblers = "assembler_line",
  mining = "mining_outpost", outpost = "mining_outpost", drills = "mining_outpost", miner = "mining_outpost",
  solar = "solar_block"}

local function kinds()
  local ok, k = pcall(function() return blueprint_gen.KINDS end)
  return ok and type(k) == "table" and k or {}
end

local function find_kind(word)
  if not word then return nil end
  word = kind_alias[word] or word
  for _, k in ipairs(kinds()) do
    if k.id == word or k.id:find("^" .. word:gsub("%p", "%%%0")) then return k end
  end
  return nil
end

local function help_text()
  local ids = {}
  for _, k in ipairs(kinds()) do ids[#ids + 1] = k.id end
  return "Free commands (no tokens) - say @claude <cmd>:\n" ..
    "  spawn | come | follow | stop | dismiss\n" ..
    "  wood [n] | stone [n] | iron [n] | copper [n] | coal [n]  (mine near you)\n" ..
    "  build (ghosts near you) | refuel | give (all its items) | take (item in your hand)\n" ..
    "  status | alerts | blueprint <kind> [count] [param=value]" ..
    (#ids > 0 and ("  kinds: " .. table.concat(ids, ", ")) or "") .. "\n" ..
    "Anything else is sent to Claude (uses tokens)."
end

local function status_text()
  local s = agent.describe()
  local st = agent.status()
  if st.alive and st.inventory then
    local items = {}
    for name, n in pairs(st.inventory) do items[#items + 1] = {name, n} end
    table.sort(items, function(x, y) return x[2] > y[2] end)
    local parts = {}
    for i = 1, math.min(6, #items) do parts[i] = items[i][2] .. " " .. items[i][1] end
    if #items > 6 then parts[#parts + 1] = "+" .. (#items - 6) .. " more" end
    if #parts > 0 then s = s .. "\nCarrying: " .. table.concat(parts, ", ") end
  end
  local ok, online = pcall(transport.companion_online)
  return s .. "\nClaude companion: " .. ((ok and online) and "online" or "offline")
end

local sev_rank = {high = 1, medium = 2, info = 3}

local function alerts_text(player, rescan)
  local ok, rep = pcall(rescan and foreman.scan or foreman.get_report, player.force)
  if not ok or type(rep) ~= "table" then return "Foreman report unavailable." end
  local list = {}
  for _, al in ipairs(rep.alerts or {}) do list[#list + 1] = al end
  if #list == 0 then return "Foreman: no problems found." end
  table.sort(list, function(x, y) return (sev_rank[x.severity] or 9) < (sev_rank[y.severity] or 9) end)
  local lines = {"Foreman: " .. #list .. " alert" .. (#list > 1 and "s" or "") .. ":"}
  for i = 1, math.min(5, #list) do
    local al = list[i]
    local where = al.position and (" [gps=" .. util.round(al.position.x, 0) .. "," .. util.round(al.position.y, 0) ..
      (al.surface and ("," .. al.surface) or "") .. "]") or ""
    lines[#lines + 1] = "  [" .. tostring(al.severity) .. "] " .. tostring(al.text or al.kind) ..
      ((al.count and al.count > 1) and (" (x" .. al.count .. ")") or "") .. where
  end
  if #list > 5 then lines[#lines + 1] = "  +" .. (#list - 5) .. " more (Foreman tab in the Claude panel)" end
  return table.concat(lines, "\n")
end

-- "blueprint <kind> [count] [name=value ...]"
local function blueprint_cmd(player, words)
  local k = find_kind(words[2])
  if not k then
    if #kinds() == 0 then return true, "Blueprint generator isn't available yet." end
    return true, "Which blueprint? " .. help_text():match("kinds: ([^\n]*)")
  end
  local params, count = {}, nil
  for i = 3, #words do
    local w = words[i]
    local key, val = w:match("^([%w_]+)=(.+)$")
    if key then
      params[key] = tonumber(val) or val
    elseif tonumber(w) then
      count = tonumber(w)
    elseif not filler[w] then
      return false  -- not a plain command; let Claude handle it
    end
  end
  if count then  -- the number goes to the first int param (count / rows / pairs)
    for _, pdef in ipairs(k.params or {}) do
      if pdef.type == "int" then params[pdef.name] = params[pdef.name] or count; break end
    end
  end
  local ok, res, err = pcall(blueprint_gen.to_cursor, player, k.id, params)
  if not ok then return true, "Blueprint failed: " .. tostring(res) end
  if not res then return true, "Blueprint failed: " .. tostring(err) end
  return true, (k.title or k.id) .. " blueprint is in your cursor."
end

-- -> handled:boolean, reply:string
function local_commands.try(player, text)
  local words = {}
  for w in text:lower():gsub("[^%w%s_=%.%-]", " "):gmatch("%S+") do words[#words + 1] = w end
  if #words == 0 then return true, help_text() end  -- bare "@claude"
  while #words > 1 and lead[words[1]] and (actions[words[2]] or words[2] == "blueprint") do table.remove(words, 1) end
  local cmd = words[1]

  if cmd == "help" or cmd == "commands" or cmd == "?" then
    if #words > 2 then return false end
    return true, help_text()
  end
  if cmd == "blueprint" or cmd == "bp" then return blueprint_cmd(player, words) end

  -- remaining words: an optional number + filler only, else it's a question for Claude
  local n
  for i = 2, #words do
    local w = words[i]
    if tonumber(w) and not n then
      n = math.floor(tonumber(w))
    elseif not filler[w] and not (actions[w] and actions[w] == actions[cmd]) then
      return false
    end
  end

  if cmd == "status" or cmd == "where" then
    local a = agent.get()
    if a and cmd == "where" then util.mark(a.entity.surface, a.entity.position, "Claude helper", 20) end
    return true, status_text()
  end
  if cmd == "alerts" or cmd == "report" or cmd == "scan" then return true, alerts_text(player, cmd == "scan") end
  local action = actions[cmd]
  if not action then return false end
  if n and n < 1 then n = nil end
  if n then n = math.min(n, 1000) end
  return true, agent.quick(player, action, {count = n})
end

local_commands.help_text = help_text

return local_commands
