import asyncio
import contextlib
from types import SimpleNamespace

import crew
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, connector, text, tool_call

PASSED = {"exit_code": 0, "stdout": "", "stderr": "test_empty ... ok\n\nRan 3 tests in 0.001s\n\nOK\n"}
FAILED = {"exit_code": 1, "stdout": "", "stderr": "Ran 3 tests in 0.001s\n\nFAILED (failures=1)\n"}
UNITTEST = {"program": "python3", "args": ["-m", "unittest", "-v"]}
DISTINCT = {"planner": ("PLANNER_API_KEY", "kp"), "coder": ("CODER_API_KEY", "kc"), "tester": ("TESTER_API_KEY", "kt")}
SHARED = {role: ("NEEV_API_KEY", "ks") for role in ("planner", "coder", "tester")}


def crew_model(passed=True, tester_runs=True):
    """Scripts the three agents in order: plan, implement with tests, run the tests and report."""
    tester = [tool_call("exec", UNITTEST, "t1")] if tester_runs else []
    return FakeModel([
        tool_call("fs_write", {"path": "PLAN.md", "content": "# slugify plan"}, "p1"),
        tool_call("finish", {"summary": "planned"}, "p2"),
        tool_call("fs_read", {"path": "PLAN.md"}, "c1"),
        tool_call("fs_write", {"path": "solution.py", "content": "def slugify(s): ..."}, "c2"),
        tool_call("fs_write", {"path": "test_solution.py", "content": "import unittest"}, "c3"),
        tool_call("finish", {"summary": "implemented"}, "c4"),
        *tester,
        tool_call("finish", {"passed": passed, "report": "3 tests"}, "t2"),
    ])


def run(model, keys=SHARED, client=None, exec_result=PASSED, lines=None, **kw):
    client = client or FakeClient(caller="cred-ks")
    connect = kw.pop("connect", None) or connector(client, {"tester": exec_result})
    log = lines.append if lines is not None else (lambda *_: None)
    code = crew.run("a slugify function", client, model, "m", connect, keys, "ks", log=log, sleep=lambda s: None, **kw)
    return code, client, connect


def test_missing_env_names_every_missing_variable():
    assert crew.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in crew.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert crew.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_agent_keys_default_to_the_sandbox_key_and_take_per_agent_overrides():
    keys = crew.agent_keys({"NEEV_API_KEY": "ks", "TESTER_API_KEY": "kt", "CODER_API_KEY": ""})
    assert keys == {"planner": ("NEEV_API_KEY", "ks"), "coder": ("NEEV_API_KEY", "ks"), "tester": ("TESTER_API_KEY", "kt")}


def test_rejected_keys_names_the_variable_and_only_probes_per_agent_keys():
    probed = []

    def probe(key):
        probed.append(key)
        if key == "kt":
            raise RuntimeError("HTTP 401 invalid token")

    keys = {"planner": ("NEEV_API_KEY", "ks"), "coder": ("CODER_API_KEY", "kc"), "tester": ("TESTER_API_KEY", "kt")}
    assert crew.rejected_keys(keys, probe) == ["TESTER_API_KEY was rejected: RuntimeError: HTTP 401 invalid token"]
    assert probed == ["kc", "kt"]


def test_happy_path_gives_each_agent_its_own_sandbox_and_key_and_hands_files_over():
    model, lines = crew_model(), []
    code, client, connect = run(model, keys=DISTINCT, lines=lines)
    assert code == 0
    names = [p["name"] for p in client.created]
    assert [n.rsplit("-", 1)[0] for n in names] == ["crew-planner", "crew-coder", "crew-tester"]
    assert len({n.rsplit("-", 1)[1] for n in names}) == 1
    assert all(p["egress"] == {"mode": "deny_all"} for p in client.created)
    assert connect.opened == [("kp", names[0]), ("kc", names[1]), ("kt", names[2])]
    coder, tester = client.sandbox("crew-coder"), client.sandbox("crew-tester")
    assert coder.files_store["PLAN.md"] == "# slugify plan"
    assert tester.files_store["solution.py"] == "def slugify(s): ..."
    assert set(tester.files_store) == {"PLAN.md", "solution.py", "test_solution.py"}
    assert any("Ran 3 tests" in l for l in lines)
    assert all(sb.deleted for sb in client.sandboxes_by_name.values())


def test_tester_is_never_offered_fs_write():
    model = crew_model()
    run(model)
    tester_tools = [t["function"]["name"] for t in model.requests[-1]["tools"]]
    assert tester_tools == ["fs_read", "fs_list", "exec", "finish"]


def test_audit_trails_print_oldest_first_and_attribute_each_record_to_its_agent():
    lines = []
    run(crew_model(), keys=DISTINCT, lines=lines)
    out = "\n".join(lines)
    planner_write = next(l for l in lines if "planner" in l and "fs.write" in l and "PLAN.md" in l)
    script_read = next(l for l in lines if "script" in l and "fs.read" in l and "PLAN.md" in l)
    assert lines.index(planner_write) < lines.index(script_read)
    assert any("tester" in l and "exec" in l for l in lines)
    assert "cred-kt" in out and "same credential" not in out


def test_audit_trails_follow_every_page():
    lines = []
    run(crew_model(), keys=DISTINCT, lines=lines)
    coder_records = [l for l in lines if l.lstrip().startswith("12:00") and ("coder" in l or "script" in l)]
    # coder sandbox: script write PLAN.md, coder read+2 writes, script reads 3 files = 7 records over 3 pages
    assert len([l for l in coder_records if "solution" in l or "PLAN.md" in l]) >= 7


def test_one_shared_key_says_every_record_shows_the_same_credential():
    lines = []
    run(crew_model(), keys=SHARED, lines=lines)
    assert any("same credential" in l for l in lines)


def test_tests_that_fail_in_the_tester_sandbox_exit_1_after_printing_audit_trails():
    lines = []
    code, client, _ = run(crew_model(passed=False), exec_result=FAILED, lines=lines)
    assert code == 1
    assert any("FAILED (failures=1)" in l for l in lines)
    assert any("Audit trail" in l for l in lines)
    assert all(sb.deleted for sb in client.sandboxes_by_name.values())


def test_a_tester_that_claims_a_pass_its_last_test_run_contradicts_fails():
    lines = []
    code, _, _ = run(crew_model(passed=True), exec_result=FAILED, lines=lines)
    assert code == 1
    assert any("last test run exited 1" in l for l in lines)


def test_a_tester_that_never_ran_the_tests_fails():
    lines = []
    code, _, _ = run(crew_model(passed=True, tester_runs=False), lines=lines)
    assert code == 1
    assert any("did not run the tests" in l for l in lines)


def test_an_agent_that_gives_up_deletes_every_sandbox():
    lines = []
    model = FakeModel([text("I would plan it like this..."), text("Done.")])
    code, client, _ = run(model, lines=lines)
    assert code == 1
    assert any(l.startswith("The planner did not finish: ") for l in lines)
    assert len(client.created) == 3 and all(sb.deleted for sb in client.sandboxes_by_name.values())


def test_ctrl_c_mid_run_deletes_every_sandbox():
    client = FakeClient(caller="cred-ks")

    @contextlib.asynccontextmanager
    async def interrupted(key, name):
        raise KeyboardInterrupt
        yield  # pragma: no cover

    lines = []
    code, _, _ = run(crew_model(), client=client, connect=interrupted, lines=lines)
    assert code == 130 and "Interrupted." in lines
    assert len(client.created) == 3 and all(sb.deleted for sb in client.sandboxes_by_name.values())


def test_a_failed_create_deletes_the_sandboxes_already_created():
    lines = []
    client = FakeClient(caller="cred-ks", fail_on_create=2)
    code, _, _ = run(crew_model(), client=client, lines=lines)
    assert code == 1 and len(client.created) == 2
    assert all(sb.deleted for sb in client.sandboxes_by_name.values())
    assert any("quota exceeded" in l for l in lines)


def test_a_failed_delete_does_not_stop_the_other_deletes():
    client = FakeClient(caller="cred-ks")
    original = client.sandboxes.create

    def create(params):
        sb = original(params)
        if params["name"].startswith("crew-planner"):
            def fail():
                raise RuntimeError("delete timed out")
            sb.delete = fail
        return sb

    client.sandboxes = SimpleNamespace(create=create)
    lines = []
    run(crew_model(), client=client, lines=lines)
    assert client.sandbox("crew-coder").deleted and client.sandbox("crew-tester").deleted
    assert any("delete timed out" in l for l in lines)


def test_an_error_wrapped_in_an_exception_group_reports_the_real_cause():
    lines = []

    @contextlib.asynccontextmanager
    async def broken(key, name):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])
        yield  # pragma: no cover

    code, _, _ = run(crew_model(), connect=broken, lines=lines)
    assert code == 1
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_agent_failure_inside_the_mcp_task_group_is_still_reported_as_an_agent_failure():
    lines = []
    client = FakeClient(caller="cred-ks")

    @contextlib.asynccontextmanager
    async def grouping(key, name):
        try:
            yield FakeSession(client.sandboxes_by_name[name], caller=f"cred-{key}")
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    model = FakeModel([tool_call("fs_list", {})] * 30)
    code, _, _ = run(model, client=client, connect=grouping, lines=lines)
    assert code == 1
    assert any(l.startswith("The planner did not finish: step limit") for l in lines)


def test_runs_get_different_sandbox_names():
    client = FakeClient(caller="cred-ks")
    run(crew_model(), client=client)
    run(crew_model(), client=client)
    names = [p["name"] for p in client.created]
    assert len(set(names)) == 6 and all(n.startswith("crew-") for n in names)


def test_all_agents_share_one_event_loop_like_the_real_async_model_client():
    model, loops = crew_model(), set()
    create = model.chat.completions.create

    async def loop_bound(**kwargs):
        # AsyncOpenAI binds its connection pool to the first loop; a second asyncio.run breaks it.
        loops.add(id(asyncio.get_running_loop()))
        return await create(**kwargs)

    model.chat.completions.create = loop_bound
    code, _, _ = run(model)
    assert code == 0 and len(loops) == 1


def test_audit_trails_are_reread_until_late_records_stop_arriving():
    sb, sleeps = FakeSandbox("crew-coder-1", "cred-ks"), []
    sb.record("fs.write", "PLAN.md", "cred-ks")
    late = iter([("exec", None), ("fs.read", "solution.py")])
    audit = sb.audit

    def audit_with_lag(**kw):
        page = audit(**kw)
        nxt = next(late, None)
        if nxt:  # a record lands after this read
            sb.record(*nxt, "cred-ks")
        return page

    sb.audit = audit_with_lag
    trails = crew._read_trails({"coder": sb}, sleeps.append)
    assert [r.tool for r in trails["coder"]] == ["fs.write", "exec", "fs.read"]
    assert len(sleeps) == 3


def test_only_a_full_test_run_counts_toward_the_verdict():
    one_test = {"program": "python3", "args": ["-m", "unittest", "test_solution.T.test_easy"]}
    result = SimpleNamespace(finish={"passed": True}, execs=[(UNITTEST, FAILED), (one_test, PASSED)])
    assert crew._verdict(result)[0] is False
    result = SimpleNamespace(finish={"passed": True}, execs=[(one_test, PASSED), (UNITTEST, PASSED)])
    assert crew._verdict(result) == (True, "Ran 3 tests in 0.001s OK")


def test_the_testers_own_failed_verdict_fails_even_when_the_run_exited_0():
    result = SimpleNamespace(finish={"passed": False}, execs=[(UNITTEST, PASSED)])
    assert crew._verdict(result)[0] is False


def test_the_last_full_run_decides():
    assert crew._verdict(SimpleNamespace(finish={"passed": True}, execs=[(UNITTEST, PASSED), (UNITTEST, FAILED)]))[0] is False
    assert crew._verdict(SimpleNamespace(finish={"passed": True}, execs=[(UNITTEST, FAILED), (UNITTEST, PASSED)]))[0] is True


def test_a_test_filter_flag_is_not_a_full_run():
    for args in (["-m", "unittest", "-ktest_easy"], ["-m", "unittest", "-k", "easy"], ["-m", "unittest", "-v", "x"]):
        assert not crew._full_test_run({"program": "python3", "args": args})
    assert crew._full_test_run({"program": "python3", "args": ["-m", "unittest", "-v", "-b"]})


def test_a_tester_that_rewrites_the_tests_it_judges_fails():
    lines = []
    client = FakeClient(caller="cred-ks")
    base = connector(client, {"tester": PASSED})

    @contextlib.asynccontextmanager
    async def tampering(key, name):
        async with base(key, name) as session:
            if name.startswith("crew-tester"):
                session.files["test_solution.py"] = "class T: pass"  # e.g. via exec, which can write files
            yield session

    code, _, _ = run(crew_model(), client=client, connect=tampering, lines=lines)
    assert code == 1
    assert any("test_solution.py changed in the tester's sandbox" in l for l in lines)


def test_a_passing_crew_saves_the_plan_code_and_tests_it_built(tmp_path):
    lines = []
    code, client, _ = run(crew_model(), lines=lines, out=tmp_path)
    assert code == 0
    [folder] = list(tmp_path.iterdir())
    assert folder.name == client.created[0]["name"].rsplit("-", 1)[1]  # the run's id, shared by its sandboxes
    tester = client.sandbox("crew-tester")
    assert {f.name: f.read_text() for f in folder.iterdir()} == {
        name: tester.files_store[name] for name in ("PLAN.md", "solution.py", "test_solution.py")}
    assert any(str(folder) in l for l in lines)


def test_a_failing_crew_saves_nothing(tmp_path):
    code, _, _ = run(crew_model(passed=False), out=tmp_path)
    assert code == 1
    assert list(tmp_path.iterdir()) == []
