"""The companion's MCP server must reach the same game + script-output + plans dir as the companion (test slots)."""
import json

from factorio_claude import companion


def test_mcp_config_passes_test_env(tmp_path, monkeypatch):
    for k, v in {"FACTORIO_UDP_PORT": "34511", "FACTORIO_RCON_PORT": "27027", "FACTORIO_SCRIPT_OUTPUT": "X:/so",
                 "FACTORIO_PLAN_UNCHARTED": "1", "FACTORIO_PLANS_DIR": "X:/plans", "FACTORIO_FAKE_CLAUDE": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(companion, "MCP_CONFIG", tmp_path / "mcp.json")
    companion.write_mcp_config()
    env = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["factorio"]["env"]
    assert env["FACTORIO_UDP_PORT"] == "34511" and env["FACTORIO_RCON_PORT"] == "27027"
    assert env["FACTORIO_SCRIPT_OUTPUT"] == "X:/so" and env["FACTORIO_PLAN_UNCHARTED"] == "1"
    assert env["FACTORIO_PLANS_DIR"] == "X:/plans"
    assert "FACTORIO_FAKE_CLAUDE" not in env
