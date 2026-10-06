import contextlib
from types import SimpleNamespace

import pytest
from neevai.errors import NotFoundError

import report
from tests.fakes import PDF, FakeModel, FakeSession, building_model, tool_call, xlsx


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params and egress args."""

    def __init__(self, sandbox):
        self.created = []

        def create(params, allow_egress=None):
            self.created.append((params, allow_egress))
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def missing(path):
    return NotFoundError(404, {"code": "not_found", "message": f"file not found: {path}"}, None)


def ready_sandbox(pip_exit=0):
    """A sandbox whose workspace dict is shared with the fake MCP session, so the agent's files can be downloaded."""
    sb = SimpleNamespace(name="report-gen-1", workspace={}, events=[], deleted=False)
    sb.wait_until_ready = lambda timeout_ms=None: sb

    def execute(command, timeout_ms=None, **kw):
        sb.events.append(("exec", command))
        return SimpleNamespace(exit_code=pip_exit, stdout="", stderr="ERROR: no matching distribution")

    def read(path, cwd=None):
        if path not in sb.workspace:
            raise missing(path)
        return sb.workspace[path]

    def upload_file(local, remote, **kw):
        sb.events.append(("upload", remote))
        sb.workspace[remote] = open(local, "rb").read()

    def stat(path, cwd=None):
        if path not in sb.workspace:
            raise missing(path)
        return SimpleNamespace(name=path, type="file", size=len(sb.workspace[path]))

    sb.exec = execute
    sb.update = lambda params: sb.events.append(("update", params))
    sb.files = SimpleNamespace(read=read, stat=stat, upload_file=upload_file)
    sb.delete = lambda: setattr(sb, "deleted", True)
    return sb


def connector(sandbox, session=None):
    """Returns a connect(sandbox_name) factory that yields a fake session over the sandbox's workspace."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session or FakeSession(files=sandbox.workspace)

    connect.names = names
    return connect


def run(sb, model, tmp_path, **kw):
    lines = kw.pop("lines", [])
    code = report.run("expense report", report.SAMPLE_CSV, tmp_path, FakeClient(sb) if "client" not in kw else kw.pop("client"),
                      model, "m", kw.pop("connect", None) or connector(sb), log=lines.append)
    return code, lines


def test_missing_env_names_every_missing_variable():
    assert report.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]


def test_check_pdf_counts_pages_and_images():
    assert report.check_pdf(PDF) == "2 pages, 1 image"


def test_check_pdf_does_not_count_a_transparency_mask_as_a_second_image():
    masked = PDF.replace(b"/Subtype /Image /Width 10>>", b"/Subtype /Image /Width 10 /SMask 6 0 R>>\nendobj\n"
                         b"6 0 obj\n<</Type /XObject /Subtype /Image /ColorSpace /DeviceGray>>")
    assert report.check_pdf(masked) == "2 pages, 1 image"


@pytest.mark.parametrize("data, error", [
    (b"<html>not a pdf</html>", "report.pdf is not a PDF"),
    (PDF.replace(b"%%EOF", b""), "report.pdf is cut short"),
    (PDF.replace(b"/Type /Page /", b"/Type /Pagex /"), "report.pdf has no pages"),
    (PDF.replace(b"/Subtype /Image", b"/Subtype /Form"), "report.pdf has no chart image"),
])
def test_check_pdf_rejects_broken_files(data, error):
    with pytest.raises(report.ReportInvalid, match=error):
        report.check_pdf(data)


def test_check_xlsx_lists_sheets_and_counts_formulas():
    assert report.check_xlsx(xlsx(formulas=4)) == "sheets Data, Summary; 4 formulas"


@pytest.mark.parametrize("data, error", [
    (b"PK not really a zip", "report.xlsx is not an Excel workbook"),
    (xlsx(sheets=("Data",)), "report.xlsx needs a data sheet and a summary sheet"),
    (xlsx(formulas=0), "report.xlsx has no formulas"),
])
def test_check_xlsx_rejects_broken_files(data, error):
    with pytest.raises(report.ReportInvalid, match=error):
        report.check_xlsx(data)


def test_check_xlsx_refuses_a_workbook_that_unpacks_past_the_cap(monkeypatch):
    monkeypatch.setattr(report, "MAX_REPORT_BYTES", 1000)
    with pytest.raises(report.ReportInvalid, match="report.xlsx unpacks to more than the size cap"):
        report.check_xlsx(xlsx(formulas=100))


def test_fetch_reports_names_a_file_the_agent_has_not_written():
    sb = ready_sandbox()
    sb.workspace["report.xlsx"] = xlsx()
    with pytest.raises(report.ReportInvalid, match="report.pdf is missing"):
        report.fetch_reports(sb)


def test_fetch_reports_refuses_an_oversized_file_without_downloading_it():
    sb = ready_sandbox()
    sb.workspace.update({"report.pdf": PDF, "report.xlsx": xlsx()})
    sb.files.stat = lambda path, cwd=None: SimpleNamespace(name=path, type="file", size=report.MAX_REPORT_BYTES + 1)
    sb.files.read = lambda path, cwd=None: pytest.fail("an oversized report must not be downloaded")
    with pytest.raises(report.ReportInvalid, match="report.pdf is larger than 50 MB"):
        report.fetch_reports(sb)


def test_fetch_reports_refuses_a_symlink_whose_target_size_stat_does_not_show():
    sb = ready_sandbox()
    sb.workspace.update({"report.pdf": PDF, "report.xlsx": xlsx()})
    sb.files.stat = lambda path, cwd=None: SimpleNamespace(name=path, type="symlink", size=12)
    sb.files.read = lambda path, cwd=None: pytest.fail("a symlink must not be downloaded")
    with pytest.raises(report.ReportInvalid, match="report.pdf is not a regular file"):
        report.fetch_reports(sb)


def test_happy_path_installs_then_locks_egress_before_the_data_and_saves_both_reports(tmp_path):
    sb = ready_sandbox()
    client = FakeClient(sb)
    code, lines = run(sb, building_model("- Cloud spend up 38%"), tmp_path, client=client)
    assert code == 0, lines
    params, allow = client.created[0]
    assert params["name"].startswith("report-gen-") and allow == ["pypi.org", "files.pythonhosted.org"]
    assert [e[0] for e in sb.events] == ["exec", "update", "upload"]
    assert sb.events[0][1][-4:] == ["pandas", "matplotlib", "openpyxl", "fpdf2>=2.8,<3"]
    assert sb.events[1][1] == {"egress": {"mode": "deny_all"}}
    assert sb.events[2][1] == "data.csv"
    assert (tmp_path / "report.pdf").read_bytes() == PDF
    assert (tmp_path / "report.xlsx").read_bytes() == xlsx()
    assert any("2 pages, 1 image" in l for l in lines) and any("sheets Data, Summary" in l for l in lines)
    assert "- Cloud spend up 38%" in lines
    assert sb.deleted


def test_failed_install_stops_before_the_data_is_uploaded_and_deletes(tmp_path):
    sb = ready_sandbox(pip_exit=1)
    code, lines = run(sb, building_model(), tmp_path)
    assert code == 1 and sb.deleted
    assert [e[0] for e in sb.events] == ["exec"]
    assert any("pip install exited 1" in l for l in lines)


def test_agent_failure_saves_nothing_and_deletes(tmp_path):
    sb = ready_sandbox()
    code, lines = run(sb, FakeModel([tool_call("fs_list", {})] * 30), tmp_path)
    assert code == 1 and sb.deleted
    assert any(l.startswith("The agent did not finish the report: step limit") for l in lines)
    assert list(tmp_path.iterdir()) == []


def test_invalid_reports_after_the_agent_finishes_fail_the_run(tmp_path):
    sb = ready_sandbox()

    def writes_a_fake_pdf(session, script):
        session.files.update({"report.pdf": b"not a pdf", "report.xlsx": xlsx()})
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    model = FakeModel([*building_model().replies[:2], *[tool_call("finish", {"findings": "x"}, f"f{i}") for i in range(30)]])
    code, lines = run(sb, model, tmp_path, connect=connector(sb, FakeSession(files=sb.workspace, on_exec=writes_a_fake_pdf)))
    assert code == 1 and sb.deleted
    assert any("report.pdf is not a PDF" in l for l in lines)
    assert list(tmp_path.iterdir()) == []


def test_ctrl_c_mid_run_deletes_and_exits_130(tmp_path):
    sb = ready_sandbox()

    def interrupted(command, timeout_ms=None, **kw):
        raise KeyboardInterrupt

    sb.exec = interrupted
    code, _ = run(sb, building_model(), tmp_path)
    assert code == 130 and sb.deleted


def test_an_error_wrapped_in_an_exception_group_is_one_line_naming_the_real_cause(tmp_path):
    sb = ready_sandbox()

    async def broken(**kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionError("MCP stream closed")])

    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    code, lines = run(sb, model, tmp_path)
    assert code == 1 and sb.deleted
    assert "Failed: ConnectionError: MCP stream closed" in lines


def test_agent_failure_inside_the_mcp_task_group_is_still_reported_as_an_agent_failure(tmp_path):
    sb = ready_sandbox()

    @contextlib.asynccontextmanager
    async def grouping_connect(name):
        # The real client runs the session in a task group, which wraps errors raised inside it.
        try:
            yield FakeSession(files=sb.workspace)
        except Exception as e:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [e]) from None

    code, lines = run(sb, FakeModel([tool_call("fs_list", {})] * 30), tmp_path, connect=grouping_connect)
    assert code == 1
    assert any(l.startswith("The agent did not finish the report: step limit") for l in lines)


def test_runs_started_in_the_same_second_get_different_sandbox_names(tmp_path):
    client = FakeClient(ready_sandbox())
    for _ in range(2):
        sb = ready_sandbox()
        client.sandboxes.create = lambda params, allow_egress=None, sb=sb: client.created.append((params, allow_egress)) or sb
        run(sb, building_model(), tmp_path, client=client)
    names = [p["name"] for p, _ in client.created]
    assert names[0] != names[1]


def test_main_exits_2_naming_missing_env_before_creating_anything(monkeypatch, capsys):
    for name in report.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert report.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_main_exits_2_when_the_csv_does_not_exist(monkeypatch, capsys):
    for name in report.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    assert report.main(["--csv", "/nope/missing.csv"]) == 2
    assert "CSV file not found" in capsys.readouterr().err
