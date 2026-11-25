"""One explicit counterexample to a value-only flop or turn betting range."""

from functools import lru_cache
from itertools import combinations

from .river_holdings import CARD_ID, DECK, UNIT, _score, _scores


def _example_below(available, scores, minimum):
    # Start with a high double-suited pocket pair and distinct side ranks.
    # Select on the current board, before examining any later board or equity.
    for rank in reversed(range(13)):
        pocket = [c for c in available if c // 4 == rank]
        for a, b in combinations(pocket, 2):
            sides_a = [c for c in available if c % 4 == a % 4 and c // 4 != rank]
            sides_b = [c for c in available if c % 4 == b % 4 and c // 4 != rank]
            for c in sides_a:
                for d in sides_b:
                    if c // 4 == d // 4:
                        continue
                    hand = (a, b, c, d)
                    category = max(_score(x, y, *scores) for x, y in combinations(hand, 2)) // UNIT
                    if category < minimum:
                        return hand, category
    # On a trips board every pocket pair makes at least a full house.
    # Include an unpaired four-card holding for the weaker scenario.
    for hand in combinations(available, 4):
        if len({c // 4 for c in hand}) != 4:
            continue
        category = max(_score(x, y, *scores) for x, y in combinations(hand, 2)) // UNIT
        if category < minimum:
            return hand, category
    return None


def _share(hero, visible, available, rival):
    hero_pairs = tuple(combinations(hero, 2))
    rival_pairs = tuple(combinations(rival, 2))
    wins = ties = total = 0
    for runout in combinations((c for c in available if c not in rival), 5 - len(visible)):
        scores = _scores(visible + runout)
        ours = max(_score(a, b, *scores) for a, b in hero_pairs)
        theirs = max(_score(a, b, *scores) for a, b in rival_pairs)
        wins += ours > theirs
        ties += ours == theirs
        total += 1
    return (wins + ties / 2) / total, total


@lru_cache(maxsize=256)
def _scenarios(hole, board, minimum):
    hero = tuple(CARD_ID[c] for c in hole)
    visible = tuple(CARD_ID[c] for c in board)
    available = tuple(c for c in reversed(range(52)) if c not in hero + visible)
    scores = _scores(visible)
    chosen = _example_below(available, scores, minimum)
    if not chosen:
        return None
    rival, category = chosen
    share, total = _share(hero, visible, available, rival)
    # Choose a value witness on this street before examining future cards.
    for pair in combinations(available, 2):
        if _score(*pair, *scores) // UNIT >= minimum:
            value = pair + tuple(c for c in available if c not in pair)[:2]
            value_share, _ = _share(hero, visible, available, value)
            return rival, category, share, total, value, value_share
    return rival, category, share, total, (), None


def showdown_example(hole, board, minimum, price=None):
    result = _scenarios(hole, board, minimum)
    if not result:
        return ""
    rival, category, share, total, value, value_share = result
    name = ("high card", "one pair", "two pair", "three of a kind", "a straight", "a flush")[category]
    note = (
        f"Against a possible {' '.join(DECK[c] for c in rival)} ({name} now), "
        f"Hero has {share:.1%} showdown equity across all {total} runouts, counting ties as half. "
        "The frequency of that hand in the betting range is unknown."
    )
    if price is None:
        return note
    note += " The minimum outside the value group assumes every other hand loses to Hero."
    if not value or value_share >= price:
        return note
    note += f" Against {' '.join(DECK[c] for c in value)} from the value group, Hero has {value_share:.1%}."
    if share < price:
        return note + " Even the weaker example falls short of the call price. No mix of these two hands covers the call."
    needed = (price - value_share) / (share - value_share)
    return note + f" If these were the only two hands bet, the weaker hand would need to account for {needed:.1%} of bets to break even."
