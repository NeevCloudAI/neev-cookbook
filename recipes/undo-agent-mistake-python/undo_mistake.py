"""Undo the agent's mistake: snapshot a running app, let an agent wreck it, roll back, and prove it all came back."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from agent import clean_up

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
PORT = 8000
APP_DIR = Path(__file__).parent / "app"
DATA_FILES = ("data/customers.csv", "data/shop.db")
TASK = "Clean up the workspace to save space."
SCRIPTED_MISTAKE = "rm -rf data"
SNAPSHOT_TIMEOUT_S = 300
APP_TIMEOUT_S = 60


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


def fetch_stats(url: str) -> tuple[int | None, dict]:
    """GETs /stats on the app's preview URL: (HTTP status, JSON body), or (None, {"error": ...}) if unreachable."""
    import httpx

    try:
        response = httpx.get(f"{url}/stats", timeout=10)
    except httpx.HTTPError as e:
        return None, {"error": f"unreachable ({type(e).__name__})"}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": response.text[:200]}


def describe(status: int | None, body: dict) -> str:
    """Renders one /stats answer for the progress log."""
    if status is None:
        return f"not answering: {body.get('error')}"
    return f"{status} {json.dumps(body)}"


def data_intact(status: int | None, body: dict, baseline: dict) -> bool:
    """Reports whether the app still answers with the customer and order counts it had before the agent ran."""
    return status == 200 and all(body.get(k) == baseline[k] for k in ("customers", "orders"))


def start_app(sandbox, fetch, sleep, clock) -> tuple[str, object, dict]:
    """Uploads the app, seeds its data, starts the server and returns (preview URL, process, first /stats answer)."""
    for name in ("server.py", "seed.py"):
        sandbox.files.write(name, (APP_DIR / name).read_text())
    seeded = sandbox.exec(["python3", "seed.py"])
    if seeded.exit_code != 0:
        raise RuntimeError(f"seeding the data failed: {seeded.stderr.strip()}")
    process = sandbox.processes.start(["python3", "server.py"])
    url = sandbox.get_url(PORT)  # waits for the route, not for the server: a gateway 502 counts as routable
    status, body = wait_for_app(fetch, url, sleep, clock)
    if status != 200:
        raise RuntimeError(f"the app did not answer: {describe(status, body)}")
    return url, process, body


def take_snapshot(client, sandbox, sleep, clock):
    """Snapshots the sandbox and waits while it is Pending or Running; anything but Ready is an error."""
    snap = sandbox.snapshot({"name": f"{sandbox.name}-before-cleanup"})
    deadline = clock() + SNAPSHOT_TIMEOUT_S
    while snap.status.value in ("Pending", "Running"):
        if clock() >= deadline:
            raise RuntimeError(f"snapshot {snap.id} did not become Ready within {SNAPSHOT_TIMEOUT_S}s")
        sleep(1)
        snap = client.sandboxes.get_snapshot(snap.id)
    if snap.status.value != "Ready":
        raise RuntimeError(f"snapshot {snap.id} is {snap.status.value}: {snap.error_message}")
    return snap


def wait_for_app(fetch, url, sleep, clock) -> tuple[int | None, dict]:
    """Polls /stats until the app answers with anything but a gateway error, or APP_TIMEOUT_S passes."""
    deadline = clock() + APP_TIMEOUT_S
    while True:
        status, body = fetch(url)
        if status not in (None, 502, 503, 504) or clock() >= deadline:
            return status, body
        sleep(1)


def show_damage(sandbox, url, status, body, log) -> None:
    """Prints which data files are gone and what the app answers now."""
    for path in DATA_FILES:
        log(f"   {path}: {'still there' if sandbox.files.exists(path) else 'GONE'}")
    log(f"   {url}/stats -> {describe(status, body)}")


def _customer_rows(sandbox) -> int | None:
    """Counts data rows in customers.csv as read back from the sandbox, or None if it cannot be read."""
    try:
        return len(sandbox.files.read_text(DATA_FILES[0]).splitlines()) - 1
    except Exception:
        return None


def prove_restore(sandbox, process, status, body, baseline, log) -> bool:
    """Checks files, data, process and memory against the moment of the snapshot and prints each result.

    The request count lives only in the server's memory: a restarted server would start again from 0,
    so count == snapshot count + 1 (this request) proves the same process came back with its memory.
    """
    rows = _customer_rows(sandbox)
    pid = body.get("pid")
    checks = [
        (f"files: customers.csv read back with {rows} rows, shop.db present",
         rows == baseline["customers"] and sandbox.files.exists(DATA_FILES[1])),
        (f"data: the app answers {describe(status, body)}", data_intact(status, body, baseline)),
        (f"process: same server, PID {pid} (was {baseline['pid']}), {process.id} running",
         pid == baseline["pid"] and sandbox.processes.get(process.id).state == "running"),
        (f"memory: request count is {body.get('served')} (was {baseline['served']} at the snapshot, +1 for this request)",
         body.get("served") == baseline["served"] + 1),
    ]
    for label, ok in checks:
        log(f"   {'ok    ' if ok else 'FAILED'} {label}")
    return all(ok for _, ok in checks)


async def _clean_up(connect, sandbox_name: str, model_client, model: str, log) -> str:
    """Opens the MCP session for the sandbox and runs the cleanup agent over it."""
    try:
        async with connect(sandbox_name) as session:
            return await clean_up(session, model_client, model, TASK, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(client, model_client, model: str, connect, log=print, fetch=fetch_stats,
        sleep=time.sleep, clock=time.monotonic) -> int:
    """Runs the whole demo in one sandbox and always deletes it; returns 0 only when the restore was proven."""
    sandbox = None
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": f"undo-mistake-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Starting a small shop app: data files plus a running server...")
        url, process, baseline = start_app(sandbox, fetch, sleep, clock)
        log(f"   {url}/stats -> {describe(200, baseline)}")
        log("3. Taking a memory snapshot (files, memory and running processes)...")
        started = clock()
        snap = take_snapshot(client, sandbox, sleep, clock)
        log(f"   snapshot {snap.id} Ready in {clock() - started:.1f}s")
        log(f'4. Asking {model} to: "{TASK}"')
        summary = asyncio.run(_clean_up(connect, sandbox.name, model_client, model, log))
        log(f"   agent's summary: {summary}")
        log("5. Checking the damage...")
        status, body = wait_for_app(fetch, url, sleep, clock)  # a gateway blip is not damage
        if data_intact(status, body, baseline):
            log("   The agent left the data alone. Making the mistake for it so the rollback has something to undo.")
            log(f"   scripted mistake: {SCRIPTED_MISTAKE}")
            sandbox.exec(["sh", "-c", SCRIPTED_MISTAKE])
            status, body = wait_for_app(fetch, url, sleep, clock)
            if data_intact(status, body, baseline):
                raise RuntimeError(f"the app still has its data after {SCRIPTED_MISTAKE}")
        else:
            log("   The agent did the damage itself.")
        show_damage(sandbox, url, status, body, log)
        log("6. Rolling back to the snapshot...")
        started = clock()
        sandbox.rollback(snap.id)
        sandbox.wait_until_ready(timeout_ms=300_000)
        ready_s = clock() - started
        status, body = wait_for_app(fetch, url, sleep, clock)
        log(f"   sandbox Ready {ready_s:.1f}s after the rollback call, app answering after {clock() - started:.1f}s")
        log("7. Checking everything against the snapshot...")
        if not prove_restore(sandbox, process, status, body, baseline, log):
            log("The rollback did not restore everything.")
            return 1
        log("Restore proven: the files, the data, the running server and its memory are back.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        if sandbox is not None:
            try:
                sandbox.delete()
                log("   Sandbox deleted.")
            except Exception as e:  # keep the run's exit code; tell the user what to clean up by hand
                log(f"   Could not delete sandbox {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")


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
