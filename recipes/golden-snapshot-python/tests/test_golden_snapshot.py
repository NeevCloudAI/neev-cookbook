import json

import pytest

import golden_snapshot
from tests.fakes import FakeClient


def fake_time():
    """Returns (sleep, clock) where sleeping advances the clock, so waits finish instantly."""
    now = [0.0]
    return (lambda s: now.__setitem__(0, now[0] + s)), (lambda: now[0])


def run(client, lines=None, **kw):
    sleep, clock = fake_time()
    log = lines.append if lines is not None else (lambda *_: None)
    return golden_snapshot.run(client, log=log, sleep=sleep, clock=clock, **kw)


def build_kept_snapshot():
    """Builds a golden snapshot with --keep-snapshot and returns (client, snapshot id)."""
    client = FakeClient()
    assert run(client, keep_snapshot=True) == 0
    return client, next(iter(client.snapshots))


def test_missing_env_names_every_missing_variable():
    assert golden_snapshot.missing_env({"NEEV_API_KEY": "k"}) == ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]


def test_main_exits_2_with_message_when_env_missing(monkeypatch, capsys):
    for name in golden_snapshot.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    assert golden_snapshot.main([]) == 2
    assert "NEEV_API_KEY" in capsys.readouterr().err


def test_happy_path_builds_once_and_every_worker_starts_from_the_golden_state():
    client, lines = FakeClient(), []
    assert run(client, lines) == 0
    golden, *workers = client.all
    assert client.created[0]["allow_egress"] == ["pypi.org", "files.pythonhosted.org"]
    assert client.created[0]["name"].startswith("golden-src-")
    assert golden.started == ["/workspace/.venv/bin/python", "server.py"]
    assert len(workers) == golden_snapshot.WORKERS
    for params in client.created[1:]:
        assert params["restore"] == "snap-1"
        assert params["egress"] == {"mode": "deny_all"}
        assert params["name"].startswith("golden-w")
    for worker in workers:  # restored, never set up again
        assert not any("pip install" in " ".join(argv) for argv in worker.execs)
    assert all(sb.deleted for sb in client.all)
    assert client.deleted_snapshots == ["snap-1"]
    assert client.max_live <= 2  # the golden source plus one worker
    out = "\n".join(lines)
    assert out.count("ok    ") == 5 * golden_snapshot.WORKERS
    assert "FAILED" not in out
    assert "saved" in out
    assert "Every worker started from the golden snapshot" in out


def test_manifest_records_the_golden_state_and_cold_setup_time():
    client = FakeClient()
    run(client)
    manifest = json.loads(client.all[0].workspace[golden_snapshot.MANIFEST])
    assert manifest["boot_id"] == "boot-" + client.all[0].name
    assert manifest["process_id"] == "proc-1"
    assert manifest["served"] == 1
    assert manifest["cold_setup_s"] > 0


def test_a_worker_whose_process_did_not_survive_fails_the_run_and_still_cleans_up():
    client, lines = FakeClient(restore_keeps_process=False), []
    assert run(client, lines) == 1
    assert all(sb.deleted for sb in client.all)
    assert client.deleted_snapshots == ["snap-1"]
    assert "did not answer" in "\n".join(lines)


@pytest.mark.parametrize("damage", [
    lambda sb: sb.service.update(pid=99),
    lambda sb: sb.service.update(served=5),
    lambda sb: sb.service.update(accuracy=0.5),
    lambda sb: sb.workspace.update({"data.csv": 10}),
    lambda sb: sb.workspace.pop(".venv"),
    lambda sb: setattr(sb, "egress", {"mode": "allow_list"}),
], ids=["new-pid", "request-count", "model", "rows", "packages", "egress"])
def test_a_worker_that_differs_from_the_golden_fails_the_run_and_cleans_up(damage):
    client, lines = FakeClient(), []
    client.on_restore = damage
    assert run(client, lines) == 1
    assert sum("FAILED" in line for line in lines) == 1
    assert any("did not start from the golden state" in line for line in lines)
    assert all(sb.deleted for sb in client.all) and client.deleted_snapshots == ["snap-1"]


def test_failed_snapshot_fails_before_any_worker_and_deletes_the_golden():
    client, lines = FakeClient(snapshot_statuses=("Pending", "Failed")), []
    assert run(client, lines) == 1
    assert len(client.all) == 1 and client.all[0].deleted
    assert "Failed: RuntimeError: snapshot snap-1 is Failed: capture failed" in lines


def test_snapshot_stuck_pending_times_out():
    client, lines = FakeClient(snapshot_statuses=("Pending",)), []
    assert run(client, lines) == 1
    assert any("did not become Ready" in line for line in lines)
    assert client.all[0].deleted


def test_failed_install_is_one_line_and_cleans_up():
    client, lines = FakeClient(), []
    client.install_fails = True
    assert run(client, lines) == 1
    assert any(line.startswith("Failed: RuntimeError: installing pandas, scikit-learn failed") for line in lines)
    assert client.all[0].deleted
    assert len(client.all) == 1


def test_ctrl_c_during_a_worker_deletes_everything_and_returns_130():
    client, lines = FakeClient(), []
    original = client._create

    def create(params, allow_egress=None):
        sandbox = original(params, allow_egress)
        client.hang_on_ready = "restore" in params  # Ctrl+C while the first worker starts
        return sandbox

    client.sandboxes.create = create
    assert run(client, lines) == 130
    assert len(client.all) == 2 and all(sb.deleted for sb in client.all)
    assert client.deleted_snapshots == ["snap-1"]


def test_keep_snapshot_keeps_the_golden_source_because_the_snapshot_is_deleted_with_it():
    client, lines = FakeClient(), []
    assert run(client, lines, keep_snapshot=True) == 0
    golden, *workers = client.all
    assert not golden.deleted and all(w.deleted for w in workers)
    assert client.deleted_snapshots == []
    assert "--snapshot snap-1" in "\n".join(lines)


def test_reusing_a_snapshot_skips_the_build_and_leaves_the_snapshot_alone():
    client, snap_id = build_kept_snapshot()
    golden = client.all[0]
    client.snapshot_statuses = ["Ready"]
    start = len(client.all)
    lines = []
    assert run(client, lines, snapshot_id=snap_id) == 0
    new = client.all[start:]
    assert len(new) == golden_snapshot.WORKERS and all(p["restore"] == snap_id for p in client.created[start:])
    assert all(w.deleted for w in new)
    assert not golden.deleted and snap_id in client.snapshots and client.deleted_snapshots == []
    assert "saved" in "\n".join(lines)  # the cold time comes from the manifest written at build time
    assert lines[-1] == f"   Left snapshot {snap_id} in place: this run did not create it."


def test_reusing_a_snapshot_that_is_not_ready_fails_without_creating_anything():
    client, snap_id = build_kept_snapshot()
    client.snapshot_statuses = ["Running"]
    start, lines = len(client.all), []
    assert run(client, lines, snapshot_id=snap_id) == 1
    assert len(client.all) == start
    assert any("is Running, not Ready" in line for line in lines)


def test_reusing_a_missing_snapshot_is_one_line():
    client, lines = FakeClient(), []
    assert run(client, lines, snapshot_id="nope") == 1
    assert "Failed: NotFoundError: HTTP 404 not_found Snapshot not found." in lines
    assert client.all == []


def test_reusing_a_snapshot_without_a_manifest_fails_clearly():
    client, snap_id = build_kept_snapshot()
    del client.snapshots[snap_id]["state"][0][golden_snapshot.MANIFEST]
    client.snapshot_statuses = ["Ready"]
    start, lines = len(client.all), []
    assert run(client, lines, snapshot_id=snap_id) == 1
    assert any("was not made by this recipe" in line for line in lines)
    assert all(w.deleted for w in client.all[start:])


@pytest.mark.parametrize("answer, expected", [
    ({"boot_id": "b", "served": 4}, True),
    ({"boot_id": "other", "served": 4}, False),
    ({"boot_id": "b", "served": 1}, False),
])
def test_same_process_check_needs_the_boot_id_and_the_next_request_count(answer, expected):
    manifest = {"boot_id": "b", "served": 3}
    assert golden_snapshot.same_memory(answer, manifest) is expected
