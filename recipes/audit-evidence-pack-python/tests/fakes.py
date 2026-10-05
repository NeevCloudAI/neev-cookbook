"""In-memory stand-ins for the NeevAI client, its sandboxes and audit trails, and the sandbox MCP session."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from neevai import AuditRecord, AuditTrail, NotFoundError

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CRED_A = "c0de0001-0000-7000-8000-00000000000a"
CRED_B = "01b20d7f-0000-7000-8000-00000000000b"
DEMO_T = T0 + timedelta(minutes=8)  # inside the window a demo run at T0+10min exports


def record(n, tool, target=None, command=None, outcome="success", reason="ok", cred=CRED_A, at=None, **kw):
    """Builds a real SDK AuditRecord, so the tests read the same fields the server returns."""
    return AuditRecord(at=at or T0 + timedelta(seconds=n), id=f"rec-{n}", tool=tool, target=target, command=command,
                       outcome=outcome, reason_code=reason, caller_source=cred, request_id=f"req-{n}", **kw)


def not_found(name):
    """The error the API raises for a sandbox that does not exist, or no longer exists."""
    return NotFoundError(404, {"code": "not_found", "message": f'sandbox "{name}" not found in this project'}, "req-404")


class Trails:
    """The project's audit trails by sandbox name, served newest first in pages like the real API."""

    def __init__(self, retention_days=30):
        self.by_name = {}
        self.ids = {}
        self.calls = []  # (name, from_, to, cursor, limit) per page read
        self.retention_days = retention_days
        self.endless = False  # hand out a cursor forever, to test the page cap

    def add(self, name, records, sandbox_id=None):
        self.by_name.setdefault(name, []).extend(records)
        self.ids.setdefault(name, sandbox_id or f"0000000{len(self.ids)}-0000-7000-8000-000000000000")

    def audit(self, id, *, from_=None, to=None, cursor=None, limit=None, **_):
        self.calls.append((id, from_, to, cursor, limit))
        if id not in self.by_name:
            raise not_found(id)
        start = datetime.fromisoformat(from_)
        oldest = datetime.fromisoformat(to) - timedelta(days=self.retention_days)
        window = [r for r in self.by_name[id] if max(start, oldest) <= r.at <= datetime.fromisoformat(to)]
        window.sort(key=lambda r: r.at, reverse=True)
        offset = int(cursor or 0)
        page = window[offset:offset + limit]
        more = self.endless or offset + limit < len(window)
        return AuditTrail.model_validate({
            "sandbox_id": self.ids[id], "from": max(start, oldest).isoformat(), "to": to,
            "retention_days": self.retention_days, "window_truncated": start < oldest,
            "next_cursor": str(offset + limit) if more else None,
            "records": [r.model_dump(mode="json") for r in page]})


class FakeSandbox:
    """One demo sandbox: SDK file, exec and process calls append audit records, as the real trail does."""

    def __init__(self, name, trails, events):
        self.name, self.trails, self.events = name, trails, events
        self.id = str(uuid.uuid4())
        self.workspace = {}
        self.deleted = False
        self.delete_error = None
        trails.add(name, [], self.id)
        self.files = SimpleNamespace(write=self._write, read_text=self._read, remove=self._remove)
        self.processes = SimpleNamespace(start=self._start)

    def _log(self, tool, target=None, command=None, outcome="success", reason="ok"):
        n = len(self.trails.by_name[self.name])
        self.trails.by_name[self.name].append(record(n, tool, target, command, outcome, reason,
                                                     at=DEMO_T + timedelta(seconds=n)))

    def wait_until_ready(self, timeout_ms=None):
        return self

    def _write(self, path, content):
        self.workspace[path] = content
        self._log("fs.write", path)
        return {"bytes_written": len(content)}

    def _read(self, path):
        if path not in self.workspace:
            self._log("fs.read", path, outcome="error", reason="not_found")
            raise not_found(path)
        self._log("fs.read", path)
        return self.workspace[path]

    def _remove(self, path, recursive=False):
        self.workspace.pop(path, None)
        self._log("fs.remove", path)

    def exec(self, command, args=None, **kw):
        argv = list(command) + list(args or [])
        self._log("exec", command=argv[0])
        return SimpleNamespace(stdout="", stderr="", exit_code=2 if "no-such-dir" in argv else 0)

    def _start(self, program, **kw):
        self._log("process.start", command=program[0])
        return SimpleNamespace(id="proc-1")

    def delete(self):
        self.events.append(("delete", self.name))
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes: create, list and audit, recording create params and the call order."""

    def __init__(self, trails=None):
        self.trails = trails or Trails()
        self.created, self.sandboxes_made, self.events = [], [], []

        def create(params):
            self.created.append(params)
            sb = FakeSandbox(params["name"], self.trails, self.events)
            self.sandboxes_made.append(sb)
            return sb

        def audit(id, **kw):
            self.events.append(("audit", id))
            return self.trails.audit(id, **kw)

        def list(name=None, **kw):
            return SimpleNamespace(items=[s for s in self.sandboxes_made if s.name == name and not s.deleted])

        self.sandboxes = SimpleNamespace(create=create, audit=audit, list=list)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; each call lands in that sandbox's trail like the real server."""

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = []

    async def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        sb = self.sandbox
        if name == "fs_write":
            sb.workspace[args["path"]] = args["content"]
            sb._log("fs.write", args["path"])
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in sb.workspace:
                sb._log("fs.read", args["path"], outcome="error", reason="not_found")
                sb._log("")  # the real server currently adds a blank record after a refused call
                return _err(f'the sandbox refused this call: not_found: file not found: "{args["path"]}"')
            sb._log("fs.read", args["path"])
            return _ok({"content": sb.workspace[args["path"]]})
        if name == "exec":
            sb._log("exec")  # the real server currently records MCP exec without its program
            return _ok({"stdout": "ok\n", "stderr": "", "exit_code": 0})
        if name == "process_start":
            sb._log("process.start", command=args["program"])
            return _ok({"process_id": "proc_1", "state": "running"})
        return _err(f"unexpected tool {name}")
