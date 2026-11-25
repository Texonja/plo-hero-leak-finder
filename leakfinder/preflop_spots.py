"""Position-specific, conditional PLO4 opening and three-bet reviews."""

from collections import Counter

from .critique import Critique
from .preflop_critique import strong_shape
from .vendor.common import RANK_VALUE

POSTFLOP_ORDER = ("SB", "BB", "UTG", "HJ", "CO", "BTN")


def _opening(snap, hero, actions):
    return (
        not actions
        and hero.position in {"UTG", "HJ", "CO", "BTN", "SB"}
        and snap.to_call == snap.bb - hero.committed
        and snap.to_call > 0
    )


def _threebet(snap, hero, actions, live):
    if (
        len(actions) == 2
        and len(live) == 2
        and all(a.action == "raise" and a.raised_to for a in actions)
        and actions[0].name == hero.name
        and actions[1].name != hero.name
        and snap.to_call > 0
    ):
        return live.get(actions[1].name)
    return None


def _shape(cards):
    ranks = sorted(RANK_VALUE[r] for r, _ in cards)
    counts = Counter(s for _, s in cards)
    suit_tops = sorted(
        (
            max(RANK_VALUE[r] for r, s in cards if s == suit)
            for suit, count in counts.items()
            if count >= 2
        ),
        reverse=True,
    )
    distinct = sorted(set(ranks))
    wheel = ([1] + distinct[:-1]) if distinct[-1] == 14 else distinct
    fits = [
        values[i + 2] - values[i]
        for values in (distinct, wheel)
        for i in range(len(values) - 2)
    ]
    return (
        ranks,
        distinct,
        tuple(suit_tops),
        min(fits, default=13),
        max(counts.values()),
    )


def opening_edges(cards, position):
    """Only the ends of the study range. Middle hands stay available for review."""
    ranks, distinct, tops, three_span, suit_count = _shape(cards)
    strong = strong_shape(cards, position) if suit_count <= 2 else ""
    if position in {"UTG", "HJ"} and ranks[-1] == 14 and ranks[0] < 10:
        strong = ""
    if (
        not strong
        and position in {"CO", "BTN"}
        and len(distinct) == 4
        and len(tops) == 2
        and ranks[0] >= 7
        and ranks[-1] - ranks[0] <= 4
    ):
        strong = "All four ranks fit one straight window from the seven up, with two flush suits"
    # A pair, an ace, a second suit or a useful three-card connection can all
    # change the decision. Three cards of one suit only give one flush path.
    weak = ""
    if (
        len(distinct) == 4
        and ranks[-1] <= 13
        and sum(r >= 10 for r in ranks) <= 1
        and three_span > 4
        and (not tops or len(tops) == 1 and tops[0] <= 10)
    ):
        suit = (
            "no flush possible"
            if suit_count == 1
            else "only a ten-high or lower flush suit"
        )
        weak = f"Four unpaired ranks, no three within one straight window, and {suit}"
    return strong, weak


def opening_comparison(cards, position, mixed, premium):
    """Suggested first-in lines outside the blinds, conditional on typical opposition."""
    strong, weak = opening_edges(cards, position)
    if strong or (mixed and premium):
        reason = strong or "A pair of aces can build a value pot against the field"
        return (
            ("raise",),
            reason
            + ". Frequent reraises behind and rake can reduce the return on opening.",
        )
    if weak and position in {"UTG", "HJ"}:
        return (
            ("fold",),
            weak
            + ". Entering from early position leaves several players to act. Frequent folds behind can make a steal worthwhile.",
        )
    return None


def stack_context(snap, hero, seats, entries):
    """A voluntary opponent must fit the normal-depth model. Unacted seats still matter."""
    others = [s for s in seats if s.name != hero.name]
    outside = {
        s.name
        for s in others
        if not 40 * snap.bb
        <= min(hero.stack + hero.committed, s.stack + s.committed)
        <= 150 * snap.bb
    }
    if not outside:
        return "standard", ""
    if len(outside) == len(others) or any(a.name in outside for a in entries):
        return (
            "outside",
            "The effective stack against a player already in the pot, or against every remaining opponent, falls outside 40–150 BB.",
        )
    outliers = ", ".join(s.position for s in others if s.name in outside)
    return (
        "mixed",
        f"Effective depth against {outliers} is outside 40–150 BB. These seats can still enter or reraise.",
    )


def _opening_support(ranks, distinct, tops, _trip_span, suit_count, position):
    lowest, _, second, highest = ranks
    late = position in {"CO", "BTN"}
    if ranks.count(14) == 2:
        return (
            "A pair of aces gives you an immediate value hand with top-set potential."
        )
    if (
        position == "BTN"
        and (ranks.count(13) == 2 or ranks.count(12) == 2)
        and len(distinct) >= 2
    ):
        pair_rank = 13 if ranks.count(13) == 2 else 12
        if not tops and len(distinct) == 3 and all(
            abs(v - pair_rank) > 4 for v in distinct if v != pair_rank
        ):
            return ""
        return "Your high pair can flop top set, and you will act last after the flop."
    if not tops or len(distinct) != 4 or suit_count != 2:
        return ""
    ds = len(tops) == 2
    gap = highest - lowest
    trip_span = min(distinct[2] - distinct[0], distinct[3] - distinct[1])
    if lowest >= (5 if ds and late else 7) and gap <= (4 if ds or late else 3):
        return "All four ranks fit within one straight window. " + (
            "Both suits add flush paths."
            if ds
            else "The suited pair adds a flush path."
        )
    if (
        tops[0] == 14
        and second >= 10
        and trip_span <= (4 if late else 3)
        and (ds or lowest >= (5 if position == "BTN" else 7))
    ):
        return "The ace-high suit can make the nut flush, and three ranks fit in one straight window."
    if position == "BTN" and ds and tops[0] >= 12 and second >= 9 and trip_span <= 3:
        return "You have two flush suits, one queen-high or better, plus three ranks in a compact straight window."
    if (
        position == "BTN"
        and tops[0] >= 13
        and second >= 10
        and trip_span <= 4
        and lowest >= 6
    ):
        return "You have a king-high or ace-high flush suit and three ranks within one straight window."
    if position == "BTN" and tops[0] == 14 and lowest >= 5 and trip_span <= 3:
        return "You have the nut flush suit and three ranks close enough to share several straight paths."
    return ""


def _thin_structure(ranks, distinct, tops, trip_span, suit_count):
    if ranks.count(14) >= 2 or tops and tops[0] == 14 or len(tops) == 2:
        return ""
    if len(distinct) == 4 and not tops and trip_span > 4:
        return "Four different suits rule out a flush and no three ranks fit in one straight window."
    pairs = [r for r in distinct if ranks.count(r) == 2]
    if (
        len(pairs) == 1
        and pairs[0] <= 9
        and trip_span > 4
        and (not tops or tops[0] <= 11)
    ):
        return "Your pair is nine-high or lower, no three ranks fit one straight window, and you lack a high flush suit."
    if (
        len(distinct) == 4
        and tops
        and tops[0] <= 10
        and trip_span > 4
        and ranks[-2] < 11
    ):
        return "Your only flush suit is ten-high or lower. The two highest ranks have little straight support from the other cards."
    return ""


def _threebet_support_issue(ranks, distinct, tops, trip_span, suit_count):
    thin = _thin_structure(ranks, distinct, tops, trip_span, suit_count)
    if thin:
        return thin
    if len(distinct) != 4 or tops and tops[0] == 14:
        return ""
    if (
        ranks[-1] <= 11
        and trip_span >= 4
        and ranks[-1] - ranks[0] >= 6
        and (not tops or tops[0] <= 10)
    ):
        return "You lack a high flush suit and the ranks are spread out. Higher rundowns can dominate some of your straight draws."
    if (
        len(tops) <= 1
        and trip_span >= 5
        and ranks[-2] <= 11
        and (not tops or tops[0] <= 12)
    ):
        return "No three ranks fit a straight window and you have no ace- or king-high flush path."
    return ""


def _review_open(snap, event, hero, live, shape):
    if event.action in {"fold", "call"}:
        ranks, _, suit_tops, _, _ = shape
        suited_ace_pair = ranks.count(14) == 2 and suit_tops and suit_tops[0] == 14
        if hero.position == "SB" and not (event.action == "call" and suited_ace_pair):
            return None
        support = _opening_support(*shape, hero.position)
        if support:
            return Critique(
                "rfi_supported_hand",
                "Passed on a first-in open with a playable hand",
                "Consider an open-raise",
                f"{support} The unopened pot reached {hero.position}, with {len(live) - 1} players left. "
                "Raising can win the blinds immediately or build a pot with a hand that can improve.",
                "A limp can be an intentional trap at a table that raises often, particularly from the small blind."
                if event.action == "call"
                else "Tightening up can pay under heavy 3-bet pressure or high rake.",
            )
    if event.action == "raise" and hero.position in {"UTG", "HJ"} and len(live) >= 4:
        weak = _thin_structure(*shape)
        if weak:
            risk = event.amount / (event.amount + snap.pot)
            return Critique(
                "rfi_thin_early_raise",
                "Opened early with limited ways to improve",
                "Consider folding first in",
                f"{weak} You raised from {hero.position} with {len(live) - 1} players yet to act. "
                f"A pure steal at this size needs all of them to fold {risk:.1%} of the time to break even.",
                "That threshold assumes zero return when anyone continues. "
                "Players who fold too much or give up after the flop can make a raise profitable.",
            )
    return None


def _threebet_line(snap, hero, opponent, actions):
    op, tb = (a.raised_to / snap.bb for a in actions)
    ip = POSTFLOP_ORDER.index(hero.position) > POSTFLOP_ORDER.index(opponent.position)
    final = snap.pot + snap.to_call
    price = snap.to_call / final
    spr = min(hero.stack - snap.to_call, opponent.stack) / final
    return (
        f"{opponent.position} raised your {hero.position} open from {op:g} to {tb:g} BB. "
        f"Calling costs {snap.to_call / snap.bb:.2f} BB ({price:.1%} of the pot after calling), "
        f"giving a flop stack-to-pot ratio of {spr:.1f}. "
        f"You would play {'in' if ip else 'out of'} position."
    )


def threebet_price_note(snap):
    if snap.street != "Preflop" or not snap.to_call or snap.pot <= 0 or snap.bb <= 0:
        return ""
    live = {s.name: s for s in snap.seats if s.live}
    hero = live.get(snap.hero)
    if (
        not hero
        or snap.to_call >= hero.stack
        or any(s.position not in POSTFLOP_ORDER or s.all_in for s in live.values())
    ):
        return ""
    actions = tuple(a for a in snap.history if a.action in {"raise", "call", "check"})
    opponent = _threebet(snap, hero, actions, live)
    if opponent:
        return (
            _threebet_line(snap, hero, opponent, actions)
            + " Pot price is before rake and future betting."
        )
    return ""


def _review_threebet(snap, event, hero, opponent, actions, shape):
    op, tb = (a.raised_to / snap.bb for a in actions)
    depth = min(hero.stack + hero.committed, opponent.stack + opponent.committed)
    if (
        not 2 <= op <= 4.7
        or not 2.25 * op <= tb <= 4.8 * op
        or not 40 <= depth / snap.bb <= 180
    ):
        return None
    ip = POSTFLOP_ORDER.index(hero.position) > POSTFLOP_ORDER.index(opponent.position)
    price = snap.to_call / (snap.pot + snap.to_call)
    weak = _threebet_support_issue(*shape)
    if (
        event.action == "call"
        and weak
        and not ip
        and price >= 0.285
        and snap.to_call >= 5 * snap.bb
    ):
        return Critique(
            "threebet_thin_oop_call",
            "Called a large 3-bet out of position",
            "Consider folding",
            f"{_threebet_line(snap, hero, opponent, actions)} {weak} Raw equity can be hard to realize when acting first.",
            "A wide 3-bettor or a near-all-in stack makes a call more attractive. Compare against "
            "their real reraising range. The pot price excludes rake and future betting.",
        )
    ranks, _, tops, _, _ = shape
    if (
        event.action == "raise"
        and not ip
        and not weak
        and 14 not in ranks
        and len(tops) <= 1
    ):
        new_total = min(hero.committed + event.amount, depth)
        if (
            55 * snap.bb <= depth <= 115 * snap.bb
            and new_total >= 0.34 * depth
            and price >= 0.29
        ):
            return Critique(
                "threebet_committing_fourbet",
                "Built a big 4-bet pot out of position without an ace",
                "Compare the 4-bet with calling",
                f"{_threebet_line(snap, hero, opponent, actions)} If called, the 4-bet commits {new_total / depth:.0%} of your effective starting stack. "
                "All opposing ace pairs remain available. Calling keeps the opponent's weaker 3-bets in and preserves more room to fold later.",
                "Reraising can work against a wide 3-bettor and can reduce the cost of playing out of position. "
                "Favor it more when they fold often or continue against a 4-bet with weaker hands.",
            )
    return None


def review_common(snap, event):
    """Review an opening or a single-reraiser line using only its observed prefix."""
    if (
        snap.street != "Preflop"
        or len(snap.hero_cards or ()) != 4
        or snap.bb <= 0
        or snap.pot <= 0
        or not 3 <= len(snap.seats) <= 6
    ):
        return None
    live = {s.name: s for s in snap.seats if s.live}
    hero = live.get(snap.hero)
    if (
        not hero
        or not hero.is_hero
        or hero.stack <= snap.to_call
        or not snap.can_raise
        or any(s.position not in POSTFLOP_ORDER or s.all_in for s in live.values())
    ):
        return None
    actions = tuple(a for a in snap.history if a.action in {"raise", "call", "check"})
    if len(actions) == 1 or len(actions) > 2:
        return None
    antes = [a for a in snap.history if a.action == "posts_ante"]
    if antes and (
        len(antes) != len(snap.seats)
        or len({a.name for a in antes}) != len(antes)
        or len({a.amount for a in antes}) != 1
        or antes[0].amount > 0.16 * snap.bb
    ):
        return None
    shape = _shape(snap.hero_cards)
    if _opening(snap, hero, actions) and all(
        35 <= min(hero.stack + hero.committed, s.stack + s.committed) / snap.bb <= 180
        for s in live.values()
        if not s.is_hero
    ):
        return _review_open(snap, event, hero, live, shape)
    opponent = _threebet(snap, hero, actions, live)
    if opponent:
        return _review_threebet(snap, event, hero, opponent, actions, shape)
    return None
