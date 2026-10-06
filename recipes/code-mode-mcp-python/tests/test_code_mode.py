import contextlib
from types import SimpleNamespace

import pytest

import code_mode
import fixture
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, finish, text, tool_call

SEED = 4
TRUTH = fixture.make(SEED).top

# A real program, run in the fake sandbox against the real fixture, so the happy path proves the data got there.
PROGRAM = """
import csv, json, pathlib, collections
c = collections.Counter()
for p in pathlib.Path("logs").iterdir():
    rows = json.loads(p.read_text()) if p.suffix == ".json" else list(csv.DictReader(p.open()))
    c.update(r["customer"] for r in rows if r["type"] == "payment" and r["status"] == "failed"
             and "2026-09-28" <= r["ts"][:10] <= "2026-10-04")
print(c.most_common(3))
"""


def connector(root):
    """Returns connect(sandbox_name) yielding a fresh FakeSession on the sandbox directory; records the names."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield FakeSession(root)

    connect.names = names
    return connect


def go(tmp_path, tools_replies, code_replies, lines=None, sandbox=None, **kw):
    sb = sandbox or FakeSandbox(tmp_path)
    client = FakeClient(sb)
    # One model serves both modes in turn: tool mode's replies first, then code mode's.
    model = FakeModel([*tools_replies, *code_replies])
    log = lines.append if lines is not None else (lambda *_: None)
    exit_code = code_mode.run(SEED, client, model, "m", connector(tmp_path), log=log, **kw)
    return exit_code, sb, client, model


def test_missing_env_names_every_missing_variable():
    assert code_mode.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_main_exits_2_when_env_is_missing(monkeypatch, capsys):
    for name in code_mode.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert code_mode.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_uploads_the_logs_runs_both_modes_and_deletes(tmp_path):
    lines = []
    code, sb, client, code_model = go(tmp_path, [tool_call("fs_list", {"path": "logs"}), finish(TRUTH)],
                                      [tool_call("run_python", {"code": PROGRAM}), finish(TRUTH)], lines)
    assert code == 0 and sb.deleted
    assert client.created[0]["egress"] == {"mode": "deny_all"} and client.created[0]["name"].startswith("code-mode-")
    assert len(list((tmp_path / "logs").iterdir())) == fixture.FILES
    assert not (tmp_path / "logs.tar.gz").exists()
    # the program ran against the uploaded files and its output reached the model
    assert str(TRUTH[0][0]) in code_model.requests[-1]["messages"][-1]["content"]
    out = "\n".join(lines)
    assert "model calls" in out and "correct" in out and "Code mode answered correctly" in out


def test_tool_mode_failing_is_reported_but_only_code_mode_decides_the_exit_code(tmp_path):
    lines = []
    code, sb, *_ = go(tmp_path, [tool_call("fs_list", {})] * code_mode.TOOL_MODE_STEPS, [finish(TRUTH)], lines)
    assert code == 0 and sb.deleted
    assert any("step limit" in l for l in lines)


def test_wrong_code_mode_answer_exits_1(tmp_path):
    lines = []
    wrong = [(TRUTH[1][0], TRUTH[0][1]), TRUTH[0], TRUTH[2]]
    code, sb, *_ = go(tmp_path, [finish(TRUTH)], [finish(wrong)], lines)
    assert code == 1 and sb.deleted
    assert any("wrong" in l for l in lines)


def test_code_mode_without_an_answer_exits_1(tmp_path):
    code, sb, *_ = go(tmp_path, [finish(TRUTH)], [text("a"), text("b")])
    assert code == 1 and sb.deleted


def test_an_mcp_failure_in_tool_mode_is_reported_and_code_mode_still_runs(tmp_path):
    lines, calls = [], []
    sb = FakeSandbox(tmp_path)

    @contextlib.asynccontextmanager
    async def connect(name):
        calls.append(name)
        if len(calls) == 1:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])
        yield FakeSession(tmp_path)

    model = FakeModel([finish(TRUTH)])
    assert code_mode.run(SEED, FakeClient(sb), model, "m", connect, log=lines.append) == 0
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_an_mcp_failure_in_code_mode_still_reports_tool_mode_exits_1_and_deletes(tmp_path):
    lines, calls = [], []
    sb = FakeSandbox(tmp_path)

    @contextlib.asynccontextmanager
    async def connect(name):
        calls.append(name)
        if len(calls) == 2:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])
        yield FakeSession(tmp_path)

    model = FakeModel([finish(TRUTH)])
    assert code_mode.run(SEED, FakeClient(sb), model, "m", connect, log=lines.append) == 1
    assert sb.deleted and lines[-2] == "Code mode did not answer correctly."
    assert any("Tool calls answered:" in l and "(answered)" in l for l in lines)
    assert any("Code mode answered:  none (failed: ConnectionError: MCP stream closed)" in l for l in lines)


def test_a_failed_delete_is_reported_with_the_sandbox_name_not_raised(tmp_path):
    sb, lines = FakeSandbox(tmp_path), []

    def broken_delete():
        raise ConnectionError("connection reset")

    sb.delete = broken_delete
    model = FakeModel([finish(TRUTH), finish(TRUTH)])
    assert code_mode.run(SEED, FakeClient(sb), model, "m", connector(tmp_path), log=lines.append) == 0
    assert lines[-1] == ("   Could not delete sandbox code-mode-1 (ConnectionError: connection reset); "
                         "delete it from the console.")


def test_ctrl_c_deletes_the_sandbox_and_exits_130(tmp_path):
    sb = FakeSandbox(tmp_path)

    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    sb.exec = interrupt
    assert code_mode.run(SEED, FakeClient(sb), None, "m", connector(tmp_path), log=lambda *_: None) == 130
    assert sb.deleted


def test_failed_upload_deletes_the_sandbox(tmp_path):
    sb = FakeSandbox(tmp_path)
    sb.exec = lambda *a, **kw: SimpleNamespace(exit_code=2, stdout="", stderr="tar: bad archive")
    lines = []
    assert code_mode.run(SEED, FakeClient(sb), None, "m", connector(tmp_path), log=lines.append) == 1
    assert sb.deleted and any("tar: bad archive" in l for l in lines)


def test_runs_started_together_get_different_sandbox_names(tmp_path):
    client = FakeClient(FakeSandbox(tmp_path))
    for _ in range(2):
        model = FakeModel([finish(TRUTH), finish(TRUTH)])
        code_mode.run(SEED, client, model, "m", connector(tmp_path), log=lambda *_: None)
    assert client.created[0]["name"] != client.created[1]["name"]


@pytest.mark.parametrize("answer, verdict", [(TRUTH, "correct"), (None, "no answer"), (TRUTH[:2], "wrong")])
def test_verdict(answer, verdict):
    assert code_mode.verdict(answer, TRUTH) == verdict


def test_show_code_prints_each_program_the_model_ran(tmp_path):
    lines = []
    replies = [tool_call("run_python", {"code": "print(1)"}), tool_call("run_python", {"code": PROGRAM}), finish(TRUTH)]
    go(tmp_path, [finish(TRUTH)], replies, lines, show_code=True)
    out = "\n".join(lines)
    assert "Program 1 the model ran" in out and "      print(1)" in out and "Program 2" in out
    hidden = []
    go(tmp_path, [finish(TRUTH)], [finish(TRUTH)], hidden)
    assert not any("Program" in l for l in hidden)
