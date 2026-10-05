import asyncio
import json
import time

import pytest

from agent import MAX_FILE, MAX_OUTPUT, AgentFailed, call_tool, finish_tool, openai_tools, run_agent
from tests.fakes import FakeModel, FakeSession, text, tool_call

WORKSPACE = ("fs_write", "fs_read", "fs_list", "exec")
SUMMARY = finish_tool("Call when done.", {"summary": {"type": "string"}})


def agent(session, model, allowed=WORKSPACE, finish=SUMMARY, required=("PLAN.md",), **kw):
    return asyncio.run(run_agent(session, model, "m", system="s", task="t", allowed=allowed, finish=finish,
                                 required_files=required, exists=session.files.__contains__, log=kw.pop("log", lambda *_: None), **kw))


def test_each_agent_sees_only_its_allowlist_with_the_server_schemas_plus_finish():
    tools = asyncio.run(openai_tools(FakeSession(), ("fs_read", "fs_list", "exec"), SUMMARY))
    names = [t["function"]["name"] for t in tools]
    assert names == ["fs_read", "fs_list", "exec", "finish"]
    fs_read = tools[0]["function"]
    assert fs_read["description"] == "fs_read from the server"
    assert fs_read["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_finish_tool_requires_every_parameter():
    params = finish_tool("d", {"passed": {"type": "boolean"}, "report": {"type": "string"}})["function"]["parameters"]
    assert params["required"] == ["passed", "report"]


def test_call_tool_returns_text_and_structured_content():
    session = FakeSession()
    asyncio.run(call_tool(session, "fs_write", {"path": "PLAN.md", "content": "# plan"}))
    out, data = asyncio.run(call_tool(session, "fs_read", {"path": "PLAN.md"}))
    assert json.loads(out)["content"] == "# plan" and data["content"] == "# plan"


def test_server_refusal_is_an_error_for_the_model():
    out, data = asyncio.run(call_tool(FakeSession(), "fs_write", {"path": "/etc/x", "content": "x"}))
    assert out.startswith("error:") and "escapes workspace root" in out and data is None


def test_a_call_that_raises_is_an_error_for_the_model_not_a_crash():
    session = FakeSession()
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    out, data = asyncio.run(call_tool(session, "exec", {"program": "sleep", "args": ["999"]}))
    assert out.startswith("error:") and data is None


def test_reads_keep_whole_files_and_other_output_is_clipped():
    session = FakeSession(exec_result={"exit_code": 0, "stdout": "x" * (MAX_OUTPUT * 3), "stderr": ""})
    session.files["solution.py"] = "y" * 12_000
    assert "y" * 12_000 in asyncio.run(call_tool(session, "fs_read", {"path": "solution.py"}))[0]
    out, _ = asyncio.run(call_tool(session, "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out
    assert MAX_FILE > 12_000


def test_loop_writes_the_required_file_then_finishes():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", {"path": "PLAN.md", "content": "# plan"}),
                       tool_call("finish", {"summary": "planned"}, "c2")])
    result = agent(session, model)
    assert result.finish == {"summary": "planned"}
    assert session.files["PLAN.md"] == "# plan"
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_finish_before_the_required_files_exist_is_an_error_the_model_can_fix():
    model = FakeModel([tool_call("finish", {"summary": "early"}),
                       tool_call("fs_write", {"path": "PLAN.md", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "done"}, "c3")])
    assert agent(FakeSession(), model).finish == {"summary": "done"}
    assert "PLAN.md" in model.requests[1]["messages"][-1]["content"]


def test_loop_refuses_tools_outside_the_allowlist():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", {"path": "solution.py", "content": "hacked"}),
                       tool_call("create_sandbox", {}, "c2"),
                       tool_call("finish", {"summary": "ok"}, "c3")])
    agent(session, model, allowed=("fs_read", "fs_list", "exec"), required=())
    assert [name for name, _ in session.calls] == []
    assert session.files == {}
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_loop_records_every_exec_with_its_result():
    session = FakeSession(exec_result={"exit_code": 1, "stdout": "", "stderr": "FAILED (failures=1)"})
    model = FakeModel([tool_call("exec", {"program": "python3", "args": ["-m", "unittest"]}),
                       tool_call("finish", {"summary": "ran"}, "c2")])
    result = agent(session, model, required=())
    assert result.execs == [({"program": "python3", "args": ["-m", "unittest"]},
                             {"exit_code": 1, "stdout": "", "stderr": "FAILED (failures=1)"})]


def test_loop_stops_at_step_limit_without_the_required_files():
    with pytest.raises(AgentFailed, match="step limit"):
        agent(FakeSession(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3)


def test_hitting_a_limit_with_the_required_files_hands_over_what_was_written():
    session, lines = FakeSession(), []
    session.files["PLAN.md"] = "# almost done"
    result = agent(session, FakeModel([tool_call("fs_list", {})] * 2), max_steps=2, log=lines.append)
    assert "step limit" in result.finish["summary"]
    assert any("handing over what it wrote" in l for l in lines)


def test_loop_nudges_once_then_fails_on_text_only():
    model = FakeModel([text("Here is a plan..."), text("Done.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        agent(FakeSession(), model)
    assert "finish" in model.requests[1]["messages"][-1]["content"]


def test_text_reply_after_the_required_files_exist_counts_as_finished():
    model = FakeModel([tool_call("fs_write", {"path": "PLAN.md", "content": "x"}), text("The plan is ready.")])
    assert agent(FakeSession(), model).finish == {"summary": "The plan is ready."}


def test_an_agent_with_no_required_files_must_call_finish():
    model = FakeModel([text("All tests passed!"), text("Really, they passed.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        agent(FakeSession(), model, required=())


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "PLAN.md", "content": "cut of'
    model = FakeModel([bad, tool_call("fs_write", {"path": "PLAN.md", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "ok"}, "c3")])
    assert agent(FakeSession(), model).finish == {"summary": "ok"}
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        agent(FakeSession(), SlowModel(), deadline_s=0.2)
    assert time.monotonic() - t < 5


def test_required_file_checks_never_go_through_the_agent_session():
    session, checked = FakeSession(), []
    model = FakeModel([tool_call("finish", {"summary": "early"}),
                       tool_call("fs_write", {"path": "PLAN.md", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "done"}, "c3")])
    asyncio.run(run_agent(session, model, "m", system="s", task="t", allowed=WORKSPACE, finish=SUMMARY,
                          required_files=("PLAN.md",), exists=lambda p: checked.append(p) or p in session.files,
                          log=lambda *_: None))
    # only the model's own call reaches the session, so the audit trail attributes nothing extra to the agent
    assert session.calls == [("fs_write", {"path": "PLAN.md", "content": "x"})]
    assert checked == ["PLAN.md", "PLAN.md"]
