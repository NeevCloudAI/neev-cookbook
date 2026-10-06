import contextlib
from types import SimpleNamespace

import pytest

import review_gate
from tests.fakes import FakeModel, FakeSandbox, FakeSession, tool_call

VALIDATED = review_gate.PROJECT["signup.py"].replace("def create_user(email, age):\n",
                                                      "def create_user(email, age):\n    if '@' not in email:\n        raise ValueError('email')\n")


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session."""

    @contextlib.asynccontextmanager
    async def connect(name):
        yield session

    return connect


class Clock:
    """A fake monotonic clock that sleep() advances."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def agent_model(*extra):
    """A model that reads .env, rewrites signup.py, runs the tests and finishes."""
    return FakeModel([tool_call("fs_read", {"path": ".env"}),
                      tool_call("fs_write", {"path": "signup.py", "content": VALIDATED}, "c2"),
                      *extra,
                      tool_call("exec", {"program": "python3", "args": ["-m", "unittest"]}, "c3"),
                      tool_call("finish", {"summary": "Added email validation."}, "c4")])


def run(tmp_path, model=None, decision="approve", sb=None, ask=None, lines=None, clock=None):
    sb = sb or FakeSandbox()
    clock = clock or Clock()
    lines = lines if lines is not None else []
    code = review_gate.run("task", tmp_path / "review.md", tmp_path / "approved", decision, FakeClient(sb),
                           model or agent_model(), "m", connector(FakeSession(sb)), log=lines.append,
                           ask=ask or (lambda _: pytest.fail("asked although a decision was given")),
                           sleep=clock.sleep, clock=clock)
    return code, sb, lines


def test_missing_env_names_every_missing_variable():
    assert review_gate.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_fixture_env_holds_only_obviously_fake_values():
    assert "dummy" in review_gate.PROJECT[".env"] and "example.com" in review_gate.PROJECT[".env"]


def test_approve_writes_only_the_changed_files_and_the_packet_then_deletes(tmp_path):
    code, sb, lines = run(tmp_path, decision="approve")
    assert code == 0 and sb.deleted
    out = tmp_path / "approved" / sb.name
    assert [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()] == ["signup.py"]
    assert (out / "signup.py").read_text() == VALIDATED
    md = (tmp_path / "review.md").read_text()
    assert "+    if '@' not in email:" in md and "sensitive read" in md and "Approved" in md
    assert any(str(out) in l for l in lines)


def test_reject_writes_nothing_but_the_packet(tmp_path):
    code, sb, lines = run(tmp_path, decision="reject")
    assert code == 0 and sb.deleted
    assert not (tmp_path / "approved").exists()
    assert "Rejected" in (tmp_path / "review.md").read_text()
    assert any("nothing written" in l for l in lines)


@pytest.mark.parametrize("answer,approved", [("y", True), ("YES", True), ("n", False), ("", False), ("maybe", False)])
def test_interactive_answer_defaults_to_no(tmp_path, answer, approved):
    prompts = []
    code, sb, _ = run(tmp_path, decision=None, ask=lambda p: prompts.append(p) or answer)
    assert code == 0 and prompts == ["Approve this change? [y/N] "]
    assert (tmp_path / "approved" / sb.name / "signup.py").exists() == approved


def test_closed_stdin_counts_as_reject(tmp_path):
    def eof(_):
        raise EOFError

    code, sb, _ = run(tmp_path, decision=None, ask=eof)
    assert code == 0 and sb.deleted and not (tmp_path / "approved").exists()


def test_ctrl_c_at_the_prompt_deletes_and_writes_nothing(tmp_path):
    def interrupt(_):
        raise KeyboardInterrupt

    code, sb, lines = run(tmp_path, decision=None, ask=interrupt)
    assert code == 130 and sb.deleted and not (tmp_path / "approved").exists()
    assert "Interrupted." in lines


def test_the_packet_shows_the_agents_trail_not_the_setup_or_the_diff_reads(tmp_path):
    assert run(tmp_path)[0] == 0
    md = (tmp_path / "review.md").read_text()
    activity = md.split("## What the agent did")[1].split("## Flagged")[0]
    assert activity.count("| +") == 3  # fs.read .env, fs.write signup.py, exec
    assert "exec (program not recorded)" in activity and "README.md" not in activity


def test_the_trail_is_read_after_the_agent_and_before_the_sandbox_is_deleted(tmp_path):
    sb = FakeSandbox()
    real_audit, real_delete = sb.audit, sb.delete
    trail_sizes = []  # trail length at each audit read

    def audit(**kw):
        trail_sizes.append(len(sb.trail))
        return real_audit(**kw)

    def delete():
        # the agent's three calls landed after the five uploads, and a read saw them before delete
        assert max(trail_sizes) >= len(review_gate.PROJECT) + 3, "deleted before reading the agent's trail"
        real_delete()

    sb.audit, sb.delete = audit, delete
    assert run(tmp_path, sb=sb)[0] == 0 and sb.deleted


def test_no_change_exits_1_without_asking_or_writing(tmp_path):
    model = FakeModel([tool_call("fs_list", {}), tool_call("finish", {"summary": "nothing to do"}, "c2")])
    code, sb, lines = run(tmp_path, model=model, decision=None)
    assert code == 1 and sb.deleted
    assert not (tmp_path / "approved").exists() and not (tmp_path / "review.md").exists()
    assert any("changed no files" in l for l in lines)


def test_caches_the_tests_leave_behind_are_not_a_change(tmp_path):
    sb = FakeSandbox()
    model = FakeModel([tool_call("fs_write", {"path": "__pycache__/signup.cpython-312.pyc", "content": "x"}),
                       tool_call("finish", {"summary": "ran tests"}, "c2")])
    assert run(tmp_path, model=model, sb=sb)[0] == 1


def test_a_symlink_the_agent_made_is_shown_but_never_exported(tmp_path):
    sb = FakeSandbox()
    sb.links["leak"] = "/etc/passwd"
    code, _, _ = run(tmp_path, sb=sb)
    out = tmp_path / "approved" / sb.name
    assert code == 0 and not (out / "leak").exists()
    assert "symlink to /etc/passwd" in (tmp_path / "review.md").read_text()


def test_sandbox_is_deny_all_with_a_unique_prefixed_name(tmp_path):
    names = []
    for i in range(2):
        sb = FakeSandbox()
        client = FakeClient(sb)
        review_gate.run("t", tmp_path / f"r{i}.md", tmp_path / "approved", "reject", client, agent_model(), "m",
                        connector(FakeSession(sb)), log=lambda *_: None, ask=None, sleep=Clock().sleep, clock=Clock())
        assert client.created[0]["egress"] == {"mode": "deny_all"}
        names.append(client.created[0]["name"])
    assert names[0] != names[1] and all(n.startswith("review-gate-") for n in names)


def test_trail_is_read_page_by_page(tmp_path, monkeypatch):
    monkeypatch.setattr(review_gate, "PAGE_SIZE", 2)
    code, sb, _ = run(tmp_path)
    assert code == 0 and any(c["cursor"] is not None for c in sb.audit_calls)
    assert (tmp_path / "review.md").read_text().split("## What the agent did")[1].count("| +") == 3


def test_waits_briefly_for_upload_records_still_on_their_way(tmp_path):
    code, _, _ = run(tmp_path, sb=FakeSandbox(hidden_polls=3))
    assert code == 0 and "fs.write" in (tmp_path / "review.md").read_text()


def test_waits_for_agent_records_that_land_late(tmp_path):
    sb = FakeSandbox()
    real_audit = sb.audit
    late = {"polls": 3}

    def audit(**kw):
        page = real_audit(**kw)
        if len(sb.trail) > len(review_gate.PROJECT) and late["polls"] > 0:  # the agent's last record is late
            if kw.get("cursor") is None:
                late["polls"] -= 1
            page.records = [r for r in page.records if r is not sb.trail[-1]]
        return page

    sb.audit = audit
    clock = Clock()
    code, _, _ = run(tmp_path, sb=sb, clock=clock)
    md = (tmp_path / "review.md").read_text()
    assert code == 0 and clock.now >= 2 and "Warning" not in md
    assert md.split("## What the agent did")[1].count("| +") == 3


def test_setup_boundary_waits_for_every_upload_not_just_a_count(tmp_path):
    sb = FakeSandbox()
    for _ in range(len(review_gate.PROJECT)):
        sb.record("exec", command="bootstrap")  # unrelated records already in a fresh trail
    real_audit, early = sb.audit, {"polls": 2}

    def audit(**kw):
        page = real_audit(**kw)
        if early["polls"] > 0:  # the uploads have not landed yet; only the bootstrap records show
            early["polls"] -= kw.get("cursor") is None
            page.records = [r for r in page.records if r.command == "bootstrap"]
            page.next_cursor = None
        return page

    sb.audit = audit
    code, _, _ = run(tmp_path, sb=sb)
    activity = (tmp_path / "review.md").read_text().split("## What the agent did")[1].split("## Flagged")[0]
    assert code == 0 and "bootstrap" not in activity and activity.count("| +") == 3


def test_agent_records_that_never_appear_still_go_to_review_with_a_warning(tmp_path):
    sb = FakeSandbox()
    real_audit = sb.audit
    setup = []

    def audit(**kw):
        page = real_audit(**kw)
        if not setup:  # the setup records are visible; the agent's never arrive
            setup.extend(page.records)
        page.records = [r for r in page.records if r in setup]
        page.next_cursor = None
        return page

    sb.audit = audit
    clock = Clock()
    code, _, _ = run(tmp_path, sb=sb, decision="reject", clock=clock)
    assert code == 0 and review_gate.AUDIT_WAIT_S <= clock.now <= review_gate.AUDIT_WAIT_S * 2 + 5
    assert "shows 0 of the agent's 3 calls" in (tmp_path / "review.md").read_text()


def test_setup_records_that_never_appear_fail_without_running_the_agent(tmp_path):
    model = agent_model()
    code, sb, lines = run(tmp_path, model=model, sb=FakeSandbox(hidden_polls=10_000))
    assert code == 1 and sb.deleted and model.requests == []
    assert any("audit trail" in l for l in lines)


def test_agent_failure_deletes_the_sandbox_and_writes_nothing(tmp_path):
    code, sb, lines = run(tmp_path, model=FakeModel([tool_call("delete_sandbox", {})] * 30))
    assert code == 1 and sb.deleted and not (tmp_path / "review.md").exists()
    assert any(l.startswith("The agent did not finish") for l in lines)


def test_unexpected_error_is_one_line_and_deletes(tmp_path):
    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    code, sb, lines = run(tmp_path, model=model)
    assert code == 1 and sb.deleted
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_a_failed_delete_exits_1_and_names_the_sandbox(tmp_path):
    sb = FakeSandbox()

    def broken_delete():
        raise ConnectionError("reset")

    sb.delete = broken_delete
    code, _, lines = run(tmp_path, sb=sb)
    assert code == 1 and any("Could not delete sandbox review-gate-" in l for l in lines)


def test_ctrl_c_during_create_still_deletes_a_sandbox_the_server_made(tmp_path):
    sb = FakeSandbox()
    client, lines = FakeClient(sb), []

    def create(params):
        sb.name = params["name"]
        raise KeyboardInterrupt

    client.sandboxes.create = create
    client.sandboxes.list = lambda name=None, limit=None: SimpleNamespace(items=[sb] if name == sb.name else [])
    code = review_gate.run("t", tmp_path / "r.md", tmp_path / "a", "approve", client, agent_model(), "m",
                           connector(FakeSession(sb)), log=lines.append, ask=None, sleep=Clock().sleep, clock=Clock())
    assert code == 130 and sb.deleted and "   Sandbox deleted." in lines


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in review_gate.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert review_gate.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_approve_and_reject_cannot_both_be_given(capsys):
    with pytest.raises(SystemExit):
        review_gate.main(["--approve", "--reject"])


def test_reading_the_trail_stops_after_max_pages(monkeypatch):
    calls = []

    class Endless:
        def audit(self, cursor=None, limit=None):
            calls.append(cursor)
            return SimpleNamespace(records=[], next_cursor="again", retention_days=30)

    monkeypatch.setattr(review_gate, "MAX_PAGES", 4)
    assert review_gate.read_trail(Endless()) == ([], 30)
    assert len(calls) == 4


def test_nothing_from_the_model_or_the_sandbox_reaches_the_terminal_with_control_characters(tmp_path):
    sb = FakeSandbox()
    sb.fs["evil\x1b[2J.py"] = "x"  # an unsafe name: skipped, and named in the approve output
    model = FakeModel([tool_call("fs_write", {"path": "signup.py", "content": VALIDATED}),
                       tool_call("finish", {"summary": "done\x1b]0;owned\x07"}, "c2")])
    code, _, lines = run(tmp_path, model=model, sb=sb)
    assert code == 0 and not any("\x1b" in l or "\x07" in l for l in lines)
    assert any("evil?[2J.py" in l for l in lines)


def test_a_failed_lookup_after_an_interrupted_create_names_the_sandbox(tmp_path):
    sb, lines = FakeSandbox(), []
    client = FakeClient(sb)

    def create(params):
        raise KeyboardInterrupt

    def lookup(name=None, limit=None):
        raise ConnectionError("reset")

    client.sandboxes.create, client.sandboxes.list = create, lookup
    code = review_gate.run("t", tmp_path / "r.md", tmp_path / "a", "approve", client, agent_model(), "m",
                           connector(FakeSession(sb)), log=lines.append, ask=None, sleep=Clock().sleep, clock=Clock())
    assert code == 130 and any("review-gate-" in l and "console" in l for l in lines)
