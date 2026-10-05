import asyncio
import json
import time

from agent import AGENT_TOOLS, MAX_OUTPUT, SYSTEM_PROMPT, call_tool, openai_tools, run_agent
from tests.fakes import (MIGRATE, SNAP_1, TESTS, FakeModel, FakeSandbox, FakeSession, careful_model, shell, text,
                         tool_call)


def seeded_session(**kw):
    sandbox = FakeSandbox(**kw)
    sandbox.exec(["python3", "seed.py"])
    return FakeSession(sandbox)


def go(session, model, **kw):
    return asyncio.run(run_agent(session, model, "m", "apply the migration", log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_the_workspace_and_undo_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    assert "delete_sandbox" not in names and "pause_sandbox" not in names
    snap = tools[names.index("create_snapshot")]["function"]
    assert snap["description"] == "create_snapshot from the server"
    assert snap["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_system_prompt_states_the_snapshot_and_rollback_rule():
    for words in ("create_snapshot", "list_snapshots", "Ready", "rollback_sandbox", "migration"):
        assert words in SYSTEM_PROMPT


def test_careful_agent_snapshots_migrates_rolls_back_and_reports_unsafe():
    session, lines = seeded_session(), []
    result = go(session, careful_model(), log=lines.append)
    assert result.verdict == "unsafe" and "10 customers" in result.summary
    assert [c.name for c in result.calls] == ["create_snapshot", "list_snapshots", "list_snapshots", "exec", "exec",
                                              "rollback_sandbox", "exec"]
    assert all(c.ok for c in result.calls)
    assert session.sandbox.rollbacks == [SNAP_1]
    assert session.sandbox.db == {"customers": 50, "orders": 120}
    out = "\n".join(lines)
    assert "create_snapshot before-migration -> snapshot 00000001 Pending" in out
    assert "list_snapshots -> 00000001 Ready" in out
    assert f"exec sh -c {TESTS} -> exit 1" in out
    assert f"rollback_sandbox {SNAP_1} -> done" in out
    assert "finish: unsafe" in out


def test_the_snapshot_id_comes_back_from_create_snapshot():
    result = go(seeded_session(), careful_model())
    assert result.calls[0].snapshot_id() == SNAP_1
    assert result.calls[3].snapshot_id() is None


def test_rolling_back_before_the_snapshot_is_ready_is_refused_and_recorded_as_failed():
    session = seeded_session(polls_to_ready=5)
    model = FakeModel([tool_call("create_snapshot", {}), tool_call("rollback_sandbox", {"snapshot_id": SNAP_1}, "c2"),
                       text("gave up")])
    result = go(session, model)
    assert result.calls[1].name == "rollback_sandbox" and not result.calls[1].ok
    assert session.sandbox.rollbacks == []
    assert model.requests[2]["messages"][-1]["content"].startswith("error:")


def test_a_text_reply_ends_the_run_without_a_verdict():
    result = go(seeded_session(), FakeModel([text("I will not run that.")]))
    assert result.verdict == "none" and result.summary == "I will not run that." and result.calls == []


def test_agent_cannot_call_tools_it_was_not_given():
    session = seeded_session()
    model = FakeModel([tool_call("delete_sandbox", {}), tool_call("finish", {"verdict": "safe", "summary": "ok"}, "c2")])
    result = go(session, model)
    assert "delete_sandbox" not in [name for name, _ in session.calls] and result.calls == []
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_step_limit_ends_the_run_without_raising():
    lines = []
    result = go(seeded_session(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3, log=lines.append)
    assert result.verdict == "none" and "step limit" in result.summary and any("step limit" in l for l in lines)


def test_malformed_arguments_are_explained_and_echoed_as_valid_json():
    bad = tool_call("exec", {})
    bad.tool_calls[0].function.arguments = '{"program": "sh", "args": ["-c", "python3'
    model = FakeModel([bad, tool_call("finish", {"verdict": "safe", "summary": "ok"}, "c2")])
    assert go(seeded_session(), model).verdict == "safe"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_an_unknown_verdict_is_kept_as_none():
    model = FakeModel([tool_call("finish", {"verdict": "probably fine", "summary": "ok"})])
    assert go(seeded_session(), model).verdict == "none"


def test_server_refusal_and_raised_calls_are_errors_for_the_model():
    session = seeded_session()
    out = asyncio.run(call_tool(session, "fs_read", {"path": "missing.txt"}))
    assert out.startswith("error:") and "not_found" in out
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep"})).startswith("error:")


def test_a_call_that_raises_is_recorded_as_failed():
    session = seeded_session()
    session.raise_on["exec"] = ConnectionError("stream closed")
    result = go(session, FakeModel([shell(MIGRATE), text("stopped")]))
    assert len(result.calls) == 1 and not result.calls[0].ok


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    result = asyncio.run(run_agent(seeded_session(), SlowModel(), "m", "x", deadline_s=0.2, log=lambda *_: None))
    assert "time limit" in result.summary and time.monotonic() - t < 5


def test_a_bare_brace_for_a_tool_without_required_arguments_is_called_with_no_arguments():
    # glm-4-7 sends "{" as the arguments of create_snapshot and list_snapshots
    session = seeded_session()
    bare = tool_call("create_snapshot", {})
    bare.tool_calls[0].function.arguments = "{"
    model = FakeModel([bare, text("stopped")])
    result = go(session, model)
    assert session.calls == [("create_snapshot", {})] and result.calls[0].ok
    assert model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_deadline_cuts_a_tool_call_that_never_returns():
    session, lines = seeded_session(), []
    session.hang_on.add("exec")
    t = time.monotonic()
    result = go(session, FakeModel([shell("sqlite3 shop.db")]), deadline_s=0.2, log=lines.append)
    assert "time limit" in result.summary and time.monotonic() - t < 5
    assert any("no answer before the time limit" in l for l in lines)


def test_a_long_multi_line_command_is_logged_on_one_short_line():
    lines = []
    script = "python3 -c \"\nimport sqlite3\n" + "print(1)\n" * 50 + "\""
    go(seeded_session(), FakeModel([shell(script), text("done")]), log=lines.append)
    step = next(l for l in lines if "step 1" in l)
    assert "\n" not in step and step.endswith("... -> exit 0") and len(step) < 160


def test_the_full_result_is_kept_for_the_review_while_the_model_gets_a_clipped_copy():
    session = seeded_session()
    session.sandbox.descriptor_size = MAX_OUTPUT * 2
    model = FakeModel([tool_call("create_snapshot", {}), tool_call("list_snapshots", {}, "c2"), text("stopped")])
    result = go(session, model)
    listed = result.calls[1]
    assert len(listed.output) > MAX_OUTPUT * 2 and listed.data()["items"][0]["id"] == SNAP_1
    assert "truncated" in model.requests[2]["messages"][-1]["content"]
