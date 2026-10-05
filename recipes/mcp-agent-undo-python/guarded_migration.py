"""MCP agent with an undo button: the agent snapshots before a risky migration and rolls back when the tests fail."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from agent import ToolCall, run_agent

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
APP_DIR = Path(__file__).parent / "app"
APP_FILES = ("seed.py", "migrate.py", "migrations/002_customer_email.sql", "test_shop.py")
BASELINE = {"customers": 50, "orders": 120}
TASK = ("Apply the pending database migration migrations/002_customer_email.sql to shop.db by running "
        "`python3 migrate.py migrations/002_customer_email.sql`. The test suite is "
        "`python3 -m unittest -v test_shop`.")
# migrate.py prints this only after the transaction committed; a dry run on a copy names a different database.
APPLIED = re.compile(r"applied \S*migrations/002_customer_email\.sql to /workspace/shop\.db\b")
COUNT_ROWS = ("import json, sqlite3; db = sqlite3.connect('shop.db'); "
              "print(json.dumps({t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('customers', 'orders')}))")


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


@dataclass(frozen=True)
class Review:
    """What the agent's own MCP calls show about the protocol, independent of what it said it did."""
    snapshot_id: str | None  # the first snapshot it took
    migrated: bool  # the migration ran and committed
    guarded: bool  # that snapshot was taken before the migration ran
    ready_first: bool  # and the agent saw it Ready before the migration ran
    rolled_back: bool  # a rollback succeeded after the migration ran


def _seen_ready(calls: list[ToolCall], snapshot_id: str) -> bool:
    """Reports whether any of these calls showed the snapshot with status Ready."""
    for c in calls:
        items = (c.data().get("items") or []) if c.name == "list_snapshots" else [c.data()]
        if any(isinstance(i, dict) and i.get("id") == snapshot_id and i.get("status") == "Ready" for i in items):
            return True
    return False


def review(calls: list[ToolCall]) -> Review:
    """Reads the order of the agent's calls: snapshot, seen Ready, then migration, then a rollback that succeeded."""
    snaps = [i for i, c in enumerate(calls) if c.snapshot_id()]
    snapshot_id = calls[snaps[0]].snapshot_id() if snaps else None
    migrations = [i for i, c in enumerate(calls) if c.name == "exec" and c.ok and APPLIED.search(c.output)]
    if not migrations:
        return Review(snapshot_id, False, False, False, False)
    first = migrations[0]
    guarded = bool(snaps) and snaps[0] < first
    ready_first = guarded and _seen_ready(calls[snaps[0]:first], snapshot_id)
    rolled_back = any(c.name == "rollback_sandbox" and c.ok for c in calls[first + 1:])
    return Review(snapshot_id, True, guarded, ready_first, rolled_back)


def count_rows(sandbox) -> dict:
    """Counts customers and orders in shop.db through the SDK; {"error": ...} if the tables cannot be read."""
    result = sandbox.exec(["python3", "-c", COUNT_ROWS])
    try:
        if result.exit_code == 0:
            return json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    lines = (result.stderr or result.stdout).strip().splitlines()
    return {"error": lines[-1] if lines else f"exit code {result.exit_code}"}


def tests_pass(sandbox) -> bool:
    """Runs the shop's test suite through the SDK."""
    return sandbox.exec(["python3", "-m", "unittest", "-q", "test_shop"]).exit_code == 0


def seed_app(sandbox) -> None:
    """Uploads the shop app, creates shop.db, and checks the data and the test suite before the agent starts."""
    for name in APP_FILES:
        sandbox.files.write(name, (APP_DIR / name).read_text())
    seeded = sandbox.exec(["python3", "seed.py"])
    if seeded.exit_code != 0:
        raise RuntimeError(f"seeding the database failed: {seeded.stderr.strip()}")
    if count_rows(sandbox) != BASELINE or not tests_pass(sandbox):
        raise RuntimeError("the seeded database does not pass its own test suite")


def check_outcome(sandbox, calls: list[ToolCall], log) -> bool:
    """Checks the agent's calls and the sandbox itself, prints each result, and returns True only if all pass."""
    r = review(calls)
    short = (r.snapshot_id or "")[:8]
    ready = {str(s.id) for s in sandbox.snapshots() if s.status.value == "Ready"}
    if not r.migrated:
        snapshot = ("the agent never ran the migration", False)
    elif not r.guarded:
        snapshot = ("the agent ran the migration without taking a snapshot first", False)
    elif not r.ready_first:
        snapshot = (f"the agent ran the migration before its snapshot {short} was Ready", False)
    elif r.snapshot_id not in ready:
        snapshot = (f"the agent took snapshot {short} before the migration, but it is not Ready in the sandbox's snapshot list", False)
    else:
        snapshot = (f"the agent took snapshot {short} before the migration, and it is Ready", True)
    if not r.migrated:
        rollback = ("nothing to roll back: the migration never ran", False)
    elif r.rolled_back:
        rollback = ("the agent rolled back after the migration ran", True)
    else:
        rollback = ("the agent did not roll back after the migration ran", False)
    rows, passing = count_rows(sandbox), tests_pass(sandbox)
    found = (f"shop.db cannot be read ({rows['error']})" if "error" in rows
             else f"shop.db has {rows.get('customers')} customers and {rows.get('orders')} orders")
    data = (f"{found} (was {BASELINE['customers']} and {BASELINE['orders']}); the test suite {'passes' if passing else 'fails'}",
            rows == BASELINE and passing)
    for label, (text, ok) in (("snapshot", snapshot), ("rollback", rollback), ("data", data)):
        log(f"   {'ok    ' if ok else 'FAILED'} {label}: {text}")
    if r.ready_first and not data[1]:
        log(f"   The agent's snapshot {short} was there to roll back to, but the data was not restored.")
    return snapshot[1] and rollback[1] and data[1]


async def _guard(connect, sandbox_name: str, model_client, model: str, log):
    """Opens the MCP session for the sandbox and runs the agent over it."""
    try:
        async with connect(sandbox_name) as session:
            return await run_agent(session, model_client, model, TASK, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(client, model_client, model: str, connect, log=print) -> int:
    """Runs the whole demo in one sandbox and always deletes it; returns 0 only if the agent guarded itself and the data survived."""
    sandbox = None
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": f"mcp-undo-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Uploading a small shop app with a SQLite database and a test suite...")
        seed_app(sandbox)
        log(f"   shop.db: {BASELINE['customers']} customers, {BASELINE['orders']} orders; the test suite passes")
        log(f"3. Asking {model} over MCP to apply migrations/002_customer_email.sql...")
        result = asyncio.run(_guard(connect, sandbox.name, model_client, model, log))
        log(f"   agent's verdict: {result.verdict}")
        log(f"   agent's summary: {result.summary}")
        log("4. Checking what the agent did and what is in the sandbox now...")
        sandbox.refresh()  # a rollback restarts the sandbox behind this handle's back
        sandbox.wait_until_ready(timeout_ms=300_000)
        if not check_outcome(sandbox, result.calls, log):
            log("The agent did not follow the snapshot-and-rollback protocol, or the data did not survive.")
            return 1
        log("The agent pressed its own undo button: snapshot, migration, failing tests, rollback, data intact.")
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
                log("   Sandbox deleted, and its snapshots with it.")
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
