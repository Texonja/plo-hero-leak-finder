"""A conditional challenge to a recorded action, based on pre-decision evidence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Critique:
    rule: str
    title: str
    alternative: str
    reason: str
    counterargument: str


def evidence_label(rule):
    if rule == "sb_fold_reference":
        return "Solver reference"
    if rule in {
        "flop-cbet-stackoff-bound",
        "turn-closing-call-value-bound",
        "turn-fold-guaranteed-share",
    }:
        return "Equity bound"
    if rule in {"river_flat_value", "river_check_back_value", "river_fold_near_nuts"}:
        return "River value review"
    return "Conditional critique"


def critique_for(snap, event, advice):
    """Evaluate only the current Hero action and information preceding it."""
    if not snap.hero_cards or len(snap.hero_cards) != 4 or snap.bb <= 0:
        return None
    if snap.street == "Preflop":
        from .preflop_critique import review_preflop
        from .preflop_spots import review_common
        from .sb_reference import compare_sb

        existing = compare_sb(snap, event) or review_preflop(snap, event, advice)
        if existing:
            return existing
        if (
            advice
            and advice.can_compare(snap)
            and advice.difference(event.action, event.amount)
        ):
            return None
        return review_common(snap, event)
    if snap.street in ("Flop", "Turn"):
        from .early_critique import review_early
        from .flop_critique import review_flop

        if snap.street == "Flop" and (critique := review_flop(snap, event)):
            return critique
        return review_early(snap, event, advice)
    if snap.street == "River":
        from .river_critique import review_river

        return review_river(snap, event, advice)
    return None
