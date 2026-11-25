"""A conditional challenge to a recorded action, based on pre-decision evidence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Critique:
    rule: str
    title: str
    alternative: str
    reason: str
    counterargument: str
    review_priority: int = 0


def evidence_label(rule):
    if rule == "fold_free_check":
        return "Recorded free check option"
    if rule == "sb_fold_reference":
        return "Solver reference"
    if rule == "turn-fold-guaranteed-share":
        return "Guaranteed showdown share"
    if rule in {"flop-cbet-stackoff-bound", "turn-closing-call-value-bound"}:
        return "Range-dependent equity bound"
    if rule in {"river_flat_value", "river_check_back_value", "river_fold_near_nuts"}:
        return "River value review"
    return "Conditional critique"


def critique_for(snap, event, advice):
    """Evaluate only the current Hero action and information preceding it."""
    if (
        event.action == "fold"
        and event.amount == 0
        and snap.to_call == 0
        and snap.price_to_match == 0
    ):
        return Critique(
            "fold_free_check",
            "Folded when checking was free",
            "Check for free",
            "The history records a fold on your turn with no chips owed. Checking keeps "
            "your claim to the pot and commits no additional chips.",
            "A disconnect, timeout or automatic fold could produce the same record. "
            "Check whether you actually chose to fold.",
        )
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
