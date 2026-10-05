import contextlib
from types import SimpleNamespace

import pytest

import undo_mistake
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, shell, text, tool_call


def connector(sandbox):
    """Returns a connect(sandbox_name) factory yielding an MCP session on the fake sandbox, recording the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(sandbox)

    connect.names = names
    return connect


def destructive_model():
    return FakeModel([shell("du -sh *"), shell("rm -rf data", "c2"), tool_call("finish", {"summary": "removed data/"}, "c3")])


def careful_model():
    return FakeModel([tool_call("fs_list", {}), text("Everything here looks needed; I left it alone.")])


def fake_time():
    """Returns (sleep, clock) where sleeping advances the clock, so waits finish instantly."""
    now = [0.0]
    return (lambda s: now.__setitem__(0, now[0] + s)), (lambda: now[0])


def run(sandbox, model, lines=None, **kw):
    log = lines.append if lines is not None else (lambda *_: None)
    client = kw.pop("client", None) or FakeClient(sandbox)
    sleep, clock = fake_time()
    return undo_mistake.run(client, model, "m", kw.pop("connect", None) or connector(sandbox),
                            log=log, fetch=sandbox.stats, sleep=sleep, clock=kw.pop("clock", clock), **kw)


def test_missing_env_names_every_missing_variable():
    assert undo_mistake.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in undo_mistake.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert undo_mistake.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_agent_deletes_the_data_and_rollback_brings_everything_back():
    sb, lines = FakeSandbox(), []
    client, connect = FakeClient(sb), connector(sb)
    assert run(sb, destructive_model(), lines, client=client, connect=connect) == 0
    assert client.created[0]["egress"] == {"mode": "deny_all"}
    assert client.created[0]["name"].startswith("undo-mistake-")
    assert connect.names == [sb.name]
    assert sb.workspace["server.py"].startswith('"""A tiny shop API')
    assert sb.started == ["python3", "server.py"]
    assert sb.rollbacks == ["snap-1"]
    assert sb.deleted
    out = "\n".join(lines)
    assert "The agent did the damage itself" in out
    assert "scripted mistake" not in out
    assert "500" in out  # the damage is visible before the rollback
    assert "Restore proven" in out
    # the in-memory request count rewinds to the snapshot moment, which a restarted server could not do
    assert sb.server["served"] == 2


def test_a_careful_agent_gets_the_scripted_mistake_so_the_rollback_still_shows():
    sb, lines = FakeSandbox(), []
    assert run(sb, careful_model(), lines) == 0
    out = "\n".join(lines)
    assert "The agent left the data alone" in out
    assert ["sh", "-c", "rm -rf data"] in sb.execs
    assert "Restore proven" in out and sb.deleted


def test_an_agent_that_kills_the_server_is_undone_too():
    sb, lines = FakeSandbox(), []
    model = FakeModel([shell("rm -rf data && pkill -f server.py"), tool_call("finish", {"summary": "done"}, "c2")])
    assert run(sb, model, lines) == 0
    assert any("not answering" in l for l in lines)
    assert sb.server == {"pid": 12, "served": 2}


def test_a_failed_snapshot_stops_before_the_agent_runs_and_deletes():
    sb, lines = FakeSandbox(snapshot_statuses=("Pending", "Failed")), []
    model = destructive_model()
    assert run(sb, model, lines) == 1
    assert len(model.requests) == 0 and sb.deleted
    assert any("Failed" in l and "disk full" in l for l in lines)


def test_a_snapshot_that_never_becomes_ready_times_out():
    sb, lines = FakeSandbox(snapshot_statuses=("Running",)), []
    clock = iter(range(0, 10_000, 30))
    assert run(sb, destructive_model(), lines, clock=lambda: next(clock)) == 1
    assert sb.deleted and any("did not become Ready" in l for l in lines)


def test_a_rollback_that_does_not_restore_fails_the_run():
    sb, lines = FakeSandbox(rollback_restores=False), []
    assert run(sb, destructive_model(), lines) == 1
    assert sb.deleted
    assert any("FAILED" in l for l in lines) and not any("Restore proven" in l for l in lines)


def test_a_restarted_server_is_not_accepted_as_a_restore():
    sb = FakeSandbox()
    original = sb.rollback

    def rollback_with_restart(snapshot_id):
        original(snapshot_id)
        sb.server = {"pid": 12, "served": 0}  # files back, but the process started fresh
        return sb

    sb.rollback = rollback_with_restart
    lines = []
    assert run(sb, destructive_model(), lines) == 1
    assert any("FAILED" in l and "memory" in l for l in lines)


def test_ctrl_c_during_the_agent_deletes_the_sandbox():
    sb = FakeSandbox()

    @contextlib.asynccontextmanager
    async def interrupted(name):
        raise KeyboardInterrupt
        yield

    assert run(sb, destructive_model(), connect=interrupted) == 130
    assert sb.deleted


def test_unexpected_error_is_one_line_with_the_real_cause_and_deletes():
    sb, lines = FakeSandbox(), []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    assert run(sb, model, lines) == 1
    assert sb.deleted and any("ConnectionError: MCP stream closed" in l for l in lines)


def test_seed_failure_stops_and_deletes():
    sb, lines = FakeSandbox(), []
    sb.exec = lambda command, args=None, **kw: SimpleNamespace(stdout="", stderr="no python3", exit_code=127)
    assert run(sb, destructive_model(), lines) == 1
    assert sb.deleted and any("no python3" in l for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names():
    names = []
    for _ in range(2):
        sb = FakeSandbox()
        client = FakeClient(sb)
        run(sb, careful_model(), client=client)
        names.append(client.created[0]["name"])
    assert names[0] != names[1]


@pytest.mark.parametrize("status,body,ok", [
    (200, {"pid": 12, "served": 3, "customers": 50, "orders": 120}, True),
    (500, {"pid": 12, "served": 3, "error": "gone"}, False),
    (None, {"error": "unreachable"}, False),
    (200, {"pid": 12, "served": 3, "customers": 50, "orders": 0}, False),
])
def test_data_intact_needs_a_200_with_the_original_counts(status, body, ok):
    baseline = {"pid": 12, "served": 2, "customers": 50, "orders": 120}
    assert undo_mistake.data_intact(status, body, baseline) is ok


def test_gateway_errors_while_the_server_comes_up_are_retried_without_counting_requests():
    sb, lines, seen = FakeSandbox(), [], set()

    def flaky(url):
        # the first answer after the start and after the rollback comes from the gateway, not the server
        moment = len(sb.rollbacks)
        if moment not in seen:
            seen.add(moment)
            return 502, {"error": "bad gateway"}
        return sb.stats(url)

    assert undo_mistake.run(FakeClient(sb), destructive_model(), "m", connector(sb), log=lines.append,
                            fetch=flaky, sleep=lambda s: None) == 0
    assert seen == {0, 1} and "Restore proven" in "\n".join(lines)


def test_a_failing_delete_is_one_line_and_keeps_the_exit_code():
    sb, lines = FakeSandbox(), []

    def broken_delete():
        raise ConnectionError("network down")

    sb.delete = broken_delete
    assert run(sb, destructive_model(), lines) == 0
    assert any("Could not delete sandbox undo-mistake-1" in l and "network down" in l for l in lines)


def test_a_scripted_mistake_that_changes_nothing_fails_the_run():
    sb, lines = FakeSandbox(), []
    sb.shell = lambda script: SimpleNamespace(stdout="", stderr="", exit_code=0)
    assert run(sb, careful_model(), lines) == 1
    assert sb.rollbacks == [] and sb.deleted
    assert any("still has its data" in l for l in lines)


def test_a_gateway_blip_after_the_agent_is_not_mistaken_for_damage():
    sb, lines, calls = FakeSandbox(), [], []

    def blip(url):
        # the first check after a careful agent hits a transient gateway error
        calls.append(url)
        if len(calls) == 2:
            return 502, {"error": "bad gateway"}
        return sb.stats(url)

    assert undo_mistake.run(FakeClient(sb), careful_model(), "m", connector(sb), log=lines.append,
                            fetch=blip, sleep=lambda s: None) == 0
    out = "\n".join(lines)
    assert "The agent left the data alone" in out and "did the damage itself" not in out
