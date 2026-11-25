"""Postflop pot fractions used in public-context matching and cash reports."""

from bisect import bisect_left

UPPER_FRACTIONS = (0.33, 0.50, 0.75, 1.00)
LABELS = ("≤33% pot", ">33–50% pot", ">50–75% pot", ">75–100% pot", ">100% pot")
MATCH_LABELS = {f"bet-{i}": label for i, label in enumerate(LABELS)}


def bet_size_key(fraction):
    return f"bet-{bisect_left(UPPER_FRACTIONS, fraction)}"
