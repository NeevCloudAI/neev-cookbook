"""Eval rollouts from one golden snapshot: every (task, model) rollout starts from the same sandbox state."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from agent import run_agent
from tasks import GOLDEN_DIR, TASKS

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODELS = ("glm-4-7", "minimax-m3")
PARALLEL = 2  # rollouts at once; with the golden that is at most 3 sandboxes alive
READY_TIMEOUT_MS = 300_000
SNAPSHOT_TIMEOUT_S = 300
SERVICE_TIMEOUT_S = 60
GRADE_TIMEOUT_MS = 60_000
HEALTH = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"
FINGERPRINT = "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12"


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


def _one_line(e: BaseException) -> str:
    """Formats an error as one line, naming the real cause."""
    cause = _root_cause(e)
    return f"{type(cause).__name__}: {str(cause).splitlines()[0] if str(cause) else ''}".rstrip(": ")


class Sandboxes:
    """Every sandbox this run asked for, so cleanup finds even one whose create was interrupted mid-call."""

    def __init__(self, client, log):
        self.client, self.log = client, log
        self.live: dict[str, object | None] = {}  # name -> handle, or None while the create call is in flight
        self.failed: list[str] = []

    async def create(self, params: dict):
        """Creates a sandbox and waits until it is Ready."""
        self.live[params["name"]] = None  # kept even if create raises: a retried create may still have made it
        sandbox = await self.client.sandboxes.create(params)
        self.live[params["name"]] = sandbox
        await sandbox.wait_until_ready(timeout_ms=READY_TIMEOUT_MS)
        return sandbox

    async def delete(self, name: str) -> None:
        """Deletes one sandbox by name, at most once; a failure is recorded and reported, never raised.

        An interrupted delete puts the sandbox back, so the final cleanup tries it again.
        """
        from neevai.errors import NotFoundError

        if name not in self.live:
            return
        sandbox = self.live.pop(name)  # taken before any await, so a concurrent cleanup cannot delete it twice
        try:
            if sandbox is None:  # the create failed or was interrupted: look the sandbox up by its unique name
                try:
                    sandbox = await self.client.sandboxes.get(name)
                except NotFoundError:
                    self.log(f"   {name} was not found, so there is nothing to delete.")
                    return
            try:
                await sandbox.delete()
            except NotFoundError:  # already gone, e.g. a retried delete whose first attempt succeeded
                pass
            self.log(f"   {name} deleted.")
        except asyncio.CancelledError:
            self.live[name] = sandbox
            raise
        except Exception as e:  # keep going; tell the user what to clean up
            self.failed.append(name)
            self.log(f"   Could not delete sandbox {name} ({_one_line(e)}); delete it from the console.")


async def exec_ok(sandbox, argv: list[str], what: str, timeout_ms: int = 60_000, stdin: str | None = None) -> str:
    """Runs one command in the sandbox and returns its stdout; a non-zero exit raises with the last stderr line."""
    result = await sandbox.exec(argv, timeout_ms=timeout_ms, stdin=stdin)
    if result.exit_code != 0:
        lines = result.stderr.strip().splitlines() or [f"exit code {result.exit_code}"]
        raise RuntimeError(f"{what} failed: {lines[-1]}")
    return result.stdout.strip()


async def environment_state(sandbox, sleep, clock) -> dict:
    """The state a rollout must start from: the service's boot id and write count, and a hash of every workspace file."""
    deadline = clock() + SERVICE_TIMEOUT_S
    while (result := await sandbox.exec(["python3", "-c", HEALTH], timeout_ms=15_000)).exit_code != 0:
        if clock() >= deadline:
            raise RuntimeError(f"the inventory service in {sandbox.name} did not answer within {SERVICE_TIMEOUT_S}s")
        await sleep(1)
    health = json.loads(result.stdout)
    files = await exec_ok(sandbox, ["sh", "-c", FINGERPRINT], "hashing the workspace")
    return {"boot_id": health["boot_id"], "writes": health["writes"], "files": files}


def _describe_state(state: dict) -> str:
    """One-line summary of an environment state for the progress log."""
    return f"service boot {state['boot_id']}, {state['writes']} writes, files {state['files']}"


async def set_up_golden(sandbox, log, sleep, clock) -> dict:
    """Writes the golden workspace, starts the inventory service, and returns the state every rollout must match."""
    paths = sorted(p for p in GOLDEN_DIR.rglob("*") if p.is_file())
    for path in paths:
        await sandbox.files.write(path.relative_to(GOLDEN_DIR).as_posix(), path.read_text())
    log(f"   wrote {len(paths)} files: {', '.join(p.relative_to(GOLDEN_DIR).as_posix() for p in paths)}")
    process = await sandbox.processes.start(["python3", "inventory_service.py"])
    state = await environment_state(sandbox, sleep, clock)
    log(f"   started the inventory service ({process.id}); golden state: {_describe_state(state)}")
    return state


async def take_snapshot(client, sandbox, sleep, clock):
    """Snapshots the sandbox and waits while it is Pending or Running; anything but Ready is an error."""
    snap = await sandbox.snapshot({"name": sandbox.name})
    deadline = clock() + SNAPSHOT_TIMEOUT_S
    while snap.status.value in ("Pending", "Running"):
        if clock() >= deadline:
            raise RuntimeError(f"snapshot {snap.id} did not become Ready within {SNAPSHOT_TIMEOUT_S}s")
        await sleep(1)
        snap = await client.sandboxes.get_snapshot(snap.id)
    if snap.status.value != "Ready":
        raise RuntimeError(f"snapshot {snap.id} is {snap.status.value}: {snap.error_message}")
    return snap


async def side_effects(sandbox, golden: dict) -> str:
    """Describes what the agent changed in its own sandbox: what a shared environment would pass to the next rollout."""
    try:
        writes = json.loads(await exec_ok(sandbox, ["python3", "-c", HEALTH], "asking the service", 15_000))["writes"]
        service = f"{writes} service write{'' if writes == 1 else 's'}"
    except Exception:  # the agent may have stopped the service; that is a side effect too
        service = "the service not answering"
    try:
        files = "unchanged" if await exec_ok(sandbox, ["sh", "-c", FINGERPRINT], "hashing") == golden["files"] else "changed"
    except Exception:  # informational only: never turn a gradeable rollout into an error
        files = "not readable"
    return f"{service}, files {files}"


async def grade(sandbox, task) -> tuple[bool, str]:
    """Runs the task's checker in the sandbox, from stdin so the agent never saw or could edit it."""
    out = await exec_ok(sandbox, ["python3", "-I", "-"], "grading", timeout_ms=GRADE_TIMEOUT_MS, stdin=task.checker)
    try:
        verdict = json.loads(out.splitlines()[-1])
        return bool(verdict["passed"]), str(verdict["detail"])
    except (IndexError, ValueError, KeyError, TypeError):
        raise RuntimeError(f"the checker printed no verdict: {out[-200:]!r}") from None


async def rollout(boxes: Sandboxes, connect, model_client, snapshot_id: str, golden: dict, name: str,
                  task, model: str, log, sleep, clock) -> dict:
    """One rollout: a new sandbox from the snapshot, a check it matches the golden, the agent, the grade, delete.

    Any error becomes an "error" row rather than an exception, so one broken rollout never stops the others.
    """
    row = {"task": task.id, "model": model, "sandbox": name, "result": "error", "detail": "",
           "steps": 0, "seconds": 0.0, "tokens": 0, "stop": "", "side_effects": ""}
    say = lambda line: log(f"   [{task.id} / {model}] {line}")  # noqa: E731
    try:
        started = clock()
        sandbox = await boxes.create({"name": name, "restore": snapshot_id, "egress": {"mode": "deny_all"}})
        state = await environment_state(sandbox, sleep, clock)
        if state != golden:
            raise RuntimeError(f"{name} did not start from the golden state ({_describe_state(state)})")
        say(f"{name} ready in {clock() - started:.1f}s, same as the golden: {_describe_state(state)}")
        try:
            async with connect(name) as session:
                run = await run_agent(session, model_client, model, task.prompt, log=say)
        except BaseExceptionGroup as group:  # the MCP client's task group wraps errors; surface the real one
            raise _root_cause(group) from None
        row.update(steps=run.steps, seconds=run.seconds, tokens=run.tokens, stop=run.stop)
        row["side_effects"] = await side_effects(sandbox, golden)
        passed, detail = await grade(sandbox, task)
        row.update(result="pass" if passed else "fail", detail=detail)
        say(f"{row['result'].upper()} in {run.steps} steps, {run.seconds:.1f}s ({run.stop}): {detail}")
        say(f"left behind {row['side_effects']}; deleting it so no other rollout sees them")
    except Exception as e:
        row["detail"] = _one_line(e)
        say(f"ERROR: {row['detail']}")
    finally:
        await boxes.delete(name)
    return row


def results_grid(rows: list[dict]) -> list[str]:
    """The results as plain lines: one per rollout, then a total per model."""
    lines = [f"   {'task':<16}{'model':<14}{'result':<8}{'steps':>6}{'seconds':>9}{'tokens':>9}"]
    for r in rows:
        lines.append(f"   {r['task']:<16}{r['model']:<14}{r['result']:<8}{r['steps']:>6}{r['seconds']:>9.1f}{r['tokens']:>9,}")
    for model in dict.fromkeys(r["model"] for r in rows):
        mine = [r for r in rows if r["model"] == model]
        lines.append(f"   {model}: {sum(r['result'] == 'pass' for r in mine)} of {len(mine)} passed, "
                     f"{sum(r['steps'] for r in mine)} steps, {sum(r['seconds'] for r in mine):.1f}s, "
                     f"{sum(r['tokens'] for r in mine):,} tokens")
    return lines


async def evaluate(client, model_client, connect, models, tasks=TASKS, out_path: str = "results.json",
                   log=print, sleep=asyncio.sleep, clock=time.monotonic) -> int:
    """Builds the golden, snapshots it, runs every (task, model) rollout from it, and always cleans up.

    Returns 0 when every rollout ran and was graded (a fail is a result) and everything was deleted.
    The golden stays alive until the rollouts end: deleting it would delete its snapshot.
    """
    prefix = f"eval-roll-{secrets.token_hex(3)}"
    boxes = Sandboxes(client, log)
    snapshot_id = None
    code = 1
    try:
        log("1. Creating the golden sandbox (no internet access)...")
        golden = await boxes.create({"name": f"{prefix}-golden", "egress": {"mode": "deny_all"}})
        log("2. Setting up the golden environment: task files and a running inventory service...")
        golden_state = await set_up_golden(golden, log, sleep, clock)
        log("3. Taking a memory snapshot of the golden (files, memory and running processes)...")
        started = clock()
        snapshot_id = str((await take_snapshot(client, golden, sleep, clock)).id)
        log(f"   snapshot {snapshot_id} Ready in {clock() - started:.1f}s")
        pairs = [(task, model) for task in tasks for model in models]
        log(f"4. Running {len(pairs)} rollouts ({len(tasks)} tasks x {len(models)} models), {PARALLEL} at a time, "
            f"each in a new sandbox from the snapshot...")
        limit = asyncio.Semaphore(PARALLEL)

        async def one(i, task, model):
            async with limit:
                return await rollout(boxes, connect, model_client, snapshot_id, golden_state, f"{prefix}-r{i}",
                                     task, model, log, sleep, clock)

        # A task group, unlike gather, waits for every rollout's cleanup when Ctrl+C cancels the run.
        async with asyncio.TaskGroup() as group:
            jobs = [group.create_task(one(i, t, m)) for i, (t, m) in enumerate(pairs, 1)]
        rows = [job.result() for job in jobs]
        log("5. Results:")
        for line in results_grid(rows):
            log(line)
        Path(out_path).write_text(json.dumps({"snapshot_id": snapshot_id, "golden": golden_state, "rollouts": rows}, indent=2))
        errors = [r for r in rows if r["result"] == "error"]
        log(f"6. Wrote {out_path}." + (f" {len(errors)} rollouts did not complete." if errors else ""))
        code = 1 if errors else 0
    except asyncio.CancelledError:  # Ctrl+C: asyncio cancels the run; clean up below, then let it end
        log("Interrupted; cleaning up.")
        raise
    except Exception as e:  # one line instead of a traceback
        log(f"Failed: {_one_line(e)}")
    finally:
        golden_name = f"{prefix}-golden"
        for name in [n for n in boxes.live if n != golden_name]:  # rollouts an interrupted run left behind
            await boxes.delete(name)
        if snapshot_id is not None:
            try:
                await client.sandboxes.delete_snapshot(snapshot_id)
                log(f"   Snapshot {snapshot_id} deleted.")
            except Exception as e:  # deleting the golden below removes it anyway
                log(f"   Could not delete snapshot {snapshot_id} ({_one_line(e)}); it goes with the golden sandbox.")
        await boxes.delete(golden_name)
    return 1 if boxes.failed else code


def run(new_client, model_client, connect, models, out_path: str = "results.json", log=print, **kw) -> int:
    """Runs the evaluation on a fresh event loop; Ctrl+C exits 130 after the cleanup has run."""
    async def go():
        async with new_client() as client:
            return await evaluate(client, model_client, connect, models, out_path=out_path, log=log, **kw)

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 130


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS),
                        help=f"comma-separated models to compare (default {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--out", default="results.json", help="where to write the results (default results.json)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import AsyncNeevAI
    from openai import AsyncOpenAI

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("--models needs at least one model name.", file=sys.stderr)
        return 2
    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    return run(AsyncNeevAI, model_client, mcp_connect(os.environ["NEEV_API_KEY"]), models, args.out)


if __name__ == "__main__":
    sys.exit(main())
