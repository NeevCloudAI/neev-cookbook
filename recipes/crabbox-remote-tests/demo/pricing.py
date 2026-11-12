"""Order totals for a small shop, with tax rates read from rates.json."""

import json
from pathlib import Path

RATES_FILE = Path(__file__).parent / "Rates.json"


def tax_rate(kind):
    """Return the tax rate for an item kind, read from the rates file."""
    return json.loads(RATES_FILE.read_text())[kind]


def total(prices, kind="standard"):
    """Return the sum of prices plus tax, rounded to cents."""
    return round(sum(prices) * (1 + tax_rate(kind)), 2)
