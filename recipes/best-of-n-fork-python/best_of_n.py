"""Best-of-N with fork: three agents race to fix the same bug, each in its own fork of one sandbox."""
from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import pathlib
import re
import secrets
import sys
import time
from dataclasses import dataclass

from neevai.errors import ConflictError

from agent import AgentFailed, fix_bug

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
FIXTURE_DIR = pathlib.Path(__file__).parent / "fixture"
PROJECT = "project"  # where the fixture lives in the sandbox, relative to the workspace root

# One agent per fork: same task and tools, a different temperature and approach.
STRATEGIES = (
    (0.2, "Make the smallest change that fixes the bug. Read the failing tests, then the code they call."),
    (0.7, "Run the tests first and reason from each failing assertion back to the line that causes it."),
    (1.0, "Reproduce the bug with a short `python3 -c` command before editing, then fix its root cause."),
)


@dataclass
class Attempt:
    """How one fork's agent did: outcome is passed, failed, error or cancelled."""
    fork: str
    temperature: float
    outcome: str
    seconds: float
    detail: str = ""
    diff: str = ""


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def load_fixture(root: pathlib.Path) -> dict[str, str]:
    """Reads the buggy package and its tests as {relative path: text}."""
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts}


def test_command(fixture: dict[str, str]) -> list[str]:
    """The script's own test run: only the shipped test modules, with the project unable to shadow the stdlib.

    -I keeps the project directory and PYTHON* variables off sys.path; '.' is appended after the stdlib, so a
    file in the project such as unittest.py cannot stand in for the stdlib's test runner.
    """
    modules = sorted(p[:-3].replace("/", ".") for p in fixture if p.startswith("tests/test_"))
    code = f"import sys, unittest; sys.path.append('.'); unittest.main(module=None, argv=['unittest', *{modules!r}])"
    return ["python3", "-I", "-c", code]


def tests_ran(output: str) -> int | None:
    """Reads the test count from unittest's "Ran N tests" line."""
    found = re.search(r"^Ran (\d+) tests? in", output, re.M)
    return int(found.group(1)) if found else None


def passed(exit_code: int, output: str, expected: int) -> bool:
    """True only for a clean exit that ran every expected test and reported a bare OK (no skips)."""
    return exit_code == 0 and tests_ran(output) == expected and re.search(r"^OK$", output, re.M) is not None


async def verify(session, fixture: dict[str, str], expected: int) -> tuple[bool, str]:
    """Puts the original tests back in the fork, runs them, and judges the result itself."""
    for path, content in fixture.items():
        if path.startswith("tests/"):
            result = await session.call_tool("fs_write", {"path": f"{PROJECT}/{path}", "content": content})
            if result.is_error:
                return False, f"could not restore {path}"
    argv = test_command(fixture)
    result = await session.call_tool("exec", {"program": argv[0], "args": argv[1:], "cwd": PROJECT})
    if result.is_error:  # e.g. the run outlived the sandbox's per-call time limit
        return False, "\n".join(getattr(block, "text", "") for block in result.content)
    out = result.structured_content or {}
    output = f"{out.get('stdout', '')}{out.get('stderr', '')}"
    return passed(out.get("exit_code", -1), output, expected), output[-3000:]


async def diff(session, fixture: dict[str, str]) -> str:
    """Unified diff of every package file in the fork against the fixture shipped with this recipe."""
    chunks = []
    for path, original in fixture.items():
        if path.startswith("tests/"):
            continue
        result = await session.call_tool("fs_read", {"path": f"{PROJECT}/{path}"})
        now = "" if result.is_error else (result.structured_content or {}).get("content", "")
        chunks += difflib.unified_diff(original.splitlines(True), now.splitlines(True), f"a/{path}", f"b/{path}")
    return "".join(chunks)


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


async def attempt(index: int, fork: str, temperature: float, hint: str, connect, model_client, model: str,
                  fixture: dict[str, str], expected: int, log, **limits) -> Attempt:
    """Runs one agent over an MCP session bound to its fork; any failure becomes this attempt's outcome."""
    start = time.monotonic()
    deadline_s = limits.get("deadline_s", 240)
    try:
        # The whole attempt shares the agent's budget, so a hung tool call or test run cannot outlast it.
        async with asyncio.timeout(deadline_s), connect(fork) as session:
            summary = await fix_bug(session, model_client, model, temperature, hint,
                                    lambda: verify(session, fixture, expected),
                                    log=lambda line: log(f"   [{index}] {line}"), **limits)
            try:
                changes = await diff(session, fixture)
            except Exception as e:  # the fix is already verified; a failed read must not cost the win
                changes = f"(could not read the changed files: {e})"
        return Attempt(fork, temperature, "passed", time.monotonic() - start, summary, changes)
    except TimeoutError:
        return Attempt(fork, temperature, "failed", time.monotonic() - start, f"time limit of {deadline_s:.0f}s reached")
    except Exception as e:  # cancellation and Ctrl+C are BaseExceptions and pass through
        cause = _root_cause(e)
        if isinstance(cause, AgentFailed):
            return Attempt(fork, temperature, "failed", time.monotonic() - start, str(cause))
        return Attempt(fork, temperature, "error", time.monotonic() - start, f"{type(cause).__name__}: {cause}")


async def race(forks: list[str], connect, model_client, model: str, fixture: dict[str, str], expected: int,
               log, **limits) -> tuple[list[Attempt], Attempt | None]:
    """Starts one agent per fork and returns as soon as one passes, cancelling the rest."""
    start = time.monotonic()
    tasks = [asyncio.create_task(attempt(i, fork, temperature, hint, connect, model_client, model, fixture, expected,
                                         log, **limits))
             for i, (fork, (temperature, hint)) in enumerate(zip(forks, STRATEGIES), 1)]
    pending, winner = set(tasks), None
    try:
        while pending and winner is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for a in sorted((t.result() for t in done), key=lambda a: a.seconds):
                log(f"   [{forks.index(a.fork) + 1}] {a.outcome} after {a.seconds:.0f}s: {a.detail}")
                winner = winner or (a if a.outcome == "passed" else None)
    finally:
        stopped = time.monotonic() - start
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results = [t.result() if t not in pending else Attempt(fork, temperature, "cancelled", stopped)
               for t, fork, (temperature, _) in zip(tasks, forks, STRATEGIES)]
    return results, winner


def fork(base, name: str, sleep, tries: int = 20):
    """Forks base, retrying while the platform is still snapshotting it for the previous fork (409)."""
    for n in range(1, tries + 1):
        try:
            return base.fork(name)
        except ConflictError:
            if n == tries:
                raise
            sleep(0.5)


def _lookup(client, name: str, log) -> list:
    """Finds the sandbox with exactly this name, if one exists, so it can be deleted too."""
    try:
        return [sb for sb in client.sandboxes.list(name=name, limit=100).items if sb.name == name]
    except Exception as e:
        log(f"   Could not check for a sandbox named {name}: {e}. Look for it in the console.")
        return []


def run(client, model_client, model: str, connect, fixture: dict[str, str], log=print, sleep=time.sleep,
        deadline_s: float = 240, max_steps: int = 20) -> int:
    """Uploads the fixture to a base sandbox, races one agent per fork, and always deletes every sandbox."""
    made = []  # every sandbox this run created, deleted in finally
    # A create or fork whose reply was lost may still have made the sandbox; finally looks this name up.
    asked = f"best-of-n-{secrets.token_hex(4)}"
    try:
        log("1. Creating the base sandbox (no internet access)...")
        base = client.sandboxes.create({"name": asked, "egress": {"mode": "deny_all"}})
        made.append(base)
        base.wait_until_ready(timeout_ms=300_000)
        log(f"2. Uploading the buggy package ({len(fixture)} files) to {base.name} and running its tests...")
        for path, content in fixture.items():
            base.files.write(f"{PROJECT}/{path}", content)
        result = base.exec(test_command(fixture), cwd=PROJECT)
        output = result.stdout + result.stderr
        expected = tests_ran(output)
        if not expected or passed(result.exit_code, output, expected):
            log(f"The base sandbox did not show the bug: expected the tests to fail, got: {output.strip()[-300:]}")
            return 1
        log(f"   {expected} tests ran: {output.strip().splitlines()[-1]}")
        log(f"3. Forking {base.name} {len(STRATEGIES)} times...")
        start = time.monotonic()
        forks = []
        for i in range(1, len(STRATEGIES) + 1):
            asked = f"{base.name}-{i}"
            forks.append(fork(base, asked, sleep))
            made.append(forks[-1])
        for f in forks:
            f.wait_until_ready(timeout_ms=300_000)
        log(f"   {len(forks)} forks ready in {time.monotonic() - start:.1f}s, each with the same files")
        log(f"4. Racing {len(forks)} agents on {model}, one per fork:")
        for i, (f, (temperature, hint)) in enumerate(zip(forks, STRATEGIES), 1):
            log(f"   [{i}] {f.name}  temperature {temperature}  {hint}")
        results, winner = asyncio.run(race([f.name for f in forks], connect, model_client, model, fixture, expected,
                                           log, deadline_s=deadline_s, max_steps=max_steps))
        log("5. Results:")
        for a in results:
            log(f"   {a.fork}  temperature {a.temperature}  {a.outcome} after {a.seconds:.0f}s")
        if winner is None:
            log("No fork passed the tests.")
            return 1
        log(f"Winner: {winner.fork} (tests verified by the script). Its change:")
        log(winner.diff.rstrip())
        return 0
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        if asked not in {sb.name for sb in made}:
            made += _lookup(client, asked, log)
        deleted = 0
        for sb in made:
            try:
                sb.delete()
                deleted += 1
            except (Exception, KeyboardInterrupt) as e:  # keep going: one failed delete must not leave the others
                log(f"   Could not delete {sb.name}: {e!r}. Delete it from the console.")
        if deleted:
            log(f"   Deleted {deleted} sandboxes (the base and its forks).")


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect, load_fixture(FIXTURE_DIR))


if __name__ == "__main__":
    sys.exit(main())
