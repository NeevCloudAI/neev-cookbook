import asyncio
import json

from agent import AGENT_TOOLS, MAX_OUTPUT, call_tool, openai_tools, run_agent
from tests.fakes import FakeModel, FakeSession, text, tool_call


def no_requests(host, reason):
    raise AssertionError("request_egress was not expected")


def run(session, model, on_egress=no_requests, **kw):
    return asyncio.run(run_agent(session, model, "m", "x", on_egress, log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_the_workspace_tools_plus_request_egress_and_finish():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "request_egress", "finish"]
    exec_tool = tools[names.index("exec")]["function"]
    assert exec_tool["description"] == "exec from the server"
    assert tools[names.index("request_egress")]["function"]["parameters"]["required"] == ["host", "reason"]


def test_request_egress_goes_to_the_approver_and_its_answer_to_the_model():
    asked = []

    def approver(host, reason):
        asked.append((host, reason))
        return "approved: api.github.com is reachable now"

    session = FakeSession()
    model = FakeModel([tool_call("request_egress", {"host": "api.github.com", "reason": "read releases"}),
                       tool_call("finish", {"summary": "done"}, "c2")])
    result = run(session, model, approver)
    assert asked == [("api.github.com", "read releases")]
    assert session.calls == []  # never sent to the sandbox server
    assert model.requests[1]["messages"][-1]["content"] == "approved: api.github.com is reachable now"
    assert result.finished and result.summary == "done"


def test_request_egress_without_a_host_is_an_error_and_never_reaches_the_approver():
    model = FakeModel([tool_call("request_egress", {"reason": "need it"}),
                       tool_call("finish", {"summary": "done"}, "c2")])
    run(FakeSession(), model)
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_time_waiting_on_the_approver_does_not_use_the_agent_budget():
    now = [0.0]

    def slow_human(host, reason):
        now[0] += 500  # a person takes minutes to answer
        return "approved"

    model = FakeModel([tool_call("request_egress", {"host": "pypi.org", "reason": "x"}),
                       tool_call("finish", {"summary": "done"}, "c2")])
    result = run(FakeSession(), model, slow_human, deadline_s=60, clock=lambda: now[0])
    assert result.finished


def test_slow_model_reply_is_cut_off_by_the_deadline():
    model = FakeModel([text("thinking")], delay_s=5)
    result = run(FakeSession(), model, deadline_s=0.05)
    assert not result.finished and "time limit" in result.summary


def test_exec_is_bounded_by_the_deadline():
    session = FakeSession()

    async def hang(name, arguments=None):
        await asyncio.sleep(5)

    session.call_tool = hang
    model = FakeModel([tool_call("exec", {"program": "curl", "args": ["https://example.com/"]})])
    result = run(session, model, deadline_s=0.05)
    assert not result.finished and "time limit" in result.summary
    assert result.exec_commands == ["curl https://example.com/"]


def test_agent_records_every_exec_command_it_runs():
    model = FakeModel([
        tool_call("exec", {"program": "curl", "args": ["-sS", "https://api.github.com/"]}),
        tool_call("finish", {"summary": "ok"}, "c2")])
    assert run(FakeSession(), model).exec_commands == ["curl -sS https://api.github.com/"]


def test_agent_refuses_tools_it_was_not_given():
    session = FakeSession()
    model = FakeModel([tool_call("delete_sandbox", {}), tool_call("finish", {"summary": "done"}, "c2")])
    assert run(session, model).finished
    assert session.calls == []
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_malformed_arguments_are_an_error_and_echoed_as_empty_json():
    model = FakeModel([tool_call("exec", '{"program": "cu'), tool_call("finish", {"summary": "done"}, "c2")])
    run(FakeSession(), model)
    sent = model.requests[1]["messages"]
    assert sent[-2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert sent[-1]["content"].startswith("error:")


def test_step_limit_ends_the_run_unfinished():
    model = FakeModel([tool_call("fs_list", {})] * 3)
    result = run(FakeSession(), model, max_steps=3)
    assert not result.finished and "step limit" in result.summary


def test_two_text_replies_end_the_run_unfinished_after_one_nudge():
    model = FakeModel([text("I will look"), text("all done")])
    result = run(FakeSession(), model)
    assert not result.finished and result.summary == "all done"
    assert "request_egress" in model.requests[1]["messages"][-1]["content"]


def test_server_refusal_and_raised_errors_are_errors_for_the_model():
    session = FakeSession()
    assert asyncio.run(call_tool(session, "fs_write", {"path": "/etc/x", "content": "x"})).startswith("error:")
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "curl"})).startswith("error:")


def test_tool_output_is_clipped():
    out = asyncio.run(call_tool(FakeSession(exec_output="x" * (MAX_OUTPUT * 3)), "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out
    assert json.loads(asyncio.run(call_tool(FakeSession(), "fs_list", {})))["entries"] == []
