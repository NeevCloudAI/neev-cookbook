"""Prints what a coding agent did in a NeevCloud sandbox, read from the sandbox's audit trail."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
PAGE_SIZE = 100
COLUMNS = "{:<8}  {:<14}  {:<10}  {:<30}  {:<22}  {:>7}  {}"
# Operations whose process resource is the program they ran, not a process they named.
PROGRAM_OPS = {"exec", "process.start", "pty_command", "ssh"}


def missing_env(environ, required=REQUIRED_ENV) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in required if not environ.get(name)]


def read_trail(sandbox) -> tuple[list, bool]:
    """Reads every page of the sandbox's trail since it was created; returns events oldest first and the truncation flag."""
    events, cursor, truncated = [], None, False
    # Without from_ the API returns only the last 24 hours; the sandbox's creation time covers its whole life.
    since = sandbox.data.get("created_at")
    while True:
        page = sandbox.audit(from_=since, cursor=cursor, limit=PAGE_SIZE)
        events.extend(page.data)
        truncated = truncated or page.window_truncated
        cursor = page.next_cursor
        if not cursor:
            return events[::-1], truncated  # pages run newest first


def settled_trail(sandbox, sleep=time.sleep, interval: float = 5, max_reads: int = 8) -> tuple[list, bool]:
    """Re-reads the trail until two reads in a row agree, since events land a few seconds after the work."""
    sleep(interval)  # the last operation may not have landed yet
    events, truncated = read_trail(sandbox)
    for _ in range(max_reads - 1):
        sleep(interval)
        again, truncated = read_trail(sandbox)
        if len(again) == len(events):
            return again, truncated
        events = again
    return events, truncated


def _clip(text: str, width: int) -> str:
    """Shortens a cell to its column width."""
    return text if len(text) <= width else text[: width - 3] + "..."


def operation(event) -> str:
    """The call the event records, such as fs.read or exec."""
    return event.api.operation or ""


def _names(event, *kinds: str) -> list[str]:
    return [r.name for r in event.resources or [] if getattr(r.type, "value", r.type) in kinds and r.name]


def program(event) -> str:
    """The program an exec, process start or terminal command ran, without its arguments."""
    names = _names(event, "process", "channel") if operation(event) in PROGRAM_OPS else []
    return names[-1] if names else ""


def target(event) -> str:
    """The path the event acted on, or for a process call the process it named."""
    files = _names(event, "file")
    if files:
        return files[0]
    return "" if operation(event) in PROGRAM_OPS else next(iter(_names(event, "process")), "")


def caller(event) -> str:
    """Who made the call: the API key it was made with, or the user or platform that made it."""
    user = event.actor.user
    return user.credential_uid or user.uid or "unknown"


def timeline(events) -> list[str]:
    """Renders events as one aligned line each: time, operation, program, target, outcome, duration, caller."""
    lines = [COLUMNS.format("UTC", "operation", "program", "target", "outcome", "took", "by")]
    for e in events:
        status = e.status.lower()
        outcome = status if e.status_code in (None, "ok") else f"{status} ({e.status_code})"
        took = "" if e.duration is None else f"{e.duration}ms"
        at = datetime.fromtimestamp(e.time / 1000, tz=timezone.utc)
        lines.append(COLUMNS.format(
            at.strftime("%H:%M:%S"), operation(e) or "-", _clip(program(e) or "-", 10), _clip(target(e) or "-", 30),
            outcome, took, caller(e)[:8]))
    return lines


def summary(events) -> list[str]:
    """Totals the trail: operation and failure counts, then how many operations each caller made."""
    failures = sum(1 for e in events if e.status != "Success")
    lines = [f"{len(events)} operations, {failures} failed. Operations per caller (API key or user):"]
    for who, count in Counter(caller(e) for e in events).most_common():
        lines.append(f"  {who}: {count}")
    return lines


def report(client, name: str, log=print, sleep=time.sleep) -> int:
    """Looks the sandbox up by name and prints its timeline and summary; 0 only when something was recorded."""
    from neevai import NotFoundError

    try:
        sandbox = client.sandboxes.get(name)
    except NotFoundError:
        log(f"No sandbox named {name} in this project. Run this before the sandbox is deleted.")
        return 1
    events, truncated = settled_trail(sandbox, sleep=sleep)
    if not events:
        log(f"No operations recorded in {name} yet.")
        return 1
    for line in timeline(events) + [""] + summary(events):
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
