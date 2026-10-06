"""An agent that sleeps: work in bursts, pause the sandbox in between, and wake up with the same process and memory."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from agent import triage

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DESK = Path(__file__).parent / "app" / "desk.py"
IDLE_TIMEOUT_S = 300  # safety net if this script dies: the platform may pause the sandbox once idle this long
DESK_TIMEOUT_S = 30
PAUSE_TIMEOUT_S = 120
WAKE_TIMEOUT_S = 180
BATCHES = (
    "1. Since this morning I get a blank page after logging in on Firefox.\n"
    "2. Could you add an export to CSV on the reports page?\n"
    "3. How do I change the email address on my invoices?\n"
    "4. The mobile app crashes when I rotate the screen on the checkout page.\n",
    "1. Search takes over 20 seconds when I filter by date.\n"
    "2. Is there a way to invite my accountant with read-only access?\n"
    "3. Please support dark mode in the dashboard.\n"
    "4. Password reset emails never arrive for addresses at our domain.\n",
)


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


def read_desk(sandbox) -> dict:
    """Asks the running desk for its state: PID, boot ID, heartbeat ticks and notes."""
    result = sandbox.exec(["python3", "desk.py", "show"])
    if result.exit_code != 0:
        raise RuntimeError(f"the desk did not answer: {result.stderr.strip()[-200:]}")
    return json.loads(result.stdout)


def start_desk(sandbox, sleep, clock) -> tuple[object, dict]:
    """Uploads and starts the desk as a supervised process, then waits until it answers."""
    sandbox.files.write("desk.py", DESK.read_text())
    process = sandbox.processes.start(["python3", "desk.py"])
    deadline = clock() + DESK_TIMEOUT_S
    while True:
        try:
            return process, read_desk(sandbox)
        except (RuntimeError, ValueError):
            if clock() >= deadline:
                raise RuntimeError(f"the desk did not answer within {DESK_TIMEOUT_S}s") from None
            sleep(0.5)


def wait_paused(sandbox, sleep, clock) -> None:
    """Polls the sandbox record, never the sandbox itself (a call into it could wake it), until Paused with no replica."""
    start = clock()
    while not (sandbox.phase == "Paused" and sandbox.replicas == 0):
        if clock() - start >= PAUSE_TIMEOUT_S:
            raise RuntimeError(f"the sandbox did not pause within {PAUSE_TIMEOUT_S}s (phase {sandbox.phase})")
        sleep(0.5)
        sandbox.refresh()


def wait_awake(sandbox, sleep, clock) -> None:
    """Polls with a cheap exec until the sandbox runs commands again; the phase can lag behind, so it is not trusted."""
    start, last = clock(), "no answer"
    while True:
        try:
            if sandbox.exec(["true"], timeout_ms=10_000).exit_code == 0:
                return
        except Exception as e:  # refused or unreachable while it wakes; kept for the timeout message
            last = f"{type(e).__name__}: {e}"
        if clock() - start >= WAKE_TIMEOUT_S:
            raise RuntimeError(f"the sandbox did not wake within {WAKE_TIMEOUT_S}s (last error: {last})")
        sleep(0.5)


def prove_same_process(before: dict, after: dict, process_state: str, away_s: float, log) -> bool:
    """Checks the desk after waking against the desk before the pause and prints each result.

    A restarted desk would get a new boot ID, start its heartbeat from 0 and have no notes,
    since notes live only in the process's memory.
    """
    ticked = after["ticks"] - before["ticks"]
    checks = [
        (f"process: PID {after['pid']} (was {before['pid']}), boot ID {after['boot_id']} (was {before['boot_id']}), "
         f"desk {process_state}",
         after["pid"] == before["pid"] and after["boot_id"] == before["boot_id"] and process_state == "running"),
        (f"memory: {len(before['notes'])} notes from before the pause, still held in memory",
         after["notes"][:len(before["notes"])] == before["notes"] and len(before["notes"]) > 0),
        (f"heartbeat: {before['ticks']} -> {after['ticks']} ticks, {ticked} in the {away_s:.0f}s the script was away "
         "(it continued, and did not count while asleep)" if ticked * 2 < away_s else
         f"heartbeat: {before['ticks']} -> {after['ticks']} ticks over {away_s:.0f}s (it continued)",
         ticked >= 0),
    ]
    for label, ok in checks:
        log(f"   {'ok    ' if ok else 'FAILED'} {label}")
    return all(ok for _, ok in checks)


async def _triage(connect, sandbox_name: str, model_client, model: str, task: str, log) -> str:
    """Opens the MCP session for the sandbox and runs one burst of the triage agent over it."""
    try:
        async with connect(sandbox_name) as session:
            return await triage(session, model_client, model, task, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def burst(n: int, sandbox, connect, model_client, model: str, run_async, log) -> dict:
    """Drops batch n in the inbox, lets the agent triage it, and returns the desk; fails if no note was added."""
    path = f"inbox/batch-{n}.txt"
    sandbox.files.write(path, BATCHES[n - 1])
    notes_before = len(read_desk(sandbox)["notes"])
    summary = run_async(_triage(connect, sandbox.name, model_client, model,
                                  f"Triage the customer messages in {path}.", log))
    log(f"   agent's summary: {summary}")
    desk = read_desk(sandbox)
    if len(desk["notes"]) == notes_before:
        raise RuntimeError(f"the agent recorded no notes for batch {n}")
    for line in desk["notes"][notes_before:]:
        log(f"   note: {line}")
    return desk


def run(client, model_client, model: str, connect, nap_s: float = 30, log=print,
        sleep=time.sleep, clock=time.monotonic) -> int:
    """Runs two bursts with a pause between them and always deletes the sandbox; 0 only when the wake-up was proven."""
    sandbox = None
    # One event loop for both bursts: the async model client is bound to the loop of its first call.
    runner = asyncio.Runner()
    try:
        log(f"1. Creating a sandbox (no internet access; set to pause itself once idle for {IDLE_TIMEOUT_S}s)...")
        sandbox = client.sandboxes.create({"name": f"sleepy-agent-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"},
                                           "lifecycle": {"idle_timeout_seconds": IDLE_TIMEOUT_S, "on_idle": "pause"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        life = sandbox.data
        log(f"   idle_timeout_seconds={life.get('idle_timeout_seconds')} on_idle={life.get('on_idle')} "
            f"idle_expires_at={life.get('idle_expires_at')}")
        log("2. Starting the agent's desk: a long-running process that keeps notes in memory only...")
        process, desk = start_desk(sandbox, sleep, clock)
        log(f"   desk running: PID {desk['pid']}, boot ID {desk['boot_id']}, {process.id}")
        log(f"3. Burst 1: asking {model} to triage {BATCHES[0].count(chr(10))} new messages...")
        before = burst(1, sandbox, connect, model_client, model, runner.run, log)
        log("4. Pausing the sandbox...")
        away = clock()
        sandbox.pause()
        wait_paused(sandbox, sleep, clock)
        log(f"   Paused {clock() - away:.1f}s after pause(): phase {sandbox.phase}, replicas {sandbox.replicas}. "
            "Its processes are frozen; nothing runs while it sleeps.")
        log(f"5. Sleeping {nap_s:g}s without touching the sandbox...")
        sleep(nap_s)
        log("6. Resuming the sandbox...")
        woke = clock()
        sandbox.resume()
        phase = sandbox.phase
        wait_awake(sandbox, sleep, clock)
        log(f"   resume() returned phase {phase}; the first command ran {clock() - woke:.1f}s after resume()")
        after = read_desk(sandbox)
        away_s = clock() - away
        log("7. Checking it is the same process...")
        if not prove_same_process(before, after, sandbox.processes.get(process.id).state, away_s, log):
            log("The desk did not come back as the same process.")
            return 1
        log(f"8. Burst 2: asking {model} to triage {BATCHES[1].count(chr(10))} more messages...")
        final = burst(2, sandbox, connect, model_client, model, runner.run, log)
        log(f"   the desk now holds {len(final['notes'])} notes, all in the memory of PID {final['pid']}")
        log("Proven: the sandbox slept, woke up with the same process and its memory, and the agent carried on.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        runner.close()
        if sandbox is not None:
            try:
                sandbox.delete()
                log("   Sandbox deleted.")
            except Exception as e:  # keep the run's exit code; tell the user what to clean up by hand
                log(f"   Could not delete sandbox {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")


def _seconds(value: str) -> float:
    """Parses a non-negative number of seconds for argparse."""
    seconds = float(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return seconds


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nap", type=_seconds, default=30, help="seconds to keep the sandbox paused (default 30)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect, nap_s=args.nap)


if __name__ == "__main__":
    sys.exit(main())
