import verify
from tests.fakes import FakeClient, FakeSandbox, record, trail


def no_sleep(_seconds):
    pass


def test_read_trail_follows_every_cursor_and_returns_oldest_first():
    newest = trail([record("exec", rid="3", at="2026-10-05T15:21:35Z"), record("fs.read", "a.js", rid="2", at="2026-10-05T15:21:34Z")], next_cursor="1")
    oldest = trail([record("fs.write", "a.js", rid="1", at="2026-10-05T15:21:33Z")])
    sb = FakeSandbox("coding-agent-1", pages=[newest, oldest])
    records, truncated = verify.read_trail(sb)
    assert [r.id for r in records] == ["1", "2", "3"]
    assert not truncated
    assert [c["cursor"] for c in sb.audit_calls] == [None, "1"]


def test_read_trail_asks_for_the_sandbox_whole_life_not_the_default_day():
    sb = FakeSandbox("coding-agent-1", created_at="2026-09-01T00:00:00Z")
    verify.read_trail(sb)
    assert sb.audit_calls[0]["from_"] == "2026-09-01T00:00:00Z"


def test_read_trail_reports_a_window_older_than_retention():
    sb = FakeSandbox("coding-agent-1", pages=[trail([record("exec")], truncated=True)])
    assert verify.read_trail(sb)[1] is True


def test_settled_trail_rereads_until_late_records_stop_arriving():
    first = trail([record("fs.write", "a.js", rid="1")])
    full = trail([record("exec", rid="2"), record("fs.write", "a.js", rid="1")])
    sb = FakeSandbox("coding-agent-1")
    reads = iter([first, full, full])
    sb.audit = lambda **kw: next(reads)
    records, _ = verify.settled_trail(sb, sleep=no_sleep)
    assert [r.id for r in records] == ["1", "2"]


def test_settled_trail_gives_up_after_its_read_limit():
    sb = FakeSandbox("coding-agent-1")
    counter = iter(range(100))
    sb.audit = lambda **kw: trail([record("exec", rid=str(i)) for i in range(next(counter) + 1)])
    records, _ = verify.settled_trail(sb, sleep=no_sleep, max_reads=4)
    assert len(records) == 4


def test_timeline_shows_time_operation_program_target_outcome_and_credential():
    records, _ = verify.read_trail(FakeSandbox("s", pages=[trail([
        record("fs.read", "nope.txt", outcome="error", reason="not_found", at="2026-10-05T15:21:35Z", duration_ms=None, rid="2"),
        record("process.start", command="sleep", at="2026-10-05T15:21:34Z", rid="1")])]))
    lines = verify.timeline(records)
    assert "15:21:34" in lines[1] and "process.start" in lines[1] and "sleep" in lines[1] and "success" in lines[1]
    assert "nope.txt" in lines[2] and "error (not_found)" in lines[2]
    assert "key-1111" in lines[1]


def test_summary_counts_operations_per_credential_and_errors():
    records, _ = verify.read_trail(FakeSandbox("s", pages=[trail([
        record("exec", rid="3", caller="key-B"), record("fs.read", "x", outcome="error", reason="not_found", rid="2"),
        record("fs.write", "x", rid="1")])]))
    text = "\n".join(verify.summary(records))
    assert "3 operations" in text and "1 ended in an error" in text
    assert "key-1111-2222: 2" in text and "key-B: 1" in text


def test_report_fails_when_the_sandbox_does_not_exist():
    lines = []
    client = FakeClient(FakeSandbox("coding-agent-1"))
    assert verify.report(client, "someone-else", log=lines.append, sleep=no_sleep) == 1
    assert any("No sandbox named someone-else" in l for l in lines)


def test_report_fails_when_nothing_was_recorded():
    lines = []
    client = FakeClient(FakeSandbox("coding-agent-1"))
    assert verify.report(client, "coding-agent-1", log=lines.append, sleep=no_sleep) == 1
    assert any("No operations recorded" in l for l in lines)


def test_report_prints_the_timeline_and_succeeds():
    lines = []
    client = FakeClient(FakeSandbox("coding-agent-1", pages=[trail([record("fs.write", "app/server.js")])]))
    assert verify.report(client, "coding-agent-1", log=lines.append, sleep=no_sleep) == 0
    assert any("app/server.js" in l for l in lines)


def test_main_exits_2_naming_missing_variables(monkeypatch, capsys):
    for name in verify.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert verify.main(["coding-agent-1"]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err
