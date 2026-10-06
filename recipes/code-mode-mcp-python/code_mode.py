"""Code mode over MCP: one question about 200 log files, answered with file tools and then with run_python."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

import fixture
from agent import Result, run_agent

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
TOOL_MODE_STEPS, TOOL_MODE_SECONDS = 20, 180
CODE_MODE_STEPS, CODE_MODE_SECONDS = 10, 120
ARCHIVE = "logs.tar.gz"


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def upload_logs(sandbox, logs: fixture.Logs) -> None:
    """Puts the log files into the sandbox's logs/ as one archive write plus one tar run, then removes the archive."""
    sandbox.files.write(ARCHIVE, fixture.tarball(logs.files))
    unpacked = sandbox.exec("tar", ["-xzf", ARCHIVE])
    if unpacked.exit_code != 0:
        raise RuntimeError(f"unpacking the logs failed: {unpacked.stderr.strip()}")
    sandbox.files.remove(ARCHIVE)


async def _mode(connect, sandbox_name: str, model_client, model: str, mode: str, steps: int, seconds: float,
                log) -> Result:
    """Opens an MCP session bound to the sandbox and answers the question in one mode."""
    try:
        async with connect(sandbox_name) as session:
            return await run_agent(session, model_client, model, mode, fixture.QUESTION, steps, seconds, log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


async def _compare(connect, sandbox_name: str, model_client, model: str, log) -> tuple[Result, Result]:
    """Runs tool mode, then code mode, on the same sandbox."""
    log(f"3. Tool calls: {model} gets fs_list and fs_read "
        f"(at most {TOOL_MODE_STEPS} model calls, {TOOL_MODE_SECONDS}s)...")
    tools = await _safe_mode(connect, sandbox_name, model_client, model, "tools", TOOL_MODE_STEPS, TOOL_MODE_SECONDS, log)
    log(f"4. Code mode: {model} gets run_python and fs_list "
        f"(at most {CODE_MODE_STEPS} model calls, {CODE_MODE_SECONDS}s)...")
    code = await _safe_mode(connect, sandbox_name, model_client, model, "code", CODE_MODE_STEPS, CODE_MODE_SECONDS, log)
    return tools, code


async def _safe_mode(*args) -> Result:
    """Runs _mode, turning a failure such as a dropped MCP session into a result, so both modes are always reported."""
    try:
        return await _mode(*args)
    except Exception as e:
        return Result(stopped=f"failed: {type(e).__name__}: {e}")


def verdict(answer: list[tuple[str, int]] | None, truth: list[tuple[str, int]]) -> str:
    """Grades an answer against the ground truth: names and counts must match, in order."""
    if answer is None:
        return "no answer"
    return "correct" if answer == truth else "wrong"


def _top(answer: list[tuple[str, int]] | None) -> str:
    """Formats an answer as "customer count, ..." for the log."""
    return ", ".join(f"{c} {n}" for c, n in answer) if answer else "none"


def report(tools: Result, code: Result, truth: list[tuple[str, int]], log) -> None:
    """Prints the two modes side by side, then each answer next to the ground truth."""
    rows = [("model calls", lambda r: f"{r.model_calls}"),
            ("tool calls", lambda r: f"{r.tool_calls}"),
            ("input tokens", lambda r: f"{r.input_tokens:,}"),
            ("output tokens", lambda r: f"{r.output_tokens:,}"),
            ("data sent to model", lambda r: f"{r.tool_bytes / 1024:.1f} KB"),
            ("wall time", lambda r: f"{r.seconds:.1f}s"),
            ("answer", lambda r: verdict(r.answer, truth))]
    log(f"   {'':<20}{'tool calls':>14}{'code mode':>14}")
    for label, cell in rows:
        log(f"   {label:<20}{cell(tools):>14}{cell(code):>14}")
    log(f"   Ground truth:        {_top(truth)}")
    log(f"   Tool calls answered: {_top(tools.answer)} ({tools.stopped})")
    log(f"   Code mode answered:  {_top(code.answer)} ({code.stopped})")


def run(seed: int, client, model_client, model: str, connect, log=print, show_code: bool = False) -> int:
    """Creates a sandbox with the logs, compares the two modes over MCP, and always deletes the sandbox."""
    sandbox = None
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": f"code-mode-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        logs = fixture.make(seed)
        size_kb = sum(len(t.encode()) for t in logs.files.values()) / 1024
        log(f"2. Uploading {len(logs.files)} payment log files ({size_kb:.0f} KB, JSON and CSV, seed {seed})...")
        upload_logs(sandbox, logs)
        log(f"   Question: {fixture.QUESTION}")
        tools, code = asyncio.run(_compare(connect, sandbox.name, model_client, model, log))
        log("5. Results, same question and same files:")
        report(tools, code, logs.top, log)
        if show_code:
            for n, program in enumerate(code.programs, 1):
                log(f"   Program {n} the model ran in the sandbox:")
                log("\n".join(f"      {line}" for line in program.strip("\n").splitlines()))
        if verdict(code.answer, logs.top) != "correct":
            log("Code mode did not answer correctly.")
            return 1
        log(f"Code mode answered correctly with {code.model_calls} model calls and "
            f"{code.input_tokens + code.output_tokens:,} tokens; tool calls used {tools.model_calls} model calls "
            f"and {tools.input_tokens + tools.output_tokens:,} tokens.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        if sandbox is not None:
            try:
                sandbox.delete()
                log("   Sandbox deleted.")
            except Exception as e:  # never mask the result with a traceback; say what to clean up instead
                log(f"   Could not delete sandbox {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-code", action="store_true", help="print the programs the model ran in code mode")
    parser.add_argument("--seed", type=int, default=None, help="generate the same logs again (default: random)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    seed = args.seed if args.seed is not None else secrets.randbelow(1_000_000)
    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(seed, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect,
                   show_code=args.show_code)


if __name__ == "__main__":
    sys.exit(main())
