"""Disposable integration tests: bring up an app with real Postgres and Redis in a sandbox, test it, tear it all down."""
from __future__ import annotations

import os
import re
import secrets
import sys
import time
from pathlib import Path

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
TEMPLATE = "sb-ubuntu-26-04-docker"
PROJECT_DIR = Path(__file__).parent / "sample-app"
REMOTE_DIR = "/workspace/sample-app"
VERSIONS = "docker version --format '{{.Server.Version}}' && docker compose version --short"
COMPOSE_UP = "docker compose --progress plain up -d --wait"
RUN_TESTS = "docker compose run --rm tests"
COMPOSE_DOWN = "docker compose down -v"
POLL_S = 2
TAIL_LINES = 15


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def project_files(root: Path) -> dict[str, bytes]:
    """Returns every file under root, keyed by its path relative to root."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class LineBuffer:
    """Joins log chunks into whole lines; a chunk can end mid-line or carry several lines."""

    def __init__(self):
        self.partial = ""

    def feed(self, data: str) -> list[str]:
        *lines, self.partial = (self.partial + data).split("\n")
        return lines

    def flush(self) -> list[str]:
        rest, self.partial = self.partial, ""
        return [rest] if rest else []


def compose_event(line: str) -> str | None:
    """Turns the one-per-service lines of `compose up` output into a short event; layer progress returns None."""
    line = line.strip()
    if m := re.fullmatch(r"(\w+) Pulled", line):
        return f"{m[1]} image pulled"
    if m := re.fullmatch(r"Container (?:.*-)?(\w+)-\d+\s+Healthy", line):
        return f"{m[1]} healthy"
    return None


def count_results(lines: list[str]) -> tuple[int, int] | None:
    """Reads (passed, failed) from the `bun test` summary, or None if the run ended before printing it."""
    text = "\n".join(lines)
    passed, failed = re.search(r"^\s*(\d+) pass$", text, re.M), re.search(r"^\s*(\d+) fail$", text, re.M)
    return (int(passed[1]), int(failed[1])) if passed and failed else None


def run_step(sandbox, command: str, timeout_s: float, show, log, clock, sleep) -> tuple[bool, list[str]]:
    """Runs a command in the project folder as a background process, polling its log so no call waits in silence.

    Each output line goes to show(line). Returns (exited 0, all lines); on failure the last lines are printed.
    """
    process = sandbox.processes.start(["sh", "-c", command], cwd=REMOTE_DIR)
    buffers = {"stdout": LineBuffer(), "stderr": LineBuffer()}
    lines: list[str] = []
    cursor, deadline, exit_code = None, clock() + timeout_s, None
    while True:
        page = sandbox.processes.logs(process.id, cursor=cursor)
        cursor = page.cursor
        for entry in page.entries:
            for line in buffers[entry.stream].feed(entry.data):
                lines.append(line)
                show(line)
        if page.state == "exited":
            exit_code = sandbox.processes.get(process.id).exit_code
            break
        if clock() >= deadline:
            sandbox.processes.kill(process.id)
            break
        sleep(POLL_S)
    for buffer in buffers.values():
        for line in buffer.flush():
            lines.append(line)
            show(line)
    if exit_code != 0:
        log(f"   `{command}` " + (f"exited with {exit_code}" if exit_code is not None else f"timed out after {timeout_s:g}s"))
        for line in [l for l in lines if l.strip()][-TAIL_LINES:]:
            log(f"   > {line}")
    return exit_code == 0, lines


def run(client, log=print, clock=time.monotonic, sleep=time.sleep) -> int:
    """Creates a Docker sandbox, runs the sample app's integration tests in it, tears down, and always deletes it."""
    sandbox = None
    try:
        log(f"1. Creating a sandbox from the {TEMPLATE} template...")
        start = clock()
        sandbox = client.sandboxes.create({"name": f"compose-tests-{secrets.token_hex(4)}", "sandbox_template_id": TEMPLATE})
        sandbox.wait_until_ready(timeout_ms=300_000)
        docker, compose = sandbox.exec(["sh", "-c", VERSIONS]).stdout.split()
        compose = compose.split("+")[0]  # distro builds append a suffix, e.g. 2.40.3+ds1-0ubuntu1
        log(f"   Ready in {clock() - start:.0f}s: Docker {docker}, Compose {compose}")

        files = project_files(PROJECT_DIR)
        log(f"2. Uploading the sample app: {', '.join(files)}")
        for path, data in files.items():
            sandbox.files.write(f"{REMOTE_DIR}/{path}", data)

        log("3. Starting Postgres, Redis and the API (docker compose up --wait)...")
        start, seen = clock(), set()

        def show_event(line: str) -> None:
            # --wait reports each service healthy twice: once for its dependents, once at the end.
            if (event := compose_event(line)) and event not in seen:
                seen.add(event)
                log(f"   | {event} ({clock() - start:.0f}s)")

        up, _ = run_step(sandbox, COMPOSE_UP, 300, show_event, log, clock, sleep)
        if not up:
            return 1
        log(f"   All services healthy in {clock() - start:.0f}s")

        log("4. Running the integration tests against them...")
        start = clock()

        def show_result(line: str) -> None:
            if line.startswith(("(pass)", "(fail)")):
                log(f"   | {line}")

        tests_ok, lines = run_step(sandbox, RUN_TESTS, 120, show_result, log, clock, sleep)
        counts = count_results(lines)
        if counts:
            log(f"   {counts[0]} passed, {counts[1]} failed in {clock() - start:.0f}s")

        log("5. Tearing it all down (docker compose down -v)...")
        start = clock()
        down, _ = run_step(sandbox, COMPOSE_DOWN, 120, lambda _: None, log, clock, sleep)
        if down:
            log(f"   Containers and volumes removed in {clock() - start:.0f}s")
        return 0 if tests_ok and down else 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        log(f"Failed: {type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}")
        return 1
    finally:
        if sandbox is not None:
            sandbox.delete()
            log("   Sandbox deleted.")


def main() -> int:
    """Checks the environment and runs the recipe."""
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(client)


if __name__ == "__main__":
    sys.exit(main())
