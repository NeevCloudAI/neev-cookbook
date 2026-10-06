import csv
import io
import json
import tarfile
from collections import Counter

import fixture


def recount(files: dict[str, str]) -> list[tuple[str, int]]:
    """Counts failed payments in the week straight from the file text, independently of the generator."""
    events = []
    for path, text in files.items():
        events += json.loads(text) if path.endswith(".json") else list(csv.DictReader(io.StringIO(text)))
    counts = Counter(e["customer"] for e in events
                     if e["type"] == "payment" and e["status"] == "failed"
                     and fixture.WEEK_START <= e["ts"][:10] <= fixture.WEEK_END)
    return counts.most_common()


def test_same_seed_gives_the_same_logs_and_answer():
    a, b = fixture.make(7), fixture.make(7)
    assert a.files == b.files and a.top == b.top
    assert fixture.make(8).files != a.files


def test_two_hundred_small_files_half_json_half_csv():
    logs = fixture.make(1)
    assert len(logs.files) == fixture.FILES
    assert all(p.startswith("logs/") for p in logs.files)
    assert sum(p.endswith(".json") for p in logs.files) == fixture.FILES // 2
    assert sum(p.endswith(".csv") for p in logs.files) == fixture.FILES // 2
    assert max(len(t) for t in logs.files.values()) < 2000


def test_ground_truth_matches_an_independent_recount_of_the_files():
    for seed in range(5):
        logs = fixture.make(seed)
        ranked = recount(logs.files)
        assert logs.top == ranked[:3]
        # Unambiguous: the top three are strictly ordered and the fourth is strictly behind.
        assert ranked[0][1] > ranked[1][1] > ranked[2][1] > ranked[3][1]


def test_logs_contain_the_traps_a_careless_count_falls_into():
    logs = fixture.make(3)
    events = [e for p, t in logs.files.items() if p.endswith(".json") for e in json.loads(t)]
    assert any(e["type"] == "refund" and e["status"] == "failed" for e in events)
    assert any(e["status"] == "failed" and not fixture.WEEK_START <= e["ts"][:10] <= fixture.WEEK_END for e in events)


def test_tarball_holds_every_file_byte_for_byte():
    logs = fixture.make(2)
    with tarfile.open(fileobj=io.BytesIO(fixture.tarball(logs.files)), mode="r:gz") as tf:
        unpacked = {m.name: tf.extractfile(m).read().decode() for m in tf.getmembers()}
    assert unpacked == logs.files
