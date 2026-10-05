import asyncio
import json
import time

import pytest

from agent import AGENT_TOOLS, MAX_FILE, MAX_OUTPUT, AgentFailed, build_app, call_tool, openai_tools
from tests.fakes import FakeModel, FakeSession, text, tool_call


def build(session, model, **kw):
    return asyncio.run(build_app(session, model, "m", "x", log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    fs_write = tools[names.index("fs_write")]["function"]
    assert fs_write["description"] == "fs_write from the server"
    assert fs_write["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_call_tool_renders_structured_content_as_plain_json():
    session = FakeSession()
    asyncio.run(call_tool(session, "fs_write", {"path": "index.html", "content": "<h1>hi</h1>"}))
    out = asyncio.run(call_tool(session, "fs_read", {"path": "index.html"}))
    assert json.loads(out)["content"] == "<h1>hi</h1>"


def test_server_refusal_is_an_error_for_the_model():
    out = asyncio.run(call_tool(FakeSession(), "fs_write", {"path": "/etc/x", "content": "x"}))
    assert out.startswith("error:") and "escapes workspace root" in out


def test_a_call_that_raises_is_an_error_for_the_model_not_a_crash():
    session = FakeSession()
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep", "args": ["999"]})).startswith("error:")


def test_reads_keep_whole_files_and_other_output_is_clipped():
    session = FakeSession(exec_output="x" * (MAX_OUTPUT * 3))
    session.files["app.js"] = "y" * 12_000
    assert "y" * 12_000 in asyncio.run(call_tool(session, "fs_read", {"path": "app.js"}))
    out = asyncio.run(call_tool(session, "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out
    assert MAX_FILE > 12_000


def test_loop_writes_files_then_finishes():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", {"path": "index.html", "content": "<h1>todo</h1>"}),
                       tool_call("finish", {"summary": "todo app"}, "c2")])
    assert build(session, model) == "todo app"
    assert session.files["index.html"] == "<h1>todo</h1>"
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_loop_refuses_tools_the_model_was_not_given():
    session = FakeSession()
    model = FakeModel([tool_call("delete_sandbox", {}), tool_call("fs_write", {"path": "index.html", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "ok"}, "c3")])
    assert build(session, model) == "ok"
    assert "delete_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_loop_stops_at_step_limit_without_index_html():
    model = FakeModel([tool_call("fs_list", {})] * 3)
    with pytest.raises(AgentFailed, match="step limit"):
        build(FakeSession(), model, max_steps=3)


def test_hitting_a_limit_with_index_html_serves_what_was_written():
    session, lines = FakeSession(), []
    session.files["index.html"] = "<h1>almost done</h1>"
    summary = build(session, FakeModel([tool_call("fs_list", {})] * 2), max_steps=2, log=lines.append)
    assert "step limit" in summary
    assert any("serving what the agent wrote" in l for l in lines)


def test_loop_nudges_once_then_fails_on_text_only():
    model = FakeModel([text("Sure! Here is some HTML..."), text("Done.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        build(FakeSession(), model)
    assert "use the tools" in model.requests[1]["messages"][-1]["content"]


def test_loop_treats_text_reply_after_index_html_as_finished():
    model = FakeModel([text("hmm"), tool_call("fs_write", {"path": "index.html", "content": "x"}), text("The app is complete!")])
    assert build(FakeSession(), model) == "The app is complete!"


def test_finish_without_index_html_is_a_tool_error_the_model_can_fix():
    model = FakeModel([tool_call("finish", {"summary": "early"}),
                       tool_call("fs_write", {"path": "index.html", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "done"}, "c3")])
    assert build(FakeSession(), model) == "done"
    assert "index.html" in model.requests[1]["messages"][-1]["content"]


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "index.html", "content": "<h1>cut of'
    model = FakeModel([bad, tool_call("fs_write", {"path": "index.html", "content": "x"}, "c2"),
                       tool_call("finish", {"summary": "ok"}, "c3")])
    assert build(FakeSession(), model) == "ok"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    # the history sent back must itself be valid JSON, or the server rejects the next request
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call_and_serves_what_was_written():
    session = FakeSession()
    session.files["index.html"] = "x"
    t = time.monotonic()
    summary = asyncio.run(build_app(session, SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
    assert "time limit" in summary and time.monotonic() - t < 5


def test_deadline_cuts_a_slow_model_call_and_fails_without_index_html():
    with pytest.raises(AgentFailed, match="time limit"):
        asyncio.run(build_app(FakeSession(), SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
