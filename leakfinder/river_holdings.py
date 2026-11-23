"""Exact two-card examples for reviewing a PLO4 river.

Each example is one legal pair available to an opponent after removing Hero
and the board. Its rank uses exactly this pair and the best three board cards.
An opponent's two additional cards may give the complete hand a higher rank.
No opponent range, action frequency, equity, or full-hand count is inferred.
"""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, combinations_with_replacement
from math import prod

from .vendor.primitives import eval_5

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(rank + suit for rank in RANKS for suit in SUITS)
CARD_ID = {card: i for i, card in enumerate(DECK)}
PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)
RANK_PAIRS = tuple(combinations_with_replacement(range(13), 2))
PAIR_ID = {pair: i for i, pair in enumerate(RANK_PAIRS)}
PAIR_PRODUCT = tuple(PRIMES[a] * PRIMES[b] for a, b in RANK_PAIRS)
PAIR_INDEX = tuple(
    tuple(PAIR_ID[tuple(sorted((a // 4, b // 4)))] for b in range(52))
    for a in range(52)
)
# Each window contains the ranks of one straight, with its high rank first.
WINDOWS = tuple(
    (high, frozenset(range(high - 4, high + 1))) for high in range(12, 3, -1)
) + ((3, frozenset((12, 0, 1, 2, 3))),)
KICKERS = (5, 4, 3, 3, 1, 5, 2, 2, 1)
UNIT = 16**5
HIGH_NAMES = {"A": "ace", "K": "king", "Q": "queen", "J": "jack"}


@lru_cache(maxsize=1)
def _five_ranks():
    """Reuse the validated five-card evaluator once per rank multiset."""
    result = {}
    for ranks in combinations_with_replacement(range(13), 5):
        key = prod(PRIMES[r] for r in ranks)
        if ranks[0] == ranks[-1]:
            result[key] = 0  # Impossible with real cards.
            continue
        # Distinct suits avoid flushes. The reference evaluates ranks directly.
        category, kickers, _ = eval_5(
            [RANKS[r] + "cdhsc"[i] for i, r in enumerate(ranks)]
        )
        result[key] = category * UNIT + sum(
            k << (16 - 4 * i) for i, k in enumerate(kickers)
        )
    return result


def _kickers(score):
    return tuple((score >> (16 - 4 * i)) & 15 for i in range(KICKERS[score // UNIT]))


def _label(category, kickers):
    r = [RANKS[value - 2] for value in kickers]
    if category in (4, 5, 8):
        name = {4: "straight", 5: "flush", 8: "straight flush"}[category]
        return f"{HIGH_NAMES.get(r[0], r[0]).capitalize()}-high {name}"
    if category == 7:
        return f"quads {r[0]} with {r[1]}"
    if category == 6:
        return f"full house, {r[0]}{r[0]}{r[0]} and {r[1]}{r[1]}"
    if category == 3:
        return f"three of a kind {r[0]}, kicker {r[1]}"
    if category == 2:
        return f"two pair, {r[0]} and {r[1]}"
    if category == 1:
        return f"pair {r[0]}, kicker {r[1]}"
    return f"high card {r[0]}"


@dataclass(frozen=True)
class PairExample:
    cards: tuple[str, str]
    category: int
    kickers: tuple[int, ...]
    turn_category: int
    turn_kickers: tuple[int, ...]
    turn_outs: tuple[str, ...] = ()

    @property
    def label(self):
        return _label(self.category, self.kickers)

    @property
    def text(self):
        return f"{' '.join(self.cards)} ({self.label})"


@dataclass(frozen=True)
class RiverHoldings:
    hero_category: int
    hero_kickers: tuple[int, ...]
    hero_turn_category: int
    river_made: int
    river_kickers: tuple[int, ...]
    stronger: tuple[PairExample, ...]
    ties: tuple[PairExample, ...]
    weaker: tuple[PairExample, ...]
    missed_flush: tuple[PairExample, ...]
    missed_straight: tuple[PairExample, ...]
    flush_blocker_bluffs: tuple[PairExample, ...]
    blockers: tuple[str, ...]


def _scores(board):
    """Nonflush base and per-suit overrides for each distinct rank pair."""
    five = _five_ranks()
    triples = tuple(combinations(board, 3))

    def signature(cards):
        return prod(PRIMES[c // 4] for c in cards)

    keys = {signature(cards) for cards in triples}
    base = [max(five[p * k] for k in keys) for p in PAIR_PRODUCT]
    by_suit = [base] * 4
    for suit, n in Counter(c % 4 for c in board).items():
        if n < 3:
            continue
        keys = {
            signature(cards) for cards in triples if all(c % 4 == suit for c in cards)
        }
        best = base.copy()
        for i, p in enumerate(PAIR_PRODUCT):
            for k in keys:
                rank = five[p * k]
                # All five suited cards in a legal selection have distinct ranks.
                # Upgrading a rank-only straight gives a straight flush.
                if rank // UNIT in (0, 4):
                    best[i] = max(
                        best[i], rank + (4 if rank // UNIT == 4 else 5) * UNIT
                    )
        by_suit[suit] = best
    return base, by_suit


def _score(a, b, by_rank, by_suit):
    return (by_suit[a % 4] if a % 4 == b % 4 else by_rank)[PAIR_INDEX[a][b]]


def _straight_out_masks(turn, available):
    """Live completion ranks, each requiring two hole + two turn + a river."""
    turn_ranks = frozenset(c // 4 for c in turn)
    live_ranks = frozenset(c // 4 for c in available)
    out_masks = []
    for a, b in RANK_PAIRS:
        candidates = set()
        if a != b:
            for _, window in WINDOWS:
                if a not in window or b not in window:
                    continue
                missing = window.difference((a, b), turn_ranks)
                if len(missing) == 1:
                    candidates.update(missing & live_ranks)
        out_masks.append(sum(1 << r for r in candidates))
    return out_masks


def _blockers(hero, board):
    messages = []
    ranks = Counter(c // 4 for c in board)
    known = set(hero + board)
    for suit, n in Counter(c % 4 for c in board).items():
        held = sorted((c for c in hero if c % 4 == suit), reverse=True)
        if n >= 3 and held:
            off_board = max(c for c in range(suit, 52, 4) if c not in board)
            detail = (
                " This includes the highest available card of that suit off the board."
                if off_board in held
                else ""
            )
            messages.append(
                f"You hold {', '.join(DECK[c] for c in held)} in a suit that can make a flush on this board.{detail}"
            )
    for r in sorted(ranks, reverse=True):
        held = [DECK[c] for c in hero if c // 4 == r]
        if ranks[r] >= 2 and held:
            remain = sum(c not in known for c in range(4 * r, 4 * r + 4))
            messages.append(
                f"You hold {' and '.join(held)} with the paired rank {RANKS[r]} on the board. Cards of that rank still available: {remain}."
            )
    for high, window in WINDOWS:
        for a, b in combinations(sorted(window, reverse=True), 2):
            if (
                window.difference((a, b)).issubset(ranks)
                and ranks[a] < 4
                and ranks[b] < 4
            ):
                held = [DECK[c] for c in hero if c // 4 in (a, b)]
                if held:
                    messages.append(
                        f"You hold {' and '.join(held)} from the ranks {RANKS[a]}–{RANKS[b]} , which make a straight topped by a {HIGH_NAMES.get(RANKS[high], RANKS[high])}."
                    )
                    return tuple(messages)
    return tuple(messages)


def _example(data, outs=()):
    river, turn, a, b = data
    return PairExample(
        (DECK[b], DECK[a]),
        river // UNIT,
        _kickers(river),
        turn // UNIT,
        _kickers(turn),
        tuple(DECK[c] for c in outs),
    )


@lru_cache(maxsize=256)
def _analyze(hero, board):
    known = set(board + hero)
    available = tuple(c for c in range(52) if c not in known)
    river_base, river_suits = _scores(board)
    turn_base, turn_suits = _scores(board[:4])
    hero_river = max(
        _score(a, b, river_base, river_suits) for a, b in combinations(hero, 2)
    )
    hero_turn = max(
        _score(a, b, turn_base, turn_suits) for a, b in combinations(hero, 2)
    )

    draw_masks = _straight_out_masks(board[:4], available)
    turn_suit_counts = Counter(c % 4 for c in board[:4])
    river_suit = board[-1] % 4
    completed_ace = 48 + river_suit if turn_suit_counts[river_suit] == 2 else -1
    by_relation = ({}, {}, {})  # weaker, tied, stronger; one example per category.
    missed_flush = {}
    missed_straight = {}
    blocker_bluffs = {}
    best = 0
    for a, b in combinations(available, 2):
        idx = PAIR_INDEX[a][b]
        suit = a % 4 if a % 4 == b % 4 else -1
        if suit < 0:
            river, turn = river_base[idx], turn_base[idx]
        else:
            river, turn = river_suits[suit][idx], turn_suits[suit][idx]
        category = river // UNIT
        best = max(best, river)
        group = by_relation[(river >= hero_river) + (river > hero_river)]
        if category not in group or group[category][0] < river:
            group[category] = (river, turn, a, b)
        if river >= hero_river or category >= 4:
            continue
        if (
            completed_ace in (a, b)
            and suit < 0
            and category <= 1
            and (category not in blocker_bluffs or blocker_bluffs[category][0] > river)
        ):
            # A lone suited ace in the shown pair. Either unseen side card might
            # still make a real flush, so the complete holding is conditional.
            blocker_bluffs[category] = (river, turn, a, b)
        if suit >= 0 and suit != river_suit and turn_suit_counts[suit] == 2:
            quality = (-category, b // 4, a // 4)
            if suit not in missed_flush or quality > missed_flush[suit][0]:
                missed_flush[suit] = (quality, (river, turn, a, b))
        mask = draw_masks[idx]
        if mask:
            quality = (-category, mask.bit_count(), b // 4, a // 4)
            if idx not in missed_straight or quality > missed_straight[idx][0]:
                missed_straight[idx] = (quality, (river, turn, a, b))

    def representatives(group):
        return tuple(_example(group[cat]) for cat in sorted(group, reverse=True))

    flush_examples = []
    for suit, (_, data) in sorted(missed_flush.items()):
        outs = [c for c in available if c % 4 == suit and c not in data[2:]]
        if outs:
            flush_examples.append(_example(data, outs))
    straight_examples = []
    for idx, (_, data) in sorted(
        missed_straight.items(), key=lambda item: item[1][0], reverse=True
    )[:3]:
        outs = [c for c in available if draw_masks[idx] & (1 << (c // 4))]
        straight_examples.append(_example(data, outs))
    return RiverHoldings(
        hero_river // UNIT,
        _kickers(hero_river),
        hero_turn // UNIT,
        best // UNIT,
        _kickers(best),
        representatives(by_relation[2]),
        representatives(by_relation[1]),
        representatives(by_relation[0]),
        tuple(flush_examples),
        tuple(straight_examples),
        tuple(_example(blocker_bluffs[cat]) for cat in sorted(blocker_bluffs)),
        _blockers(hero, board),
    )


def river_holdings(hero_cards, board):
    """Review the five-card board in deal order, given Hero's four PLO cards.

    Input uses standard two-character card names. Returns a strongest example
    for each available river category per comparison. Missed draws are limited
    to weaker pairs whose best river hand ranks below a straight. Each listed
    turn out would have completed the named draw using the two shown cards.
    Example lists contain representative pairs, never opponent probabilities.
    """
    hero, board = tuple(hero_cards), tuple(board)
    if len(hero) != 4 or len(board) != 5:
        raise ValueError("Four Hero cards and five board cards are required.")
    try:
        cards = tuple(CARD_ID[c] for c in hero + board)
    except (KeyError, TypeError) as exc:
        raise ValueError("Unknown card. Use standard names such as As or Td.") from exc
    if len(set(cards)) != len(cards):
        raise ValueError("Duplicate Hero or board card.")
    # The identities of the four turn board cards matter, their ordering does not.
    return _analyze(tuple(sorted(cards[:4])), tuple(sorted(cards[4:8])) + cards[8:])
