"""Hosted coding agent: OpenCode fixes failing tests on a NeevCloud agent, and the script checks, audits and pauses it."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MODEL_HOST = "inference.ai.neevcloud.com"
MODEL_BASE_URL = f"https://{MODEL_HOST}/v1"
DEFAULT_MODEL = "glm-4-7"
TEMPLATE = "opencode"
PROJECT_DIR = Path(__file__).parent / "project"
PROJECT_FILES = ("slugify.js", "slugify.test.js")
TEST_COMMAND = ["node", "--test", "slugify.test.js"]
TASK = ("The tests in slugify.test.js fail. Fix slugify.js so that `node --test slugify.test.js` passes. "
        "Do not change the tests. Run the tests to check your fix.")
MAX_STEPS = 25  # OpenCode's own step limit for the run
AGENT_TIMEOUT_S = 300
POLL_S = 3
STATUS_TIMEOUT_S = 180
AUDIT_WAIT_S = 60
THINKING = re.compile(r"^\s*<think>.*?(</think>|$)", re.DOTALL)  # reasoning some models put before their answer
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
COUNT = re.compile(r"^(?:ℹ|#) (tests|pass) (\d+)", re.MULTILINE)


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def opencode_config(model: str) -> dict:
    """OpenCode settings: NeevCloud as an OpenAI-compatible provider, keyed from the environment, plus a step limit."""
    return {
        "provider": {"neevcloud": {
            "npm": "@ai-sdk/openai-compatible", "name": "NeevCloud",
            "options": {"baseURL": MODEL_BASE_URL, "apiKey": "{env:NEEV_MODEL_API_KEY}"},
            "models": {model: {"name": model}}}},
        "agent": {"build": {"steps": MAX_STEPS}},
    }


def mask_key_id(key_id: str | None) -> str:
    """Masks an API key ID entirely, since run logs can be public."""
    if not key_id:
        return "unknown"
    return re.sub(r"[0-9a-fA-F]", "x", key_id)


def collapse(rows: list[tuple]) -> list[tuple[tuple, int]]:
    """Merges runs of rows that differ only in their first field (the time) into (first row, count)."""
    out: list[tuple[tuple, int]] = []
    for row in rows:
        if out and out[-1][0][1:] == row[1:]:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((row, 1))
    return out


class LineBuffer:
    """Turns output chunks into whole lines without terminal colour codes; chunks may split a line anywhere."""

    def __init__(self):
        self.pending = ""

    def feed(self, chunk: str) -> list[str]:
        """Adds a chunk and returns the non-blank lines it completed."""
        *done, self.pending = (self.pending + chunk).split("\n")
        return [line for line in (ANSI.sub("", d).rstrip() for d in done) if line.strip()]

    def flush(self) -> list[str]:
        """Returns the last unterminated line, if any."""
        rest, self.pending = self.pending, ""
        return self.feed(rest + "\n") if rest else []


def run_tests(machine) -> tuple[bool, str]:
    """Runs the test suite in the agent's workspace: (all passed, "N of M tests pass")."""
    result = machine.exec(TEST_COMMAND, cwd="/workspace")
    counts = dict(COUNT.findall(result.stdout))
    summary = f"{counts['pass']} of {counts['tests']} tests pass" if {"pass", "tests"} <= counts.keys() \
        else f"exit code {result.exit_code}"
    return result.exit_code == 0, summary


def parse_event(line: str) -> dict | None:
    """Parses one line of `opencode run --format json` output, or returns None if it is not a JSON object."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def describe_event(line: str) -> str | None:
    """Renders one output line for the log: a step, the agent's words, or plain text as is; None to skip it."""
    event = parse_event(line)
    if event is None:
        return line  # plain text, such as an error message
    part = event.get("part") or {}
    if event.get("type") == "text":
        text = THINKING.sub("", part.get("text") or "").strip()
        return f"says: {text.splitlines()[0]}" if text else None
    if event.get("type") == "tool_use":
        state = part.get("state") or {}
        args = state.get("input") or {}
        what = str(args.get("command") or args.get("filePath") or args.get("pattern") or "").removeprefix("/workspace/")
        status = "" if state.get("status") == "completed" else f" ({state.get('status')})"
        return f"{part.get('tool')} {what}".rstrip() + status
    if event.get("type") == "error":
        error = event.get("error") or {}
        return f"error: {error.get('name', '')} {(error.get('data') or {}).get('message', '')}".rstrip()
    return None


def run_coding_agent(machine, model: str, model_key: str, log, sleep, clock) -> tuple[int | None, str]:
    """Runs `opencode run` as a background process, printing each step as it happens; returns (exit code, usage).

    Polling the process log keeps the run alive through long silent model calls. The model key goes only
    into this process's environment. Past AGENT_TIMEOUT_S the process is killed and the exit code is None.
    """
    process = machine.processes.start(["opencode", "run", "--format", "json", "-m", f"neevcloud/{model}", TASK],
                                      cwd="/workspace", env={"NEEV_MODEL_API_KEY": model_key})
    buffers = {"stdout": LineBuffer(), "stderr": LineBuffer()}
    usage = {"steps": 0, "input": 0, "output": 0}
    cursor, deadline = None, clock() + AGENT_TIMEOUT_S

    def show(stream: str, lines: list[str]) -> None:
        for line in lines:
            event = parse_event(line) if stream == "stdout" else None
            if event and event.get("type") == "step_finish":
                tokens = (event.get("part") or {}).get("tokens") or {}
                usage["steps"] += 1
                usage["input"] += tokens.get("input", 0)
                usage["output"] += tokens.get("output", 0)
            text = describe_event(line) if stream == "stdout" else line
            if text:
                log(f"   | {text[:160]}")

    exit_code = None
    while True:
        page = machine.processes.logs(process.id, cursor=cursor)
        cursor = page.cursor
        for entry in page.entries:
            show(entry.stream, buffers[entry.stream].feed(entry.data))
        if page.state == "exited":
            exit_code = machine.processes.get(process.id).exit_code
            break
        if clock() >= deadline:
            machine.processes.kill(process.id)
            log(f"   time limit of {AGENT_TIMEOUT_S}s reached; stopped the agent")
            break
        sleep(POLL_S)
    for stream, buffer in buffers.items():
        show(stream, buffer.flush())
    return exit_code, f"{usage['steps']} model steps, {usage['input']} tokens in, {usage['output']} out"


def one_line(error: Exception) -> str:
    """The first line of an error message, kept short: some carry a whole HTML error page."""
    return (str(error).splitlines() or [""])[0][:200]


def check(label: str, ok: bool, log) -> bool:
    """Prints one verification result."""
    log(f"   {'ok    ' if ok else 'FAILED'} {label}")
    return ok


def verify(machine, original_tests: str, log) -> bool:
    """Checks the agent's work ourselves: the tests are untouched and they now pass."""
    try:
        unchanged = machine.files.read_text("slugify.test.js") == original_tests
    except Exception:  # the agent deleted or moved the file
        unchanged = False
    ok_unchanged = check("the tests are unchanged", unchanged, log)
    passed, summary = run_tests(machine)
    return check(f"node --test: {summary}", passed, log) and ok_unchanged


def read_trail(agent, sleep, clock, execs: int) -> list:
    """Reads the agent's whole audit trail, oldest first, waiting up to AUDIT_WAIT_S for the last `execs` to land.

    Records arrive a few seconds after the call, so the wait ends once every exec this script made is there.
    """
    deadline = clock() + AUDIT_WAIT_S
    while True:
        records, cursor = [], None
        for _ in range(20):  # 20 pages is far more than one run makes
            page = agent.audit(cursor=cursor, limit=100)
            records += page.records
            cursor = page.next_cursor
            if not cursor:
                break
        if sum(r.tool == "exec" for r in records) >= execs or clock() >= deadline:
            return records[::-1]
        sleep(POLL_S)


def show_trail(records: list, log) -> None:
    """Prints the trail with repeated polls collapsed, and the API key IDs the calls were made under (masked)."""
    rows = [(r.at.strftime("%H:%M:%S"), r.tool, r.command or r.target or "", r.outcome.value) for r in records]
    for (at, tool, what, outcome), n in collapse(rows):
        log(f"   {at}  {tool:<14} {what[:40]:<40} {outcome}{f'  x{n}' if n > 1 else ''}")
    keys = sorted({mask_key_id(r.caller_source) for r in records})
    log(f"   {len(records)} records, made under API key {', '.join(keys)}")


def wait_for_status(agent, status: str, sleep, clock) -> None:
    """Polls until the agent reports `status`, or raises after STATUS_TIMEOUT_S.

    Also used after resume, where a status still reading Paused would make the SDK's wait_until_ready fail at once.
    """
    deadline = clock() + STATUS_TIMEOUT_S
    while agent.refresh().status != status:
        if clock() >= deadline:
            raise RuntimeError(f"agent {agent.name} did not reach {status} within {STATUS_TIMEOUT_S}s")
        sleep(2)


def show_fix(machine, out: Path | None, log) -> None:
    """Prints OpenCode's change to slugify.js as a diff and, with `out`, saves the fixed file there."""
    original = (PROJECT_DIR / "slugify.js").read_text()
    fixed = machine.files.read_text("slugify.js")
    log("   OpenCode's change:")
    for line in difflib.unified_diff(original.splitlines(), fixed.splitlines(), "a/slugify.js", "b/slugify.js", lineterm=""):
        log(f"   {line}")
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "slugify.js").write_text(fixed)
        log(f"   Saved the fixed file to {out / 'slugify.js'}")


def demo(agent, model: str, model_key: str, log, sleep, clock, out: Path | None = None) -> bool:
    """Has the agent fix the tests, verifies, audits, then pauses and resumes it; True only if every check passed."""
    machine = agent.sandbox()
    log("2. Uploading a small project whose tests fail...")
    for name in PROJECT_FILES:
        machine.files.write(name, (PROJECT_DIR / name).read_text())
    machine.files.write("opencode.json", json.dumps(opencode_config(model), indent=2))
    _, summary = run_tests(machine)
    log(f"   node --test: {summary}")

    log(f'3. Asking OpenCode ({model}) to: "{TASK}"')
    started = clock()
    exit_code, usage = run_coding_agent(machine, model, model_key, log, sleep, clock)
    ended = "stopped" if exit_code is None else f"exited with code {exit_code}"
    log(f"   OpenCode {ended} after {clock() - started:.0f}s: {usage}")

    log("4. Checking the agent's work ourselves...")
    fixed = verify(machine, (PROJECT_DIR / "slugify.test.js").read_text(), log)
    if fixed:
        show_fix(machine, out, log)

    log("5. Audit trail: every call this script made into the agent, oldest first")
    show_trail(read_trail(agent, sleep, clock, execs=2), log)  # the two test runs above
    if not fixed:
        log("The agent did not fix the code: see the checks in step 4.")
        return False

    log("6. Starting a long-running process, pausing the agent, then resuming it...")
    marker = machine.processes.start(["sleep", "3600"])
    started = clock()
    agent.pause()
    wait_for_status(agent, "Paused", sleep, clock)
    log(f"   Paused after {clock() - started:.0f}s")
    started = clock()
    agent.resume()
    wait_for_status(agent, "Ready", sleep, clock)
    log(f"   Ready again after {clock() - started:.0f}s")
    # A restarted machine would have lost every process started before the pause.
    state = machine.processes.get(marker.id).state
    carried_on = check(f"the process started before the pause is {state}: the agent carried on, no restart",
                       state == "running", log)
    passed, summary = run_tests(machine)
    if not (check(f"node --test after the resume: {summary}", passed, log) and carried_on):
        return False
    log("Task verified: OpenCode fixed slugify.js, the tests pass, and the agent kept its work through a pause.")
    return True


def run(client, model: str, model_key: str, log=print, sleep=time.sleep, clock=time.monotonic,
        out: Path | None = None) -> int:
    """Creates the agent, runs the demo on it, and always deletes it; returns 0 only when every check passed."""
    agent, code = None, 1
    try:
        log(f"1. Creating an agent from the {TEMPLATE} template (it can reach only the NeevCloud model API)...")
        started = clock()
        # The idle timeout pauses an agent that a killed script left behind.
        agent = client.agents.create({"name": f"hosted-agent-{secrets.token_hex(4)}", "agent_template": TEMPLATE,
                                      "idle_timeout_seconds": 600}, allow_egress=[MODEL_HOST])
        agent.wait_until_ready(timeout_ms=300_000)
        log(f"   {agent.name} Ready in {clock() - started:.0f}s")
        code = 0 if demo(agent, model, model_key, log, sleep, clock, out) else 1
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # e.g. a rejected key or quota: one line instead of a traceback
        log(f"Failed: {type(e).__name__}: {one_line(e)}")
    finally:
        if agent is not None:
            try:
                agent.delete()
                log("   Agent deleted.")
            except Exception as e:
                log(f"   could not delete agent {agent.name} ({type(e).__name__}: {one_line(e)}); delete it from the console")
                code = code or 1
    return code


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("hosted-agent-output"),
                        help="where the fixed slugify.js is saved once verified (default hosted-agent-output/)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(client, os.environ.get("MODEL", DEFAULT_MODEL), os.environ["NEEV_MODEL_API_KEY"], out=args.out)


if __name__ == "__main__":
    sys.exit(main())
