"""Agent loop for the human-review-gate recipe: the model changes code through the sandbox's MCP tools."""
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
    "You are making a code change in a small Python project inside a Linux sandbox. Work only through "
    "the tools. Read files with fs_read and list directories with fs_list; use exec only to run programs "
    "(exec takes a program and an argument list, not a shell string). There is no internet access and no "
    "package installs. Keep the change focused on the task, and run the tests before you finish. Never "
    "copy a secret value (a password, key or token) into a file or an answer: name the variable instead. "
    "When you are done, call finish with a plain-text summary of at most five sentences."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


class AgentFailed(Exception):
    """The agent loop ended without doing any work in the sandbox."""


@dataclass
class Session:
    """What the agent reported, and how many tool calls it sent to the sandbox."""

    summary: str
    calls: int


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} chars]"


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
    return _clip(out)


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


def _out_of_budget(reason: str, calls: int, log) -> Session:
    """Ends a run that hit a limit: what the agent did so far still goes to review, if it did anything."""
    if calls == 0:
        raise AgentFailed(f"{reason} before the agent used the sandbox")
    log(f"   {reason}; reviewing what the agent did so far")
    return Session(f"{reason}; the agent did not call finish", calls)


async def run_agent(session, model_client, model: str, task: str, max_steps: int = 20,
                    deadline_s: float = 240, log=print) -> Session:
    """Runs a bounded tool-calling loop until the model calls finish, counting the calls that reached the sandbox."""
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    calls = 0
    acted = False  # whether the agent has written a file or run a program yet
    nudged = False
    start = time.monotonic()
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            return _out_of_budget(f"time limit of {deadline_s:.0f}s reached", calls, log)
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=4000), left)).choices[0].message
        except asyncio.TimeoutError:
            return _out_of_budget(f"time limit of {deadline_s:.0f}s reached", calls, log)
        tool_calls = reply.tool_calls or []
        if not tool_calls:
            # A text reply once the agent has changed or run something is its summary; after reads only,
            # it is usually a plan or a reply cut off mid-thought.
            if acted:
                return Session(reply.content or "done", calls)
            # One reminder covers models that describe the change instead of making it.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Please use the tools to make the change, run the tests, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in tool_calls]})
        for call in tool_calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                return Session(str(args.get("summary", "")), calls)
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                calls += 1
                acted = acted or name in ("fs_write", "exec")
                try:
                    # Tool calls share the budget too: a hung program cannot stretch the run past it.
                    result = await asyncio.wait_for(call_tool(session, name, args),
                                                    max(deadline_s - (time.monotonic() - start), 0))
                except asyncio.TimeoutError:
                    return _out_of_budget(f"time limit of {deadline_s:.0f}s reached", calls, log)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return _out_of_budget(f"step limit of {max_steps} reached", calls, log)
