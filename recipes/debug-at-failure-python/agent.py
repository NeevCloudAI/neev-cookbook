"""Agent loop for the debug-at-failure recipe: the model investigates a fork of the failed sandbox over MCP."""
from __future__ import annotations

import json

# The only MCP tools the model is given: no write, process or lifecycle tools. exec still runs any command,
# but only inside the fork, so the failed job's own state stays as it was.
AGENT_TOOLS = ("fs_read", "fs_list", "exec")
MAX_OUTPUT = 6000
FINDINGS = ("record_id", "field", "value", "cause")

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Report the root cause once you have found it.",
    "parameters": {"type": "object", "properties": {
        "record_id": {"type": "string", "description": "The id of the record that broke the pipeline."},
        "field": {"type": "string", "description": "The field of that record that holds the bad value."},
        "value": {"type": "string", "description": "The bad value exactly as the pipeline holds it."},
        "cause": {"type": "string", "description": "One or two sentences: why this value made the stage fail."},
    }, "required": list(FINDINGS)}}}

SYSTEM_PROMPT = (
    "You are debugging a data pipeline that failed part-way. You are in an exact copy of its machine, taken "
    "at the moment it failed: the pipeline process is still running and still holds its batch and state in "
    "memory. It serves JSON on http://127.0.0.1:8080 (GET / lists the endpoints); query it with exec, "
    'program "curl", args ["-s", "<url>"]. Its code is pipeline.py in the workspace. Do not restart or '
    "change anything: investigate. Find the record that broke the pipeline and why, then call finish."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


class AgentFailed(Exception):
    """The agent loop ended without a diagnosis."""


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


async def diagnose(session, model_client, model: str, task: str = "", max_steps: int = 15,
                   log=print) -> dict[str, str]:
    """Runs a tool-calling loop of at most max_steps until the model calls finish with every finding.

    The caller bounds its wall-clock time, together with opening the session.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    nudged = False
    for step in range(1, max_steps + 1):
        reply = (await model_client.chat.completions.create(
            model=model, messages=messages, tools=tools, max_tokens=4000)).choices[0].message
        calls = reply.tool_calls or []
        if not calls:
            # One reminder covers models that explain the bug in prose instead of reporting it.
            if nudged:
                raise AgentFailed("the model kept answering without calling finish")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Keep investigating with the tools, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                missing = [k for k in FINDINGS if not str(args.get(k) or "").strip()]
                if not missing:
                    return {k: str(args[k]).strip() for k in FINDINGS}
                result = f"error: finish needs {', '.join(missing)}; find them, then call finish again"
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise AgentFailed(f"step limit of {max_steps} reached")
