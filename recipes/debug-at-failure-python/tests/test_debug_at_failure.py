import asyncio
import contextlib
import copy
import json
import time
from types import SimpleNamespace

import pytest

import debug_at_failure
from tests.fakes import FakeClient, FakeModel, FakeSession, curl, text, tool_call


def connector(client):
    """Returns connect(name): an MCP session on the named fake sandbox, recording each name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(client.named(name))

    connect.names = names
    return connect


def fork_of(client):
    return next(s for s in client.made if s.name.endswith("-fork"))


def solver(client, **override):
    """A model that reads /state, then reports the bad record it finds in the fork's memory (or `override`)."""
    seen = {}

    def answer():
        p = fork_of(client).pipeline
        record = p.batch[p.bad]
        return {"record_id": record["id"], "field": "amount", "value": record["amount"],
                "cause": "the amount is a string with a thousands separator", **override}

    async def create(**kwargs):
        step = len(seen)
        seen[step] = {"source_deleted": client.made[0].deleted, "messages": copy.deepcopy(kwargs["messages"])}
        msg = curl("/state") if step == 0 else tool_call("finish", answer(), "c2")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), seen=seen)


def fake_time():
    """Returns (sleep, clock) where sleeping advances the clock, so waits finish instantly."""
    now = [0.0]
    return (lambda s: now.__setitem__(0, now[0] + s)), (lambda: now[0])


def run(client, model=None, lines=None, **kw):
    sleep, clock = fake_time()
    log = lines.append if lines is not None else (lambda *_: None)
    return debug_at_failure.run(client, model or solver(client), "m", kw.pop("connect", None) or connector(client),
                                log=log, sleep=sleep, clock=kw.pop("clock", clock), **kw)


def test_missing_env_names_every_missing_variable():
    assert debug_at_failure.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in debug_at_failure.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert debug_at_failure.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_forks_at_the_failure_and_the_agent_finds_the_injected_record():
    client, lines = FakeClient(), []
    model, connect = solver(client), connector(client)
    assert run(client, model, lines, connect=connect) == 0
    source, fork = client.made
    assert client.created[0] == {"name": source.name, "egress": {"mode": "deny_all"}}
    assert source.name.startswith("debug-fail-") and fork.name == f"{source.name}-fork"
    assert source.started == ["python3", "pipeline.py"] and source.workspace["pipeline.py"].startswith('"""A three-stage')
    # the fork is created from the snapshot taken at the failure, with no internet access
    assert client.created[1] == {"name": fork.name, "restore": "snap-1", "egress": {"mode": "deny_all"}}
    assert source.snapshot_name == f"{source.name}-at-failure"
    assert connect.names == [fork.name]
    # the failing sandbox was gone before the agent started: the fork stands on its own
    assert model.seen[0]["source_deleted"] is True
    assert source.deleted and fork.deleted
    out = "\n".join(lines)
    bad = fork.pipeline.batch[fork.pipeline.bad]
    assert f"failed at record {fork.pipeline.bad}" in out
    assert "same pipeline process" in out and bad["id"] in out and "Root cause found" in out


def test_the_agent_is_told_what_failed_but_not_which_record():
    client = FakeClient()
    model = solver(client)
    assert run(client, model) == 0
    p = fork_of(client).pipeline
    prompt = json.dumps(model.seen[0]["messages"])
    assert "stage" in prompt and p.batch[p.bad]["id"] not in prompt


@pytest.mark.parametrize("override,why", [
    ({"record_id": "ord-00000"}, "record"),
    ({"field": "region"}, "field"),
    ({"value": "12.50"}, "value"),
])
def test_a_diagnosis_that_misses_the_injected_fault_fails_the_run(override, why):
    client, lines = FakeClient(), []
    assert run(client, solver(client, **override), lines) == 1
    assert any(l.strip().startswith("FAILED") and why in l for l in lines)
    assert all(sb.deleted for sb in client.made)


def test_a_pipeline_that_finishes_cleanly_has_nothing_to_debug():
    client, lines = FakeClient(pipeline_options={"outcome": "done"}), []
    assert run(client, lines=lines) == 1
    assert len(client.made) == 1 and client.made[0].deleted
    assert any("finished without failing" in l for l in lines)


def test_a_pipeline_that_never_fails_times_out():
    client, lines = FakeClient(pipeline_options={"outcome": "stuck"}), []
    assert run(client, lines=lines) == 1
    assert len(client.made) == 1 and client.made[0].deleted
    assert any("did not fail or finish within" in l for l in lines)


def test_a_failed_snapshot_stops_before_forking():
    client, lines = FakeClient(snapshot_statuses=("Pending", "Failed")), []
    assert run(client, lines=lines) == 1
    assert len(client.made) == 1 and client.made[0].deleted
    assert any("disk full" in l for l in lines)


def test_a_snapshot_that_never_becomes_ready_times_out():
    client, lines = FakeClient(snapshot_statuses=("Running",)), []
    ticks = iter(range(0, 100_000, 20))
    assert run(client, lines=lines, clock=lambda: next(ticks)) == 1
    assert client.made[0].deleted and any("did not become Ready" in l for l in lines)


@pytest.mark.parametrize("change,why", [
    (lambda p: p.state.update(pid=99, reads=0), "process"),  # a restarted pipeline
    (lambda p: p.state.update(status="running", position=None, error=None) or setattr(p, "outcome", "stuck"), "failure"),
    (lambda p: p.state.update(reads=0), "memory"),
    (lambda p: setattr(p, "process_state", "exited"), "process"),  # answering, but not the supervised process
])
def test_a_fork_that_does_not_reproduce_the_failure_stops_before_the_agent(change, why):
    client, lines = FakeClient(restored_pipeline=change), []
    model = FakeModel([])
    assert run(client, model, lines) == 1
    assert model.requests == []
    assert any(l.strip().startswith("FAILED") and why in l for l in lines)
    assert all(sb.deleted for sb in client.made)


def test_an_agent_that_runs_out_of_steps_fails_and_deletes_everything():
    client, lines = FakeClient(), []
    assert run(client, FakeModel([curl("/state")] * 30), lines) == 1
    assert all(sb.deleted for sb in client.made)
    assert any("no diagnosis" in l and "step limit" in l for l in lines)


def test_ctrl_c_during_the_agent_deletes_every_sandbox():
    client = FakeClient()
    assert run(client, FakeModel([KeyboardInterrupt()])) == 130
    assert len(client.made) == 2 and all(sb.deleted for sb in client.made)


def test_ctrl_c_while_waiting_for_the_snapshot_deletes_the_source():
    client = FakeClient(snapshot_statuses=("Running",))
    calls = []

    def sleep(_):
        calls.append(1)
        if len(calls) > 6:  # past the pipeline polls, inside the snapshot wait
            raise KeyboardInterrupt

    assert debug_at_failure.run(client, solver(client), "m", connector(client), log=lambda *_: None, sleep=sleep) == 130
    assert client.made[0].deleted


def test_an_error_in_the_mcp_task_group_is_one_line_with_the_real_cause():
    client, lines = FakeClient(), []
    model = FakeModel([ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])])
    assert run(client, model, lines) == 1
    assert any(l == "Failed: ConnectionError: MCP stream closed" for l in lines)
    assert all(sb.deleted for sb in client.made)


@pytest.mark.parametrize("suffix,made", [("-fork", 2), ("", 1)])
def test_a_sandbox_whose_create_reply_was_lost_is_still_found_and_deleted(suffix, made):
    client, lines = FakeClient(), []
    # names are random, so lose the reply for whichever sandbox ends with the suffix
    client.lose_reply_for = type("Match", (), {"__contains__": lambda self, n: n.endswith("-fork") == (suffix == "-fork")})()
    assert run(client, lines=lines) == 1
    assert len(client.made) == made and all(sb.deleted for sb in client.made)


def test_a_failing_delete_is_one_line_and_keeps_the_exit_code():
    client, lines = FakeClient(), []
    model = solver(client)
    client.fail_delete = type("First", (), {"__contains__": lambda self, n: n.endswith("-fork")})()
    assert run(client, model, lines) == 0
    assert any("Could not delete" in l and "-fork" in l and "network down" in l for l in lines)


def test_runs_started_in_the_same_second_get_different_names_and_batches():
    names, batches = [], []
    for _ in range(2):
        client = FakeClient()
        run(client)
        names.append(client.made[0].name)
        batches.append(client.made[0].stdin)
    assert names[0] != names[1] and batches[0] != batches[1]


def test_a_text_reply_is_not_a_diagnosis():
    client, lines = FakeClient(), []
    assert run(client, FakeModel([text("Probably bad data."), text("Bad data.")]), lines) == 1
    assert any("no diagnosis" in l for l in lines)


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def test_the_agent_deadline_cuts_a_slow_model_call_and_deletes():
    client, lines = FakeClient(), []
    t = time.monotonic()
    assert run(client, SlowModel(), lines, agent_deadline_s=0.2) == 1
    assert time.monotonic() - t < 5 and all(sb.deleted for sb in client.made)
    assert any("no diagnosis" in l and "time limit" in l for l in lines)


def test_the_agent_deadline_covers_a_hung_mcp_handshake():
    client, lines = FakeClient(), []

    @contextlib.asynccontextmanager
    async def hung(name):
        await asyncio.sleep(30)
        yield

    t = time.monotonic()
    assert run(client, lines=lines, connect=hung, agent_deadline_s=0.2) == 1
    assert time.monotonic() - t < 5 and any("time limit" in l for l in lines)


def test_the_agent_deadline_cuts_a_hung_tool_call():
    client, lines = FakeClient(), []

    @contextlib.asynccontextmanager
    async def hanging_exec(name):
        session = FakeSession(client.named(name))

        async def hang(tool, arguments=None):
            await asyncio.sleep(30)

        session.call_tool = hang
        yield session

    t = time.monotonic()
    assert run(client, lines=lines, connect=hanging_exec, agent_deadline_s=0.2) == 1
    assert time.monotonic() - t < 5 and any("time limit" in l for l in lines)


def test_ctrl_c_during_the_cleanup_lookup_is_one_line_and_still_deletes_the_rest():
    client, lines = FakeClient(), []
    client.lose_reply_for = type("Match", (), {"__contains__": lambda self, n: n.endswith("-fork")})()

    def interrupted_list(name=None, limit=None):
        raise KeyboardInterrupt

    client.sandboxes.list = interrupted_list
    assert run(client, lines=lines) == 1
    assert client.made[0].deleted and any("Could not check for a sandbox" in l for l in lines)
