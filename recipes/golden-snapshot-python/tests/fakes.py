"""In-memory stand-ins for NeevAI().sandboxes, sandboxes and their snapshots, shaped like the real SDK."""
import copy
import json
from types import SimpleNamespace

from neevai import SnapshotStatus
from neevai.errors import NotFoundError

ROWS = 200_000


def _result(stdout="", stderr="", exit_code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


class FakeSandbox:
    """One sandbox: workspace files plus the churn service's process memory, which snapshots capture together."""

    def __init__(self, client, name, egress, state=None, service_starts_after=2):
        self.client, self.name = client, name
        self.egress = egress
        self.workspace, self.service = copy.deepcopy(state) if state else ({}, None)
        self.pings_until_up = service_starts_after  # the service needs a few polls to train its model
        self.deleted = False
        self.execs = []
        self.files = SimpleNamespace(write=self._write, read_text=self._read)
        self.processes = SimpleNamespace(start=self._start, get=self._get)

    @property
    def data(self):
        return {"egress": self.egress}

    def wait_until_ready(self, timeout_ms=None):
        if self.client.hang_on_ready:
            raise KeyboardInterrupt
        return self

    def _write(self, path, content):
        self.workspace[path] = content

    def _read(self, path):
        if path not in self.workspace:
            raise FileNotFoundError(path)
        return self.workspace[path]

    def exec(self, command, args=None, timeout_ms=None, **kw):
        argv = list(command) + list(args or [])
        self.execs.append(argv)
        if argv[:2] == ["sh", "-c"] and "pip install" in argv[2]:
            if self.client.install_fails:
                return _result(stderr="ERROR: Could not find a version that satisfies the requirement pandas", exit_code=1)
            self.workspace[".venv"] = "venv"
            return _result()
        if argv[1:] == ["make_data.py"]:
            self.workspace["data.csv"] = ROWS
            return _result(stdout=f"{ROWS}\n")
        if argv[:2] == ["python3", "-c"]:
            return self._ping()
        if argv[1:3] == ["-c", "import pandas, sklearn; print(f'pandas {pandas.__version__}, scikit-learn {sklearn.__version__}')"]:
            ok = ".venv" in self.workspace
            return _result(stdout="pandas 3.0.6, scikit-learn 1.9.1\n" if ok else "", exit_code=0 if ok else 1)
        if argv == ["wc", "-l", "data.csv"]:
            return _result(stdout=f"{self.workspace['data.csv'] + 1} data.csv\n") if "data.csv" in self.workspace else _result(exit_code=1)
        return _result(stderr=f"unknown command {argv}", exit_code=127)

    def _ping(self):
        """What the service answers on 127.0.0.1:8000, mirroring app/server.py."""
        if self.service is None or self.pings_until_up > 0:
            self.pings_until_up -= 1
            return _result(stderr="urllib.error.URLError: <urlopen error [Errno 111] Connection refused>", exit_code=1)
        self.service["served"] += 1
        return _result(stdout=json.dumps(self.service) + "\n")

    def _start(self, program, **kw):
        self.started = program
        self.service = {"boot_id": f"boot-{self.name}", "pid": 24, "served": 0, "rows": ROWS, "accuracy": 0.9731}
        return SimpleNamespace(id="proc-1", state="running")

    def _get(self, process_id):
        return SimpleNamespace(process_id=process_id, state="running" if self.service else "exited")

    def snapshot(self, params=None):
        snap_id = f"snap-{len(self.client.snapshots) + 1}"
        self.client.snapshots[snap_id] = {"source": self, "state": copy.deepcopy((self.workspace, self.service))}
        return SimpleNamespace(id=snap_id, status=SnapshotStatus.Pending, error_message=None)

    def delete(self):
        """Deletes the sandbox; like the platform, its snapshots go with it."""
        self.deleted = True
        self.client.live.remove(self)
        for snap_id in [s for s, v in self.client.snapshots.items() if v["source"] is self]:
            del self.client.snapshots[snap_id]


class FakeClient:
    """Mimics NeevAI().sandboxes: create (cold or with restore), get_snapshot and delete_snapshot."""

    def __init__(self, snapshot_statuses=("Pending", "Running", "Ready"), restore_keeps_process=True):
        self.created, self.live, self.all = [], [], []
        self.snapshots = {}
        self.deleted_snapshots = []
        self.max_live = 0
        self.snapshot_statuses = list(snapshot_statuses)
        self.restore_keeps_process = restore_keeps_process
        self.install_fails = False
        self.hang_on_ready = False
        self.on_restore = lambda sandbox: None  # lets a test damage what a worker gets from the snapshot
        self.sandboxes = SimpleNamespace(create=self._create, get_snapshot=self._get_snapshot,
                                         delete_snapshot=self._delete_snapshot)

    def _create(self, params, allow_egress=None):
        self.created.append({**params, "allow_egress": allow_egress})
        egress = params.get("egress") or ({"mode": "allow_list", "allow": [{"host": h} for h in allow_egress]}
                                          if allow_egress else {"mode": "deny_all"})
        state = None
        if "restore" in params:
            if params["restore"] not in self.snapshots:
                raise NotFoundError(404, {"code": "not_found", "message": "Snapshot not found."}, None)
            workspace, service = self.snapshots[params["restore"]]["state"]
            state = (workspace, service if self.restore_keeps_process else None)
        sandbox = FakeSandbox(self, params["name"], egress, state, service_starts_after=0 if state else 2)
        if state:
            self.on_restore(sandbox)
        self.live.append(sandbox)
        self.all.append(sandbox)
        self.max_live = max(self.max_live, len(self.live))
        return sandbox

    def _get_snapshot(self, snapshot_id):
        if snapshot_id not in self.snapshots:
            raise NotFoundError(404, {"code": "not_found", "message": "Snapshot not found."}, None)
        statuses = self.snapshot_statuses
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return SimpleNamespace(id=snapshot_id, status=SnapshotStatus(status),
                               error_message="capture failed" if status == "Failed" else None)

    def _delete_snapshot(self, snapshot_id):
        self.deleted_snapshots.append(snapshot_id)
        del self.snapshots[snapshot_id]
