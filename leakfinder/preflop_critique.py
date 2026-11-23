"""Conditional, action-specific reviews of selected PLO4 cash preflop lines."""

from collections import Counter

from .critique import Critique
from .vendor.common import RANK_VALUE
from .vendor.primitives import classify_known_hand


def _strong_shape(cards, position):
    ranks = sorted(RANK_VALUE[c[0]] for c in cards)
    suits = Counter(c[1] for c in cards)
    suited = max(suits.values()) >= 2
    if ranks.count(13) == 2 and len(set(ranks)) == 3 and ranks[0] >= 10 and suited:
        return "Kings with two distinct Broadway side cards and a possible flush"
    if len(set(ranks)) != 4:
        return ""
    flags = classify_known_hand(cards)
    if (
        max(suits.values()) == 2
        and ranks[-1] == 14
        and ranks[0] >= 9
        and any(c[0] == "A" and suits[c[1]] >= 2 for c in cards)
        and (ranks[0] >= 10 or flags["double_suited"])
    ):
        return "Four different high ranks, including an ace that can make a nut flush"
    if (
        position in {"CO", "BTN", "BB"}
        and flags["double_suited"]
        and ranks[0] >= 8
        and ranks[-1] - ranks[0] == 3
    ):
        return "Four consecutive high ranks and two separate flush possibilities"
    return ""


def _weak_shape(cards):
    ranks = sorted(RANK_VALUE[c[0]] for c in cards)
    if ranks[-1] > 12:
        return ""
    suits = Counter(c[1] for c in cards)
    tripled = next((r for r in ranks if ranks.count(r) == 3), None)
    if tripled is not None and tripled <= 7 and ranks[-1] - ranks[0] >= 5:
        return "Three cards share a small rank, leaving one out to hit that rank, and the side card cannot make a straight with it."
    if len(set(ranks)) != 4:
        return ""
    flags = classify_known_hand(cards)
    if flags["connected_window"] < 3 and len(suits) == 4:
        return "Four different suits rule out a flush and at most two hole ranks fit in any five-rank straight window."
    if flags["connected_window"] < 3 and len(suits) < 4 and max(suits.values()) >= 3:
        return "Three or more cards share a non-ace suit, taking away flush outs, and at most two ranks fit in any straight window."
    if (
        ranks[-1] <= 9
        and ranks[-1] - ranks[0] >= 6
        and all(ranks[i + 2] - ranks[i] > 2 for i in (0, 1))
    ):
        return "The highest card is a nine or lower, the ranks span at least seven spots and no three are consecutive. Higher straights can dominate and no suited hole card ranks above a nine."
    return ""


def _large_fivebet(snap, event, entries, hero, live):
    # Only a heads-up, straight raise / reraise / 4-bet sequence. Extra chips
    # beyond the opponent's stack would be returned, so cap both contributions.
    if len(live) != 2 or len(entries) != 3 or any(a.action != "raise" for a in entries):
        return None
    opener, threebet, fourbet = entries
    opponent = live.get(fourbet.name)
    if not opponent or opener.name != opponent.name or threebet.name != hero.name:
        return None
    if not fourbet.raised_to or "A" in {c[0] for c in snap.hero_cards}:
        return None
    if not 0 < snap.to_call < event.amount <= hero.stack:
        return None
    added = min(event.amount, opponent.stack + snap.to_call)
    opponent_call = added - snap.to_call
    final_pot = snap.pot + added + opponent_call
    behind = min(hero.stack - added, opponent.stack - opponent_call)
    if opponent_call <= 0 or added < 30 * snap.bb or behind / final_pot > 0.5:
        return None
    reason = (
        f"The {opponent.position} 4-bet to {fourbet.raised_to / snap.bb:g} BB. "
        f"Your 5-bet risks another {added / snap.bb:g} BB against that player. "
        "With no ace in your hand, all four aces are available to the opponent. "
    )
    if behind == 0:
        reason += (
            f"If always called, the showdown share needed to break even against folding is "
            f"{added / final_pot:.1%} before rake. The actual share depends on the calling range."
        )
    else:
        reason += (
            f"If called, the pot would be {final_pot / snap.bb:g} BB with only "
            f"{behind / snap.bb:g} BB effective left to play."
        )
    return Critique(
        "preflop_nonace_fivebet",
        "Committed heavily against a 4-bet without an ace",
        "Compare calling the 4-bet",
        reason,
        "Wide 4-bets and folds to further raises can justify the 5-bet. Connected or double-suited cards can retain equity against aces. Check this opponent's range before judging it.",
    )


def _vulnerable_cold_threebet(snap, entries, hero, live, cost):
    opener, raiser = entries
    if (
        raiser.action != "raise"
        or not raiser.raised_to
        or hero.name in {opener.name, raiser.name}
        or opener.name not in live
        or raiser.name not in live
        or raiser.raised_to < 2.5 * opener.raised_to
        or snap.to_call < 7.5 * snap.bb
        or live[opener.name].stack <= raiser.raised_to - live[opener.name].committed
    ):
        return None
    ranks = [RANK_VALUE[c[0]] for c in snap.hero_cards]
    suits = Counter(c[1] for c in snap.hero_cards)
    if (
        14 in ranks
        or min(ranks) + 5 > max(ranks)
        or any(ranks.count(r) > 1 for r in (11, 12, 13))
    ):
        return None
    tops = [
        max(RANK_VALUE[r] for r, s in snap.hero_cards if s == suit)
        for suit, count in suits.items()
        if count >= 2
    ]
    if len(tops) > 1 or (tops and tops[0] > 10):
        return None
    suit_note = (
        "Your only flush suit is topped by a ten or lower."
        if tops
        else "You have no possible flush."
    )
    return Critique(
        "preflop_cold_threebet_exposed",
        "Called two raises before the opener responded",
        "Consider folding to the 3-bet",
        f"The {live[raiser.name].position} 3-bet to {raiser.raised_to / snap.bb:g} BB. "
        f"The {live[opener.name].position} opener still has chips and can 4-bet. {suit_note} {cost}",
        "A wide 3-bettor, an opener who seldom 4-bets, and good postflop position can make calling defensible. Check all three before relying on the displayed price.",
    )


def _discounted_blind_fold(snap, opening, live, price):
    opponent = live.get(opening.name)
    if (
        len(live) != 2
        or not opponent
        or opponent.position not in {"SB", "BTN"}
        or opening.action != "raise"
        or not opening.raised_to
        or price > 0.28
        or not 2 <= opening.raised_to / snap.bb <= 2.5
    ):
        return None
    cards = snap.hero_cards
    ranks = sorted({RANK_VALUE[c[0]] for c in cards})
    suits = Counter(c[1] for c in cards)
    if len(ranks) != 4 or max(suits.values()) != 2:
        return None
    if ranks[0] >= 6 and ranks[-1] - ranks[0] <= 4:
        structure = "All four ranks fit within five consecutive ranks and you have a possible flush."
    elif (
        ranks[-1] == 14
        and ranks[1] >= 10
        and any(c[0] == "A" and suits[c[1]] == 2 for c in cards)
    ):
        structure = "You have an ace-high suit and two different Broadway side cards."
    else:
        return None
    position = (
        "You would have postflop position."
        if opponent.position == "SB"
        else "BTN would have postflop position."
    )
    return Critique(
        "preflop_discounted_bb_fold",
        "Folded a supported hand to a small late open",
        "Compare defending with a call",
        f"{structure} Calling the {opponent.position} open costs {snap.to_call / snap.bb:g} BB, "
        f"{price:.1%} of the resulting pot before rake. Your call closes the betting. {position}",
        "High rake, a tight small-open range or poor equity realization can justify folding. The pot price alone cannot establish the value of a call.",
    )


def review_preflop(snap, event, advice):
    """Assess only the recorded choice against a specific, available alternative."""
    if advice is None or snap.street != "Preflop" or snap.board or not snap.can_raise:
        return None
    cards = snap.hero_cards
    if (
        not cards
        or len(cards) != 4
        or len(set(cards)) != 4
        or snap.bb <= 0
        or snap.pot <= 0
    ):
        return None
    if not 3 <= len(snap.seats) <= 6:
        return None
    live = {s.name: s for s in snap.seats if s.live}
    hero = live.get(snap.hero)
    if not hero or not hero.is_hero or len(live) < 2 or snap.to_call >= hero.stack:
        return None
    if any(s.all_in for s in live.values()):
        return None
    if any(
        not 40
        <= min(hero.stack + hero.committed, s.stack + s.committed) / snap.bb
        <= 150
        for s in live.values()
        if s != hero
    ):
        return None
    antes = [a for a in snap.history if a.action == "posts_ante"]
    if antes and (
        len(antes) != len(snap.seats)
        or len({a.name for a in antes}) != len(antes)
        or len({a.amount for a in antes}) != 1
        or antes[0].amount > 0.16 * snap.bb
    ):
        return None
    entries = [a for a in snap.history if a.action in {"raise", "call", "check"}]
    pos = hero.position
    played = event.action
    if pos == "BB" and played == "fold" and len(entries) == 1 and snap.to_call:
        return _discounted_blind_fold(
            snap, entries[0], live, snap.to_call / (snap.pot + snap.to_call)
        )
    if (
        played == "call"
        and pos in {"CO", "BTN"}
        and len(entries) == 1
        and entries[0].action == "call"
        and snap.to_call == snap.bb
        and sum(c[0] == "A" for c in cards) == 2
        and entries[0].name in live
    ):
        return Critique(
            "preflop_overlimped_aces",
            "Overlimped aces in position on a limper",
            "Compare an isolation raise",
            f"The {live[entries[0].name].position} limped. Paying one blind from {pos} gives the blinds a cheap way into the pot. "
            "With two aces, raising can charge weaker hands and narrow the field.",
            "Overlimping can induce a raise behind. Poor side cards or a limper who only continues with strong hands can favor keeping the pot small.",
        )
    if played == "raise" and len(entries) == 3:
        challenge = _large_fivebet(snap, event, entries, hero, live)
        if challenge:
            return challenge
    if not entries and pos in {"UTG", "HJ", "CO", "BTN"} and snap.to_call == snap.bb:
        if played == "fold":
            shape = _strong_shape(cards, pos)
            if shape:
                return Critique(
                    "preflop_fold_supported_open",
                    "Passed on a supported opening hand",
                    "Open-raise",
                    f"{shape}. The pot reached {pos} unopened. Raising can win the blinds or build a pot with several ways to improve.",
                    "Frequent reraises behind and high rake make marginal opens less attractive. Check the players and stakes before widening.",
                )
        if pos in {"UTG", "HJ"} and played in {"raise", "call"}:
            weak = _weak_shape(cards)
            if weak:
                return Critique(
                    "preflop_early_weak_entry",
                    "Entered first in with weak support",
                    "Fold",
                    f"{weak} Entering from {pos} leaves at least CO, BTN and the blinds scheduled behind you.",
                    "An open-raise can still profit if the remaining players overfold. A very passive table can also let a limp see a cheap flop.",
                )
        return None
    if (
        pos == "BB"
        and played == "check"
        and not snap.to_call
        and len(entries) == 1
        and len(live) == 2
    ):
        limp = entries[0]
        other = live.get(limp.name)
        if limp.action == "call" and other and other.position in {"SB", "BTN", "CO"}:
            shape = _strong_shape(cards, pos)
            if shape:
                return Critique(
                    "preflop_checked_supported_limp",
                    "Checked a strong hand against one limper",
                    "Raise the limper",
                    f"{shape}. Only the {other.position} limper remains. A raise can build the pot against weaker hands that call or collect it immediately.",
                    "Checking guarantees a free flop. A limper who traps or who continues almost exclusively with stronger hands gives raising less value.",
                )
        return None
    if played not in {"call", "raise"} or not entries or not snap.to_call:
        return None
    opening = entries[0]
    if (
        opening.action != "raise"
        or not opening.raised_to
        or not 2.5 <= opening.raised_to / snap.bb <= 3.5
    ):
        return None
    price = snap.to_call / (snap.pot + snap.to_call)
    cost = f"Calling costs {snap.to_call / snap.bb:g} BB, {price:.1%} of the pot after calling before rake or later betting."
    if len(entries) == 2 and played == "call" and price >= 0.29:
        challenge = _vulnerable_cold_threebet(snap, entries, hero, live, cost)
        if challenge:
            return challenge
    weak = _weak_shape(cards)
    if not weak:
        return None
    if (
        len(entries) == 2
        and opening.name == hero.name
        and entries[1].action == "raise"
        and played == "call"
    ):
        threebet = entries[1]
        opponent = live.get(threebet.name)
        if (
            len(live) == 2
            and opponent
            and threebet.raised_to
            and threebet.raised_to >= 2.7 * opening.raised_to
            and snap.to_call >= 6 * snap.bb
            and price >= 0.30
        ):
            return Critique(
                "preflop_weak_threebet_defense",
                "Paid a sizable 3-bet with weak support",
                "Fold to the 3-bet",
                f"After your {pos} open the {opponent.position} 3-bet to {threebet.raised_to / snap.bb:g} BB. {weak} {cost}",
                "A documented wide reraising range or a large postflop edge can make calling more attractive. Compare the hands this opponent actually reraises.",
            )
        return None
    if (
        opening.name == hero.name
        or opening.name not in live
        or any(a.action != "call" for a in entries[1:])
    ):
        return None
    opener_pos = live[opening.name].position
    if played == "call" and pos != "BB" and price >= 0.29:
        return Critique(
            "preflop_weak_cold_call",
            "Paid an open with weak support",
            "Fold to the open",
            f"The {opener_pos} opened to {opening.raised_to / snap.bb:g} BB. {weak} {cost}",
            "A wide opener who gives up too often after the flop can make a call more attractive. Check who can still squeeze and how often you realize your equity.",
        )
    if (
        played == "raise"
        and opener_pos in {"UTG", "HJ", "CO"}
        and event.amount >= 8 * snap.bb
    ):
        return Critique(
            "preflop_weak_reraise",
            "Committed a large reraise with weak support",
            "Fold before reraising",
            f"The {opener_pos} opened to {opening.raised_to / snap.bb:g} BB. Your reraise added {event.amount / snap.bb:g} BB to a {snap.pot / snap.bb:g} BB pot. {weak}",
            "A reraise can win immediately against an opener and callers who overfold. Check their responses to comparable sizes before treating this bluff as repeatable.",
        )
    return None
