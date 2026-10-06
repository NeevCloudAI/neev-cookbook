"""Agent loop for the eval-rollouts recipe: the model works on one task through one sandbox's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

# The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once the task is done.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You are working on a task inside a Linux sandbox. Work only through the tools. The workspace is "
    "/workspace and commands run there. There is no internet access and no package installs; python3 "
    "and curl are available. Check your work before you finish. When the task is done, call finish "
    "with a one-sentence summary."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off)"


@dataclass
class AgentRun:
    """What one agent run cost and how it ended: finished, or the limit it hit."""

    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0
    stop: str = "finished"


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


async def call_tool(session, name: str, args: dict, timeout_s: float | None = None) -> str:
    """Calls one MCP tool and returns the text for the model; failures and timeouts become errors the model can act on."""
    try:
        result = await session.call_tool(name, args, read_timeout_seconds=timeout_s)
    except Exception as e:  # e.g. a command that outlived the sandbox's per-call time limit
        return f"error: {e}"
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    return _clip(f"error: {out}" if result.is_error else out)


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
    """Summarises a call's target as one short line for the progress log."""
    target = " ".join([str(args["program"]), *map(str, args.get("args") or [])]) if "program" in args else str(args.get("path") or "")
    target = " ".join(target.split())
    return target if len(target) <= 80 else target[:77] + "..."


async def run_agent(session, model_client, model: str, task: str, max_steps: int = 15,
                    deadline_s: float = 180, log=print, clock=time.monotonic) -> AgentRun:
    """Runs a bounded tool-calling loop on one task; hitting a limit ends the run, it is still graded."""
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    run = AgentRun()
    start = clock()
    try:
        while True:
            left = deadline_s - (clock() - start)
            if run.steps >= max_steps or left <= 0:
                run.stop = f"step limit of {max_steps}" if run.steps >= max_steps else f"time limit of {deadline_s:.0f}s"
                return run
            run.steps += 1
            try:
                # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
                response = await asyncio.wait_for(model_client.chat.completions.create(
                    model=model, messages=messages, tools=tools, max_tokens=4000), left)
            except asyncio.TimeoutError:
                run.stop = f"time limit of {deadline_s:.0f}s"
                return run
            run.tokens += getattr(getattr(response, "usage", None), "total_tokens", 0) or 0
            reply = response.choices[0].message
            calls = reply.tool_calls or []
            if not calls:  # a text reply ends the rollout: the model stopped working, and is graded as it stands
                run.stop = "replied without a tool call"
                return run
            messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
            for call in calls:
                name, args = call.function.name, _parse_args(call.function.arguments)
                if args is None:
                    log(f"step {run.steps}: {name} (invalid arguments)")
                    result = BAD_ARGUMENTS
                elif name == "finish":
                    log(f"step {run.steps}: finish")
                    return run
                elif name not in allowed:
                    log(f"step {run.steps}: {name} (refused)")
                    result = f"error: {name} is not one of your tools"
                else:
                    log(f"step {run.steps}: {name} {_describe(args)}".rstrip())
                    # Like the model call, a tool call gets only what is left of the budget.
                    result = await call_tool(session, name, args, max(deadline_s - (clock() - start), 1))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    finally:
        run.seconds = round(clock() - start, 1)
