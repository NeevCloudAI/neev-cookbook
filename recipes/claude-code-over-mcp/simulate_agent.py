"""Plays the coding agent over the sandbox MCP server with a NeevCloud model, then checks its work and audit trail."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import sys
import time

import verify

REQUIRED_ENV = (*verify.REQUIRED_ENV, "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
# The MCP tools a coding agent needs for this task; delete_sandbox and the rest stay with the script.
AGENT_TOOLS = ("create_sandbox", "get_sandbox", "fs_write", "fs_read", "fs_list", "exec")
MAX_OUTPUT = 4000
TEST_RUN = {"program": "node", "args": ["--test", "--test-reporter=tap"], "cwd": "/workspace/app"}
TEST_TIMEOUT_S = 120  # a test that never closes its server must not hang the run

# The same prompt the README asks you to paste into Claude Code, Cursor or Codex.
TASK = (
    "Create a sandbox, then in its app folder write a small Node.js HTTP server (server.js) that answers "
    "GET /health with the JSON {\"ok\":true}, and one test (server.test.js) that uses node:test to start "
    "the server on a free port and check that reply. Use only Node's built-in modules, no npm packages. "
    "Run the tests with `node --test` from the app folder and tell me the result."
)
SYSTEM_PROMPT = (
    "You are a coding agent. Your tools act on a remote Linux sandbox through the NeevCloud sandbox MCP "
    "server, not on this machine. File paths are relative to the workspace, /workspace. exec runs one "
    "program with its args, not a shell line: use program \"sh\" with args [\"-c\", \"...\"] for shell "
    "syntax. The sandbox has no internet access. When the task is done, call finish with a one-line result."
)
FINISH = {"type": "function", "function": {
    "name": "finish", "description": "Call once the task is done.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}
BAD_ARGUMENTS = "error: the arguments were not valid JSON (the output may have been cut off); try again"


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox name."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    @contextlib.asynccontextmanager
    async def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        # The MCP client does not close an HTTP client it was handed, so this closes it.
        async with create_mcp_http_client(headers=headers) as http:
            async with Client(streamable_http_client(MCP_URL, http_client=http)) as session:
                yield session

    return connect


async def openai_tools(session) -> list[dict]:
    """Turns the server's tool list into OpenAI tools, keeping only AGENT_TOOLS, and adds finish."""
    listed = {t.name: t for t in (await session.list_tools()).tools}
    tools = [{"type": "function", "function": {
        "name": name, "description": listed[name].description or "", "parameters": listed[name].input_schema}}
        for name in AGENT_TOOLS if name in listed]
    return [*tools, FINISH]


async def call_tool(session, name: str, args: dict, timeout: float) -> str:
    """Calls one MCP tool within the time left and returns text for the model; failures become errors it can act on."""
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout)
    except Exception as e:  # e.g. a command that outlived the per-call or overall time limit
        return f"error: {type(e).__name__}: {e}"
    if result.structured_content is not None:
        out = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        out = "\n".join(getattr(block, "text", "") for block in result.content)
    if result.is_error:
        out = f"error: {out}"
    return out if len(out) <= MAX_OUTPUT else out[:MAX_OUTPUT] + f"\n... [truncated {len(out) - MAX_OUTPUT} chars]"


def _parse_args(raw: str | None) -> dict | None:
    """Parses tool-call arguments, or returns None if they are not a JSON object."""
    if (raw or "").strip() in ("", "{", "null"):  # some models send a lone "{" for a tool with no arguments
        return {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def _echo(call) -> dict:
    """Rebuilds a tool call for the history; the model API rejects invalid JSON there, so a broken call is echoed as {}."""
    args = _parse_args(call.function.arguments)
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": json.dumps(args or {})}}


def _describe(args: dict) -> str:
    """Summarises a call's target for the progress log."""
    if "program" in args:
        line = " ".join([str(args["program"]), *map(str, args.get("args") or [])])
        return line if len(line) <= 80 else line[:77] + "..."
    return str(args.get("path") or "")


async def work(session, model_client, model: str, task: str, sandbox_name: str, max_steps: int = 20,
               deadline_s: float = 300, log=print) -> str:
    """Runs a bounded tool-calling loop until the model finishes, stops calling tools, or hits a limit."""
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
            return "time limit reached"
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
            elif name == "create_sandbox" and args.get("name", sandbox_name) != sandbox_name:
                # Another name would create a second sandbox this connection cannot reach or clean up.
                log(f"   step {step}: create_sandbox {args['name']} (refused)")
                result = f"error: this connection works in {sandbox_name}; call create_sandbox without a name"
            else:
                log(f"   step {step}: {name} {_describe(args)}".rstrip())
                result = await call_tool(session, name, args, max(deadline_s - (time.monotonic() - start), 1))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    log(f"   step limit of {max_steps} reached")
    return "step limit reached"


async def check_tests(session) -> tuple[int, int, int | None]:
    """Runs the tests in the sandbox itself, never trusting the model's word; returns passed, failed, exit code."""
    result = await asyncio.wait_for(session.call_tool("exec", TEST_RUN), TEST_TIMEOUT_S)
    if result.is_error or result.structured_content is None:
        return 0, 0, None
    out = result.structured_content.get("stdout", "")
    count = lambda word: int(m.group(1)) if (m := re.search(rf"^# {word} (\d+)", out, re.M)) else 0
    return count("pass"), count("fail"), result.structured_content.get("exit_code")


async def _session(connect, sandbox_name: str, model_client, model: str, log) -> tuple[int, int, int | None]:
    """Opens the MCP session, lets the agent work, then checks its tests over the same connection."""
    try:
        async with connect(sandbox_name) as session:
            summary = await work(session, model_client, model, TASK, sandbox_name, log=log)
            log(f"2. Agent says: {' '.join(summary.split())[:200]}")
            return await check_tests(session)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def _attempt(client, model_client, model: str, connect, name: str, log, sleep) -> int:
    """Lets the agent work, then requires passing tests and a trail showing the agent's file writes."""
    from neevai import NotFoundError

    log(f"1. Connecting a {model} coding agent to the sandbox MCP server as {name} (no sandbox exists yet)")
    passed, failed, exit_code = asyncio.run(_session(connect, name, model_client, model, log))
    log(f"3. Ran node --test in the sandbox: {passed} passed, {failed} failed")
    try:
        sandbox = client.sandboxes.get(name)
    except NotFoundError:
        log(f"There is no sandbox named {name}: the agent did not call create_sandbox, or the call failed.")
        return 1
    log("4. What the agent did, from the sandbox's audit trail:")
    records, _ = verify.settled_trail(sandbox, sleep=sleep)
    for line in verify.timeline(records) + [""] + verify.summary(records):
        log(f"   {line}")
    # Only the agent writes files; the script's own test run shares its key, so an exec proves nothing.
    if not any(r.tool == "fs.write" for r in records):
        log("The audit trail shows no file writes.")
        return 1
    if exit_code != 0 or passed < 1 or failed:
        log("The tests did not pass.")
        return 1
    return 0


def _delete(client, name: str, log) -> bool:
    """Deletes the sandbox by name if it exists; False only when one may have been left behind."""
    from neevai import NotFoundError

    try:
        client.sandboxes.get(name).delete()
        log("   Sandbox deleted.")
    except NotFoundError:
        pass
    except Exception as e:
        log(f"Could not delete {name}: {type(e).__name__}: {e}. Delete it from the console.")
        return False
    return True


def run(client, model_client, model: str, connect, name: str | None = None, log=print, sleep=time.sleep) -> int:
    """Runs one simulated coding-agent session and always deletes the sandbox it created, even on Ctrl+C."""
    name = name or f"coding-agent-{secrets.token_hex(4)}"
    code = 1
    try:
        code = _attempt(client, model_client, model, connect, name, log, sleep)
    except KeyboardInterrupt:
        log("Interrupted.")
        code = 130
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {str(cause).splitlines()[0] if str(cause) else ''}".rstrip())
    finally:
        deleted = _delete(client, name, log)
    return code if deleted else 1


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the simulated session."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    missing = verify.missing_env(os.environ, REQUIRED_ENV)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    with NeevAI() as client:
        return run(client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), mcp_connect(os.environ["NEEV_API_KEY"]))


if __name__ == "__main__":
    sys.exit(main())
