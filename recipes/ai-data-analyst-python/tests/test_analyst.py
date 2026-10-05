import contextlib
from pathlib import Path
from types import SimpleNamespace

import analyst
from tests.fakes import FakeModel, FakeSession, tool_call

PNG = b"\x89PNG\r\n\x1a\n" + b"chart"
CHART_CODE = "plt.savefig('chart.png')"


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params and keyword arguments."""

    def __init__(self, sandbox):
        self.created = []

        def create(params, **kw):
            self.created.append({**params, **kw})
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def ready_sandbox(chart=PNG, pip_exit=0):
    """A sandbox that records exec, uploads, egress updates and deletion; chart.png reads back as `chart`."""
    sb = SimpleNamespace(name="data-analyst-1", execs=[], uploads=[], updates=[], deleted=False)
    sb.wait_until_ready = lambda timeout_ms=None: sb
    sb.exec = lambda command, **kw: sb.execs.append(command) or SimpleNamespace(exit_code=pip_exit, stdout="", stderr="ERROR: no matching distribution")
    sb.update = lambda params: sb.updates.append(params) or sb

    def read(path):
        if chart is None:
            raise FileNotFoundError(f'file not found: "{path}"')
        return chart

    sb.files = SimpleNamespace(upload_file=lambda local, remote: sb.uploads.append((local, remote)) or {"bytes_written": 1}, read=read)
    sb.delete = lambda: setattr(sb, "deleted", True)
    return sb


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect


def finishing_model():
    return FakeModel([tool_call("run_python", {"code": CHART_CODE}), tool_call("finish", {"findings": "Mumbai leads."}, "c2")])


def go(sb, tmp_path, model=None, lines=None, client=None, session=None):
    log = lines.append if lines is not None else (lambda *_: None)
    return analyst.run("q", analyst.SAMPLE_CSV, tmp_path / "chart.png", client or FakeClient(sb), model or finishing_model(), "m",
                       connector(session or FakeSession()), log=log)


def test_missing_env_names_every_missing_variable():
    assert analyst.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_the_bundled_sample_is_a_few_hundred_rows_of_csv():
    rows = Path(analyst.SAMPLE_CSV).read_text().splitlines()
    assert rows[0] == "month,city,category,orders,revenue_inr" and 200 < len(rows) < 1000


def test_happy_path_installs_with_pypi_only_then_locks_egress_and_downloads_the_chart(tmp_path):
    sb, lines = ready_sandbox(), []
    client, session = FakeClient(sb), FakeSession()
    connect = connector(session)
    code = analyst.run("q", analyst.SAMPLE_CSV, tmp_path / "chart.png", client, finishing_model(), "m", connect, log=lines.append)
    assert code == 0
    assert client.created[0]["allow_egress"] == ["pypi.org", "files.pythonhosted.org"]
    assert client.created[0]["name"].startswith("data-analyst-")
    assert sb.execs[0][:3] == ["python3", "-m", "pip"] and sb.execs[0][-2:] == ["pandas", "matplotlib"]
    assert sb.updates == [{"egress": {"mode": "deny_all"}}]
    assert sb.uploads == [(analyst.SAMPLE_CSV, "data.csv")]
    assert connect.names == ["data-analyst-1"]
    assert (tmp_path / "chart.png").read_bytes() == PNG
    assert any("Mumbai leads." in l for l in lines)
    assert sb.deleted


def test_egress_is_locked_before_the_data_is_uploaded(tmp_path):
    sb, order = ready_sandbox(), []
    sb.update = lambda params: order.append("lock") or sb
    sb.files.upload_file = lambda local, remote: order.append("upload") or {"bytes_written": 1}
    assert go(sb, tmp_path) == 0
    assert order == ["lock", "upload"]


def test_egress_is_locked_before_the_agent_runs(tmp_path):
    sb = ready_sandbox()
    seen = []

    @contextlib.asynccontextmanager
    async def connect(name):
        seen.append(list(sb.updates))
        yield FakeSession()

    analyst.run("q", analyst.SAMPLE_CSV, tmp_path / "c.png", FakeClient(sb), finishing_model(), "m", connect, log=lambda *_: None)
    assert seen == [[{"egress": {"mode": "deny_all"}}]]


def test_failed_install_stops_before_the_agent_and_deletes(tmp_path):
    sb, lines = ready_sandbox(pip_exit=1), []
    model = finishing_model()
    assert go(sb, tmp_path, model=model, lines=lines) == 1
    assert model.requests == [] and sb.deleted
    assert any("no matching distribution" in l for l in lines)


def test_agent_failure_deletes_and_writes_no_chart(tmp_path):
    sb = ready_sandbox()
    assert go(sb, tmp_path, model=FakeModel([tool_call("fs_list", {})] * 40)) == 1
    assert sb.deleted and not (tmp_path / "chart.png").exists()


def test_a_chart_that_is_not_a_png_fails_and_writes_nothing(tmp_path):
    sb, lines = ready_sandbox(chart=b"<svg/>"), []
    assert go(sb, tmp_path, lines=lines) == 1
    assert sb.deleted and not (tmp_path / "chart.png").exists()
    assert any("not a PNG" in l for l in lines)


def test_ctrl_c_part_way_deletes_the_sandbox_and_exits_130(tmp_path):
    sb = ready_sandbox()

    def interrupted(command, **kw):
        raise KeyboardInterrupt

    sb.exec = interrupted
    assert go(sb, tmp_path) == 130 and sb.deleted


def test_unexpected_error_is_one_line_and_deletes(tmp_path):
    sb, lines = ready_sandbox(), []

    async def broken(**kwargs):
        raise RuntimeError("Error code: 401 - invalid api key")

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    assert go(sb, tmp_path, model=model, lines=lines) == 1 and sb.deleted
    assert any(l.startswith("Failed: RuntimeError") and "401" in l for l in lines)


def test_an_error_wrapped_in_an_exception_group_reports_the_real_cause(tmp_path):
    sb, lines = ready_sandbox(), []

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    assert go(sb, tmp_path, model=model, lines=lines) == 1
    assert any("ConnectionError: MCP stream closed" in l for l in lines)


def test_agent_failure_inside_the_mcp_task_group_is_still_reported_as_an_agent_failure(tmp_path):
    sb, lines = ready_sandbox(), []

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps errors raised inside it.
        try:
            yield FakeSession()
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    model = FakeModel([tool_call("fs_list", {})] * 40)
    code = analyst.run("q", analyst.SAMPLE_CSV, tmp_path / "c.png", FakeClient(sb), model, "m", grouping_connect, log=lines.append)
    assert code == 1
    assert any(l.startswith("The agent did not finish the analysis: step limit") for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names(tmp_path):
    client = FakeClient(ready_sandbox())
    for _ in range(2):
        go(None, tmp_path, client=client)
    names = [p["name"] for p in client.created]
    assert names[0] != names[1]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in analyst.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert analyst.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_main_exits_2_when_the_csv_does_not_exist(monkeypatch, capsys):
    for name in analyst.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert analyst.main(["--csv", "/no/such/file.csv"]) == 2
    assert "/no/such/file.csv" in capsys.readouterr().err
