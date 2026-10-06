def order_total(items, discount_code=None):
    """Returns the order total in rupees, rounded to 2 decimal places.

    Each item is a dict with "price" (per unit) and "qty". The code "SAVE10" takes 10% off the
    whole order; any other code, or none, changes nothing.
    """
    total = sum(item["price"] for item in items)
    if discount_code == "SAVE10":
        total = total - 10
    return round(total, 2)
