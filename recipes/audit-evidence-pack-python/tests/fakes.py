"""In-memory stand-ins for the NeevAI client, its sandboxes and audit trails, and the sandbox MCP session."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from neevai import AuditEvent, AuditTrail, NotFoundError

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CRED_A = "c0de0001-0000-7000-8000-00000000000a"
CRED_B = "01b20d7f-0000-7000-8000-00000000000b"
DEMO_T = T0 + timedelta(minutes=8)  # inside the window a demo run at T0+10min exports


def record(n, tool, target=None, command=None, outcome="success", reason="ok", cred=CRED_A, at=None, sandbox="", **kw):
    """Builds a real SDK AuditEvent shaped as the API returns it, so the tests read the same fields the server sends.

    A path is a file resource for fs operations and a process resource otherwise; a program is a process resource.
    """
    resources = [{"type": "sandbox", "uid": sandbox}]
    if target is not None:
        resources.append({"type": "file" if tool.startswith("fs.") else "process", "name": target})
    if command is not None:
        resources.append({"type": "process", "name": command})
    failed = outcome != "success"
    at = at or T0 + timedelta(seconds=n)
    return AuditEvent.model_validate({
        "class_uid": 6003, "class_name": "API Activity", "category_uid": 6, "category_name": "Application Activity",
        "activity_id": 99, "activity_name": "Other", "type_uid": 600399, "type_name": "API Activity: Other",
        "time": int(at.timestamp() * 1000), "severity_id": 1, "severity": "Informational",
        "status_id": 2 if failed else 1, "status": "Failure" if failed else "Success", "status_code": reason,
        "actor": {"user": {"type": "api_key", "type_id": 99, "uid": cred, "credential_uid": cred}},
        "api": {"operation": tool, "request": {"uid": f"req-{n}"}}, "resources": resources,
        "cloud": {"provider": "NeevCloud"}, "src_endpoint": {"name": "unknown"}, "osint": [],
        "metadata": {"version": "1.9.0", "product": {"name": "Sandbox Audit", "vendor_name": "NeevCloud"}, "uid": f"rec-{n}"},
        **kw})


def event_at(e):
    return datetime.fromtimestamp(e.time / 1000, tz=timezone.utc)


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
        self.ids.setdefault(name, sandbox_id or f"0000000{len(self.ids)}-0000-7000-8000-000000000000")
        for r in records:  # every event names the sandbox it came from
            r.resources[0].uid = self.ids[name]
        self.by_name.setdefault(name, []).extend(records)

    def audit(self, id, *, from_=None, to=None, cursor=None, limit=None, **_):
        self.calls.append((id, from_, to, cursor, limit))
        if id not in self.by_name:
            raise not_found(id)
        start = datetime.fromisoformat(from_)
        oldest = datetime.fromisoformat(to) - timedelta(days=self.retention_days)
        window = [r for r in self.by_name[id] if max(start, oldest) <= event_at(r) <= datetime.fromisoformat(to)]
        window.sort(key=lambda r: r.time, reverse=True)
        offset = int(cursor or 0)
        page = window[offset:offset + limit]
        more = self.endless or offset + limit < len(window)
        return AuditTrail.model_validate({
            "from": max(start, oldest).isoformat(), "to": to,
            "retention_days": self.retention_days, "window_truncated": start < oldest,
            "next_cursor": str(offset + limit) if more else None,
            "data": [r.model_dump(mode="json", exclude_none=True) for r in page]})


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
                                                     at=DEMO_T + timedelta(seconds=n), sandbox=self.trails.ids[self.name]))

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
