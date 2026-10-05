import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import simulate_agent
from simulate_agent import AGENT_TOOLS, openai_tools, work
from tests.fakes import NOT_BOUND, FakeClient, FakeModel, FakeSandbox, FakeSession, record, text, tool_call, trail

NAME = "coding-agent-test"
WORKED_TRAIL = trail([record("exec", rid="2"), record("fs.write", "app/server.js", rid="1")])


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect


def coding_model():
    """A model that behaves like a coding agent: tries, is told no sandbox exists, creates one, writes, tests."""
    return FakeModel([
        tool_call("exec", {"program": "node", "args": ["--version"]}),
        tool_call("create_sandbox", {}, "c2"),
        tool_call("fs_write", {"path": "app/server.js", "content": "// server"}, "c3"),
        tool_call("exec", {"program": "node", "args": ["--test"], "cwd": "/workspace/app"}, "c4"),
        tool_call("finish", {"summary": "1 test passed"}, "c5")])


def setup(session=None, pages=None):
    """Builds a session and a client whose sandbox NAME exists once the session has created it."""
    session = session or FakeSession()
    sb = FakeSandbox(NAME, pages=[pages or WORKED_TRAIL])
    return session, FakeClient(sb, exists=lambda: session.created), connector(session), sb


def run(client, model, connect, **kw):
    lines = kw.pop("lines", [])
    kw.setdefault("name", NAME)
    return simulate_agent.run(client, model, "m", connect, log=lines.append, sleep=lambda s: None, **kw)


def test_model_sees_only_the_allowed_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"]
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}
    assert "delete_sandbox" not in names


def test_happy_path_creates_over_mcp_checks_tests_prints_trail_and_deletes():
    session, client, connect, sb = setup()
    lines = []
    code = run(client, coding_model(), connect, lines=lines)
    assert code == 0
    assert connect.names == [NAME]
    assert ("create_sandbox", {}) in session.calls
    # The script runs the tests itself rather than trusting the model's summary.
    assert session.calls[-1] == ("exec", {"program": "node", "args": ["--test", "--test-reporter=tap"], "cwd": "/workspace/app"})
    assert any("app/server.js" in l for l in lines)
    assert any("1 passed, 0 failed" in l for l in lines)
    assert sb.deleted


def test_the_model_learns_no_sandbox_exists_from_the_server_error():
    session, client, connect, _ = setup()
    model = coding_model()
    run(client, model, connect)
    assert NOT_BOUND in model.requests[1]["messages"][-1]["content"]


def test_failing_tests_exit_1_and_still_delete():
    session, client, connect, sb = setup(FakeSession(test_output="# pass 0\n# fail 1\n", test_exit=1))
    lines = []
    assert run(client, coding_model(), connect, lines=lines) == 1
    assert any("0 passed, 1 failed" in l for l in lines)
    assert sb.deleted


def test_a_trail_without_the_agent_file_writes_is_a_failure():
    session, client, connect, sb = setup(pages=trail([record("exec")]))
    lines = []
    assert run(client, coding_model(), connect, lines=lines) == 1
    assert any("no file writes" in l for l in lines)
    assert sb.deleted


def test_agent_that_never_creates_a_sandbox_fails_without_deleting_anything():
    session, client, connect, sb = setup()
    lines = []
    assert run(client, FakeModel([text("I cannot do that.")]), connect, lines=lines) == 1
    assert not session.created
    assert not sb.deleted
    assert any("did not call create_sandbox, or the call failed" in l for l in lines)


def test_create_with_another_name_is_refused_so_no_untracked_sandbox_appears():
    session, client, connect, _ = setup()
    model = FakeModel([tool_call("create_sandbox", {"name": "elsewhere"}), text("ok")])
    run(client, model, connect)
    assert not session.created
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_tools_outside_the_allowlist_are_refused():
    session, client, connect, _ = setup()
    model = FakeModel([tool_call("create_sandbox", {}), tool_call("delete_sandbox", {}, "c2"), text("done")])
    run(client, model, connect)
    assert "delete_sandbox" not in [n for n, _ in session.calls]
    assert model.requests[2]["messages"][-1]["content"].startswith("error:")


def test_malformed_arguments_are_answered_not_sent_and_echoed_as_empty_json():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", None, raw='{"path": "a.js", "content": "x'), text("done")])
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", log=lambda *_: None))
    assert session.calls == []
    assert model.requests[1]["messages"][-1]["content"].startswith("error: the arguments were not valid JSON")
    assert model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_step_limit_stops_the_loop():
    session, lines = FakeSession(), []
    model = FakeModel([tool_call("fs_list", {})] * 5)
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", max_steps=3, log=lines.append))
    assert len(model.requests) == 3
    assert any("step limit of 3" in l for l in lines)


def test_time_limit_cuts_a_slow_model_call_short():
    async def slow(**kwargs):
        await asyncio.sleep(5)

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=slow)))
    lines = []
    asyncio.run(work(FakeSession(), model, "m", "task", "coding-agent-1", deadline_s=0.05, log=lines.append))
    assert any("time limit" in l for l in lines)


def test_a_tool_call_that_raises_is_an_error_for_the_model():
    session = FakeSession()
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    model = FakeModel([tool_call("exec", {"program": "sleep"}), text("done")])
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", log=lambda *_: None))
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_ctrl_c_part_way_deletes_the_sandbox_and_exits_130():
    session, client, connect, sb = setup()

    replies = iter([tool_call("create_sandbox", {})])

    async def create(**kwargs):
        try:
            return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])
        except StopIteration:
            raise KeyboardInterrupt

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(client, model, connect) == 130
    assert session.created and sb.deleted


def test_model_error_is_one_line_and_deletes():
    session, client, connect, sb = setup()
    replies = iter([tool_call("create_sandbox", {})])

    async def create(**kwargs):
        try:
            return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])
        except StopIteration:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("Error code: 401 - invalid api key")])

    lines = []
    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(client, model, connect, lines=lines) == 1
    assert any(l == "Failed: RuntimeError: Error code: 401 - invalid api key" for l in lines)
    assert sb.deleted


def test_a_failed_delete_is_reported_and_fails_an_otherwise_good_run():
    session, client, connect, sb = setup()

    def broken_delete():
        raise ConnectionError("network down")

    sb.delete = broken_delete
    lines = []
    assert run(client, coding_model(), connect, lines=lines) == 1
    assert any(l.startswith(f"Could not delete {NAME}") for l in lines)


def test_runs_get_different_sandbox_names():
    names = []
    for _ in range(2):
        session, client, connect, _ = setup()
        run(client, coding_model(), connect, name=None)
        names.append(connect.names[0])
    assert all(n.startswith("coding-agent-") for n in names)
    assert names[0] != names[1]


def test_main_exits_2_naming_missing_variables(monkeypatch, capsys):
    for name in simulate_agent.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert simulate_agent.main([]) == 2
    err = capsys.readouterr().err
    assert "NEEV_API_KEY" in err and "NEEV_MODEL_API_KEY" in err


def test_tool_results_reach_the_model_as_json():
    session = FakeSession()
    model = FakeModel([tool_call("create_sandbox", {}), tool_call("fs_write", {"path": "a.js", "content": "x"}, "c2"), text("done")])
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", log=lambda *_: None))
    assert json.loads(model.requests[2]["messages"][-1]["content"]) == {"bytes_written": 1}


def test_ctrl_c_wrapped_by_the_mcp_task_group_is_still_an_interrupt():
    session, client, _, sb = setup()

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps whatever is raised inside it.
        try:
            yield session
        except BaseException as e:
            raise BaseExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    replies = iter([tool_call("create_sandbox", {})])

    async def create(**kwargs):
        try:
            return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])
        except StopIteration:
            raise KeyboardInterrupt

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(client, model, grouping_connect) == 130
    assert sb.deleted


def test_a_bare_brace_for_a_no_argument_tool_is_read_as_no_arguments():
    session = FakeSession()
    model = FakeModel([tool_call("create_sandbox", None, raw="{"), text("done")])
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", log=lambda *_: None))
    assert session.calls == [("create_sandbox", {})]
    assert model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_a_test_run_that_hangs_is_cut_off_and_still_deletes(monkeypatch):
    session, client, connect, sb = setup()
    real_call = session.call_tool

    async def call_tool(name, arguments=None):
        if arguments == simulate_agent.TEST_RUN:
            await asyncio.sleep(5)
        return await real_call(name, arguments)

    session.call_tool = call_tool
    monkeypatch.setattr(simulate_agent, "TEST_TIMEOUT_S", 0.05)
    lines = []
    assert run(client, coding_model(), connect, lines=lines) == 1
    assert any(l.startswith("Failed: TimeoutError") for l in lines)
    assert sb.deleted


def test_time_limit_cuts_a_slow_tool_call_short():
    session = FakeSession()

    async def hang(name, arguments=None):
        await asyncio.sleep(5)

    session.call_tool = hang
    model = FakeModel([tool_call("exec", {"program": "node", "args": ["server.js"]}), text("done")])
    start = time.monotonic()
    asyncio.run(work(session, model, "m", "task", "coding-agent-1", deadline_s=0.05, log=lambda *_: None))
    assert time.monotonic() - start < 2


def test_a_multi_line_error_is_reported_on_one_line():
    session, client, connect, sb = setup()

    async def create(**kwargs):
        raise ValueError("2 validation errors\nfield a\nfield b")

    lines = []
    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(client, model, connect, lines=lines) == 1
    assert "Failed: ValueError: 2 validation errors" in lines
