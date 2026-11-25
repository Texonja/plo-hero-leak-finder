"""Evaluate heads-up opening bet/fold sizes with observed opponent responses."""

from dataclasses import dataclass
from itertools import product

from .analysis import wilson
from .bet_sizing import bet_size_key
from .opponent_history import responses

CHECK_MARGIN = 0.02


@dataclass(frozen=True)
class Estimate:
    amount: int
    gain: float
    low: float
    high: float


def historical_estimate(pot, amount, check, call_equities, sample):
    """Reweight payoffs by F/C/R; equity against callers remains modeled."""
    folds, calls, raises = responses(sample)
    n = folds + calls + raises
    if not n:
        return None

    fold = folds / n
    continuation = calls + raises
    raise_if_continue = raises / continuation if continuation else 0.0
    fold_bounds = wilson(fold, n)
    raise_bounds = wilson(raise_if_continue, continuation) or (0.0, 1.0)
    low_equity, mid_equity, high_equity = (
        min(call_equities),
        call_equities[1],
        max(call_equities),
    )

    def gain(f, r, equity):
        called = equity * (pot + 2 * amount) - amount
        return f * pot + (1 - f) * ((1 - r) * called - r * amount) - check

    limits = [
        gain(f, r, eq)
        for f, r, eq in product(fold_bounds, raise_bounds, (low_equity, high_equity))
    ]
    return Estimate(
        amount, gain(fold, raise_if_continue, mid_equity), min(limits), max(limits)
    )


def supported_sizing(pot, baseline, ready):
    """Keep the range-model choice until historical bounds support a change."""
    floor = CHECK_MARGIN * pot
    if baseline and not any(entry.amount == baseline for entry in ready):
        return baseline, "The original size has too few comparable responses."
    baseline_band = bet_size_key(baseline / pot)
    groups = {}
    for entry in ready:
        groups.setdefault(bet_size_key(entry.amount / pot), []).append(entry)
    # Prices in one group use the exact same historical responses. Compare
    # the envelope across its offered prices, with each observation counted once.
    bounds = {
        key: (min(e.low for e in estimates), max(e.high for e in estimates))
        for key, estimates in groups.items()
    }
    required = max(0.0, bounds[baseline_band][1]) + floor if baseline else floor
    alternatives = {
        key: low
        for key, (low, _) in bounds.items()
        if (not baseline or key != baseline_band) and low > required
    }
    if alternatives:
        best = max(alternatives, key=alternatives.get)
        if any(
            high + floor >= alternatives[best]
            for key, (_, high) in bounds.items()
            if key != best and (not baseline or key != baseline_band)
        ):
            return (
                baseline,
                "The evidence does not yet distinguish the alternative sizing groups.",
            )
        # The caller displays the group when several offered prices share it.
        return (
            groups[best][0].amount,
            "The lower estimate supports betting in this sizing group.",
        )
    if baseline and all(high <= floor for _, high in bounds.values()):
        return (
            0,
            "The upper estimate stays below the betting threshold for sizes with enough history.",
        )
    return baseline, "The estimate ranges still allow the original recommendation."
