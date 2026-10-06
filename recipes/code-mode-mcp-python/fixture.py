"""Synthetic payment logs for the comparison, and the answer computed straight from them."""
from __future__ import annotations

import csv
import io
import json
import random
import tarfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

FILES = 200
EVENTS_PER_FILE = 8
FIRST_DAY = date(2026, 9, 21)  # three weeks of logs; the question asks about the middle one
DAYS = 21
WEEK_START, WEEK_END = "2026-09-28", "2026-10-04"
FIELDS = ("ts", "customer", "type", "status", "amount_inr")
CUSTOMERS = (
    "acme-foods", "bluepeak-retail", "coral-travel", "deccan-motors", "everfresh-dairy", "finch-labs",
    "ganges-textiles", "harbor-logistics", "indigo-books", "jade-pharma", "kestrel-media", "lotus-hotels",
    "monsoon-apparel", "nimbus-cloudworks", "orchid-salons", "peacock-jewels", "quartz-electronics",
    "river-bakery", "saffron-kitchens", "teak-furniture", "umber-studios", "vega-sports", "willow-clinics",
    "xenon-solar", "yamuna-tea", "zest-juices", "arcade-games", "banyan-schools", "cedar-insurance",
    "dune-cosmetics",
)

QUESTION = (
    f"Which 3 customers had the most failed payments last week (Monday {WEEK_START} to Sunday {WEEK_END}, "
    "UTC), and how many failed payments did each have? The payment logs are the JSON and CSV files in logs/. "
    'A failed payment is an event whose type is "payment" and whose status is "failed".'
)


@dataclass
class Logs:
    """The log files (path -> text) and the true top three as (customer, failed payments), highest first."""
    files: dict[str, str]
    top: list[tuple[str, int]]


def _events(rng: random.Random, day: date, customers: list[str]) -> list[dict]:
    """One file's events, all on its day; customers earlier in the list pay (and fail) more often."""
    weights = [1 / (i + 2) for i in range(len(customers))]
    events = []
    for _ in range(EVENTS_PER_FILE):
        second = rng.randrange(86_400)
        events.append({
            "ts": f"{day.isoformat()}T{second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}Z",
            "customer": rng.choices(customers, weights)[0],
            "type": rng.choices(("payment", "refund"), (85, 15))[0],
            "status": rng.choices(("succeeded", "failed", "pending"), (78, 17, 5))[0],
            "amount_inr": rng.randrange(100, 50_000),
        })
    return sorted(events, key=lambda e: e["ts"])


def _render(path: str, events: list[dict]) -> str:
    """Writes events as a JSON array or as CSV with a header row, by the file's extension."""
    if path.endswith(".json"):
        return json.dumps(events, indent=1) + "\n"
    out = io.StringIO()
    writer = csv.DictWriter(out, FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(events)
    return out.getvalue()


def _ranked(events: list[dict]) -> list[tuple[str, int]]:
    """Failed payments in the question's week, per customer, most first."""
    return Counter(e["customer"] for e in events if e["type"] == "payment" and e["status"] == "failed"
                   and WEEK_START <= e["ts"][:10] <= WEEK_END).most_common()


def make(seed: int) -> Logs:
    """Generates the logs for a seed, redrawing until the top three are strictly ordered with no tie at third place."""
    rng = random.Random(seed)
    customers = rng.sample(CUSTOMERS, len(CUSTOMERS))  # who is busiest changes with the seed
    while True:
        files, events, seq = {}, [], Counter()
        for i in range(FILES):
            day = FIRST_DAY + timedelta(days=i * DAYS // FILES)
            seq[day] += 1
            path = f"logs/payments-{day.isoformat()}-{seq[day]:02d}.{'json' if i % 2 == 0 else 'csv'}"
            batch = _events(rng, day, customers)
            files[path] = _render(path, batch)
            events += batch
        ranked = _ranked(events)
        if ranked[0][1] > ranked[1][1] > ranked[2][1] > ranked[3][1]:
            return Logs(files, ranked[:3])


def tarball(files: dict[str, str]) -> bytes:
    """Packs the files into one .tar.gz, so the upload is a single write instead of 200."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()
