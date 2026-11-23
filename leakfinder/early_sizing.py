"""Evaluate heads-up opening bet/fold sizes with observed opponent responses."""

from dataclasses import dataclass
from itertools import product

from .analysis import wilson
from .opponent_history import responses, size_band

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
    original = next((entry for entry in ready if entry.amount == baseline), None)
    if baseline and original is None:
        return baseline, "The original size has too few comparable responses."
    baseline_band = size_band(baseline, pot)
    alternatives = [
        entry
        for entry in ready
        if (not baseline or size_band(entry.amount, pot) != baseline_band)
        and entry.low > floor
        and (original is None or entry.low > original.high + floor)
    ]
    if alternatives:
        best = max(alternatives, key=lambda entry: entry.low)
        if baseline and any(
            entry.high + floor >= best.low
            and size_band(entry.amount, pot)
            not in {size_band(best.amount, pot), baseline_band}
            for entry in ready
        ):
            return (
                baseline,
                "The evidence does not yet distinguish the alternative sizing groups.",
            )
        return best.amount, "The lower estimate supports betting in this sizing group."
    if (
        original
        and original.high <= floor
        and all(entry.high <= floor for entry in ready)
    ):
        return (
            0,
            "The upper estimate stays below the betting threshold for sizes with enough history.",
        )
    return baseline, "The estimate ranges still allow the original recommendation."
