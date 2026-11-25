"""Observed postflop bet fractions shared by reports and historical matching."""

from bisect import bisect_left

# Boundaries separate common third-, half-, three-quarter- and full-pot bets.
# A recorded fraction just over 1 is grouped with pot-sized observations.
# The exact fraction is retained for pricing and the history need not record
# which sizing button, if any, the bettor used.
UPPER_FRACTIONS = (0.40, 0.625, 0.875, 1.01)
LABELS = ("≤40% pot", ">40–62.5% pot", ">62.5–87.5% pot", ">87.5–101% pot", ">101% pot")
MATCH_LABELS = {f"bet-{i}": label for i, label in enumerate(LABELS)}


def bet_size_key(fraction):
    return f"bet-{bisect_left(UPPER_FRACTIONS, fraction)}"
