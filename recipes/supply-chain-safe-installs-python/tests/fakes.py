"""In-memory stand-ins for the SDK sandbox and client, shaped like the pieces safe_install.py touches."""
import json
from types import SimpleNamespace


def audit_record(command="pip3", target=None, outcome="success", tool="exec"):
    """Builds one audit record; outcome exposes .value, as the real SDK's enum does."""
    return SimpleNamespace(tool=tool, command=command, target=target, outcome=SimpleNamespace(value=outcome))


# A phone-home marker that recorded a blocked attempt: the TCP connection to the collector never opened.
BLOCKED_MARKER = {"url": "https://example.org/collect", "secret_len": 34, "connected": False,
                  "sent_ok": False, "error": "OSError: [Errno 101] Network is unreachable"}


class FakeSandbox:
    """Mimics an SDK sandbox: files backed by a dict, pip/python execs scripted, audit pages served."""

    def __init__(self, marker=BLOCKED_MARKER, legit_exit=0, import_exit=0,
                 audit_pages=None, ready_error=None):
        self.name = "safe-install-test"
        self.store = {}
        self.execs = []
        self.deleted = False
        self._marker = marker
        self._legit_exit = legit_exit
        self._import_exit = import_exit
        self._ready_error = ready_error
        self.audit_queries = []
        # Each audit() call pops one page; a single page repeats so extra polls still get records.
        self._audit_pages = list(audit_pages) if audit_pages is not None else [
            [audit_record(), audit_record()]]
        self.files = SimpleNamespace(write=self._write, read_text=self._read_text)

    def _write(self, path, content, cwd=None):
        self.store[path] = content
        return {"bytes_written": len(content)}

    def _read_text(self, path, cwd=None):
        key = path[len("/workspace/"):] if path.startswith("/workspace/") else path
        if key in self.store:
            return self.store[key]
        if path == "/workspace/phone_home.json":
            if self._marker is None:
                raise FileNotFoundError(path)
            return json.dumps(self._marker)
        raise FileNotFoundError(path)

    def wait_until_ready(self, timeout_ms=None):
        if self._ready_error:
            raise self._ready_error
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        args = list(args or [])
        self.execs.append((command, args, cwd))
        if command == "pip3" and any("evilpkg" in a for a in args):
            return SimpleNamespace(exit_code=0, stdout="Successfully installed evilpkg-0.0.1", stderr="")
        if command == "pip3" and "--target" in args:
            return SimpleNamespace(exit_code=self._legit_exit, stdout="Successfully installed", stderr="")
        if command == "python3":
            return SimpleNamespace(exit_code=self._import_exit, stdout="2.32.0" if self._import_exit == 0 else "",
                                   stderr="" if self._import_exit == 0 else "ModuleNotFoundError")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    def audit(self, from_=None, limit=None, **kwargs):
        self.audit_queries.append(from_)
        page = self._audit_pages[0] if len(self._audit_pages) == 1 else self._audit_pages.pop(0)
        return SimpleNamespace(records=page, next_cursor=None)

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params and egress convenience args."""

    def __init__(self, sandbox):
        self.created = []

        def create(params, org_id=None, project_id=None, *, allow_internet=None, allow_egress=None):
            self.created.append({"params": params, "allow_egress": allow_egress, "allow_internet": allow_internet})
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)
