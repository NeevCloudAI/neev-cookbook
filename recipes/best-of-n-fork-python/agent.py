"""Agent loop for the best-of-N recipe: the model fixes a bug through one fork's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time

# The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000
MAX_FILE = 30_000  # reads return whole source files, so the model never "repairs" a file it saw cut short

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once the tests pass. The tests are then run again to check.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You are fixing a bug in a small Python package inside a Linux sandbox. Work only through the tools. "
    "The project is in the `project` directory: the package is `project/scheduler`, its unittest suite is "
    "`project/tests`, and some tests fail. Run them with exec: program `python3`, args `[\"-m\", \"unittest\"]`, "
    "cwd `project`. Fix the package code so every test passes. Do not edit or add test files: the original "
    "tests are put back before your fix is checked. There is no internet access and nothing to install. "
    "When the tests pass, call finish with a one-sentence summary of the fix.\n\nStrategy: {hint}"
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


class AgentFailed(Exception):
    """The agent loop ended without passing tests."""


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str, limit: int) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


async def call_tool(session, name: str, args: dict) -> str:
    """Calls one MCP tool and returns the text for the model; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a command that outlived the sandbox's per-call time limit
        return f"error: {e}"
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


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        return " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    return str(args.get("path") or "")


async def fix_bug(session, model_client, model: str, temperature: float, hint: str, verify,
                  max_steps: int = 20, deadline_s: float = 240, log=print) -> str:
    """Runs a bounded tool-calling loop until verify() reports passing tests; the model's word is never enough.

    verify is an async callable returning (passed, test output); it runs when the model calls finish or stops
    calling tools, and a failure goes back to the model as the tool result.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(hint=hint)},
                {"role": "user", "content": "Find and fix the bug so that the test suite passes."}]
    nudged = False
    start = time.monotonic()
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            raise AgentFailed(f"time limit of {deadline_s:.0f}s reached")
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, temperature=temperature, max_tokens=4000),
                left)).choices[0].message
        except asyncio.TimeoutError:
            raise AgentFailed(f"time limit of {deadline_s:.0f}s reached") from None
        calls = reply.tool_calls or []
        if not calls:
            # A text reply means the model thinks it is done: take it only if the tests agree.
            passed, _ = await verify()
            if passed:
                return reply.content or "done"
            # One reminder covers models that describe the fix instead of making it.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "The tests still fail. Please use the tools to fix the code, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                passed, output = await verify()
                log(f"step {step}: finish ({'tests pass' if passed else 'tests still fail'})")
                if passed:
                    return str(args.get("summary", ""))
                result = _clip(f"error: the tests still fail:\n{output}", MAX_OUTPUT)
            elif name not in allowed:
                log(f"step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise AgentFailed(f"step limit of {max_steps} reached")
