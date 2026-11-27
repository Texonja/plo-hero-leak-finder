"""Exact single-board draw opportunities. Only the deal and public board enter here."""

from collections import Counter
from functools import lru_cache
from itertools import combinations, combinations_with_replacement, permutations, product
from math import comb

from .river_holdings import PRIMES, UNIT, _five_ranks

DECK = tuple(range(52))
FULL = (1 << 52) - 1
SUITS = tuple(sum(1 << c for c in range(s, 52, 4)) for s in range(4))
RANK_BITS = tuple(15 << (4 * r) for r in range(13))
TRIPLES = tuple(combinations_with_replacement(range(13), 3))
FLOPS = comb(48, 3)
DEALS = comb(52, 4)
NAMES = (
    "Deal · At least one pair",
    "Deal · At least two aces",
    "Deal · Double suited",
    "Deal · Four-card rundown",
    "Deal · Four of one suit",
    "Flop · Hit the pocket pair",
    "Flop · Hit either pocket pair",
    "Flop · Flush draw or better",
    "Flop · Straight or better",
    "Flop · Two pair or better (unpaired pocket)",
    "Turn · Flush draw → flush+",
    "Turn · Straight draw → straight+",
    "Turn · Set / two pair → full house+",
    "River · Flush draw → flush+",
    "River · Straight draw → straight+",
    "River · Set / two pair → full house+",
    "Combined · Flush draw → flush+",
    "Combined · Straight draw → straight+",
    "Combined · Set / two pair → full house+",
)
DENOM = (DEALS,) * 5 + (FLOPS,) * 5 + (45,) * 3 + (44,) * 3
RUNDOWNS = {tuple(range(i, i + 4)) for i in range(10)} | {(0, 1, 2, 12)}
STRAIGHTS = tuple(frozenset(range(i, i + 5)) for i in range(9)) + (
    frozenset((12, 0, 1, 2, 3)),
)
PAIR_HITS = tuple(FLOPS - comb(48 - n, 3) for n in (2, 4))


def deal_flags(pocket):
    ranks = tuple(sorted(c // 4 for c in pocket))
    suits = len({c % 4 for c in pocket})
    double = suits == 2 and sum(c % 4 == pocket[0] % 4 for c in pocket) == 2
    return (len(set(ranks)) < 4, ranks[2] == 12, double, ranks in RUNDOWNS, suits == 1)


@lru_cache(maxsize=1)
def deal_counts():
    counts = [0] * 5
    for hand in combinations(DECK, 4):
        for i, hit in enumerate(deal_flags(hand)):
            counts[i] += hit
    return tuple(counts)


def _index(a, b, c):
    return 169 * a + 13 * b + c


@lru_cache(maxsize=1820)
def _rank_table(ranks):
    # Best non-flush category using exactly two hole and all three board cards.
    # The small five-rank table is built by the existing validated evaluator.
    five = _five_ranks()
    pairs = {PRIMES[a] * PRIMES[b] for a, b in combinations(ranks, 2)}
    table = bytearray(13**3)
    for a, b, c in TRIPLES:
        k = PRIMES[a] * PRIMES[b] * PRIMES[c]
        cat = max(five[p * k] for p in pairs) // UNIT
        for x, y, z in set(permutations((a, b, c))):
            table[_index(x, y, z)] = cat
    return bytes(table)


@lru_cache(maxsize=1820)
def _flop_base(ranks):
    table = _rank_table(ranks)
    left = [4 - ranks.count(i) for i in range(13)]
    two = straight = 0
    boats = []
    for triple in TRIPLES:
        a, b, c = triple
        ways = left[a] * (left[b] - (a == b)) * (left[c] - (a == c) - (b == c))
        ways //= 6 if a == c else 2 if a == b or b == c else 1
        cat = table[_index(a, b, c)]
        two += ways if cat >= 2 else 0
        straight += ways if cat >= 4 else 0
        if ways and cat >= 6:
            boats.append(triple)
    return two, straight, boats


@lru_cache(maxsize=32768)
def _flop_counts(ranks, suit_holes):
    # Partition all C(48,3) physical flops by rank multiset. On monotone
    # flops upgrade the non-flush category if a matching hole pair exists.
    two, straight, boats = _flop_base(ranks)
    table = _rank_table(ranks)
    draw = 0
    for blocked in suit_holes:
        left = [r for r in range(13) if r not in blocked]
        draw += comb(len(left), 2) * (48 - len(left)) + comb(len(left), 3)
        for a, b, c in combinations(left, 3):
            cat = table[_index(a, b, c)]
            two += cat < 2
            straight += cat < 4
    # A full house or quads already beats a flush. Add only boards not
    # already counted as a front-door flush draw or made flush.
    if suit_holes and boats:
        all_holes = Counter(ranks)
        suited_holes = Counter(r for group in suit_holes for r in group)
        for a, b, c in boats:
            choices = []
            for r, n in Counter((a, b, c)).items():
                active = [i for i, group in enumerate(suit_holes) if r not in group]
                others = 4 - len(suit_holes) - all_holes[r] + suited_holes[r]
                choices.append(
                    tuple(combinations(active + list(range(2, 2 + others)), n))
                )
            for groups in product(*choices):
                picked = sum(groups, ())
                if all(picked.count(s) < 2 for s in range(len(suit_holes))):
                    draw += 1
    return draw, straight, two


def _suit_groups(hole):
    return tuple(
        (s, tuple(sorted(c // 4 for c in hole if c % 4 == s)))
        for s in range(4)
        if sum(c % 4 == s for c in hole) >= 2
    )


@lru_cache(maxsize=180000)
def _next_ranks(ranks, a, b):
    table = _rank_table(ranks)
    start = _index(a, b, 0)
    straight = boat = 0
    for r, cat in enumerate(table[start : start + 13]):
        if cat == 4:
            straight |= RANK_BITS[r]
        elif cat >= 6:
            boat |= RANK_BITS[r]
    return straight, boat


def _mask(cards):
    return sum(1 << c for c in cards)


def _draws(ranks, suits, pocket_pairs, hole_mask, board):
    br = tuple(c // 4 for c in board)
    table = _rank_table(ranks)
    cat = max(table[_index(*triple)] for triple in combinations(br, 3))
    board_mask = _mask(board)
    flush = 0
    for suit, _ in suits:
        n = (board_mask & SUITS[suit]).bit_count()
        if n >= 3:
            cat = max(cat, 5)
        elif n == 2:
            flush |= SUITS[suit]
    remaining = FULL ^ (board_mask | hole_mask)
    straight = boat = 0
    for a, b in combinations(br, 2):
        s, f = _next_ranks(ranks, min(a, b), max(a, b))
        straight |= s
        boat |= f
    flush &= remaining
    straight &= remaining
    boat &= remaining
    boat_target = cat == 2 or (cat == 3 and bool(pocket_pairs.intersection(br)))
    boat_or_better = boat
    if boat_target and flush and straight:
        for suit, suited_hole in suits:
            suited_board = [c // 4 for c in board if c % 4 == suit]
            if len(suited_board) == 2:
                for a, b in combinations(suited_hole, 2):
                    partial = frozenset((a, b, *suited_board))
                    for window in STRAIGHTS:
                        if partial < window:
                            (r,) = window - partial
                            boat_or_better |= (1 << (4 * r + suit)) & remaining
    # Other improvements at least as strong as the named target also hit.
    # Eligibility itself requires the named draw, before seeing the next card.
    return (
        flush | boat if cat < 5 and flush else None,
        straight | flush | boat if cat < 4 and straight else None,
        boat_or_better if boat_target else None,
    )


def events(hole, board, saw_flop=True):
    """Yield metric index, favorable deals and observed 0/1. Input cards are IDs."""
    if len(hole) != 4 or len(board) not in (0, 3, 4, 5):
        raise ValueError("Incomplete pocket or board")
    if len({*hole, *board}) != 4 + len(board) or any(
        not 0 <= c < 52 for c in (*hole, *board)
    ):
        raise ValueError("Invalid or duplicate card")
    for i, (ways, hit) in enumerate(zip(deal_counts(), deal_flags(hole))):
        yield i, ways, int(hit)
    if not board:
        return
    ranks = tuple(sorted(c // 4 for c in hole))
    suits = _suit_groups(hole)
    pairs = {r for r in ranks if ranks.count(r) >= 2}
    if saw_flop:
        flop = board[:3]
        fr = [c // 4 for c in flop]
        if len(set(ranks)) == 3 or len(pairs) == 2:
            i = len(pairs) - 1
            yield 5 + i, PAIR_HITS[i], int(bool(pairs.intersection(fr)))
        fd, straight, two = _flop_counts(ranks, tuple(sorted(g for _, g in suits)))
        best = _rank_table(ranks)[_index(*fr)]
        matching = max((sum(c % 4 == s for c in flop) for s, _ in suits), default=0)
        if matching == 3:
            best = max(best, 5)
        if suits:
            yield 7, fd, int(matching >= 2 or best >= 6)
        yield 8, straight, int(best >= 4)
        if len(set(ranks)) == 4:
            yield 9, two, int(best >= 2)
    hole_mask = _mask(hole)
    for street, actual in enumerate(board[3:]):
        outs = _draws(ranks, suits, pairs, hole_mask, board[: 3 + street])
        for i, mask in enumerate(outs):
            if mask is not None:
                yield (
                    10 + street * 3 + i,
                    mask.bit_count(),
                    int(bool(mask & (1 << actual))),
                )
