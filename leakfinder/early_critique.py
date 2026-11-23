"""Conditional reviews of the action Hero took on the flop or turn."""

from collections import Counter
from functools import lru_cache

from .critique import Critique
from .early_postflop import _boat_cards, _straight_pairs, next_draws
from .opponent_history import heads_up_price
from .postflop_value import RANK_LABEL
from .river_holdings import DECK, RANKS, nuts
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
            "Compare calling to folding",
            f"The call costs {price:.1%} of the contestable pot after calling. "
            f"{count} unseen rivers guarantee at least a tie for Hero. Even if the "
            f"opponent holds four of them, at least {count - 4} of the 40 possible rivers "
            f"earn at least half the pot. Hero can secure that {floor:.1%} share "
            "before rake by continuing only on the guaranteed rivers against a further bet.",
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
    if best_plo_hand(hole, board)[0] >= minimum:
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
        "turn-closing-call-value-bound", "Stack-off against a possible turn value range",
        "Compare folding to the call",
        f"The call closes the betting and needs {price:.1%} equity before rake. "
        f"Against an opponent already holding {segment}, at most {eligible} of the "
        f"40 possible rivers per opposing hand can even reach that category for Hero. "
        f"Counting them all as wins caps equity against that group at {ceiling:.1%}. "
        f"Even if Hero always beats the rest, at least {weaker:.1%} of the betting "
        "range must come from outside that group to cover the call.",
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


def _set_raise(snap, amount, hole, board):
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
    pair = "-".join(RANK_LABEL[r] for r in reversed(max(examples)[1]))
    boats = _boat_cards(tuple(sorted(c[0] for c in hole)), tuple(sorted(c[0] for c in board)))
    return Critique(
        "turn-set-raise-straight-possible", "Turn raise with a set on a straight board",
        "Compare calling to raising",
        f"Hero put in {amount / snap.bb:g} BB with a set. Calling cost "
        f"{snap.to_call / snap.bb:g} BB. An opponent using {pair} can already have "
        f"a straight. Of the 44 unseen river cards, {boats} make Hero a full house "
        "or quads before assigning opposing cards. Calling keeps weaker bets in "
        "and avoids putting extra chips in against that straight.",
        "Raising can collect from worse made hands or draws, or deny equity to "
        "hands that would fold. Blockers and effective "
        "stacks change how often those benefits outweigh the risk.",
    )


def _bluff_blockers(hole, board):
    suits = Counter(c[1] for c in board)
    notes = []
    for suit, n in suits.items():
        highest = next(r + suit for r in reversed(RANKS) if r + suit not in board)
        if highest not in hole:
            continue
        if n >= 3:
            notes.append(f"{highest} blocks the strongest ordinary flush.")
        if n == 1 and len(board) == 3 and sum(c[1] == suit for c in hole) >= 2:
            notes.append(f"{highest} gives Hero a backdoor draw to the highest ordinary flush in that suit.")
    ranks = Counter(RANK_VALUE[c[0]] for c in board)
    pairs = _straight_pairs(frozenset(ranks))
    if max(suits.values()) < 3 and pairs:
        top = max(high for _, high in pairs)
        used = Counter(RANK_VALUE[c[0]] for c in hole)
        before = after = 0
        for (a, b), high in pairs:
            if high == top:
                before += (4 - ranks[a]) * (4 - ranks[b])
                after += (4 - ranks[a] - used[a]) * (4 - ranks[b] - used[b])
        if after < before:
            notes.append(
                f"Hero blocks {before - after} of the {before} two-card combinations "
                "that make the highest straight available on this board."
            )
    if notes:
        return " ".join(notes) + " Folds from better hands or a profitable later barrel can justify the bet."
    return (
        "Opponents who fold too often, later barrels or equity from pairing "
        "can favor the bluff. Checking also retains a chance to improve."
    )


def _large_weak_bet(snap, amount, opponents, hole, board):
    if amount * 4 < snap.pot * 3 or any(s.all_in or s.stack < amount for s in opponents):
        return None
    if len({c[0] for c in board}) < len(board):
        return None
    rank, kicks, *_ = best_plo_hand(hole, board)
    if rank > 1 or rank == 1 and kicks[0] >= max(RANK_VALUE[c[0]] for c in board):
        return None
    draws = next_draws(hole, board).total
    if draws > 4:
        return None
    required = amount / (amount + snap.pot)
    made = "high card" if rank == 0 else "a pair below the top board card"
    players = "one opponent" if len(opponents) == 1 else f"{len(opponents)} opponents"
    return Critique(
        "large-weak-hand-bet", "Large bet with few immediate straight or flush cards",
        "Compare checking to the large bet",
        f"Hero bet {amount / snap.pot:.0%} pot into {players} with "
        f"{made}. {draws} unseen next cards make a straight or flush for Hero. "
        f"If every continuation beats Hero and no more chips go in, everyone must "
        f"fold {required:.1%} of the time to break even before rake.",
        _bluff_blockers(hole, board),
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
        return _set_raise(snap, event.amount, hole, board)
    if snap.to_call or not snap.can_raise:
        return None
    if event.action == "check" and len(opponents) == 1 and not opponents[0].all_in:
        return _checked_back(snap, opponents[0], hole, board)
    if event.action == "bet":
        return _large_weak_bet(snap, event.amount, opponents, hole, board)
    return None
