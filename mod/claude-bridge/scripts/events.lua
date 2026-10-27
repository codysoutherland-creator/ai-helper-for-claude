-- Tiny multi-handler event dispatcher: modules register here, control.lua calls finalize() once.
-- Handlers run in registration order (= require order in control.lua).

local events = {}

local handlers = {}  -- event id/name -> {fn...}
local nth = {}       -- n -> {fn...}
local init, loads, config = {}, {}, {}

-- event: defines.events.* or a custom-input name (string)
function events.on(event, fn)
  local list = handlers[event]
  if not list then list = {}; handlers[event] = list end
  list[#list + 1] = fn
end

function events.on_nth_tick(n, fn)
  local list = nth[n]
  if not list then list = {}; nth[n] = list end
  list[#list + 1] = fn
end

function events.on_init(fn) init[#init + 1] = fn end
function events.on_load(fn) loads[#loads + 1] = fn end
function events.on_configuration_changed(fn) config[#config + 1] = fn end

local function run_all(list)
  if #list == 1 then return list[1] end
  return function(e) for i = 1, #list do list[i](e) end end
end

function events.finalize()
  for event, list in pairs(handlers) do script.on_event(event, run_all(list)) end
  for n, list in pairs(nth) do script.on_nth_tick(n, run_all(list)) end
  if #init > 0 then script.on_init(run_all(init)) end
  if #loads > 0 then script.on_load(run_all(loads)) end
  if #config > 0 then script.on_configuration_changed(run_all(config)) end
end

return events
