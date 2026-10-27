-- Free rule-based base monitor: periodic cheap scans -> report + native alerts for NEW problems only.
-- report = {tick, force, alerts = {{severity, kind, key, text, count, surface, position}}, stats = {...}}

local events = require("scripts.events")
local util = require("scripts.util")
local api = require("scripts.api")
local round, pos_out, status_names = util.round, util.pos_out, util.status_names

local foreman = {}
foreman.on_change = function(force, report) end  -- gui.lua may override (fires after every scan)

-- One find per surface covers everything the rules need.
local SCAN_TYPES = {"assembling-machine", "furnace", "mining-drill", "lab", "boiler", "burner-generator", "reactor"}
local CRAFTERS = {["assembling-machine"] = true, furnace = true}
local SEVERITY_ORDER = {high = 1, medium = 2, info = 3}
local SHORTAGE = 0.98      -- network satisfaction below this = power_shortage
local MIN_OUTPUT_FULL = 3
local MIN_STARVED = 5

---------------------------------------------------------------------------
-- settings (cached; re-read on change)
---------------------------------------------------------------------------

local cfg
local function config()
  if not cfg then
    local g = settings.global
    cfg = {enabled = g["claude-foreman-enabled"].value, interval = g["claude-foreman-interval"].value * 60,
      chat = g["claude-foreman-chat"].value}
  end
  return cfg
end

events.on(defines.events.on_runtime_mod_setting_changed, function(e)
  if e.setting:sub(1, 15) == "claude-foreman-" then cfg = nil end
end)

local function init()
  storage.foreman = storage.foreman or {}
  local s = storage.foreman
  s.reports = s.reports or {}   -- force name -> last report
  s.active = s.active or {}     -- force name -> {key -> {entity, text}} alerts currently raised
  s.last_scan = s.last_scan or {}  -- force name -> tick
  storage.foreman_report = nil  -- v0.2 stub leftover
end
events.on_init(init)
events.on_configuration_changed(init)

---------------------------------------------------------------------------
-- scan
---------------------------------------------------------------------------

-- {name -> n} -> "stone-furnace x3, boiler x1" (biggest first)
local function names_text(by_name)
  local rows = {}
  for n, c in pairs(by_name) do rows[#rows + 1] = {n, c} end
  table.sort(rows, function(a, b) return a[2] > b[2] end)
  local out = {}
  for i = 1, math.min(3, #rows) do out[i] = rows[i][1] .. " x" .. rows[i][2] end
  if #rows > 3 then out[#out + 1] = "..." end
  return table.concat(out, ", ")
end

-- Per-surface bucket of entities with one status: count, by-name counts, first entity as sample.
local function bump(buckets, key, e)
  local b = buckets[key]
  if not b then b = {count = 0, by_name = {}, sample = e}; buckets[key] = b end
  b.count = b.count + 1
  b.by_name[e.name] = (b.by_name[e.name] or 0) + 1
end

local function has_items(e, inv_id)
  local inv = e.get_inventory(inv_id)
  return inv and not inv.is_empty()
end

local function network_of(e)
  local ok, sub = pcall(function() return e.electric_network end)
  return ok and sub and sub.valid and sub.parent_network or nil
end

-- any technology left that labs could research (trigger techs don't use labs)
local function lab_research_left(force)
  for _, t in pairs(force.technologies) do
    if t.enabled and not t.researched and not t.prototype.research_trigger then return true end
  end
  return false
end

-- -> report; also returns {key -> sample entity} for alerting
local function do_scan(force)
  local alerts, samples = {}, {}
  local stats = {machines = 0, working = 0, labs = 0, networks = {}, scanned_surfaces = 0}
  local function add(severity, kind, key, text, count, surface, e)
    alerts[#alerts + 1] = {severity = severity, kind = kind, key = key, text = text, count = count,
      surface = surface.name, position = pos_out(e.position)}
    samples[key] = e
  end

  local lab_sample  -- first lab on any surface (research_idle is one force-wide alert)
  for _, surface in pairs(game.surfaces) do
    local found = surface.find_entities_filtered{force = force, type = SCAN_TYPES}
    if #found > 0 then
      stats.scanned_surfaces = stats.scanned_surfaces + 1
      local b = {}          -- status bucket -> {count, by_name, sample}
      local nets = {}       -- electric network id -> {sample, low = bucket}
      for i = 1, #found do
        local e = found[i]
        stats.machines = stats.machines + 1
        local st = status_names[e.status] or "unknown"
        if st == "working" or st == "normal" then stats.working = stats.working + 1 end
        if e.type == "lab" then stats.labs = stats.labs + 1; lab_sample = lab_sample or e end
        local nid = e.electric_network_id
        if nid and not nets[nid] then nets[nid] = {sample = e, low = 0} end
        if st == "no_fuel" then
          -- an empty, unused furnace isn't a problem; one with ore waiting is
          if not CRAFTERS[e.type] or has_items(e, defines.inventory.crafter_input) then bump(b, "no_fuel", e) end
        elseif st == "no_minable_resources" then bump(b, "depleted", e)
        elseif st == "no_power" then bump(b, "no_power", e)
        elseif st == "low_power" then bump(b, "low_power", e); if nid then nets[nid].low = nets[nid].low + 1 end
        elseif st == "full_output" and CRAFTERS[e.type] then bump(b, "output_full", e)
        elseif (st == "no_ingredients" or st == "item_ingredient_shortage") and CRAFTERS[e.type] then
          -- furnaces idle on an empty belt are normal; only count machines with a recipe set
          if e.type == "assembling-machine" and e.get_recipe() then bump(b, "starved", e) end
        end
      end

      -- power: one network object per id (sampled from a consumer on it). Ids change when poles are rebuilt,
      -- so the alert is one per surface (worst network); zero production is left to the no_power alert.
      local shortage_low, worst, short_nets = 0, nil, 0
      for nid, n in pairs(nets) do
        local net = network_of(n.sample)
        if net and net.valid then
          local f = net.flow_last_tick
          local st5 = net.statistics
          local prod, cons = 0, 0
          for name in pairs(st5.output_counts) do
            prod = prod + st5.get_flow_count{name = name, category = "output", precision_index = defines.flow_precision_index.five_seconds}
          end
          for name in pairs(st5.input_counts) do
            cons = cons + st5.get_flow_count{name = name, category = "input", precision_index = defines.flow_precision_index.five_seconds}
          end
          local sat = f.consumption_satisfaction or 1
          local row = {id = nid, surface = surface.name, satisfaction = round(sat, 3),
            production_MW = round(prod * 60 / 1e6, 2), consumption_MW = round(cons * 60 / 1e6, 2),
            demand_MW = round((f.maximum_consumption or 0) * 60 / 1e6, 2)}
          stats.networks[#stats.networks + 1] = row
          if (f.maximum_consumption or 0) > 0 and sat < SHORTAGE and (f.maximum_production or 0) > 0 then
            shortage_low = shortage_low + n.low
            short_nets = short_nets + 1
            if not worst or sat < worst.sat then worst = {sat = sat, row = row, sample = n.sample} end
          end
        end
      end
      if worst then
        local r = worst.row
        add("high", "power_shortage", "power_shortage|" .. surface.name,
          string.format("Power shortage on %s: %d%% satisfied (%.2f MW produced, %.2f MW demanded)%s",
            surface.name, math.floor(worst.sat * 100), r.production_MW, r.demand_MW,
            short_nets > 1 and (" + " .. (short_nets - 1) .. " more network(s)") or ""),
          short_nets, surface, worst.sample)
      end

      local function bucket_alert(kind, severity, min, fmt)
        local x = b[kind]
        if x and x.count >= min then
          add(severity, kind, kind .. "|" .. surface.name,
            string.format(fmt, x.count, surface.name) .. " (" .. names_text(x.by_name) .. ")", x.count, surface, x.sample)
        end
      end
      bucket_alert("no_power", "high", 1, "%d machines have no power on %s")
      bucket_alert("no_fuel", "high", 1, "%d burner machines out of fuel on %s")
      -- low_power already explained by a power_shortage alert is not repeated
      if b.low_power and b.low_power.count - shortage_low > 0 then
        b.low_power.count = b.low_power.count - shortage_low
        bucket_alert("low_power", "medium", 1, "%d machines on low power on %s")
      end
      bucket_alert("depleted", "medium", 1, "%d mining drills depleted on %s")
      bucket_alert("output_full", "medium", MIN_OUTPUT_FULL, "%d machines blocked, output full on %s")
      bucket_alert("starved", "info", MIN_STARVED, "%d machines waiting for ingredients on %s")
    end
  end
  if lab_sample and force.research_enabled and not force.current_research and lab_research_left(force) then
    add("medium", "research_idle", "research_idle", "Labs are idle: no research queued", stats.labs,
      lab_sample.surface, lab_sample)
  end

  table.sort(alerts, function(a, b2)
    local sa, sb = SEVERITY_ORDER[a.severity], SEVERITY_ORDER[b2.severity]
    if sa ~= sb then return sa < sb end
    return a.count > b2.count
  end)
  local cur = force.current_research
  stats.research = cur and cur.name or nil
  return {tick = game.tick, force = force.name, alerts = alerts, stats = stats}, samples
end

---------------------------------------------------------------------------
-- alerting: only alerts whose key wasn't active in the previous scan
---------------------------------------------------------------------------

local function icon_for(e)
  return {type = "entity", name = e.name}
end

local function raise(force, alert, e)
  if alert.severity ~= "info" and e.valid then
    local msg = "[Foreman] " .. alert.text
    if not pcall(force.add_custom_alert, e, icon_for(e), msg, true) then
      pcall(force.add_custom_alert, e, {type = "virtual", name = "signal-alert"}, msg, true)
    end
  end
  if config().chat then
    force.print("[Foreman] " .. alert.text, {color = util.CLAUDE_COLOR, skip = defines.print_skip.never})
  end
end

local function clear(force, old)
  local e = old.entity
  if e and e.valid then pcall(force.remove_alert, {entity = e, type = defines.alert_type.custom}) end
end

function foreman.scan(force)
  force = force or game.forces.player
  if not storage.foreman then init() end
  local s = storage.foreman
  local report, samples = do_scan(force)
  local prev = s.active[force.name] or {}
  local now = {}
  local new = 0
  for _, a in pairs(report.alerts) do
    local e = samples[a.key]
    if prev[a.key] then
      now[a.key] = prev[a.key]
      a.new = nil
    else
      now[a.key] = {entity = a.severity ~= "info" and e or nil, tick = game.tick}
      a.new = true
      new = new + 1
      raise(force, a, e)
    end
  end
  for key, old in pairs(prev) do
    if not now[key] then clear(force, old) end
  end
  report.new_alerts = new
  s.active[force.name] = now
  s.reports[force.name] = report
  s.last_scan[force.name] = game.tick
  local ok, err = pcall(foreman.on_change, force, report)
  if not ok then log("foreman.on_change: " .. tostring(err)) end
  return report
end

function foreman.get_report(force)
  force = force or game.forces.player
  local r = storage.foreman and storage.foreman.reports[force.name]
  return r or foreman.scan(force)
end

-- Scheduler: check once a second; scan forces with connected players when their interval elapsed.
events.on_nth_tick(60, function()
  local c = config()
  if not c.enabled then return end
  local s = storage.foreman
  if not s then return end
  for _, force in pairs(game.forces) do
    if #force.connected_players > 0 and game.tick - (s.last_scan[force.name] or 0) >= c.interval then
      foreman.scan(force)
    end
  end
end)

-- Cached report; rescan=true forces a scan now (works with no players / foreman disabled).
api.register("foreman_report", function(args)
  local force = util.get_force(args)
  if not force then error("no force " .. tostring(args.force)) end
  if args.rescan then return foreman.scan(force) end
  return foreman.get_report(force)
end)

return foreman
