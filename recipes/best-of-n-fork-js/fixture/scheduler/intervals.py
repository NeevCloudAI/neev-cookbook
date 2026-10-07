"""Time intervals as (start, end) pairs of minutes since midnight."""


def to_minutes(hhmm: str) -> int:
    """Converts "HH:MM" to minutes since midnight."""
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def to_hhmm(minutes: int) -> str:
    """Converts minutes since midnight to "HH:MM"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def merge(intervals):
    """Merges overlapping or touching intervals into a sorted list of disjoint ones."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]
