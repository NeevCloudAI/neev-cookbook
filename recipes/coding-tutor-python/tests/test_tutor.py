import asyncio
import json
import time

import pytest

from tests.fakes import HINTS, FakeModel, FakeSession, text, tool_call
from tutor import MAX_OUTPUT, TUTOR_TOOLS, TutorFailed, call_tool, openai_tools, review


def run_review(session, model, **kw):
    return asyncio.run(review(session, model, "m", log=kw.pop("log", lambda *_: None), **kw))


def finish(hints, call_id="c9"):
    return tool_call("finish", {"hints": hints}, call_id)


def test_tutor_sees_only_read_and_run_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*TUTOR_TOOLS, "finish"]
    assert "fs_write" not in names
    fs_read = tools[names.index("fs_read")]["function"]
    assert fs_read["description"] == "fs_read from the server"
    assert fs_read["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_review_reads_the_work_then_returns_the_hints():
    session = FakeSession()
    session.sandbox.workspace["app.py"] = "return len(text.split(' '))"
    model = FakeModel([tool_call("fs_read", {"path": "app.py"}), finish(HINTS)])
    assert run_review(session, model) == HINTS
    assert json.loads(model.requests[1]["messages"][-1]["content"])["content"] == "return len(text.split(' '))"


def test_a_write_attempt_is_refused_and_never_reaches_the_sandbox():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", {"path": "app.py", "content": "fixed"}), finish(HINTS)])
    assert run_review(session, model) == HINTS
    assert "fs_write" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


@pytest.mark.parametrize("hints, problem", [
    (["only one"], "2 or 3 hints"),
    (["a", "b", "c", "d"], "2 or 3 hints"),
    (["fine", "   "], "empty"),
    (["fine", "```python\nreturn len(text.split())\n```"], "code"),
    ("not a list", "2 or 3 hints"),
])
def test_bad_hints_are_sent_back_for_the_model_to_fix(hints, problem):
    model = FakeModel([finish(hints, "c1"), finish(HINTS, "c2")])
    assert run_review(FakeSession(), model) == HINTS
    assert problem in model.requests[1]["messages"][-1]["content"]


def test_hints_are_trimmed():
    model = FakeModel([finish(["  first  ", "second\n"])])
    assert run_review(FakeSession(), model) == ["first", "second"]


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("fs_read", {})
    bad.tool_calls[0].function.arguments = '{"path": "app.p'
    model = FakeModel([bad, finish(HINTS)])
    assert run_review(FakeSession(), model) == HINTS
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_server_refusal_and_raised_errors_become_errors_for_the_model():
    session = FakeSession()
    assert asyncio.run(call_tool(session, "fs_read", {"path": "nope.py"})).startswith("error:")
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})) == "error: deadline_exceeded"


def test_long_tool_output_is_clipped():
    out = asyncio.run(call_tool(FakeSession(exec_output="x" * (MAX_OUTPUT * 3)), "exec", {"program": "cat"}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_text_reply_is_nudged_once_then_fails():
    model = FakeModel([text("Your code looks good!"), text("Really, it is fine.")])
    with pytest.raises(TutorFailed, match="without calling finish"):
        run_review(FakeSession(), model)
    assert "finish" in model.requests[1]["messages"][-1]["content"]


def test_review_stops_at_the_step_limit():
    with pytest.raises(TutorFailed, match="step limit of 3"):
        run_review(FakeSession(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3)


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    with pytest.raises(TutorFailed, match="time limit"):
        run_review(FakeSession(), SlowModel(), deadline_s=0.2)
    assert time.monotonic() - t < 5


class SlowToolSession(FakeSession):
    """A session whose exec never returns, like a command waiting for input."""

    async def call_tool(self, name, arguments=None):
        if name == "exec":
            await asyncio.sleep(30)
        return await super().call_tool(name, arguments)


def test_deadline_cuts_a_tool_call_that_never_returns():
    model = FakeModel([tool_call("exec", {"program": "python3", "args": ["-c", "input()"]})] * 3)
    t = time.monotonic()
    with pytest.raises(TutorFailed, match="time limit"):
        run_review(SlowToolSession(), model, deadline_s=0.3)
    assert time.monotonic() - t < 5


def test_deadline_cuts_a_tool_listing_that_never_returns():
    class SlowListing(FakeSession):
        async def list_tools(self):
            await asyncio.sleep(30)

    with pytest.raises(TutorFailed, match="time limit"):
        run_review(SlowListing(), FakeModel([]), deadline_s=0.2)
