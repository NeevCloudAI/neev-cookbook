import asyncio
import importlib
from types import SimpleNamespace

import pytest

import mcp_agent
from mcp_agent import AGENT_TOOLS, MCP_URL, host_call, sandbox_server


class FakeServer:
    """Records host calls and answers each with the given error flag."""

    def __init__(self, is_error):
        self.is_error = is_error
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(is_error=self.is_error, content=[SimpleNamespace(text="no sandbox is bound")])


def test_agent_sees_only_the_working_tools():
    assert sandbox_server("key", "box").tool_filter == {"allowed_tool_names": ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove"]}
    assert "create_sandbox" not in AGENT_TOOLS and "delete_sandbox" not in AGENT_TOOLS


def test_allow_processes_adds_the_process_tools(monkeypatch):
    monkeypatch.setenv("ALLOW_PROCESSES", "1")
    reloaded = importlib.reload(mcp_agent)
    try:
        assert reloaded.AGENT_TOOLS == ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove", "process_start", "process_get", "process_logs", "process_kill", "process_list"]
    finally:
        monkeypatch.delenv("ALLOW_PROCESSES")
        importlib.reload(mcp_agent)


def test_headers_carry_the_key_and_pick_the_sandbox():
    params = sandbox_server("key", "box").params
    assert params["url"] == MCP_URL
    assert params["headers"] == {"Authorization": "Bearer key", "x-sandbox-name": "box"}


def test_host_call_sends_no_arguments():
    server = FakeServer(is_error=False)
    asyncio.run(host_call(server, "create_sandbox"))
    assert server.calls == [("create_sandbox", {})]


def test_host_call_raises_on_a_tool_error():
    with pytest.raises(RuntimeError, match="delete_sandbox: no sandbox is bound"):
        asyncio.run(host_call(FakeServer(is_error=True), "delete_sandbox"))
