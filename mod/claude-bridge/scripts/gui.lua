-- mod-gui top button + draggable panel: Helper | Foreman | Blueprints | Ask Claude | Plans.
-- Everything except "Ask Claude" is free (pure Lua). All element names start with "claude_".
-- Plans tab: Claude's proposed plans (plans.lua) with Preview / Show / Approve / Adjust / Reject; plan buttons are
-- named "claude_plan_<action>|<id>" and carry tags {plan_action, plan}.
-- storage.gui[player_index] = {open, tab, location, kind, params = {[kind_id] = {...}}, count, msg, bp_msg, seen_tick,
--   clear_pending (dry-run counts while "Clear Claude's ghosts" waits for confirmation)}
-- Test ops: gui_click {player, name, value?} and gui_dump {player} (no mouse needed; debug ops only).
-- Access (access.lua): asking Claude, helper buttons, clearing ghosts and plan actions follow the "Who can use Claude"
-- setting; Run / Cancel and Approve also need the host or an admin. Viewing, coordinates and blueprints stay free.

local mod_gui = require("mod-gui")
local events = require("scripts.events")
local api = require("scripts.api")
local util = require("scripts.util")
local access = require("scripts.access")
local transport = require("scripts.transport")
local agent = require("scripts.agent")
local foreman = require("scripts.foreman")
local blueprint_gen = require("scripts.blueprint_gen")
local plans = require("scripts.plans")

local gui = {}

local WIDTH = 420
local TEXT_W = WIDTH - 44
local TABS = {"helper", "foreman", "blueprints", "ask", "plans"}  -- append only: st.tab is a stored index
local TAB_CAPTIONS = {helper = "Helper", foreman = "Foreman", blueprints = "Blueprints", ask = "Ask Claude", plans = "Plans"}
local ACTIONS = {  -- {action, caption, tooltip}
  {"spawn", "Spawn", "Spawn the helper next to you (no free items)."},
  {"follow", "Follow me", "Follow you until stopped."},
  {"come", "Come here", "Walk to you once."},
  {"stop", "Stop", "Stop the current task and clear the queue."},
  {"wood", "Chop wood", "Chop 10 trees near you."},
  {"build_ghosts", "Build ghosts", "Build ghosts within 32 tiles of you from its inventory."},
  {"refuel", "Refuel", "Fuel burner machines within 32 tiles of you from its inventory."},
  {"give_all", "Get items", "Walk over and hand you everything it carries."},
  {"dismiss", "Dismiss", "Drop its items in a wooden chest and remove it."},
}
local ORES = {{"stone", "stone"}, {"iron", "iron-ore"}, {"copper", "copper-ore"}, {"coal", "coal"}}
local SEV_COLOR = {high = "1,0.3,0.3", medium = "1,0.65,0.2", info = "0.7,0.7,0.7"}

---------------------------------------------------------------------------
-- helpers
---------------------------------------------------------------------------

local function pstate(player)
  storage.gui = storage.gui or {}
  local st = storage.gui[player.index]
  if not st then
    st = {open = false, tab = 1, kind = 1, params = {}, count = 50}
    storage.gui[player.index] = st
  end
  return st
end

local function panel(player) return player.gui.screen.claude_panel end

-- depth-first search by element name
local function find(el, name)
  if not (el and el.valid) then return nil end
  if el.name == name then return el end
  for _, c in pairs(el.children) do
    local r = find(c, name)
    if r then return r end
  end
  return nil
end

local function wrap_label(parent, name, caption, style)
  local l = parent.add{type = "label", name = name, caption = caption or "", style = style}
  l.style.single_line = false
  l.style.maximal_width = TEXT_W
  return l
end

local function ago(tick)
  if not tick then return "never" end
  local s = math.floor((game.tick - tick) / 60)
  if s < 60 then return s .. " s ago" end
  return math.floor(s / 60) .. " min ago"
end

-- call a module function; never throws (other modules may still be stubs)
local function try(fn, ...)
  if type(fn) ~= "function" then return false, "not available" end
  local ok, a, b = pcall(fn, ...)
  if not ok then log("claude-bridge gui: " .. tostring(a)) end
  return ok, a, b
end

local function kinds()
  local k = blueprint_gen.KINDS
  return type(k) == "table" and k or {}
end

local function icon(name)
  if prototypes.item[name] then return "[item=" .. name .. "] " end
  if prototypes.entity[name] then return "[entity=" .. name .. "] " end
  if prototypes.recipe[name] then return "[recipe=" .. name .. "] " end
  return ""
end

-- choices may be plain strings or {value|name|id, title|caption}
local function choice_value(c) return type(c) == "table" and (c.value or c.name or c.id or c[1]) or c end
local function choice_caption(c)
  local v = tostring(choice_value(c))
  local t = type(c) == "table" and (c.title or c.caption or c[2])
  if t then return icon(v) .. tostring(t) end
  local proto = prototypes.item[v] or prototypes.entity[v] or prototypes.recipe[v]
  if proto then return {"", icon(v), proto.localised_name} end
  return v
end

---------------------------------------------------------------------------
-- tab builders (static widgets) + refreshers (dynamic content)
---------------------------------------------------------------------------

local build_tab, refresh_tab = {}, {}

function build_tab.helper(player, c, st)
  wrap_label(c, "claude_helper_status", "", "bold_label")
  local msg = wrap_label(c, "claude_helper_msg", st.msg or "")
  msg.style.font_color = {0.75, 0.75, 0.75}
  local t = c.add{type = "table", name = "claude_helper_actions", column_count = 3}
  t.style.horizontal_spacing = 4
  t.style.vertical_spacing = 4
  for _, a in ipairs(ACTIONS) do
    local b = t.add{type = "button", name = "claude_act_" .. a[1], caption = a[2], tooltip = a[3]}
    b.style.width = 108
  end
  local row = c.add{type = "flow", name = "claude_mine_row", direction = "horizontal"}
  row.style.vertical_align = "center"
  row.add{type = "label", caption = "Mine"}
  local n = row.add{type = "textfield", name = "claude_mine_count", text = tostring(st.count or 50), numeric = true,
    tooltip = "How much ore to mine (default 50)."}
  n.style.width = 56
  for _, o in ipairs(ORES) do
    row.add{type = "sprite-button", name = "claude_act_" .. o[1], sprite = "item/" .. o[2], style = "slot_button",
      tooltip = "Mine " .. o[2] .. " nearest to you."}
  end
  local hrow = c.add{type = "flow", direction = "horizontal"}
  local h = hrow.add{type = "button", name = "claude_act_take_hand", caption = "Hand item in cursor to helper",
    tooltip = "Moves the stack you're holding into the helper's inventory."}
  h.style.horizontally_stretchable = true
  hrow.add{type = "button", name = "claude_show_helper", caption = "Show helper",
    tooltip = "Mark the helper in the world and on the map, and post a clickable map pin."}
  local grow = c.add{type = "flow", name = "claude_ghost_row", direction = "horizontal"}
  local cg = grow.add{type = "button", name = "claude_clear_ghosts", caption = "Clear Claude's ghosts", style = "red_button",
    tooltip = "Removes the ghosts Claude placed (all surfaces). Ghosts you placed yourself are never touched. Asks first."}
  cg.style.horizontally_stretchable = true
  local cf = c.add{type = "flow", name = "claude_clear_confirm", direction = "horizontal"}
  cf.style.vertical_align = "center"
  local q = cf.add{type = "label", name = "claude_clear_q", caption = ""}
  q.style.single_line = false
  q.style.maximal_width = TEXT_W - 150
  cf.add{type = "button", name = "claude_clear_yes", caption = "Remove", style = "red_confirm_button",
    tooltip = "Remove them now."}.style.width = 70
  cf.add{type = "button", name = "claude_clear_no", caption = "Cancel", style = "back_button"}.style.width = 70
  c.add{type = "label", name = "claude_helper_inv_title", caption = "Helper inventory", style = "bold_label"}
  c.add{type = "table", name = "claude_helper_inv", column_count = 8}
end

function refresh_tab.helper(player, c, st)
  local _, desc = try(agent.describe)
  c.claude_helper_status.caption = desc or "Helper status unavailable."
  c.claude_helper_msg.caption = st.msg or ""
  local cp = st.clear_pending
  c.claude_clear_confirm.visible = cp ~= nil
  c.claude_ghost_row.visible = cp == nil
  if cp then
    c.claude_clear_confirm.claude_clear_q.caption = "Remove " .. cp.removed .. " ghost(s) placed by Claude?" ..
      (cp.kept_player > 0 and (" Your " .. cp.kept_player .. " ghost(s) stay.") or "")
  end
  local inv = c.claude_helper_inv
  inv.clear()
  local a = agent.get and select(2, try(agent.get))
  local e = a and a.entity
  local main = e and e.valid and e.get_main_inventory()
  local items = main and main.get_contents() or {}
  table.sort(items, function(x, y) return x.count > y.count end)
  for i, it in ipairs(items) do
    if i > 24 then break end
    local path = "item/" .. it.name
    if helpers.is_valid_sprite_path(path) then
      inv.add{type = "sprite-button", sprite = path, number = it.count, style = "slot_button",
        tooltip = it.name .. " x" .. it.count, quality = it.quality}
    end
  end
  c.claude_helper_inv_title.caption = #items > 0 and "Helper inventory" or "Helper inventory: empty"
end

function build_tab.foreman(player, c, st)
  local row = c.add{type = "flow", name = "claude_foreman_row", direction = "horizontal"}
  row.style.vertical_align = "center"
  row.add{type = "button", name = "claude_foreman_scan", caption = "Scan now", tooltip = "Free rule-based check of the base."}
  row.add{type = "label", name = "claude_foreman_time", caption = ""}
  wrap_label(c, "claude_foreman_note", "")
  local sp = c.add{type = "scroll-pane", name = "claude_foreman_list", vertical_scroll_policy = "auto-and-reserve-space"}
  sp.style.maximal_height = 320
  sp.style.horizontally_stretchable = true
end

function refresh_tab.foreman(player, c, st)
  local _, rep = try(foreman.get_report, player.force)
  rep = type(rep) == "table" and rep or {}
  c.claude_foreman_row.claude_foreman_time.caption = "Last scan: " .. ago(rep.tick)
  local enabled = settings.global["claude-foreman-enabled"]
  local note = (enabled and not enabled.value) and "Auto-scan is off (mod settings). " or ""
  local alerts = rep.alerts or {}
  c.claude_foreman_note.caption = note .. (#alerts == 0 and "No problems found." or (#alerts .. " alert(s)."))
  local sp = c.claude_foreman_list
  sp.clear()
  sp.visible = #alerts > 0
  for i, al in ipairs(alerts) do
    if i > 30 then break end
    local row = sp.add{type = "flow", direction = "horizontal"}
    row.style.vertical_align = "center"
    local text = "[color=" .. (SEV_COLOR[al.severity] or SEV_COLOR.info) .. "]" .. (al.severity or "info") ..
      "[/color] " .. tostring(al.text or al.kind or "?")
    local l = row.add{type = "label", caption = text}
    l.style.single_line = false
    l.style.width = TEXT_W - 50
    local pos = al.position
    if pos and pos.x then
      row.add{type = "sprite-button", name = "claude_alert_" .. i, sprite = "utility/gps_map_icon", style = "tool_button",
        tooltip = "Post a map ping for this location in chat (only you see it).",
        tags = {x = pos.x, y = pos.y, surface = al.surface or player.surface.name}}
    end
  end
end

-- param widgets for the selected kind
local function build_params(player, c, st)
  local t = c.claude_bp_params
  t.clear()
  local kind = kinds()[st.kind]
  if not kind then return end
  local saved = st.params[kind.id] or {}
  for _, p in ipairs(kind.params or {}) do
    t.add{type = "label", caption = p.title or p.name, tooltip = p.name}
    local name = "claude_bp_p_" .. p.name
    local v = saved[p.name]
    if v == nil then v = p.default end
    if p.type == "choice" then
      local items, values, sel = {}, {}, 1
      for i, ch in ipairs(p.choices or {}) do
        items[i] = choice_caption(ch)
        values[i] = choice_value(ch)
        if choice_value(ch) == v then sel = i end
      end
      local dd = t.add{type = "drop-down", name = name, items = items, selected_index = #items > 0 and sel or nil,
        tags = {param = p.name, values = values}}
      dd.style.width = 220
    else
      local tip = (p.min or p.max) and ("Range " .. (p.min or "?") .. " - " .. (p.max or "?")) or nil
      local tf = t.add{type = "textfield", name = name, text = tostring(v or ""), numeric = true, tooltip = tip,
        tags = {param = p.name}}
      tf.style.width = 80
    end
  end
end

function build_tab.blueprints(player, c, st)
  local ks = kinds()
  if #ks == 0 then
    wrap_label(c, "claude_bp_none", "Blueprint generator isn't available in this version of the mod.")
    return
  end
  if st.kind > #ks then st.kind = 1 end
  local items = {}
  for i, k in ipairs(ks) do items[i] = k.title or k.id end
  local dd = c.add{type = "drop-down", name = "claude_bp_kind", items = items, selected_index = st.kind}
  dd.style.horizontally_stretchable = true
  local t = c.add{type = "table", name = "claude_bp_params", column_count = 2, vertical_centering = true}
  t.style.horizontal_spacing = 12
  build_params(player, c, st)
  local b = c.add{type = "button", name = "claude_bp_cursor", caption = "Put blueprint in cursor", style = "confirm_button_without_tooltip",
    tooltip = "Free: generated by the mod. Place it like any blueprint."}
  b.style.horizontally_stretchable = true
  local m = wrap_label(c, "claude_bp_msg", st.bp_msg or "")
  m.style.font_color = {0.75, 0.75, 0.75}
end

function refresh_tab.blueprints() end

function build_tab.ask(player, c, st)
  wrap_label(c, "claude_ask_online", "", "bold_label")
  wrap_label(c, "claude_ask_status", "")
  c.add{type = "flow", name = "claude_confirm_box", direction = "vertical"}
  local sp = c.add{type = "scroll-pane", name = "claude_ask_history", vertical_scroll_policy = "auto-and-reserve-space"}
  sp.style.maximal_height = 360
  sp.style.horizontally_stretchable = true
  local tb = c.add{type = "text-box", name = "claude_ask_text", text = ""}
  tb.word_wrap = true
  tb.style.width = TEXT_W
  tb.style.height = 64
  local row = c.add{type = "flow", name = "claude_ask_row", direction = "horizontal"}
  row.style.vertical_align = "center"
  row.add{type = "button", name = "claude_ask_send", caption = "Send to Claude", style = "confirm_button_without_tooltip",
    tooltip = "Uses Claude tokens. Helper, Foreman and Blueprints tabs are free."}
  row.add{type = "label", caption = "[color=0.7,0.7,0.7]uses tokens[/color]"}
end

function refresh_tab.ask(player, c, st)
  local online = transport.companion_online and select(2, try(transport.companion_online))
  c.claude_ask_online.caption = online and "[color=0.3,0.9,0.3]Companion online[/color]" or
    "[color=1,0.35,0.35]Companion offline[/color] - launch Factorio with the Factorio (Claude) shortcut."
  local _, status = try(transport.status)
  status = online and status or nil
  c.claude_ask_status.caption = (status and status ~= "idle") and ("Claude: " .. status .. "...") or ""
  c.claude_ask_status.visible = c.claude_ask_status.caption ~= ""
  -- cost gate: Run / Cancel for each pending expensive request (clicking is free)
  local box = c.claude_confirm_box
  local _, confirms = try(transport.confirms, player)
  confirms = type(confirms) == "table" and confirms or {}
  local csig = ""
  for _, cf in ipairs(confirms) do csig = csig .. cf.id .. ";" end
  if st.confirm_sig ~= csig or #box.children ~= #confirms then
    st.confirm_sig = csig
    box.clear()
    for _, cf in ipairs(confirms) do
      local f = box.add{type = "frame", style = "bordered_frame", direction = "vertical"}
      f.style.horizontally_stretchable = true
      local l = f.add{type = "label", caption = "[color=1,0.8,0.4]" .. tostring(cf.text) .. "[/color]"}
      l.style.single_line = false
      l.style.maximal_width = TEXT_W - 24
      local row = f.add{type = "flow", direction = "horizontal"}
      row.add{type = "button", name = "claude_confirm_run", caption = "Run", style = "confirm_button_without_tooltip",
        tooltip = "Send it to Claude (uses tokens).", tags = {id = cf.id}}
      row.add{type = "button", name = "claude_confirm_cancel", caption = "Cancel", style = "back_button",
        tooltip = "Drop it - nothing is sent to Claude.", tags = {id = cf.id}}
    end
  end
  box.visible = #confirms > 0
  local sp = c.claude_ask_history
  local _, hist = try(transport.history, 30)
  hist = type(hist) == "table" and hist or {}
  -- only rebuild (and jump to the newest message) when the history actually changed, so the
  -- player can scroll back through older messages between refreshes
  local last = hist[#hist]
  local sig = #hist .. ":" .. (last and (tostring(last.tick) .. ":" .. #tostring(last.text)) or "")
  if st.ask_sig == sig and #sp.children > 0 then return end
  st.ask_sig = sig
  sp.clear()
  if #hist == 0 then
    wrap_label(sp, nil, "[color=0.7,0.7,0.7]No messages yet. Free commands: type @claude help in chat.[/color]").style.width = TEXT_W - 16
  end
  for _, h in ipairs(hist) do
    local who = h.who == "Claude" and "[color=0.85,0.47,0.34]Claude[/color]" or ("[font=default-bold]" .. tostring(h.who) .. "[/font]")
    local l = sp.add{type = "label", caption = who .. ": " .. tostring(h.text)}
    l.style.single_line = false
    l.style.width = TEXT_W - 16
    if h.cost then
      local cl = sp.add{type = "label", caption = "[color=0.55,0.55,0.55]" .. tostring(h.cost) .. "[/color]"}
      cl.style.font = "default-small"
    end
  end
  sp.scroll_to_bottom()
end

local function hex(l) return l[3] .. "," .. l[4] .. "," .. l[5] end

function build_tab.plans(player, c, st)
  wrap_label(c, nil, "[color=0.75,0.75,0.75]Plans Claude proposes show up here. Preview and Approve are free: " ..
    "approving places ghosts for the helper and robots to build.[/color]")
  local lg = c.add{type = "table", name = "claude_plans_legend", column_count = 3}
  lg.style.horizontal_spacing = 10
  lg.style.vertical_spacing = 0
  for _, l in ipairs(plans.LEGEND) do
    local lab = lg.add{type = "label", caption = "[color=" .. hex(l) .. "]■[/color] " .. l[2]}
    lab.style.font = "default-small"
  end
  local m = wrap_label(c, "claude_plans_msg", st.plan_msg or "")
  m.style.font_color = {0.75, 0.75, 0.75}
  local sp = c.add{type = "scroll-pane", name = "claude_plans_list", vertical_scroll_policy = "auto-and-reserve-space"}
  sp.style.maximal_height = 440
  sp.style.horizontally_stretchable = true
end

-- width nil = share the row with the other stretchable buttons
local function plan_button(row, action, id, caption, style, tooltip, width)
  local b = row.add{type = "button", name = "claude_plan_" .. action .. "|" .. id, caption = caption, style = style,
    tooltip = tooltip, tags = {plan_action = action, plan = id}}
  b.style.minimal_width = 0
  if width then b.style.width = width else b.style.horizontally_stretchable = true end
  return b
end

local function button_row(f)
  local row = f.add{type = "flow", direction = "horizontal"}
  row.style.horizontal_spacing = 4
  row.style.horizontally_stretchable = true
  return row
end

local function plan_card(sp, p)
  local f = sp.add{type = "frame", style = "bordered_frame", direction = "vertical"}
  f.style.horizontally_stretchable = true
  local tags = {}
  if p.variant and p.variant ~= "" then tags[#tags + 1] = "variant " .. p.variant end
  if p.stage and p.stage ~= "" then tags[#tags + 1] = p.stage end
  local head = "[font=default-bold]" .. tostring(p.title) .. "[/font]"
  if #tags > 0 then head = head .. "  [color=0.6,0.6,0.6]" .. table.concat(tags, " · ") .. "[/color]" end
  local hl = f.add{type = "label", caption = head}
  hl.style.single_line = false
  hl.style.maximal_width = TEXT_W - 30
  local lines = {}
  for i, s in ipairs(p.lines or {}) do
    if i > 8 then lines[#lines + 1] = "..."; break end
    lines[#lines + 1] = s
  end
  local facts = #p.entities .. " entities"
  if #p.deconstruct > 0 then facts = facts .. " · " .. #p.deconstruct .. " to clear" end
  local v = p.validation
  if v then
    facts = facts .. " · " .. (v.ok and "[color=0.4,0.9,0.4]validated[/color]" or
      ("[color=1,0.35,0.35]validator: " .. v.errors .. " error(s)[/color]"))
  end
  lines[#lines + 1] = "[color=0.6,0.6,0.6]" .. facts .. "[/color]"
  for i, w in ipairs(p.warnings or {}) do
    if i > 3 then break end
    lines[#lines + 1] = "[color=1,0.65,0.2]" .. w .. "[/color]"
  end
  local sup = p.status == "proposed" and p.superseded and plans.get(p.superseded)
  if p.status == "applied" then
    lines[#lines + 1] = "[color=0.4,0.9,0.4]Approved. " .. plans.result_text(p.result) .. "[/color]"
  elseif p.status == "applying" then
    lines[#lines + 1] = "[color=0.4,0.9,0.4]Approved - placing ghosts...[/color]"
  elseif p.status == "proposed" and p.result and p.result.error then
    lines[#lines + 1] = "[color=1,0.35,0.35]Placing failed: " .. p.result.error .. "[/color]"
  end
  if p.status == "proposed" and p.superseded then
    lines[#lines + 1] = "[color=0.6,0.6,0.6]Replaced by " .. (sup and tostring(sup.title) or "an approved plan") ..
      " on the same ground. Remove its ghosts first to build this one instead.[/color]"
  end
  local body = f.add{type = "label", caption = table.concat(lines, "\n")}
  body.style.single_line = false
  body.style.maximal_width = TEXT_W - 30
  local id = p.id
  local row = button_row(f)
  if p.status == "applying" then
    plan_button(row, "dismiss", id, "Dismiss", "back_button", "Remove it from this list (ghosts placed so far stay).")
    return
  end
  if p.status == "proposed" and p.superseded then
    plan_button(row, "preview", id, p.shown and "Hide" or "Preview", nil,
      p.shown and "Hide the overlay." or "Show this plan as a coloured overlay.")
    plan_button(row, "dismiss", id, "Dismiss", "back_button", "Remove it from this list.")
    return
  end
  if p.status ~= "applied" then
    plan_button(row, "preview", id, p.shown and "Hide" or "Preview", nil,
      p.shown and "Hide the overlay." or "Show this plan as a coloured overlay in the world and on the map (hides other previews).")
    local sb = row.add{type = "sprite-button", name = "claude_plan_show|" .. id, sprite = "utility/gps_map_icon",
      style = "tool_button", tooltip = "Look at it (opens remote view at the plan).", tags = {plan_action = "show", plan = id}}
    sb.style.top_margin = 2
    plan_button(row, "adjust", id, "Adjust", nil, "Tell Claude what to change (Ask Claude tab; uses tokens).")
    local row2 = button_row(f)
    local risky = v and not v.ok
    plan_button(row2, "approve", id, risky and "Approve anyway" or "Approve",
      risky and "red_confirm_button" or "confirm_button_without_tooltip",
      (risky and "The validator found errors in this plan - approving places it anyway. " or "") ..
      "Places this plan as ghosts (tagged, removable with Clear Claude's ghosts) and marks trees/rocks in the way for removal.")
    plan_button(row2, "reject", id, "Reject", "red_back_button", "Drop this plan. Nothing is built.")
  else
    plan_button(row, "build", id, "Helper: build", nil,
      "The helper builds this plan's ghosts (spawning it if needed; it fetches, crafts or mines the materials), " ..
      "then fills the hand-fed drills/inserters with coal. Free.")
    if #plans.fuel_at(p) > 0 then
      plan_button(row, "refuel", id, "Helper: refuel", nil,
        "Hand-fed burners (drills, output inserters, boilers) run about 20 minutes on 50 coal: the helper tops " ..
        "them all up to 50 coal (it gathers coal first if it has none). Free.")
    end
    local row2 = button_row(f)
    plan_button(row2, "unghost", id, "Remove its ghosts", "red_button",
      "Remove the ghosts this plan placed that aren't built yet. Your own ghosts are never touched.")
    plan_button(row2, "dismiss", id, "Dismiss", "back_button", "Remove it from this list (built parts stay).")
  end
end

function refresh_tab.plans(player, c, st)
  c.claude_plans_msg.caption = st.plan_msg or ""
  c.claude_plans_msg.visible = (st.plan_msg or "") ~= ""
  local list = plans.list(player.force.name)
  local sig = ""
  for _, p in ipairs(list) do
    sig = sig .. p.id .. ":" .. p.status .. ":" .. tostring(p.shown) .. ":" .. (p.seq or 0) .. ":" ..
      #p.entities .. ":" .. (p.result and p.result.tick or "") .. ":" .. tostring(p.superseded) .. ";"
  end
  local sp = c.claude_plans_list
  if st.plans_sig == sig and #sp.children > 0 then return end
  st.plans_sig = sig
  sp.clear()
  if #list == 0 then
    wrap_label(sp, nil, "[color=0.7,0.7,0.7]No plans yet. Ask Claude for one, e.g. \"set me up with a starter base\".[/color]")
    return
  end
  for _, p in ipairs(list) do
    local ok, err = pcall(plan_card, sp, p)
    if not ok then log("claude-bridge plan card " .. tostring(p.id) .. ": " .. tostring(err)) end
  end
end

---------------------------------------------------------------------------
-- panel lifecycle
---------------------------------------------------------------------------

local function content(player, tab)
  local p = panel(player)
  return p and find(p, "claude_content_" .. tab)
end

function gui.refresh(player, tab)
  local st = pstate(player)
  tab = tab or TABS[st.tab]
  local c = content(player, tab)
  if not c then return end
  local ok, err = pcall(refresh_tab[tab], player, c, st)
  if not ok then log("claude-bridge gui refresh " .. tab .. ": " .. tostring(err)) end
end

local update_badge  -- defined below

local function close(player)
  local st = pstate(player)
  local p = panel(player)
  if p then
    st.location = p.location
    p.destroy()
  end
  st.open = false
end

local function open(player)
  local st = pstate(player)
  if panel(player) then panel(player).destroy() end
  local f = player.gui.screen.add{type = "frame", name = "claude_panel", direction = "vertical"}
  f.style.width = WIDTH
  local scale = player.display_scale
  f.location = st.location or {x = math.floor(12 * scale), y = math.floor(96 * scale)}
  local tb = f.add{type = "flow", name = "claude_titlebar", direction = "horizontal"}
  tb.drag_target = f
  tb.add{type = "label", name = "claude_title", caption = "Claude", style = "frame_title", ignored_by_interaction = true}
  local drag = tb.add{type = "empty-widget", style = "draggable_space_header", ignored_by_interaction = true}
  drag.style.horizontally_stretchable = true
  drag.style.height = 24
  drag.style.right_margin = 4
  tb.add{type = "sprite-button", name = "claude_close", sprite = "utility/close", style = "frame_action_button",
    tooltip = "Close (Ctrl+Shift+C)"}
  local inner = f.add{type = "frame", style = "inside_deep_frame", direction = "vertical"}
  local tp = inner.add{type = "tabbed-pane", name = "claude_tabs", style = "tabbed_pane_with_no_side_padding"}
  for _, id in ipairs(TABS) do
    local tab = tp.add{type = "tab", name = "claude_tab_" .. id, caption = TAB_CAPTIONS[id]}
    tab.style.minimal_width = 0
    tab.style.left_padding = 6
    tab.style.right_padding = 6
    local c = tp.add{type = "flow", name = "claude_content_" .. id, direction = "vertical"}
    c.style.padding = 8
    c.style.vertical_spacing = 6
    c.style.horizontally_stretchable = true
    tp.add_tab(tab, c)
    local ok, err = pcall(build_tab[id], player, c, st)
    if not ok then
      log("claude-bridge gui build " .. id .. ": " .. tostring(err))
      c.clear()
      wrap_label(c, nil, "This tab failed to load: " .. tostring(err))
    end
  end
  if st.tab < 1 or st.tab > #TABS then st.tab = 1 end
  tp.selected_tab_index = st.tab
  st.open = true
  for _, id in ipairs(TABS) do gui.refresh(player, id) end
  if TABS[st.tab] == "ask" then st.seen_tick = game.tick end
  update_badge(player, st)
end

function gui.toggle(player)
  if panel(player) then close(player) else open(player) end
end

local function ensure_button(player)
  local flow = mod_gui.get_button_flow(player)
  if flow.claude_toggle then flow.claude_toggle.destroy() end
  if flow.claude_coords then flow.claude_coords.destroy() end
  flow.add{type = "sprite-button", name = "claude_toggle", sprite = "entity/character", style = mod_gui.button_style,
    tooltip = "Claude helper panel (Ctrl+Shift+C)"}
  local b = flow.add{type = "button", name = "claude_coords", caption = "", style = mod_gui.button_style,
    tooltip = "Your position (x, y) - the coordinates Claude and the helper talk about. x grows to the right (east), "
      .. "y grows DOWN (south). Click to post a map pin of where you are."}
  b.style.minimal_width = 96
  b.style.horizontally_squashable = false
end

local function coords_text(player)
  local p = player.position
  local s = math.floor(p.x) .. ", " .. math.floor(p.y)
  if player.surface.name ~= "nauvis" then s = s .. " (" .. player.surface.name .. ")" end
  return s
end

-- live position readout next to the Claude button (+ in the panel title); cheap: one caption per player
local function update_coords(player)
  local flow = mod_gui.get_button_flow(player)
  local b = flow.claude_coords
  if not b then return end
  local ps = player.mod_settings["claude-show-coords"]
  local show = not ps or ps.value
  b.visible = show
  local text = coords_text(player)
  if show then b.caption = text end
  local p = panel(player)
  local title = p and find(p, "claude_title")
  if title then title.caption = "Claude  -  you are at " .. text end
end

-- unseen Claude replies -> badge on the Ask Claude tab; unseen proposed plans -> badge on Plans
function update_badge(player, st)
  local p = panel(player)
  local tab = p and find(p, "claude_tab_ask")
  if not tab then return end
  local n = 0
  if TABS[st.tab] ~= "ask" then
    local _, hist = try(transport.history, 8)
    for _, h in ipairs(type(hist) == "table" and hist or {}) do
      if h.who == "Claude" and h.tick > (st.seen_tick or 0) then n = n + 1 end
    end
  end
  tab.badge_text = n > 0 and tostring(n) or ""
  local ptab = find(p, "claude_tab_plans")
  if not ptab then return end
  local np = 0
  if TABS[st.tab] == "plans" then
    st.plans_seen = storage.plan_seq or 0
  else
    for _, pl in ipairs(plans.list(player.force.name)) do
      if pl.status == "proposed" and (pl.seq or 0) > (st.plans_seen or 0) then np = np + 1 end
    end
  end
  ptab.badge_text = np > 0 and tostring(np) or ""
end

---------------------------------------------------------------------------
-- handlers (shared by real events and the gui_click test op)
---------------------------------------------------------------------------

local function read_count(player)
  local f = content(player, "helper")
  local n = f and f.claude_mine_row and tonumber(f.claude_mine_row.claude_mine_count.text)
  if not n or n < 1 then n = 50 end
  n = math.min(math.floor(n), 5000)
  pstate(player).count = n
  return n
end

local function quick(player, action)
  local st = pstate(player)
  if not access.can_ask(player) then
    st.msg = access.denied_text()
    return gui.refresh(player, "helper")
  end
  local opts = {}
  for _, o in ipairs(ORES) do if o[1] == action then opts.count = read_count(player) end end
  local ok, msg = try(agent.quick, player, action, opts)
  st.msg = ok and tostring(msg or "") or ("Error: " .. tostring(msg))
  gui.refresh(player, "helper")
end

local function read_params(player, st)
  local kind = kinds()[st.kind]
  if not kind then return nil end
  local c = content(player, "blueprints")
  local t = c and c.claude_bp_params
  local out = {}
  for _, p in ipairs(kind.params or {}) do
    local el = t and t["claude_bp_p_" .. p.name]
    local v = p.default
    if el and p.type == "choice" then
      local ch = (p.choices or {})[el.selected_index]
      if ch ~= nil then v = choice_value(ch) end
    elseif el then
      local n = tonumber(el.text)
      if n then
        n = math.floor(n)
        if p.min then n = math.max(p.min, n) end
        if p.max then n = math.min(p.max, n) end
        v = n
        el.text = tostring(n)
      end
    end
    out[p.name] = v
  end
  st.params[kind.id] = out
  return kind, out
end

local function bp_cursor(player)
  local st = pstate(player)
  local kind, params = read_params(player, st)
  if not kind then
    st.bp_msg = "Pick a blueprint kind first."
  else
    local ok, res, err = try(blueprint_gen.to_cursor, player, kind.id, params)
    if ok and res then
      st.bp_msg = (kind.title or kind.id) .. " is in your cursor."
    else
      st.bp_msg = "Couldn't make it: " .. tostring(err or res)
    end
  end
  local c = content(player, "blueprints")
  if c and c.claude_bp_msg then c.claude_bp_msg.caption = st.bp_msg end
end

local function send_ask(player)
  local c = content(player, "ask")
  local tb = c and c.claude_ask_text
  if not tb then return end
  local text = tb.text:gsub("^%s+", ""):gsub("%s+$", "")
  if text == "" then return end
  tb.text = ""
  try(transport.ask, player, text, "gui")  -- fires on_change -> refresh
  pstate(player).seen_tick = game.tick
end

-- "Clear Claude's ghosts": first click = dry run + confirmation row, then Remove / Cancel
local function clear_ghosts(player, step)
  local st = pstate(player)
  if not access.can_ask(player) then
    st.clear_pending = nil
    st.msg = access.denied_text()
    return gui.refresh(player, "helper")
  end
  if step == "ask" then
    local r = api.call("clear_claude_ghosts", {dry_run = true})
    if not r.ok then
      st.msg = "Couldn't check ghosts: " .. tostring(r.error)
    elseif r.result.removed == 0 then
      st.msg = "No Claude ghosts to remove." ..
        (r.result.kept_player > 0 and (" (" .. r.result.kept_player .. " of your own ghosts left alone.)") or "")
    else
      st.clear_pending = {removed = r.result.removed, kept_player = r.result.kept_player}
      st.msg = ""
    end
  elseif step == "yes" and st.clear_pending then
    st.clear_pending = nil
    local r = api.call("clear_claude_ghosts", {})
    st.msg = r.ok and ("Removed " .. r.result.removed .. " Claude ghost(s)." ..
      (r.result.kept_player > 0 and (" Kept your " .. r.result.kept_player .. ".") or "")) or
      ("Clear failed: " .. tostring(r.error))
  else
    st.clear_pending = nil
    st.msg = "Nothing removed."
  end
  gui.refresh(player, "helper")
end

local function answer_confirm(player, el, run)
  local id = el.tags and el.tags.id
  local ok, done, msg = try(transport.confirm, player, id, run)
  if ok and not done then player.print(tostring(msg)) end
  gui.refresh(player, "ask")
end

local select_tab

-- Plans tab buttons (all free except Adjust, which only pre-fills the Ask box)
local function plan_click(player, el)
  local st = pstate(player)
  local action, id = el.tags.plan_action, el.tags.plan
  local p = plans.get(id)
  if not p then
    st.plan_msg = "That plan is gone."
    return gui.refresh(player, "plans")
  end
  local title = tostring(p.title)
  local needs = (action == "approve") and access.can_confirm or
    ((action ~= "preview" and action ~= "show") and access.can_ask or nil)
  if needs and not needs(player) then
    st.plan_msg = action == "approve" and
      ("Only the host" .. (access.policy() == "host-only" and "" or " or an admin") .. " can approve plans here.") or
      access.denied_text()
    return gui.refresh(player, "plans")
  end
  if action == "preview" then
    if p.shown then plans.hide(id); st.plan_msg = "" else
      plans.show(id)
      st.plan_msg = "Showing " .. title .. " (also on the map). Legend above."
    end
  elseif action == "show" then
    local bb = p.bbox
    if bb then
      local pos = {x = (bb[1] + bb[3]) / 2, y = (bb[2] + bb[4]) / 2}
      if not p.shown and p.status ~= "applied" then plans.show(id) end
      local ok = pcall(player.set_controller, {type = defines.controllers.remote, position = pos, surface = p.surface})
      if not ok then
        player.print("[gps=" .. math.floor(pos.x) .. "," .. math.floor(pos.y) .. "," .. p.surface .. "]")
      end
    end
  elseif action == "approve" then
    local ok, r = pcall(plans.apply, id)
    if ok then
      st.plan_msg = "Approved " .. title .. ". " .. (r.pending and ("Placing " .. r.total .. " ghosts...") or
        plans.result_text(r))
      -- the helper starts building right away (free); the companion only gets told
      local hok, hmsg = pcall(agent.work_area, player, p.bbox, "build", title, plans.fuel_at(p))
      st.plan_msg = st.plan_msg .. " " .. (hok and tostring(hmsg) or "")
      if not hok then log("claude-bridge plan build: " .. tostring(hmsg)) end
      try(transport.ask, player, "approved " .. id, "plan")
    else
      st.plan_msg = "Couldn't apply it: " .. tostring(r)
    end
  elseif action == "reject" then
    plans.drop(id)
    st.plan_msg = "Rejected " .. title .. ". Nothing was built."
  elseif action == "dismiss" then
    plans.drop(id)
    st.plan_msg = ""
  elseif action == "build" or action == "refuel" then
    local ok, msg = pcall(agent.work_area, player, p.bbox, action, title, plans.fuel_at(p))
    st.plan_msg = ok and tostring(msg) or ("Couldn't start the helper: " .. tostring(msg))
  elseif action == "unghost" then
    plans.unsupersede(id)
    local r = api.call("clear_claude_ghosts", {plan = id, force = p.force})
    st.plan_msg = r.ok and ("Removed " .. r.result.removed .. " unbuilt ghost(s) of " .. title .. ".") or
      ("Couldn't remove them: " .. tostring(r.error))
  elseif action == "adjust" then
    local tp = panel(player) and find(panel(player), "claude_tabs")
    for i, t in ipairs(TABS) do
      if t == "ask" and tp then tp.selected_tab_index = i; select_tab(player, i) end
    end
    local c = content(player, "ask")
    local tb = c and c.claude_ask_text
    if tb then
      tb.text = "Adjust plan " .. id .. " (" .. title .. "): "
      tb.focus()
    end
    return
  end
  gui.refresh(player, "plans")
end

function select_tab(player, index)
  local st = pstate(player)
  st.tab = index
  local id = TABS[index]
  if id == "ask" then st.seen_tick = game.tick end
  update_badge(player, st)
  if id then gui.refresh(player, id) end
end

local function on_click(e)
  local el = e.element
  if not (el and el.valid) then return end
  local name = el.name
  if name:sub(1, 7) ~= "claude_" then return end
  local player = game.get_player(e.player_index)
  if not player then return end
  if name == "claude_toggle" or name == "claude_close" then return gui.toggle(player) end
  if name == "claude_coords" then
    player.print("You are here: [gps=" .. math.floor(player.position.x) .. "," .. math.floor(player.position.y) ..
      "," .. player.surface.name .. "]")
    return
  end
  if name == "claude_show_helper" then
    local _, a = try(agent.get)
    local st = pstate(player)
    if a and a.entity and a.entity.valid then
      local hp = a.entity.position
      util.mark(a.entity.surface, hp, "Claude helper", 20)
      player.print({"", "[color=0.85,0.47,0.34][Claude helper][/color] I'm here: [gps=", math.floor(hp.x), ",",
        math.floor(hp.y), ",", a.entity.surface.name, "]"})
      st.msg = "Helper marked on the map and in the world for 20 s (click the pin in chat to open the map)."
    else
      st.msg = "No helper yet - press Spawn."
    end
    return gui.refresh(player, "helper")
  end
  local action = name:match("^claude_act_(.+)$")
  if action then return quick(player, action) end
  if name == "claude_foreman_scan" then
    try(foreman.scan, player.force)
    return gui.refresh(player, "foreman")
  end
  if name:match("^claude_alert_%d+$") then
    local t = el.tags
    player.print("[gps=" .. t.x .. "," .. t.y .. "," .. t.surface .. "]")
    return
  end
  if name == "claude_bp_cursor" then return bp_cursor(player) end
  if name == "claude_ask_send" then return send_ask(player) end
  if name == "claude_clear_ghosts" then return clear_ghosts(player, "ask") end
  if name == "claude_clear_yes" then return clear_ghosts(player, "yes") end
  if name == "claude_clear_no" then return clear_ghosts(player, "no") end
  if name == "claude_confirm_run" then return answer_confirm(player, el, true) end
  if name == "claude_confirm_cancel" then return answer_confirm(player, el, false) end
  if el.tags and el.tags.plan_action then return plan_click(player, el) end
end

local function on_selection(e)
  local el = e.element
  if not (el and el.valid) then return end
  local player = game.get_player(e.player_index)
  if not player then return end
  if el.name == "claude_bp_kind" then
    local st = pstate(player)
    read_params(player, st)  -- remember edits of the old kind
    st.kind = el.selected_index
    st.bp_msg = ""
    local c = content(player, "blueprints")
    build_params(player, c, st)
    c.claude_bp_msg.caption = ""
  end
end

local function on_confirmed(e)
  local el = e.element
  if not (el and el.valid) then return end
  local player = game.get_player(e.player_index)
  if not player then return end
  if el.name == "claude_ask_text" then return send_ask(player) end
  if el.name == "claude_mine_count" then return read_count(player) end
  if el.name:match("^claude_bp_p_") then return bp_cursor(player) end
end

local function on_tab_changed(e)
  local el = e.element
  if not (el and el.valid and el.name == "claude_tabs") then return end
  local player = game.get_player(e.player_index)
  if player then select_tab(player, el.selected_tab_index) end
end

-- a GUI bug must never crash the game: log it instead
local function safe(fn)
  return function(e)
    local ok, err = pcall(fn, e)
    if not ok then log("claude-bridge gui handler: " .. tostring(err)) end
  end
end

events.on(defines.events.on_gui_click, safe(on_click))
events.on(defines.events.on_gui_selection_state_changed, safe(on_selection))
events.on(defines.events.on_gui_confirmed, safe(on_confirmed))
events.on(defines.events.on_gui_selected_tab_changed, safe(on_tab_changed))
events.on(defines.events.on_gui_location_changed, function(e)
  if e.element and e.element.valid and e.element.name == "claude_panel" then
    pstate(game.get_player(e.player_index)).location = e.element.location
  end
end)
events.on("claude-toggle-gui", function(e)
  local player = game.get_player(e.player_index)
  if player then gui.toggle(player) end
end)

events.on(defines.events.on_player_created, function(e)
  local player = game.get_player(e.player_index)
  if player then ensure_button(player) end
end)
events.on(defines.events.on_player_joined_game, function(e)
  local player = game.get_player(e.player_index)
  if not player then return end
  local flow = mod_gui.get_button_flow(player)
  if not (flow.claude_toggle and flow.claude_coords) then ensure_button(player) end
  update_coords(player)
  local st = pstate(player)
  if st.reopen then
    st.reopen = nil
    if not panel(player) then open(player) end
  end
end)
events.on(defines.events.on_player_removed, function(e)
  if storage.gui then storage.gui[e.player_index] = nil end
end)

events.on_init(function()
  storage.gui = storage.gui or {}
  for _, player in pairs(game.players) do ensure_button(player) end
end)

-- mod update / save from another version: rebuild everything from scratch
events.on_configuration_changed(function()
  storage.gui = storage.gui or {}
  for _, player in pairs(game.players) do
    local st = pstate(player)
    local was_open = panel(player) ~= nil or st.open
    if panel(player) then
      st.location = panel(player).location
      panel(player).destroy()
    end
    ensure_button(player)
    st.open = false
    if was_open then
      if player.connected then open(player) else st.reopen = true end  -- reopen on next join
    end
  end
end)

events.on_nth_tick(30, function()
  for _, player in pairs(game.connected_players) do
    local ok, err = pcall(update_coords, player)
    if not ok then log("claude-bridge coords: " .. tostring(err)) end
  end
end)

-- helper status etc.: only while open, only the visible tab
events.on_nth_tick(60, function()
  if not storage.gui then return end
  for _, player in pairs(game.connected_players) do
    local st = storage.gui[player.index]
    if st and st.open then
      if panel(player) then gui.refresh(player) else st.open = false end
    end
  end
end)

transport.on_change = function()
  if not storage.gui then return end
  for _, player in pairs(game.connected_players) do
    local st = storage.gui[player.index]
    if st and st.open and panel(player) then
      gui.refresh(player, "ask")
      if TABS[st.tab] == "ask" then st.seen_tick = game.tick end
      update_badge(player, st)
    end
  end
end

plans.on_change = function()
  if not storage.gui then return end
  for _, player in pairs(game.connected_players) do
    local st = storage.gui[player.index]
    if st and st.open and panel(player) then
      gui.refresh(player, "plans")
      update_badge(player, st)
    end
  end
end

---------------------------------------------------------------------------
-- test ops
---------------------------------------------------------------------------

local function gui_player(args)
  local p = util.get_player(args)
  if not p then error("no player") end
  return p
end

-- {player, name, value?}: same handler path as a real click / selection / text confirm / tab switch
api.register_debug("gui_click", function(args)
  local player = gui_player(args)
  local root = args.name == "claude_toggle" and mod_gui.get_button_flow(player) or panel(player)
  local el = root and find(root, args.name)
  if not el then error("no element " .. tostring(args.name) .. (panel(player) and "" or " (panel closed)")) end
  local e = {element = el, player_index = player.index, name = args.name, tick = game.tick}
  local etype = el.type
  local v = args.value
  if el.type == "drop-down" or el.type == "list-box" then
    if type(v) == "string" then  -- match a choice value, else item text
      local vals = el.tags.values or {}
      for i, it in ipairs(el.items) do
        if vals[i] == v or (type(it) == "string" and it:find(v, 1, true)) then v = i; break end
      end
    end
    if type(v) ~= "number" then error("value must be an index or item text") end
    el.selected_index = v
    on_selection(e)
  elseif el.type == "textfield" or el.type == "text-box" then
    if v ~= nil then el.text = tostring(v) end
    on_confirmed(e)
  elseif el.type == "tabbed-pane" then
    el.selected_tab_index = v
    on_tab_changed(e)
  elseif el.type == "tab" then
    local tp = el.parent
    for i, t in ipairs(tp.tabs) do
      if t.tab == el then tp.selected_tab_index = i end
    end
    on_tab_changed{element = tp, player_index = player.index}
  else
    on_click(e)
  end
  return {type = etype, panel_open = panel(player) ~= nil}
end)

local function dump(el)
  local o = {name = el.name ~= "" and el.name or nil, type = el.type}
  if el.visible == false then o.visible = false end
  local ok, cap = pcall(function() return el.caption end)
  if ok and cap and cap ~= "" then o.caption = cap end
  if el.type == "textfield" or el.type == "text-box" then o.text = el.text end
  if el.type == "drop-down" or el.type == "list-box" then o.items = el.items; o.selected_index = el.selected_index end
  if el.type == "tabbed-pane" then o.selected_tab_index = el.selected_tab_index end
  if el.type == "tab" and el.badge_text and el.badge_text ~= "" then o.badge = el.badge_text end
  if el.type == "sprite-button" then o.sprite = el.sprite; o.number = el.number end
  local kids = el.children
  if #kids > 0 then
    o.children = {}
    for i, c in ipairs(kids) do o.children[i] = dump(c) end
  end
  return o
end

-- {player}: element tree of the toggle button + panel
api.register_debug("gui_dump", function(args)
  local player = gui_player(args)
  local flow = mod_gui.get_button_flow(player)
  local p = panel(player)
  return {button = flow.claude_toggle and dump(flow.claude_toggle) or nil, panel = p and dump(p) or nil,
    location = p and p.location or nil, state = storage.gui and storage.gui[player.index]}
end)

return gui
