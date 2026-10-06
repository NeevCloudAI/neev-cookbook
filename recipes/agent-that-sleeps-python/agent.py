"""Agent loop for the agent-that-sleeps recipe: a triage agent works through the sandbox's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time

# The only MCP tools the model is given; pause, resume and delete stay with the script.
AGENT_TOOLS = ("fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once every message in the batch has a note on the desk.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You are a support triage agent working inside a Linux sandbox. Work only through the tools; the "
    "workspace root is /workspace and exec runs there. A program called the desk is already running and "
    "keeps your notes. For each customer message in the batch you are given, decide whether it is a bug, "
    "a feature request or a question, and record it with exec: program \"python3\", args "
    "[\"desk.py\", \"add\", \"<label>: <one-line summary>\"], where <label> is bug, feature or question. "
    "One note per message. Act without asking questions. When every message has a note, call finish "
    "with a one-sentence summary."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


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
    return _clip(out, MAX_OUTPUT)


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


async def triage(session, model_client, model: str, task: str, max_steps: int = 12,
                 deadline_s: float = 120, log=print) -> str:
    """Runs a bounded tool-calling loop on one batch and returns the agent's summary.

    Hitting the step or time limit ends the burst normally: the script judges the outcome by
    reading the desk afterwards, not from what the agent says it did.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    start = time.monotonic()
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        try:
            if left <= 0:
                raise asyncio.TimeoutError
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=4000), left)).choices[0].message
        except asyncio.TimeoutError:
            log(f"   time limit of {deadline_s:.0f}s reached")
            return f"time limit of {deadline_s:.0f}s reached"
        calls = reply.tool_calls or []
        if not calls:
            return reply.content or "done"
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                return str(args.get("summary", ""))
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    log(f"   step limit of {max_steps} reached")
    return f"step limit of {max_steps} reached"
