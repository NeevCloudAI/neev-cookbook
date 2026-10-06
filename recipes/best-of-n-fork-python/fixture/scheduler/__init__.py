"""A tiny meeting scheduler: merge busy intervals and find free slots."""
from .intervals import merge, to_hhmm, to_minutes
from .slots import free_slots

__all__ = ["free_slots", "merge", "to_hhmm", "to_minutes"]
