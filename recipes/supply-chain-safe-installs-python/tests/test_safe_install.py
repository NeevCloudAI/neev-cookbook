import http.server
import json
import socket
import threading
from types import SimpleNamespace

import safe_install
from safe_install import (
    evil_package_files, legit_install_ok, phone_home_blocked, pip_records,
)
from tests.fakes import BLOCKED_MARKER, FakeClient, FakeSandbox, audit_record


def _closed_port() -> int:
    """Returns a loopback port with nothing listening, so a connect to it is refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Answering(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_):
        pass


def _local_collector():
    """Starts a loopback HTTP server that answers every POST 405, standing in for a reachable collector."""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Answering)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def quiet(**kw):
    return {"log": kw.get("log", lambda *_: None), "wait": kw.get("wait", lambda s: None)}


def go(sandbox, package="requests", **kw):
    """Runs the recipe against a fake sandbox."""
    return safe_install.run(package, FakeClient(sandbox), **quiet(**kw))


def test_missing_env_names_every_missing_variable():
    assert safe_install.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_missing_env_does_not_require_the_model_key():
    assert safe_install.missing_env({"NEEV_API_KEY": "k", "NEEV_ORG_ID": "o", "NEEV_PROJECT_ID": "p"}) == []


def test_evil_setup_reads_the_secret_and_posts_to_the_collector():
    setup_py = evil_package_files()["setup.py"]
    assert safe_install.COLLECTOR_URL in setup_py
    assert safe_install.SECRET_ABS in setup_py
    assert 'method="POST"' in setup_py
    assert "socket.create_connection" in setup_py  # reachability, not HTTP status, is the signal
    assert "from setuptools import setup" in setup_py  # it is still a normal, installable package
    assert safe_install.MARKER_PATH in setup_py  # it records the attempt for the script to read back


def test_phone_home_blocked_requires_the_connection_to_never_open():
    assert phone_home_blocked(BLOCKED_MARKER) is True
    assert phone_home_blocked({"connected": True, "sent_ok": False}) is False  # channel opened, HTTP just errored
    assert phone_home_blocked({"connected": True, "sent_ok": True}) is False   # secret actually left
    assert phone_home_blocked(None) is False                                   # setup.py never ran


def test_generated_phone_home_marks_blocked_when_the_collector_is_unreachable(tmp_path):
    marker, secret = tmp_path / "m.json", tmp_path / "s"
    secret.write_text("sk-demo-secret")
    code = safe_install._phone_home_code(f"http://127.0.0.1:{_closed_port()}/collect", str(marker), str(secret), 1)
    exec(compile(code, "<evil-setup>", "exec"), {})
    rec = json.loads(marker.read_text())
    assert rec["connected"] is False and rec["sent_ok"] is False
    assert phone_home_blocked(rec) is True


def test_generated_phone_home_is_not_blocked_when_the_collector_is_reachable(tmp_path):
    # Regression: a reachable collector that answers 405 still received the body, so it must NOT read as blocked.
    srv, port = _local_collector()
    try:
        marker, secret = tmp_path / "m.json", tmp_path / "s"
        secret.write_text("sk-demo-secret")
        code = safe_install._phone_home_code(f"http://127.0.0.1:{port}/collect", str(marker), str(secret), 3)
        exec(compile(code, "<evil-setup>", "exec"), {})
        rec = json.loads(marker.read_text())
        assert rec["connected"] is True and rec["sent_ok"] is False
        assert phone_home_blocked(rec) is False
    finally:
        srv.shutdown()


def test_legit_install_ok_tracks_the_pip_exit_code():
    assert legit_install_ok(SimpleNamespace(exit_code=0)) is True
    assert legit_install_ok(SimpleNamespace(exit_code=1)) is False


def test_happy_path_writes_fixture_installs_both_and_deletes():
    sandbox, lines = FakeSandbox(), []
    assert go(sandbox, log=lines.append) == 0
    assert set(sandbox.store) == {"evilpkg/setup.py", "evilpkg/evilpkg.py", safe_install.SECRET_PATH}
    assert sandbox.deleted
    assert any("boundary held" in l for l in lines)


def test_create_uses_allow_egress_with_only_the_registry_hosts():
    sandbox = FakeSandbox()
    client = FakeClient(sandbox)
    safe_install.run("requests", client, **quiet())
    assert client.created[0]["allow_egress"] == ["pypi.org", "files.pythonhosted.org"]
    assert client.created[0]["params"]["name"].startswith("safe-install-")


def test_phone_home_not_blocked_returns_1_and_still_deletes():
    sandbox = FakeSandbox(marker={"connected": True, "sent_ok": True, "secret_len": 34})  # channel opened
    assert go(sandbox) == 1
    assert sandbox.deleted


def test_missing_marker_returns_1_and_still_deletes():
    sandbox = FakeSandbox(marker=None)  # the untrusted setup.py never wrote a marker
    assert go(sandbox) == 1
    assert sandbox.deleted


def test_legit_install_failure_returns_1_and_still_deletes():
    sandbox = FakeSandbox(legit_exit=1)
    assert go(sandbox) == 1
    assert sandbox.deleted


def test_legit_import_failure_is_informational_and_the_run_still_passes():
    sandbox = FakeSandbox(import_exit=1)  # package installed but imports under another name
    assert go(sandbox) == 0
    assert sandbox.deleted


def test_install_commands_have_the_expected_shape():
    sandbox = FakeSandbox()
    go(sandbox)
    evil = next(e for e in sandbox.execs if e[0] == "pip3" and any("evilpkg" in a for a in e[1]))
    legit = next(e for e in sandbox.execs if e[0] == "pip3" and "--target" in e[1])
    assert evil[1] == ["install", "--no-input", "./evilpkg"] and evil[2] == "/workspace"
    assert legit[1] == ["install", "--no-input", "--target", safe_install.LEGIT_TARGET, "requests"]


def test_sandbox_that_never_becomes_ready_is_still_deleted():
    sandbox = FakeSandbox(ready_error=RuntimeError("did not become Ready"))
    assert go(sandbox) == 1
    assert sandbox.deleted and sandbox.execs == []


def test_create_failure_is_one_line_and_there_is_nothing_to_delete():
    lines = []

    def create(params, org_id=None, project_id=None, *, allow_internet=None, allow_egress=None):
        raise RuntimeError("Error code: 401 - invalid api key")

    client = SimpleNamespace(sandboxes=SimpleNamespace(create=create))
    assert safe_install.run("requests", client, log=lines.append, wait=lambda s: None) == 1
    assert any("401" in l for l in lines)


def test_keyboard_interrupt_during_audit_poll_deletes_and_returns_130():
    sandbox = FakeSandbox(audit_pages=[[]])  # never yields pip records, so the poll waits

    def interrupt(_seconds):
        raise KeyboardInterrupt

    assert safe_install.run("requests", FakeClient(sandbox), log=lambda *_: None, wait=interrupt) == 130
    assert sandbox.deleted


def test_pip_records_polls_from_since_until_both_runs_appear():
    waited = []
    sandbox = FakeSandbox(audit_pages=[
        [audit_record(tool="fs.write", command=None, target="evilpkg/setup.py")],
        [audit_record(), audit_record(command="python3"), audit_record()]])
    records = pip_records(sandbox, "T0", wait=lambda s: waited.append(s), log=lambda *_: None)
    assert [r.command for r in records] == ["pip3", "pip3"]
    assert waited == [safe_install.AUDIT_POLL_INTERVAL_S]
    assert sandbox.audit_queries == ["T0", "T0"]


def test_pip_records_returns_what_it_has_when_the_poll_runs_out():
    lines = []
    sandbox = FakeSandbox(audit_pages=[[audit_record()]])
    records = pip_records(sandbox, "T0", attempts=3, wait=lambda s: None, log=lines.append)
    assert len(records) == 1 and any("1 of 2" in l for l in lines)


def test_runs_get_different_sandbox_names():
    names = []
    for _ in range(2):
        client = FakeClient(FakeSandbox())
        safe_install.run("requests", client, **quiet())
        names.append(client.created[0]["params"]["name"])
    assert names[0] != names[1] and all(n.startswith("safe-install-") for n in names)


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in safe_install.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert safe_install.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err
