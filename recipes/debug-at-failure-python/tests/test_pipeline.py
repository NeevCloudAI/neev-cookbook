import json

import pytest

import debug_at_failure
from app import pipeline


@pytest.fixture
def fresh(monkeypatch):
    """Runs the real pipeline module with no pauses and its state reset."""
    monkeypatch.setattr(pipeline, "PAUSE_S", 0)
    monkeypatch.setattr(pipeline, "state", json.loads(json.dumps(pipeline.state)))
    return pipeline


def test_the_batch_has_exactly_one_bad_record_in_the_second_half():
    records, bad = debug_at_failure.make_batch("seed")
    assert len(records) == debug_at_failure.BATCH_SIZE and len({r["id"] for r in records}) == len(records)
    assert [i for i, r in enumerate(records) if isinstance(r["amount"], str)] == [bad]
    assert bad >= len(records) // 2 and "," in records[bad]["amount"]
    assert debug_at_failure.make_batch("seed") == (records, bad)


def test_the_pipeline_fails_in_stage_3_at_the_bad_record_and_keeps_its_state(fresh, monkeypatch):
    records, bad = debug_at_failure.make_batch("seed")
    monkeypatch.setattr(fresh, "batch", records)
    fresh.work()
    s = fresh.state
    assert (s["status"], s["stage"], s["position"]) == ("error", "aggregate", bad)
    assert s["error"].startswith("TypeError") and s["done"] == {"parse": 240, "enrich": 240, "aggregate": bad}
    assert fresh.batch[bad]["amount"] == records[bad]["amount"]  # the bad record is still in memory


def test_a_clean_batch_finishes(fresh, monkeypatch):
    records, bad = debug_at_failure.make_batch("seed")
    records[bad]["amount"] = 1.0
    monkeypatch.setattr(fresh, "batch", records)
    fresh.work()
    assert fresh.state["status"] == "done" and fresh.state["error"] is None


def test_the_code_in_the_sandbox_does_not_give_the_fault_away():
    source = (debug_at_failure.PIPELINE).read_text().lower()
    assert "thousands" not in source and "csv" not in source and "make_batch" not in source
