"""Runs the real checkers and the real inventory service locally against a copy of the golden workspace."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tasks import GOLDEN_DIR, TASKS

CHECKERS = {t.id: t.checker for t in TASKS}

GOOD_PRICING = '''def order_total(items, discount_code=None):
    total = sum(i["price"] * i["qty"] for i in items)
    if discount_code == "SAVE10":
        total *= 0.9
    return round(total, 2)
'''
GOOD_SLUGIFY = '''import re
def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
'''
UNICODE_SLUGIFY = '''import re
def slugify(title):
    return re.sub(r"[\\W_]+", "-", title.lower()).strip("-")
'''


@pytest.fixture
def workspace(tmp_path):
    """A fresh copy of the golden workspace, like a rollout sandbox gets."""
    shutil.copytree(GOLDEN_DIR, tmp_path / "ws")
    return tmp_path / "ws"


def grade(task_id, cwd, env=None):
    """Runs a checker the way the script does (python3 -I, source on stdin) and returns its verdict."""
    out = subprocess.run([sys.executable, "-I", "-"], input=CHECKERS[task_id], cwd=cwd, capture_output=True,
                         text=True, timeout=30, env={**os.environ, **(env or {})})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture
def service(workspace):
    """Starts golden/inventory_service.py on a free port and returns its base URL."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "inventory_service.py"], cwd=workspace, env={**os.environ, "PORT": str(port)})
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    yield url
    proc.kill()
    proc.wait()


def put(url, sku, body):
    req = urllib.request.Request(f"{url}/items/{sku}", data=json.dumps(body).encode(), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_every_task_fails_on_the_untouched_golden_workspace(workspace, service):
    for task in TASKS:
        verdict = grade(task.id, workspace, {"INVENTORY_URL": service})
        assert verdict["passed"] is False, task.id


def test_the_broken_pricing_fails_with_the_case_that_shows_it(workspace):
    verdict = grade("fix-discount", workspace)
    assert "want 300.0" in verdict["detail"]


def test_a_correct_pricing_fix_passes(workspace):
    (workspace / "app" / "pricing.py").write_text(GOOD_PRICING)
    assert grade("fix-discount", workspace)["passed"] is True


def test_a_syntax_error_in_the_agent_code_is_a_fail_with_the_reason_not_a_crash(workspace):
    (workspace / "app" / "pricing.py").write_text("def order_total(:\n")
    verdict = grade("fix-discount", workspace)
    assert verdict["passed"] is False and "SyntaxError" in verdict["detail"]


def test_agent_code_that_exits_or_hangs_is_a_fail_with_the_reason(workspace, monkeypatch):
    (workspace / "app" / "pricing.py").write_text("import sys\ndef order_total(items, discount_code=None):\n    sys.exit(0)\n")
    verdict = grade("fix-discount", workspace)
    assert verdict["passed"] is False and "SystemExit" in verdict["detail"]
    (workspace / "app" / "pricing.py").write_text("def order_total(items, discount_code=None):\n    while True:\n        pass\n")
    fast = CHECKERS["fix-discount"].replace("signal.alarm(30)", "signal.alarm(1)")
    assert fast != CHECKERS["fix-discount"]
    monkeypatch.setitem(CHECKERS, "fix-discount", fast)
    verdict = grade("fix-discount", workspace)
    assert verdict == {"passed": False, "detail": "the check took longer than 30s"}
    # a hang that swallows every exception still ends with a fail
    (workspace / "app" / "pricing.py").write_text(
        "def order_total(items, discount_code=None):\n    while True:\n        try:\n            pass\n"
        "        except BaseException:\n            continue\n")
    assert grade("fix-discount", workspace)["passed"] is False


def test_slugify_passes_only_when_non_ascii_letters_become_hyphens(workspace):
    (workspace / "app" / "text.py").write_text(UNICODE_SLUGIFY)
    assert "Café" in grade("slugify", workspace)["detail"]
    (workspace / "app" / "text.py").write_text(GOOD_SLUGIFY)
    assert grade("slugify", workspace)["passed"] is True


def test_revenue_report_counts_only_paid_orders(workspace):
    (workspace / "report.json").write_text(json.dumps({"north": 3624.75, "south": 2425.55, "east": 3200.49, "west": 6939.74}))
    assert grade("revenue-report", workspace)["passed"] is True
    (workspace / "report.json").write_text(json.dumps({"north": 4684.75, "south": 3545.55, "east": 5414.49, "west": 7059.74}))
    assert grade("revenue-report", workspace)["passed"] is False


def test_restock_passes_only_on_the_service_not_the_file(workspace, service):
    env = {"INVENTORY_URL": service}
    seed = json.loads((workspace / "data" / "inventory.json").read_text())
    (workspace / "data" / "inventory.json").write_text(json.dumps([{**i, "stock": max(i["stock"], 20) if i["stock"] < 5 else i["stock"]} for i in seed]))
    assert grade("restock", workspace, env)["passed"] is False  # editing the file does not change the running service
    for item in seed:
        if item["stock"] < 5:
            assert put(service, item["sku"], {"stock": 20}) == 200
    assert grade("restock", workspace, env)["passed"] is True
    assert put(service, "HUB-7P", {"stock": 20}) == 200  # 5 units is not "fewer than 5"
    assert "HUB-7P" in grade("restock", workspace, env)["detail"]


def test_service_counts_writes_and_rejects_bad_bodies(service):
    assert put(service, "MSE-WL", {"stock": -1}) == 400
    assert put(service, "MSE-WL", {"qty": 3}) == 400
    assert put(service, "NOPE", {"stock": 3}) == 404
    assert put(service, "MSE-WL", {"stock": 9}) == 200
    health = json.load(urllib.request.urlopen(service + "/health", timeout=5))
    assert health["writes"] == 1 and len(health["boot_id"]) == 16
