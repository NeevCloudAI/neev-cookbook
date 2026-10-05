"""Prints what a coding agent did in a NeevCloud sandbox, read from the sandbox's audit trail."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
PAGE_SIZE = 100
COLUMNS = "{:<8}  {:<14}  {:<10}  {:<30}  {:<22}  {:>7}  {}"


def missing_env(environ, required=REQUIRED_ENV) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in required if not environ.get(name)]


def read_trail(sandbox) -> tuple[list, bool]:
    """Reads every page of the sandbox's trail since it was created; returns records oldest first and the truncation flag."""
    records, cursor, truncated = [], None, False
    # Without from_ the API returns only the last 24 hours; the sandbox's creation time covers its whole life.
    since = sandbox.data.get("created_at")
    while True:
        page = sandbox.audit(from_=since, cursor=cursor, limit=PAGE_SIZE)
        records.extend(page.records)
        truncated = truncated or page.window_truncated
        cursor = page.next_cursor
        if not cursor:
            return records[::-1], truncated  # pages run newest first


def settled_trail(sandbox, sleep=time.sleep, interval: float = 5, max_reads: int = 8) -> tuple[list, bool]:
    """Re-reads the trail until two reads in a row agree, since records land a few seconds after the work."""
    sleep(interval)  # the last operation may not have landed yet
    records, truncated = read_trail(sandbox)
    for _ in range(max_reads - 1):
        sleep(interval)
        again, truncated = read_trail(sandbox)
        if len(again) == len(records):
            return again, truncated
        records = again
    return records, truncated


def _clip(text: str, width: int) -> str:
    """Shortens a cell to its column width."""
    return text if len(text) <= width else text[: width - 3] + "..."


def timeline(records) -> list[str]:
    """Renders records as one aligned line each: time, operation, program, target, outcome, duration, credential."""
    lines = [COLUMNS.format("UTC", "operation", "program", "target", "outcome", "took", "credential")]
    for r in records:
        outcome = r.outcome.value if r.reason_code in (None, "ok") else f"{r.outcome.value} ({r.reason_code})"
        took = "" if r.duration_ms is None else f"{r.duration_ms}ms"
        lines.append(COLUMNS.format(
            r.at.strftime("%H:%M:%S"), r.tool or "-", _clip(r.command or "-", 10), _clip(r.target or "-", 30),
            outcome, took, (r.caller_source or "-")[:8]))
    return lines


def summary(records) -> list[str]:
    """Totals the trail: operation and error counts, then how many operations each credential made."""
    errors = sum(1 for r in records if r.outcome.value != "success")
    lines = [f"{len(records)} operations, {errors} ended in an error. Operations per credential:"]
    for caller, count in Counter(r.caller_source or "unknown" for r in records).most_common():
        lines.append(f"  {caller}: {count}")
    return lines


def report(client, name: str, log=print, sleep=time.sleep) -> int:
    """Looks the sandbox up by name and prints its timeline and summary; 0 only when something was recorded."""
    from neevai import NotFoundError

    try:
        sandbox = client.sandboxes.get(name)
    except NotFoundError:
        log(f"No sandbox named {name} in this project. Run this before the sandbox is deleted.")
        return 1
    records, truncated = settled_trail(sandbox, sleep=sleep)
    if not records:
        log(f"No operations recorded in {name} yet.")
        return 1
    for line in timeline(records) + [""] + summary(records):
        log(line)
    if truncated:
        log("The sandbox is older than the trail's retention, so its earliest operations are not shown.")
    return 0


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and prints the sandbox's audit trail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sandbox_name", help="the x-sandbox-name your coding agent used")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    try:
        with NeevAI() as client:
            return report(client, args.sandbox_name)
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        print(f"Failed: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".rstrip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
