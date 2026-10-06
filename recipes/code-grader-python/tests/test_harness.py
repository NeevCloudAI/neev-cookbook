"""Runs harness.py for real, as the sandbox does, against the shipped submissions and a few inline ones."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

RECIPE = Path(__file__).resolve().parent.parent


def grade(tmp_path, source, timeout_s=0.5, nonce="n0nce"):
    """Lays out grader/ and submission/ like the sandbox, runs the harness, and returns (report, stdout)."""
    (tmp_path / "grader").mkdir()
    (tmp_path / "submission").mkdir()
    shutil.copy(RECIPE / "harness.py", tmp_path / "grader")
    shutil.copy(RECIPE / "assignment" / "test_top_words.py", tmp_path / "grader")
    (tmp_path / "submission" / "solution.py").write_text(source)
    run = subprocess.run([sys.executable, "-I", "grader/harness.py"], cwd=tmp_path, capture_output=True, text=True,
                         input=json.dumps({"nonce": nonce, "timeout_s": timeout_s}) + "\n", timeout=60)
    assert run.returncode == 0, run.stderr
    lines = [line for line in run.stdout.splitlines() if line.startswith(f"GRADE {nonce} ")]
    assert len(lines) == 1, run.stdout
    return json.loads(lines[0].split(" ", 2)[2]), run.stdout


def sample(name):
    return (RECIPE / "submissions" / name).read_text()


def test_correct_submission_passes_every_test(tmp_path):
    report, _ = grade(tmp_path, sample("correct.py"))
    assert report["total"] == 6 and report["passed"] == 6
    assert report["failed"] == [] and report["timeouts"] == 0 and report["blocked"] == []


def test_partial_submission_names_the_tests_it_failed(tmp_path):
    report, _ = grade(tmp_path, sample("partial.py"))
    assert report["passed"] == 4
    assert report["failed"] == ["test_punctuation_separates_words", "test_ties_break_alphabetically"]


def test_wrong_submission_passes_nothing(tmp_path):
    report, _ = grade(tmp_path, sample("wrong.py"))
    assert report["passed"] == 0 and report["timeouts"] == 0


def test_infinite_loop_times_out_only_the_test_that_loops(tmp_path):
    report, _ = grade(tmp_path, sample("infinite_loop.py"))
    assert report["passed"] == 5 and report["timeouts"] == 1
    assert report["failed"] == ["test_punctuation_separates_words"]


def test_cheater_is_flagged_and_its_patches_and_fake_output_do_nothing(tmp_path):
    report, stdout = grade(tmp_path, sample("cheater.py"))
    assert report["passed"] == 0  # assertEqual was patched in the submission's process, not the tests'
    assert any("test_top_words.py" in b for b in report["blocked"])
    assert "GRADE 6/6" not in stdout  # its print went to its own captured output, not the harness's


def test_network_attempt_is_flagged(tmp_path):
    source = "import socket\ntry:\n    socket.getaddrinfo('localhost', 9)\nexcept OSError:\n    pass\n" + sample("correct.py")
    report, _ = grade(tmp_path, source)
    assert report["blocked"] == ["network access to localhost"]


def test_a_crash_at_import_fails_every_test(tmp_path):
    report, _ = grade(tmp_path, "raise SystemExit('boom')\n")
    assert report["passed"] == 0 and len(report["failed"]) == 6


def test_a_forged_result_line_without_the_nonce_is_not_the_result(tmp_path):
    source = "print('GRADE n0nce {\"total\": 6, \"passed\": 6}')\nimport sys\nsys.stdout.flush()\n" + sample("wrong.py")
    report, stdout = grade(tmp_path, source)
    assert report["passed"] == 0
    assert stdout.count("GRADE n0nce") == 1


def test_a_forked_process_that_keeps_the_output_open_does_not_hang_the_harness(tmp_path):
    source = "import os, time\nos.fork()\ntime.sleep(60)\n" + sample("correct.py")
    report, _ = grade(tmp_path, source)
    assert report["timeouts"] == 6


def test_report_says_whether_the_submission_ran_as_another_user(tmp_path):
    report, _ = grade(tmp_path, sample("correct.py"))
    assert report["isolated"] is False  # pytest does not run as root; in the sandbox it does
