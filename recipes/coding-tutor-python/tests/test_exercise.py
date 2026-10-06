import shutil
import subprocess
import sys

import pytest

import coding_tutor


def check(tmp_path, app_source):
    """Runs the exercise's check.py against the given app.py and returns (exit code, output)."""
    shutil.copy(coding_tutor.EXERCISE_DIR / "check.py", tmp_path / "check.py")
    (tmp_path / "app.py").write_text(app_source)
    done = subprocess.run([sys.executable, "check.py"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    return done.returncode, done.stdout


def starter():
    return (coding_tutor.EXERCISE_DIR / "app.py").read_text()


@pytest.mark.parametrize("source, failing", [
    (starter(), 6),
    (coding_tutor.student_code(), 4),  # passes the easy cases, so there is something to hint at
    (starter().replace("raise NotImplementedError", "return len(text.split())"), 0),
])
def test_check_py_tells_the_starter_the_students_attempt_and_a_correct_answer_apart(tmp_path, source, failing):
    code, out = check(tmp_path, source)
    assert out.count("FAIL") == failing
    assert code == (1 if failing else 0)


def test_the_student_edit_only_changes_count_words():
    changed = [line for line in coding_tutor.student_code().splitlines() if line not in starter().splitlines()]
    assert changed == ['    return len(text.split(" "))']
