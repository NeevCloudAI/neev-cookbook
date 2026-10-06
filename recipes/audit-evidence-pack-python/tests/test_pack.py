import csv
import hashlib
from datetime import timedelta

import pack
from tests.fakes import CRED_A, CRED_B, T0, record

SINCE, UNTIL = T0 - timedelta(hours=1), T0 + timedelta(hours=1)


def trail(name, records, truncated=False, retention=30):
    return pack.Trail(name=name, sandbox_id=f"id-{name}", window_from=SINCE, window_to=UNTIL,
                      truncated=truncated, retention_days=retention, records=records)


def two_trails():
    a = trail("billing", [record(3, "exec", command="ls"), record(1, "fs.write", "notes.txt"),
                          record(5, "fs.read", ".env"), record(6, "fs.read", "nope.txt", outcome="error", reason="not_found")])
    b = trail("support", [record(2, "fs.write", "a.txt", cred=CRED_B), record(4, "fs.remove", "a.txt", cred=CRED_B),
                          record(7, "", cred=CRED_B)])
    return [a, b]


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write(tmp_path, trails=None):
    return pack.write_pack(tmp_path, trails or two_trails(), SINCE, UNTIL, generated_at=T0 + timedelta(hours=2))


def test_rows_from_every_sandbox_are_ordered_oldest_first():
    rows = pack.make_rows(two_trails())
    assert [r.record_id for r in rows] == [f"rec-{n}" for n in range(1, 8)]
    assert [r.sandbox for r in rows[:2]] == ["billing", "support"]


def test_flags_name_sensitive_reads_deletes_and_terminal_sessions():
    assert pack.flag(record(1, "fs.read", ".env")) == "sensitive read"
    assert pack.flag(record(1, "fs.read", "home/.ssh/id_ed25519")) == "sensitive read"
    assert pack.flag(record(1, "fs.read", "/etc/shadow")) == "sensitive read"
    assert pack.flag(record(1, "fs.remove", "a.txt")) == "delete"
    assert pack.flag(record(1, "exec", command="/bin/rm")) == "delete"
    assert pack.flag(record(1, "pty_command", command="bash")) == "terminal session"
    assert pack.flag(record(1, "ssh")) == "terminal session"
    assert pack.flag(record(1, "fs.read", "envelope.yaml")) is None
    assert pack.flag(record(1, "fs.write", ".env")) is None


def test_credential_summary_counts_records_errors_and_flags_per_key():
    summary = {c.credential: c for c in pack.by_credential(pack.make_rows(two_trails()))}
    a, b = summary[CRED_A], summary[CRED_B]
    assert (a.records, a.errors, a.flagged, a.sandboxes) == (4, 1, 1, ["billing"])
    assert (b.records, b.errors, b.flagged, b.sandboxes) == (3, 0, 1, ["support"])
    assert a.first_at == T0 + timedelta(seconds=1) and a.last_at == T0 + timedelta(seconds=6)
    assert b.operations["(unnamed)"] == 1 and a.operations["exec ls"] == 1


def test_pack_writes_records_credentials_summary_and_a_manifest_that_verifies(tmp_path):
    digest = write(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MANIFEST.sha256", "credentials.csv", "records.csv", "summary.md"]
    assert digest == hashlib.sha256((tmp_path / "MANIFEST.sha256").read_bytes()).hexdigest()
    assert pack.verify_manifest(tmp_path) == []
    rows = read_csv(tmp_path / "records.csv")
    assert len(rows) == 7 and rows[0]["credential"] == CRED_A and rows[0]["at"] == "2026-10-05T12:00:01Z"
    assert rows[4]["sensitive"] == "sensitive read" and rows[5]["outcome"] == "error" and rows[5]["reason_code"] == "not_found"
    assert {r["credential"] for r in read_csv(tmp_path / "credentials.csv")} == {CRED_A, CRED_B}


def test_manifest_uses_the_sha256sum_format(tmp_path):
    write(tmp_path)
    lines = (tmp_path / "MANIFEST.sha256").read_text().splitlines()
    assert [l.split("  ")[1] for l in lines] == ["credentials.csv", "records.csv", "summary.md"]
    assert lines[1].split("  ")[0] == hashlib.sha256((tmp_path / "records.csv").read_bytes()).hexdigest()


def test_an_edited_or_missing_file_fails_verification(tmp_path):
    write(tmp_path)
    (tmp_path / "records.csv").write_text("nothing to see\n")
    (tmp_path / "summary.md").unlink()
    problems = pack.verify_manifest(tmp_path)
    assert any("records.csv" in p and "does not match" in p for p in problems)
    assert any("summary.md" in p and "missing" in p for p in problems)


def test_a_value_that_already_starts_with_a_quote_stays_distinguishable(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.write", "=x"), record(2, "fs.write", "'=x")])])
    assert [r["target"] for r in read_csv(tmp_path / "records.csv")] == ["'=x", "''=x"]


def test_non_ascii_file_names_are_written_as_utf8(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.write", "रिपोर्ट.txt")])])
    assert "रिपोर्ट.txt" in (tmp_path / "records.csv").read_bytes().decode("utf-8")
    assert pack.verify_manifest(tmp_path) == []


def test_csv_cells_that_a_spreadsheet_would_run_as_formulas_are_neutralised(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.write", "=HYPERLINK(\"http://evil\")"), record(2, "fs.write", "-rf")])])
    rows = read_csv(tmp_path / "records.csv")
    assert rows[0]["target"] == "'=HYPERLINK(\"http://evil\")" and rows[1]["target"] == "'-rf"


def test_summary_escapes_crafted_targets_so_they_cannot_forge_markdown(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.read", ".env|x\n## Clean", outcome="error", reason="not_found")])])
    text = (tmp_path / "summary.md").read_text()
    assert "\n## Clean" not in text and "\\|x?## Clean" in text


def test_links_images_and_html_in_file_names_stay_inside_code_spans(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.remove", "![](https://evil.example/p.png) <details>")])])
    text = (tmp_path / "summary.md").read_text()
    assert "`![](https://evil.example/p.png) <details>`" in text
    assert text.count("![](") == 1


def test_summary_states_window_retention_what_is_not_recorded_and_makes_no_compliance_claim(tmp_path):
    write(tmp_path, [trail("billing", [record(1, "fs.write", "a")], truncated=True)] + two_trails()[1:])
    text = (tmp_path / "summary.md").read_text()
    assert "2026-10-05T11:00:00Z to 2026-10-05T13:00:00Z" in text
    assert "30 days" in text and "starts at the 30-day retention limit, later than requested" in text
    assert "arguments" in text and "file contents" in text and "Lifecycle events" in text
    assert "deleted sandbox" in text
    assert "does not by itself make" in text
    assert "shasum -a 256 -c MANIFEST.sha256" in text


def test_long_lists_in_the_summary_are_capped_and_point_to_the_csv(tmp_path):
    many = [record(n, "fs.read", f"missing-{n}", outcome="error", reason="not_found") for n in range(pack.LIST_CAP + 5)]
    write(tmp_path, [trail("x", many)])
    text = (tmp_path / "summary.md").read_text()
    assert "missing-0" in text and f"missing-{pack.LIST_CAP + 4}" not in text and "5 more in records.csv" in text


def test_terminal_summary_names_each_credential_by_its_short_prefix():
    out = pack.to_terminal(two_trails(), pack.make_rows(two_trails()))
    assert "c0de0001" in out and "01b20d7f" in out and "7 records" in out


def test_counts_read_naturally_in_the_singular_and_plural(tmp_path):
    write(tmp_path, [trail("x", [record(1, "fs.write", "a")])])
    assert "1 record from 1 sandbox under 1 credential: 0 errors, 0 sensitive operations." in (tmp_path / "summary.md").read_text()
    assert "7 records from 2 sandboxes" in pack.to_terminal(two_trails(), pack.make_rows(two_trails()))


def test_terminal_summary_of_an_empty_window_says_so():
    assert "no records in this window" in pack.to_terminal([trail("x", [])], [])
