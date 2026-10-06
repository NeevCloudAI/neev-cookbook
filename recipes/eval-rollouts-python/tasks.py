"""The eval tasks and their deterministic checkers, which run inside the rollout sandbox after the agent finishes."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"

# Every checker defines check() -> (passed, detail); this wrapper prints one JSON verdict and never crashes,
# so agent code that raises, exits or hangs when the checker calls it is a fail with the reason, not a grading error.
_RUNNER = """
import json, os, signal, sys
sys.path.insert(0, os.getcwd())
def _too_slow(*_):
    print(json.dumps({"passed": False, "detail": "the check took longer than 30s"}), flush=True)
    os._exit(0)  # exits even if the agent's code catches every exception
signal.signal(signal.SIGALRM, _too_slow)
signal.alarm(30)
try:
    passed, detail = check()
except BaseException as e:
    passed, detail = False, f"{type(e).__name__}: {e}"
signal.alarm(0)
print(json.dumps({"passed": bool(passed), "detail": str(detail)[:300]}))
"""


@dataclass(frozen=True)
class Task:
    """One eval task: what the agent is asked, and the checker source the script runs to grade it."""

    id: str
    prompt: str
    checker: str


def _checker(body: str, **expected) -> str:
    """Builds a checker program: the expected values as JSON literals, the check() body, then the runner."""
    consts = "".join(f"{name} = json.loads({json.dumps(json.dumps(value))})\n" for name, value in expected.items())
    return f"import json\n{consts}{body}{_RUNNER}"


def _price_cases() -> list[dict]:
    """Order totals the docstring of app/pricing.py specifies, computed by a correct implementation."""
    orders = [
        ([{"price": 100.0, "qty": 3}], None),
        ([{"price": 249.5, "qty": 2}, {"price": 10.0, "qty": 1}], "SAVE10"),
        ([{"price": 19.99, "qty": 4}], "SAVE20"),
        ([], "SAVE10"),
        ([{"price": 5.0, "qty": 0}, {"price": 1200.0, "qty": 1}], None),
    ]
    return [{"items": items, "code": code,
             "want": round(sum(i["price"] * i["qty"] for i in items) * (0.9 if code == "SAVE10" else 1), 2)}
            for items, code in orders]


def _paid_revenue() -> dict[str, float]:
    """Total of the paid orders per region in golden/data/orders.csv."""
    totals: dict[str, float] = {}
    with open(GOLDEN_DIR / "data" / "orders.csv", newline="") as f:
        for row in csv.DictReader(f):
            if row["status"] == "paid":
                totals[row["region"]] = totals.get(row["region"], 0) + float(row["amount"])
    return {region: round(total, 2) for region, total in totals.items()}


def _restocked() -> dict[str, int]:
    """Stock per SKU after a correct restock: under 5 becomes 20, everything else unchanged."""
    items = json.loads((GOLDEN_DIR / "data" / "inventory.json").read_text())
    return {i["sku"]: 20 if i["stock"] < 5 else i["stock"] for i in items}


TASKS = (
    Task("restock",
         "The inventory service described in SERVICE.md is running. Through its API, set the stock of every item "
         "that has fewer than 5 units to 20. Leave every other item unchanged.",
         _checker("""
def check():
    import urllib.request
    url = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8000")
    with urllib.request.urlopen(url + "/items", timeout=10) as r:
        got = {i["sku"]: i["stock"] for i in json.load(r)["items"]}
    wrong = {sku: got.get(sku) for sku in WANT if got.get(sku) != WANT[sku]}
    if wrong:
        return False, f"service stock wrong for {wrong}, want {({sku: WANT[sku] for sku in wrong})}"
    return True, f"service holds the right stock for all {len(WANT)} items"
""", WANT=_restocked())),
    Task("fix-discount",
         "app/pricing.py: order_total returns wrong totals. Its docstring is the specification. "
         "Fix the function so it matches the docstring.",
         _checker("""
def check():
    from app.pricing import order_total
    for case in CASES:
        got = order_total(case["items"], case["code"])
        if not isinstance(got, (int, float)) or abs(got - case["want"]) > 0.01:
            return False, f"order_total({case['items']}, {case['code']!r}) = {got!r}, want {case['want']}"
    return True, f"{len(CASES)} orders priced correctly"
""", CASES=_price_cases())),
    Task("slugify",
         "Implement slugify in app/text.py as its docstring describes.",
         _checker("""
def check():
    from app.text import slugify
    for title, want in CASES:
        got = slugify(title)
        if got != want:
            return False, f"slugify({title!r}) = {got!r}, want {want!r}"
    return True, f"{len(CASES)} titles slugified correctly"
""", CASES=[["  Hello, World! 2026 ", "hello-world-2026"], ["NeevCloud", "neevcloud"],
            ["a--b__c", "a-b-c"], ["---", ""], ["Rs. 1,299 only!!", "rs-1-299-only"],
            ["Café Déjà vu", "caf-d-j-vu"]])),
    Task("revenue-report",
         "From data/orders.csv, write report.json in the workspace root: a JSON object that maps each region "
         "to the total amount of its paid orders, rounded to 2 decimal places. Orders with any other status do not count.",
         _checker("""
def check():
    with open("report.json") as f:
        got = json.load(f)
    if not isinstance(got, dict) or sorted(got) != sorted(WANT):
        return False, f"regions {sorted(got) if isinstance(got, dict) else got!r}, want {sorted(WANT)}"
    for region, want in WANT.items():
        if not isinstance(got[region], (int, float)) or abs(got[region] - want) > 0.01:
            return False, f"{region} = {got[region]!r}, want {want}"
    return True, f"{len(WANT)} regional totals correct"
""", WANT=_paid_revenue())),
)
