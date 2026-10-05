"""Agent loop for the mcp-agent-undo recipe: the agent guards its own risky steps with the server's snapshot tools."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

# The only MCP tools the model is given: the workspace tools plus the server's own undo button.
AGENT_TOOLS = ("fs_write", "fs_read", "fs_list", "exec", "create_snapshot", "list_snapshots", "rollback_sandbox")
MAX_OUTPUT = 4000
VERDICTS = ("safe", "unsafe")

FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once you are done, with your verdict on the change you were asked to make.",
    "parameters": {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS), "description": "safe if it was applied and the tests pass, unsafe if you rolled it back"},
        "summary": {"type": "string", "description": "two sentences: what you did and why"}},
        "required": ["verdict", "summary"]}}}

SYSTEM_PROMPT = (
    "You are a careful database maintenance agent working inside a Linux sandbox. Work only through the tools; "
    "the workspace root is /workspace and exec runs there. For shell commands call exec with program "
    '"sh" and args ["-c", "<command>"]. Act without asking questions.\n\n'
    "Safety rule, which you must follow every time:\n"
    "1. Before any destructive or risky step (a database migration, deleting files or data, upgrading packages), "
    "call create_snapshot. Then call list_snapshots until that snapshot's status is Ready. Do not run the risky "
    "step before the snapshot is Ready.\n"
    "2. Run the risky step, then run the test suite.\n"
    "3. If the step fails or any test fails, do not try to fix it forward: call rollback_sandbox with the id of "
    "your Ready snapshot, then run the test suite again to confirm everything is back.\n"
    "4. Call finish with verdict unsafe if you rolled back, or safe if the change is applied and the tests pass."
)
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


@dataclass(frozen=True)
class ToolCall:
    """One MCP call the agent made, as the script later audits it."""
    name: str
    args: dict
    ok: bool
    output: str

    def data(self) -> dict:
        """The result as a JSON object, or {} if it failed or is not one."""
        return _json(self.output) if self.ok else {}

    def snapshot_id(self) -> str | None:
        """The snapshot id a successful create_snapshot returned, or None."""
        return self.data().get("id") if self.name == "create_snapshot" else None


@dataclass
class AgentRun:
    """What the agent decided ("safe", "unsafe" or "none"), in its own words, and every MCP call it made."""
    verdict: str
    summary: str
    calls: list[ToolCall] = field(default_factory=list)


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


def _json(text: str) -> dict:
    """Parses a tool result as a JSON object, or returns {} for anything else."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def call_tool(session, name: str, args: dict) -> str:
    """Calls one MCP tool and returns its full text; failures become errors the model can act on."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:  # e.g. a command that outlived the sandbox's per-call time limit
        return f"error: {e}"
    # The snapshot tools answer with JSON in a text block rather than structured content.
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        out = f"error: {out}"
    return out


def _parse_args(raw: str | None) -> dict | None:
    """Parses tool-call arguments, or returns None if they are not a JSON object.

    A bare "{" counts as no arguments: glm-4-7 sends it for tools whose arguments are all optional.
    """
    if (raw or "").strip() == "{":
        return {}
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def _echo(call) -> dict:
    """Rebuilds a tool call for the history as valid JSON; the server rejects anything else, so a broken call is echoed as {}."""
    args = json.dumps(_parse_args(call.function.arguments) or {})
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": args}}


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log, on one line of at most 120 characters."""
    if "program" in args:
        text = " ".join([str(args["program"]), *map(str, args.get("args") or [])])
    else:
        text = str(args.get("path") or args.get("name") or args.get("snapshot_id") or "")
    text = " ".join(text.split())
    return text if len(text) <= 120 else text[:117] + "..."


def _outcome(call: ToolCall) -> str:
    """Summarises a call's result for the progress log: exit codes and snapshot states, the parts worth watching."""
    if not call.ok:
        return " -> " + _clip(call.output.splitlines()[0] if call.output else "error", 160)
    data = call.data()
    if call.name == "exec":
        return f" -> exit {data.get('exit_code')}"
    if call.name == "create_snapshot":
        return f" -> snapshot {str(data.get('id'))[:8]} {data.get('status')}"
    if call.name == "list_snapshots":
        items = [i for i in data.get("items") or [] if isinstance(i, dict)]
        return " -> " + (", ".join(f"{str(i.get('id'))[:8]} {i.get('status')}" for i in items) or "none")
    if call.name == "rollback_sandbox":
        return " -> done"
    return ""


async def run_agent(session, model_client, model: str, task: str, max_steps: int = 20,
                    deadline_s: float = 240, log=print) -> AgentRun:
    """Runs a bounded tool-calling loop on the task and returns the verdict plus every MCP call made.

    Hitting the step or time limit ends the run normally with verdict "none": the script judges the
    outcome from the sandbox and the recorded calls afterwards, not from what the agent says it did.
    """
    tools = await openai_tools(session)
    allowed = {t["function"]["name"] for t in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    calls: list[ToolCall] = []
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
            return AgentRun("none", f"time limit of {deadline_s:.0f}s reached", calls)
        tool_calls = reply.tool_calls or []
        if not tool_calls:
            return AgentRun("none", reply.content or "", calls)
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [_echo(c) for c in tool_calls]})
        for tc in tool_calls:
            name, args = tc.function.name, _parse_args(tc.function.arguments)
            if args is None:
                log(f"   step {step}: {name} (invalid arguments)")
                result = BAD_ARGUMENTS
            elif name == "finish":
                verdict = args.get("verdict") if args.get("verdict") in VERDICTS else "none"
                log(f"   step {step}: finish: {verdict}")
                return AgentRun(verdict, str(args.get("summary", "")), calls)
            elif name not in allowed:
                log(f"   step {step}: {name} (refused)")
                result = f"error: {name} is not one of your tools"
            else:
                left = deadline_s - (time.monotonic() - start)
                try:
                    # Tool calls share the budget: a command that never returns must not hold the run open.
                    result = await asyncio.wait_for(call_tool(session, name, args), max(left, 0))
                except asyncio.TimeoutError:
                    log(f"   step {step}: {name} {_describe(args)} -> no answer before the time limit of {deadline_s:.0f}s")
                    return AgentRun("none", f"time limit of {deadline_s:.0f}s reached", calls)
                calls.append(call := ToolCall(name, args, not result.startswith("error:"), result))
                log(f"   step {step}: {name} {_describe(args)}".rstrip() + _outcome(call))
            # The review reads the full result; only the model's copy is clipped.
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _clip(result, MAX_OUTPUT)})
    log(f"   step limit of {max_steps} reached")
    return AgentRun("none", f"step limit of {max_steps} reached", calls)
