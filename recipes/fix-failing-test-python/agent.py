"""Agent loop for the fix-failing-test recipe: the model fixes the code through the sandbox's MCP tools."""
from __future__ import annotations

import asyncio
import json
import posixpath
import time
from collections.abc import Callable

# The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000
MAX_FILE = 30_000  # reads return whole source files, so the model never "repairs" a file it saw cut short

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once the tests pass. The tests are run again before this is accepted.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You fix bugs in a Python repository inside a Linux sandbox. Work only through the tools; the "
    "repository is the workspace root /workspace and exec runs there. For shell commands call exec "
    'with program "sh" and args ["-c", "<command>"]. There is no internet access and no package installs. '
    "Fix the source code so the tests pass. Change existing Python source files only: never edit, delete "
    "or add test files, do not touch non-Python files, and do not create new files. Act without asking questions. When the tests pass, "
    "call finish with a one-sentence summary of the bug and the fix."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"
READ_ONLY = "error: {path} is read-only (tests and non-Python files); fix the Python source code instead"
NUDGE = "The tests still fail. Please use the tools to fix the source code, then call finish.\n\n{failure}"
MAX_NUDGES = 2


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str, limit: int) -> str:
    """Keeps tool output small enough for the model's context, keeping the end where test summaries are."""
    if len(text) <= limit:
        return text
    return f"[truncated {len(text) - limit} chars] ...\n" + text[-limit:]


async def call_tool(session, name: str, args: dict) -> str:
    """Calls one MCP tool and returns the text for the model; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a command that outlived the sandbox's per-call time limit
        return _clip(f"error: {e}", MAX_OUTPUT)
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        out = f"error: {out}"
    return _clip(out, MAX_FILE if name == "fs_read" else MAX_OUTPUT)


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


def _workspace_path(path) -> str:
    """Normalises a path the model gave (absolute, ./ or ..) to one relative to the workspace root."""
    return posixpath.normpath(str(path)).removeprefix("/workspace/")


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        return " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    return str(args.get("path") or "")


async def _test_failure(session, test_cmd: str) -> str | None:
    """Runs the test command over MCP; returns None if it passed, else the error to show the model."""
    try:
        result = await session.call_tool("exec", {"program": "sh", "args": ["-c", test_cmd]})
    except Exception as e:
        return _clip(f"error: could not run the tests: {e}", MAX_OUTPUT)
    out = result.structured_content or {}
    if not result.is_error and out.get("exit_code") == 0:
        return None
    output = f"{out.get('stdout', '')}{out.get('stderr', '')}"
    return _clip(f"error: the tests still fail (exit code {out.get('exit_code')}); keep fixing the source code.\n{output}", MAX_OUTPUT)


async def fix_code(session, model_client, model: str, task: str, test_cmd: str, is_read_only: Callable[[str], bool],
                   max_steps: int = 30, deadline_s: float = 300, log=print) -> str:
    """Runs a bounded tool-calling loop until the model finishes with passing tests, and returns its summary.

    is_read_only(path) marks the files, existing or new, that fs_write refuses.
    Hitting the step or time limit ends the run normally: the script judges the outcome from the
    sandbox afterwards, not from what the agent says it did.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    nudges = 0
    start = time.monotonic()
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        try:
            if left <= 0:
                raise asyncio.TimeoutError
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8000), left)).choices[0].message
        except asyncio.TimeoutError:
            log(f"   time limit of {deadline_s:.0f}s reached")
            return f"time limit of {deadline_s:.0f}s reached"
        calls = reply.tool_calls or []
        if not calls:
            # A text reply ends the loop once the tests pass; otherwise remind the model, a bounded number of times.
            failure = await _test_failure(session, test_cmd)
            if failure is None or nudges == MAX_NUDGES:
                return reply.content or "done"
            nudges += 1
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": NUDGE.format(failure=failure)})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish (running the tests)")
                result = await _test_failure(session, test_cmd)
                if result is None:
                    return str(args.get("summary", ""))
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            elif name == "fs_write" and is_read_only(_workspace_path(args.get("path", ""))):
                # A courtesy to the model; the script still checks every read-only file byte for byte afterwards.
                log(f"   step {step}: fs_write {args.get('path')} (refused: read-only)")
                result = READ_ONLY.format(path=args.get("path"))
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    log(f"   step limit of {max_steps} reached")
    return f"step limit of {max_steps} reached"
