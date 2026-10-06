import asyncio
import json
import time

from agent import AGENT_TOOLS, MAX_OUTPUT, call_tool, openai_tools, run_agent
from tests.fakes import SOLVE, FakeClient, FakeModel, FakeSandbox, FakeSession, text, tool_call


def session():
    return FakeSession(FakeSandbox(FakeClient(), "eval-roll-t"))


def agent(sess, script, **kw):
    model = script if isinstance(script, FakeModel) else FakeModel({"m": script})
    return asyncio.run(run_agent(sess, model, "m", "do the task", log=kw.pop("log", lambda *_: None), **kw)), model


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(session()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    fs_write = tools[names.index("fs_write")]["function"]
    assert fs_write["description"] == "fs_write from the server"
    assert fs_write["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_server_refusal_and_a_raising_call_are_errors_for_the_model_not_crashes():
    sess = session()
    assert "escapes workspace root" in asyncio.run(call_tool(sess, "fs_write", {"path": "/etc/x", "content": "x"}))
    sess.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(sess, "exec", {"program": "sleep"})).startswith("error:")


def test_long_output_is_clipped():
    sess = session()
    sess.sandbox.workspace["big.txt"] = "x" * (MAX_OUTPUT * 3)
    out = asyncio.run(call_tool(sess, "fs_read", {"path": "big.txt"}))
    assert len(out) < MAX_OUTPUT + 100 and "truncated" in out


def test_run_counts_steps_and_tokens_and_stops_at_finish():
    sess = session()
    run, model = agent(sess, SOLVE)
    assert (run.steps, run.tokens, run.stop) == (2, 200, "finished")
    assert sess.sandbox.workspace["done.txt"] == "ok"
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_a_text_reply_ends_the_run_and_says_so():
    run, _ = agent(session(), [text("I think it is done.")])
    assert (run.steps, run.stop) == (1, "replied without a tool call")


def test_refuses_tools_the_model_was_not_given():
    sess = session()
    run, model = agent(sess, [tool_call("delete_sandbox", {}), tool_call("finish", {"summary": "ok"}, "c2")])
    assert "delete_sandbox" not in [name for name, _ in sess.calls]
    assert model.requests[1]["messages"][-1]["content"] == "error: delete_sandbox is not one of your tools"
    assert run.stop == "finished"


def test_step_limit_ends_the_run_without_raising():
    run, model = agent(session(), [tool_call("fs_list", {})], max_steps=3)
    assert (run.steps, run.stop) == (3, "step limit of 3")
    assert len(model.requests) == 3


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "done.txt", "content": "o'
    run, model = agent(session(), [bad, *SOLVE[:1], SOLVE[1]])
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    # the history sent back must itself be valid JSON, or the server rejects the next request
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_tool_calls_get_only_the_time_left_in_the_budget():
    sess = session()
    agent(sess, [tool_call("fs_list", {}), tool_call("finish", {"summary": "ok"}, "c2")], deadline_s=120)
    assert sess.timeouts and 100 < sess.timeouts[0] <= 120


def test_deadline_cuts_a_slow_model_call():
    async def slow(messages):
        await asyncio.sleep(30)

    t = time.monotonic()
    run, _ = agent(session(), FakeModel({"m": slow}), deadline_s=0.2)
    assert run.stop == "time limit of 0s" and time.monotonic() - t < 5 and run.seconds < 5


def test_a_missing_usage_block_counts_as_zero_tokens():
    model = FakeModel({"m": [text("done")]})
    original = model.chat.completions.create

    async def no_usage(**kw):
        r = await original(**kw)
        r.usage = None
        return r

    model.chat.completions.create = no_usage
    run, _ = agent(session(), model)
    assert run.tokens == 0
