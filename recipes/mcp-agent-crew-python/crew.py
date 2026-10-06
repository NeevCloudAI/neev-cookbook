"""One MCP URL, a crew of isolated agents: a planner, a coder and a tester, each in its own sandbox with its own key."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent import AgentFailed, finish_tool, run_agent

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DEFAULT_TASK = "a slugify(text) function that turns any title into a URL slug, with its edge cases"
WORKSPACE_TOOLS = ("fs_write", "fs_read", "fs_list", "exec")
PLAN, CODE = "PLAN.md", ("solution.py", "test_solution.py")
UNITTEST = 'exec with program "python3" and args ["-m", "unittest", "-v"] (pytest is not installed)'
UNITTEST_FLAGS = {"-v", "--verbose", "-q", "--quiet", "-b", "--buffer", "-c", "--catch", "-f", "--failfast"}
SUMMARY = {"summary": {"type": "string", "description": "One sentence on what you did."}}


@dataclass(frozen=True)
class Role:
    """One crew member: its key variable, the MCP tools it may use, its finish report and its limits."""
    name: str
    key_env: str
    tools: tuple
    finish: dict
    required_files: tuple
    system: str
    max_steps: int
    deadline_s: float


ROLES = (
    Role("planner", "PLANNER_API_KEY", WORKSPACE_TOOLS, finish_tool("Call once PLAN.md is written.", SUMMARY), (PLAN,),
         "You are the planner in a crew of three agents. Work only through the tools, in your own Linux sandbox. "
         "Write PLAN.md in the workspace root: a short spec for one Python function that will live in solution.py, "
         "with its signature, the rules it follows, and at most 8 edge cases, each with an example input and the "
         "exact expected output. Check that every expected output follows your rules exactly; prefer plain ASCII "
         "examples. Keep it under 40 lines and write no code. Then call finish.", 10, 150),
    Role("coder", "CODER_API_KEY", WORKSPACE_TOOLS, finish_tool("Call once the code and tests pass.", SUMMARY), CODE,
         "You are the coder in a crew of three agents. Work only through the tools, in your own Linux sandbox. "
         "PLAN.md in the workspace root is the planner's spec: read it first. Implement it in solution.py with the "
         "Python standard library only (there is no internet). Write unittest tests in test_solution.py that import "
         f"from solution and test exactly the edge cases in the plan. Run them with {UNITTEST} and fix any failure. "
         "If an example in the plan contradicts its own rules, follow the rules and say so in your summary. "
         "Then call finish.", 20, 300),
    Role("tester", "TESTER_API_KEY", ("fs_read", "fs_list", "exec"),
         finish_tool("Call with your verdict after running the tests.", {
             "passed": {"type": "boolean", "description": "True only if your last test run passed."},
             "report": {"type": "string", "description": "Two sentences: what ran and the result."}}), (),
         "You are the tester in a crew of three agents. Work only through the tools, in your own Linux sandbox. "
         "PLAN.md, solution.py and test_solution.py were handed to you; do not change them. Run the tests with "
         f"{UNITTEST}. If a test fails, read the files to explain why. Then call finish with your verdict.", 10, 150),
)


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def agent_keys(environ) -> dict[str, tuple[str, str]]:
    """Maps each role to (variable, key): its own key variable if set, otherwise NEEV_API_KEY."""
    return {r.name: (r.key_env, environ[r.key_env]) if environ.get(r.key_env) else ("NEEV_API_KEY", environ["NEEV_API_KEY"])
            for r in ROLES}


def rejected_keys(keys: dict[str, tuple[str, str]], probe) -> list[str]:
    """Probes each per-agent key once (NEEV_API_KEY is checked by the first create) and names the rejected ones."""
    rejected = []
    for env, key in dict(keys.values()).items():
        if env == "NEEV_API_KEY":
            continue
        try:
            probe(key)
        except Exception as e:
            rejected.append(f"{env} was rejected: {type(e).__name__}: {e}")
    return rejected


def mcp_connect(key: str, sandbox_name: str):
    """Opens an MCP session on the sandbox MCP server, authenticated by `key` and bound to one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    headers = {"Authorization": f"Bearer {key}", "x-sandbox-name": sandbox_name}
    return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


async def _work(connect, key: str, sandbox, role: Role, model_client, model: str, task: str, log):
    """Opens the role's own MCP session on its sandbox and runs its agent loop; file checks use the script's SDK."""
    try:
        async with connect(key, sandbox.name) as session:
            return await run_agent(session, model_client, model, system=role.system, task=task, allowed=role.tools,
                                   finish=role.finish, required_files=role.required_files, exists=sandbox.files.exists,
                                   max_steps=role.max_steps, deadline_s=role.deadline_s, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _hand_over(source, target, paths, log) -> dict[str, str]:
    """Copies files between two crew sandboxes with the SDK, under the script's own key; returns what it copied."""
    copied = {path: source.files.read_text(path) for path in paths}
    for path, content in copied.items():
        target.files.write(path, content)
    log(f"   {', '.join(paths)}: {source.name} -> {target.name}")
    return copied


async def _crew(task: str, sandboxes: dict, model_client, model: str, connect, keys: dict, log) -> tuple[dict, dict]:
    """Runs planner -> coder -> tester in one event loop; returns each result and the files handed to the tester.

    One loop for all three because the async model client is bound to the loop it first ran in.
    """
    planner, coder, tester = ROLES
    results, handed = {}, {}
    for n, role, handoff in ((2, planner, None), (4, coder, ("planner", (PLAN,))), (6, tester, ("coder", (PLAN, *CODE)))):
        if handoff:
            log(f"{n - 1}. Handing files to the {role.name}...")
            handed = _hand_over(sandboxes[handoff[0]], sandboxes[role.name], handoff[1], log)
        log(f"{n}. The {role.name} ({model}) is working in {sandboxes[role.name].name}...")
        try:
            result = await _work(connect, keys[role.name][1], sandboxes[role.name], role, model_client, model, task, log)
        except AgentFailed as e:
            raise AgentFailed(f"The {role.name} did not finish: {e}") from None
        log(f"   {role.name}: {result.finish.get('summary') or result.finish.get('report', '')}")
        results[role.name] = result
    return results, handed


def _full_test_run(args: dict) -> bool:
    """True for `python3 -m unittest` with only output flags, which discovers every test rather than a chosen few."""
    argv = [str(a) for a in args.get("args") or []]
    return args.get("program") in ("python3", "python") and argv[:2] == ["-m", "unittest"] and all(
        a in UNITTEST_FLAGS for a in argv[2:])


def _verdict(result) -> tuple[bool, str]:
    """Judges the tester by its last full test run, not only its claim; returns (passed, the line to show)."""
    runs = [data for args, data in result.execs if _full_test_run(args)]
    if not runs or runs[-1] is None:
        return False, "the tester did not run the tests"
    last = runs[-1]
    tail = [l for l in (last.get("stderr", "") + last.get("stdout", "")).splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    shown = " ".join(tail) or "no unittest summary"
    if last.get("exit_code") != 0:
        return False, f"{shown} (the last test run exited {last.get('exit_code')})"
    if result.finish.get("passed") is not True:
        return False, f"{shown} (but the tester reported a failure)"
    return True, shown


def _read_trails(sandboxes: dict, sleep, attempts: int = 10) -> dict[str, list]:
    """Reads every sandbox's audit trail, oldest first, following each page.

    Records land a second or two after the call, so it rereads every 3s until two reads in a row agree.
    """
    previous = None
    for _ in range(attempts):
        trails = {}
        for role, sb in sandboxes.items():
            records, cursor = [], None
            while True:
                page = sb.audit(cursor=cursor, limit=100)
                records.extend(page.records)
                cursor = page.next_cursor
                if not cursor:
                    break
            trails[role] = list(reversed(records))
        counts = {role: len(r) for role, r in trails.items()}
        if counts == previous:
            return trails
        previous = counts
        sleep(3)
    return trails


def _credentials(trails: dict, keys: dict, script_key: str) -> dict[str, list[str]]:
    """Maps each credential id in the trails to who used it: the script and/or the agents.

    The coder's oldest record is the script's hand-over of PLAN.md, made before the coder connected,
    so it names the script's credential; every other record in an agent's own sandbox is that agent's.
    """
    script_cred = trails["coder"][0].caller_source if trails.get("coder") else None
    who: dict[str, list[str]] = {script_cred: ["script"]} if script_cred else {}
    for role, (_, key) in keys.items():
        cred = script_cred if key == script_key else next(
            (r.caller_source for r in trails.get(role, []) if r.caller_source != script_cred), None)
        if cred:
            who.setdefault(cred, []).append(role)
    return who


def _print_trails(trails: dict, sandboxes: dict, keys: dict, script_key: str, log) -> None:
    """Prints each sandbox's trail with the credential that acted, labelled by agent when it is unambiguous."""
    who = _credentials(trails, keys, script_key)
    for cred, names in who.items():
        log(f"   credential {cred} = {', '.join(names)}")
    if len(who) == 1:
        log("   One key ran everything, so every record shows the same credential. "
            "Set PLANNER_API_KEY, CODER_API_KEY and TESTER_API_KEY to tell the agents apart.")
    for role, records in trails.items():
        log(f"   Audit trail of {sandboxes[role].name}:")
        for r in records:
            names = who.get(r.caller_source, [])
            label = names[0] if len(names) == 1 else (r.caller_source or "unknown")[:8]
            what = r.command or r.target or "-"
            log(f"     {r.at:%H:%M:%S}  {label:<8}  {r.tool:<9} {what:<20} {r.outcome.value}")


def _save(out: Path, suffix: str, files: dict[str, str], log) -> None:
    """Writes the files the tester judged to out/<run id>/, so the crew's work outlives its sandboxes."""
    folder = out / suffix
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (folder / name).write_text(content)
    log(f"   Saved {', '.join(files)} to {folder}/")


def run(task: str, client, model_client, model: str, connect, keys: dict, script_key: str, log=print,
        sleep=time.sleep, out: Path | None = None) -> int:
    """Creates one sandbox per agent, runs the crew, prints the audit trails, and always deletes every sandbox.

    With `out`, a passing crew's plan, code and tests are saved there before the sandboxes go.
    """
    sandboxes: dict = {}
    try:
        suffix = secrets.token_hex(4)
        log("1. Creating one sandbox per agent (no internet access)...")
        for role in ROLES:
            sandboxes[role.name] = client.sandboxes.create(
                {"name": f"crew-{role.name}-{suffix}", "egress": {"mode": "deny_all"}})
        for role in ROLES:
            sandboxes[role.name].wait_until_ready(timeout_ms=300_000)
            log(f"   {role.name:<8} {sandboxes[role.name].name}  (key from {keys[role.name][0]})")
        results, handed = asyncio.run(_crew(task, sandboxes, model_client, model, connect, keys, log))
        passed, shown = _verdict(results["tester"])
        # exec can write files, so check the tester judged the code and tests it was given
        changed = [p for p, content in handed.items() if sandboxes["tester"].files.read_text(p) != content]
        if changed:
            passed, shown = False, f"{', '.join(changed)} changed in the tester's sandbox; {shown}"
        log(f"   Tests in {sandboxes['tester'].name}: {'passed' if passed else 'FAILED'}: {shown}")
        if passed and out is not None:
            _save(out, suffix, handed, log)
        log("7. Audit trails, oldest first (who acted, what, on which file):")
        _print_trails(_read_trails(sandboxes, sleep), sandboxes, keys, script_key, log)
        return 0 if passed else 1
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except AgentFailed as e:
        log(str(e))
        return 1
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        deleted = 0
        for sb in sandboxes.values():
            try:
                sb.delete()
                deleted += 1
            except Exception as e:  # keep deleting the others
                log(f"   Could not delete {sb.name}: {e}")
        if deleted:
            log(f"   Deleted {deleted} sandboxes.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment and every per-agent key, and runs the crew."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK, help="the small function the crew builds")
    parser.add_argument("--out", type=Path, default=Path("crew-output"),
                        help="where a passing crew's plan, code and tests are saved (default crew-output/)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    keys = agent_keys(os.environ)

    def probe(key):
        with NeevAI(api_key=key) as c:
            c.sandboxes.list(limit=1)

    rejected = rejected_keys(keys, probe)
    if rejected:
        print("\n".join(rejected), file=sys.stderr)
        return 2
    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    with NeevAI() as client:
        return run(args.task, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), mcp_connect, keys,
                   os.environ["NEEV_API_KEY"], out=args.out)


if __name__ == "__main__":
    sys.exit(main())
