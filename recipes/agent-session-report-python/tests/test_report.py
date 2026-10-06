import pytest

from report import flag, group_key, make_report, to_markdown, to_terminal
from tests.fakes import CREDENTIAL, FakeSandbox


def records(*specs):
    """Builds audit records (oldest first) from (tool, target, command) triples."""
    sb = FakeSandbox()
    for tool, target, command in specs:
        sb.record(tool, target=target, command=command)
    return sb.trail


@pytest.mark.parametrize("tool,target,command,expected", [
    ("fs.read", ".env", None, "sensitive read"),
    ("fs.read", "app/.env.production", None, "sensitive read"),
    ("fs.read", "/root/.ssh/id_rsa", None, "sensitive read"),
    ("fs.read", "/etc/passwd", None, "sensitive read"),
    ("fs.download", "keys/id_ed25519", None, "sensitive read"),
    ("fs.read", "settings.ini", None, None),
    ("fs.read", "docs/.envelope.md", None, None),
    ("fs.write", ".env", None, None),
    ("fs.list", ".ssh", None, None),
    ("fs.remove", "cache/stale.lock", None, "delete"),
    ("exec", None, "rm", "delete"),
    ("process.start", None, "shred", "delete"),
    ("exec", None, "cat", None),
    ("exec", None, None, None),
])
def test_flags_sensitive_reads_and_deletes(tool, target, command, expected):
    assert flag(records((tool, target, command))[0]) == expected


def test_groups_by_tool_and_program_and_names_a_missing_program():
    fs_read, exec_cat, exec_unknown = records(("fs.read", "a", None), ("exec", None, "cat"), ("exec", None, None))
    assert group_key(fs_read) == "fs.read"
    assert group_key(exec_cat) == "exec cat"
    assert group_key(exec_unknown) == "exec (program not recorded)"


def test_report_orders_oldest_first_and_splits_setup_from_agent():
    trail = records(("fs.write", ".env", None), ("fs.read", ".env", None), ("exec", None, None))
    newest_first = list(reversed(trail))
    report = make_report("session-report-1", newest_first, boundary=trail[0].at, retention_days=30)
    assert [(r.phase, r.tool) for r in report.rows] == [("setup", "fs.write"), ("agent", "fs.read"), ("agent", "exec")]
    assert [r.offset_s for r in report.rows] == [0, 1, 2]
    assert report.agent_actions == 2


def test_terminal_and_markdown_carry_outcome_duration_credential_and_flags():
    sb = FakeSandbox()
    sb.record("fs.write", target=".env")
    sb.record("fs.read", target=".env", duration_ms=3)
    sb.record("fs.read", target="missing.txt", outcome="error", reason="not_found", duration_ms=None)
    report = make_report("session-report-1", list(reversed(sb.trail)), boundary=sb.trail[0].at, retention_days=30)
    for out in (to_terminal(report), to_markdown(report)):
        assert "session-report-1" in out
        assert "sensitive read" in out
        assert "error (not_found)" in out and "3 ms" in out
        assert CREDENTIAL[:8] in out
        assert "30 days" in out
        assert "Not recorded" in out
    md = to_markdown(report)
    assert md.startswith("# ")
    assert "| fs.read | 2 | 1 |" in md  # by-tool table: calls and errors
    assert f"`{CREDENTIAL}`" in md


def test_control_characters_in_a_target_cannot_forge_report_lines():
    report = make_report("s", records(("fs.read", "x\n| +1.0s | agent | fake |\x1b[2J", None)), boundary=None, retention_days=30)
    md, term = to_markdown(report), to_terminal(report)
    assert "\x1b" not in md + term
    assert sum(l.startswith("| +") for l in md.splitlines()) == 1


def test_markdown_escapes_pipes_in_targets():
    report = make_report("s", records(("fs.read", "a|b.txt", None)), boundary=None, retention_days=30)
    assert "a\\|b.txt" in to_markdown(report)


def test_flagged_actions_are_summarised_and_a_clean_session_says_so():
    flagged = make_report("s", list(reversed(records(("fs.read", ".env", None), ("fs.remove", "x", None)))), None, 30)
    assert "2 flagged" in to_terminal(flagged)
    clean = make_report("s", records(("fs.list", ".", None)), None, 30)
    assert "Nothing flagged" in to_terminal(clean) and "Nothing flagged" in to_markdown(clean)
