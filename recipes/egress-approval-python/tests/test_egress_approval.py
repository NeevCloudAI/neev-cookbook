import contextlib
from types import SimpleNamespace

import pytest

import egress_approval
from egress_approval import EgressGate, mask_id, normalize_host
from tests.fakes import FakeClient, FakeModel, FakeSandbox, FakeSession, audit_record, connector, tool_call

GITHUB, PYPI = "api.github.com", "pypi.org"


def quiet(**kw):
    return {"log": kw.get("log", lambda *_: None), "sleep": lambda s: None}


def gate(sandbox, approve=(GITHUB,), auto_deny=True, ask=None, **kw):
    return EgressGate(sandbox, set(approve), auto_deny, ask or (lambda prompt: pytest.fail("asked")), **quiet(**kw))


def demo_model():
    """The model asks for GitHub (approved) and PyPI (denied), uses GitHub, then finishes."""
    return FakeModel([
        tool_call("request_egress", {"host": GITHUB, "reason": "read the latest release"}),
        tool_call("exec", {"program": "curl", "args": ["--max-time", "15", "https://api.github.com/repos/x/y"]}, "c2"),
        tool_call("request_egress", {"host": PYPI, "reason": "compare the PyPI version"}, "c3"),
        tool_call("finish", {"summary": "latest tag v1.2.3; PyPI was not approved"}, "c4")])


def go(sandbox, model=None, approve=(GITHUB,), auto_deny=True, ask=None, **kw):
    """Runs the recipe against fakes; returns (exit code, client)."""
    client = FakeClient(sandbox)
    code = egress_approval.run("the task", client, model or demo_model(), "m", connector(FakeSession()),
                               set(approve), auto_deny, ask or (lambda p: pytest.fail("asked")), **quiet(**kw))
    return code, client


@pytest.mark.parametrize("raw, host", [
    ("api.github.com", "api.github.com"), (" PyPI.org ", "pypi.org"), ("files.pythonhosted.org", "files.pythonhosted.org")])
def test_normalize_host_accepts_exact_hostnames(raw, host):
    assert normalize_host(raw) == host


@pytest.mark.parametrize("raw", [
    "https://api.github.com", "api.github.com/repos", "*.github.com", "0.0.0.0/0", "::/0", "1.2.3.4",
    "localhost", "", "a..b.com", "-bad.com", "host.com:443", "x" * 300 + ".com"])
def test_normalize_host_rejects_urls_wildcards_ips_and_cidrs(raw):
    assert normalize_host(raw) is None


def test_mask_id_hides_every_character_but_keeps_the_shape():
    assert mask_id("3f9a12bc-1111-2222-3333-444455556666") == "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    assert mask_id(None) == "-"


def test_auto_approved_host_is_added_live_and_proven_blocked_then_reachable():
    sandbox, lines = FakeSandbox(), []
    g = gate(sandbox, log=lines.append)
    answer = g(GITHUB, "read releases")
    assert answer.startswith("approved")
    assert sandbox.updates == [{"egress_add": {"allow": [{"host": GITHUB}]}}]
    assert g.approved == [GITHUB] and g.blocked_before[GITHUB] and g.reachable_after_s[GITHUB] is not None
    assert any("--auto-approve" in l for l in lines)


def test_auto_deny_refuses_without_asking_or_touching_the_allow_list():
    sandbox = FakeSandbox()
    g = gate(sandbox)
    assert g(PYPI, "compare versions").startswith("denied")
    assert sandbox.updates == [] and g.denied == [PYPI]


@pytest.mark.parametrize("reply, approved", [("y", True), ("YES", True), ("n", False), ("", False)])
def test_interactive_answer_decides(reply, approved):
    prompts = []
    g = gate(FakeSandbox(), approve=(), auto_deny=False, ask=lambda p: prompts.append(p) or reply)
    assert g(PYPI, "need it").startswith("approved" if approved else "denied")
    assert len(prompts) == 1 and PYPI in prompts[0]


def test_no_terminal_to_answer_means_denied():
    def eof(_prompt):
        raise EOFError

    assert gate(FakeSandbox(), approve=(), auto_deny=False, ask=eof)(PYPI, "x").startswith("denied")


def test_a_host_is_decided_once():
    sandbox, prompts = FakeSandbox(), []
    g = gate(sandbox, approve=(), auto_deny=False, ask=lambda p: prompts.append(p) or "y")
    g(PYPI, "x")
    assert g("pypi.org", "again").startswith("approved")
    assert len(prompts) == 1 and len(sandbox.updates) == 1


def test_invalid_host_is_an_error_and_never_asked():
    sandbox = FakeSandbox()
    answer = gate(sandbox, approve=("0.0.0.0/0",), auto_deny=False)("0.0.0.0/0", "everything")
    assert answer.startswith("error:") and sandbox.updates == []


def test_requests_are_capped():
    g = gate(FakeSandbox(), approve=())
    for i in range(egress_approval.MAX_HOST_REQUESTS):
        g(f"h{i}.example.com", "x")
    assert "limit" in g("one-more.example.com", "x")
    assert len(g.denied) == egress_approval.MAX_HOST_REQUESTS


def test_reason_is_stripped_of_terminal_control_characters():
    prompts, lines = [], []
    g = gate(FakeSandbox(), approve=(), auto_deny=False, ask=lambda p: prompts.append(p) or "n", log=lines.append)
    g(PYPI, "ok\x1b[2J\x1b]0;pwned\x07 fine")
    shown = "\n".join([*prompts, *lines])
    assert "\x1b" not in shown and "\x07" not in shown


def test_failed_allow_list_update_is_reported_to_the_model_and_not_counted_as_approved():
    g = gate(FakeSandbox(update_error=RuntimeError("HTTP 400")))
    assert g(GITHUB, "x").startswith("error:") and g.approved == [] and g.denied == [GITHUB]


def test_approved_host_that_never_answers_is_not_called_reachable_to_the_model():
    answer = gate(FakeSandbox(dead={GITHUB}))(GITHUB, "x")
    assert answer.startswith("approved") and "not answering" in answer


def test_the_reason_keeps_its_text_when_escape_sequences_are_removed():
    lines = []
    gate(FakeSandbox(), log=lines.append)(GITHUB, "read \x1b[31mreleases\x1b[0m")
    assert any(l.endswith("reason: read releases") for l in lines)


def test_happy_path_exits_0_and_deletes():
    sandbox, lines = FakeSandbox(), []
    code, client = go(sandbox, log=lines.append)
    assert code == 0 and sandbox.deleted
    assert client.created[0]["egress"] == {"mode": "allow_list", "allow": []}
    assert client.created[0]["name"].startswith("egress-approval-")
    out = "\n".join(lines)
    assert "Allow-list now: api.github.com" in out
    assert "key xxxxxxxx-xxxx" in out and "3f9a12bc" not in out


def test_denied_and_control_hosts_are_probed_after_the_run():
    sandbox = FakeSandbox()
    go(sandbox)
    probed = [args[-1] for _, args in sandbox.execs]
    assert f"https://{PYPI}/" in probed and f"https://{egress_approval.CONTROL_HOST}/" in probed


def test_agent_that_does_not_finish_fails():
    model = FakeModel([tool_call("request_egress", {"host": GITHUB, "reason": "x"})] + [tool_call("fs_list", {})] * 20)
    sandbox = FakeSandbox()
    assert go(sandbox, model)[0] == 1 and sandbox.deleted


def test_nothing_approved_fails():
    model = FakeModel([tool_call("finish", {"summary": "did nothing"})])
    assert go(FakeSandbox(), model)[0] == 1


def test_denied_host_that_answers_fails():
    sandbox = FakeSandbox(leaky={PYPI})
    assert go(sandbox)[0] == 1 and sandbox.deleted


def test_approved_host_that_never_answers_fails():
    sandbox = FakeSandbox(dead={GITHUB})
    assert go(sandbox)[0] == 1 and sandbox.deleted


def test_ctrl_c_at_the_approval_prompt_deletes_and_returns_130():
    sandbox = FakeSandbox()

    def interrupt(_prompt):
        raise KeyboardInterrupt

    code, _ = go(sandbox, approve=(), auto_deny=False, ask=interrupt)
    assert code == 130 and sandbox.deleted


def test_agent_error_inside_the_mcp_task_group_is_one_line_and_deletes():
    sandbox, lines = FakeSandbox(), []

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        try:
            raise ConnectionError("MCP stream closed")
            yield  # pragma: no cover
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    code = egress_approval.run("x", FakeClient(sandbox), demo_model(), "m", grouping_connect, {GITHUB}, True,
                               input, **quiet(log=lines.append))
    assert code == 1 and sandbox.deleted
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_create_failure_is_one_line_and_there_is_nothing_to_delete():
    lines = []

    def create(_params):
        raise RuntimeError("Error code: 401 - invalid api key")

    client = SimpleNamespace(sandboxes=SimpleNamespace(create=create))
    code = egress_approval.run("x", client, demo_model(), "m", connector(FakeSession()), {GITHUB}, True, input,
                               **quiet(log=lines.append))
    assert code == 1 and any("401" in l for l in lines)


def test_sandbox_that_never_becomes_ready_is_still_deleted():
    sandbox = FakeSandbox(ready_error=RuntimeError("did not become Ready"))
    assert go(sandbox)[0] == 1 and sandbox.deleted and sandbox.execs == []


def test_audit_poll_waits_for_the_probe_records():
    waited = []
    sandbox = FakeSandbox(audit_pages=[[], [audit_record(tool="fs.read", command=None), audit_record()]])
    records = egress_approval.exec_records(sandbox, "T0", expected=1, sleep=waited.append, log=lambda *_: None)
    assert [r.command for r in records] == ["curl"] and waited == [egress_approval.AUDIT_POLL_INTERVAL_S]


def test_main_exits_2_when_env_missing(monkeypatch, capsys):
    for name in egress_approval.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert egress_approval.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_main_exits_2_on_an_invalid_auto_approve_host(monkeypatch, capsys):
    for name in egress_approval.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert egress_approval.main(["--auto-approve", "*.github.com"]) == 2
    assert "*.github.com" in capsys.readouterr().err


def test_runs_get_different_sandbox_names():
    names = {go(FakeSandbox())[1].created[0]["name"] for _ in range(2)}
    assert len(names) == 2


PIP_HOSTS = {"pypi.org", "files.pythonhosted.org"}


def pip_model(second_host="files.pythonhosted.org"):
    return FakeModel([
        tool_call("request_egress", {"host": "pypi.org", "reason": "pip index"}),
        tool_call("request_egress", {"host": second_host, "reason": "pip downloads"}, "c2"),
        tool_call("finish", {"summary": "installed"}, "c3")])


def test_a_later_host_that_already_answers_is_noted_not_failed():
    sandbox, lines = FakeSandbox(linked=[PIP_HOSTS]), []
    code, _ = go(sandbox, pip_model(), approve=PIP_HOSTS, log=lines.append)
    assert code == 0
    assert sum("already reachable" in l for l in lines) == 1


def test_the_first_approved_host_must_have_been_blocked_before():
    sandbox = FakeSandbox(leaky={"pypi.org"})
    assert go(sandbox, pip_model(), approve=PIP_HOSTS)[0] == 1


def test_a_denied_host_that_answers_anyway_fails():
    sandbox, lines = FakeSandbox(linked=[PIP_HOSTS]), []
    code, _ = go(sandbox, pip_model(), approve={"pypi.org"}, log=lines.append)
    assert code == 1 and any("FAIL files.pythonhosted.org stayed blocked" in l for l in lines)


def test_model_text_cannot_put_escape_sequences_on_the_terminal():
    model = FakeModel([
        tool_call("request_egress", {"host": "api.github.com\x1b[8m", "reason": "x"}),
        tool_call("request_egress", {"host": GITHUB, "reason": "x"}, "c2"),
        tool_call("exec", {"program": "curl\x1b[1A\x1b[2K", "args": ["\x1b[8m", "\x1b]52;c;aGk=\x07"]}, "c3"),
        tool_call("finish", {"summary": "done\x1b[2J"}, "c4")])
    lines = []
    go(FakeSandbox(audit_pages=[[audit_record(command="curl\x1b[8m")]]), model, log=lines.append)
    assert lines and not any(c in l for l in lines for c in ("\x1b", "\x07"))


@pytest.mark.parametrize("existing", [True, False])
def test_ctrl_c_while_creating_deletes_a_sandbox_the_server_already_made(existing):
    sandbox = FakeSandbox()
    client = FakeClient(sandbox, create_error=KeyboardInterrupt(), existing=sandbox if existing else None)
    code = egress_approval.run("x", client, demo_model(), "m", connector(FakeSession()), {GITHUB}, True, input,
                               **quiet())
    assert code == 130 and sandbox.deleted is existing


def test_first_ctrl_c_at_a_real_prompt_interrupts_at_once():
    """Runs the gate under asyncio in a pseudo-terminal, as a person would, and presses Ctrl+C once."""
    import os
    import pty
    import select
    import time as _time

    pid, fd = pty.fork()
    if pid == 0:  # child: the gate's prompt inside asyncio.run, exactly as the recipe calls it
        import asyncio
        from tests.fakes import FakeSandbox as Sandbox

        def ask(prompt):  # a blocking terminal read like input(), which pytest's capture would intercept
            os.write(1, prompt.encode())
            return os.read(0, 100).decode()

        gate_ = egress_approval.EgressGate(Sandbox(), set(), False, ask=ask, log=lambda *_: None)

        async def main():
            gate_("pypi.org", "x")

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            os._exit(42)
        os._exit(0)
    out, deadline = b"", _time.monotonic() + 10
    while b"[y/N]" not in out and _time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            out += os.read(fd, 1024)
    assert b"[y/N]" in out, out
    os.write(fd, b"\x03")
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        _time.sleep(0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("one Ctrl+C at the prompt did not interrupt")
    assert os.waitstatus_to_exitcode(status) == 42
