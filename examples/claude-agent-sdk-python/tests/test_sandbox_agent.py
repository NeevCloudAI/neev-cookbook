import asyncio
from types import SimpleNamespace

import neevai.agent_tools.claude
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from neevai.agent_tools import SandboxTools
from neevai.errors import AuthenticationError, BadRequestError

import sandbox_agent


def read_text(path, cwd=None):
    """Stands in for files.read_text: boom.txt fails fatally, anything else is missing."""
    if path == "boom.txt":
        raise AuthenticationError(401, {"message": "invalid api key for /orgs/org-123"}, None)
    raise BadRequestError(400, {"message": f"{path}: no such file"}, None)


def tools():
    return SandboxTools(SimpleNamespace(files=SimpleNamespace(read_text=read_text)))


def result_message(text, subtype="success", is_error=False):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1, session_id="s", result=text
    )


def fake_cli(monkeypatch, path, result=None):
    """Replaces the CLI: Claude reads path once, then the turn ends with a result. Returns the messages consumed."""
    consumed = []
    # Keep the SdkMcpTool list so the fake can call a handler the way the CLI would.
    monkeypatch.setattr(neevai.agent_tools.claude, "create_sdk_mcp_server", lambda name, version, tools: tools)

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
            args = {"path": path}
            call = AssistantMessage(content=[ToolUseBlock(id="t1", name="mcp__neev__read_file", input=args)], model="m")
            consumed.append(call)
            yield call
            await next(t for t in self.options.mcp_servers["neev"] if t.name == "read_file").handler(args)
            for msg in ("tool result", result or result_message("done")):
                consumed.append(msg)
                yield msg

    monkeypatch.setattr(sandbox_agent, "ClaudeSDKClient", FakeClient)
    return consumed


def test_run_agent_stops_and_raises_the_fatal_exception(monkeypatch):
    consumed = fake_cli(monkeypatch, "boom.txt")
    with pytest.raises(AuthenticationError):
        asyncio.run(sandbox_agent.run_agent(tools()))
    assert not any(isinstance(m, ResultMessage) for m in consumed)


def test_run_agent_returns_the_result_when_tools_only_report_errors(monkeypatch):
    fake_cli(monkeypatch, "x.txt")
    assert asyncio.run(sandbox_agent.run_agent(tools())) == "done"


@pytest.mark.parametrize(("subtype", "is_error"), [("error_max_turns", True), ("success", True)])
def test_run_agent_raises_when_the_run_did_not_succeed(monkeypatch, subtype, is_error):
    fake_cli(monkeypatch, "x.txt", result_message("partial", subtype, is_error))
    with pytest.raises(RuntimeError, match=f"agent run did not succeed: {subtype}: partial"):
        asyncio.run(sandbox_agent.run_agent(tools()))
