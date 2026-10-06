import contextlib
from types import SimpleNamespace

import session_report
from tests.fakes import FakeModel, FakeSandbox, FakeSession, tool_call


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        session.files.update(session.sandbox_files)  # the seeded project, as the agent would see it
        yield session

    connect.names = names
    return connect


class Clock:
    """A fake monotonic clock that sleep() advances."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def setup(hidden_polls=0):
    sb = FakeSandbox(hidden_polls=hidden_polls)
    session = FakeSession(sb)
    session.sandbox_files = dict(session_report.PROJECT)
    return sb, session


def agent_model():
    return FakeModel([tool_call("fs_read", {"path": "README.md"}),
                      tool_call("fs_read", {"path": ".env"}, "c2"),
                      tool_call("exec", {"program": "python3", "args": ["app.py", "--check"]}, "c3"),
                      tool_call("finish", {"summary": "port 8080; SMTP_PASSWORD missing"}, "c4")])


def run(sb, session, model, tmp_path, lines=None, clock=None):
    clock = clock or Clock()
    out = tmp_path / "report.md"
    code = session_report.run("task", out, FakeClient(sb), model, "m", connector(session),
                              log=(lines if lines is not None else []).append, sleep=clock.sleep, clock=clock)
    return code, out


def test_missing_env_names_every_missing_variable():
    assert session_report.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_project_seeds_a_dummy_env_that_is_obviously_fake():
    env = session_report.PROJECT[".env"]
    assert "not-a-real" in env or "dummy" in env


def test_happy_path_writes_a_report_with_the_agents_actions_and_deletes(tmp_path):
    sb, session = setup()
    lines = []
    code, out = run(sb, session, agent_model(), tmp_path, lines)
    assert code == 0 and sb.deleted
    md = out.read_text()
    assert "sensitive read" in md and ".env" in md
    assert "exec (program not recorded)" in md
    assert md.count("| setup |") == len(session_report.PROJECT)
    assert md.count("| agent |") == 3
    assert any("Report written" in l for l in lines)


def test_sandbox_is_deny_all_with_a_unique_prefixed_name(tmp_path):
    names = []
    for _ in range(2):
        sb, session = setup()
        client = FakeClient(sb)
        session_report.run("t", tmp_path / "r.md", client, agent_model(), "m", connector(session),
                           log=lambda *_: None, sleep=Clock().sleep, clock=Clock())
        assert client.created[0]["egress"] == {"mode": "deny_all"}
        names.append(client.created[0]["name"])
    assert names[0] != names[1] and all(n.startswith("session-report-") for n in names)


def test_trail_is_read_page_by_page(tmp_path, monkeypatch):
    monkeypatch.setattr(session_report, "PAGE_SIZE", 3)
    sb, session = setup()
    code, out = run(sb, session, agent_model(), tmp_path)
    md = out.read_text()
    assert code == 0  # every page reached the report, not just the last one
    assert md.count("| setup |") == len(session_report.PROJECT) and md.count("| agent |") == 3
    cursors = [c["cursor"] for c in sb.audit_calls]
    assert any(c is not None for c in cursors)  # more records than one page holds
    assert all(c["limit"] == session_report.PAGE_SIZE for c in sb.audit_calls)


def test_waits_briefly_for_records_still_on_their_way(tmp_path):
    sb, session = setup(hidden_polls=3)
    code, out = run(sb, session, agent_model(), tmp_path)
    assert code == 0 and "| agent |" in out.read_text()


def test_setup_records_that_never_appear_fail_without_running_the_agent(tmp_path):
    sb, session = setup(hidden_polls=10_000)
    lines, model = [], agent_model()
    code, out = run(sb, session, model, tmp_path, lines)
    assert code == 1 and sb.deleted and not out.exists()
    assert model.requests == []
    assert any("audit trail" in l for l in lines)


def test_agent_records_that_never_appear_fail_after_the_bound(tmp_path):
    sb, session = setup()
    real_audit = sb.audit
    seen_setup = []

    def audit(**kw):
        page = real_audit(**kw)
        if not seen_setup:  # the setup records are visible; after that, nothing new ever arrives
            seen_setup.extend(page.records)
        page.records = [r for r in page.records if r in seen_setup]
        page.next_cursor = None
        return page

    sb.audit = audit
    clock, lines = Clock(), []
    code, out = run(sb, session, agent_model(), tmp_path, lines, clock)
    assert code == 1 and sb.deleted
    assert session_report.AUDIT_WAIT_S <= clock.now <= session_report.AUDIT_WAIT_S * 2 + 5
    assert any("no agent actions" in l for l in lines)


def test_agent_failure_deletes_the_sandbox_and_writes_no_report(tmp_path):
    sb, session = setup()
    lines = []
    code, out = run(sb, session, FakeModel([tool_call("delete_sandbox", {})] * 30), tmp_path, lines)
    assert code == 1 and sb.deleted and not out.exists()
    assert any(l.startswith("The agent did not finish") for l in lines)


def test_ctrl_c_deletes_the_sandbox_and_exits_130(tmp_path):
    sb, session = setup()

    async def interrupted(**kwargs):
        raise KeyboardInterrupt

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=interrupted)))
    lines = []
    code, out = run(sb, session, model, tmp_path, lines)
    assert code == 130 and sb.deleted and not out.exists()
    assert "Interrupted." in lines


def test_unexpected_error_is_one_line_and_deletes(tmp_path):
    sb, session = setup()
    lines = []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    code, _ = run(sb, session, model, tmp_path, lines)
    assert code == 1 and sb.deleted
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in session_report.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert session_report.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_waits_for_every_agent_call_before_writing_the_report(tmp_path):
    sb, session = setup()
    real_audit = sb.audit
    state = {"setup": None, "late_polls": 3}

    def audit(**kw):
        page = real_audit(**kw)
        if state["setup"] is None:
            state["setup"] = list(page.records)
        elif len(sb.trail) > len(state["setup"]) and state["late_polls"] > 0:
            # the agent's last call lands a few polls late
            if kw.get("cursor") is None:
                state["late_polls"] -= 1
            page.records = [r for r in page.records if r is not sb.trail[-1]]
        return page

    sb.audit = audit
    clock = Clock()
    code, out = run(sb, session, agent_model(), tmp_path, clock=clock)
    assert code == 0 and out.read_text().count("| agent |") == 3
    assert clock.now >= 2


def test_ctrl_c_during_create_still_deletes_a_sandbox_the_server_made(tmp_path):
    sb, session = setup()
    client, lines = FakeClient(sb), []

    def create(params):
        sb.name = params["name"]
        raise KeyboardInterrupt

    client.sandboxes.create = create
    client.sandboxes.list = lambda name=None, limit=None: SimpleNamespace(items=[sb] if name == sb.name else [])
    code = session_report.run("t", tmp_path / "r.md", client, agent_model(), "m", connector(session),
                              log=lines.append, sleep=Clock().sleep, clock=Clock())
    assert code == 130 and sb.deleted and "   Sandbox deleted." in lines


def test_a_failed_delete_keeps_the_result_and_names_the_sandbox(tmp_path):
    sb, session = setup()

    def broken_delete():
        raise ConnectionError("reset")

    sb.delete = broken_delete
    lines = []
    code, out = run(sb, session, agent_model(), tmp_path, lines)
    assert code == 0 and out.exists()
    assert any("Could not delete sandbox session-report-" in l for l in lines)


def test_reading_the_trail_stops_after_max_pages(monkeypatch):
    calls = []

    class Endless:
        def audit(self, cursor=None, limit=None):
            calls.append(cursor)
            return SimpleNamespace(records=[], next_cursor="again", retention_days=30)

    monkeypatch.setattr(session_report, "MAX_PAGES", 4)
    assert session_report.read_trail(Endless()) == ([], 30)
    assert len(calls) == 4
