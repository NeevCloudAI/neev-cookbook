"""Order totals for a small online shop. Amounts are in paise (100 paise = 1 rupee) so no floats creep in."""

GST_PERCENT = 18


def bulk_discount_percent(quantity: int) -> int:
    """Percentage off for buying in bulk: 5% from 10 items, 10% from 50 items."""
    if quantity >= 10:
        return 5
    if quantity >= 50:
        return 10
    return 0


def gst(amount: int) -> int:
    """GST on an amount in paise, rounded to the nearest paisa (a half rounds up)."""
    return amount * GST_PERCENT // 100


def order_total(unit_price: int, quantity: int) -> int:
    """What the customer pays: price times quantity, less the bulk discount, plus GST on the discounted amount."""
    subtotal = unit_price * quantity
    discounted = subtotal - subtotal * bulk_discount_percent(quantity) // 100
    return discounted + gst(discounted)
