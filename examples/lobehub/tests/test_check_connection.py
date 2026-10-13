import pytest

import check_connection
from tests.fakes import FakeMCP


@pytest.fixture
def run(monkeypatch):
    servers = []

    def start(**options):
        server = FakeMCP(**options)
        servers.append(server)
        monkeypatch.setenv("NEEV_API_KEY", "sk-test")
        monkeypatch.setenv("NEEV_SANDBOX_MCP_URL", server.url)
        return server

    yield start
    for server in servers:
        server.close()


def test_happy_path_lists_creates_execs_and_deletes(run, capsys):
    server = run()

    assert check_connection.main() == 0

    assert server.tools_called() == ["tools/list", "create_sandbox", "exec", "delete_sandbox"]
    out = capsys.readouterr().out
    assert "2 tools published" in out
    assert "hello from the sandbox" in out


def test_sends_the_key_as_a_bearer_and_a_prefixed_random_name(run):
    server = run()

    check_connection.main()

    _, _, headers = server.calls[0]
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["x-sandbox-name"].startswith("lobehub-check-")
    assert len({calls[2]["x-sandbox-name"] for calls in server.calls}) == 1


def test_sends_its_own_user_agent_because_cloudflare_blocks_urllibs(run):
    server = run()

    check_connection.main()

    assert not server.calls[0][2]["user-agent"].startswith("Python-urllib")


def test_missing_key_exits_2_before_any_request(monkeypatch, capsys):
    monkeypatch.delenv("NEEV_API_KEY", raising=False)

    assert check_connection.main() == 2

    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_deletes_the_sandbox_when_exec_fails(run, capsys):
    server = run(exec_exit_code=3)

    assert check_connection.main() == 1

    assert server.tools_called()[-1] == "delete_sandbox"
    assert "exited 3" in capsys.readouterr().err


def test_rejected_key_is_one_line_and_creates_nothing(run, capsys):
    server = run(status=401)

    assert check_connection.main() == 1

    assert server.tools_called() == ["tools/list"]
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "invalid credentials" in err
    assert "Traceback" not in err


def test_reads_a_server_sent_event_response(run):
    run(sse=True)

    assert check_connection.main() == 0


def test_never_prints_the_key(run, capsys):
    run()

    check_connection.main()

    captured = capsys.readouterr()
    assert "sk-test" not in captured.out + captured.err


def test_unreachable_server_is_one_line(monkeypatch, capsys):
    monkeypatch.setenv("NEEV_API_KEY", "sk-test")
    monkeypatch.setenv("NEEV_SANDBOX_MCP_URL", "http://127.0.0.1:1/mcp")

    assert check_connection.main() == 1

    assert "cannot reach" in capsys.readouterr().err


def test_ctrl_c_deletes_the_sandbox_and_prints_one_line(run, monkeypatch, capsys):
    server = run()
    real_call_tool = check_connection.call_tool

    def interrupt_on_exec(url, key, name, tool, arguments):
        if tool == "exec":
            raise KeyboardInterrupt
        return real_call_tool(url, key, name, tool, arguments)

    monkeypatch.setattr(check_connection, "call_tool", interrupt_on_exec)

    assert check_connection.main() == 130

    assert server.tools_called()[-1] == "delete_sandbox"
    err = capsys.readouterr().err
    assert "interrupted" in err and "Traceback" not in err
