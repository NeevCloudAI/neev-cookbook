"""Agent loop for the AI data analyst recipe: the model analyses a CSV by running Python in the sandbox over MCP."""
from __future__ import annotations

import asyncio
import json
import time

# MCP tools the model gets as-is; it reads, writes and runs code only through run_python, never exec or fs_write directly.
AGENT_TOOLS = ("fs_list",)
SCRIPT = "analysis.py"
CHART = "chart.png"
MAX_OUTPUT = 6000

RUN_PYTHON = {"type": "function", "function": {
    "name": "run_python",
    "description": "Runs a Python 3 script in the workspace and returns its exit code, stdout and stderr. "
                   "Each call is a fresh process: variables do not carry over, so reload the data every time.",
    "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "The full Python script."}},
                   "required": ["code"]}}}
FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once chart.png is saved, with your findings.",
    "parameters": {"type": "object", "properties": {"findings": {
        "type": "string", "description": "3 to 5 short bullet points, each with concrete numbers from your outputs."}},
        "required": ["findings"]}}}

SYSTEM_PROMPT = (
    "You are a data analyst working in a Linux sandbox. The dataset is data.csv in the working directory. "
    "pandas and matplotlib are installed; do not install anything, there is no internet access. "
    "Use run_python to inspect the data first (columns, types, a few rows), then compute the answer with code. "
    "Never guess a number: every figure you report must come from a script's output. "
    "Save exactly one clear chart that answers the question to chart.png with matplotlib "
    "(a title, labelled axes, readable tick labels, plt.savefig('chart.png', dpi=150, bbox_inches='tight')). "
    "Then call finish with your findings."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); send a shorter script"
NO_CODE = "error: run_python needs a non-empty string argument named code"
NO_CHART = "error: chart.png is missing from the working directory; save the chart, then call finish"
NO_FINDINGS = "error: findings are empty; call finish with your findings"


class AgentFailed(Exception):
    """The agent loop ended without a chart and findings."""


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds run_python and finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, RUN_PYTHON, FINISH]


def _clip(text: str) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} chars]"


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
    return _clip(out)


async def run_python(session, code: str, timeout: float | None = None) -> str:
    """Writes the code to analysis.py with fs_write, then runs it with exec; MPLBACKEND=Agg since there is no display."""
    written = await call_tool(session, "fs_write", {"path": SCRIPT, "content": code}, timeout)
    if written.startswith("error:"):
        return written
    return await call_tool(session, "exec", {"program": "python3", "args": [SCRIPT], "env": ["MPLBACKEND=Agg"]}, timeout)


def _parse_args(raw: str | None) -> dict | None:
    """Parses tool-call arguments, or returns None if they are not a JSON object."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def _echo(call) -> dict:
    """Rebuilds a tool call for the history; the server rejects invalid JSON there, so a broken call is echoed as {}."""
    args = call.function.arguments if _parse_args(call.function.arguments) is not None else "{}"
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": args}}


async def _has_chart(session) -> bool:
    """Reports whether chart.png exists in the workspace yet."""
    try:
        return not (await session.call_tool("fs_read", {"path": CHART, "length": 1})).is_error
    except Exception:
        return False


async def _finish(session, args: dict) -> tuple[str | None, str]:
    """Checks a finish call: returns (findings, "") when it stands, or (None, error for the model)."""
    findings = str(args.get("findings") or "").strip()
    if not findings:
        return None, NO_FINDINGS
    if not await _has_chart(session):
        return None, NO_CHART
    return findings, ""


async def analyse(session, model_client, model: str, question: str, max_steps: int = 20,
                  deadline_s: float = 240, log=print) -> str:
    """Runs a bounded tool-calling loop until the model finishes with chart.png saved; returns the findings."""
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    nudged = False
    start = time.monotonic()

    def time_left() -> float:
        """Seconds left in the budget; every model and tool call is bounded by it."""
        return deadline_s - (time.monotonic() - start)

    for step in range(1, max_steps + 1):
        left = time_left()
        if left <= 0:
            raise AgentFailed(f"time limit of {deadline_s:.0f}s reached")
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=4000), left)).choices[0].message
        except asyncio.TimeoutError:
            raise AgentFailed(f"time limit of {deadline_s:.0f}s reached") from None
        calls = reply.tool_calls or []
        if not calls:
            # One reminder covers models that describe the analysis, or report it, without the tools.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Please use the tools to run the analysis and save chart.png, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                findings, result = await _finish(session, args)
                if findings:
                    return findings
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            elif name == "run_python":
                code = args.get("code")
                if not isinstance(code, str) or not code.strip():
                    log(f"   step {step}: run_python (no code)")
                    result = NO_CODE
                else:
                    log(f"   step {step}: run_python ({len(code.splitlines())} lines)")
                    result = await run_python(session, code, max(time_left(), 0.1))
            else:
                log(f"   step {step}: {name} {args.get('path') or ''}".rstrip())
                result = await call_tool(session, name, args, max(time_left(), 0.1))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise AgentFailed(f"step limit of {max_steps} reached")
