"""Agent loop for the live egress approval recipe: the model works offline and asks a human for each host it needs."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

# The only MCP tools the model is given; lifecycle and egress stay with the script, never the agent.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000

REQUEST_EGRESS = {"type": "function", "function": {
    "name": "request_egress",
    "description": "Ask a human to let the sandbox reach one internet host. Returns approved or denied.",
    "parameters": {"type": "object", "properties": {
        "host": {"type": "string", "description": "Exact hostname, e.g. api.github.com (no scheme, path or wildcard)."},
        "reason": {"type": "string", "description": "One line: why the task needs this host."}},
        "required": ["host", "reason"]}}}
FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once with your answer when the task is done.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You work inside a Linux sandbox that starts with no internet access. Before anything that uses the network "
    "(curl, git clone, package installs: pip needs pypi.org and files.pythonhosted.org), call request_egress for "
    "each host, with its exact hostname and a one-line reason; a human decides. If approved, the host "
    "is reachable at once. If denied, do not try to reach it: carry on without it and say in your answer what "
    "you could not do. Run commands with exec, which takes a program and args, not a shell line; give curl "
    "--max-time 15. When the task is done, call finish with your answer."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); send smaller arguments"


@dataclass
class AgentRun:
    """What the agent did: its closing summary, whether it called finish, and every exec it ran."""
    summary: str
    finished: bool = False
    exec_commands: list[str] = field(default_factory=list)


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds the two local tools."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, REQUEST_EGRESS, FINISH]


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


async def call_tool(session, name: str, args: dict) -> str:
    """Calls one MCP tool and returns the text for the model; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a curl to a blocked host that outlived the server's per-call limit
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
    """Summarises a call's target for the progress log and the exec record (program plus its args)."""
    if "program" in args:
        rest = args.get("args") or []
        return " ".join([str(args["program"]), *([rest] if isinstance(rest, str) else map(str, rest))])
    return str(args.get("path") or "")


async def run_agent(session, model_client, model: str, request: str, on_egress_request, max_steps: int = 16,
                    deadline_s: float = 180, log=print, clock=time.monotonic) -> AgentRun:
    """Runs a bounded tool-calling loop; request_egress goes to on_egress_request(host, reason), which returns the answer.

    Time spent inside on_egress_request (a person deciding) is not charged to the deadline.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]
    execs: list[str] = []
    nudged = False
    start = clock()

    def left() -> float:
        return deadline_s - (clock() - start)

    timed_out = f"time limit of {deadline_s:.0f}s reached"
    for step in range(1, max_steps + 1):
        if left() <= 0:
            return AgentRun(timed_out, False, execs)
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=4000), left())).choices[0].message
        except asyncio.TimeoutError:
            return AgentRun(timed_out, False, execs)
        calls = reply.tool_calls or []
        if not calls:
            # One nudge covers models that narrate before acting; a second text-only reply ends the run.
            if nudged:
                return AgentRun(reply.content or "done", False, execs)
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Use the tools (request_egress first for any host), then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                return AgentRun(str(args.get("summary", "")), True, execs)
            elif name == "request_egress":
                if not isinstance(args.get("host"), str) or not args["host"].strip():
                    result = "error: request_egress needs a host such as api.github.com"
                else:
                    log(f"   step {step}: request_egress {args['host']}")
                    asked = clock()
                    result = on_egress_request(args["host"], str(args.get("reason", "")))
                    start += clock() - asked  # the person's thinking time is not the agent's
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                if name == "exec":
                    execs.append(_describe(args))
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                try:
                    # Tool calls share the budget too: a curl to a blocked host would otherwise hang to the server's limit.
                    result = await asyncio.wait_for(call_tool(session, name, args), max(left(), 0.001))
                except asyncio.TimeoutError:
                    return AgentRun(timed_out, False, execs)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return AgentRun(f"step limit of {max_steps} reached", False, execs)
