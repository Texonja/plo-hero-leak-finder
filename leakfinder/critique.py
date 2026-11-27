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


def stack_context(snap):
    """Chips still behind at the decision, before Hero acts."""
    live = [seat for seat in snap.seats if seat.live]
    hero = next((seat for seat in live if seat.name == snap.hero), None)
    if not hero or len(live) < 2 or snap.bb <= 0:
        return ""
    field = "Heads-up" if len(live) == 2 else f"{len(live)}-way"
    stacks = [f"Hero {hero.stack / snap.bb:g}"]
    for seat in live:
        if seat != hero:
            position = seat.position or f"Seat {seat.seat}"
            stacks.append(f"{position} {seat.stack / snap.bb:g}")
    return " · ".join((field, "Stacks behind (BB)", *stacks))


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


def critique_for(snap, event, advice, *, study=True):
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
            "The history records a fold on your turn with no chips owed. Check to stay in the pot for free, and check whether your client timed out.",
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

        if snap.street == "Flop" and (critique := review_flop(snap, event, study=study)):
            return critique
        if study or (snap.street == "Turn" and event.action == "fold"):
            return review_early(snap, event, advice)
        return None
    if snap.street == "River":
        from .river_critique import review_river

        return review_river(snap, event, advice)
    return None


# A price bound, a plausible bluff, or an alternative sizing alone cannot
# establish that Hero should change the recorded action. Those lines remain
# available for explicit study. New review rules require an affirmative choice.
_ACTION_RULES = {
    "fold_free_check", "sb_fold_reference", "aa_closing_squeeze", "low_blind_backraise",
    "preflop_early_weak_entry", "preflop_fold_supported_open", "preflop_checked_supported_limp",
    "preflop_weak_cold_call", "preflop_weak_reraise", "preflop_cold_threebet_exposed",
    "preflop_weak_threebet_defense", "preflop_discounted_bb_fold", "threebet_thin_oop_call",
    "rfi_supported_hand", "rfi_thin_early_raise",
    "flop_raise_call_no_draw", "flop_cbet_call_low_gutshot", "turn-fold-guaranteed-share",
    "river_call_against_prior_bets", "river_fold_against_prior_bets",
    "river_check_against_prior_folds",
}


def action_critique(snap, event, advice):
    """Return an actionable alternative for the home list and the replay."""
    idea = critique_for(snap, event, advice, study=False)
    if idea and idea.rule == "rfi_supported_hand" and event.action == "call":
        return None  # An intentional limp can be mixed with an open-raise.
    if idea is None or idea.rule in _ACTION_RULES:
        return idea
    if idea.rule in {"river_flat_value", "river_check_back_value"}:
        from .river_holdings import river_holdings

        if not river_holdings(snap.hero_cards, snap.board).stronger:
            return idea
    return None
