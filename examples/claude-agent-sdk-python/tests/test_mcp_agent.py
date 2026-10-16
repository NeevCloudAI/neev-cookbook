import asyncio
import importlib
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

import mcp_agent
from mcp_agent import ALLOWED, MCP_URL, build_options, host_call

PUBLISHED = [
    "exec",
    "fs_read",
    "fs_write",
    "fs_list",
    "fs_exists",
    "fs_move",
    "fs_remove",
    "process_start",
    "create_sandbox",
    "delete_sandbox",
]


def test_server_is_remote_http_bound_to_one_sandbox():
    opts = build_options("key", "box", PUBLISHED)
    assert opts.mcp_servers == {
        "neev": {
            "type": "http",
            "url": MCP_URL,
            "headers": {"Authorization": "Bearer key", "x-sandbox-name": "box"},
        }
    }
    assert opts.strict_mcp_config is True


def test_built_ins_are_off_and_only_the_working_tools_are_allowed():
    opts = build_options("key", "box", PUBLISHED)
    assert opts.tools == []
    assert opts.allowed_tools == [f"mcp__neev__{n}" for n in ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove"]]


def test_settings_are_isolated_and_unlisted_tools_denied():
    opts = build_options("key", "box", PUBLISHED)
    assert opts.setting_sources == []
    assert opts.permission_mode == "dontAsk"


def test_everything_else_the_server_publishes_is_disallowed():
    opts = build_options("key", "box", PUBLISHED)
    assert opts.disallowed_tools == [
        "mcp__neev__fs_list",
        "mcp__neev__process_start",
        "mcp__neev__create_sandbox",
        "mcp__neev__delete_sandbox",
    ]


def test_allow_processes_adds_the_process_tools(monkeypatch):
    monkeypatch.setenv("ALLOW_PROCESSES", "1")
    reloaded = importlib.reload(mcp_agent)
    try:
        assert reloaded.AGENT_TOOLS == ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove", "process_start", "process_get", "process_logs", "process_kill", "process_list"]
    finally:
        monkeypatch.delenv("ALLOW_PROCESSES")
        importlib.reload(mcp_agent)


class FakeHost:
    """Answers every host call with the given error flag."""

    def __init__(self, is_error):
        self.is_error = is_error
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(is_error=self.is_error, content=[SimpleNamespace(text="no sandbox is bound")])


def test_host_call_sends_no_arguments():
    host = FakeHost(is_error=False)
    assert asyncio.run(host_call(host, "create_sandbox")) == "no sandbox is bound"
    assert host.calls == [("create_sandbox", {})]


def test_host_call_raises_on_a_tool_error():
    with pytest.raises(RuntimeError, match="delete_sandbox: no sandbox is bound"):
        asyncio.run(host_call(FakeHost(is_error=True), "delete_sandbox"))


def fake_cli(monkeypatch, tool_name, subtype="success", is_error=False):
    """Replaces the CLI: Claude calls one tool, then the turn ends with a result."""

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield AssistantMessage(content=[ToolUseBlock(id="t1", name=tool_name, input={})], model="m")
            yield ResultMessage(
                subtype=subtype,
                duration_ms=1,
                duration_api_ms=1,
                is_error=is_error,
                num_turns=1,
                session_id="s",
                result="done",
            )

    monkeypatch.setattr(mcp_agent, "ClaudeSDKClient", FakeClient)


def test_run_agent_returns_the_result_when_only_allowed_tools_ran(monkeypatch):
    fake_cli(monkeypatch, ALLOWED[0])
    assert asyncio.run(mcp_agent.run_agent(build_options("key", "box", PUBLISHED))) == "done"


@pytest.mark.parametrize("tool", ["Bash", "mcp__neev__delete_sandbox"])
def test_run_agent_fails_on_a_tool_outside_the_allow_list(monkeypatch, tool):
    fake_cli(monkeypatch, tool)
    with pytest.raises(RuntimeError, match=tool):
        asyncio.run(mcp_agent.run_agent(build_options("key", "box", PUBLISHED)))


@pytest.mark.parametrize(("subtype", "is_error"), [("error_max_turns", True), ("success", True)])
def test_run_agent_raises_when_the_run_did_not_succeed(monkeypatch, subtype, is_error):
    fake_cli(monkeypatch, ALLOWED[0], subtype=subtype, is_error=is_error)
    with pytest.raises(RuntimeError, match=f"did not succeed: {subtype}"):
        asyncio.run(mcp_agent.run_agent(build_options("key", "box", PUBLISHED)))
