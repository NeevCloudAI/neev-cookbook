"""Human review gate: an agent changes code in a sandbox; you see its diff and its audit trail, then approve or reject."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent import AgentFailed, run_agent
from review import MAX_ENTRIES, Packet, collect_changes, export, make_activity, printable, to_markdown, to_terminal

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DEFAULT_TASK = (
    "Add input validation to create_user in signup.py: raise ValueError when the email does not have "
    "exactly one '@' with text on both sides, when its domain is in the BLOCKED_DOMAINS setting, or when "
    "age is not an integer from 13 to 120. Add tests for each case to test_signup.py, then run the tests "
    "with: python3 -m unittest -v"
)
PAGE_SIZE = 25
MAX_PAGES = 100  # a cap on one read, in case the server never stops handing out cursors
AUDIT_WAIT_S = 30  # records usually appear within a second; this bounds the wait if they are slow
AUDIT_POLL_S = 1

# The small repository the agent changes. The .env holds obviously fake values and only ever exists in the sandbox.
PROJECT = {
    "README.md": (
        "# signup\n\nA tiny signup module.\n\n"
        "- `signup.py` creates user records.\n"
        "- `config.py` loads settings from `.env`.\n"
        "- Run the tests with `python3 -m unittest -v`.\n"),
    "config.py": (
        "import os\n\n\n"
        "def load(path=\".env\"):\n"
        "    \"\"\"Reads KEY=VALUE lines from the dotenv file into a dict.\"\"\"\n"
        "    values = {}\n"
        "    if os.path.exists(path):\n"
        "        for line in open(path):\n"
        "            if \"=\" in line and not line.startswith(\"#\"):\n"
        "                key, value = line.strip().split(\"=\", 1)\n"
        "                values[key] = value\n"
        "    return values\n"),
    "signup.py": (
        "import config\n\n\n"
        "def create_user(email, age):\n"
        "    \"\"\"Returns a new user record.\"\"\"\n"
        "    settings = config.load()\n"
        "    return {\"email\": email.strip().lower(), \"age\": age, \"welcome_from\": settings.get(\"WELCOME_FROM\", \"\")}\n"),
    "test_signup.py": (
        "import unittest\n\nfrom signup import create_user\n\n\n"
        "class CreateUserTest(unittest.TestCase):\n"
        "    def test_normalises_email(self):\n"
        "        self.assertEqual(create_user(\"  Ada@Example.com \", 36)[\"email\"], \"ada@example.com\")\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n"),
    ".env": ("WELCOME_FROM=hello@example.com\nBLOCKED_DOMAINS=mailinator.com,trashmail.example\n"
             "SMTP_PASSWORD=dummy-not-a-real-password\n"),
}


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


async def _agent(connect, sandbox_name: str, model_client, model: str, task: str, log):
    """Opens the MCP session for the sandbox and runs the agent loop over it."""
    try:
        async with connect(sandbox_name) as session:
            return await run_agent(session, model_client, model, task, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def read_trail(sandbox) -> tuple[list, int]:
    """Reads the whole audit trail, following next_cursor page by page; returns records and retention days."""
    records, cursor = [], None
    for _ in range(MAX_PAGES):
        page = sandbox.audit(cursor=cursor, limit=PAGE_SIZE)
        records += page.records
        cursor = page.next_cursor
        if not cursor:
            break
    return records, page.retention_days


def wait_for_trail(sandbox, enough, sleep, clock) -> tuple[list, int]:
    """Re-reads the trail until enough(records) holds or AUDIT_WAIT_S passes; records land a moment after the call."""
    deadline = clock() + AUDIT_WAIT_S
    while True:
        records, retention = read_trail(sandbox)
        if enough(records) or clock() >= deadline:
            return records, retention
        sleep(AUDIT_POLL_S)


def _uploads(records) -> dict:
    """Maps each written path to its earliest fs.write record time, to find where the script's upload ends."""
    times = {}
    for r in records:
        if r.tool == "fs.write" and r.target:
            times[r.target] = min(r.at, times.get(r.target, r.at))
    return times


def _decide(decision: str | None, ask, log) -> bool:
    """Returns True to approve: from --approve/--reject, else the reviewer's answer, where only y/yes approves."""
    if decision is not None:
        return decision == "approve"
    try:
        return ask("Approve this change? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # no terminal to answer from: the safe default is to reject
        log("   No answer (stdin is closed); rejecting.")
        return False


def _cleanup(client, sandbox, name: str, log) -> bool:
    """Deletes the sandbox, looking it up by name if create never returned; True when nothing is left behind."""
    if sandbox is None:
        try:
            sandbox = next((s for s in client.sandboxes.list(name=name, limit=10).items if s.name == name), None)
        except Exception as e:  # we cannot tell whether the server made it, so say which name to look for
            log(f"   Could not check for sandbox {name} ({type(e).__name__}: {e}); if it exists, delete it from the console.")
            return False
        if sandbox is None:
            return True
    try:
        sandbox.delete()
        log("   Sandbox deleted.")
        return True
    except Exception as e:
        log(f"   Could not delete sandbox {name} ({type(e).__name__}: {e}); delete it from the console.")
        return False


def run(task: str, review_path: Path, out_root: Path, decision: str | None, client, model_client, model: str,
        connect, log=print, ask=input, sleep=time.sleep, clock=time.monotonic) -> int:
    """Lets the agent change the project in a sandbox, puts its diff beside its audit trail, and exports only on approval.

    The trail is read before the sandbox is deleted, because it cannot be read afterwards.
    """
    raw_log = log

    def log(line: str) -> None:  # every line passes here, so model or sandbox text cannot drive the terminal
        raw_log(printable(line))

    sandbox, name, code = None, f"review-gate-{secrets.token_hex(4)}", 1
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": name, "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"2. Uploading the project ({len(PROJECT)} files, including a .env with dummy values)...")
        for path, content in PROJECT.items():
            sandbox.files.write(path, content)
        records, _ = wait_for_trail(sandbox, lambda rs: set(PROJECT) <= _uploads(rs).keys(), sleep, clock)
        uploads = _uploads(records)
        if set(PROJECT) - uploads.keys():
            log(f"The audit trail did not show the upload within {AUDIT_WAIT_S}s; try again shortly.")
            return 1
        # Everything after the newest upload record is the agent's. Both sides are server timestamps.
        boundary = max(uploads[path] for path in PROJECT)
        log(f"3. Asking {model} to: {task}")
        session = asyncio.run(_agent(connect, sandbox.name, model_client, model, task, log))
        log(f"4. Agent finished: {session.summary}")
        log("5. Reading the audit trail page by page...")
        # Read before diffing, so the script's own file reads stay out of the agent's activity.
        records, retention = wait_for_trail(
            sandbox, lambda rs: sum(r.at > boundary for r in rs) >= session.calls, sleep, clock)
        log("6. Diffing the sandbox against the original files...")
        entries = sandbox.files.list(".", recursive=True, max_count=MAX_ENTRIES)
        if len(entries) >= MAX_ENTRIES:
            log(f"The workspace holds more than {MAX_ENTRIES} entries; too many to review.")
            return 1
        changes = collect_changes(PROJECT, entries, sandbox.files.read)
        if not changes:
            log("The agent changed no files; nothing to review.")
            return 1
        packet = Packet(sandbox.name, model, task, session.summary, changes,
                        make_activity(records, boundary), session.calls, retention)
        log("")
        log(to_terminal(packet))
        log("")
        review_path.write_text(to_markdown(packet), encoding="utf-8")
        log(f"7. Review packet written to {review_path}.")
        approved = _decide(decision, ask, log)
        how = f"with --{decision}" if decision else "by the reviewer"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if approved:
            out = out_root / sandbox.name
            written = export(changes, out)
            log(f"8. Approved {how}. Wrote {len(written)} changed files to {out}/")
            for c in changes:
                if c.status in ("deleted", "skipped"):
                    log(f"   {c.status} in the sandbox, not written: {c.path}")
        else:
            log(f"8. Rejected {how}: nothing written. The change is discarded with the sandbox.")
        with review_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Decision\n\n{'Approved' if approved else 'Rejected'} {how}, {stamp}.\n")
        code = 0
    except KeyboardInterrupt:
        log("Interrupted.")
        code = 130
    except AgentFailed as e:
        log(f"The agent did not finish: {e}")
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
    finally:
        deleted = _cleanup(client, sandbox, name, log)
    # A sandbox left behind fails the run even when the gate itself completed.
    return code if deleted else max(code, 1)


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--approve", dest="decision", action="store_const", const="approve",
                        help="approve without asking (for CI)")
    choice.add_argument("--reject", dest="decision", action="store_const", const="reject",
                        help="reject without asking (for CI)")
    parser.add_argument("--review", type=Path, default=Path("review.md"),
                        help="where to write the review packet (default review.md)")
    parser.add_argument("--out", type=Path, default=Path("approved"),
                        help="approved changes go to OUT/<sandbox name>/ (default approved)")
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
        return run(args.task, args.review, args.out, args.decision, client, model_client,
                   os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
