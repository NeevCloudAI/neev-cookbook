import asyncio
import json

import pytest

from agent import AGENT_TOOLS, MAX_OUTPUT, AgentFailed, call_tool, diagnose, openai_tools
from debug_at_failure import make_batch
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, curl, text, tool_call

ANSWER = {"record_id": "ord-1", "field": "amount", "value": "1,234.00", "cause": "a string amount"}


def failed_sandbox():
    """A sandbox whose pipeline has already failed, with pipeline.py in the workspace."""
    sb = FakeSandbox("debug-fail-1-fork", FakeClient())
    sb.workspace["pipeline.py"] = "def aggregate(record): ..."
    sb.processes.start(["python3", "pipeline.py"], stdin=json.dumps(make_batch("s")[0]))
    while sb.pipeline.state["status"] == "running":
        sb.pipeline.get("/state")
    return sb


def run(model, session=None, **kw):
    return asyncio.run(diagnose(session or FakeSession(failed_sandbox()), model, "m",
                                log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_only_the_investigation_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession(failed_sandbox())))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"] and "fs_write" not in names
    exec_tool = tools[names.index("exec")]["function"]
    assert exec_tool["description"] == "exec from the server"
    assert exec_tool["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_the_agent_queries_the_live_process_and_returns_its_finding():
    session = FakeSession(failed_sandbox())
    model = FakeModel([curl("/state"), tool_call("fs_read", {"path": "pipeline.py"}, "c2"),
                       tool_call("finish", ANSWER, "c3")])
    assert run(model, session) == ANSWER
    assert [name for name, _ in session.calls] == ["exec", "fs_read"]
    state = json.loads(json.loads(model.requests[1]["messages"][-1]["content"])["stdout"])
    assert state["status"] == "error"  # the model saw the live process's memory


def test_finish_without_every_field_is_an_error_the_model_can_fix():
    model = FakeModel([tool_call("finish", {"record_id": "ord-1"}), tool_call("finish", ANSWER, "c2")])
    assert run(model) == ANSWER
    assert "field" in model.requests[1]["messages"][-1]["content"]


def test_the_model_cannot_call_tools_it_was_not_given():
    session = FakeSession(failed_sandbox())
    model = FakeModel([tool_call("process_kill", {"process_id": "proc-1"}), tool_call("finish", ANSWER, "c2")])
    assert run(model, session) == ANSWER
    assert session.calls == []
    assert model.requests[1]["messages"][-1]["content"].startswith("error: process_kill is not one of your tools")


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("exec", {})
    bad.tool_calls[0].function.arguments = '{"program": "curl", "args": ["-s'
    model = FakeModel([bad, tool_call("finish", ANSWER, "c2")])
    assert run(model) == ANSWER
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_server_refusal_is_an_error_for_the_model():
    out = asyncio.run(call_tool(FakeSession(failed_sandbox()), "fs_read", {"path": "/etc/passwd"}))
    assert out.startswith("error:") and "escapes workspace root" in out


def test_a_call_that_raises_is_an_error_for_the_model_not_a_crash():
    session = FakeSession(failed_sandbox())
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})).startswith("error:")


def test_long_output_is_clipped():
    session = FakeSession(failed_sandbox())
    session.sandbox.workspace["big.log"] = "x" * (MAX_OUTPUT * 3)
    out = asyncio.run(call_tool(session, "fs_read", {"path": "big.log"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_step_limit_ends_without_a_diagnosis():
    with pytest.raises(AgentFailed, match="step limit of 3"):
        run(FakeModel([curl("/state")] * 3), max_steps=3)


def test_text_only_replies_get_one_reminder_then_fail():
    model = FakeModel([text("It is probably a bad record."), text("Yes, a bad record.")])
    with pytest.raises(AgentFailed, match="without calling finish"):
        run(model)
    assert "finish" in model.requests[1]["messages"][-1]["content"]
