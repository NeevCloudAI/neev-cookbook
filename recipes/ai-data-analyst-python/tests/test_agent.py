import asyncio
import json
import time

import pytest

from agent import AGENT_TOOLS, MAX_OUTPUT, AgentFailed, analyse, call_tool, openai_tools, run_python
from tests.fakes import FakeModel, FakeSession, text, tool_call

CHART_CODE = "import matplotlib.pyplot as plt\nplt.plot([1]); plt.savefig('chart.png')"


def run(session, model, **kw):
    return asyncio.run(analyse(session, model, "m", "q", log=kw.pop("log", lambda *_: None), **kw))


def test_model_sees_fs_list_plus_run_python_and_finish():
    tools = asyncio.run(openai_tools(FakeSession()))
    names = [t["function"]["name"] for t in tools]
    assert names == [*AGENT_TOOLS, "run_python", "finish"]
    assert "exec" not in names and "fs_write" not in names and "delete_sandbox" not in names
    fs_list = tools[0]["function"]
    assert fs_list["description"] == "fs_list from the server"


def test_run_python_writes_the_script_and_runs_it_with_python3_headless():
    session = FakeSession()
    out = asyncio.run(run_python(session, CHART_CODE))
    assert session.files["analysis.py"] == CHART_CODE
    name, args = session.calls[-1]
    assert name == "exec" and args["program"] == "python3" and args["args"] == ["analysis.py"]
    assert "MPLBACKEND=Agg" in args["env"]
    assert json.loads(out)["exit_code"] == 0 and "chart.png" in session.files


def test_run_python_reports_a_failing_script_with_its_traceback():
    session = FakeSession(on_exec=lambda s, code: {"exit_code": 1, "stdout": "", "stderr": "KeyError: 'sales'"})
    out = json.loads(asyncio.run(run_python(session, "df['sales']")))
    assert out["exit_code"] == 1 and "KeyError" in out["stderr"]


def test_run_python_stops_when_the_script_cannot_be_written():
    session = FakeSession()
    session.raise_on["fs_write"] = ConnectionError("stream closed")
    assert asyncio.run(run_python(session, "print(1)")).startswith("error:")
    assert [n for n, _ in session.calls] == ["fs_write"]


def test_long_output_is_clipped_and_a_raising_call_is_an_error_not_a_crash():
    session = FakeSession(on_exec=lambda s, code: {"exit_code": 0, "stdout": "x" * (MAX_OUTPUT * 3), "stderr": ""})
    out = asyncio.run(run_python(session, "print('x' * 99999)"))
    assert len(out) < MAX_OUTPUT + 200 and "truncated" in out
    session.raise_on["fs_list"] = TimeoutError("deadline_exceeded")
    assert asyncio.run(call_tool(session, "fs_list", {})).startswith("error:")


def test_loop_runs_code_saves_the_chart_and_returns_the_findings():
    session = FakeSession()
    model = FakeModel([tool_call("run_python", {"code": CHART_CODE}),
                       tool_call("finish", {"findings": "Mumbai leads revenue."}, "c2")])
    assert run(session, model) == "Mumbai leads revenue."
    assert model.requests[1]["messages"][-1]["role"] == "tool"
    assert "chart.png" in session.files


def test_finish_without_a_chart_is_an_error_the_model_can_fix():
    model = FakeModel([tool_call("finish", {"findings": "early"}),
                       tool_call("run_python", {"code": CHART_CODE}, "c2"),
                       tool_call("finish", {"findings": "done"}, "c3")])
    assert run(FakeSession(), model) == "done"
    assert "chart.png" in model.requests[1]["messages"][-1]["content"]


def test_finish_with_empty_findings_is_an_error_the_model_can_fix():
    model = FakeModel([tool_call("run_python", {"code": CHART_CODE}),
                       tool_call("finish", {"findings": "  "}, "c2"),
                       tool_call("finish", {"findings": "real findings"}, "c3")])
    assert run(FakeSession(), model) == "real findings"
    assert model.requests[2]["messages"][-1]["content"].startswith("error:")


def test_loop_refuses_tools_the_model_was_not_given():
    session = FakeSession()
    model = FakeModel([tool_call("exec", {"program": "curl"}), tool_call("delete_sandbox", {}, "c2"),
                       tool_call("run_python", {"code": CHART_CODE}, "c3"), tool_call("finish", {"findings": "ok"}, "c4")])
    assert run(session, model) == "ok"
    assert ("exec", {"program": "curl"}) not in session.calls
    assert "delete_sandbox" not in [name for name, _ in session.calls]
    assert model.requests[1]["messages"][-1]["content"].startswith("error:")


def test_loop_explains_malformed_arguments_and_echoes_them_as_valid_json():
    bad = tool_call("run_python", {})
    bad.tool_calls[0].function.arguments = '{"code": "import pandas as pd\\ndf = pd.read_csv('
    model = FakeModel([bad, tool_call("run_python", {"code": CHART_CODE}, "c2"), tool_call("finish", {"findings": "ok"}, "c3")])
    assert run(FakeSession(), model) == "ok"
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]
    assert json.loads(model.requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {}


def test_run_python_without_code_is_an_error_not_a_crash():
    model = FakeModel([tool_call("run_python", {"source": "print(1)"}), tool_call("run_python", {"code": CHART_CODE}, "c2"),
                       tool_call("finish", {"findings": "ok"}, "c3")])
    assert run(FakeSession(), model) == "ok"
    assert "code" in model.requests[1]["messages"][-1]["content"]


def test_text_reply_after_the_chart_is_nudged_towards_finish():
    model = FakeModel([tool_call("run_python", {"code": CHART_CODE}), text("Next I'll compute the lift."),
                       tool_call("finish", {"findings": "Pune grew fastest."}, "c2")])
    assert run(FakeSession(), model) == "Pune grew fastest."
    assert "call finish" in model.requests[2]["messages"][-1]["content"]


def test_loop_nudges_once_then_fails_on_text_only():
    model = FakeModel([text("I would use pandas..."), text("Done.")])
    with pytest.raises(AgentFailed, match="without using the tools"):
        run(FakeSession(), model)
    assert "use the tools" in model.requests[1]["messages"][-1]["content"]


def test_step_limit_fails_even_when_a_chart_exists():
    session = FakeSession()
    session.files["chart.png"] = "\x89PNG"
    with pytest.raises(AgentFailed, match="step limit of 3"):
        run(session, FakeModel([tool_call("fs_list", {})] * 3), max_steps=3)


class SlowModel:
    """A model whose call never returns in time."""

    def __init__(self):
        async def create(**kwargs):
            await asyncio.sleep(30)

        self.chat = type("C", (), {"completions": type("CC", (), {"create": staticmethod(create)})})()


def test_tool_calls_wait_no_longer_than_the_time_left():
    session = FakeSession()
    model = FakeModel([tool_call("fs_list", {}), tool_call("run_python", {"code": CHART_CODE}, "c2"),
                       tool_call("finish", {"findings": "ok"}, "c3")])
    assert run(session, model, deadline_s=60) == "ok"
    bounded = [t for (name, _), t in zip(session.calls, session.timeouts) if name in ("fs_list", "fs_write", "exec")]
    assert len(bounded) == 3 and all(t is not None and 0 < t <= 60 for t in bounded)


class HangingExecSession(FakeSession):
    """exec never finishes: it waits out the read timeout it was given, or 30s without one, then raises."""

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        if name != "exec":
            return await super().call_tool(name, arguments, read_timeout_seconds)
        await asyncio.sleep(30 if read_timeout_seconds is None else read_timeout_seconds)
        raise TimeoutError("timed out waiting for the tool")


def test_a_hanging_script_is_cut_at_the_deadline():
    model = FakeModel([tool_call("run_python", {"code": "while True: pass"})] * 3)
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        asyncio.run(analyse(HangingExecSession(), model, "m", "q", deadline_s=0.3, log=lambda *_: None))
    assert time.monotonic() - t < 5


def test_deadline_cuts_a_slow_model_call():
    t = time.monotonic()
    with pytest.raises(AgentFailed, match="time limit"):
        asyncio.run(analyse(FakeSession(), SlowModel(), "m", "q", deadline_s=0.2, log=lambda *_: None))
    assert time.monotonic() - t < 5
