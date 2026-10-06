import asyncio
import json
import time

from agent import AGENT_TOOLS, MAX_FILE, MAX_OUTPUT, call_tool, fix_code, openai_tools
from tests.fakes import TEST_CMD, FakeModel, FakeSandbox, FakeSession, shell, text, tool_call

PROTECTED = lambda path: path.startswith("tests/")  # noqa: E731
FIX = {"path": "shop/cart.py", "content": "# FIXED\n"}


def fix(session, model, **kw):
    return asyncio.run(fix_code(session, model, "m", "make the tests pass", TEST_CMD, PROTECTED,
                                log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    fs_write = tools[names.index("fs_write")]["function"]
    assert fs_write["description"] == "fs_write from the server"
    assert fs_write["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_call_tool_renders_structured_content_as_plain_json():
    session = FakeSession()
    session.sandbox.workspace["shop/cart.py"] = b"x = 1\n"
    assert json.loads(asyncio.run(call_tool(session, "fs_read", {"path": "shop/cart.py"})))["content"] == "x = 1\n"


def test_server_refusal_is_an_error_for_the_model():
    out = asyncio.run(call_tool(FakeSession(), "fs_write", {"path": "/etc/x", "content": "x"}))
    assert out.startswith("error:") and "escapes workspace root" in out


def test_a_call_that_raises_is_an_error_for_the_model_not_a_crash():
    session = FakeSession()
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep", "args": ["999"]})).startswith("error:")


def test_reads_keep_whole_files_and_other_output_is_clipped():
    session = FakeSession()
    session.sandbox.workspace["big.py"] = b"y" * 12_000
    assert "y" * 12_000 in asyncio.run(call_tool(session, "fs_read", {"path": "big.py"}))
    assert MAX_FILE > 12_000
    session.raise_on["exec"] = RuntimeError("x" * (MAX_OUTPUT * 3))
    out = asyncio.run(call_tool(session, "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_loop_fixes_the_code_then_finishes_once_the_tests_pass():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", FIX), tool_call("finish", {"summary": "fixed the tiers"}, "c2")])
    assert fix(session, model) == "fixed the tiers"
    assert session.sandbox.workspace["shop/cart.py"] == b"# FIXED\n"
    # finish ran the test command before it was accepted
    assert ("exec", {"program": "sh", "args": ["-c", TEST_CMD]}) in session.calls


def test_finish_while_tests_still_fail_returns_the_failure_to_the_model():
    session = FakeSession()
    model = FakeModel([tool_call("finish", {"summary": "early"}), tool_call("fs_write", FIX, "c2"),
                       tool_call("finish", {"summary": "done"}, "c3")])
    assert fix(session, model) == "done"
    result = model.requests[1]["messages"][-1]["content"]
    assert result.startswith("error: the tests still fail") and "FAILED (failures=1)" in result


def test_writes_to_test_files_existing_or_new_are_refused_in_any_spelling():
    session = FakeSession()
    session.sandbox.workspace["tests/test_cart.py"] = b"original"
    writes = [tool_call("fs_write", {"path": p, "content": "assert True"}, f"c{i}")
              for i, p in enumerate(["tests/test_cart.py", "/workspace/tests/test_cart.py", "./tests/../tests/test_cart.py",
                                     "tests/test_new.py"])]
    model = FakeModel([*writes, tool_call("fs_write", FIX, "c8"), tool_call("finish", {"summary": "ok"}, "c9")])
    assert fix(session, model) == "ok"
    assert session.sandbox.workspace["tests/test_cart.py"] == b"original"
    assert "tests/test_new.py" not in session.sandbox.workspace
    for i in (1, 2, 3, 4):
        assert "read-only" in model.requests[i]["messages"][-1]["content"]


def test_loop_refuses_tools_the_model_was_not_given():
    session = FakeSession()
    model = FakeModel([tool_call("delete_sandbox", {}), tool_call("fs_write", FIX, "c2"), tool_call("finish", {"summary": "ok"}, "c3")])
    assert fix(session, model) == "ok"
    assert "delete_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_step_limit_ends_the_loop_and_leaves_the_verdict_to_the_script():
    lines = []
    assert "step limit" in fix(FakeSession(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3, log=lines.append)
    assert any("step limit of 3 reached" in l for l in lines)


def test_text_reply_once_the_tests_pass_ends_the_loop():
    model = FakeModel([tool_call("fs_write", FIX), text("Fixed the tier order.")])
    assert fix(FakeSession(), model) == "Fixed the tier order."


def test_text_reply_while_tests_fail_is_nudged_twice_then_ends_the_loop():
    # e.g. a model that prints a tool call as text instead of making it
    model = FakeModel([text("</think>fs_list<arg_key>path</arg_key>")] * 3)
    assert fix(FakeSession(), model) == "</think>fs_list<arg_key>path</arg_key>"
    nudge = model.requests[1]["messages"][-1]
    assert nudge["role"] == "user" and "use the tools" in nudge["content"] and "FAILED (failures=1)" in nudge["content"]
    assert len(model.requests) == 3


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "shop/cart.py", "content": "cut of'
    model = FakeModel([bad, tool_call("fs_write", FIX, "c2"), tool_call("finish", {"summary": "ok"}, "c3")])
    assert fix(FakeSession(), model) == "ok"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    # the history sent back must itself be valid JSON, or the server rejects the next request
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_shell_commands_run_through_exec():
    sandbox = FakeSandbox()
    session = FakeSession(sandbox)
    model = FakeModel([shell("echo FIXED >> shop/cart.py"), tool_call("finish", {"summary": "ok"}, "c2")])
    assert fix(session, model) == "ok"
    assert sandbox.workspace["shop/cart.py"] == b"FIXED\n"


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    assert "time limit" in fix(FakeSession(), SlowModel(), deadline_s=0.2)
    assert time.monotonic() - t < 5
