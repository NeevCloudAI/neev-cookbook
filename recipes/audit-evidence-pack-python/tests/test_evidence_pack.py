import contextlib
import csv
from datetime import datetime, timedelta, timezone

import pytest
from neevai import AuthenticationError

import evidence_pack
import pack
from tests.fakes import CRED_B, T0, FakeClient, FakeSession, Trails, record

NOW = T0 + timedelta(minutes=10)


class Clock:
    """A fake monotonic clock that sleep advances, so polling tests run instantly."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def connector(client):
    """Returns connect(name) yielding a FakeSession bound to the fake sandbox of that name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(next(s for s in client.sandboxes_made if s.name == name))

    connect.names = names
    return connect


def demo(client, out, lines=None, connect=None, clock=None):
    clock = clock or Clock()
    return evidence_pack.run_demo(client, connect or connector(client), out, log=(lines if lines is not None else []).append,
                                  sleep=clock.sleep, clock=clock, now=lambda: NOW)


def project_with_two_sandboxes():
    trails = Trails()
    trails.add("billing-agent", [record(1, "fs.write", "a.txt"), record(2, "exec", command="ls"),
                                 record(3, "fs.read", ".env")])
    trails.add("support-agent", [record(4, "fs.read", "nope", outcome="error", reason="not_found", cred=CRED_B)])
    return FakeClient(trails)


def export(client, names, out, lines=None, since=T0 - timedelta(days=1), until=NOW):
    return evidence_pack.run_export(client, names, since, until, out, log=(lines if lines is not None else []).append,
                                    now=lambda: NOW)


# --- arguments and environment


def test_missing_env_names_every_missing_variable_and_never_asks_for_a_model_key():
    assert evidence_pack.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]
    assert "NEEV_MODEL_API_KEY" not in evidence_pack.REQUIRED_ENV


def test_main_exits_2_naming_missing_env_before_creating_anything(monkeypatch, capsys, tmp_path):
    for name in evidence_pack.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert evidence_pack.main(["--demo", "--out", str(tmp_path / "p")]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    ([], "--sandbox"),
    (["--sandbox", "a", "--since", "2026-10-05", "--until", "2026-10-01"], "before"),
    (["--sandbox", "a", "--since", "last tuesday"], "--since"),
    (["--demo", "--sandbox", "a"], "either"),
])
def test_main_rejects_bad_arguments_with_exit_2(monkeypatch, capsys, tmp_path, argv, message):
    for name in evidence_pack.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert evidence_pack.main([*argv, "--out", str(tmp_path / "p")]) == 2
    assert message in capsys.readouterr().err


def test_main_refuses_to_write_into_a_folder_that_already_has_files(monkeypatch, capsys, tmp_path):
    for name in evidence_pack.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    (tmp_path / "old.csv").write_text("x")
    assert evidence_pack.main(["--sandbox", "a", "--out", str(tmp_path)]) == 2
    assert "not empty" in capsys.readouterr().err


def test_times_accept_dates_rfc3339_and_relative_days_or_hours():
    assert evidence_pack.parse_time("2026-10-01", NOW) == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert evidence_pack.parse_time("2026-10-01T09:00:00+05:30", NOW) == datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
    assert evidence_pack.parse_time("7d", NOW) == NOW - timedelta(days=7)
    assert evidence_pack.parse_time("12h", NOW) == NOW - timedelta(hours=12)
    with pytest.raises(ValueError):
        evidence_pack.parse_time("soon", NOW)


# --- reading the trail


def test_read_trail_follows_the_cursor_with_the_same_window_on_every_page():
    trails = Trails()
    trails.add("sb", [record(n, "fs.write", f"f{n}") for n in range(7)])
    since, until = T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    trail = evidence_pack.read_trail(FakeClient(trails), "sb", since, until, page_size=3)
    assert len(trail.records) == 7 and len({r.id for r in trail.records}) == 7
    assert [c[3] for c in trails.calls] == [None, "3", "6"]
    assert {(c[1], c[2], c[4]) for c in trails.calls} == {(since.isoformat(), until.isoformat(), 3)}


def test_read_trail_fails_rather_than_export_a_trail_cut_short_by_the_page_cap(monkeypatch):
    trails = Trails()
    trails.add("sb", [record(1, "fs.write", "a")])
    trails.endless = True
    monkeypatch.setattr(evidence_pack, "MAX_PAGES", 4)
    with pytest.raises(evidence_pack.PackFailed, match="narrow the window"):
        evidence_pack.read_trail(FakeClient(trails), "sb", T0 - timedelta(hours=1), NOW)


def test_read_trail_reports_the_window_the_server_served_and_truncation():
    trails = Trails(retention_days=30)
    trails.add("sb", [record(1, "fs.write", "a")])
    trail = evidence_pack.read_trail(FakeClient(trails), "sb", NOW - timedelta(days=90), NOW)
    assert trail.truncated and trail.window_from == NOW - timedelta(days=30) and trail.retention_days == 30


# --- normal mode


def test_export_writes_a_verified_pack_for_the_named_sandboxes(tmp_path):
    lines = []
    assert export(project_with_two_sandboxes(), ["billing-agent", "support-agent"], tmp_path / "p", lines) == 0
    assert pack.verify_manifest(tmp_path / "p") == []
    assert "billing-agent" in (tmp_path / "p" / "summary.md").read_text()
    assert any("MANIFEST.sha256" in l and "SHA-256" in l for l in lines)


def test_export_of_a_deleted_or_unknown_sandbox_fails_and_writes_nothing(tmp_path):
    lines = []
    assert export(project_with_two_sandboxes(), ["billing-agent", "gone"], tmp_path / "p", lines) == 1
    assert not (tmp_path / "p").exists()
    assert any("gone" in l and "deleted sandbox" in l for l in lines)


def test_export_fails_when_the_written_pack_does_not_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "verify_manifest", lambda out: ["records.csv: does not match"])
    lines = []
    assert export(project_with_two_sandboxes(), ["billing-agent"], tmp_path / "p", lines) == 1
    assert any("does not match" in l for l in lines)


def test_export_rejected_key_is_one_line(tmp_path):
    client = project_with_two_sandboxes()

    def refuse(id, **kw):
        raise AuthenticationError(401, {"code": "unauthorized", "message": "invalid api key"}, "r")

    client.sandboxes.audit = refuse
    lines = []
    assert export(client, ["billing-agent"], tmp_path / "p", lines) == 1
    assert lines[-1].startswith("Failed: AuthenticationError: HTTP 401")


# --- demo mode


def test_demo_does_sdk_and_mcp_work_exports_before_deleting_and_succeeds(tmp_path):
    client, lines = FakeClient(), []
    connect = connector(client)
    assert demo(client, tmp_path / "p", lines, connect) == 0
    names = [p["name"] for p in client.created]
    assert len(names) == 2 and all(n.startswith("evidence-") for n in names)
    assert all(p["egress"] == {"mode": "deny_all"} for p in client.created)
    assert connect.names == [names[1]]
    assert all(s.deleted for s in client.sandboxes_made)
    last_audit = max(i for i, e in enumerate(client.events) if e[0] == "audit")
    first_delete = min(i for i, e in enumerate(client.events) if e[0] == "delete")
    assert last_audit < first_delete
    assert pack.verify_manifest(tmp_path / "p") == []
    rows = pack.make_rows([evidence_pack.read_trail(FakeClient(client.trails), n, T0 - timedelta(hours=1), NOW) for n in names])
    tools = {(r.sandbox, r.operation, r.outcome) for r in rows}
    assert (names[0], "fs.read", "error") in tools and (names[0], "process.start sleep", "success") in tools
    assert (names[1], "exec (program not recorded)", "success") in tools and (names[1], "fs.read", "error") in tools
    summary = (tmp_path / "p" / "summary.md").read_text()
    assert "sensitive read" in summary and "delete" in summary


def test_demo_waits_for_late_records_before_exporting(tmp_path):
    client, clock = FakeClient(), Clock()
    real_audit = client.trails.audit
    reads = []

    def late(id, **kw):
        page = real_audit(id, **kw)
        reads.append(id)
        return page if len(reads) > 4 else page.model_copy(update={"records": [], "next_cursor": None})

    client.trails.audit = late
    assert demo(client, tmp_path / "p", clock=clock) == 0
    assert clock.slept and len(reads) > 4


def test_demo_fails_without_a_pack_when_records_never_arrive_and_still_deletes(tmp_path):
    client, clock, lines = FakeClient(), Clock(), []
    real_audit = client.trails.audit
    client.trails.audit = lambda id, **kw: real_audit(id, **kw).model_copy(update={"records": [], "next_cursor": None})
    assert demo(client, tmp_path / "p", lines, clock=clock) == 1
    assert clock.t >= evidence_pack.AUDIT_WAIT_S
    assert not (tmp_path / "p").exists() and all(s.deleted for s in client.sandboxes_made)


def test_demo_deletes_both_sandboxes_when_the_mcp_step_fails(tmp_path):
    client, lines = FakeClient(), []

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps errors raised inside it.
        try:
            raise ConnectionError("MCP stream closed")
            yield
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    assert demo(client, tmp_path / "p", lines, grouping_connect) == 1
    assert any("ConnectionError: MCP stream closed" in l for l in lines)
    assert len(client.sandboxes_made) == 2 and all(s.deleted for s in client.sandboxes_made)


def test_demo_ctrl_c_deletes_every_sandbox_and_exits_130(tmp_path):
    client = FakeClient()
    original = client.sandboxes.create

    def create_then_interrupt(params):
        sb = original(params)
        if len(client.sandboxes_made) == 2:  # interrupt the first piece of SDK work, once both exist
            client.sandboxes_made[0].files.write = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        return sb

    client.sandboxes.create = create_then_interrupt
    assert demo(client, tmp_path / "p") == 130
    assert all(s.deleted for s in client.sandboxes_made)


def test_demo_fails_when_a_sandbox_cannot_be_deleted_even_though_the_pack_is_good(tmp_path):
    client, lines = FakeClient(), []
    original = client.sandboxes.create

    def create(params):
        sb = original(params)
        sb.delete_error = RuntimeError("HTTP 503")
        return sb

    client.sandboxes.create = create
    assert demo(client, tmp_path / "p", lines) == 1
    assert pack.verify_manifest(tmp_path / "p") == []
    assert any("Could not delete" in l and "evidence-" in l for l in lines)


def test_demo_names_differ_between_runs(tmp_path):
    client = FakeClient()
    demo(client, tmp_path / "a")
    demo(client, tmp_path / "b")
    names = [p["name"] for p in client.created]
    assert len(set(names)) == 4


def test_demo_waits_for_a_late_operation_even_when_another_record_already_makes_up_the_count(tmp_path):
    client, clock = FakeClient(), Clock()
    real_audit = client.trails.audit
    reads = []

    def late_process_start(id, **kw):
        page = real_audit(id, **kw)
        reads.append(id)
        if id.startswith("evidence-mcp-") and len(reads) <= 4:
            # The refused fs_read adds a blank record, so 4 records arrive while process.start is still missing.
            kept = [r for r in page.records if r.tool != "process.start"]
            return page.model_copy(update={"records": kept})
        return page

    client.trails.audit = late_process_start
    assert demo(client, tmp_path / "p", clock=clock) == 0
    assert clock.slept
    assert "process.start" in {r["tool"] for r in csv.DictReader(open(tmp_path / "p" / "records.csv")) if r["sandbox"].startswith("evidence-mcp-")}


def test_demo_deletes_a_sandbox_whose_create_call_failed_after_the_server_made_it(tmp_path):
    client, lines = FakeClient(), []
    original = client.sandboxes.create

    def create(params):
        sb = original(params)
        if len(client.sandboxes_made) == 2:  # the server made it, but the response never arrived
            raise TimeoutError("read timed out")
        return sb

    client.sandboxes.create = create
    assert demo(client, tmp_path / "p", lines) == 1
    assert len(client.sandboxes_made) == 2 and all(s.deleted for s in client.sandboxes_made)
    assert any("TimeoutError" in l for l in lines)
