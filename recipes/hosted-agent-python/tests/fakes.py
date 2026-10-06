"""In-memory stand-ins for a hosted agent, the machine behind it, and its audit trail."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

FIXED = "// fixed\n"
TESTS_FAILING = "ℹ tests 4\nℹ pass 1\nℹ fail 3\n"
TESTS_PASSING = "ℹ tests 4\nℹ pass 4\nℹ fail 0\n"
KEY_ID = "1234abcd-5678-4def-9abc-def012345678"


def event(kind, **part):
    """One line of `opencode run --format json` output."""
    return json.dumps({"type": kind, "timestamp": 1, "part": {"type": kind, **part}}) + "\n"


class FakeMachine:
    """The agent's machine: workspace files and supervised processes, the coding CLI among them.

    `effect` is what the coding agent does to the workspace when its process ends: "fix", "edit-tests",
    "nothing", or "hang" (the process never ends). `log_pages` is the output it prints, one list per poll.
    """

    def __init__(self, effect="fix", log_pages=None):
        self.workspace = {}
        self.effect = effect
        self.log_pages = list(log_pages if log_pages is not None else [
            [("stdout", event("step_start")), ("stdout", event("tool_use", tool="edit", state={
                "status": "completed", "input": {"filePath": "/workspace/slugify.js"}})[:30])],
            [("stdout", event("tool_use", tool="edit", state={
                "status": "completed", "input": {"filePath": "/workspace/slugify.js"}})[30:]),
             ("stdout", event("step_finish", tokens={"input": 7000, "output": 100})),
             ("stdout", event("tool_use", tool="bash", state={"status": "completed", "input": {"command": "node --test slugify.test.js"}})),
             ("stdout", event("text", text="All tests pass.\nDone.")),
             ("stdout", event("step_finish", tokens={"input": 8000, "output": 20})),
             ("stderr", "\x1b[91mwarning\x1b[0m: slow model\n")]])
        self.ops = []  # (tool, command, target) for every call, which the fake audit trail reports
        self.started, self.killed = [], []
        self.running = set()  # ids of processes still running; a reboot loses them all
        self.reboot_on_resume = False
        self.interrupt_on_logs = False
        self.files = SimpleNamespace(write=self._write, read_text=self._read)
        self.processes = SimpleNamespace(start=self._start, logs=self._logs, get=self._get, kill=self._kill)

    def _write(self, path, content):
        self.ops.append(("fs.write", None, path))
        self.workspace[path] = content
        return {"bytes_written": len(content)}

    def _read(self, path):
        self.ops.append(("fs.read", None, path))
        if path not in self.workspace:
            raise FileNotFoundError(path)
        return self.workspace[path]

    def exec(self, command, args=None, **kw):
        argv = list(command) + list(args or [])
        self.ops.append(("exec", argv[0], None))
        if argv[:2] == ["node", "--test"]:
            fixed = self.workspace.get("slugify.js") == FIXED
            return SimpleNamespace(stdout=TESTS_PASSING if fixed else TESTS_FAILING, stderr="", exit_code=0 if fixed else 1)
        return SimpleNamespace(stdout="", stderr=f"unknown command {argv}", exit_code=127)

    def _start(self, program, args=None, cwd=None, env=None, stdin=None):
        self.ops.append(("process.start", program[0], None))
        self.started.append({"argv": list(program), "cwd": cwd, "env": dict(env or {})})
        process_id = f"proc-{len(self.started)}"
        self.running.add(process_id)
        return SimpleNamespace(id=process_id, state="running", exit_code=None)

    def _logs(self, process_id, cursor=None):
        self.ops.append(("process.logs", None, process_id))
        if self.interrupt_on_logs:
            raise KeyboardInterrupt
        entries = self.log_pages.pop(0) if self.log_pages else []
        exited = not self.log_pages and self.effect != "hang"
        if exited:
            self.running.discard(process_id)
            self._finish()
        return SimpleNamespace(entries=[SimpleNamespace(stream=s, data=d) for s, d in entries],
                               cursor=(cursor or 0) + len(entries), dropped=False, state="exited" if exited else "running")

    def _finish(self):
        """Applies what the coding agent did to the workspace."""
        if self.effect == "fix":
            self.workspace["slugify.js"] = FIXED
        elif self.effect == "edit-tests":
            self.workspace["slugify.test.js"] = "// tests removed\n"

    def _get(self, process_id, wait=False):
        self.ops.append(("process.get", None, process_id))
        if process_id in self.running:
            return SimpleNamespace(process_id=process_id, state="running", exit_code=None)
        return SimpleNamespace(process_id=process_id, state="exited", exit_code=143 if process_id in self.killed else 0)

    def _kill(self, process_id, signal=None):
        self.ops.append(("process.kill", None, process_id))
        self.killed.append(process_id)
        self.running.discard(process_id)
        return True


class FakeAgent:
    """Mimics the agent handle: lifecycle, the backing machine, and an audit trail that lags behind the calls."""

    def __init__(self, machine=None, name="hosted-agent-1", pause_statuses=("Pausing", "Paused"),
                 resume_statuses=("Paused", "Provisioning", "Ready"), audit_lag=1):
        self.name = name
        self.status = "Provisioning"
        self.machine = machine or FakeMachine()
        self.pause_statuses = list(pause_statuses)
        self.resume_statuses = list(resume_statuses)  # starts with Paused: the status can lag the resume call
        self.audit_lag = audit_lag  # how many audit reads miss the newest exec records
        self.deleted = False
        self.delete_error = None
        self.calls = []

    def wait_until_ready(self, timeout_ms=None):
        self.calls.append("wait_until_ready")
        self.status = "Ready"
        return self

    def sandbox(self):
        return self.machine

    def pause(self):
        self.calls.append("pause")
        self.status = self.pause_statuses.pop(0)
        return self

    def refresh(self):
        queue = self.resume_statuses if "resume" in self.calls else self.pause_statuses
        if queue:
            self.status = queue.pop(0)
        return self

    def resume(self):
        self.calls.append("resume")
        self.status = "Paused"
        if self.machine.reboot_on_resume:
            self.machine.running.clear()
        return self

    def audit(self, cursor=None, limit=None, **kw):
        """Returns the calls newest first, two per page; while lagging, the trail ends before the last exec."""
        ops = list(self.machine.ops)
        if self.audit_lag > 0:
            self.audit_lag -= 1
            last_exec = max(i for i, op in enumerate(ops) if op[0] == "exec")
            ops = ops[:last_exec]
        records = [SimpleNamespace(at=datetime(2026, 1, 1, 7, 0, i % 60, tzinfo=timezone.utc), tool=t, command=c,
                                   target=tg, outcome=SimpleNamespace(value="success"), caller_source=KEY_ID)
                   for i, (t, c, tg) in enumerate(ops)][::-1]
        start = int(cursor or 0)
        page = records[start:start + 2]
        more = start + 2 < len(records)
        return SimpleNamespace(records=page, next_cursor=str(start + 2) if more else None)

    def delete(self):
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().agents.create, recording the params and the egress convenience argument."""

    def __init__(self, agent=None, create_error=None):
        self.agent = agent or FakeAgent()
        self.created = []

        def create(params, allow_egress=None, **kw):
            if create_error:
                raise create_error
            self.created.append({**params, "allow_egress": allow_egress})
            return self.agent

        self.agents = SimpleNamespace(create=create)


def fake_time():
    """Returns (sleep, clock) where sleeping advances the clock, so waits finish instantly."""
    now = [0.0]
    return (lambda s: now.__setitem__(0, now[0] + s)), (lambda: now[0])
