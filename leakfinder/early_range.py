"""Chronological opponent ranges and showdown samples on the flop and turn."""

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from math import comb
from random import Random

from .opponent_range import (
    CLASSES,
    PRE,
    STREETS,
    BoardFeatures,
    _multiplier,
    _observations,
    _pre_band,
    _rule,
)
from .river_holdings import CARD_ID, DECK, UNIT, _kickers, _label, _score, _scores

BOARD_SIZE = {"Preflop": 0, "Flop": 3, "Turn": 4}
FLOP_RUNOUTS = 24


@dataclass(frozen=True, slots=True)
class Holding:
    cards: tuple
    pre: int
    streets: tuple
    outcomes: tuple


@dataclass(frozen=True)
class Checkpoint:
    action: str
    rule: str
    equity: float | None
    effective_n: float
    classes: tuple
    draws: float | None
    by_strength: tuple
    example: str


@dataclass(frozen=True)
class StreetRange:
    street: str
    board: tuple
    legal_total: int
    runouts_per_hand: int
    steps: tuple[Checkpoint, ...]
    uniform: Checkpoint


@lru_cache(maxsize=12)
def _sample(hero, board, count):
    remaining = tuple(c for c in range(52) if c not in hero and c not in board)
    rng = Random("early-range-v1/" + "".join(DECK[c] for c in hero + board))
    features = [BoardFeatures(board[:n]) for n in range(3, len(board) + 1)]
    own_pairs = tuple(combinations(hero, 2))
    scored_boards = {}
    seen = set()
    holdings = []
    while len(holdings) < count:
        cards = tuple(sorted(rng.sample(remaining, 4)))
        if cards in seen:
            continue
        seen.add(cards)
        pairs = tuple(combinations(cards, 2))
        made = tuple(feature.evaluate(cards, pairs) for feature in features)
        outcomes = [0, 0, 0]
        if board:
            deck = tuple(c for c in remaining if c not in cards)
            if len(board) == 3:
                # Independent indexed samples per holding keep input ordering
                # fixed when action weights change.
                draws = (rng.sample(deck, 2) for _ in range(FLOP_RUNOUTS))
            else:
                draws = ((river,) for river in deck)
            for add in draws:
                tail = tuple(sorted(add))
                if tail not in scored_boards:
                    base, suits = _scores(board + tail)
                    own = max(_score(a, b, base, suits) for a, b in own_pairs)
                    scored_boards[tail] = base, suits, own
                base, suits, own = scored_boards[tail]
                rival = max(_score(a, b, base, suits) for a, b in pairs)
                outcomes[1 + (rival > own) - (rival < own)] += 1
        holdings.append(Holding(cards, _pre_band(cards), made, tuple(outcomes)))
    return tuple(holdings)


def _measure(holdings, weights, street, action="", rule=None):
    total = sum(weights)
    concentration = total**2 / sum(w * w for w in weights)
    classes = defaultdict(float)
    by_strength = [[0.0, 0.0, 0.0] for _ in range(5)]
    draws = 0.0
    for hand, weight in zip(holdings, weights, strict=True):
        mass = weight / total
        if street == 0:
            classes[PRE[hand.pre]] += mass
            continue
        made = hand.streets[-1]
        classes[CLASSES[made.score // UNIT]] += mass
        draws += mass * made.draw
        band = max(2, made.band) if made.draw else made.band
        n = sum(hand.outcomes)
        for i, outcome in enumerate(hand.outcomes):
            by_strength[band][i] += mass * outcome / n
    example = ""
    if street:
        hand = holdings[max(range(len(weights)), key=weights.__getitem__)]
        score = hand.streets[-1].score
        example = f"{' '.join(DECK[c] for c in hand.cards)} ({_label(score // UNIT, _kickers(score))})"
    return Checkpoint(
        action or "Before the opponent's first action on this street",
        rule or "",
        sum(w + ties / 2 for w, ties, _ in by_strength) if street else None,
        concentration,
        tuple(sorted(classes.items(), key=lambda item: (-item[1], item[0]))),
        draws if street else None,
        tuple(tuple(band) for band in by_strength),
        example,
    )


def early_ranges(snap, opponent, count=768, intensity=1, *, current_only=False):
    """Build each checkpoint from its own board and prefix.

    Previous streets are reapplied using the smaller board for that street.
    New board cards start a new legal sample, while all actions on the same
    street share their holdings and simulated runouts.
    """
    if (
        snap.street not in {"Flop", "Turn"}
        or len(snap.hero_cards or ()) != 4
        or len(snap.board) != BOARD_SIZE[snap.street]
    ):
        return ()
    if not 32 <= count <= 4096 or intensity not in (0.75, 1, 1.25):
        raise ValueError("Unsupported early-street sample or compatibility weight")
    try:
        hero = tuple(sorted(CARD_ID[c] for c in snap.hero_cards))
        board = tuple(CARD_ID[c] for c in snap.board)
    except (KeyError, TypeError):
        return ()
    if len(set(hero + board)) != len(hero + board):
        return ()
    board = tuple(sorted(board[:3])) + board[3:]
    index = STREETS.index(snap.street)
    actions = _observations(snap, opponent.name)
    result = []
    for i in range(index if current_only else 0, index + 1):
        street = STREETS[i]
        visible = board[: BOARD_SIZE[street]]
        holdings = _sample(hero, visible, count)
        weights = [1.0] * count
        street_actions = []
        for a in actions:
            k = STREETS.index(a.street)
            if k > i:
                break
            rule = _rule(a, opponent.position)
            if k == i:
                street_actions.append((a, rule))
                continue
            for j, hand in enumerate(holdings):
                weights[j] *= _multiplier(rule, hand, k) ** intensity
        steps = [_measure(holdings, weights, i)]
        for a, rule in street_actions:
            for j, hand in enumerate(holdings):
                weights[j] *= _multiplier(rule, hand, i) ** intensity
            steps.append(_measure(holdings, weights, i, a.text, rule.explain(i == 0)))
        runs = (0, FLOP_RUNOUTS, 40)[i]
        uniform = (
            _measure(holdings, [1.0] * count, i) if street_actions or i else steps[0]
        )
        result.append(
            StreetRange(
                street,
                snap.board[: BOARD_SIZE[street]],
                comb(48 - len(visible), 4),
                runs,
                tuple(steps),
                uniform,
            )
        )
    return tuple(result)
