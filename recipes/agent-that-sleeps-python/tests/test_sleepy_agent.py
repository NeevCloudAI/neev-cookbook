import contextlib
from types import SimpleNamespace

import pytest

import sleepy_agent
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, note, text, tool_call


def connector(sandbox):
    """Returns a connect(sandbox_name) factory yielding an MCP session on the fake sandbox, recording the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(sandbox)

    connect.names = names
    return connect


def two_bursts():
    """A model that records two notes in each burst."""
    return FakeModel([note("bug: login fails"), note("question: invoices", "c2"), tool_call("finish", {"summary": "2"}, "c3"),
                      note("feature: export", "c4"), note("bug: slow search", "c5"), tool_call("finish", {"summary": "2"}, "c6")])


def fake_time():
    """Returns (sleep, clock, naps) where sleeping advances the clock and is recorded, so waits finish instantly."""
    now, naps = [0.0], []

    def sleep(s):
        naps.append(s)
        now[0] += s

    return sleep, (lambda: now[0]), naps


def run(sandbox, model, lines=None, **kw):
    log = lines.append if lines is not None else (lambda *_: None)
    client = kw.pop("client", None) or FakeClient(sandbox)
    sleep, clock, _ = fake_time()
    return sleepy_agent.run(client, model, "m", kw.pop("connect", None) or connector(sandbox), log=log,
                            sleep=kw.pop("sleep", sleep), clock=kw.pop("clock", clock), **kw)


def test_missing_env_names_every_missing_variable():
    assert sleepy_agent.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in sleepy_agent.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert sleepy_agent.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_sleeps_wakes_and_the_same_desk_carries_on():
    sb, lines = FakeSandbox(), []
    client, connect = FakeClient(sb), connector(sb)
    sleep, clock, naps = fake_time()
    assert run(sb, two_bursts(), lines, client=client, connect=connect, sleep=sleep, clock=clock, nap_s=45) == 0
    params = client.created[0]
    assert params["name"].startswith("sleepy-agent-") and params["egress"] == {"mode": "deny_all"}
    assert params["lifecycle"] == {"idle_timeout_seconds": sleepy_agent.IDLE_TIMEOUT_S, "on_idle": "pause"}
    assert connect.names == [sb.name, sb.name]
    assert sb.started == ["python3", "desk.py"] and sb.workspace["desk.py"].startswith('"""The agent\'s desk')
    assert sb.pauses == 1 and sb.resumes == 1 and 45 in naps
    assert sb.desk["notes"] == ["bug: login fails", "question: invoices", "feature: export", "bug: slow search"]
    assert sb.deleted
    out = "\n".join(lines)
    assert "phase Paused, replicas 0" in out
    assert "Proven:" in out and "FAILED" not in out


def test_both_bursts_share_one_event_loop_so_the_model_client_survives():
    model = two_bursts()
    assert run(FakeSandbox(), model) == 0
    assert len(model.loops) == 1


def test_the_script_never_touches_the_sandbox_while_it_sleeps():
    # FakeSandbox raises on any runtime call while paused, so a clean run proves the nap was hands-off
    sb = FakeSandbox(pause_polls=5)
    assert run(sb, two_bursts()) == 0


def test_a_desk_that_came_back_restarted_is_not_accepted():
    sb, lines = FakeSandbox(resume_restarts=True), []
    assert run(sb, two_bursts(), lines) == 1
    out = "\n".join(lines)
    assert "FAILED" in out and "boot" in out and "Proven:" not in out
    assert sb.deleted


def test_an_agent_that_records_nothing_stops_before_the_pause():
    sb, lines = FakeSandbox(), []
    assert run(sb, FakeModel([text("Nothing to do.")]), lines) == 1
    assert sb.pauses == 0 and sb.deleted
    assert any("recorded no notes" in l for l in lines)


def test_an_agent_that_records_nothing_after_waking_fails_the_run():
    sb, lines = FakeSandbox(), []
    model = FakeModel([note("bug: a"), tool_call("finish", {"summary": "1"}, "c2"), text("Nothing new.")])
    assert run(sb, model, lines) == 1
    assert sb.resumes == 1 and sb.deleted
    assert any("recorded no notes" in l for l in lines)


def test_a_pause_that_never_lands_times_out_and_deletes():
    sb, lines = FakeSandbox(pause_polls=10_000), []
    assert run(sb, two_bursts(), lines) == 1
    assert sb.deleted and any("did not pause" in l for l in lines)


def test_a_sandbox_that_never_wakes_times_out_and_deletes():
    sb, lines = FakeSandbox(wake_failures=10_000), []
    assert run(sb, two_bursts(), lines) == 1
    assert sb.deleted and any("did not wake" in l and "503 sandbox waking" in l for l in lines)


def test_a_desk_that_never_answers_stops_before_the_agent_runs():
    sb, lines = FakeSandbox(), []
    sb.processes.start = lambda program, **kw: SimpleNamespace(id="proc-1", state="exited")
    model = two_bursts()
    assert run(sb, model, lines) == 1
    assert len(model.requests) == 0 and sb.deleted
    assert any("desk did not answer" in l for l in lines)


def test_ctrl_c_during_the_nap_deletes_the_sandbox():
    sb = FakeSandbox()

    def sleep(s):
        if s == 30:
            raise KeyboardInterrupt

    assert run(sb, two_bursts(), sleep=sleep, nap_s=30) == 130
    assert sb.pauses == 1 and sb.deleted


def test_ctrl_c_during_the_agent_deletes_the_sandbox():
    sb = FakeSandbox()

    @contextlib.asynccontextmanager
    async def interrupted(name):
        raise KeyboardInterrupt
        yield

    assert run(sb, two_bursts(), connect=interrupted) == 130
    assert sb.deleted


def test_unexpected_error_is_one_line_with_the_real_cause_and_deletes():
    sb, lines = FakeSandbox(), []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    assert run(sb, model, lines) == 1
    assert sb.deleted and any("ConnectionError: MCP stream closed" in l for l in lines)


def test_a_failing_delete_is_one_line_and_keeps_the_exit_code():
    sb, lines = FakeSandbox(), []

    def broken_delete():
        raise ConnectionError("network down")

    sb.delete = broken_delete
    assert run(sb, two_bursts(), lines) == 0
    assert any("Could not delete sandbox sleepy-agent-1" in l and "network down" in l for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names():
    names = []
    for _ in range(2):
        sb = FakeSandbox()
        client = FakeClient(sb)
        run(sb, two_bursts(), client=client)
        names.append(client.created[0]["name"])
    assert names[0] != names[1]


@pytest.mark.parametrize("after,ok", [
    ({"pid": 12, "boot_id": "b1", "ticks": 9, "notes": ["a", "b"]}, True),
    ({"pid": 13, "boot_id": "b1", "ticks": 9, "notes": ["a", "b"]}, False),
    ({"pid": 12, "boot_id": "b2", "ticks": 9, "notes": ["a", "b"]}, False),
    ({"pid": 12, "boot_id": "b1", "ticks": 2, "notes": ["a", "b"]}, False),
    ({"pid": 12, "boot_id": "b1", "ticks": 9, "notes": ["a"]}, False),
])
def test_prove_same_process_needs_pid_boot_id_ticks_and_notes(after, ok):
    before = {"pid": 12, "boot_id": "b1", "ticks": 5, "notes": ["a", "b"]}
    assert sleepy_agent.prove_same_process(before, after, "running", 30, lambda *_: None) is ok


def test_main_rejects_a_negative_nap(monkeypatch):
    with pytest.raises(SystemExit):
        sleepy_agent.main(["--nap", "-1"])
