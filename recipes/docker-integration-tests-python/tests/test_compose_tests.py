import itertools

import compose_tests
from tests.fakes import TESTS_FAIL, FakeClient, FakeSandbox, script


def run(sandbox, lines=None, clock=None):
    clock = clock or itertools.count().__next__
    return compose_tests.run(FakeClient(sandbox), log=(lines if lines is not None else []).append, clock=clock, sleep=lambda s: None)


def test_missing_env_names_every_missing_variable():
    assert compose_tests.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_sample_app_is_the_compose_file_service_and_tests():
    assert list(compose_tests.project_files(compose_tests.PROJECT_DIR)) == ["api/server.ts", "compose.yaml", "tests/api.test.ts"]


def test_line_buffer_joins_chunks_split_mid_line():
    buffer = compose_tests.LineBuffer()
    assert buffer.feed("a\nb") == ["a"]
    assert buffer.feed("c\nd\n") == ["bc", "d"]
    assert buffer.feed("tail") == [] and buffer.flush() == ["tail"]


def test_compose_event_keeps_one_line_per_service_and_drops_progress():
    assert compose_tests.compose_event(" db Pulled") == "db image pulled"
    assert compose_tests.compose_event(" Container sample-app-db-1  Healthy") == "db healthy"
    assert compose_tests.compose_event(" f3b07e8a357c Extracting [==>   ]  43.45MB/112.4MB") is None
    assert compose_tests.compose_event(" Container sample-app-db-1  Started") is None


def test_count_results_reads_the_bun_summary_or_none():
    assert compose_tests.count_results(["(pass) a", "", " 4 pass", " 1 fail", "Ran 5 tests"]) == (4, 1)
    assert compose_tests.count_results(["error: connection refused"]) is None


def test_happy_path_uploads_runs_up_tests_down_and_deletes():
    sb, lines = FakeSandbox(script()), []
    client = FakeClient(sb)
    code = compose_tests.run(client, log=lines.append, clock=itertools.count().__next__, sleep=lambda s: None)
    assert code == 0
    assert client.created[0]["sandbox_template_id"] == "sb-ubuntu-26-04-docker"
    assert client.created[0]["name"].startswith("compose-tests-")
    assert set(sb.files_written) == {f"/workspace/sample-app/{p}" for p in compose_tests.project_files(compose_tests.PROJECT_DIR)}
    assert sb.processes.started == [(c, "/workspace/sample-app") for c in
                                    (compose_tests.COMPOSE_UP, compose_tests.RUN_TESTS, compose_tests.COMPOSE_DOWN)]
    events = [l.split(" (")[0].strip() for l in lines if l.startswith("   | ") and "(pass)" not in l]
    assert events == ["| cache image pulled", "| api image pulled", "| db image pulled", "| db healthy", "| cache healthy", "| api healthy"]
    assert "   Ready in 1s: Docker 29.1.3, Compose 2.40.3" in lines
    assert "   | (pass) health check answers [3.93ms]" in lines
    assert any(l.startswith("   2 passed, 0 failed") for l in lines)
    assert sb.deleted


def test_failing_tests_exit_1_but_still_tear_down_and_delete():
    sb, lines = FakeSandbox(script(tests=(TESTS_FAIL, 1))), []
    assert run(sb, lines) == 1
    assert any(l.startswith("   1 passed, 1 failed") for l in lines)
    assert "   > (fail) creating a note [8ms]" in lines
    assert sb.processes.started[-1][0] == compose_tests.COMPOSE_DOWN
    assert sb.deleted


def test_a_daemon_that_never_answers_fails_clearly_and_deletes():
    sb, lines = FakeSandbox(script(), versions=""), []
    assert run(sb, lines) == 1
    assert any("Docker isn't answering in the sandbox" in l for l in lines)
    assert sb.processes.started == []
    assert sb.deleted


def test_compose_up_failure_skips_the_tests_and_shows_why():
    sb, lines = FakeSandbox(script(up=(["Error response from daemon: pull access denied\n"], 1))), []
    assert run(sb, lines) == 1
    assert [c for c, _ in sb.processes.started] == [compose_tests.COMPOSE_UP]
    assert "   > Error response from daemon: pull access denied" in lines
    assert sb.deleted


def test_a_step_past_its_time_limit_is_killed():
    sb, lines = FakeSandbox(script(up=([], "hang"))), []
    assert run(sb, lines, clock=itertools.count(step=10).__next__) == 1
    assert sb.processes.killed == [compose_tests.COMPOSE_UP]
    assert any("timed out after 300s" in l for l in lines)
    assert sb.deleted


def test_ctrl_c_still_deletes_the_sandbox():
    sb = FakeSandbox(script())

    def interrupt(*_args, **_kw):
        raise KeyboardInterrupt

    sb.processes.logs = interrupt
    assert run(sb) == 130
    assert sb.deleted
