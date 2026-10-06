import json

import pytest

import hosted_agent
from tests.fakes import KEY_ID, FakeAgent, FakeClient, FakeMachine, fake_time

MODEL_KEY = "model-key-for-tests"


def run(agent=None, lines=None, client=None, **kw):
    agent = agent or FakeAgent()
    client = client or FakeClient(agent)
    log = lines.append if lines is not None else (lambda *_: None)
    sleep, clock = fake_time()
    return hosted_agent.run(client, "m", MODEL_KEY, log=log, sleep=sleep, clock=clock, **kw)


def test_missing_env_names_every_missing_variable():
    assert hosted_agent.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in hosted_agent.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert hosted_agent.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_agent_fixes_the_code_and_everything_is_verified():
    agent, lines = FakeAgent(), []
    client = FakeClient(agent)
    assert run(agent, lines, client) == 0
    params = client.created[0]
    assert params["name"].startswith("hosted-agent-") and params["name"] != "hosted-agent-"
    assert params["agent_template"] == "opencode"
    assert params["allow_egress"] == ["inference.ai.neevcloud.com"]
    assert agent.calls == ["wait_until_ready", "pause", "resume", "delete"]
    out = "\n".join(lines)
    assert "1 of 4 tests pass" in out  # the failure is shown before the agent runs
    assert "Task verified" in out
    assert "FAILED" not in out


def test_the_coding_cli_runs_in_the_workspace_with_the_model_key_only_in_its_environment():
    machine = FakeMachine()
    assert run(FakeAgent(machine)) == 0
    started = machine.started[0]
    assert started["argv"][:6] == ["opencode", "run", "--format", "json", "-m", "neevcloud/m"]
    assert started["cwd"] == "/workspace"
    assert started["env"] == {"NEEV_MODEL_API_KEY": MODEL_KEY}
    assert not any(MODEL_KEY in content for content in machine.workspace.values())


def test_uploads_the_project_and_a_config_that_points_the_agent_at_the_chosen_model():
    machine = FakeMachine()
    run(FakeAgent(machine))
    assert machine.workspace["slugify.test.js"] == (hosted_agent.PROJECT_DIR / "slugify.test.js").read_text()
    config = json.loads(machine.workspace["opencode.json"])
    provider = config["provider"]["neevcloud"]
    assert provider["options"]["baseURL"] == "https://inference.ai.neevcloud.com/v1"
    assert provider["options"]["apiKey"] == "{env:NEEV_MODEL_API_KEY}"
    assert list(provider["models"]) == ["m"]
    assert config["agent"]["build"]["steps"] == hosted_agent.MAX_STEPS


def test_shows_each_agent_step_from_its_json_events_and_totals_the_tokens():
    lines = []
    run(lines=lines)
    assert "   | edit slugify.js" in lines  # an event split across two log chunks
    assert "   | bash node --test slugify.test.js" in lines
    assert "   | says: All tests pass." in lines  # first line of the agent's text only
    assert "   | warning: slow model" in lines  # stderr shown as plain text, colours stripped
    assert any("2 model steps, 15000 tokens in, 120 out" in line for line in lines)


@pytest.mark.parametrize("line, expected", [
    ('{"type":"tool_use","part":{"tool":"read","state":{"status":"error","input":{"filePath":"/workspace/x.js"}}}}',
     "read x.js (error)"),
    ('{"type":"tool_use","part":{"tool":"glob","state":{"status":"completed","input":{"pattern":"*.js"}}}}', "glob *.js"),
    ('{"type":"text","part":{"text":"   "}}', None),
    ('{"type":"text","part":{"text":"<think>\\nplanning\\n</think>\\nFixed it."}}', "says: Fixed it."),
    ('{"type":"text","part":{"text":"<think>\\nstill planning"}}', None),
    ('{"type":"step_start","part":{}}', None),
    ('{"type":"error","error":{"name":"APIError","data":{"message":"401 Unauthorized"}}}', "error: APIError 401 Unauthorized"),
    ("Error: model not found", "Error: model not found"),
    ("[1, 2]", "[1, 2]"),
])
def test_describe_event(line, expected):
    assert hosted_agent.describe_event(line) == expected


def test_an_agent_that_leaves_the_tests_failing_exits_1_and_still_deletes():
    agent, lines = FakeAgent(FakeMachine(effect="nothing")), []
    assert run(agent, lines) == 1
    assert agent.deleted
    assert "FAILED" in "\n".join(lines)
    assert "pause" not in agent.calls  # nothing worth keeping, so no pause and resume


def test_an_agent_that_edits_the_tests_fails_even_though_they_pass():
    agent, lines = FakeAgent(FakeMachine(effect="edit-tests")), []
    assert run(agent, lines) == 1
    assert "FAILED the tests are unchanged" in "\n".join(lines)
    assert agent.deleted


def test_the_time_limit_kills_an_agent_that_never_finishes():
    machine = FakeMachine(effect="hang")
    agent, lines = FakeAgent(machine), []
    assert run(agent, lines) == 1
    assert machine.killed == ["proc-1"]
    assert f"time limit of {hosted_agent.AGENT_TIMEOUT_S}s" in "\n".join(lines)
    assert agent.deleted


def test_a_process_started_before_the_pause_must_still_run_after_the_resume():
    machine = FakeMachine()
    run(FakeAgent(machine))
    assert machine.started[1]["argv"] == ["sleep", "3600"]
    assert "proc-2" in machine.running


def test_a_reboot_on_resume_is_caught():
    machine = FakeMachine()
    machine.reboot_on_resume = True
    agent, lines = FakeAgent(machine), []
    assert run(agent, lines) == 1
    assert "FAILED" in "\n".join(lines)
    assert agent.deleted


def test_a_resume_that_never_reaches_ready_is_an_error_and_the_agent_is_deleted():
    agent, lines = FakeAgent(resume_statuses=["Provisioning"] * 1000), []
    assert run(agent, lines) == 1
    assert any(line.startswith("Failed:") and "Ready" in line for line in lines)
    assert agent.deleted


def test_a_pause_that_never_completes_is_an_error_and_the_agent_is_deleted():
    agent, lines = FakeAgent(pause_statuses=["Pausing"] * 1000), []
    assert run(agent, lines) == 1
    assert any(line.startswith("Failed:") and "Paused" in line for line in lines)
    assert agent.deleted


def test_ctrl_c_while_the_agent_works_deletes_it_and_exits_130():
    machine = FakeMachine()
    machine.interrupt_on_logs = True
    agent = FakeAgent(machine)
    assert run(agent) == 130
    assert agent.deleted


def test_a_failed_create_is_one_line_and_deletes_nothing():
    lines = []
    client = FakeClient(create_error=RuntimeError("quota exceeded"))
    assert run(client=client, lines=lines) == 1
    assert lines[-1] == "Failed: RuntimeError: quota exceeded"
    assert client.agent.calls == []


def test_a_long_multi_line_error_is_shown_as_one_short_line():
    lines = []
    client = FakeClient(create_error=RuntimeError("HTTP 504 (<!DOCTYPE html>\n<html>" + "x" * 1000))
    assert run(client=client, lines=lines) == 1
    assert lines[-1].startswith("Failed: RuntimeError: HTTP 504 (<!DOCTYPE html>")
    assert "\n" not in lines[-1] and len(lines[-1]) < 300


def test_a_failed_delete_is_reported_and_fails_the_run():
    agent, lines = FakeAgent(), []
    agent.delete_error = RuntimeError("timeout")
    assert run(agent, lines) == 1
    assert any("could not delete" in line and agent.name in line for line in lines)


def test_the_audit_trail_waits_for_the_last_records_and_collapses_repeats():
    agent, lines = FakeAgent(audit_lag=2), []
    assert run(agent, lines) == 0
    trail = lines[lines.index(next(line for line in lines if line.startswith("5."))):]
    text = "\n".join(trail)
    assert text.count("exec") == 2  # both test runs, including the last one
    assert "process.start  opencode" in text
    assert "process.logs" in text and "x2" in text  # polls collapsed into one row
    assert KEY_ID not in text
    assert "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" in text


@pytest.mark.parametrize("raw, expected", [
    ("1234abcd-5678-4def-9abc-def012345678", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
    (None, "unknown"),
])
def test_mask_key_id(raw, expected):
    assert hosted_agent.mask_key_id(raw) == expected


def test_line_buffer_holds_partial_lines_and_strips_colours():
    buffer = hosted_agent.LineBuffer()
    assert buffer.feed("\x1b[0mhel") == []
    assert buffer.feed("lo\n\nworld\r\n") == ["hello", "world"]
    assert buffer.flush() == []
    buffer.feed("tail")
    assert buffer.flush() == ["tail"]


def test_collapse_merges_consecutive_rows_that_differ_only_in_time():
    rows = [("07:00:01", "a"), ("07:00:02", "a"), ("07:00:03", "b"), ("07:00:04", "a")]
    assert hosted_agent.collapse(rows) == [(("07:00:01", "a"), 2), (("07:00:03", "b"), 1), (("07:00:04", "a"), 1)]
