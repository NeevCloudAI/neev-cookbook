"""The AI tutor: a model that reviews the student's work through the sandbox's MCP tools and gives hints."""
from __future__ import annotations

import asyncio
import json
import time

# The only MCP tools the tutor is given: it can look and run, but has no tool to write files.
TUTOR_TOOLS = ("fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000
MAX_HINT = 400

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once with your hints for the student.",
    "parameters": {"type": "object", "properties": {"hints": {
        "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3,
        "description": "2 or 3 short hints, each one or two sentences, with no code."}}, "required": ["hints"]}}}

SYSTEM_PROMPT = (
    "You are a patient programming tutor. A student is working on an exercise in this Linux sandbox. "
    "EXERCISE.md describes the task, app.py is the student's code, and `python3 check.py` tests it. "
    "Use the tools to read their work and run the checks. Do not change any file and do not start servers. "
    "Then call finish with 2 or 3 short hints that lead the student to find and fix the problem "
    "themselves. Point at what to look at and why. Never give the corrected code, and never name the exact "
    "function call or argument that fixes it: the student learns by finding that part."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


class TutorFailed(Exception):
    """The review ended without hints."""


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only TUTOR_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in TUTOR_TOOLS if name in listed]
    return [*tools, FINISH]


def _clip(text: str) -> str:
    """Keeps tool output small enough for the model's context."""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} chars]"


async def call_tool(session, name: str, args: dict, timeout_s: float | None = None) -> str:
    """Calls one MCP tool within timeout_s and returns the text for the model; failures become errors it can act on."""
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout_s)
    except Exception as e:  # e.g. a command that outlived the review's time budget or the sandbox's per-call limit
        return f"error: {e or type(e).__name__}"
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


def check_hints(hints) -> tuple[list[str] | None, str]:
    """Validates finish's hints: (trimmed hints, "") when usable, else (None, why) for the model to fix.

    A code fence is refused so the tutor cannot hand over a rewritten solution as a "hint".
    """
    if not isinstance(hints, list) or not 2 <= len(hints) <= 3:
        return None, "error: give 2 or 3 hints as a list of strings"
    hints = [str(h).strip() for h in hints]
    if any(not h for h in hints):
        return None, "error: a hint is empty"
    if any("```" in h for h in hints):
        return None, "error: a hint contains code; describe what to look at instead of writing the fix"
    if any(len(h) > MAX_HINT for h in hints):
        return None, f"error: keep each hint under {MAX_HINT} characters"
    return hints, ""


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        return " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    return str(args.get("path") or "")


async def review(session, model_client, model: str, max_steps: int = 12, deadline_s: float = 150,
                 log=print) -> list[str]:
    """Runs a bounded tool-calling loop until the model calls finish with 2 or 3 usable hints.

    deadline_s bounds the whole review: listing tools, every model call and every tool call.
    """
    start = time.monotonic()
    try:
        tools = await asyncio.wait_for(openai_tools(session), deadline_s)
    except asyncio.TimeoutError:
        raise TutorFailed(f"time limit of {deadline_s:.0f}s reached") from None
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Please review my work on the exercise and give me hints."}]
    nudged = False
    for step in range(1, max_steps + 1):
        left = deadline_s - (time.monotonic() - start)
        if left <= 0:
            raise TutorFailed(f"time limit of {deadline_s:.0f}s reached")
        try:
            # The model call gets only what is left of the budget, so one slow reply cannot stretch the run.
            reply = (await asyncio.wait_for(model_client.chat.completions.create(
                model=model, messages=messages, tools=tools, max_tokens=4000), left)).choices[0].message
        except asyncio.TimeoutError:
            raise TutorFailed(f"time limit of {deadline_s:.0f}s reached") from None
        calls = reply.tool_calls or []
        if not calls:
            # One reminder covers models that answer in prose instead of calling finish.
            if nudged:
                raise TutorFailed("the model kept answering without calling finish")
            nudged = True
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append({"role": "user", "content": "Please give your hints by calling finish."})
            continue
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in calls]})
        for call in calls:
            name, args = call.function.name, _parse_args(call.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                log(f"   step {step}: finish")
                hints, result = check_hints(args.get("hints"))
                if hints:
                    return hints
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args, max(deadline_s - (time.monotonic() - start), 0.01))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise TutorFailed(f"step limit of {max_steps} reached")
