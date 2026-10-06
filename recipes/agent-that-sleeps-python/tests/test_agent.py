import asyncio
import json
import time

from agent import AGENT_TOOLS, MAX_OUTPUT, call_tool, openai_tools, triage
from tests.fakes import FakeModel, FakeSandbox, FakeSession, note, text, tool_call


def run_agent(session, model, **kw):
    return asyncio.run(triage(session, model, "m", "triage inbox/batch-1.txt", log=kw.pop("log", lambda *_: None), **kw))


def desk_session():
    sandbox = FakeSandbox()
    sandbox.wait_until_ready()
    sandbox.processes.start(["python3", "desk.py"])
    sandbox.workspace["inbox/batch-1.txt"] = "1. The app crashes.\n2. Add dark mode.\n"
    return FakeSession(sandbox)


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    assert "pause_sandbox" not in names and "delete_sandbox" not in names
    exec_tool = tools[names.index("exec")]["function"]
    assert exec_tool["description"] == "exec from the server"
    assert exec_tool["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_agent_reads_the_batch_and_records_notes_on_the_desk():
    session, lines = desk_session(), []
    model = FakeModel([tool_call("fs_read", {"path": "inbox/batch-1.txt"}), note("bug: the app crashes", "c2"),
                       note("feature: dark mode", "c3"), tool_call("finish", {"summary": "2 notes"}, "c4")])
    assert run_agent(session, model, log=lines.append) == "2 notes"
    assert session.sandbox.desk["notes"] == ["bug: the app crashes", "feature: dark mode"]
    assert "The app crashes" in model.requests[1]["messages"][-1]["content"]
    assert any("exec python3 desk.py add bug: the app crashes" in l for l in lines)


def test_a_text_reply_ends_the_burst():
    assert run_agent(desk_session(), FakeModel([text("All triaged.")])) == "All triaged."


def test_agent_cannot_call_tools_it_was_not_given():
    session = desk_session()
    model = FakeModel([tool_call("pause_sandbox", {}), tool_call("finish", {"summary": "ok"}, "c2")])
    assert run_agent(session, model) == "ok"
    assert "pause_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_step_limit_ends_the_burst_without_raising():
    lines = []
    summary = run_agent(desk_session(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3, log=lines.append)
    assert "step limit" in summary and any("step limit" in l for l in lines)


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("exec", {})
    bad.tool_calls[0].function.arguments = '{"program": "python3", "args": ["desk.py", "add'
    model = FakeModel([bad, tool_call("finish", {"summary": "ok"}, "c2")])
    assert run_agent(desk_session(), model) == "ok"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_server_refusal_and_raised_calls_are_errors_for_the_model():
    session = desk_session()
    out = asyncio.run(call_tool(session, "fs_read", {"path": "/etc/passwd"}))
    assert out.startswith("error:") and "escapes workspace root" in out
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})).startswith("error:")


def test_long_output_is_clipped():
    out = asyncio.run(call_tool(FakeSession(exec_output="x" * (MAX_OUTPUT * 3)), "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    summary = asyncio.run(triage(desk_session(), SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
    assert "time limit" in summary and time.monotonic() - t < 5
