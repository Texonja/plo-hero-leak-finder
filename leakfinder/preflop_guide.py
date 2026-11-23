"""Authored, qualitative study notes for one six-handed PLO4 preflop decision."""

from collections import Counter
from dataclasses import dataclass

from .vendor.common import RANK_VALUE
from .vendor.primitives import classify_known_hand


@dataclass(frozen=True)
class Group:
    title: str
    play: str
    why: str
    example: str


GROUPS = {
    "aces": Group(
        "AA with an ace-high suit or another high pair",
        "3-bet",
        "Build the pot with the highest starting pair. An ace-high suit or another high pair gives you more ways to continue.",
        "As Ah Ks Kh",
    ),
    "queens": Group(
        "Double-suited QQ or JJ with a suited ace",
        "3-bet or call",
        "The ace blocks some AA combinations. The high pair and two suits favor a 3-bet against a wide CO open and a call against a tighter open.",
        "Ah Qh Qs 9s",
    ),
    "kings": Group(
        "KK with a king-high suit and two connected middle cards",
        "Call",
        "You can flop a high set in position, and the side cards work together. The kings alone offer little reason to build the pot.",
        "Kh Kc 7c 5s",
    ),
    "highrun": Group(
        "Four consecutive high cards, double-suited",
        "3-bet or call",
        "Four connected high cards and two suits play well in position. Lean toward a 3-bet against a wide open.",
        "Qs Js Th 9h",
    ),
    "run": Group(
        "Four connected cards from the six up, with a suit",
        "Call",
        "All four cards contribute to potential straights. Calling preserves position and controls the size of the pot.",
        "Js Ts 9d 8c",
    ),
    "ak_ds": Group(
        "Double-suited AK with connected side cards",
        "Call or 3-bet",
        "Call by default. Two suits, an ace blocker and connected side cards make a 3-bet reasonable against a wide CO open.",
        "Ac 7c Kh 8h",
    ),
    "ak_link": Group(
        "AK with an ace-high suit and two consecutive middle cards",
        "Call",
        "The ace-high suit and connected side cards give you multiple ways to play the flop in position.",
        "As Ks 8d 7h",
    ),
    "ak_link_lower_suit": Group(
        "AK with consecutive side cards and a suit without the ace",
        "Call or fold",
        "The side cards work together and the flush potential is weaker. Favor a call against a smaller open, especially when the blinds rarely squeeze.",
        "Ad Ks 8s 7h",
    ),
    "ak_split": Group(
        "AK with an ace-high suit, a ten or jack, and a disconnected low card",
        "Call or fold",
        "The ace-high suit favors a call against a small open. An isolated low card and active blinds favor folding against a larger open.",
        "As Kd Ts 4h",
    ),
    "ak_low": Group(
        "AK with an ace-high suit and two low cards that can make a wheel",
        "Call or fold",
        "The ace connects with the low cards for a wheel. Those low cards limit other high combinations. The price and the blinds' tendencies matter in this close spot.",
        "As Kd 5s 2h",
    ),
    "small_open": Group(
        "Three consecutive Broadway cards and one low card, facing a small open",
        "Call or fold",
        "An open of up to 2.5 BB gives the three high connected cards a better price. Calling is reasonable when the blinds rarely squeeze.",
        "Ks Jc Qc 3h",
    ),
    "bare_ace": Group(
        "Ace, disconnected low card and limited connectivity, facing a larger open",
        "Fold",
        "An open of at least 3 BB calls for more ways to connect. Even an ace-high suit leaves many flops where continuing is difficult.",
        "Jh 9d Ac 3c",
    ),
    "weak_suit": Group(
        "Three cards of one suit without the ace, with two low cards far from the high cards",
        "Fold",
        "The third card of the same suit reduces the available flush cards. The large gap between the low and high cards limits other ways to improve.",
        "2d 4d Tc Qd",
    ),
    "rainbow": Group(
        "Rainbow and unpaired, with no straight window spanning all four ranks",
        "Fold",
        "The hand cannot make a flush. Calling a CO open usually requires stronger connectivity or more high cards.",
        "Qd 7h 6s 4c",
    ),
    "review": Group(
        "Outside the covered groups",
        "Manual review",
        "This hand needs a closer look at the opening size, rake and both blinds.",
        "Kc 9c Th 9d",
    ),
}
SCOPE = (
    "Six-handed PLO4 cash. UTG and HJ fold, CO opens to 2–3.5 BB, BTN acts, and both blinds are active. "
    "CO and BTN start with 60–150 BB effective after antes. Both blinds have at least 40 BB behind. Any ante is uniform and at most 0.16 BB per player. "
    "Rake, squeeze frequency and opponent ranges can change the preferred action within this scope."
)


def hand_group(cards, open_bb):
    """Return the most specific matching study rule. No stakes, results or frequencies inferred."""
    if (
        len(cards) != 4
        or len(set(cards)) != 4
        or any(
            len(c) != 2 or c[0] not in RANK_VALUE or c[1] not in "cdhs" for c in cards
        )
    ):
        raise ValueError("Expected four distinct PLO4 cards")
    flags = classify_known_hand(cards)
    values = sorted(RANK_VALUE[c[0]] for c in cards)
    lo, mid, high, top = values
    counts = Counter(values)
    suits = Counter(c[1] for c in cards)
    ds = flags["double_suited"]
    ace_suit = any(c[0] == "A" and suits[c[1]] >= 2 for c in cards)
    if counts[14] == 2 and (ace_suit or counts[13] == 2 or counts[12] == 2):
        return "aces"
    if flags["pairs"] == 1 and (counts[12] == 2 or counts[11] == 2) and ace_suit and ds:
        return "queens"
    if (
        counts[13] == 2
        and 5 <= lo < mid <= 10
        and mid - lo <= 2
        and any(c[0] == "K" and suits[c[1]] == 2 for c in cards)
    ):
        return "kings"
    if len(counts) < 4:
        return "review"
    if lo >= 9 and top - lo == 3 and ds:
        return "highrun"
    if lo >= 6 and top - lo <= 4 and max(suits.values()) == 2:
        return "run"
    if top == 14 and high == 13:
        if 6 <= lo and mid - lo == 1:
            if ds:
                return "ak_ds"
            if ace_suit and max(suits.values()) == 2:
                return "ak_link"
            if not ace_suit and max(suits.values()) == 2:
                return "ak_link_lower_suit"
        if ace_suit and sorted(suits.values()) == [1, 1, 2]:
            if lo <= 5 and mid in (10, 11):
                return "ak_split"
            if mid <= 5:
                return "ak_low"
        return "review"
    if (
        lo <= 5
        and mid >= 10
        and top - mid == 2
        and max(suits.values()) == 2
        and open_bb is not None
        and open_bb <= 2.5
    ):
        return "small_open"
    if (
        top == 14
        and lo <= 4
        and 6 <= mid <= 9
        and max(suits.values()) <= 2
        and not ds
        and open_bb is not None
        and open_bb >= 3
        and (mid - lo >= 4 or (not ace_suit and high - mid >= 4))
    ):
        return "bare_ace"
    if top <= 13 and max(suits.values()) == 3 and mid <= 5 and high - mid >= 4:
        return "weak_suit"
    if (
        len(suits) == 4
        and flags["broadways"] <= 1
        and flags["connected_window"] < 4
        and not (top == 14 and high <= 5)
    ):
        return "rainbow"
    return "review"


def at_decision(snap):
    """Apply the guide only to the dealt Hero at the eligible predecision snapshot."""
    if (
        snap.street != "Preflop"
        or not snap.hero_cards
        or snap.board
        or len(snap.seats) != 6
        or snap.bb <= 0
    ):
        return None
    by_pos = {s.position: s for s in snap.seats}
    if (
        set(by_pos) != {"UTG", "HJ", "CO", "BTN", "SB", "BB"}
        or not by_pos["BTN"].is_hero
    ):
        return None
    if {s.position for s in snap.seats if s.live} != {"CO", "BTN", "SB", "BB"}:
        return None
    pos = {s.name: s.position for s in snap.seats}
    forced = {"posts_ante", "posts_sb", "posts_bb"}
    actions = [(pos[a.name], a.action) for a in snap.history if a.action not in forced]
    if actions != [("UTG", "fold"), ("HJ", "fold"), ("CO", "raise")]:
        return None
    blinds = sorted(
        (pos[a.name], a.action)
        for a in snap.history
        if a.action in {"posts_sb", "posts_bb"}
    )
    if blinds != [("BB", "posts_bb"), ("SB", "posts_sb")]:
        return None
    antes = [a.amount for a in snap.history if a.action == "posts_ante"]
    if antes and (
        len(antes) != 6
        or len({a.name for a in snap.history if a.action == "posts_ante"}) != 6
        or len(set(antes)) != 1
        or antes[0] * 100 > 16 * snap.bb
    ):
        return None
    co, btn, sb, bb = (by_pos[p] for p in ("CO", "BTN", "SB", "BB"))
    effective = min(co.stack + co.committed, btn.stack + btn.committed) / snap.bb
    open_bb = co.committed / snap.bb
    if (
        not 60 <= effective <= 150
        or min(sb.stack, bb.stack) < 40 * snap.bb
        or not 2 <= open_bb <= 3.5
        or btn.committed
        or snap.to_call != co.committed
        or bb.committed != snap.bb
        or not 0 < sb.committed <= snap.bb
        or snap.pot != sum(s.committed for s in snap.seats) + sum(antes)
    ):
        return None
    return GROUPS[hand_group(snap.hero_cards, open_bb)]
