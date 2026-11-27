"""Conditional reviews of the action Hero took on the flop or turn."""

from collections import Counter
from functools import lru_cache

from .critique import Critique
from .early_postflop import _straight_pairs, next_draws
from .opponent_history import heads_up_price
from .postflop_value import RANK_LABEL
from .river_holdings import DECK, nuts
from .vendor.common import RANK_VALUE
from .vendor.primitives import best_plo_hand


@lru_cache(maxsize=512)
def _possible_rivers(hole, board, minimum):
    """Give Hero full credit for every river reaching the target category."""
    seen = set(hole + board)
    return sum(
        best_plo_hand(hole, board + (river,))[0] >= minimum
        for river in DECK if river not in seen
    )


def _turn_response(snap, action, hero, villain, hole, board):
    closes = villain.all_in or hero.stack == snap.to_call
    # If Hero is short, a folded player's excess chips are a separate pot.
    if any(s.committed > hero.committed + snap.to_call for s in snap.seats if not s.live):
        return None
    price = heads_up_price(snap)
    if action == "fold":
        count = next_draws(hole, board).nuts
        # Each guaranteed nuts/tie card earns at least half the heads-up pot.
        # One opposing hand can block at most four such rivers.
        floor = max(0, count - 4) / 80
        if floor <= price + 0.01:
            return None
        return Critique(
            "turn-fold-guaranteed-share", "Fold at a price covered by a showdown bound",
            "Call",
            f"The call costs {price:.1%} of the contestable pot after calling. "
            f"Of {count} unseen rivers that secure a share, at least {count - 4} of 40 remain unblocked and "
            f"guarantee half the pot or more, covering the call before rake ({floor:.1%} minimum).",
            "A sufficiently large rake can change a thin price. Recheck the amount "
            "Hero can win after fees before treating the fold as a mistake.",
        )
    # For expensive calls this upper bound assumes no future implied odds.
    if not closes or price < 0.20:
        return None
    if len({c[0] for c in board}) < 4:
        minimum, segment = 6, "a full house or better"
    elif max(Counter(c[1] for c in board).values()) >= 3:
        minimum, segment = 5, "a flush or better"
    else:
        minimum, segment = 3, "a set or better"
    made = best_plo_hand(hole, board)[0]
    # A paired-board value bound against boats alone does not justify
    # questioning a stack-off with trips. The opponent may shove worse trips or draws.
    if made >= minimum or (minimum == 6 and made >= 3):
        return None
    # Sensitivity to a possible value range, not a claim that it was actually held.
    # A fixed villain leaves 40 possible rivers. Blockers can REMOVE eligible
    # cards but can never add one outside this 44-card pre-decision pool.
    eligible = _possible_rivers(hole, board, minimum)
    ceiling = min(1, eligible / 40)
    if ceiling + 0.05 > price:
        return None
    weaker = (price - ceiling) / (1 - ceiling)
    return Critique(
        "turn-closing-call-value-bound", "Closing turn call against different betting ranges",
        "Compare call and fold by range",
        f"The call closes the betting and needs {price:.1%} equity before rake. "
        f"Against an opponent already holding {segment}, at most {eligible} of the "
        f"40 possible rivers per opposing hand can even reach that category for Hero. "
        f"Counting them all as wins caps equity against that group at {ceiling:.1%}. "
        f"Even if Hero always beats the rest, at least {weaker:.1%} of the betting "
        f"range must come from outside that group to cover the call.",
        "Draws, weaker value or bluffs can justify calling if numerous enough. "
        "The hand history gives the price but cannot establish that range.",
    )


def _checked_back(snap, villain, hole, board):
    actions = [a for a in snap.history if a.street == snap.street]
    if not actions or actions[-1].name != villain.name or actions[-1].action != "check":
        return None
    if any(a.action in {"bet", "raise"} for a in actions):
        return None
    if len({c[0] for c in board}) != len(board):
        return None
    hole_suits = Counter(c[1] for c in hole)
    exposed = {s for s, n in Counter(c[1] for c in board).items()
               if n == 2 and hole_suits[s] < 2}
    if not exposed:
        return None
    rank, kickers, *_ = best_plo_hand(hole, board)
    if rank not in (3, 4) or not nuts(hole, board, rank, tuple(kickers)):
        return None
    name = "top set" if rank == 3 else "the nut straight"
    board_ranks = {b[0] for b in board}
    exposed_cards = sum(c[1] in exposed and c not in hole and c[0] not in board_ranks for c in DECK)
    return Critique(
        "checked-back-vulnerable-nuts", "Free card with a vulnerable nut hand",
        "Compare a value bet to checking back",
        f"The opponent checked to Hero with {name}. Of the {52-len(hole)-len(board)} "
        f"unseen cards, {exposed_cards} keep the board unpaired and complete a possible "
        "opposing flush in a suit Hero cannot make. A bet can charge a flush draw "
        "or collect from a weaker made hand before one of those cards arrives.",
        "Checking back can induce a later bluff or keep weaker hands in. The exposed "
        "cards only enable a flush when the opponent holds two of the suit.",
    )


def _set_raise(snap, amount, opponent, hole, board):
    if amount <= snap.to_call or amount * 2 < snap.pot:
        return None
    board_ranks = {RANK_VALUE[c[0]] for c in board}
    if len(board_ranks) != 4 or max(Counter(c[1] for c in board).values()) >= 3:
        return None
    if best_plo_hand(hole, board)[0] != 3:
        return None
    known = Counter(RANK_VALUE[c[0]] for c in hole + board)
    examples = [
        (high, pair) for pair, high in _straight_pairs(frozenset(board_ranks))
        if all(known[r] < 4 for r in pair)
    ]
    if not examples:
        return None
    high_pair = tuple(reversed(max(examples)[1]))
    pair = "-".join(RANK_LABEL[r] for r in high_pair)
    held = [c for c in hole if RANK_VALUE[c[0]] in high_pair]
    blocker = (f" Holding {' and '.join(held)} reduces the available {pair} combinations."
               if held else "")
    matched = min(amount, snap.to_call + opponent.stack)
    risk = (f" Only {matched / snap.bb:g} BB of that can be matched, including "
            f"{(matched - snap.to_call) / snap.bb:g} BB beyond calling."
            if matched < amount else "")
    boats = extra = unbeaten = 0
    for river in DECK:
        if river in hole or river in board:
            continue
        final = board + (river,)
        rank, kicks, *_ = best_plo_hand(hole, final)
        if rank in (6, 7):
            boats += 1
        elif rank in (4, 5, 8):
            extra += 1
            unbeaten += nuts(hole, final, rank, tuple(kicks))
    redraws = (
        f" Another {extra} make a straight, flush or straight flush as Hero's best hand. "
        f"Of those, {unbeaten} guarantee at least a tie against every legal holding."
        if extra else ""
    )
    return Critique(
        "turn-set-raise-straight-possible", "Turn raise with a set on a straight board",
        "Compare calling to raising",
        f"Hero put in {amount / snap.bb:g} BB with a set. Calling cost "
        f"{snap.to_call / snap.bb:g} BB.{risk} An opponent using {pair} can already have "
        f"a straight.{blocker} Of the 44 unseen river cards, {boats} make Hero a full house "
        f"or quads before assigning opposing cards.{redraws} Calling keeps weaker bets in "
        "and avoids putting extra chips in against that straight.",
        "Raising can collect from worse made hands or draws, or deny equity to "
        "hands that would fold. Blockers and effective "
        "stacks change how often those benefits outweigh the risk.",
    )


def review_early(snap, event, advice):
    """The recorded action selects the comparison. All evidence stops before it."""
    length = {"Flop": 3, "Turn": 4}.get(snap.street)
    if (length is None or len(snap.board) != length or len(snap.hero_cards or ()) != 4
            or snap.bb <= 0 or snap.pot <= 0 or event.action not in {"check", "bet", "call", "fold", "raise"}):
        return None
    hole, board = tuple(sorted(snap.hero_cards)), tuple(sorted(snap.board))
    if len(set(hole + board)) != 4 + length:
        return None
    hero = next((s for s in snap.seats if s.is_hero and s.live), None)
    opponents = [s for s in snap.seats if s.live and not s.is_hero]
    if not hero or hero.all_in or not opponents:
        return None
    if event.action in {"call", "fold"} and snap.to_call and snap.street == "Turn" and len(opponents) == 1:
        return _turn_response(snap, event.action, hero, opponents[0], hole, board)
    if (event.action == "raise" and snap.to_call and snap.can_raise and
            snap.street == "Turn" and len(opponents) == 1 and not opponents[0].all_in):
        return _set_raise(snap, event.amount, opponents[0], hole, board)
    if snap.to_call or not snap.can_raise:
        return None
    if event.action == "check" and len(opponents) == 1 and not opponents[0].all_in:
        return _checked_back(snap, opponents[0], hole, board)
    return None
