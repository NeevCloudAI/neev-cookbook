"""Coding tutor with a shared box: a student (over SSH) and an AI tutor (over MCP) share one sandbox across sessions."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tutor import TutorFailed, review

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
PORT = 8000
EXERCISE_DIR = Path(__file__).parent / "exercise"
EXERCISE_FILES = ("EXERCISE.md", "app.py", "check.py", "index.html")
# The student's first attempt: it works for "hello world" but miscounts repeated spaces and empty text.
STUDENT_EDIT = ("raise NotImplementedError", 'return len(text.split(" "))')
PROBE = "/count?text=two%20%20spaces"
APP_TIMEOUT_S = 60
PAUSE_TIMEOUT_S = 120
RESUME_TIMEOUT_S = 180
# Host keys of a loopback tunnel are not worth remembering, and the WebSocket under it is already authenticated.
SSH_OPTIONS = ("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR")


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


def ssh_command(tunnel) -> str:
    """The command a person types to open a shell in the sandbox through the tunnel."""
    return " ".join(["ssh", "-p", str(tunnel.port), *SSH_OPTIONS, f"root@{tunnel.host}"])


def ssh_run(tunnel, command: str, stdin: str | None = None):
    """Runs one shell command in the sandbox with the local ssh client, through the tunnel."""
    argv = ["ssh", "-p", str(tunnel.port), *SSH_OPTIONS, "-o", "BatchMode=yes", f"root@{tunnel.host}", command]
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=60)


def student_code() -> str:
    """The starter app.py with the student's edit applied."""
    starter = (EXERCISE_DIR / "app.py").read_text()
    if STUDENT_EDIT[0] not in starter:
        raise RuntimeError("exercise/app.py no longer has the line the student edits")
    return starter.replace(*STUDENT_EDIT)


def fetch_count(url: str) -> tuple[int | None, dict]:
    """GETs a URL: (HTTP status, JSON body), or (None, {"error": ...}) if unreachable."""
    import httpx

    try:
        response = httpx.get(url, timeout=10)
    except httpx.HTTPError as e:
        return None, {"error": f"unreachable ({type(e).__name__})"}
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"error": response.text[:200]} if response.text else {}


def describe(status: int | None, body: dict) -> str:
    """Renders one answer for the progress log."""
    if status is None:
        return f"not answering: {body.get('error')}"
    return f"{status} {json.dumps(body)}" if body else str(status)


def wait_for_app(fetch, url, sleep, clock) -> tuple[int | None, dict]:
    """Polls the URL until it answers with anything but a gateway error, or APP_TIMEOUT_S passes."""
    deadline = clock() + APP_TIMEOUT_S
    while True:
        status, body = fetch(url)
        if status not in (None, 502, 503, 504) or clock() >= deadline:
            return status, body
        sleep(1)


def answered(status, body) -> bool:
    """Reports whether the exercise app answered the probe with a word count."""
    return status == 200 and isinstance(body.get("words"), int)


def ssh_ok(tunnel, ssh, command: str, stdin: str | None = None) -> str:
    """Runs a command over SSH and returns its stdout; a non-zero exit is an error."""
    result = ssh(tunnel, command, stdin)
    if result.returncode != 0:
        raise RuntimeError(f"ssh '{command}' exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def wait_for_phase(sandbox, phase: str, sleep, clock) -> float:
    """Polls the sandbox until it reports phase, returning the seconds it took; bounded by PAUSE_TIMEOUT_S."""
    started = clock()
    while sandbox.refresh().phase != phase:
        if clock() - started >= PAUSE_TIMEOUT_S:
            raise RuntimeError(f"sandbox {sandbox.name} did not reach {phase} within {PAUSE_TIMEOUT_S}s")
        sleep(1)
    return clock() - started


def wait_until_answering(sandbox, sleep, clock) -> float:
    """Polls a resumed sandbox until a command runs in it, returning the seconds it took.

    The reported phase can lag behind a resume, so a command that runs is the signal, not the phase.
    """
    started = clock()
    while True:
        try:
            sandbox.refresh()
            if sandbox.exec(["true"]).exit_code == 0:
                return clock() - started
        except Exception:  # not answering yet
            pass
        if clock() - started >= RESUME_TIMEOUT_S:
            raise RuntimeError(f"sandbox {sandbox.name} did not answer within {RESUME_TIMEOUT_S}s of the resume")
        sleep(1)


def snapshot_files(sandbox) -> dict[str, str]:
    """Reads the exercise files back, to tell whether the tutor changed any of them."""
    return {name: sandbox.files.read_text(name) for name in EXERCISE_FILES}


async def _review(connect, sandbox_name: str, model_client, model: str, log) -> list[str]:
    """Opens the MCP session for the sandbox and runs the tutor over it."""
    try:
        async with connect(sandbox_name) as session:
            return await review(session, model_client, model, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(client, model_client, model: str, connect, keep_minutes: float = 0, log=print, ssh=ssh_run,
        fetch=fetch_count, sleep=time.sleep, clock=time.monotonic, wait=time.sleep) -> int:
    """Runs two tutoring sessions in one sandbox with a pause between them, and always deletes it.

    Returns 0 only when the preview URL answered, the tutor gave hints without touching the
    student's files, and the files, the server and the URL all survived the pause and resume.
    """
    sandbox = None
    proven = False
    try:
        log("1. Creating the student's sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": f"tutor-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"   {sandbox.name}")
        log(f"2. Seeding the exercise: {', '.join(EXERCISE_FILES)}")
        for name in EXERCISE_FILES:
            sandbox.files.write(name, (EXERCISE_DIR / name).read_text())

        log("3. Session 1: the student opens an SSH tunnel and saves their attempt")
        attempt = student_code()
        with sandbox.ssh() as tunnel:
            log(f"   {ssh_command(tunnel)} 'cat > app.py'")
            ssh_ok(tunnel, ssh, "cat > app.py", attempt)
        log(f"   their count_words: {STUDENT_EDIT[1]}")

        log(f"4. Starting their app on 0.0.0.0:{PORT}...")
        process = sandbox.processes.start(["python3", "app.py"])
        url = sandbox.get_url(PORT).rstrip("/")
        status, body = wait_for_app(fetch, url + PROBE, sleep, clock)
        log(f"   preview: {url}")
        log(f"   GET {PROBE} -> {describe(status, body)}")
        if not answered(status, body):
            raise RuntimeError(f"the preview URL did not answer: {describe(status, body)}")
        first_answer = (status, body)
        before = snapshot_files(sandbox)

        log(f"5. Asking {model} to review the work (it can read and run, not write)...")
        hints = asyncio.run(_review(connect, sandbox.name, model_client, model, log))
        changed = [name for name, content in snapshot_files(sandbox).items() if before[name] != content]
        if changed:
            raise RuntimeError(f"the tutor changed the student's files: {', '.join(changed)}")
        if (state := sandbox.processes.get(process.id).state) != "running":
            raise RuntimeError(f"the student's server is {state} after the review; the tutor must not stop it")
        log("   Hints for the student:")
        for i, hint in enumerate(hints, 1):
            log(f"   {i}. {hint}")
        notes = "# Hints from your tutor\n\n" + "".join(f"{i}. {h}\n" for i, h in enumerate(hints, 1))
        sandbox.files.write("TUTOR_NOTES.md", notes)
        log("   The tutor left the student's files untouched; the hints are saved in TUTOR_NOTES.md.")

        log("6. Pausing the sandbox between sessions...")
        sandbox.pause()
        log(f"   Paused in {wait_for_phase(sandbox, 'Paused', sleep, clock):.1f}s; "
            f"the preview URL now answers {describe(*fetch(url + PROBE))}")

        log("7. Resuming for the next session...")
        sandbox.resume()
        log(f"   answering {wait_until_answering(sandbox, sleep, clock):.1f}s after the resume call")

        log("8. Session 2: checking the student's work survived")
        with sandbox.ssh() as tunnel:
            code_now = ssh_ok(tunnel, ssh, "cat app.py")
            notes_now = ssh_ok(tunnel, ssh, "cat TUTOR_NOTES.md")
            state = sandbox.processes.get(process.id).state
            status, body = wait_for_app(fetch, url + PROBE, sleep, clock)
            checks = [
                ("files: app.py over SSH still has the student's edit", code_now == attempt),
                (f"files: TUTOR_NOTES.md has the {len(hints)} hints", notes_now == notes),
                (f"process: {process.id} is {state}", state == "running"),
                (f"preview: the same URL answers {describe(status, body)}", (status, body) == first_answer),
            ]
            for label, ok in checks:
                log(f"   {'ok    ' if ok else 'FAILED'} {label}")
            if not all(ok for _, ok in checks):
                log("The sandbox did not come back as the student left it.")
                return 1
            proven = True
            log("Session state survived the pause and resume: files, server and preview URL.")
            if keep_minutes > 0:
                log(f"   Keeping the sandbox, SSH tunnel and preview URL open for {keep_minutes:g} min. "
                    "Press Ctrl+C to stop sooner.")
                log(f"   Student shell: {ssh_command(tunnel)}")
                log(f"   Their app:     {url}")
                wait(keep_minutes * 60)
        return 0
    except KeyboardInterrupt:
        return 0 if proven else 130
    except TutorFailed as e:
        log(f"The tutor did not give hints: {e}")
        return 1
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
    """Parses arguments, checks the environment and the ssh client, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=float, default=0,
                        help="minutes to keep the sandbox, SSH tunnel and preview URL open at the end (default 0)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    if shutil.which("ssh") is None:
        print("No ssh client found on PATH; install OpenSSH. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect, args.keep)


if __name__ == "__main__":
    sys.exit(main())
