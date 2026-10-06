import asyncio
import contextlib
import json
import signal

import eval_rollouts
from tasks import TASKS
from tests.fakes import RESTOCK, SOLVE, WRONG, FakeClient, FakeModel, FakeSession, tool_call

MODELS = ["model-a", "model-b"]


def fake_time():
    """Returns (sleep, clock) where sleeping advances the clock, so waits finish instantly."""
    now = [0.0]

    async def sleep(s):
        now[0] += s
        await asyncio.sleep(0)

    return sleep, (lambda: now[0])


def connector(client):
    """Returns a connect(sandbox_name) factory yielding an MCP session on that fake sandbox, recording the names."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(next(sb for sb in client.live if sb.name == name))

    connect.names = names
    return connect


def run(client, model, tmp_path, lines=None, connect=None, models=MODELS):
    sleep, clock = fake_time()
    log = lines.append if lines is not None else (lambda *_: None)
    return eval_rollouts.run(lambda: client, model, connect or connector(client), models,
                             out_path=str(tmp_path / "results.json"), log=log, sleep=sleep, clock=clock)


def results(tmp_path):
    return json.loads((tmp_path / "results.json").read_text())


def test_missing_env_names_every_missing_variable():
    assert eval_rollouts.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in eval_rollouts.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert eval_rollouts.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_every_rollout_starts_from_the_snapshot_is_graded_and_deleted(tmp_path):
    client, lines = FakeClient(), []
    connect = connector(client)
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, lines, connect) == 0
    golden, *rollouts = client.all
    assert client.created[0]["name"].endswith("-golden") and client.created[0]["egress"] == {"mode": "deny_all"}
    assert golden.started == ["python3", "inventory_service.py"]
    assert set(golden.workspace) >= {"inventory_service.py", "SERVICE.md", "app/pricing.py", "app/text.py",
                                     "data/orders.csv", "data/inventory.json"}
    assert len(rollouts) == len(TASKS) * len(MODELS)
    for params in client.created[1:]:
        assert params["restore"] == "snap-1" and params["egress"] == {"mode": "deny_all"}
    assert connect.names == [p["name"] for p in client.created[1:]]  # each agent bound to its own rollout sandbox
    assert len({p["name"] for p in client.created}) == len(client.created)
    assert client.max_live <= 3  # the golden plus two rollouts
    assert all(sb.deleted for sb in client.all) and client.deleted_snapshots == ["snap-1"]
    rows = results(tmp_path)["rollouts"]
    assert [(r["task"], r["model"], r["result"]) for r in rows] == [(t.id, m, "pass") for t in TASKS for m in MODELS]
    assert all(r["steps"] == 2 and r["tokens"] == 200 for r in rows)
    out = "\n".join(lines)
    assert "model-a: 4 of 4 passed, 8 steps" in out and "800 tokens" in out


def test_the_golden_outlives_every_rollout_because_deleting_it_deletes_the_snapshot(tmp_path):
    client = FakeClient()
    assert run(client, FakeModel({}, default=SOLVE), tmp_path) == 0
    assert client.delete_order[-2] == "snap-1" and client.delete_order[-1].endswith("-golden")
    assert len(client.delete_order) == len(client.all) + 1


def test_a_wrong_answer_is_a_fail_result_and_still_exits_0(tmp_path):
    client = FakeClient()
    assert run(client, FakeModel({"model-a": SOLVE, "model-b": WRONG}), tmp_path) == 0
    rows = results(tmp_path)["rollouts"]
    assert {r["result"] for r in rows if r["model"] == "model-b"} == {"fail"}
    assert {r["result"] for r in rows if r["model"] == "model-a"} == {"pass"}


def test_side_effects_of_one_rollout_never_reach_the_next(tmp_path):
    client, lines = FakeClient(), []
    assert run(client, FakeModel({}, default=RESTOCK), tmp_path, lines) == 0
    rollouts = client.all[1:]
    assert all(sb.service["writes"] == 1 for sb in rollouts)  # every agent wrote to its own service...
    assert sum("same as the golden" in l and "0 writes" in l for l in lines) == len(rollouts)  # ...and each started at 0
    assert sum("left behind 1 service write, files changed" in l for l in lines) == len(rollouts)
    assert {r["side_effects"] for r in results(tmp_path)["rollouts"]} == {"1 service write, files changed"}


def test_a_rollout_that_does_not_start_from_the_golden_state_is_an_error_and_exits_1(tmp_path):
    client = FakeClient()
    client.restore_shares_state = True  # a leak: every restore sees the writes of the ones before it
    assert run(client, FakeModel({}, default=RESTOCK), tmp_path, models=["model-a"]) == 1
    rows = results(tmp_path)["rollouts"]
    errors = [r for r in rows if r["result"] == "error"]
    assert errors and all("did not start from the golden state" in r["detail"] for r in errors)
    assert all(sb.deleted for sb in client.all)


def test_a_model_error_marks_that_rollout_an_error_and_the_others_still_run(tmp_path):
    async def broken(messages):
        raise RuntimeError("Error code: 524")

    client = FakeClient()
    assert run(client, FakeModel({"model-a": SOLVE, "model-b": broken}), tmp_path) == 1
    rows = results(tmp_path)["rollouts"]
    assert all(r["result"] == "error" and "524" in r["detail"] for r in rows if r["model"] == "model-b")
    assert all(r["result"] == "pass" for r in rows if r["model"] == "model-a")
    assert all(sb.deleted for sb in client.all)


def test_an_mcp_error_wrapped_in_an_exception_group_reports_the_real_cause(tmp_path):
    @contextlib.asynccontextmanager
    async def broken_connect(name):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])
        yield

    client = FakeClient()
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, connect=broken_connect, models=["model-a"]) == 1
    assert {r["detail"] for r in results(tmp_path)["rollouts"]} == {"ConnectionError: MCP stream closed"}


def test_a_checker_without_a_verdict_is_an_error_not_a_fail(tmp_path):
    client = FakeClient()
    client.grader = lambda sandbox, stdin: type("R", (), {"stdout": "Traceback...", "stderr": "", "exit_code": 0})()
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, models=["model-a"]) == 1
    assert all("no verdict" in r["detail"] for r in results(tmp_path)["rollouts"])


def test_each_checker_is_sent_on_stdin_after_the_agent_is_done(tmp_path):
    client, seen = FakeClient(), []

    def grader(sandbox, stdin):
        seen.append(stdin)
        return type("R", (), {"stdout": json.dumps({"passed": True, "detail": "ok"}), "stderr": "", "exit_code": 0})()

    client.grader = grader
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, models=["model-a"]) == 0
    assert seen == [t.checker for t in TASKS]
    assert not any("check()" in content for sb in client.all for content in sb.workspace.values())


def test_a_failed_snapshot_stops_before_any_rollout_and_deletes_the_golden(tmp_path):
    client, lines = FakeClient(snapshot_statuses=("Pending", "Failed")), []
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, lines) == 1
    assert len(client.all) == 1 and client.all[0].deleted
    assert any(l.startswith("Failed: RuntimeError: snapshot snap-1 is Failed") for l in lines)


def test_ctrl_c_mid_rollout_deletes_every_sandbox_and_the_snapshot_and_exits_130(tmp_path):
    pressed = []

    async def interrupt(messages):
        if not pressed:  # one Ctrl+C: asyncio cancels the run (a second one would force-quit)
            pressed.append(True)
            signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(30)

    client, lines = FakeClient(), []
    assert run(client, FakeModel({}, default=interrupt), tmp_path, lines) == 130
    assert len(client.all) == 3 and not client.live  # golden + two rollouts in flight, all gone
    assert client.deleted_snapshots == ["snap-1"]
    assert client.delete_order[-2] == "snap-1" and client.delete_order[-1].endswith("-golden")  # rollouts first
    assert "Interrupted; cleaning up." in lines
    assert not any("Could not delete" in l for l in lines)


def test_ctrl_c_while_a_create_is_in_flight_still_finds_and_deletes_that_sandbox(tmp_path):
    client = FakeClient()

    async def on_create(sandbox):
        if sandbox.name.endswith("-r1"):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(30)  # cancelled here: the script never receives this sandbox's handle

    client.on_create = on_create
    assert run(client, FakeModel({}, default=SOLVE), tmp_path) == 130
    assert not client.live and all(sb.deleted for sb in client.all)


def test_a_failed_delete_is_reported_by_name_and_exits_1(tmp_path):
    client, lines = FakeClient(), []

    async def on_create(sandbox):
        if sandbox.name.endswith("-r8"):
            client.delete_fails.add(sandbox.name)

    client.on_create = on_create
    code = run(client, FakeModel({}, default=SOLVE), tmp_path, lines)
    assert code == 1
    assert [l for l in lines if "Could not delete" in l] == [
        f"   Could not delete sandbox {client.all[-1].name} (RuntimeError: delete refused); delete it from the console."]
    assert all(sb.deleted for sb in client.all[:-1])


def test_ctrl_c_while_a_rollout_is_being_deleted_retries_that_delete(tmp_path):
    client, lines = FakeClient(), []

    async def on_create(sandbox):
        if sandbox.name.endswith("-r1"):
            client.interrupt_delete.add(sandbox.name)

    client.on_create = on_create
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, lines) == 130
    assert not client.live and all(sb.deleted for sb in client.all)
    assert client.delete_order[-2] == "snap-1" and client.delete_order[-1].endswith("-golden")


def test_a_create_that_errors_after_making_the_sandbox_still_gets_it_deleted(tmp_path):
    from neevai.errors import ConflictError

    client, lines = FakeClient(), []

    async def on_create(sandbox):  # e.g. a retried create: the first attempt made it, the retry got a 409
        if sandbox.name.endswith("-r1"):
            raise ConflictError(409, {"code": "conflict", "message": "name already exists"}, None)

    client.on_create = on_create
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, lines, models=["model-a"]) == 1
    assert not client.live and all(sb.deleted for sb in client.all)


def test_a_create_that_made_nothing_is_reported_as_nothing_to_delete(tmp_path):
    from neevai.errors import BadRequestError

    client, lines = FakeClient(), []
    original = client.sandboxes.create

    async def create(params, **kw):
        if params["name"].endswith("-r1"):
            raise BadRequestError(400, {"code": "invalid_request", "message": "bad"}, None)
        return await original(params, **kw)

    client.sandboxes.create = create
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, lines, models=["model-a"]) == 1
    assert any(l.endswith("-r1 was not found, so there is nothing to delete.") for l in lines)
    assert not client.live


def test_a_sandbox_already_gone_counts_as_deleted(tmp_path):
    client = FakeClient()

    async def on_create(sandbox):
        original = sandbox.delete

        async def delete():  # the first attempt succeeded but its answer was lost, so the retry gets a 404
            await original()
            await original()

        sandbox.delete = delete

    client.on_create = on_create
    assert run(client, FakeModel({}, default=SOLVE), tmp_path, models=["model-a"]) == 0


def test_an_empty_model_list_exits_2_before_creating_anything(monkeypatch, capsys):
    for name in eval_rollouts.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert eval_rollouts.main(["--models", " , "]) == 2
    assert "--models" in capsys.readouterr().err


def test_runs_get_different_sandbox_names(tmp_path):
    client = FakeClient()
    for _ in range(2):
        run(client, FakeModel({}, default=SOLVE), tmp_path, models=["model-a"])
    golden_names = [p["name"] for p in client.created if p["name"].endswith("-golden")]
    assert len(set(golden_names)) == 2 and all(n.startswith("eval-roll-") for n in golden_names)


def test_the_agent_is_refused_lifecycle_tools(tmp_path):
    client = FakeClient()
    model = FakeModel({}, default=[tool_call("delete_sandbox", {}), *SOLVE])
    assert run(client, model, tmp_path, models=["model-a"]) == 0
    assert any(m.get("content") == "error: delete_sandbox is not one of your tools"
               for req in model.requests for m in req["messages"] if m["role"] == "tool")


def test_an_agent_that_stops_the_service_is_still_graded_and_the_damage_is_reported(tmp_path):
    client = FakeClient()
    model = FakeModel({}, default=[tool_call("exec", {"program": "pkill", "args": ["python3"]}), *SOLVE])
    assert run(client, model, tmp_path, models=["model-a"]) == 0
    assert {r["side_effects"] for r in results(tmp_path)["rollouts"]} == {"the service not answering, files changed"}
