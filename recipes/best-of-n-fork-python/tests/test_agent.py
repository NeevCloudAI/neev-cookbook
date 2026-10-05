import asyncio
import json
import time

import pytest

from agent import AGENT_TOOLS, MAX_OUTPUT, AgentFailed, call_tool, fix_bug, openai_tools
from tests.fakes import FIXED, FakeModel, FakeSession, run_tests, text, tool_call

BUGGY = "merged[-1][1] = end"


def project():
    """A session holding the buggy package, as a fork starts out."""
    return FakeSession({"project/scheduler/intervals.py": BUGGY, "project/tests/test_slots.py": "tests"})


def verifier(session):
    """Checks the session's files the way the script does, counting how often it ran."""
    async def verify():
        verify.runs += 1
        out = run_tests(session.files, "python3", [])
        return out["exit_code"] == 0, out["stderr"]

    verify.runs = 0
    return verify


def fix(session, model, **kw):
    return asyncio.run(fix_bug(session, model, "m", 0.2, "hint", verifier(session) if "verify" not in kw else kw.pop("verify"),
                               log=kw.pop("log", lambda *_: None), **kw))


def model(*replies):
    return FakeModel({0.2: replies})


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    assert tools[names.index("exec")]["function"]["description"] == "exec from the server"


def test_server_refusal_and_raised_calls_are_errors_for_the_model():
    session = FakeSession()
    assert "escapes workspace root" in asyncio.run(call_tool(session, "fs_write", {"path": "/etc/x", "content": "x"}))
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})).startswith("error:")


def test_long_output_is_clipped():
    session = FakeSession({"x" * (MAX_OUTPUT * 3): ""})
    out = asyncio.run(call_tool(session, "fs_list", {}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_fix_then_finish_returns_once_the_script_sees_the_tests_pass():
    session = project()
    m = model(tool_call("exec", {"program": "python3", "args": ["-m", "unittest"], "cwd": "project"}),
              tool_call("fs_write", {"path": "project/scheduler/intervals.py", "content": FIXED}, "c2"),
              tool_call("finish", {"summary": "use max for the end"}, "c3"))
    assert fix(session, m) == "use max for the end"
    assert m.requests[0]["temperature"] == 0.2
    assert "hint" in m.requests[0]["messages"][0]["content"]


def test_finish_with_failing_tests_hands_the_output_back_and_continues():
    session = project()
    verify = verifier(session)
    m = model(tool_call("finish", {"summary": "done?"}),
              tool_call("fs_write", {"path": "project/scheduler/intervals.py", "content": FIXED}, "c2"),
              tool_call("finish", {"summary": "fixed"}, "c3"))
    assert fix(session, m, verify=verify) == "fixed"
    assert verify.runs == 2
    back = m.requests[1]["messages"][-1]["content"]
    assert back.startswith("error: the tests still fail") and "FAILED (failures=3)" in back


def test_a_text_reply_with_passing_tests_counts_as_finished():
    session = project()
    m = model(tool_call("fs_write", {"path": "project/scheduler/intervals.py", "content": FIXED}), text("All fixed."))
    assert fix(session, m) == "All fixed."


def test_text_replies_with_failing_tests_get_one_reminder_then_fail():
    m = model(text("The bug is in merge."), text("Use max()."))
    with pytest.raises(AgentFailed, match="without using the tools"):
        fix(project(), m)
    assert "use the tools" in m.requests[1]["messages"][-1]["content"]


def test_loop_refuses_tools_the_model_was_not_given():
    session = project()
    m = model(tool_call("delete_sandbox", {}),
              tool_call("fs_write", {"path": "project/scheduler/intervals.py", "content": FIXED}, "c2"),
              tool_call("finish", {"summary": "ok"}, "c3"))
    assert fix(session, m) == "ok"
    assert "delete_sandbox" not in [name for name, _ in session.calls]
    assert m.requests[1]["messages"][-1]["content"] == "error: delete_sandbox is not one of your tools"


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "project/scheduler/intervals.py", "content": "cut of'
    m = model(bad, tool_call("fs_write", {"path": "project/scheduler/intervals.py", "content": FIXED}, "c2"),
              tool_call("finish", {"summary": "ok"}, "c3"))
    assert fix(project(), m) == "ok"
    assert "not valid JSON" in m.requests[1]["messages"][-1]["content"]
    assert json.loads(m.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_loop_stops_at_the_step_limit():
    with pytest.raises(AgentFailed, match="step limit of 3"):
        fix(project(), model(*[tool_call("fs_list", {})] * 3), max_steps=3)


def test_deadline_cuts_a_slow_model_call():
    slow = FakeModel({0.2: [text("late")]}, delay_by_temperature={0.2: 30})
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        fix(project(), slow, deadline_s=0.2)
    assert time.monotonic() - t < 5
