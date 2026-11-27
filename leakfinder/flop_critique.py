"""Action-specific, conditional reviews of selected flop decisions."""

from collections import Counter
from itertools import combinations

from .critique import Critique
from .early_postflop import _straight_pairs, next_draws
from .opponent_history import heads_up_price
from .postflop_value import RANK_LABEL, bet_size
from .river_holdings import CARD_ID, DECK, UNIT, _score, _scores, nuts
from .vendor.common import RANK_VALUE
from .vendor.primitives import best_plo_hand


def flop_context(snap):
    live = [seat for seat in snap.seats if seat.live]
    if snap.street != "Flop" or len(live) != 2 or snap.pot <= 0 or snap.bb <= 0:
        return None
    hero = next(
        (seat for seat in live if seat.name == snap.hero and seat.is_hero), None
    )
    if hero is None or hero.all_in:
        return None
    opponent = next(seat for seat in live if seat != hero)
    actions = tuple((a.name, a.action) for a in snap.history if a.street == "Flop")
    raiser = next(
        (
            a.name
            for a in reversed(snap.history)
            if a.street == "Preflop" and a.action == "raise"
        ),
        None,
    )
    if (snap.to_call and actions == ((hero.name, "bet"), (opponent.name, "raise"))
            and not any(s.committed for s in snap.seats if not s.live)):
        return "raised", hero, opponent, raiser
    if (
        snap.to_call
        and raiser == opponent.name
        and actions
        in (
            ((opponent.name, "bet"),),
            ((hero.name, "check"), (opponent.name, "bet")),
        )
        and not any(s.committed for s in snap.seats if not s.live)
    ):
        return "cbet", hero, opponent, raiser
    if (
        not snap.to_call
        and not opponent.all_in
        and actions == ((opponent.name, "check"),)
    ):
        return "checked", hero, opponent, raiser
    return None


def _cbet_price(snap, opponent):
    bet = snap.history[-1]
    opening = snap.pot - bet.amount
    if opening <= 0 or bet.action != "bet" or bet.amount != snap.price_to_match:
        return None
    price = heads_up_price(snap)
    cost = (
        f"{opponent.position} made a {bet.amount / opening:.0%}-pot continuation bet. "
        f"Calling costs {snap.to_call / snap.bb:g} BB, or {price:.1%} of "
        "the contestable pot after calling, before rake."
    )
    return cost, price, min(bet.amount, snap.to_call) / opening


def flop_cbet_note(snap):
    context = flop_context(snap)
    if not context or context[0] != "cbet":
        return ""
    _, hero, opponent, _ = context
    details = _cbet_price(snap, opponent)
    if not details:
        return ""
    cost, _, _ = details
    left = min(hero.stack - snap.to_call, opponent.stack)
    note = cost + (
        " No further betting is possible after a call."
        if left == 0
        else f" A call leaves {left / snap.bb:g} BB effective."
    )
    hole, board = tuple(sorted(snap.hero_cards or ())), tuple(sorted(snap.board))
    if len(hole) == 4 and len(board) == 3 and best_plo_hand(hole, board)[0] < 3:
        draws = next_draws(hole, board)
        note += (
            f" {draws.total} unseen turn cards make a straight or flush"
            + (
                f", including {draws.nuts} that make the current nuts"
                if draws.nuts
                else ""
            )
            + ". The turn count can change when the opponent blocks cards or the river changes the board."
        )
    return note


def _weak_oop_call(snap, hero, opponent, hole, board, facing_raise):
    """Only direct heads-up calls with a meaningful turn bet still available."""
    call = snap.to_call
    flat_pot = snap.pot + call
    left = min(hero.stack - call, opponent.stack)
    if (opponent.all_in or call != snap.price_to_match or not call
            or left < flat_pot / 2 or len({c[0] for c in board}) != 3
            or len({c[1] for c in board}) != 3):
        return None
    pre = [a for a in snap.history if a.street == "Preflop" and a.action == "raise"]
    rank, kicks, *_ = best_plo_hand(hole, board)
    board_high = max(RANK_VALUE[c[0]] for c in board)
    if facing_raise:
        # Hero bet first and faces the in-position player's raise, not a
        # check-raise against Hero in position or a cheap minraise.
        if (not pre or pre[-1].name != hero.name or rank != 0
                or call / flat_pot < 0.25):
            return None
        draws = next_draws(hole, board)
        if draws.total:
            return None
        # Several distinct overcards can turn a strong pair even on this texture.
        if sum(RANK_VALUE[c[0]] > board_high for c in hole) > 1:
            return None
        return Critique(
            "flop_raise_call_no_draw", "Called a flop raise with high card", "Prefer folding",
            f"Calling the raise costs {call / snap.bb:g} BB out of position with only high card. "
            f"No turn makes a straight or flush, and {left / snap.bb:g} BB can still go in.",
            "",
        )
    # A large c-bet in a 3-bet pot against a low pocket pair. Limit this
    # review to short, non-nut straight draws and modest implied odds.
    if (len(pre) != 2 or pre[-1].name != opponent.name or call / flat_pot < 0.29
            or left > 3 * flat_pot or rank != 1 or kicks[0] > 9
            or kicks[0] >= board_high
            or sum(RANK_VALUE[c[0]] == kicks[0] for c in hole) != 2
            or _nut_backdoor(hole, board)
            or max(RANK_VALUE[c[0]] for c in hole) > board_high):
        return None
    draws = next_draws(hole, board)
    if not 0 < draws.straight <= 4 or draws.nuts or draws.flush:
        return None
    return Critique(
        "flop_cbet_call_low_gutshot", "Paid a large c-bet with a low pair and a weak draw",
        "Prefer folding",
        f"You paid {call / snap.bb:g} BB out of position with a low pocket pair. "
        f"Only {draws.straight} turns make a straight, none make the nuts, "
        f"and {left / snap.bb:g} BB remains effective.",
        "",
    )


def _cbet(snap, hero, opponent, hole, board):
    details = _cbet_price(snap, opponent)
    if not details:
        return None
    cost, price, fraction = details
    if fraction < 0.60 or price < 0.26:
        return None
    left = min(hero.stack - snap.to_call, opponent.stack)
    rank, kicks, *_ = best_plo_hand(hole, board)
    suits = Counter(c[1] for c in board)
    suit, texture = suits.most_common(1)[0]
    in_suit = [c for c in hole if c[1] == suit]
    top = max(RANK_VALUE[c[0]] for c in board)
    if left == 0 and rank <= 2:
        # Use only the exact stack-off bound for closing calls. Future
        # realization and facing another bet cannot justify a fold here.
        return _closing_call(hole, board, price, cost)
    if (
        left >= (snap.pot + snap.to_call) / 2
        and texture <= 2
        and len({c[0] for c in board}) == 3
        and (rank == 0 or rank == 1 and kicks[0] < top)
    ):
        draws = next_draws(hole, board)
        if (
            draws.total <= 2
            and not _nut_backdoor(hole, board)
            and (texture == 1 or len(in_suit) < 2)
        ):
            return Critique(
                "flop-cbet-call-thin",
                "Paid a large c-bet with limited turn improvements",
                "Compare call and fold",
                f"{cost} You have {'high card' if rank == 0 else 'less than top pair'}, "
                f"no front-door flush draw, and {draws.total} turn cards that make a straight or flush. "
                "A pair can already lead missed high cards. Compare the bettor's made hands and draws, "
                "the stacks left after calling and how often you face another barrel.",
                "A call keeps weaker hands in and can win when the opponent gives up. "
                "A fold saves the price against ranges that keep applying pressure.",
            )
    return None


def _nut_backdoor(hole, board):
    return any(
        sum(c[1] == suit for c in board) == 1
        and sum(c[1] == suit for c in hole) >= 2
        and next(c for c in reversed(DECK) if c[1] == suit and c not in board) in hole
        for suit in "cdhs"
    )


def _closing_call(hole, board, price, cost):
    if len({c[0] for c in board}) != 3:
        return None  # A paired board needs separate full house and trips treatment.
    if max(Counter(c[1] for c in board).values()) == 3:
        minimum, value = 5, "a flush or better"
    elif _straight_pairs(frozenset(RANK_VALUE[c[0]] for c in board)):
        minimum, value = 4, "a straight or better"
    else:
        minimum, value = 3, "a set or better"
    hole_ids, board_ids = (tuple(CARD_ID[c] for c in cards) for cards in (hole, board))
    pairs = tuple(combinations(hole_ids, 2))
    deck = set(range(52)).difference(hole_ids, board_ids)
    favorable = 0
    for runout in combinations(sorted(deck), 2):
        scores = _scores((*board_ids, *runout))
        favorable += any(_score(a, b, *scores) // UNIT >= minimum for a, b in pairs)
    # There are 45 unseen cards, but a fixed opponent holds four of them.
    # The 820 legal runouts for any such holding are a subset of these 990.
    bound = min(1, favorable / 820)
    if bound + 0.05 > price:
        return None
    weak = (price - bound) / (1 - bound)
    return Critique(
        "flop-cbet-stackoff-bound",
        "Closing flop call against different betting ranges",
        "Compare call and fold by range",
        f"{cost} No further betting is possible. Against {value}, at most {favorable} of "
        f"820 possible turn–river pairs per fixed opponent can even reach that category for Hero. "
        f"Treating all of them as wins gives a generous {bound:.1%} equity ceiling. "
        f"Even if you always beat the rest, at least {weak:.1%} of the betting range must be outside that value group.",
        "Draws and weaker made hands can raise your equity enough to call. Check what else "
        "this opponent bets for this size and stack depth.",
    )


def _value_type(hole, board, rank, kicks):
    values = frozenset(RANK_VALUE[c[0]] for c in board)
    texture = max(Counter(c[1] for c in board).values())
    if len(values) != 3 or texture == 3:
        return ""
    has_straight = bool(_straight_pairs(values))
    if rank == 3 and not has_straight:
        if kicks[0] == max(values):
            return "top set"
        if kicks[0] == sorted(values)[1]:
            return "middle set"
    if rank == 4 and texture == 2 and nuts(hole, board, rank, tuple(kicks)):
        return "the nut straight"
    return ""


def _checked(snap, event, hero, villain, raiser, hole, board):
    if not snap.can_raise or min(hero.stack, villain.stack) < 1.5 * snap.pot:
        return None
    rank, kicks, *_ = best_plo_hand(hole, board)
    kind = _value_type(hole, board, rank, kicks)
    if kind:
        suit, texture = Counter(c[1] for c in board).most_common(1)[0]
        remaining = [
            c for c in reversed(DECK) if c[1] == suit and c not in hole + board
        ]
        draw_pair = " ".join(remaining[:2])
        if kind == "middle set" and raiser != hero.name:
            return None
        if event.action == "check":
            size = bet_size(
                snap, hero, [villain], 50 if rank >= 3 and texture == 2 else 33
            )
            if texture == 2:
                target = f"Hole cards {draw_pair} give the opponent a possible flush draw to pay for a turn."
            else:
                used = Counter(RANK_VALUE[c[0]] for c in (*hole, *board))
                lower = max(
                    (
                        RANK_VALUE[c[0]]
                        for c in board
                        if RANK_VALUE[c[0]] < kicks[0] and used[RANK_VALUE[c[0]]] <= 2
                    ),
                    default=0,
                )
                target = (
                    f"{RANK_LABEL[lower]}{RANK_LABEL[lower]} makes a lower set that can call."
                    if lower
                    else "Two pair can pay a small bet."
                )
            if kind == "middle set":
                opposite = "A higher set can be ahead. Checking avoids a raise and can induce a later bet."
            else:
                opposite = "Checking can keep weak hands in and induce a later bet."
            return Critique(
                "flop-value-after-check",
                f"Checked back {kind} on the flop",
                f"Compare a value bet of {size.contestable / snap.pot:.0%} pot",
                f"{villain.position} checked and your check closed the street. With {kind}, "
                f"you can compare a bet of about {size.contestable / snap.bb:.2f} BB. {target}",
                opposite + " A check-raising range can make a bet less attractive.",
            )
        if (
            event.action == "bet"
            and kind in {"top set", "the nut straight"}
            and texture == 2
            and event.amount <= 0.30 * snap.pot
        ):
            larger = bet_size(snap, hero, [villain], 50).contestable
            if larger < 1.6 * event.amount or larger < snap.bb or event.amount <= 0:
                return None
            original_price = event.amount / (snap.pot + 2 * event.amount)
            new_price = larger / (snap.pot + 2 * larger)
            blocked = sum(c[1] == suit for c in hole)
            blocks = (
                f"You hold {blocked} cards of the draw suit, leaving fewer flush-draw "
                "combinations for this player to call with. "
                if blocked >= 2
                else ""
            )
            return Critique(
                "flop-size-wet-value",
                f"Small value bet with {kind} on a two-tone flop",
                f"Compare betting {larger / snap.pot:.0%} pot",
                f"Your {event.amount / snap.pot:.0%}-pot bet gives a caller a {original_price:.1%} "
                f"price. About {larger / snap.bb:.2f} BB raises this to {new_price:.1%} before rake and "
                f"later betting. A hand with {draw_pair} has a possible flush draw. Compare "
                "the extra value when a draw calls with the value lost if it folds.",
                blocks
                + "The smaller size can keep weaker pairs in and work better as part of a range "
                "that bets small. Calls and raises can change substantially with size.",
            )
    if event.action == "bet" and raiser == hero.name and event.amount / snap.pot >= 0.8 - 1e-9:
        ranks = Counter(RANK_VALUE[c[0]] for c in board)
        if rank == 2 and len(ranks) == 2 and len({c[1] for c in board}) == 3:
            pocket = [v for v, n in Counter(RANK_VALUE[c[0]] for c in hole).items() if n == 2]
            if pocket and ranks[max(ranks)] == 2 and min(ranks) < max(pocket) < max(ranks):
                smaller = bet_size(snap, hero, [villain], 33).contestable
                return Critique(
                    "flop-size-underpair-dry-paired",
                    "Large c-bet with a pocket pair below the paired board rank",
                    "Compare checking back and a small c-bet",
                    f"After {villain.position} checked, you bet {event.amount / snap.bb:g} BB "
                    f"into {snap.pot / snap.bb:g} BB ({event.amount / snap.pot:.0%} pot). "
                    "Your pocket pair has showdown value against unpaired overcards. "
                    f"Checking takes a free turn. About {smaller / snap.bb:.2f} BB can deny "
                    "some overcard equity while risking less against higher pairs, trips and full houses.",
                    "The larger bet can gain if better pairs fold or worse hands call often. "
                    "Compare those responses with what the smaller bet achieves.",
                )
            if any(
                v > max(ranks) and n == 2
                for v, n in Counter(RANK_VALUE[c[0]] for c in hole).items()
            ):
                smaller = bet_size(snap, hero, [villain], 33).contestable
                if event.amount < 1.8 * smaller:
                    return None
                return Critique(
                    "flop-size-large-dry-paired",
                    "Large bet with an overpair on a paired rainbow flop",
                    f"Compare betting {smaller / snap.pot:.0%} pot",
                    f"You bet {event.amount / snap.pot:.0%} pot. No player can hold a made flush "
                    f"or a made straight here. A smaller bet of about {smaller / snap.bb:.2f} BB can keep "
                    "weaker pocket pairs in while risking less against trips or a full house.",
                    "A bigger size can deny equity from hands that fold, or fit a polarized "
                    "betting range. Some opponents call a large bet with weaker pairs.",
                )
    return None


def review_flop(snap, event, *, study=True):
    if event.action not in {"check", "bet", "call", "fold"}:
        return None
    hole, board = tuple(sorted(snap.hero_cards or ())), tuple(sorted(snap.board))
    if len(hole) != 4 or len(board) != 3 or len(set(hole + board)) != 7:
        return None
    context = flop_context(snap)
    if context is None:
        return None
    spot, hero, opponent, raiser = context
    if event.action == "call" and spot in {"raised", "cbet"}:
        # The first flop action identifies who actually acted out of position,
        # including tables that started with only two seats.
        first = next(a for a in snap.history if a.street == "Flop")
        if first.name == hero.name:
            idea = _weak_oop_call(snap, hero, opponent, hole, board, spot == "raised")
            if idea:
                return idea
    if not study:
        return None
    if spot == "cbet" and event.action == "call":
        return _cbet(snap, hero, opponent, hole, board)
    if spot == "checked" and event.action in {"bet", "check"}:
        return _checked(snap, event, hero, opponent, raiser, hole, board)
    return None
