-- Claude Bridge: free in-game helper/foreman/blueprints + a JSON API for Claude (RCON `/claude <json>` or UDP).
-- Usage (RCON): /claude {"op":"overview","args":{}} -> {"ok":true,"result":...} or {"ok":false,"error":"..."}

local events = require("scripts.events")
require("scripts.util")
require("scripts.api")
local transport = require("scripts.transport")
require("scripts.agent")
transport.local_commands = require("scripts.local_commands")
require("scripts.foreman")
require("scripts.blueprint_gen")
require("scripts.export_map")
require("scripts.export_data")
require("scripts.plans")
require("scripts.gui")

events.finalize()
