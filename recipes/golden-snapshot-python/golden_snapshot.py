"""Skip setup with a golden snapshot: set a sandbox up once, snapshot it, and start every worker from that snapshot."""
from __future__ import annotations

import argparse
import itertools
import json
import os
import secrets
import sys
import time
from pathlib import Path

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
PACKAGES = ("pandas", "scikit-learn")
PACKAGE_HOSTS = ["pypi.org", "files.pythonhosted.org"]
WORKERS = 3
APP_DIR = Path(__file__).parent / "app"
VENV_PYTHON = "/workspace/.venv/bin/python"
MANIFEST = "golden.json"
PING = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5).read().decode())"
IMPORT_CHECK = "import pandas, sklearn; print(f'pandas {pandas.__version__}, scikit-learn {sklearn.__version__}')"
SETUP_TIMEOUT_MS = 600_000
READY_TIMEOUT_MS = 300_000
SNAPSHOT_TIMEOUT_S = 300
SERVICE_TIMEOUT_S = 120


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def run_step(sandbox, argv: list[str], what: str) -> str:
    """Runs one command in the sandbox and returns its stdout; a non-zero exit raises with the last stderr line."""
    result = sandbox.exec(argv, timeout_ms=SETUP_TIMEOUT_MS)
    if result.exit_code != 0:
        lines = result.stderr.strip().splitlines() or [f"exit code {result.exit_code}"]
        raise RuntimeError(f"{what} failed: {lines[-1]}")
    return result.stdout.strip()


def ask_service(sandbox) -> dict | None:
    """Calls the service on 127.0.0.1:8000 from inside the sandbox; None while it is not answering."""
    result = sandbox.exec(["python3", "-c", PING], timeout_ms=15_000)
    if result.exit_code != 0:
        return None
    return json.loads(result.stdout)


def wait_for_service(sandbox, sleep, clock) -> dict:
    """Polls the service until it answers; raises after SERVICE_TIMEOUT_S."""
    deadline = clock() + SERVICE_TIMEOUT_S
    while (answer := ask_service(sandbox)) is None:
        if clock() >= deadline:
            raise RuntimeError(f"the service in {sandbox.name} did not answer within {SERVICE_TIMEOUT_S}s")
        sleep(1)
    return answer


def set_up(sandbox, log, sleep, clock) -> tuple[object, dict]:
    """The slow setup a worker would otherwise repeat: install packages, build the dataset, start the service."""
    started = clock()
    run_step(sandbox, ["sh", "-c", f"python3 -m venv .venv && .venv/bin/pip install -q --disable-pip-version-check {' '.join(PACKAGES)}"],
             f"installing {', '.join(PACKAGES)}")
    log(f"   installed {', '.join(PACKAGES)} into /workspace/.venv in {clock() - started:.1f}s")
    for name in ("make_data.py", "server.py"):
        sandbox.files.write(name, (APP_DIR / name).read_text())
    started = clock()
    rows = run_step(sandbox, [VENV_PYTHON, "make_data.py"], "generating the dataset")
    log(f"   generated data.csv with {int(rows):,} rows in {clock() - started:.1f}s")
    started = clock()
    process = sandbox.processes.start([VENV_PYTHON, "server.py"])
    answer = wait_for_service(sandbox, sleep, clock)
    log(f"   started the service ({process.id}); it trained its model and answered after {clock() - started:.1f}s")
    return process, answer


def take_snapshot(client, sandbox, sleep, clock):
    """Snapshots the sandbox and waits while it is Pending or Running; anything but Ready is an error."""
    snap = sandbox.snapshot({"name": sandbox.name.replace("-src-", "-")})
    deadline = clock() + SNAPSHOT_TIMEOUT_S
    while snap.status.value in ("Pending", "Running"):
        if clock() >= deadline:
            raise RuntimeError(f"snapshot {snap.id} did not become Ready within {SNAPSHOT_TIMEOUT_S}s")
        sleep(1)
        snap = client.sandboxes.get_snapshot(snap.id)
    if snap.status.value != "Ready":
        raise RuntimeError(f"snapshot {snap.id} is {snap.status.value}: {snap.error_message}")
    return snap


def same_memory(answer: dict, manifest: dict) -> bool:
    """True when the answer comes from the golden's own process: same boot id, and one request past the snapshot.

    The boot id is drawn when the process starts and the request count lives only in its memory, so a
    restarted service would show a new id and start counting from zero.
    """
    return answer.get("boot_id") == manifest["boot_id"] and answer.get("served") == manifest["served"] + 1


def read_manifest(sandbox, snapshot_id: str) -> dict:
    """Reads golden.json, which the build wrote into the golden sandbox just before the snapshot."""
    try:
        return json.loads(sandbox.files.read_text(MANIFEST))
    except Exception:
        raise RuntimeError(f"snapshot {snapshot_id} has no {MANIFEST}: it was not made by this recipe") from None


def check_worker(worker, manifest: dict, answer: dict, log) -> bool:
    """Checks one worker against the golden state recorded in the manifest and prints each result."""
    process = worker.processes.get(manifest["process_id"])
    rows = int(run_step(worker, ["wc", "-l", "data.csv"], "counting data.csv").split()[0]) - 1  # minus the header
    packages = worker.exec([VENV_PYTHON, "-c", IMPORT_CHECK], timeout_ms=60_000)
    egress = (worker.data.get("egress") or {}).get("mode")
    checks = [
        (f"process: the golden's service answered (boot id {answer.get('boot_id')}, PID {answer.get('pid')}), "
         f"{process.process_id} {process.state}",
         answer.get("pid") == manifest["pid"] and answer.get("boot_id") == manifest["boot_id"] and process.state == "running"),
        (f"memory: request count {answer.get('served')} (was {manifest['served']} at the snapshot, +1 for this request), "
         f"model trained in the golden still loaded (accuracy {answer.get('accuracy')})",
         same_memory(answer, manifest) and answer.get("accuracy") == manifest["accuracy"]),
        (f"files: data.csv has {rows:,} rows", rows == manifest["rows"]),
        (f"packages: {packages.stdout.strip() or packages.stderr.strip()[-120:]} import in a new process",
         packages.exit_code == 0),
        (f"network: egress {egress}, as this worker was created (the golden's allow-list is not carried over)",
         egress == "deny_all"),
    ]
    for label, ok in checks:
        log(f"     {'ok    ' if ok else 'FAILED'} {label}")
    return all(ok for _, ok in checks)


def delete_sandbox(sandbox, log) -> None:
    """Deletes a sandbox; a failure is reported with the name to delete by hand, never raised."""
    try:
        sandbox.delete()
        log(f"   {sandbox.name} deleted.")
    except Exception as e:  # keep the run's exit code; tell the user what to clean up
        log(f"   Could not delete sandbox {sandbox.name} ({type(e).__name__}: {e}); delete it from the console.")


def run_workers(client, snapshot_id: str, suffix: str, log, sleep, clock) -> list[tuple[float, float]]:
    """Starts each worker from the snapshot, checks it, and deletes it before the next; returns (start, cold) times.

    One worker at a time keeps at most two sandboxes alive, the golden source and the current worker.
    """
    timings = []
    for i in range(1, WORKERS + 1):
        worker = None
        try:
            started = clock()
            worker = client.sandboxes.create({"name": f"golden-w{i}-{suffix}", "restore": snapshot_id,
                                              "egress": {"mode": "deny_all"}})
            worker.wait_until_ready(timeout_ms=READY_TIMEOUT_MS)
            answer = wait_for_service(worker, sleep, clock)
            start_s = clock() - started
            manifest = read_manifest(worker, snapshot_id)
            cold_s = manifest["cold_setup_s"]
            log(f"   worker {i} {worker.name}: service answering {start_s:.1f}s after create "
                f"(cold setup took {cold_s:.1f}s, saved {cold_s - start_s:.1f}s)")
            if not check_worker(worker, manifest, answer, log):
                raise RuntimeError(f"worker {worker.name} did not start from the golden state")
            timings.append((start_s, cold_s))
        finally:
            if worker is not None:
                delete_sandbox(worker, log)
    return timings


def run(client, log=print, sleep=time.sleep, clock=time.monotonic, snapshot_id: str | None = None,
        keep_snapshot: bool = False) -> int:
    """Builds (or reuses) the golden snapshot, proves every worker starts from it, and cleans up; 0 only when all did."""
    step = itertools.count(1)
    suffix = secrets.token_hex(3)
    golden = None
    snapshot_ready = False
    try:
        if snapshot_id is None:
            log(f"{next(step)}. Creating the golden sandbox (egress allow-list: {', '.join(PACKAGE_HOSTS)})...")
            started = clock()
            golden = client.sandboxes.create({"name": f"golden-src-{suffix}"}, allow_egress=PACKAGE_HOSTS)
            golden.wait_until_ready(timeout_ms=READY_TIMEOUT_MS)
            log(f"{next(step)}. Cold setup, the work every worker would otherwise repeat...")
            process, answer = set_up(golden, log, sleep, clock)
            cold_s = clock() - started
            log(f"   cold setup: {cold_s:.1f}s from create to a warm service: {json.dumps(answer)}")
            manifest = {**answer, "process_id": process.id, "cold_setup_s": round(cold_s, 1)}
            golden.files.write(MANIFEST, json.dumps(manifest))
            log(f"{next(step)}. Taking a memory snapshot of the golden sandbox (files, memory and running processes)...")
            started = clock()
            snapshot_id = str(take_snapshot(client, golden, sleep, clock).id)
            snapshot_ready = True
            log(f"   snapshot {snapshot_id} Ready in {clock() - started:.1f}s")
        else:
            log(f"{next(step)}. Using golden snapshot {snapshot_id}...")
            status = client.sandboxes.get_snapshot(snapshot_id).status.value
            if status != "Ready":
                raise RuntimeError(f"snapshot {snapshot_id} is {status}, not Ready")
        log(f"{next(step)}. Starting {WORKERS} workers from the snapshot, each with no internet access...")
        timings = run_workers(client, snapshot_id, suffix, log, sleep, clock)
        start_avg = sum(s for s, _ in timings) / len(timings)
        cold_avg = sum(c for _, c in timings) / len(timings)
        log(f"Every worker started from the golden snapshot: {start_avg:.1f}s on average instead of "
            f"{cold_avg:.1f}s of cold setup, {cold_avg - start_avg:.1f}s saved per worker.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # one line instead of a traceback
        log(f"Failed: {type(e).__name__}: {e}")
        return 1
    finally:
        if golden is None and snapshot_id is not None:
            log(f"   Left snapshot {snapshot_id} in place: this run did not create it.")
        if golden is not None:
            if keep_snapshot and snapshot_ready:
                log(f"   Kept snapshot {snapshot_id} and its source sandbox {golden.name}: deleting that sandbox deletes "
                    f"the snapshot, and it is billed while it runs. Reuse it with: python golden_snapshot.py --snapshot {snapshot_id}")
            else:
                if snapshot_ready:
                    try:
                        client.sandboxes.delete_snapshot(snapshot_id)
                        log(f"   Snapshot {snapshot_id} deleted.")
                    except Exception as e:  # deleting the source sandbox below removes it anyway
                        log(f"   Could not delete snapshot {snapshot_id} ({type(e).__name__}: {e}); it goes with its sandbox.")
                delete_sandbox(golden, log)


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", metavar="ID", help="reuse a golden snapshot an earlier --keep-snapshot run kept")
    parser.add_argument("--keep-snapshot", action="store_true",
                        help="keep the snapshot (and the golden sandbox it belongs to) for later runs")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(client, snapshot_id=args.snapshot, keep_snapshot=args.keep_snapshot)


if __name__ == "__main__":
    sys.exit(main())
