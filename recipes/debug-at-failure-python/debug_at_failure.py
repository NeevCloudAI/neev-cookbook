"""Debug at the failure point: when a job fails, snapshot its sandbox, fork it, and have an agent investigate."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import secrets
import sys
import time
from pathlib import Path

from agent import AgentFailed, diagnose

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
PIPELINE = Path(__file__).parent / "app" / "pipeline.py"
SERVICE_URL = "http://127.0.0.1:8080"  # the pipeline's own endpoint, reachable only inside the sandbox
BATCH_SIZE = 240
REGIONS = ("north", "south", "east", "west")
PIPELINE_TIMEOUT_S = 120
AGENT_DEADLINE_S = 180
SNAPSHOT_TIMEOUT_S = 300


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def make_batch(seed: str) -> tuple[list[dict], int]:
    """Builds the job's batch of orders and returns it with the index of the one bad record injected into it.

    The bad amount is a string with a thousands separator, as a CSV export would write it.
    """
    rng = random.Random(seed)
    records = [{"id": f"ord-{n:05x}", "region": rng.choice(REGIONS), "amount": round(rng.uniform(5, 900), 2)}
               for n in rng.sample(range(16**5), BATCH_SIZE)]
    bad = rng.randrange(BATCH_SIZE // 2, BATCH_SIZE - 10)
    records[bad]["amount"] = f"{rng.uniform(1000, 9999):,.2f}"
    return records, bad


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def read_state(sandbox) -> dict | None:
    """Asks the pipeline for its in-memory /state from inside the sandbox; None while it is not answering."""
    result = sandbox.exec(["curl", "-s", f"{SERVICE_URL}/state"], timeout_ms=15_000)
    if result.exit_code != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def wait_for_failure(sandbox, sleep, clock, log) -> dict:
    """Polls the pipeline until it stops running and returns its state; finishing cleanly is an error here."""
    deadline, seen = clock() + PIPELINE_TIMEOUT_S, set()
    while True:
        state = read_state(sandbox)
        if state and state.get("status") != "running":
            break
        if state and state.get("stage") not in seen | {None}:
            seen.add(state["stage"])
            log(f"   stage {state['stage']}...")
        if clock() >= deadline:
            raise RuntimeError(f"the pipeline did not fail or finish within {PIPELINE_TIMEOUT_S}s")
        sleep(1)
    if state.get("status") != "error":
        raise RuntimeError(f"the pipeline finished without failing (status {state.get('status')})")
    return state


def take_snapshot(client, sandbox, sleep, clock):
    """Snapshots the sandbox and waits while it is Pending or Running; anything but Ready is an error."""
    snap = sandbox.snapshot({"name": f"{sandbox.name}-at-failure"})
    deadline = clock() + SNAPSHOT_TIMEOUT_S
    while snap.status.value in ("Pending", "Running"):
        if clock() >= deadline:
            raise RuntimeError(f"snapshot {snap.id} did not become Ready within {SNAPSHOT_TIMEOUT_S}s")
        sleep(1)
        snap = client.sandboxes.get_snapshot(snap.id)
    if snap.status.value != "Ready":
        raise RuntimeError(f"snapshot {snap.id} is {snap.status.value}: {snap.error_message}")
    return snap


def report(checks: list[tuple[str, bool]], log) -> bool:
    """Prints each check as ok or FAILED and reports whether all passed."""
    for label, ok in checks:
        log(f"   {'ok    ' if ok else 'FAILED'} {label}")
    return all(ok for _, ok in checks)


def reproduced(fork, process, before: dict, log) -> bool:
    """Checks the fork holds the failed pipeline itself: same process, same failure, memory carried over.

    The read count lives only in the process's memory: a restarted pipeline would count from zero,
    so one more than the last read before the snapshot proves the same process came across.
    """
    after = read_state(fork) or {}
    state = fork.processes.get(process.id).state
    return report([
        (f"process: same pipeline process, PID {after.get('pid')} (was {before['pid']}), {process.id} {state}",
         after.get("pid") == before["pid"] and state == "running"),
        (f"failure: still failed in stage {after.get('stage')} at record {after.get('position')}: {after.get('error')}",
         after.get("status") == "error" and all(after.get(k) == before[k] for k in ("stage", "position", "error"))),
        (f"memory: /state read {after.get('reads')} times "
         f"(was {before['reads']} before the snapshot, +1 for this read)",
         after.get("reads") == before["reads"] + 1),
    ], log)


def matches(diagnosis: dict[str, str], record: dict, log) -> bool:
    """Checks the agent's findings against the record the script injected."""
    value = record["amount"]
    return report([
        (f"record: {diagnosis['record_id']} (injected: {record['id']})",
         diagnosis["record_id"].lower() == record["id"]),
        (f"field: {diagnosis['field']} (injected: amount)", diagnosis["field"].strip("'\"` ").lower() == "amount"),
        (f"value: {diagnosis['value']} (injected: {value!r})", value in diagnosis["value"]),
    ], log)


async def _diagnose(connect, sandbox_name: str, model_client, model: str, task: str, log,
                    deadline_s: float) -> dict[str, str]:
    """Opens the MCP session for the fork and runs the debugging agent over it, all within deadline_s.

    The deadline covers the session handshake and every model and tool call, so nothing hung outlasts it.
    """
    try:
        async with asyncio.timeout(deadline_s), connect(sandbox_name) as session:
            return await diagnose(session, model_client, model, task, log=log)
    except TimeoutError:
        raise AgentFailed(f"time limit of {deadline_s:.0f}s reached") from None
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def _lookup(client, name: str, log) -> list:
    """Finds the sandbox with exactly this name, if one exists, so it can be deleted too."""
    try:
        return [sb for sb in client.sandboxes.list(name=name, limit=100).items if sb.name == name]
    except (Exception, KeyboardInterrupt) as e:  # cleanup carries on with the other sandboxes
        log(f"   Could not check for a sandbox named {name} ({type(e).__name__}: {e}); look for it in the console.")
        return []


def _delete(sandbox, log) -> bool:
    """Deletes one sandbox; a failure is one line naming it, so the caller can carry on."""
    try:
        sandbox.delete()
        return True
    except (Exception, KeyboardInterrupt) as e:
        log(f"   Could not delete {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")
        return False


def run(client, model_client, model: str, connect, log=print, sleep=time.sleep, clock=time.monotonic,
        agent_deadline_s: float = AGENT_DEADLINE_S) -> int:
    """Runs the pipeline until it fails, forks the failure, has the agent diagnose the fork; always deletes both."""
    # Every sandbox asked for and not yet deleted, by name; None until its create call returns.
    sandboxes: dict[str, object] = {}
    name = f"debug-fail-{secrets.token_hex(4)}"
    records, bad = make_batch(secrets.token_hex(8))
    try:
        log("1. Creating a sandbox for the job (no internet access)...")
        sandboxes[name] = None
        source = sandboxes[name] = client.sandboxes.create({"name": name, "egress": {"mode": "deny_all"}})
        source.wait_until_ready(timeout_ms=300_000)
        log(f"2. Running a 3-stage order pipeline over {len(records)} records as a background process...")
        source.files.write("pipeline.py", PIPELINE.read_text())
        # The batch arrives on stdin, so the records exist only in the process's memory, never on disk.
        process = source.processes.start(["python3", "pipeline.py"], stdin=json.dumps(records))
        started = clock()
        failed = wait_for_failure(source, sleep, clock, log)
        log(f"   Stage {failed['stage']} failed at record {failed['position']} after {clock() - started:.0f}s: "
            f"{failed['error']}")
        log("   The process is still up, holding the batch and its running totals in memory.")
        log("3. Snapshotting the sandbox at the failure point (files, memory and running processes)...")
        started = clock()
        snap = take_snapshot(client, source, sleep, clock)
        log(f"   snapshot {snap.id} Ready in {clock() - started:.1f}s")
        log("4. Forking a debug sandbox from that snapshot (no internet access)...")
        started, fork_name = clock(), f"{name}-fork"
        sandboxes[fork_name] = None
        fork = sandboxes[fork_name] = client.sandboxes.create(
            {"name": fork_name, "restore": snap.id, "egress": {"mode": "deny_all"}})
        fork.wait_until_ready(timeout_ms=300_000)
        log(f"   {fork.name} Ready in {clock() - started:.1f}s")
        if not reproduced(fork, process, failed, log):
            log("The fork did not reproduce the failure.")
            return 1
        if _delete(source, log):
            del sandboxes[name]
            log(f"   Deleted the failed job's sandbox {source.name}: the fork does not need it.")
        log(f"5. Asking {model} to find the root cause in the fork:")
        task = (f"The pipeline stopped in stage {failed['stage']} with: {failed['error']}. "
                "Find the root cause.")
        try:
            diagnosis = asyncio.run(_diagnose(connect, fork.name, model_client, model, task, log, agent_deadline_s))
        except AgentFailed as e:
            log(f"The agent gave no diagnosis: {e}")
            return 1
        log("6. The agent's diagnosis:")
        log(f"   record  {diagnosis['record_id']}")
        log(f"   field   {diagnosis['field']} = {diagnosis['value']}")
        log(f"   cause   {diagnosis['cause']}")
        log("7. Checking it against the fault the script injected...")
        if not matches(diagnosis, records[bad], log):
            log("The diagnosis does not match the injected fault.")
            return 1
        log("Root cause found in the fork, from the live process's memory, without rerunning the job.")
        return 0
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        for asked, sb in sandboxes.items():
            # A create whose reply was lost may still have made the sandbox, so look it up by name.
            for found in [sb] if sb is not None else _lookup(client, asked, log):
                if _delete(found, log):
                    log(f"   Deleted {found.name}.")


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
        return run(client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
