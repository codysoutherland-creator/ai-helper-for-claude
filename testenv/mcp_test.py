import asyncio, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession
async def main():
    # slot 6 test instance: UDP 34506 (primary), RCON 27022 (fallback / run_lua)
    env = dict(os.environ, FACTORIO_UDP_PORT=os.environ.get("FACTORIO_UDP_PORT", "34506"),
               FACTORIO_RCON_PORT=os.environ.get("FACTORIO_RCON_PORT", "27022"), FACTORIO_RCON_PASSWORD="testpw")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "factorio_claude.server"], cwd=str(ROOT), env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(len(tools), "tools:", ", ".join(t.name for t in tools))
            for name, args in [("factorio_overview", {}), ("factorio_map", {"x": -60, "y": 120, "radius": 6}),
                               ("blueprint_generate", {"kind": "smelter_column", "params": {"count": 2}, "copy": False}),
                               ("factorio_agent_status", {}), ("factorio_foreman_report", {"rescan": True}),
                               ("factorio_quick_action", {"action": "spawn"}), ("factorio_chat_pop", {}),
                               ("factorio_run_lua", {"code": "rcon.print(game.tick)"})]:
                res = await s.call_tool(name, args)
                txt = res.content[0].text
                print(f"--- {name} ({len(txt)} chars):\n{txt[:600]}")
asyncio.run(main())
