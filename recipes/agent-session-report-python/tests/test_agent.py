import asyncio
import json
import time

import pytest

from agent import AGENT_TOOLS, MAX_OUTPUT, AgentFailed, call_tool, openai_tools, run_agent
from tests.fakes import FakeModel, FakeSession, text, tool_call


def run(session, model, **kw):
    return asyncio.run(run_agent(session, model, "m", "x", log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    fs_read = tools[names.index("fs_read")]["function"]
    assert fs_read["description"] == "fs_read from the server"
    assert fs_read["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_call_tool_renders_structured_content_as_plain_json():
    session = FakeSession()
    session.files[".env"] = "A=1"
    assert json.loads(asyncio.run(call_tool(session, "fs_read", {"path": ".env"})))["content"] == "A=1"


def test_server_refusal_is_an_error_for_the_model():
    out = asyncio.run(call_tool(FakeSession(), "fs_write", {"path": "/etc/x", "content": "x"}))
    assert out.startswith("error:") and "escapes workspace root" in out


def test_a_call_that_raises_is_an_error_for_the_model_not_a_crash():
    session = FakeSession()
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep", "args": ["999"]})).startswith("error:")


def test_long_output_is_clipped():
    out = asyncio.run(call_tool(FakeSession(exec_output="x" * (MAX_OUTPUT * 3)), "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_loop_counts_the_calls_that_reached_the_sandbox_and_returns_the_summary():
    session = FakeSession()
    session.files["README.md"] = "# demo"
    model = FakeModel([tool_call("fs_read", {"path": "README.md"}),
                       tool_call("exec", {"program": "python3", "args": ["app.py"]}, "c2"),
                       tool_call("finish", {"summary": "port 8080, one secret missing"}, "c3")])
    result = run(session, model)
    assert result.summary == "port 8080, one secret missing"
    assert result.calls == 2
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_refused_and_malformed_calls_do_not_count_as_sandbox_calls():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "a", "content": "cut of'
    model = FakeModel([tool_call("delete_sandbox", {}), bad, tool_call("fs_list", {}, "c3"),
                       tool_call("finish", {"summary": "ok"}, "c4")])
    session = FakeSession()
    result = run(session, model)
    assert result.calls == 1
    assert [name for name, _ in session.calls] == ["fs_list"]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")
    # the history sent back must itself be valid JSON, or the model server rejects the next request
    assert json.loads(model.requests[2]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_step_limit_after_some_work_ends_the_session_so_it_can_be_reported():
    lines = []
    result = run(FakeSession(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3, log=lines.append)
    assert result.calls == 3 and "step limit" in result.summary
    assert any("step limit" in l for l in lines)


def test_step_limit_without_any_sandbox_call_fails():
    with pytest.raises(AgentFailed, match="step limit"):
        run(FakeSession(), FakeModel([tool_call("delete_sandbox", {})] * 2), max_steps=2)


def test_text_reply_after_work_is_the_summary():
    model = FakeModel([tool_call("fs_list", {}), text("The project listens on port 8080.")])
    assert run(FakeSession(), model).summary == "The project listens on port 8080."


def test_loop_nudges_once_then_fails_on_text_only():
    model = FakeModel([text("I would read the README..."), text("Done.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        run(FakeSession(), model)
    assert "use the tools" in model.requests[1]["messages"][-1]["content"]


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_hung_tool_call_and_reports_what_was_done():
    class HungExec(FakeSession):
        async def call_tool(self, name, arguments=None):
            if name == "exec":
                await asyncio.sleep(30)
            return await super().call_tool(name, arguments)

    model = FakeModel([tool_call("fs_list", {}), tool_call("exec", {"program": "python3"}, "c2")])
    t = time.monotonic()
    result = asyncio.run(run_agent(HungExec(), model, "m", "x", deadline_s=0.3, log=lambda *_: None))
    assert "time limit" in result.summary and result.calls == 2
    assert time.monotonic() - t < 5


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        asyncio.run(run_agent(FakeSession(), SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
    assert time.monotonic() - t < 5
