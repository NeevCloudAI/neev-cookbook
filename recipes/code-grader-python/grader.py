"""Code grader: grade untrusted submissions in parallel, each in its own sandbox with no internet access."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
ASSIGNMENT_DIR = HERE / "assignment"
SUBMISSIONS_DIR = HERE / "submissions"
EXPECTED_PATH = HERE / "expected.json"
REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
CONCURRENCY = 3  # sandboxes alive at once
TEST_TIMEOUT_S = 3  # per test, enforced by the harness inside the sandbox
RUN_TIMEOUT_MS = 60_000  # per submission, enforced by the sandbox on the whole run
FEEDBACK_BUDGET_S = 120
HINT_OUTCOMES = ("partial", "failed", "timeout")
# chmod first: the grader's files are root-only before any submission code runs; the harness runs it as nobody.
RUN_COMMAND = ["sh", "-c", "chmod 700 grader && exec python3 -I grader/harness.py"]


@dataclass
class Result:
    """One submission's grade, as printed and written to the results file."""

    submission: str
    score: int = 0
    outcome: str = "error"
    seconds: float = 0.0
    detail: str = ""
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    hint: str | None = None


class Sandboxes:
    """Tracks the sandboxes alive across worker threads so each is deleted exactly once, even on Ctrl+C."""

    def __init__(self, log):
        self.log, self.lock, self.stop = log, threading.Lock(), threading.Event()
        self.alive, self.created, self.undeleted = {}, 0, []

    def add(self, sandbox) -> None:
        """Registers a sandbox that now exists and must be deleted."""
        with self.lock:
            self.alive[sandbox.name] = sandbox
            self.created += 1

    def delete(self, sandbox) -> None:
        """Deletes the sandbox unless another thread already did; a failure is remembered and fails the run."""
        with self.lock:
            if self.alive.pop(sandbox.name, None) is None:
                return
        try:
            sandbox.delete()
        except Exception as e:
            self.failed(sandbox.name, e)
        except BaseException:  # interrupted mid-delete: put it back so the next cleanup pass retries it
            with self.lock:
                self.alive[sandbox.name] = sandbox
            raise

    def failed(self, name: str, e: Exception) -> None:
        """Remembers a sandbox that may still exist; it fails the run."""
        with self.lock:
            self.undeleted.append(name)
        self.log(f"   Could not delete sandbox {name} ({one_line(e)}); delete it from the console.")

    def delete_all(self) -> None:
        """Deletes every sandbox still registered, whichever thread created it."""
        with self.lock:
            alive = list(self.alive.values())
        for sandbox in alive:
            self.delete(sandbox)


def create(client, sandboxes: Sandboxes):
    """Creates a deny_all sandbox; if the call fails, deletes it by name in case it was made anyway."""
    from neevai import NotFoundError

    name = f"grader-{secrets.token_hex(4)}"
    try:
        return client.sandboxes.create({"name": name, "egress": {"mode": "deny_all"}})
    except Exception:
        try:
            client.sandboxes.delete(name)
        except NotFoundError:
            pass
        except Exception as e:
            sandboxes.failed(name, e)
        raise


def one_line(e: BaseException) -> str:
    """Names an error in one short line; some gateway errors carry a whole HTML page."""
    lines = str(e).strip().splitlines()
    return f"{type(e).__name__}: {lines[0][:200] if lines else ''}"


def missing_env(environ, feedback: bool) -> list[str]:
    """Returns the required variables that are not set; the model key is required only for --feedback."""
    required = REQUIRED_ENV + (("NEEV_MODEL_API_KEY",) if feedback else ())
    return [name for name in required if not environ.get(name)]


def score(report: dict) -> tuple[int, str]:
    """Turns the harness report into (score out of 100, outcome); any blocked attempt scores 0."""
    total, passed = report["total"], report["passed"]
    if report["blocked"]:
        return 0, "blocked"
    points = round(100 * passed / total) if total else 0
    if total and passed == total:
        return points, "passed"
    if report["timeouts"]:
        return points, "timeout"
    return points, "partial" if passed else "failed"


def parse_report(run, nonce: str) -> dict:
    """Finds the harness's GRADE line carrying this run's nonce; any other line is ignored."""
    prefix = f"GRADE {nonce} "
    for line in run.stdout.splitlines():
        if line.startswith(prefix):
            report = json.loads(line[len(prefix):])
            if not report.get("isolated"):
                raise RuntimeError("the harness could not run the submission as an unprivileged user")
            return report
    last = (run.stderr.strip().splitlines() or ["no output"])[-1]
    raise RuntimeError(f"the harness printed no result (exit {run.exit_code}): {last[:200]}")


def grade_one(client, path: Path, sandboxes: Sandboxes) -> Result:
    """Grades one submission in a new deny_all sandbox and always deletes it before returning."""
    from neevai import DeadlineExceededError

    result, started, sandbox = Result(path.name), time.monotonic(), None
    try:
        if sandboxes.stop.is_set():
            raise RuntimeError("cancelled")
        sandbox = create(client, sandboxes)
        sandboxes.add(sandbox)
        if sandboxes.stop.is_set():
            raise RuntimeError("cancelled")
        sandbox.wait_until_ready(timeout_ms=300_000)
        sandbox.files.write("submission/solution.py", path.read_text())
        # The grader's files go in last, right before the run, and nothing of the submission has run yet.
        sandbox.files.write("grader/harness.py", (HERE / "harness.py").read_text())
        for test in sorted(ASSIGNMENT_DIR.glob("test_*.py")):
            sandbox.files.write(f"grader/{test.name}", test.read_text())
        nonce = secrets.token_hex(16)
        job = json.dumps({"nonce": nonce, "timeout_s": TEST_TIMEOUT_S}) + "\n"
        try:
            run = sandbox.exec(RUN_COMMAND, stdin=job, timeout_ms=RUN_TIMEOUT_MS)
        except DeadlineExceededError:
            result.outcome, result.detail = "timeout", f"the whole run hit the {RUN_TIMEOUT_MS // 1000}s limit"
            return result
        report = parse_report(run, nonce)
        result.score, result.outcome = score(report)
        result.failed, result.blocked = report["failed"], report["blocked"]
        result.detail = describe(result, report)
    except Exception as e:
        result.detail = one_line(e)
    finally:
        if sandbox is not None:
            sandboxes.delete(sandbox)
        result.seconds = round(time.monotonic() - started, 1)
    return result


def describe(result: Result, report: dict) -> str:
    """One short reason for anything but a pass."""
    if result.outcome == "blocked":
        return "blocked: " + "; ".join(report["blocked"])
    if result.outcome == "timeout":
        return f"{report['timeouts']} test(s) hit the {TEST_TIMEOUT_S}s limit"
    if report["failed"] and report["passed"] == 0:
        return f"failed all {report['total']} tests"
    return "failed: " + ", ".join(report["failed"]) if report["failed"] else ""


def make_hinter(model_client, model: str, clock=time.monotonic):
    """Returns hint(source, failed): one short hint from the model, all calls sharing FEEDBACK_BUDGET_S from the first."""
    deadline = []
    problem = (ASSIGNMENT_DIR / "problem.md").read_text()

    def hint(source: str, failed: list[str]) -> str:
        deadline[:] = deadline or [clock() + FEEDBACK_BUDGET_S]
        remaining = deadline[0] - clock()
        if remaining <= 0:
            raise TimeoutError(f"the {FEEDBACK_BUDGET_S}s feedback budget is used up")
        prompt = (f"Assignment:\n{problem}\nStudent's solution.py:\n```python\n{source}\n```\n"
                  f"Hidden tests it failed or timed out on: {', '.join(failed)}.\n"
                  "Give one hint of at most 25 words pointing at the bug. Do not write the fix.")
        response = model_client.chat.completions.create(
            model=model, timeout=min(60.0, remaining),
            messages=[{"role": "system", "content": "You are a concise programming teaching assistant."},
                      {"role": "user", "content": prompt}])
        text = re.sub(r"<think>.*?</think>", "", response.choices[0].message.content or "", flags=re.DOTALL)
        return " ".join(text.split())  # some models put their reasoning inline; keep only the answer

    return hint


def row(r: Result) -> str:
    """Formats one result line."""
    return f"   {r.submission:<17} {r.score:>3}/100  {r.outcome:<8} {r.seconds:5.1f}s  {r.detail}".rstrip()


def run(client, submissions: list[Path], expected: dict, out_path: Path, log=print, hints=None) -> int:
    """Grades every submission at most CONCURRENCY at a time; 0 only if all match the key and all sandboxes are gone."""
    sandboxes = Sandboxes(log)
    results: list[Result] = []
    pool = ThreadPoolExecutor(CONCURRENCY)
    try:
        log(f"1. Grading {len(submissions)} submissions, each in its own sandbox with no internet access, "
            f"at most {CONCURRENCY} at a time...")
        futures = [pool.submit(grade_one, client, path, sandboxes) for path in submissions]
        for future in as_completed(futures):
            results.append(future.result())
            log(row(results[-1]))
    except KeyboardInterrupt:
        sandboxes.stop.set()
        log("Interrupted: deleting the sandboxes still running...")
        while True:  # a second Ctrl+C must not leave a sandbox behind: keep cleaning until done
            try:
                sandboxes.delete_all()
                pool.shutdown(wait=True, cancel_futures=True)
                sandboxes.delete_all()  # any a worker created while the pool drained
                break
            except KeyboardInterrupt:
                log("   Still deleting sandboxes, please wait...")
        return 130
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    results.sort(key=lambda r: r.submission)
    step = 2
    if hints is not None:
        log(f"{step}. Asking the model for one hint per failing submission...")
        step += 1
        for r in results:
            if r.outcome in HINT_OUTCOMES:
                try:
                    r.hint = hints((SUBMISSIONS_DIR / r.submission).read_text(), r.failed)
                    log(f"   {r.submission}: {r.hint}")
                except Exception as e:  # hints are optional: the grades stand without them
                    log(f"   {r.submission}: hint unavailable ({one_line(e)})")
    log(f"{step}. Checking the grades against the answer key ({EXPECTED_PATH.name})...")
    mismatches = [r for r in results if expected.get(r.submission) != {"score": r.score, "outcome": r.outcome}]
    for r in mismatches:
        want = expected.get(r.submission)
        want_text = f"{want['score']} {want['outcome']}" if want else "no entry in the key"
        log(f"   {r.submission}: got {r.score} {r.outcome}, expected {want_text}")
    if not mismatches:
        log(f"   all {len(results)} match")
    out_path.write_text(json.dumps([{k: v for k, v in asdict(r).items() if v is not None} for r in results], indent=2) + "\n")
    log(f"{step + 1}. Wrote {out_path}")
    if mismatches or sandboxes.undeleted:
        log(f"Failed: {len(mismatches)} grade(s) differ from the key, {len(sandboxes.undeleted)} sandbox(es) not deleted.")
        return 1
    log(f"Done: {len(results)} submissions graded, all match the key, {sandboxes.created} sandboxes created and deleted.")
    return 0


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and grades the shipped submissions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback", action="store_true", help="ask a NeevCloud model for one hint per failing submission")
    parser.add_argument("--out", type=Path, default=Path("results.json"), help="where to write the results")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ, args.feedback)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    hints = None
    if args.feedback:
        from openai import OpenAI

        model_client = OpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"], max_retries=0)
        hints = make_hinter(model_client, os.environ.get("MODEL", DEFAULT_MODEL))
    submissions = sorted(SUBMISSIONS_DIR.glob("*.py"))
    expected = json.loads(EXPECTED_PATH.read_text())
    try:
        with NeevAI() as client:
            return run(client, submissions, expected, args.out, hints=hints)
    except KeyboardInterrupt:  # during the hints: no sandbox is left by then
        return 130


if __name__ == "__main__":
    sys.exit(main())
