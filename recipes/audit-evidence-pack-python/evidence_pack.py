"""Audit evidence pack: export sandboxes' audit trails per credential as CSV and Markdown, with a SHA-256 manifest."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pack

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
DEFAULT_DAYS = 30  # the platform keeps 30 days; the server moves an older start forward and says so
PAGE_SIZE = 200  # the most the audit API returns per page
MAX_PAGES = 500  # 100,000 records per sandbox; a longer trail fails instead of being exported cut short
AUDIT_WAIT_S = 30  # demo records usually land within a second; this bounds the wait if they are slow
AUDIT_POLL_S = 1
DEMO_MARGIN = timedelta(minutes=5)  # widens the demo window around the run, against clock skew with the server
RELATIVE = re.compile(r"^(\d+)([dh])$")


class PackFailed(Exception):
    """The pack could not be produced, or did not verify."""


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def parse_time(text: str, now: datetime) -> datetime:
    """Reads 7d / 12h (before now), a date, or an RFC 3339 time; a time without a zone is UTC."""
    m = RELATIVE.match(text)
    if m:
        return now - timedelta(**{"days" if m[2] == "d" else "hours": int(m[1])})
    at = datetime.fromisoformat(text)
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def read_trail(client, sandbox: str, since: datetime, until: datetime, page_size: int = PAGE_SIZE) -> pack.Trail:
    """Reads one sandbox's whole trail for the window, following next_cursor with the window fixed on every page."""
    records, cursor, first = {}, None, None
    for _ in range(MAX_PAGES):
        page = client.sandboxes.audit(sandbox, from_=since.isoformat(), to=until.isoformat(), cursor=cursor, limit=page_size)
        first = first or page
        records.update((r.id, r) for r in page.records)  # keyed by id, so an overlapping page cannot double-count
        cursor = page.next_cursor
        if not cursor:
            return pack.Trail(name=sandbox, sandbox_id=str(first.sandbox_id), window_from=first.from_,
                              window_to=first.to, truncated=first.window_truncated,
                              retention_days=first.retention_days, records=list(records.values()))
    raise PackFailed(f"Sandbox {sandbox} has more than {MAX_PAGES * page_size} records in the window; narrow the window")


def _read_all(client, names, since, until, log) -> list[pack.Trail]:
    """Reads every named sandbox's trail; a sandbox the API cannot find fails the whole pack, naming it."""
    from neevai import NotFoundError

    trails = []
    for name in names:
        try:
            trails.append(read_trail(client, name, since, until))
        except NotFoundError:
            raise PackFailed(f"Sandbox {name} was not found in this project. The trail of a deleted sandbox "
                             "can no longer be read, so export before deleting.") from None
        log(f"   {name}: {len(trails[-1].records)} records")
    return trails


def wait_for_trails(client, expected: dict[str, Counter], since, until, sleep, clock) -> list[pack.Trail]:
    """Re-reads the trails until each shows every expected operation; records land a moment after each call.

    It waits per operation, not for a total, so an extra record cannot stand in for a late one.
    """
    deadline = clock() + AUDIT_WAIT_S
    while True:
        trails = [read_trail(client, name, since, until) for name in expected]
        missing = {t.name: expected[t.name] - Counter(r.tool for r in t.records) for t in trails}
        short = [f"{name} (missing {', '.join(sorted(ops.elements()))})" for name, ops in missing.items() if ops]
        if not short:
            return trails
        if clock() >= deadline:
            raise PackFailed(f"The audit trail did not show every operation within {AUDIT_WAIT_S}s: {', '.join(short)}")
        sleep(AUDIT_POLL_S)


def _write_verified(out: Path, trails, since, until, generated_at, log) -> str:
    """Writes the pack, then re-hashes it from disk; returns the manifest's SHA-256 only if every file matches."""
    digest = pack.write_pack(out, trails, since, until, generated_at)
    problems = pack.verify_manifest(out)
    if problems:
        raise PackFailed(f"The pack in {out} did not verify: {'; '.join(problems)}")
    log(pack.to_terminal(trails, pack.make_rows(trails)))
    return digest


def _report_written(out: Path, digest: str, step: int, log) -> None:
    """Prints where the pack is and the manifest hash to hand over separately."""
    log(f"{step}. Pack written to {out}/ and verified against {pack.MANIFEST}.")
    log(f"   SHA-256 of {pack.MANIFEST}: {digest}")
    log("   Hand this hash over separately from the pack, so the recipient can tell if anything was changed.")


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def _fail(e: BaseException, log) -> int:
    """Logs a failure as one line instead of a traceback and returns exit code 1."""
    cause = _root_cause(e)
    log(str(cause) if isinstance(cause, PackFailed) else f"Failed: {type(cause).__name__}: {cause}")
    return 1


def run_export(client, names: list[str], since: datetime, until: datetime, out: Path, log=print, now=None) -> int:
    """Normal mode: exports the named sandboxes' trails for the window into a verified pack."""
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        log(f"1. Reading the audit trail of {pack.count(len(names), 'sandbox')}, {pack.stamp(since)} to {pack.stamp(until)}...")
        trails = _read_all(client, names, since, until, log)
        log("2. Writing the evidence pack...")
        digest = _write_verified(out, trails, since, until, now(), log)
        _report_written(out, digest, 3, log)
        return 0
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except Exception as e:
        return _fail(e, log)


def sdk_work(sandbox, log) -> Counter:
    """Representative work through the SDK, including a failed read and a delete; returns the records to expect."""
    from neevai import NotFoundError

    sandbox.files.write("report.txt", "quarterly numbers\n")
    sandbox.files.write(".env", "PAYMENTS_API_KEY=dummy-not-a-real-key\n")
    sandbox.files.read_text(".env")
    log("   wrote report.txt and a .env with a dummy value, read the .env back")
    sandbox.exec(["ls", "-la"])
    code = sandbox.exec(["ls", "no-such-dir"]).exit_code
    log(f"   ran ls -la, and ls on a missing folder (exit code {code})")
    try:
        sandbox.files.read_text("missing.txt")
        raise PackFailed("Reading missing.txt unexpectedly succeeded.")
    except NotFoundError:
        log("   read missing.txt: refused, not found (as intended)")
    sandbox.processes.start(["sleep", "30"])
    sandbox.files.remove("report.txt")
    log("   started sleep 30 in the background, removed report.txt")
    return Counter({"fs.write": 2, "fs.read": 2, "exec": 2, "process.start": 1, "fs.remove": 1})


# The MCP calls the demo makes, and whether each is meant to fail.
MCP_CALLS = (
    ("fs_write", {"path": "notes.txt", "content": "call notes\n"}, False),
    ("exec", {"program": "sh", "args": ["-c", "wc -c notes.txt"]}, False),
    ("fs_read", {"path": "missing.txt"}, True),
    ("process_start", {"program": "sleep", "args": ["30"]}, False),
)


async def mcp_work(session, log) -> Counter:
    """Representative work over the sandbox MCP server, including a refused read; returns the records to expect."""
    for tool, args, should_fail in MCP_CALLS:
        result = await session.call_tool(tool, args)
        text = json.dumps(result.structured_content) if result.structured_content is not None else \
            " ".join(getattr(b, "text", "") for b in result.content)
        if result.is_error != should_fail:
            raise PackFailed(f"MCP {tool} {'succeeded' if should_fail else 'failed'} unexpectedly: {text[:200]}")
        log(f"   {tool} {args.get('path') or shlex.join([args['program'], *args.get('args', [])])}"
            + (f": {text[:80]} (as intended)" if should_fail else ""))
    return Counter(tool.replace("_", ".") for tool, _, _ in MCP_CALLS)  # fs_write is recorded as fs.write


async def _mcp(connect, sandbox_name: str, log) -> Counter:
    """Opens the MCP session for the sandbox and runs the MCP work over it."""
    try:
        async with connect(sandbox_name) as session:
            return await mcp_work(session, log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def _delete(client, sandbox, name: str, log) -> bool:
    """Deletes one demo sandbox, looking it up by name if create never returned; True once it is gone."""
    try:
        if sandbox is None:
            sandbox = next((s for s in client.sandboxes.list(name=name, limit=10).items if s.name == name), None)
            if sandbox is None:
                return True
        sandbox.delete()
        log(f"   Sandbox {name} deleted.")
        return True
    except Exception as e:
        log(f"   Could not delete sandbox {name} ({type(e).__name__}: {e}); delete it from the console.")
        return False


def run_demo(client, connect, out: Path, log=print, sleep=time.sleep, clock=time.monotonic, now=None) -> int:
    """Demo mode: works in two sandboxes (SDK and MCP), exports their trails, then deletes both.

    Exits 0 only when the pack verified and every sandbox was deleted. The export must come first:
    a deleted sandbox's trail can no longer be read.
    """
    now = now or (lambda: datetime.now(timezone.utc))
    suffix = secrets.token_hex(4)
    names = [f"evidence-sdk-{suffix}", f"evidence-mcp-{suffix}"]
    made: dict[str, object] = {}  # name -> handle, or None while create has not returned
    code = 1
    try:
        since = (now() - DEMO_MARGIN).replace(microsecond=0)
        log(f"1. Creating two sandboxes with no internet access: {', '.join(names)}")
        for name in names:
            made[name] = None
            made[name] = client.sandboxes.create({"name": name, "egress": {"mode": "deny_all"}})
        for sandbox in made.values():
            sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"2. Working in {names[0]} through the SDK:")
        expected = {names[0]: sdk_work(made[names[0]], log)}
        log(f"3. Working in {names[1]} over MCP:")
        expected[names[1]] = asyncio.run(_mcp(connect, names[1], log))
        log("4. Waiting for both audit trails to show every operation...")
        until = (now() + DEMO_MARGIN).replace(microsecond=0)
        trails = wait_for_trails(client, expected, since, until, sleep, clock)
        log(f"5. Exporting {pack.stamp(since)} to {pack.stamp(until)}, before the sandboxes are deleted:")
        digest = _write_verified(out, trails, since, until, now(), log)
        _report_written(out, digest, 6, log)
        code = 0
    except KeyboardInterrupt:
        log("Interrupted.")
        code = 130
    except Exception as e:
        code = _fail(e, log)
    finally:
        deleted = [_delete(client, sandbox, name, log) for name, sandbox in made.items()]
    if code == 0 and not all(deleted):
        log("The pack is complete, but a demo sandbox was left behind.")
        return 1
    return code


def _usage(argv) -> tuple[argparse.Namespace, datetime, datetime, Path] | str:
    """Parses and checks the arguments; returns them with the window and output folder, or an error message."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="create two sandboxes, do some work, export, delete them")
    parser.add_argument("--sandbox", action="append", default=[], metavar="NAME", help="a sandbox to export (repeatable)")
    parser.add_argument("--since", help="window start: 7d, 12h, a date or an RFC 3339 time (default 30 days ago)")
    parser.add_argument("--until", help="window end, same forms (default now)")
    parser.add_argument("--out", type=Path, help="folder for the pack (default evidence-pack-<UTC time>)")
    args = parser.parse_args(argv)
    if args.demo == bool(args.sandbox):
        return "Use either --demo or --sandbox NAME (repeatable), not both." if args.demo else \
            "Name at least one --sandbox NAME to export, or run --demo."
    if args.demo and (args.since or args.until):
        return "--since and --until apply to --sandbox exports; --demo exports its own run."
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        until = parse_time(args.until, now) if args.until else now
    except ValueError:
        return f"--until: cannot read {args.until!r}; use 7d, 12h, a date (2026-10-01) or an RFC 3339 time."
    try:
        since = parse_time(args.since, now) if args.since else until - timedelta(days=DEFAULT_DAYS)
    except ValueError:
        return f"--since: cannot read {args.since!r}; use 7d, 12h, a date (2026-10-01) or an RFC 3339 time."
    if since >= until:
        return "--since must be before --until."
    out = args.out or Path(f"evidence-pack-{now:%Y%m%dT%H%M%SZ}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        return f"{out} is not empty; choose a new folder with --out, so the pack holds only its own files."
    return args, since, until, out


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the demo or the export."""
    parsed = _usage(argv)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return 2
    args, since, until, out = parsed
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        if args.demo:
            return run_demo(client, mcp_connect(os.environ["NEEV_API_KEY"]), out)
        return run_export(client, list(dict.fromkeys(args.sandbox)), since, until, out)


if __name__ == "__main__":
    sys.exit(main())
