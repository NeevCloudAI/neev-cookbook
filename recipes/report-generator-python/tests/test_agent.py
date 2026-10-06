import asyncio
import json
import time

import pytest

from agent import AGENT_TOOLS, MAX_FILE, MAX_OUTPUT, AgentFailed, build_report, call_tool, openai_tools
from tests.fakes import FakeModel, FakeSession, building_model, text, tool_call


def has_reports(session):
    """A check that passes once both outputs exist in the fake workspace."""
    async def check():
        missing = [n for n in ("report.pdf", "report.xlsx") if n not in session.files]
        return f"{' and '.join(missing)} missing" if missing else None
    return check


def build(session, model, check=None, **kw):
    return asyncio.run(build_report(session, model, "m", "x", check or has_reports(session),
                                    log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_only_the_workspace_tools_with_the_server_schemas():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "finish"] and AGENT_TOOLS == ("fs_write", "fs_read", "fs_list", "exec")
    exec_tool = tools[names.index("exec")]["function"]
    assert exec_tool["description"] == "exec from the server"
    assert exec_tool["parameters"] == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_call_tool_renders_structured_content_and_passes_the_timeout():
    session = FakeSession()
    asyncio.run(call_tool(session, "fs_write", {"path": "a.py", "content": "print(1)"}, 12.5))
    out = asyncio.run(call_tool(session, "fs_read", {"path": "a.py"}))
    assert json.loads(out)["content"] == "print(1)"
    assert session.timeouts[0] == 12.5


def test_server_refusal_and_raised_errors_are_errors_for_the_model():
    session = FakeSession()
    assert "escapes workspace root" in asyncio.run(call_tool(session, "fs_write", {"path": "/etc/x", "content": "x"}))
    session.raise_on["exec"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "exec", {"program": "sleep", "args": ["999"]})).startswith("error:")


def test_reads_keep_whole_files_and_other_output_keeps_head_and_tail():
    warnings = "DeprecationWarning: ln is deprecated\n" * 500
    session = FakeSession(on_exec=lambda s, _: {"stdout": "", "stderr": warnings + "NameError: name 'df' is not defined",
                                                "exit_code": 1})
    session.files["build_report.py"] = "y" * 12_000
    assert "y" * 12_000 in asyncio.run(call_tool(session, "fs_read", {"path": "build_report.py"}))
    out = asyncio.run(call_tool(session, "exec", {"program": "python3", "args": ["build_report.py"]}))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out
    # the traceback sits at the end of stderr, after the warnings: the model must still see it
    assert "NameError: name 'df' is not defined" in out and '"exit_code": 1' in out
    assert MAX_FILE > 12_000


def test_loop_writes_the_script_runs_it_and_returns_the_findings():
    session = FakeSession()
    model = building_model("- Sales travel jumped in Q4")
    assert build(session, model) == "- Sales travel jumped in Q4"
    assert {"report.pdf", "report.xlsx"} <= set(session.files)
    assert [n for n, _ in session.calls if n != "fs_read"] == ["fs_write", "exec"]
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_reports_built_by_two_scripts_pass_once_both_have_run():
    session = FakeSession()
    model = FakeModel([tool_call("fs_write", {"path": "build_xlsx.py", "content": "wb.save('report.xlsx')"}),
                       tool_call("exec", {"program": "python3", "args": ["build_xlsx.py"]}, "c2"),
                       tool_call("finish", {"findings": "half"}, "c3"),
                       tool_call("fs_write", {"path": "build_pdf.py", "content": "pdf.output('report.pdf')"}, "c4"),
                       tool_call("exec", {"program": "python3", "args": ["build_pdf.py"]}, "c5"),
                       tool_call("finish", {"findings": "both"}, "c6")])
    assert build(session, model) == "both"
    assert model.requests[3]["messages"][-1]["content"] == "error: report.pdf missing"


def test_rewriting_a_script_that_never_ran_tells_the_model_to_run_it_first():
    session = FakeSession()
    write = {"path": "build_pdf.py", "content": "pdf.output('report.pdf')"}
    model = FakeModel([tool_call("fs_write", write),
                       tool_call("fs_write", write, "c2"),
                       tool_call("exec", {"program": "python3", "args": ["build_pdf.py"]}, "c3"),
                       tool_call("fs_write", write, "c4"),
                       tool_call("finish", {"findings": "done"}, "c5")])
    build(session, model, check=lambda: asyncio.sleep(0))
    results = [m["content"] for m in model.requests[4]["messages"] if m["role"] == "tool"]
    assert "run it" not in results[0]
    # glm-4-7 once rewrote build_pdf.py eight times without running it and ran out of time
    assert "build_pdf.py has not run since you last wrote it" in results[1]
    # a write after a run is an ordinary fix, with no reminder
    assert "has not run" not in results[3]


def test_a_run_through_a_shell_counts_as_running_the_script():
    session = FakeSession()
    write = {"path": "build_xlsx.py", "content": "wb.save('report.xlsx')"}
    model = FakeModel([tool_call("fs_write", write),
                       tool_call("exec", {"program": "sh", "args": ["-c", "python3 ./build_xlsx.py"]}, "c2"),
                       tool_call("fs_write", write, "c3"),
                       tool_call("finish", {"findings": "done"}, "c4")])
    build(session, model, check=lambda: asyncio.sleep(0))
    results = [m["content"] for m in model.requests[3]["messages"] if m["role"] == "tool"]
    assert "has not run" not in results[2]


def test_finish_is_refused_with_the_check_result_until_the_reports_are_valid():
    session = FakeSession()
    model = FakeModel([tool_call("finish", {"findings": "early"}),
                       tool_call("fs_write", {"path": "build_report.py", "content": "report.pdf report.xlsx"}, "c2"),
                       tool_call("exec", {"program": "python3", "args": ["build_report.py"]}, "c3"),
                       tool_call("finish", {"findings": "done"}, "c4")])
    assert build(session, model) == "done"
    assert model.requests[1]["messages"][-1]["content"] == "error: report.pdf and report.xlsx missing"


def test_finish_with_empty_findings_is_an_error_the_model_can_fix():
    session = FakeSession()
    session.files.update({"report.pdf": b"", "report.xlsx": b""})
    model = FakeModel([tool_call("finish", {"findings": "  "}), tool_call("finish", {"findings": "ok"}, "c2")])
    assert build(session, model) == "ok"
    assert "findings are empty" in model.requests[1]["messages"][-1]["content"]


def test_loop_refuses_tools_the_model_was_not_given():
    session = FakeSession()
    model = FakeModel([tool_call("delete_sandbox", {}), *building_model().replies])
    assert build(session, model) == "- Engineering cloud spend rose 38%"
    assert "delete_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"] == "error: delete_sandbox is not one of your tools"


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("fs_write", {})
    bad.tool_calls[0].function.arguments = '{"path": "build_report.py", "content": "import pan'
    model = FakeModel([bad, *building_model().replies])
    assert build(FakeSession(), model) == "- Engineering cloud spend rose 38%"
    # the parse error tells the model where its JSON broke (glm-4-7 sometimes drops a closing bracket)
    assert "not valid JSON (Unterminated string" in model.requests[1]["messages"][-1]["content"]
    # the history sent back must itself be valid JSON, or the server rejects the next request
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_loop_nudges_once_with_the_check_result_then_fails_on_text_only():
    model = FakeModel([text("Here is how I would build the report..."), text("Done.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        build(FakeSession(), model)
    nudge = model.requests[1]["messages"][-1]["content"]
    assert "use the tools" in nudge and "report.pdf and report.xlsx missing" in nudge


def test_text_reply_once_the_reports_pass_the_check_counts_as_the_findings():
    session = FakeSession()
    model = FakeModel([*building_model().replies[:2], text("- Marketing events doubled in October")])
    assert build(session, model) == "- Marketing events doubled in October"


def test_step_limit_fails_when_the_reports_are_not_valid():
    with pytest.raises(AgentFailed, match="step limit of 3 reached: report.pdf and report.xlsx missing"):
        build(FakeSession(), FakeModel([tool_call("fs_list", {})] * 3), max_steps=3)


def test_step_limit_keeps_reports_that_already_pass_the_check():
    session, lines = FakeSession(), []
    session.files.update({"report.pdf": b"", "report.xlsx": b""})
    findings = build(session, FakeModel([tool_call("fs_list", {})] * 2), max_steps=2, log=lines.append)
    assert "step limit" in findings
    assert any("keeping the reports the agent built" in l for l in lines)


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_deadline_cuts_a_slow_model_call_and_fails_without_reports():
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        build(FakeSession(), SlowModel(), deadline_s=0.2)
    assert time.monotonic() - t < 5


def test_tool_calls_get_only_the_time_left_in_the_budget():
    session = FakeSession()
    build(session, building_model(), deadline_s=100)
    assert len(session.timeouts) == 2 and all(0 < t <= 100 for t in session.timeouts)
