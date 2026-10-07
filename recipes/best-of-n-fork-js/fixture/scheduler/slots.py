"""Finds free meeting slots in a day from a list of busy "HH:MM" ranges."""
from .intervals import merge, to_hhmm, to_minutes


def free_slots(busy, day_start="09:00", day_end="17:00", min_minutes=30):
    """Returns the free ("HH:MM", "HH:MM") slots of at least min_minutes between day_start and day_end."""
    start_of_day, end_of_day = to_minutes(day_start), to_minutes(day_end)
    taken = merge((to_minutes(a), to_minutes(b)) for a, b in busy)
    free, cursor = [], start_of_day
    for start, end in taken:
        start, end = max(start, start_of_day), min(end, end_of_day)
        if start - cursor >= min_minutes:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if end_of_day - cursor >= min_minutes:
        free.append((cursor, end_of_day))
    return [(to_hhmm(a), to_hhmm(b)) for a, b in free]
