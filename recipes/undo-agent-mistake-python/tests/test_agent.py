import asyncio
import json
import time

from agent import AGENT_TOOLS, MAX_OUTPUT, call_tool, clean_up, openai_tools
from tests.fakes import FakeModel, FakeSandbox, FakeSession, shell, text, tool_call


def run_agent(session, model, **kw):
    return asyncio.run(clean_up(session, model, "m", "clean up", log=kw.pop("log", lambda *_: None), **kw))


def seeded_session():
    sandbox = FakeSandbox()
    sandbox.exec(["python3", "seed.py"])
    return FakeSession(sandbox)


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    assert "rollback_sandbox" not in names and "create_snapshot" not in names
    exec_tool = tools[names.index("exec")]["function"]
    assert exec_tool["description"] == "exec from the server"
    assert exec_tool["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_agent_runs_its_commands_in_the_sandbox_and_finishes():
    session, lines = seeded_session(), []
    model = FakeModel([shell("rm -rf data"), tool_call("finish", {"summary": "freed space"}, "c2")])
    assert run_agent(session, model, log=lines.append) == "freed space"
    assert "data/customers.csv" not in session.sandbox.workspace
    assert any("exec sh -c rm -rf data" in l for l in lines)
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_a_text_reply_ends_the_run():
    assert run_agent(seeded_session(), FakeModel([text("Nothing to clean up.")])) == "Nothing to clean up."


def test_agent_cannot_call_tools_it_was_not_given():
    session = seeded_session()
    model = FakeModel([tool_call("rollback_sandbox", {}), tool_call("finish", {"summary": "ok"}, "c2")])
    assert run_agent(session, model) == "ok"
    assert "rollback_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_step_limit_ends_the_run_without_raising():
    lines = []
    summary = run_agent(seeded_session(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3, log=lines.append)
    assert "step limit" in summary and any("step limit" in l for l in lines)


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("exec", {})
    bad.tool_calls[0].function.arguments = '{"program": "sh", "args": ["-c", "rm'
    model = FakeModel([bad, tool_call("finish", {"summary": "ok"}, "c2")])
    assert run_agent(seeded_session(), model) == "ok"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_server_refusal_and_raised_calls_are_errors_for_the_model():
    session = FakeSession()
    out = asyncio.run(call_tool(session, "fs_write", {"path": "/etc/x", "content": "x"}))
    assert out.startswith("error:") and "escapes workspace root" in out
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})).startswith("error:")


def test_long_output_is_clipped():
    out = asyncio.run(call_tool(FakeSession(exec_output="x" * (MAX_OUTPUT * 3)), "exec", {"program": "du"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    summary = asyncio.run(clean_up(seeded_session(), SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
    assert "time limit" in summary and time.monotonic() - t < 5
