"""Runs the hidden tests inside the sandbox, calling the submission in a separate, unprivileged process.

Layout: grader/harness.py and grader/test_*.py (readable by root only), submission/solution.py.
Reads {"nonce", "timeout_s"} on stdin and prints one line: GRADE <nonce> <json report>.
"""
import json
import os
import signal
import subprocess
import sys
import unittest

GRADER_DIR = os.path.dirname(os.path.abspath(__file__))
SUBMISSION_DIR = os.path.join(os.path.dirname(GRADER_DIR), "submission")
NOBODY = 65534
ISOLATED = os.geteuid() == 0  # only root can run the submission as another user

# Runs in the submission's process: answers one call, and reports any reach for the network or the grader's files.
CHILD = r"""
import json, os, sys
GRADER_DIR = sys.argv[1]
channel = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)  # the submission's prints go to its stderr, never to the answer channel

def report(**message):
    channel.write(json.dumps(message) + "\n")
    channel.flush()

def watch(event, args):
    if event == "socket.getaddrinfo":
        report(blocked=f"network access to {args[0]}")
    elif event == "socket.connect":
        report(blocked=f"network access to {args[1]}")
    elif event in ("open", "os.listdir", "os.scandir") and isinstance(args[0], (str, bytes)):
        path = os.path.realpath(os.fsdecode(args[0]))
        if path == GRADER_DIR or path.startswith(GRADER_DIR + os.sep):
            report(blocked=f"access to the grader's files ({os.path.basename(path)})")

sys.addaudithook(watch)
request = json.loads(sys.stdin.readline())
sys.path.insert(0, os.getcwd())
import solution
report(value=getattr(solution, request["function"])(*request["args"]))
"""


class SubmissionTimeout(Exception):
    """The submission did not answer one call within the per-test time limit."""


class SubmissionError(Exception):
    """The submission crashed, or answered with something that is not a value."""


class Submission:
    """Calls the submission's functions, each in a fresh process, and records timeouts and blocked attempts."""

    def __init__(self):
        self.timeout_s = 3.0
        self.timeouts = 0
        self.blocked = set()

    def __getattr__(self, function):
        """submission.top_words(...) becomes a call into the submission's top_words."""
        return lambda *args: self.call(function, list(args))

    def call(self, function, args):
        """Runs one call as nobody with no grader access; kills it and everything it started on timeout."""
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", CHILD, GRADER_DIR], cwd=SUBMISSION_DIR, env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, preexec_fn=_drop_privileges if ISOLATED else None)
        request = json.dumps({"function": function, "args": args}) + "\n"
        try:
            out, err = child.communicate(request, timeout=self.timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out, out, err = True, "", ""
        for _ in range(5):  # kill until nothing holds the pipes open; this also reaps background leftovers
            _kill_all(child)
            if not timed_out:
                break
            try:
                out, err = child.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
        messages = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        self.blocked.update(m["blocked"] for m in messages if "blocked" in m)
        if timed_out:
            self.timeouts += 1
            raise SubmissionTimeout(f"no answer within {self.timeout_s}s")
        values = [m["value"] for m in messages if "value" in m]
        if not values:
            raise SubmissionError((err.strip().splitlines() or ["no answer"])[-1])
        return values[-1]


def _drop_privileges():
    """Runs in the child before exec: become nobody and cap processes, memory and file size."""
    import resource

    os.setgroups([])
    os.setgid(NOBODY)
    os.setuid(NOBODY)
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 << 20, 10 << 20))


def _kill_all(child):
    """Kills the call's process group and, when isolated, every process the nobody user owns."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if ISOLATED:
        subprocess.run(["pkill", "-KILL", "-u", str(NOBODY)], check=False)


submission = Submission()


def main():
    """Runs every test_*.py next to this file and prints the report tagged with the caller's nonce."""
    job = json.loads(sys.stdin.readline())
    submission.timeout_s = float(job["timeout_s"])
    sys.modules["harness"] = sys.modules[__name__]  # the tests' "from harness import submission" gets this one
    sys.path.insert(0, GRADER_DIR)
    suite = unittest.defaultTestLoader.discover(GRADER_DIR, pattern="test_*.py", top_level_dir=GRADER_DIR)
    result = unittest.TestResult()
    suite.run(result)
    failed = sorted(test.id().rsplit(".", 1)[-1] for test, _ in result.failures + result.errors)
    report = {
        "total": result.testsRun,
        "passed": result.testsRun - len(failed),
        "failed": failed,
        "timeouts": submission.timeouts,
        "blocked": sorted(submission.blocked),
        "isolated": ISOLATED,
    }
    print(f"GRADE {job['nonce']} {json.dumps(report)}", flush=True)


if __name__ == "__main__":
    main()
