import contextlib

import coding_tutor
from tests.fakes import HINTS, FakeClient, FakeModel, FakeSandbox, FakeSession, fake_ssh, tool_call


class Clock:
    """A fake monotonic clock that only moves when the code sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect


def tutor_model():
    return FakeModel([tool_call("exec", {"program": "python3", "args": ["check.py"]}),
                      tool_call("finish", {"hints": HINTS}, "c2")])


def go(sb, model=None, session=None, keep=0, wait=None, lines=None, fetch=None, client=None):
    clock = Clock()
    session = session or FakeSession(sb)
    connect = connector(session)
    code = coding_tutor.run(client or FakeClient(sb), model or tutor_model(), "m", connect, keep,
                            log=(lines.append if lines is not None else lambda *_: None), ssh=fake_ssh,
                            fetch=fetch or sb.fetch, sleep=clock.sleep, clock=clock, wait=wait or (lambda s: None))
    return code, connect


def test_missing_env_names_every_missing_variable():
    assert coding_tutor.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_happy_path_student_edits_over_ssh_tutor_hints_and_state_survives_pause_resume():
    sb, lines, client = FakeSandbox(), [], None
    client = FakeClient(sb)
    code, connect = go(sb, lines=lines, client=client)
    assert code == 0, lines
    assert client.created[0]["egress"] == {"mode": "deny_all"}
    assert client.created[0]["name"].startswith("tutor-")
    # the exercise is seeded, and the student's attempt arrives over SSH
    assert {"EXERCISE.md", "check.py", "index.html", "TUTOR_NOTES.md"} <= set(sb.workspace)
    assert 'return len(text.split(" "))' in sb.workspace["app.py"]
    assert sb.tunnels[0].commands == ["cat > app.py"]
    assert sb.started == ["python3", "app.py"]
    # the tutor is bound to this sandbox, and its hints are printed and left in the box
    assert connect.names == [sb.name]
    assert all(h in sb.workspace["TUTOR_NOTES.md"] for h in HINTS)
    assert any(HINTS[0] in l for l in lines)
    # session 2 reads the student's work back over a fresh tunnel after the resume
    assert sb.tunnels[1].commands == ["cat app.py", "cat TUTOR_NOTES.md"]
    assert all(t.closed for t in sb.tunnels)
    assert any('200 {"text": "two  spaces", "words": 3}' in l for l in lines)
    assert any(l.startswith("Session state survived") for l in lines)
    assert sb.deleted


def test_a_tutor_that_changes_the_students_code_fails_the_run():
    sb, lines = FakeSandbox(), []

    class RewritingSession(FakeSession):
        async def call_tool(self, name, arguments=None):
            if name == "exec":  # exec is the tutor's only way to change a file
                self.sandbox.workspace["app.py"] = "def count_words(text): return len(text.split())"
            return await super().call_tool(name, arguments)

    code, _ = go(sb, session=RewritingSession(sb), lines=lines)
    assert code == 1 and sb.deleted
    assert any("app.py" in l and "changed" in l for l in lines)


def test_a_server_that_did_not_survive_the_resume_fails_the_check():
    sb, lines = FakeSandbox(server_survives=False), []
    code, _ = go(sb, lines=lines)
    assert code == 1 and sb.deleted
    assert any(l.strip().startswith("FAILED") and "process" in l for l in lines)
    assert all(t.closed for t in sb.tunnels)


def test_a_student_edit_lost_across_the_resume_fails_the_check():
    sb, lines = FakeSandbox(), []
    resume = sb.resume

    def resume_from_scratch():
        sb.workspace["app.py"] = (coding_tutor.EXERCISE_DIR / "app.py").read_text()
        return resume()

    sb.resume = resume_from_scratch
    code, _ = go(sb, lines=lines)
    assert code == 1 and sb.deleted
    assert any(l.strip().startswith("FAILED") and "student's edit" in l for l in lines)


def test_a_preview_url_that_never_answers_fails_and_deletes():
    sb, lines = FakeSandbox(), []
    code, _ = go(sb, lines=lines, fetch=lambda url: (502, {"error": "bad gateway"}))
    assert code == 1 and sb.deleted
    assert any("preview URL did not answer" in l for l in lines)


def test_a_resume_that_never_answers_times_out_and_deletes():
    sb, lines = FakeSandbox(resume_polls=10**9), []
    code, _ = go(sb, lines=lines)
    assert code == 1 and sb.deleted
    assert any("did not answer within" in l for l in lines)


def test_tutor_failure_deletes_the_sandbox():
    sb, lines = FakeSandbox(), []
    code, _ = go(sb, model=FakeModel([tool_call("fs_list", {})] * 20), lines=lines)
    assert code == 1 and sb.deleted
    assert any(l.startswith("The tutor did not give hints: step limit") for l in lines)


def test_ctrl_c_part_way_deletes_and_exits_130():
    sb = FakeSandbox()

    class Interrupting(FakeSession):
        async def call_tool(self, name, arguments=None):
            raise KeyboardInterrupt

    code, _ = go(sb, session=Interrupting(sb))
    assert code == 130 and sb.deleted
    assert all(t.closed for t in sb.tunnels)


def test_keep_prints_the_ssh_command_and_url_and_ctrl_c_still_succeeds():
    sb, lines, waited = FakeSandbox(), [], []

    def interrupt(seconds):
        waited.append(seconds)
        raise KeyboardInterrupt

    code, _ = go(sb, keep=5, wait=interrupt, lines=lines)
    assert code == 0 and sb.deleted and waited == [300]
    port = sb.tunnels[1].port
    assert any(f"ssh -p {port} " in l and "root@127.0.0.1" in l for l in lines)
    assert any("https://8000-preview.example" in l for l in lines)
    assert all(t.closed for t in sb.tunnels)


def test_delete_failure_is_reported_and_the_exit_code_is_kept():
    sb, lines = FakeSandbox(), []

    def broken_delete():
        raise ConnectionError("network down")

    sb.delete = broken_delete
    code, _ = go(sb, lines=lines)
    assert code == 0
    assert any("Could not delete sandbox tutor-1" in l for l in lines)


def test_an_error_wrapped_in_an_exception_group_reports_the_real_cause():
    sb, lines = FakeSandbox(), []

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])
        yield  # pragma: no cover

    clock = Clock()
    code = coding_tutor.run(FakeClient(sb), tutor_model(), "m", grouping_connect, 0, log=lines.append,
                            ssh=fake_ssh, fetch=sb.fetch, sleep=clock.sleep, clock=clock, wait=lambda s: None)
    assert code == 1 and sb.deleted
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names():
    names = []
    for _ in range(2):
        sb = FakeSandbox()
        client = FakeClient(sb)
        go(sb, client=client)
        names.append(client.created[0]["name"])
    assert names[0] != names[1]


def test_main_exits_2_when_env_is_missing(monkeypatch, capsys):
    for name in coding_tutor.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert coding_tutor.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_main_exits_2_when_no_ssh_client_is_installed(monkeypatch, capsys):
    for name in coding_tutor.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setattr(coding_tutor.shutil, "which", lambda name: None)
    assert coding_tutor.main([]) == 2
    assert "ssh" in capsys.readouterr().err


def test_describe_renders_answers_for_the_log():
    assert coding_tutor.describe(200, {"words": 3}) == '200 {"words": 3}'
    assert coding_tutor.describe(503, {}) == "503"
    assert coding_tutor.describe(None, {"error": "unreachable (ConnectError)"}) == "not answering: unreachable (ConnectError)"


def test_a_preview_url_that_answers_differently_after_the_resume_fails_the_check():
    sb, lines = FakeSandbox(), []
    resume = sb.resume

    def resume_with_a_different_server():
        sb.server = coding_tutor.EXERCISE_DIR.joinpath("app.py").read_text()  # a server running other code
        return resume()

    sb.resume = resume_with_a_different_server
    code, _ = go(sb, lines=lines)
    assert code == 1 and sb.deleted
    assert any(l.strip().startswith("FAILED") and "preview" in l and '"words": 2' in l for l in lines)


def test_a_tutor_that_stops_the_students_server_fails_the_run():
    sb, lines = FakeSandbox(), []

    class KillingSession(FakeSession):
        async def call_tool(self, name, arguments=None):
            if name == "exec":
                self.sandbox.server, self.sandbox.process_state = None, "exited"
            return await super().call_tool(name, arguments)

    code, _ = go(sb, session=KillingSession(sb), lines=lines)
    assert code == 1 and sb.deleted
    assert any("server is exited after the review" in l for l in lines)


def test_a_pause_that_never_completes_times_out_and_deletes():
    sb, lines = FakeSandbox(pause_polls=10**9), []
    code, _ = go(sb, lines=lines)
    assert code == 1 and sb.deleted
    assert any("did not reach Paused within" in l for l in lines)


def test_ctrl_c_while_paused_deletes_and_exits_130():
    sb, lines = FakeSandbox(), []

    def interrupted_resume():
        raise KeyboardInterrupt

    sb.resume = interrupted_resume
    code, _ = go(sb, lines=lines)
    assert code == 130 and sb.deleted and sb.phase == "Paused"
