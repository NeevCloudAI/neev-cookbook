import contextlib
from types import SimpleNamespace

import app_builder
from tests.fakes import FakeModel, FakeSession, tool_call


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def ready_sandbox():
    sb = SimpleNamespace(name="live-app-1", started=[], deleted=False)
    sb.wait_until_ready = lambda timeout_ms=None: sb
    sb.processes = SimpleNamespace(start=lambda program, **kw: sb.started.append(program) or SimpleNamespace(id="p1"))
    sb.get_url = lambda port, **kw: f"https://{port}-preview.example"
    sb.delete = lambda: setattr(sb, "deleted", True)
    return sb


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect


def finishing_model():
    return FakeModel([tool_call("fs_write", {"path": "index.html", "content": "<h1>x</h1>"}),
                      tool_call("finish", {"summary": "done"}, "c2")])


def quiet(**kw):
    return {"log": kw.get("log", lambda *_: None), "wait": kw.get("wait", lambda s: None)}


def test_missing_env_names_every_missing_variable():
    assert app_builder.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_happy_path_connects_mcp_to_the_new_sandbox_serves_on_0_0_0_0_and_deletes():
    sb, lines, session = ready_sandbox(), [], FakeSession()
    client, connect = FakeClient(sb), connector(session)
    code = app_builder.run("a todo app", 0, client, finishing_model(), "m", connect, **quiet(log=lines.append))
    assert code == 0
    assert client.created[0]["egress"] == {"mode": "deny_all"}
    assert connect.names == ["live-app-1"]
    assert session.files["index.html"] == "<h1>x</h1>"
    assert sb.started == [["python3", "-m", "http.server", "3000", "--bind", "0.0.0.0"]]
    assert any("https://3000-preview.example" in l for l in lines)
    assert sb.deleted


def test_builder_deletes_sandbox_when_agent_fails():
    sb = ready_sandbox()
    model = FakeModel([tool_call("fs_list", {})] * 30)
    code = app_builder.run("x", 0, FakeClient(sb), model, "m", connector(FakeSession()), **quiet())
    assert code == 1 and sb.deleted and sb.started == []


def test_keep_alive_interrupt_deletes_and_succeeds():
    sb = ready_sandbox()

    def interrupt(_seconds):
        raise KeyboardInterrupt

    code = app_builder.run("x", 10, FakeClient(sb), finishing_model(), "m", connector(FakeSession()), **quiet(wait=interrupt))
    assert code == 0 and sb.deleted


def test_unexpected_error_is_one_line_and_deletes():
    sb, lines = ready_sandbox(), []

    async def broken(**kwargs):
        raise RuntimeError("Error code: 401 - invalid api key")

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    code = app_builder.run("x", 0, FakeClient(sb), model, "m", connector(FakeSession()), **quiet(log=lines.append))
    assert code == 1 and sb.deleted
    assert any("401" in l for l in lines)


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in app_builder.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert app_builder.main(["a todo app"]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_an_error_wrapped_in_an_exception_group_reports_the_real_cause():
    sb, lines = ready_sandbox(), []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    assert app_builder.run("x", 0, FakeClient(sb), model, "m", connector(FakeSession()), **quiet(log=lines.append)) == 1
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_agent_failure_inside_the_mcp_task_group_is_still_reported_as_an_agent_failure():
    sb, lines = ready_sandbox(), []

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps errors raised inside it.
        try:
            yield FakeSession()
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    model = FakeModel([tool_call("fs_list", {})] * 30)
    assert app_builder.run("x", 0, FakeClient(sb), model, "m", grouping_connect, **quiet(log=lines.append)) == 1
    assert any(l.startswith("The agent did not finish the app: step limit") for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names():
    sb = ready_sandbox()
    client = FakeClient(sb)
    for _ in range(2):
        app_builder.run("x", 0, client, finishing_model(), "m", connector(FakeSession()), **quiet())
    names = [p["name"] for p in client.created]
    assert names[0] != names[1] and all(n.startswith("live-app-") for n in names)
