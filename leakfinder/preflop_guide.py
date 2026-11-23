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
        "AA with two ace-high suits, a second high pair, or suited-ace support and two Broadway side cards",
        "3-bet",
        "Build the pot with the highest starting pair and additional high-card or two-suit support.",
        "As Ah Ks Kh",
    ),
    "queens": Group(
        "Double-suited QQ or JJ with a suited ace",
        "Manual review",
        "The ace removes some AA combinations. A connected fourth card and a wide CO can support a 3-bet. With a low fourth card, compare calling in position with folding against a tight range.",
        "Ah Qh Qs 9s",
    ),
    "kings": Group(
        "KK with a king-high suit and two connected middle cards",
        "Manual review",
        "The kings can flop a high set and the side cards can make straights. Calling keeps position against CO if the blinds fold. A tight CO and frequent squeezes make that call less attractive.",
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
        "Manual review",
        "All four ranks share a straight window. Look at the gap, highest flush possible, price and risk of a squeeze. Low rundowns can make a straight dominated by a higher rundown.",
        "Js Ts 9d 8c",
    ),
    "ak_ds": Group(
        "Double-suited AK with connected side cards",
        "Manual review",
        "AK and the two adjacent side cards can work as separate rank pairs. Calling can keep a wide opener in. Consider a 3-bet if worse hands will continue, and weigh the risk of squeezes when calling.",
        "Ac 7c Kh 8h",
    ),
    "ak_link": Group(
        "AK with an ace-high suit and two consecutive middle cards",
        "Manual review",
        "The ace-high suit and adjacent side cards provide several ways to play a flop. A large gap between the king and the middle cards limits how often these strengths work together.",
        "As Ks 8d 7h",
    ),
    "ak_link_lower_suit": Group(
        "AK with consecutive side cards and a suit without the ace",
        "Manual review",
        "The side cards work together and the flush potential is weaker. Favor a call against a smaller open, especially when the blinds rarely squeeze.",
        "Ad Ks 8s 7h",
    ),
    "ak_split": Group(
        "AK with an ace-high suit, a ten or jack, and a wheel card",
        "Manual review",
        "The low card can share a wheel straight with the ace. It does little for the high-card straight draws. Consider both the pot price and the blinds behind.",
        "As Kd Ts 4h",
    ),
    "ak_low": Group(
        "AK with an ace-high suit and two low cards that can make a wheel",
        "Manual review",
        "The ace connects with the low cards for a wheel. Those low cards limit other high combinations. The price and the blinds' tendencies matter in this close spot.",
        "As Kd 5s 2h",
    ),
    "small_open": Group(
        "Three consecutive Broadway cards and one low card, facing a small open",
        "Manual review",
        "An open of up to 2.5 BB gives the three high connected cards a better price. Calling is reasonable when the blinds rarely squeeze.",
        "Ks Jc Qc 3h",
    ),
    "bare_ace": Group(
        "Ace with a wheel card and a gap, facing a larger open",
        "Manual review",
        "The low card shares a wheel window with the ace. Some holdings also have a nut suit or adjacent middle cards. Weigh these against the larger open, rake and the players behind.",
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
    if counts[14] == 2 and (
        ds or counts[13] == 2 or counts[12] == 2 or (ace_suit and lo >= 10)
    ):
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
        and 2 <= open_bb <= 2.5
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
        and not (top == 14 and mid <= 5)
        and high - lo != 2
        and top - mid != 2
    ):
        return "rainbow"
    return "review"


def at_decision(snap):
    """Apply the guide only to the dealt Hero at the eligible predecision snapshot."""
    if (
        snap.street != "Preflop"
        or not snap.hero_cards
        or len(snap.hero_cards) != 4
        or snap.board
        or len(snap.seats) != 6
        or snap.bb <= 0
        or not snap.can_raise
        or any(s.live and s.all_in for s in snap.seats)
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
    opened = next(a for a in snap.history if a.action == "raise")
    effective = min(co.stack + co.committed, btn.stack + btn.committed) / snap.bb
    open_bb = co.committed / snap.bb
    if (
        not 60 <= effective <= 150
        or min(sb.stack, bb.stack) < 40 * snap.bb
        or not 2 <= open_bb <= 3.5
        or btn.committed
        or opened.amount != co.committed
        or opened.raised_to != co.committed
        or snap.to_call != co.committed
        or bb.committed != snap.bb
        or not 0 < sb.committed <= snap.bb
        or snap.pot != sum(s.committed for s in snap.seats) + sum(antes)
    ):
        return None
    return GROUPS[hand_group(snap.hero_cards, open_bb)]
