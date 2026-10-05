"""One crew member's loop: a model works in its own sandbox through an allowlist of the server's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

MAX_OUTPUT = 4000
MAX_FILE = 30_000  # reads return whole source files, so the model never "repairs" a file it saw cut short

BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); write smaller files"


class AgentFailed(Exception):
    """The agent loop ended without finishing its job."""


@dataclass
class AgentResult:
    """What the agent passed to finish, and every exec it ran with the server's structured result."""
    finish: dict
    execs: list[tuple[dict, dict | None]] = field(default_factory=list)


def finish_tool(description: str, properties: dict) -> dict:
    """Builds the local finish tool; every property is required, so the script can rely on the report."""
    return {"type": "function", "function": {"name": "finish", "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(properties)}}}


async def openai_tools(session, allowed, finish: dict) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only `allowed`, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in allowed if name in listed]
    return [*tools, finish]


def _clip(text: str, limit: int) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


async def call_tool(session, name: str, args: dict) -> tuple[str, dict | None]:
    """Calls one MCP tool; returns the text for the model and the structured result (None on failure)."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a command that outlived the sandbox's per-call time limit
        return f"error: {e}", None
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        return f"error: {_clip(out, MAX_OUTPUT)}", None
    return _clip(out, MAX_FILE if name == "fs_read" else MAX_OUTPUT), result.structured_content


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


def describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        return " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    return str(args.get("path") or "")


async def run_agent(session, model_client, model: str, *, system: str, task: str, allowed, finish: dict,
                    required_files=(), exists=None, max_steps: int = 20, deadline_s: float = 240,
                    log=print) -> AgentResult:
    """Runs a bounded tool-calling loop until the model calls finish with every required file written.

    `exists(path)` checks files outside the agent's session, so the check is never attributed to the agent.
    Hitting a limit hands over what was written if the required files exist (an agent with none fails),
    and a text reply counts as finish only for an agent that has required files and wrote them all.
    """
    tools = await openai_tools(session, allowed, finish)
    offered = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    execs: list[tuple[dict, dict | None]] = []
    nudged = False
    start = time.monotonic()

    def missing() -> list[str]:
        """Returns the required files that do not exist yet."""
        return [path for path in required_files if not exists(path)]

    def out_of_budget(reason: str) -> AgentResult:
        """Ends a run that hit a limit: hand over the required files if they all exist, otherwise fail."""
        if not required_files or missing():
            raise AgentFailed(f"{reason} before it finished")
        log(f"   {reason}; handing over what it wrote")
        return AgentResult({"summary": f"{reason}; handed over what it wrote"}, execs)

    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            return out_of_budget(f"time limit of {deadline_s:.0f}s reached")
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8000), left)).choices[0].message
        except asyncio.TimeoutError:
            return out_of_budget(f"time limit of {deadline_s:.0f}s reached")
        calls = reply.tool_calls or []
        if not calls:
            if required_files and not missing():
                return AgentResult({"summary": reply.content or "done"}, execs)
            # One reminder covers models that describe the work instead of doing it.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Please use the tools to do the work, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                absent = missing()
                if not absent:
                    return AgentResult(args, execs)
                result = f"error: {', '.join(absent)} missing from the workspace root; write it, then call finish"
            elif name not in offered:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {describe(args)}".rstrip())
                result, data = await call_tool(session, name, args)
                if name == "exec":
                    execs.append((args, data))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return out_of_budget(f"step limit of {max_steps} reached")
