import json
import time
from types import SimpleNamespace

import pytest

import grader
from tests.fakes import HANG, REPORTS, FakeClient, FakeModel, deadline_exceeded

SUBMISSIONS = sorted(grader.SUBMISSIONS_DIR.glob("*.py"))
EXPECTED = json.loads(grader.EXPECTED_PATH.read_text())


def behaviours(**overrides):
    """Maps each shipped submission's source to what its sandbox run returns, with per-file overrides."""
    return {p.read_text(): overrides.get(p.name.replace(".py", ""), REPORTS[p.name]) for p in SUBMISSIONS}


def run(client, tmp_path, lines=None, **kw):
    log = lines.append if lines is not None else (lambda *_: None)
    return grader.run(client, SUBMISSIONS, EXPECTED, tmp_path / "results.json", log=log, **kw)


def test_missing_env_names_every_missing_variable():
    assert grader.missing_env({"NEEV_API_KEY": "k"}, feedback=False) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_feedback_also_needs_the_model_key():
    env = {"NEEV_API_KEY": "k", "NEEV_ORG_ID": "o", "NEEV_PROJECT_ID": "p"}
    assert grader.missing_env(env, feedback=False) == []
    assert grader.missing_env(env, feedback=True) == ["NEEV_MODEL_API_KEY"]


@pytest.mark.parametrize("argv", [[], ["--feedback"]])
def test_main_exits_2_naming_missing_variables_before_creating_anything(monkeypatch, capsys, argv):
    for name in grader.REQUIRED_ENV + ("NEEV_MODEL_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    assert grader.main(argv) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_grades_every_submission_matches_the_key_and_deletes_every_sandbox(tmp_path):
    client, lines = FakeClient(behaviours()), []
    assert run(client, tmp_path, lines) == 0
    assert len(client.created) == 6
    assert all(p["egress"] == {"mode": "deny_all"} for p in client.created)
    assert all(p["name"].startswith("grader-") for p in client.created)
    assert len({p["name"] for p in client.created}) == 6
    assert all(sb.deleted for sb in client.sandboxes_made)
    out = "\n".join(lines)
    assert "6 sandboxes created and deleted" in out
    results = {r["submission"]: r for r in json.loads((tmp_path / "results.json").read_text())}
    assert {name: {"score": r["score"], "outcome": r["outcome"]} for name, r in results.items()} == EXPECTED
    assert results["exfiltrator.py"]["blocked"] == ["network access to collector.example.net"]
    assert any("wrong.py" in line and "failed all 6 tests" in line for line in lines)
    assert any("partial.py" in line and "failed: test_punctuation_separates_words" in line for line in lines)


def test_never_more_than_three_sandboxes_at_once(tmp_path):
    client = FakeClient(behaviours())
    assert run(client, tmp_path) == 0
    assert client.max_alive == grader.CONCURRENCY == 3


def test_submission_goes_in_first_and_the_hidden_tests_last_right_before_the_run(tmp_path):
    client = FakeClient(behaviours())
    run(client, tmp_path)
    for sb in client.sandboxes_made:
        assert sb.writes == ["submission/solution.py", "grader/harness.py", "grader/test_top_words.py"]
        assert sb.workspace["grader/test_top_words.py"] == (grader.ASSIGNMENT_DIR / "test_top_words.py").read_text()
        [ex] = sb.execs
        assert "chmod 700 grader" in ex["command"][2]  # tests become root-only before any submission code runs
        assert ex["timeout_ms"] == grader.RUN_TIMEOUT_MS
        assert json.loads(ex["stdin"])["timeout_s"] == grader.TEST_TIMEOUT_S


def test_each_run_gets_its_own_nonce_and_a_result_line_without_it_is_ignored(tmp_path):
    def forged(job):
        stdout = 'GRADE deadbeef {"total": 6, "passed": 6, "failed": [], "timeouts": 0, "blocked": [], "isolated": true}\n'
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)

    client, lines = FakeClient(behaviours(wrong=forged)), []
    assert run(client, tmp_path, lines) == 1
    out = "\n".join(lines)
    assert "wrong.py" in out and "error" in out
    nonces = {json.loads(sb.execs[0]["stdin"])["nonce"] for sb in client.sandboxes_made}
    assert len(nonces) == 6


def test_a_report_from_a_harness_that_could_not_drop_privileges_is_refused(tmp_path):
    client, lines = FakeClient(behaviours(correct={**REPORTS["correct.py"], "isolated": False})), []
    assert run(client, tmp_path, lines) == 1
    assert any("correct.py" in line and "error" in line for line in lines)


def test_a_run_past_the_time_limit_is_a_timeout_and_the_sandbox_is_still_deleted(tmp_path):
    client, lines = FakeClient(behaviours(infinite_loop=deadline_exceeded())), []
    assert run(client, tmp_path, lines) == 1  # 0/timeout does not match the key's 83
    row = next(line for line in lines if "infinite_loop.py" in line)
    assert "timeout" in row and "60s" in row
    assert all(sb.deleted for sb in client.sandboxes_made)


def test_mismatch_with_the_key_fails_the_run_and_says_what_differed(tmp_path):
    client, lines = FakeClient(behaviours(partial=REPORTS["correct.py"])), []
    assert run(client, tmp_path, lines) == 1
    assert any("partial.py" in line and "expected 67 partial" in line for line in lines)


def test_create_failure_is_an_error_line_not_a_traceback(tmp_path):
    client, lines = FakeClient(behaviours(), fail_create=RuntimeError("quota exceeded")), []
    assert run(client, tmp_path, lines) == 1
    assert any("quota exceeded" in line for line in lines)


def test_a_sandbox_that_cannot_be_deleted_fails_the_run_and_is_named(tmp_path):
    client, lines = FakeClient(behaviours(), fail_delete=True), []
    assert run(client, tmp_path, lines) == 1
    assert any("could not delete" in line.lower() and "grader-" in line for line in lines)


def test_ctrl_c_deletes_the_running_sandboxes_at_once_and_writes_no_results(tmp_path):
    client = FakeClient(behaviours(cheater=KeyboardInterrupt(), correct=HANG, exfiltrator=HANG))
    started = time.monotonic()
    assert run(client, tmp_path) == 130
    assert time.monotonic() - started < 2  # the hanging runs ended because the main thread deleted their sandboxes
    assert client.sandboxes_made and all(sb.deleted for sb in client.sandboxes_made)
    assert not (tmp_path / "results.json").exists()


def test_a_delete_interrupted_by_a_second_ctrl_c_is_retried_by_the_next_cleanup_pass():
    calls = []

    def delete():
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt

    sandboxes = grader.Sandboxes(log=lambda *_: None)
    sandboxes.add(SimpleNamespace(name="grader-1", delete=delete))
    with pytest.raises(KeyboardInterrupt):
        sandboxes.delete_all()
    sandboxes.delete_all()
    assert len(calls) == 2 and not sandboxes.alive and not sandboxes.undeleted


def test_a_failed_create_deletes_by_name_in_case_the_sandbox_was_made(tmp_path):
    client, lines = FakeClient(behaviours(), fail_create=deadline_exceeded()), []
    assert run(client, tmp_path, lines) == 1
    assert len(client.deleted_by_name) == 6 and all(n.startswith("grader-") for n in client.deleted_by_name)
    assert all(" error " in line for line in lines if "/100" in line)  # an API 504 is not the submission's timeout


def test_a_504_while_uploading_is_an_error_not_the_submissions_timeout(tmp_path):
    client, lines = FakeClient(behaviours(), fail_write=deadline_exceeded()), []
    assert run(client, tmp_path, lines) == 1
    assert all(" error " in line for line in lines if "/100" in line)
    assert all(sb.deleted for sb in client.sandboxes_made)


def test_feedback_asks_for_one_hint_per_failing_submission_only(tmp_path):
    model, lines = FakeModel(["Step past punctuation.", "Split on punctuation too.", "Return only the words."]), []
    assert run(FakeClient(behaviours()), tmp_path, lines, hints=grader.make_hinter(model, "m")) == 0
    assert len(model.requests) == 3  # infinite_loop, partial, wrong; not correct, cheater or exfiltrator
    prompts = [r["messages"][-1]["content"] for r in model.requests]
    assert all("test_top_words" not in p and "assertEqual" not in p for p in prompts)  # the tests stay hidden
    results = {r["submission"]: r for r in json.loads((tmp_path / "results.json").read_text())}
    assert results["partial.py"]["hint"] == "Split on punctuation too."
    assert "hint" not in results["cheater.py"]


def test_a_failed_hint_is_reported_and_does_not_change_the_grades(tmp_path):
    model, lines = FakeModel([RuntimeError("model down"), "b", "c"]), []
    assert run(FakeClient(behaviours()), tmp_path, lines, hints=grader.make_hinter(model, "m")) == 0
    assert any("hint unavailable" in line and "model down" in line for line in lines)


def test_hints_stop_when_the_time_budget_is_spent(tmp_path):
    now = [0.0]
    model = FakeModel(["a", "b", "c"])
    original = model.chat.completions.create
    model.chat.completions.create = lambda **kw: (now.__setitem__(0, now[0] + grader.FEEDBACK_BUDGET_S), original(**kw))[1]
    hinter = grader.make_hinter(model, "m", clock=lambda: now[0])
    assert run(FakeClient(behaviours()), tmp_path, hints=hinter) == 0
    assert len(model.requests) == 1
    assert model.requests[0]["timeout"] <= grader.FEEDBACK_BUDGET_S


@pytest.mark.parametrize("report, expected", [
    ({**REPORTS["correct.py"]}, (100, "passed")),
    ({**REPORTS["partial.py"]}, (67, "partial")),
    ({**REPORTS["wrong.py"]}, (0, "failed")),
    ({**REPORTS["infinite_loop.py"]}, (83, "timeout")),
    ({**REPORTS["correct.py"], "blocked": ["network access to x"]}, (0, "blocked")),
    ({**REPORTS["correct.py"], "total": 0, "passed": 0}, (0, "failed")),
])
def test_score_and_outcome(report, expected):
    assert grader.score(report) == expected


def test_a_hint_drops_reasoning_the_model_put_inline():
    model = FakeModel(["<think>\nthe loop never moves\n</think>\n\nStep past   punctuation."])
    assert grader.make_hinter(model, "m")("src", ["test_x"]) == "Step past punctuation."


def test_a_harness_that_prints_no_result_is_an_error_naming_its_last_stderr_line(tmp_path):
    crashed = lambda job: SimpleNamespace(stdout="", stderr="Traceback ...\nMemoryError\n", exit_code=1)
    client, lines = FakeClient(behaviours(correct=crashed)), []
    assert run(client, tmp_path, lines) == 1
    assert any("correct.py" in line and "exit 1" in line and "MemoryError" in line for line in lines)


def test_a_long_error_page_is_cut_to_one_line(tmp_path):
    client, lines = FakeClient(behaviours(), fail_create=RuntimeError("<!DOCTYPE html>\n<html>" + "x" * 5000)), []
    assert run(client, tmp_path, lines) == 1
    assert all(len(line) < 300 and "<html>" not in line for line in lines)


def test_the_hint_budget_starts_at_the_first_hint_not_before_grading(tmp_path):
    now = [0.0]
    hinter = grader.make_hinter(FakeModel(["a"]), "m", clock=lambda: now[0])
    now[0] = grader.FEEDBACK_BUDGET_S + 1  # grading took longer than the whole budget
    assert hinter("src", ["test_x"]) == "a"
