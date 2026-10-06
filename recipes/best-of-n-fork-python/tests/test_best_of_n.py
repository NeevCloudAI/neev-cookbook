import asyncio
import contextlib
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import best_of_n
from tests.fakes import FIXED, FakeModel, FakeSandboxes, FakeSession, connector, text, tool_call

FIXTURE = best_of_n.load_fixture(pathlib.Path(best_of_n.__file__).parent / "fixture")
TEMPS = [t for t, _ in best_of_n.STRATEGIES]
FIX = tool_call("fs_write", {"path": "project/scheduler/intervals.py",
                             "content": FIXTURE["scheduler/intervals.py"].replace("merged[-1][1] = end", f"merged[-1][1] = {FIXED}")})
FINISH = tool_call("finish", {"summary": "keep the larger end"}, "c2")
STALL = [tool_call("fs_list", {})] * 30


def client(**kw):
    sandboxes = FakeSandboxes(**kw)
    return type("Client", (), {"sandboxes": sandboxes})(), sandboxes


def run(model, fx=FIXTURE, lines=None, **kw):
    c, sandboxes = client(**kw)
    code = best_of_n.run(c, model, "m", connector(sandboxes), fx, log=(lines.append if lines is not None else lambda *_: None),
                         sleep=lambda s: None, deadline_s=5)
    return code, sandboxes


def test_fixture_ships_a_package_and_its_tests():
    assert {"scheduler/intervals.py", "scheduler/slots.py", "tests/test_intervals.py", "tests/test_slots.py"} <= set(FIXTURE)
    assert "merged[-1][1] = end" in FIXTURE["scheduler/intervals.py"]


def test_missing_env_names_every_missing_variable():
    assert best_of_n.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in best_of_n.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert best_of_n.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_test_command_runs_only_the_shipped_test_modules_in_isolated_mode():
    argv = best_of_n.test_command(FIXTURE)
    assert argv[:3] == ["python3", "-I", "-c"]
    assert "'tests.test_intervals', 'tests.test_slots'" in argv[3]
    assert "sys.path.append('.')" in argv[3]  # after the stdlib, so the project cannot shadow unittest


@pytest.mark.parametrize("exit_code, output, passed", [
    (0, "Ran 12 tests in 0.1s\n\nOK\n", True),
    (1, "Ran 12 tests in 0.1s\n\nFAILED (failures=3)\n", False),
    (0, "Ran 3 tests in 0.1s\n\nOK\n", False),       # tests went missing
    (0, "", False),                                  # exited 0 without running anything
    (0, "Ran 12 tests in 0.1s\n\nOK (skipped=2)\n", False),
])
def test_passed_needs_exit_0_every_test_run_and_none_skipped(exit_code, output, passed):
    assert best_of_n.passed(exit_code, output, 12) is passed


def test_verify_puts_the_original_tests_back_before_running_them():
    session = FakeSession({f"project/{p}": c for p, c in FIXTURE.items()})
    session.files["project/tests/test_slots.py"] = "SKIP_ALL"
    ok, _ = asyncio.run(best_of_n.verify(session, FIXTURE, 12))
    assert not ok and session.files["project/tests/test_slots.py"] == FIXTURE["tests/test_slots.py"]
    assert session.calls[-1][1]["args"][:2] == ["-I", "-c"]


def test_verify_reports_a_refused_test_run_as_a_failure():
    session = FakeSession({f"project/{p}": c for p, c in FIXTURE.items()})

    async def refused(name, arguments=None):
        if name == "exec":
            return SimpleNamespace(is_error=True, structured_content=None,
                                   content=[SimpleNamespace(text="the sandbox refused this call: deadline_exceeded")])
        return await FakeSession.call_tool(session, name, arguments)

    session.call_tool = refused
    assert asyncio.run(best_of_n.verify(session, FIXTURE, 12)) == (False, "the sandbox refused this call: deadline_exceeded")


def test_an_agent_failure_inside_the_mcp_task_group_is_reported_as_failed():
    _, sandboxes = client()
    sandboxes.create({"name": "fork-1"})

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps errors raised inside it.
        try:
            yield FakeSession(dict(sandboxes.made[0].files_by_path))
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    a = asyncio.run(best_of_n.attempt(1, "fork-1", TEMPS[0], "hint", grouping_connect, FakeModel({TEMPS[0]: STALL}),
                                      "m", FIXTURE, 12, lambda *_: None, max_steps=2))
    assert (a.outcome, a.detail) == ("failed", "step limit of 2 reached")


def test_diff_shows_only_the_changed_package_files():
    session = FakeSession({f"project/{p}": c for p, c in FIXTURE.items()})
    session.files["project/scheduler/intervals.py"] = FIXTURE["scheduler/intervals.py"].replace(
        "merged[-1][1] = end", f"merged[-1][1] = {FIXED}")
    diff = asyncio.run(best_of_n.diff(session, FIXTURE))
    assert "--- a/scheduler/intervals.py" in diff and f"+            merged[-1][1] = {FIXED}" in diff
    assert "slots.py" not in diff


def test_happy_path_uploads_once_forks_three_times_picks_the_winner_and_deletes_everything():
    lines = []
    model = FakeModel({TEMPS[0]: STALL, TEMPS[1]: [FIX, FINISH], TEMPS[2]: STALL}, delay_by_temperature={TEMPS[0]: 0.05, TEMPS[2]: 0.05})
    code, sandboxes = run(model, lines=lines)
    base, forks = sandboxes.made[0], sandboxes.made[1:]
    assert code == 0
    assert sandboxes.created == [{"name": base.name, "egress": {"mode": "deny_all"}}]
    assert base.name.startswith("best-of-n-") and [f.name for f in forks] == [f"{base.name}-{i}" for i in (1, 2, 3)]
    assert all(f.ready for f in forks)
    assert sandboxes.execs == [(base.name, best_of_n.test_command(FIXTURE), "project")]  # confirmed failing once, on the base
    assert all(sb.deleted for sb in sandboxes.made)
    out = "\n".join(lines)
    assert f"Winner: {forks[1].name}" in out and "+            merged[-1][1] = max" in out
    assert "cancelled" in out


def test_losers_are_cancelled_as_soon_as_one_fork_passes():
    model = FakeModel({TEMPS[0]: [text("thinking")], TEMPS[1]: [FIX, FINISH], TEMPS[2]: [text("thinking")]},
                      delay_by_temperature={TEMPS[0]: 30, TEMPS[2]: 30})
    t = time.monotonic()
    code, sandboxes = run(model)
    assert code == 0 and time.monotonic() - t < 5
    assert all(sb.deleted for sb in sandboxes.made)


def test_forks_retry_while_the_base_is_still_being_snapshotted():
    code, sandboxes = run(FakeModel({t: [FIX, FINISH] for t in TEMPS}), conflicts=2)
    assert code == 0 and len(sandboxes.made) == 4


def test_no_fork_passing_exits_1_and_deletes_everything():
    lines = []
    code, sandboxes = run(FakeModel({t: STALL for t in TEMPS}), lines=lines)
    assert code == 1 and all(sb.deleted for sb in sandboxes.made)
    assert sum("temperature" in l and "failed after" in l for l in lines) == 3 and any("No fork" in l for l in lines)


def test_one_crashing_agent_does_not_stop_the_others():
    lines = []
    model = FakeModel({TEMPS[0]: [RuntimeError("Error code: 500")], TEMPS[1]: [FIX, FINISH], TEMPS[2]: STALL},
                      delay_by_temperature={TEMPS[1]: 0.05, TEMPS[2]: 0.05})
    code, _ = run(model, lines=lines)
    assert code == 0 and any("error after" in l and "RuntimeError: Error code: 500" in l for l in lines)


def test_a_base_where_the_tests_already_pass_is_refused_before_forking():
    fx = dict(FIXTURE, **{"scheduler/intervals.py": FIXED})
    lines = []
    code, sandboxes = run(FakeModel({}), fx=fx, lines=lines)
    assert code == 1 and len(sandboxes.made) == 1 and sandboxes.made[0].deleted
    assert any("expected the tests to fail" in l for l in lines)


def test_ctrl_c_while_forks_start_deletes_the_base_and_the_forks_made_so_far():
    c, sandboxes = client()
    sandboxes.interrupt_ready = {"never"}
    real_create = sandboxes.create

    def create(params):
        sb = real_create(params)
        sandboxes.interrupt_ready = {f"{sb.name}-2"}
        return sb

    sandboxes.create = create
    code = best_of_n.run(c, FakeModel({}), "m", connector(sandboxes), FIXTURE, log=lambda *_: None, sleep=lambda s: None)
    assert code == 130
    assert len(sandboxes.made) == 4 and all(sb.deleted for sb in sandboxes.made)


def test_ctrl_c_during_the_race_deletes_everything():
    model = FakeModel({TEMPS[0]: [KeyboardInterrupt()], TEMPS[1]: STALL, TEMPS[2]: STALL},
                      delay_by_temperature={TEMPS[1]: 0.05, TEMPS[2]: 0.05})
    code, sandboxes = run(model)
    assert code == 130 and all(sb.deleted for sb in sandboxes.made)


def test_a_failed_delete_still_deletes_the_rest():
    lines = []
    c, sandboxes = client()
    code = None

    def boom():
        raise RuntimeError("HTTP 503")

    real_fork = sandboxes.create

    def create(params):
        sb = real_fork(params)
        sb.delete = boom
        return sb

    sandboxes.create = create
    code = best_of_n.run(c, FakeModel({t: [FIX, FINISH] for t in TEMPS}), "m", connector(sandboxes), FIXTURE,
                         log=lines.append, sleep=lambda s: None)
    assert code == 0 and all(sb.deleted for sb in sandboxes.made[1:])
    assert any("Could not delete" in l and "HTTP 503" in l for l in lines)


def test_runs_started_together_get_different_names():
    names = []
    for _ in range(2):
        _, sandboxes = run(FakeModel({t: [FIX, FINISH] for t in TEMPS}))
        names.append(sandboxes.made[0].name)
    assert names[0] != names[1]


def test_fork_gives_up_after_bounded_retries_and_deletes_the_base():
    lines = []
    code, sandboxes = run(FakeModel({}), lines=lines, conflicts=100)
    assert code == 1 and len(sandboxes.made) == 1 and sandboxes.made[0].deleted
    assert any(l.startswith("Failed: ConflictError") for l in lines)


def test_a_fork_whose_reply_was_lost_is_found_by_name_and_deleted():
    c, sandboxes = client()
    real_create = sandboxes.create

    def create(params):
        sandboxes.lose_reply_for = {f"{params['name']}-2"}
        return real_create(params)

    sandboxes.create = create
    lines = []
    code = best_of_n.run(c, FakeModel({}), "m", connector(sandboxes), FIXTURE, log=lines.append, sleep=lambda s: None)
    assert code == 1 and [s.name[-2:] for s in sandboxes.made] == [sandboxes.made[0].name[-2:], "-1", "-2"]
    assert all(sb.deleted for sb in sandboxes.made)


def test_a_hung_tool_call_cannot_outlast_the_agent_budget():
    _, sandboxes = client()
    sandboxes.create({"name": "fork-1"})

    @contextlib.asynccontextmanager
    async def hanging_connect(name):
        session = FakeSession(dict(sandboxes.made[0].files_by_path))
        session.hang_on = {"exec"}
        yield session

    model = FakeModel({TEMPS[0]: [tool_call("exec", {"program": "python3", "args": ["-m", "unittest"]})]})
    t = time.monotonic()
    a = asyncio.run(best_of_n.attempt(1, "fork-1", TEMPS[0], "hint", hanging_connect, model, "m", FIXTURE, 12,
                                      lambda *_: None, deadline_s=0.3))
    assert (a.outcome, a.detail) == ("failed", "time limit of 0s reached") and time.monotonic() - t < 5


@pytest.mark.parametrize("fixed, ok", [(False, False), (True, True)])
def test_the_real_test_command_fails_on_the_shipped_bug_and_passes_once_fixed(tmp_path, fixed, ok):
    for path, content in FIXTURE.items():
        if fixed and path == "scheduler/intervals.py":
            content = content.replace("merged[-1][1] = end", f"merged[-1][1] = {FIXED}")
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content)
    argv = best_of_n.test_command(FIXTURE)
    done = subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    output = done.stdout + done.stderr
    assert best_of_n.tests_ran(output) == 12
    assert best_of_n.passed(done.returncode, output, 12) is ok
    assert ok or "FAILED (failures=3)" in output


def test_a_fake_unittest_in_the_project_cannot_fake_a_pass(tmp_path):
    for path, content in FIXTURE.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(content)
    (tmp_path / "unittest.py").write_text("import sys\nprint('Ran 12 tests in 0.0s\\n\\nOK', file=sys.stderr)\nsys.exit(0)\n")
    argv = best_of_n.test_command(FIXTURE)
    done = subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert not best_of_n.passed(done.returncode, done.stdout + done.stderr, 12)
