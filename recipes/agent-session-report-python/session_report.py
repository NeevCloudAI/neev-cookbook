"""What did my agent do? Run a short agent task in a sandbox, then report it from the sandbox's audit trail."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from pathlib import Path

from agent import AgentFailed, run_agent
from report import make_report, to_markdown, to_terminal

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DEFAULT_TASK = (
    "Set up the project in the workspace by following its README, then summarise its configuration: "
    "which settings it uses, where each one comes from, and whether anything is missing."
)
PAGE_SIZE = 25
MAX_PAGES = 100  # a cap on one read, in case the server never stops handing out cursors
AUDIT_WAIT_S = 30  # records usually appear within a second; this bounds the wait if they are slow
AUDIT_POLL_S = 1

# The small project the agent works on. The .env holds obviously fake values and only ever exists in the sandbox.
PROJECT = {
    "README.md": (
        "# invoice-api\n\nA tiny invoice service.\n\n## Setup\n\n"
        "1. Remove the stale lock file `cache/stale.lock` left by an old run.\n"
        "2. Check the configuration with `python3 app.py --check`. Settings live in `settings.ini`; "
        "secrets come from `.env`.\n"
        "3. Write what you found to `SETUP_NOTES.md`.\n"),
    "app.py": (
        "import configparser, sys\n\n"
        "def main():\n"
        "    cfg = configparser.ConfigParser()\n"
        "    cfg.read('settings.ini')\n"
        "    env = dict(l.strip().split('=', 1) for l in open('.env') if '=' in l)\n"
        "    needed = [cfg[s][k] for s in cfg.sections() for k in cfg[s] if k.endswith('_env')]\n"
        "    missing = [n for n in needed if not env.get(n)]\n"
        "    print('port', cfg['server']['port'], '| secrets needed:', ', '.join(needed))\n"
        "    print('missing:', ', '.join(missing) or 'none')\n"
        "    return 1 if missing else 0\n\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"),
    "settings.ini": (
        "[server]\nhost = 0.0.0.0\nport = 8080\n\n[database]\nurl_env = DATABASE_URL\n\n"
        "[payments]\nprovider = dummypay\napi_key_env = PAYMENTS_API_KEY\n\n"
        "[email]\nsmtp_host = smtp.example.com\npassword_env = SMTP_PASSWORD\n"),
    ".env": (
        "DATABASE_URL=postgres://demo:not-a-real-password@localhost:5432/invoices\n"
        "PAYMENTS_API_KEY=dummy-key-0000-not-real\n"),
    "cache/stale.lock": "pid=4242\n",
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


def _cleanup(client, sandbox, name: str, log) -> None:
    """Deletes the sandbox; if create never returned (Ctrl+C, timeout), looks it up by name in case it exists.

    A failed delete is reported with the name instead of hiding the run's own result.
    """
    if sandbox is None:
        try:
            sandbox = next((s for s in client.sandboxes.list(name=name, limit=10).items if s.name == name), None)
        except Exception:  # e.g. the key that failed create fails here too: nothing we can see to delete
            return
        if sandbox is None:
            return
    try:
        sandbox.delete()
        log("   Sandbox deleted.")
    except Exception as e:
        log(f"   Could not delete sandbox {name} ({type(e).__name__}: {e}); delete it from the console.")


def run(task: str, out: Path, client, model_client, model: str, connect, log=print,
        sleep=time.sleep, clock=time.monotonic) -> int:
    """Seeds a sandbox, lets the agent work over MCP, writes the report from the audit trail, always deletes."""
    sandbox, name = None, f"session-report-{secrets.token_hex(4)}"
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": name, "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"2. Seeding {len(PROJECT)} project files, including a .env with dummy values...")
        for path, content in PROJECT.items():
            sandbox.files.write(path, content)
        setup, _ = wait_for_trail(sandbox, lambda rs: len(rs) >= len(PROJECT), sleep, clock)
        if len(setup) < len(PROJECT):
            log(f"The audit trail did not show the setup writes within {AUDIT_WAIT_S}s; try again shortly.")
            return 1
        # Everything after the newest setup record is the agent's. Both sides are server timestamps.
        boundary = max(r.at for r in setup)
        log(f"3. Asking {model} to: {task}")
        session = asyncio.run(_agent(connect, sandbox.name, model_client, model, task, log))
        log(f"4. Agent finished: {session.summary}")
        log("5. Reading the audit trail page by page...")
        records, retention = wait_for_trail(
            sandbox, lambda rs: sum(r.at > boundary for r in rs) >= session.calls, sleep, clock)
        report = make_report(sandbox.name, records, boundary, retention)
        if report.agent_actions == 0:
            log(f"The audit trail shows no agent actions after {AUDIT_WAIT_S}s; no report written.")
            return 1
        log("")
        log(to_terminal(report))
        log("")
        out.write_text(to_markdown(report))
        log(f"6. Report written to {out} ({report.agent_actions} of {session.calls} agent calls in the trail).")
        return 0
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except AgentFailed as e:
        log(f"The agent did not finish: {e}")
        return 1
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        _cleanup(client, sandbox, name, log)


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK)
    parser.add_argument("--out", type=Path, default=Path("report.md"), help="where to write the Markdown report (default report.md)")
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
        return run(args.task, args.out, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
