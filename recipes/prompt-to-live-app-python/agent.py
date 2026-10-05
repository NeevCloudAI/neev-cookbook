"""Agent loop for the prompt-to-live-app recipe: the model works through the sandbox's MCP tools."""
from __future__ import annotations

import asyncio
import json
import time

# The only MCP tools the model is given; lifecycle tools such as delete_sandbox stay with the script.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000
MAX_FILE = 30_000  # reads return whole app files, so the model never "repairs" a file it saw cut short

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once the app is complete.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}

SYSTEM_PROMPT = (
    "You are building a small web app inside a Linux sandbox. Work only through the tools. "
    "Write a complete single-page app as static files in the workspace root: index.html, plus "
    "styles.css and app.js if useful. There is no internet access and no package installs, so "
    "use plain HTML, CSS and JavaScript with no CDN links. Make it polished and fully working. "
    "Do not start a web server: the app is served for you after you finish. "
    "When the app is complete, call finish with a one-sentence summary."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); write smaller files"
NO_INDEX = "error: index.html is missing from the workspace root; write it, then call finish"


class AgentFailed(Exception):
    """The agent loop ended without a finished app."""


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


async def _has_index(session) -> bool:
    """Reports whether the app's entry page exists yet."""
    try:
        return not (await session.call_tool("fs_read", {"path": "index.html", "length": 1})).is_error
    except Exception:
        return False


async def _out_of_budget(session, reason: str, log) -> str:
    """Ends a run that hit a limit: serve what exists if index.html was written, otherwise fail."""
    if not await _has_index(session):
        raise AgentFailed(f"{reason} without an index.html")
    log(f"   {reason}; serving what the agent wrote")
    return f"{reason}; the app is what the agent wrote so far"


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        return " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    return str(args.get("path") or "")


async def build_app(session, model_client, model: str, request: str, max_steps: int = 25,
                    deadline_s: float = 240, log=print) -> str:
    """Runs a bounded tool-calling loop until the model calls finish with index.html written."""
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]
    nudged = False
    start = time.monotonic()
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            return await _out_of_budget(session, f"time limit of {deadline_s:.0f}s reached", log)
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=8000), left)).choices[0].message
        except asyncio.TimeoutError:
            return await _out_of_budget(session, f"time limit of {deadline_s:.0f}s reached", log)
        calls = reply.tool_calls or []
        if not calls:
            # A text reply once index.html exists means the model considers the app done.
            if await _has_index(session):
                return reply.content or "done"
            # One reminder covers models that describe the app instead of writing it.
            if nudged:
                raise AgentFailed("the model kept answering without using the tools")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Please use the tools to write the files, then call finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                if await _has_index(session):
                    return str(args.get("summary", ""))
                result = NO_INDEX
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return await _out_of_budget(session, f"step limit of {max_steps} reached", log)
