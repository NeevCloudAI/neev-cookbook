"""In-memory stand-ins for NeevAI().sandboxes and a model client, shaped like the real SDK objects."""
import json
import threading
import time
from types import SimpleNamespace

from neevai import DeadlineExceededError, NotFoundError

HANG = object()  # an exec that runs until its sandbox is deleted

PERFECT = {"total": 6, "passed": 6, "failed": [], "timeouts": 0, "blocked": [], "isolated": True}

# What the harness reports for each shipped submission, as the sandbox runs printed it.
REPORTS = {
    "correct.py": PERFECT,
    "partial.py": {**PERFECT, "passed": 4, "failed": ["test_punctuation_separates_words", "test_ties_break_alphabetically"]},
    "wrong.py": {**PERFECT, "passed": 0, "failed": ["test_a", "test_b", "test_c", "test_d", "test_e", "test_f"]},
    "infinite_loop.py": {**PERFECT, "passed": 5, "failed": ["test_punctuation_separates_words"], "timeouts": 1},
    "cheater.py": {**PERFECT, "passed": 0, "failed": ["test_a", "test_b", "test_c", "test_d", "test_e", "test_f"],
                   "blocked": ["access to the grader's files (test_top_words.py)"]},
    "exfiltrator.py": {**PERFECT, "passed": 0, "failed": ["test_a", "test_b", "test_c", "test_d", "test_e", "test_f"],
                       "timeouts": 6, "blocked": ["network access to collector.example.net"]},
}


class FakeSandbox:
    """One sandbox: a file map, recorded exec calls, and an exec that answers like the harness would."""

    def __init__(self, client, name):
        self.client, self.name = client, name
        self.workspace, self.writes, self.execs = {}, [], []
        self.deleted, self.gone = False, threading.Event()
        self.files = SimpleNamespace(write=self._write)

    def wait_until_ready(self, timeout_ms=None):
        return self

    def _write(self, path, content):
        if self.client.fail_write:
            raise self.client.fail_write
        self.workspace[path] = content
        self.writes.append(path)
        return {"bytes_written": len(content)}

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        self.execs.append({"command": command, "stdin": stdin, "timeout_ms": timeout_ms})
        time.sleep(0.02)  # long enough for the pool's workers to overlap
        behaviour = self.client.behaviour(self.workspace["submission/solution.py"])
        if behaviour is HANG:
            self.gone.wait(5)
            raise RuntimeError("the sandbox was deleted")
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            return behaviour(json.loads(stdin))
        stdout = f"GRADE {json.loads(stdin)['nonce']} {json.dumps(behaviour)}\n"
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)

    def delete(self):
        if self.client.fail_delete:
            raise RuntimeError("delete refused")
        self.client.release(self)
        self.deleted = True
        self.gone.set()


class FakeClient:
    """Mimics NeevAI().sandboxes.create; records params and the most sandboxes alive at once."""

    def __init__(self, behaviours, fail_create=None, fail_delete=False, fail_write=None):
        self.behaviours = behaviours  # submission source -> report dict, exception, HANG, or fn(job) -> exec result
        self.fail_create, self.fail_delete, self.fail_write = fail_create, fail_delete, fail_write
        self.created, self.sandboxes_made, self.deleted_by_name = [], [], []
        self.alive, self.max_alive = 0, 0
        self.lock = threading.Lock()
        self.sandboxes = SimpleNamespace(create=self._create, delete=self._delete_by_name)

    def behaviour(self, source):
        return self.behaviours[source]

    def _delete_by_name(self, name):
        self.deleted_by_name.append(name)
        raise NotFoundError(404, {"code": "not_found", "message": f"sandbox {name} not found"}, None)

    def _create(self, params):
        if self.fail_create:
            raise self.fail_create
        with self.lock:
            self.created.append(params)
            self.alive += 1
            self.max_alive = max(self.max_alive, self.alive)
        sandbox = FakeSandbox(self, params["name"])
        self.sandboxes_made.append(sandbox)
        return sandbox

    def release(self, sandbox):
        with self.lock:
            if not sandbox.deleted:
                self.alive -= 1


def deadline_exceeded():
    """The error the SDK raises when exec runs past its timeout_ms."""
    return DeadlineExceededError(504, {"code": "deadline_exceeded", "message": "exec timed out"}, None)


class FakeModel:
    """Mimics OpenAI().chat.completions.create, answering each request with the next scripted reply."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
