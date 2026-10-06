"""Agent loop for the report generator recipe: the model builds a PDF and an XLSX through the sandbox's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time

# The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 6000
MAX_FILE = 30_000  # reads return the whole build script, so the model never "repairs" a file it saw cut short

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once report.pdf and report.xlsx are built, with your key findings.",
    "parameters": {"type": "object", "properties": {"findings": {
        "type": "string", "description": "3 to 5 short bullet points, each with concrete numbers from your outputs."}},
        "required": ["findings"]}}}

SYSTEM_PROMPT = (
    "You are a financial analyst producing a finished business report inside a Linux sandbox. "
    "The dataset is data.csv in the working directory. Python 3 with pandas, matplotlib, openpyxl and fpdf2 "
    "is installed; there is no internet access, so do not install anything. "
    "Write every piece of code to a .py file with fs_write and run it with exec "
    '(program "python3", args ["<file>.py"]); never pass code with python3 -c. '
    "Keep each script short and plain, without comments, and replace a broken script whole rather than "
    "appending to it. Run every script with exec right after you write it, and only rewrite it after a run "
    "has shown what is wrong. Build the report with two scripts:\n"
    "1. build_xlsx.py writes report.xlsx with openpyxl: a sheet named Data with every row of data.csv, and a "
    "sheet named Summary whose totals are Excel formulas over the Data sheet, such as "
    "=SUMIFS(Data!E:E,Data!B:B,A2).\n"
    "2. build_pdf.py writes report.pdf with fpdf2: a title, a summary paragraph with concrete numbers computed "
    "from the data, at least one matplotlib chart saved as PNG and placed with pdf.image(), and a table of the "
    "key figures. Use the built-in Helvetica font and plain ASCII text only (write INR, not the rupee sign), and "
    "pdf.multi_cell(0, ...) for paragraphs. fpdf2 is version 2.8: pass text= (not txt=) and "
    "new_x=XPos.LMARGIN, new_y=YPos.NEXT (not ln=True), from fpdf.enums. Label chart axes in INR millions, "
    "never with a scientific 1e7 offset, and keep each heading on the same page as its chart or table. "
    "build_pdf.py must also print the key figures it puts in the report.\n"
    "Never guess a number: your findings must quote figures exactly as a script printed them. If a script "
    "fails, read the error, fix the script and run it again. Then call finish with your key findings."
)
BAD_ARGUMENTS = ("error: the arguments were not valid JSON ({}); check the brackets and quotes, and put code in "
                 "a file with fs_write instead of passing it inline")
NO_FINDINGS = "error: findings are empty; call finish with your key findings"
NOT_RUN = ("\nnote: {path} has not run since you last wrote it. Run it with exec (program \"python3\", "
           "args [\"{path}\"]) and fix what the run shows, instead of rewriting it again.")


class AgentFailed(Exception):
    """The agent loop ended without valid reports."""


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str, limit: int) -> str:
    """Keeps tool output small enough for the model's context: the head and the tail, where a traceback ends."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - 2 * half} chars] ...\n{text[-half:]}"


async def call_tool(session, name: str, args: dict, timeout: float | None = None) -> str:
    """Calls one MCP tool, waiting at most `timeout` seconds; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args, read_timeout_seconds=timeout)
    except Exception as e:  # e.g. a script that outlived the time left or the sandbox's per-call limit
        return f"error: {e}"
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        out = f"error: {out}"
    return _clip(out, MAX_FILE if name == "fs_read" else MAX_OUTPUT)


def _parse_args(raw: str | None) -> dict | str:
    """Parses tool-call arguments into a dict, or returns why they are not a JSON object."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return str(e)
    return args if isinstance(args, dict) else "not an object"


def _echo(call) -> dict:
    """Rebuilds a tool call for the history; the server rejects invalid JSON there, so a broken call is echoed as {}."""
    args = call.function.arguments if isinstance(_parse_args(call.function.arguments), dict) else "{}"
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": args}}


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log, cut to one short line."""
    if "program" in args:
        line = " ".join([str(args["program"]), *map(str, args.get("args") or [])]).split("\n")[0]
        return line if len(line) <= 80 else line[:77] + "..."
    return str(args.get("path") or "")


def _track_runs(unrun: set[str], name: str, args: dict) -> str:
    """Tracks scripts written but not yet run; returns a reminder when one is rewritten before it ever ran."""
    if name == "exec":
        command = " ".join([str(args.get("program") or ""), *map(str, args.get("args") or [])])
        unrun.difference_update({p for p in unrun if p.rsplit("/", 1)[-1] in command})
        return ""
    path = str(args.get("path") or "")
    if name != "fs_write" or not path.endswith(".py"):
        return ""
    if path in unrun:
        return NOT_RUN.format(path=path)
    unrun.add(path)
    return ""


async def _out_of_budget(check, reason: str, log) -> str:
    """Ends a run that hit a limit: keep the reports if they already pass the check, otherwise fail."""
    problem = await check()
    if problem:
        raise AgentFailed(f"{reason}: {problem}")
    log(f"   {reason}; keeping the reports the agent built")
    return f"({reason} before the agent wrote its findings)"


async def build_report(session, model_client, model: str, request: str, check, max_steps: int = 25,
                       deadline_s: float = 420, log=print) -> str:
    """Runs a bounded tool-calling loop until finish is called and `check()` finds no problem; returns the findings.

    `check` is an async callable returning None when both reports are valid, or a problem the model can fix.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]
    nudged = False
    unrun: set[str] = set()  # scripts written since they last ran
    start = time.monotonic()

    def time_left() -> float:
        """Seconds left in the budget; every model and tool call is bounded by it."""
        return deadline_s - (time.monotonic() - start)

    for step in range(1, max_steps + 1):
        left = time_left()
        if left <= 0:
            return await _out_of_budget(check, f"time limit of {deadline_s:.0f}s reached", log)
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8000), left)).choices[0].message
        except asyncio.TimeoutError:
            return await _out_of_budget(check, f"time limit of {deadline_s:.0f}s reached", log)
        calls = reply.tool_calls or []
        if not calls:
            # A text reply once the reports pass the check means the model considers the work done.
            problem = await check()
            if reply.content and not problem:
                return reply.content
            # One reminder covers models that describe the report instead of building it.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": f"Not done yet: {problem or 'findings are empty'}. "
                             "Please use the tools to build report.pdf and report.xlsx, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if isinstance(args, str):
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS.format(args)
            elif name == "finish":
                log(f"   step {step}: finish")
                findings = str(args.get("findings") or "").strip()
                problem = await check() if findings else None
                if findings and not problem:
                    return findings
                result = f"error: {problem}" if findings else NO_FINDINGS
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args, max(time_left(), 0.1))
                result += _track_runs(unrun, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return await _out_of_budget(check, f"step limit of {max_steps} reached", log)
