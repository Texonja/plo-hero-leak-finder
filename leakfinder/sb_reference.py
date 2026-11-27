"""First-in SB fold frequency in PokerData's downloadable PLO4 100 BB sample."""

from collections import defaultdict
from functools import cache
from pathlib import Path

from .critique import Critique


def hand_key(cards):
    suits = defaultdict(list)
    for rank, suit in cards:
        suits[suit].append(rank)
    return "/".join(sorted("".join(sorted(ranks)) for ranks in suits.values()))


@cache
def fold_weights():
    rows = Path(__file__).with_name("sb_fold_100bb.tsv").read_text(encoding="ascii")
    return {
        key: float(value) for key, value in (line.split() for line in rows.splitlines())
    }


def sb_reference(snap):
    if (
        snap.street != "Preflop"
        or len(snap.seats) != 6
        or not snap.hero_cards
        or snap.sb * 2 != snap.bb
        or not snap.can_raise
        or snap.to_call != snap.sb
    ):
        return None
    live = [s for s in snap.seats if s.live]
    if (
        len(live) != 2
        or {s.position for s in live} != {"SB", "BB"}
        or not any(s.position == "SB" and s.is_hero for s in live)
    ):
        return None
    forced = {"posts_sb", "posts_bb"}
    folded = [a.name for a in snap.history if a.action == "fold"]
    if (
        len(folded) != 4
        or set(folded) != {s.name for s in snap.seats if not s.live}
        or any(a.action not in forced | {"fold"} for a in snap.history)
        or snap.pot != snap.sb + snap.bb
    ):
        return None
    depth = min(s.stack + s.committed for s in live) / snap.bb
    if not 95 <= depth <= 105:
        return None
    return fold_weights()[hand_key(snap.hero_cards)], depth


def reference_choices(fold):
    """Compare only near-pure fold/continue frequencies in this particular model."""
    if fold <= 0.01:
        return ("call", "raise")
    if fold >= 0.99:
        return ("fold",)
    return ()


def compare_sb(snap, event):
    reference = sb_reference(snap)
    if reference is None:
        return None
    fold, depth = reference
    choices = reference_choices(fold)
    if not choices or event.action in choices:
        return None
    alternative = "fold" if choices == ("fold",) else "continue (complete or raise)"
    return Critique(
        "sb_fold_reference",
        "Small blind first in · 100 BB solver reference",
        alternative,
        f"Four folds to your small blind at {depth:g} BB effective. "
        f"The 100 BB reference folds this hand class {fold:.1%} of the time.",
        "The model assumes 100 BB, 5% rake capped at 1 BB and its own betting tree. "
        "Actual rake, other stacks and the big blind's response can favor a different play.",
    )
