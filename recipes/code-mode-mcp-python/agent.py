"""Agent loop for the code-mode recipe: the same question, answered with plain file tools or with run_python."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field

# The MCP tools each mode is given; code mode adds the local run_python. Lifecycle tools stay with the script.
MODES = {"tools": ("fs_list", "fs_read"), "code": ("fs_list",)}
MAX_OUTPUT = 4000      # a program's output is clipped: in code mode only the result should come back
MAX_LISTING = 100_000  # listings and reads are not clipped, so tool mode always sees every file whole
SNIPPET_DIR = ".code-mode"

RUN_PYTHON = {"type": "function", "function": {
    "name": "run_python",
    "description": ("Run a Python 3 program inside the sandbox, with the workspace root (where logs/ is) as the "
                    "working directory. Returns its stdout, stderr and exit code. Only the standard library is "
                    "available. Print just the result you need, not the raw data."),
    "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "The full program."}},
                   "required": ["code"]}}}

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once with the answer: the customers and their counts, highest first.",
    "parameters": {"type": "object", "required": ["top_customers"], "properties": {"top_customers": {
        "type": "array", "items": {"type": "object", "required": ["customer", "failed_payments"], "properties": {
            "customer": {"type": "string"}, "failed_payments": {"type": "integer"}}}}}}}}

SYSTEM_PROMPT = (
    "You answer questions about data that lives inside a Linux sandbox. Work only through the tools you are "
    "given, and be exact: the answer is checked. When you know the answer, call finish."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); send smaller arguments"
BAD_ANSWER = 'error: top_customers must be a list of {"customer": string, "failed_payments": integer}'


@dataclass
class Result:
    """How one mode did: its answer (or None), why it stopped, and what it cost."""
    answer: list[tuple[str, int]] | None = None
    stopped: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_bytes: int = 0  # tool output sent back to the model: the data that passed through its context
    seconds: float = 0.0
    programs: list[str] = field(default_factory=list)  # every program run_python ran, in order


async def openai_tools(session, mode: str) -> list[dict]:
    """Turns the server's tool list into OpenAI tools for one mode, plus run_python in code mode, plus finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in MODES[mode] if name in listed]
    return [*tools, *([RUN_PYTHON] if mode == "code" else []), FINISH]


def _clip(text: str, limit: int) -> str:
    """Keeps tool output within the limit, saying how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


async def call_tool(session, name: str, args: dict) -> str:
    """Calls one MCP tool and returns the text for the model; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a program that outlived the sandbox's per-call time limit
        return f"error: {e}"
    data = result.structured_content
    if name == "fs_read" and not result.is_error and isinstance(data, dict) and data.get("eof") is True:
        out = str(data.get("content", ""))  # the file as written: JSON-escaping it would inflate tool mode's tokens
    elif data is not None:
        out = json.dumps(data, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        out = f"error: {out}"
    return _clip(out, MAX_LISTING if name in ("fs_list", "fs_read") else MAX_OUTPUT)


async def run_python(session, code: str, n: int) -> str:
    """Runs the model's program in the sandbox over MCP: fs_write saves it, exec runs it with python3."""
    path = f"{SNIPPET_DIR}/snippet_{n}.py"
    wrote = await call_tool(session, "fs_write", {"path": path, "content": code})
    if wrote.startswith("error:"):
        return wrote
    return await call_tool(session, "exec", {"program": "python3", "args": [path]})


def parse_answer(args: dict) -> list[tuple[str, int]] | None:
    """Reads finish's top_customers as [(customer, count)], lower-cased and trimmed, or None if malformed."""
    rows = args.get("top_customers")
    if not isinstance(rows, list):
        return None
    try:
        return [(str(r["customer"]).strip().lower(), int(r["failed_payments"])) for r in rows]
    except (KeyError, TypeError, ValueError):
        return None


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


async def _loop(session, model_client, model: str, mode: str, question: str, max_steps: int,
                deadline_s: float, log, result: Result) -> None:
    """The bounded tool-calling loop; fills `result` as it goes and sets result.stopped when it ends."""
    tools = await openai_tools(session, mode)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    start, nudged = time.monotonic(), False
    timed_out = f"time limit of {deadline_s:.0f}s reached"
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            result.stopped = timed_out
            return
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            response = await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8000), left)
        except asyncio.TimeoutError:
            result.stopped = timed_out
            return
        except Exception as e:  # e.g. the history outgrew the model's context window
            result.stopped = f"model error: {e}"[:120]  # one line in the results
            return
        result.model_calls += 1
        if response.usage is not None:
            result.input_tokens += response.usage.prompt_tokens or 0
            result.output_tokens += response.usage.completion_tokens or 0
        reply = response.choices[0].message
        calls = reply.tool_calls or []
        if not calls:
            # One nudge covers models that state the answer in prose; a second prose reply ends the run.
            if nudged:
                result.stopped = "answered in text without calling finish"
                return
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Call finish with the answer."})
            continue
        log(f"   call {step}: " + ", ".join(f"{name} x{n}" if n > 1 else name
                                            for name, n in Counter(c.function.name for c in calls).items()))
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                out = BAD_ARGUMENTS
            elif name == "finish":
                result.answer = parse_answer(args)
                if result.answer is not None:
                    result.stopped = "answered"
                    return
                out = BAD_ANSWER
            elif name not in allowed:
                out = f"error: {name} is not one of your tools"
            else:
                result.tool_calls += 1
                left = deadline_s - (time.monotonic() - start)
                if name == "run_python":
                    result.programs.append(str(args.get("code", "")))
                    work = run_python(session, result.programs[-1], len(result.programs))
                else:
                    work = call_tool(session, name, args)
                try:
                    # Tool calls share the budget too, so a long program cannot stretch the run.
                    out = await asyncio.wait_for(work, max(left, 0.001))
                except asyncio.TimeoutError:
                    result.stopped = timed_out
                    return
                result.tool_bytes += len(out.encode())
            messages.append({"role": "tool", "tool_call_id": call.id, "content": out})
    result.stopped = f"step limit of {max_steps} reached"


async def run_agent(session, model_client, model: str, mode: str, question: str, max_steps: int = 10,
                    deadline_s: float = 120, log=print) -> Result:
    """Answers the question in one mode within a step limit and a wall-clock budget, and measures the run."""
    result, start = Result(), time.monotonic()
    try:
        await _loop(session, model_client, model, mode, question, max_steps, deadline_s, log, result)
    finally:
        result.seconds = time.monotonic() - start
    return result
