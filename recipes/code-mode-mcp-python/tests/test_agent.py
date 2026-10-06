import asyncio
import json
import time

import pytest

from agent import MAX_OUTPUT, MODES, SNIPPET_DIR, call_tool, openai_tools, parse_answer, run_agent, run_python
from tests.fakes import FakeModel, FakeSession, finish, text, tool_call, tool_calls

TOP = [("acme-foods", 9), ("coral-travel", 7), ("finch-labs", 5)]


def names(tools):
    return [t["function"]["name"] for t in tools]


def run(session, model, mode="code", **kw):
    return asyncio.run(run_agent(session, model, "m", mode, "q?", log=kw.pop("log", lambda *_: None), **kw))


@pytest.fixture
def session(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.csv").write_text("customer,status\nacme-foods,failed\n")
    return FakeSession(tmp_path)


def test_tool_mode_sees_only_fs_list_and_fs_read_with_the_server_schemas(session):
    tools = asyncio.run(openai_tools(session, "tools"))
    assert names(tools) == ["fs_list", "fs_read", "finish"]
    assert tools[0]["function"]["description"] == "fs_list from the server"
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_code_mode_sees_fs_list_and_run_python_but_not_fs_read_or_exec(session):
    assert names(asyncio.run(openai_tools(session, "code"))) == ["fs_list", "run_python", "finish"]
    assert MODES["code"] == ("fs_list",)


def test_run_python_writes_the_program_into_the_sandbox_and_runs_it_with_python3(session):
    out = json.loads(asyncio.run(run_python(session, "print(open('logs/a.csv').read().count('failed'))", 1)))
    assert out == {"stdout": "1\n", "stderr": "", "exit_code": 0}
    path = f"{SNIPPET_DIR}/snippet_1.py"
    assert session.calls == [("fs_write", {"path": path, "content": "print(open('logs/a.csv').read().count('failed'))"}),
                             ("exec", {"program": "python3", "args": [path]})]


def test_run_python_returns_the_traceback_so_the_model_can_fix_its_program(session):
    out = json.loads(asyncio.run(run_python(session, "1/0", 2)))
    assert out["exit_code"] == 1 and "ZeroDivisionError" in out["stderr"]


def test_run_python_output_is_clipped(session):
    out = asyncio.run(run_python(session, f"print('x' * {MAX_OUTPUT * 3})", 1))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out


def test_run_python_reports_a_refused_write_and_does_not_run_anything(session):
    session.raise_on["fs_write"] = ConnectionError("stream closed")
    assert asyncio.run(run_python(session, "print(1)", 1)).startswith("error:")
    assert [n for n, _ in session.calls] == ["fs_write"]


def test_fs_read_returns_the_file_text_as_is_not_json_escaped(session):
    (session.root / "logs" / "b.json").write_text('[\n {"ts": "2026-09-28"}\n]\n')
    assert asyncio.run(call_tool(session, "fs_read", {"path": "logs/b.json"})) == '[\n {"ts": "2026-09-28"}\n]\n'


def test_server_refusal_and_a_raising_call_become_errors_for_the_model(session):
    assert "leaves the workspace" in asyncio.run(call_tool(session, "fs_read", {"path": "/etc/passwd"}))
    session.raise_on["fs_list"] = TimeoutError("deadline exceeded")
    assert asyncio.run(call_tool(session, "fs_list", {})).startswith("error:")


def test_file_listings_are_not_clipped_so_tool_mode_sees_every_file(session):
    for i in range(300):
        (session.root / "logs" / f"payments-{i:03d}.json").write_text("[]")
    out = asyncio.run(call_tool(session, "fs_list", {"path": "logs"}))
    assert "payments-299.json" in out and "truncated" not in out


@pytest.mark.parametrize("args, expected", [
    ({"top_customers": [{"customer": " Acme-Foods ", "failed_payments": 9}]}, [("acme-foods", 9)]),
    ({"top_customers": [{"customer": "a", "failed_payments": "4"}]}, [("a", 4)]),
    ({"top_customers": "acme-foods 9"}, None),
    ({"top_customers": [{"customer": "a"}]}, None),
    ({"top_customers": [{"customer": "a", "failed_payments": "many"}]}, None),
    ({}, None),
])
def test_parse_answer(args, expected):
    assert parse_answer(args) == expected


def test_code_mode_answers_and_counts_calls_tokens_and_bytes(session):
    model = FakeModel([tool_call("run_python", {"code": "print('acme-foods 9')"}), finish(TOP)], usage=(1000, 50))
    result = run(session, model)
    assert result.answer == TOP and result.stopped == "answered"
    assert (result.model_calls, result.tool_calls, result.input_tokens, result.output_tokens) == (2, 1, 2000, 100)
    assert 0 < result.tool_bytes < 200 and result.seconds > 0
    assert result.programs == ["print('acme-foods 9')"]
    assert "acme-foods 9" in model.requests[1]["messages"][-1]["content"]


def test_tool_mode_reads_files_with_several_calls_per_turn(session):
    model = FakeModel([tool_call("fs_list", {"path": "logs"}),
                       tool_calls(("fs_read", {"path": "logs/a.csv"}), ("fs_read", {"path": "logs/a.csv"})),
                       finish(TOP)])
    result = run(session, model, mode="tools")
    assert result.answer == TOP and result.model_calls == 3 and result.tool_calls == 3
    assert [n for n, _ in session.calls] == ["fs_list", "fs_read", "fs_read"]


def test_tools_outside_the_mode_are_refused_and_never_reach_the_sandbox(session):
    model = FakeModel([tool_calls(("fs_read", {"path": "logs/a.csv"}), ("delete_sandbox", {}), ("exec", {"program": "ls"})),
                       finish(TOP)])
    assert run(session, model, mode="code").answer == TOP
    assert session.calls == []
    assert all(m["content"].startswith("error:") for m in model.requests[1]["messages"][-3:])


def test_malformed_answer_is_an_error_the_model_can_fix(session):
    model = FakeModel([tool_call("finish", {"top_customers": "acme-foods"}), finish(TOP)])
    result = run(session, model)
    assert result.answer == TOP and result.model_calls == 2
    assert "top_customers" in model.requests[1]["messages"][-1]["content"]


def test_invalid_json_arguments_are_explained_and_echoed_as_valid_json(session):
    bad = tool_call("run_python", {})
    bad.tool_calls[0].function.arguments = '{"code": "print(1'
    model = FakeModel([bad, finish(TOP)])
    assert run(session, model).answer == TOP
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_step_limit_ends_the_run_without_an_answer(session):
    result = run(session, FakeModel([tool_call("fs_list", {})] * 3), mode="tools", max_steps=3)
    assert result.answer is None and result.stopped == "step limit of 3 reached" and result.model_calls == 3


def test_text_reply_is_nudged_once_then_ends_without_an_answer(session):
    model = FakeModel([text("probably acme-foods"), text("acme-foods, 9")])
    result = run(session, model)
    assert result.answer is None and "without calling finish" in result.stopped
    assert "finish" in model.requests[1]["messages"][-1]["content"]


def test_model_error_is_recorded_not_raised(session):
    async def broken(**kwargs):
        raise RuntimeError("Error code: 400 - prompt is too long " + "x" * 500)

    model = type("M", (), {"chat": type("C", (), {"completions": type("CC", (), {"create": staticmethod(broken)})})})()
    result = run(session, model)
    assert result.answer is None and result.stopped.startswith("model error: Error code: 400")
    assert len(result.stopped) == 120 and "\n" not in result.stopped


def test_missing_usage_counts_as_zero_tokens(session):
    result = run(session, FakeModel([finish(TOP)], usage=None))
    assert (result.input_tokens, result.output_tokens) == (0, 0)


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call(session):
    t = time.monotonic()
    result = run(session, SlowModel(), deadline_s=0.2)
    assert result.answer is None and result.stopped == "time limit of 0s reached" and time.monotonic() - t < 5


def test_deadline_cuts_a_slow_tool_call(session):
    async def hang(*a, **kw):
        await asyncio.sleep(30)

    session.call_tool = hang
    t = time.monotonic()
    result = run(session, FakeModel([tool_call("fs_list", {})]), mode="tools", deadline_s=0.3)
    assert result.stopped == "time limit of 0s reached" and time.monotonic() - t < 5
