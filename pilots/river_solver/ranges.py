"""Full-deck river arrival models and deterministic, weighted representative hands.

The preference coefficients are pilot assumptions. They are not measured player frequencies.
Features on a street use only the current board and the four hole cards.
"""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

import numpy as np

from leakfinder.vendor.primitives import eval_5

from .model import GROUPS

PROFILES = ("tight", "middle", "wide")
RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(r + s for r in RANKS for s in SUITS)
WINDOWS = ((12, 0, 1, 2, 3),) + tuple(tuple(range(i, i + 5)) for i in range(9))
PAIR_SLOTS = tuple(combinations(range(4), 2))
# Exact input units for preflop structure (not a percentile or estimated equity).
STRUCTURE = {
    "intercept": -0.80,
    "broadway": 0.20,
    "suited_pair": 0.30,
    "suited_ace": 0.35,
    "straight_window_rank": 0.24,
    "top_pair_rank": 0.32,
    "aces": 1.15,
    "second_pair": 0.18,
    "three_of_rank": -0.90,
    "monotone_penalty": -0.28,
}

PARAMETERS = {
    "preflop_structure": STRUCTURE,
    "preflop": {
        "decision_slope": 1.65,
        "enter_intercept": -1.45,
        "raise_intercept": -2.1,
        "prior_raise_enter_penalty": 0.64,
        "prior_raise_aggression_penalty": 0.44,
        "raise_structure_slope": 2.05,
    },
    "postflop": {
        "made_score": [0, 0.35, 1.0, 1.5, 2.12, 2.45, 2.83, 3.3, 3.55],
        "within_category_rank_weight": 0.38,
        "fd_weight": 0.62,
        "nut_fd_bonus": 0.22,
        "straight_out_weight": 0.068,
        "straight_out_cap": 17,
        "blocker_weight": 0.26,
        "bet_intercept": -1.85,
        "bet_strength_slope": 1.48,
        "bet_draw_slope": 0.96,
        "raise_intercept": -3.15,
        "raise_strength_slope": 1.55,
        "raise_draw_slope": 0.90,
        "continue_intercept": -1.85,
        "continue_strength_slope": 2.0,
        "continue_draw_slope": 1.55,
        "facing_raise_penalty": 0.55,
        "turn_draw_discount": 0.71,
        "slowplay_probability_floor": 0.035,
    },
    "profiles": {
        "tight": {
            "entry_shift": -0.55,
            "continuation_shift": -0.47,
            "thin_aggression_shift": -0.36,
            "trash_bluff_shift": -0.55,
            "tail_mass": 0.045,
        },
        "middle": {
            "entry_shift": 0.0,
            "continuation_shift": 0.0,
            "thin_aggression_shift": 0.0,
            "trash_bluff_shift": 0.0,
            "tail_mass": 0.018,
        },
        "wide": {
            "entry_shift": 0.52,
            "continuation_shift": 0.44,
            "thin_aggression_shift": 0.30,
            "trash_bluff_shift": 0.52,
            "tail_mass": 0.004,
        },
    },
    "pruning": "Drop complete lowest probability tiers while their combined mass is at most tail_mass; keep ties together.",
    "grouping": "Preserve all river categories; allocate slots by largest remainder of mass. Within each category split a Morton-ordered list into weighted quantile bins, using history, kicker percentile and blockers. Choose weighted median member. Full group mass transfers to its representative. Split off exact Hero singleton.",
    "assumptions": "Likelihood times likelihood from an initially uniform deal. Preflop conditional on number of previous raises, postflop conditional on bet/check or fold/call/raise. Bet sizes and opponent identity have no fitted coefficients. Unblocked straight outs can be dirty. A wrap has >8 raw next-card straight outs. No showdown outcomes enter the prior.",
}


@lru_cache(maxsize=1)
def templates():
    return np.array(list(combinations(range(47), 4)), dtype=np.uint8)


def packed_rank(cards):
    category, kickers, _ = eval_5(cards)
    return category * 16**5 + sum(val * 16 ** (4 - i) for i, val in enumerate(kickers))


def pair_scores(board_ids, available):
    """On a fixed two-card kernel every legal board triple uses the project's evaluator."""
    pairs = list(combinations(available, 2))
    table = np.zeros((52, 52), dtype=np.int32)
    street = []
    for extent in (3, 4, 5):
        triples = [
            t for t in combinations(board_ids[:extent], 3) if board_ids[extent - 1] in t
        ]
        for a, b in pairs:
            previous = table[a, b]
            table[a, b] = max(
                previous,
                *(
                    packed_rank([DECK[a], DECK[b], DECK[c], DECK[d], DECK[e]])
                    for c, d, e in triples
                ),
            )
        street.append(table.copy())
    return street


def percentile(values):
    """Compare equal ranks equally, using the uniform distribution of four-card holdings."""
    ranks, inv, freq = np.unique(values, return_inverse=True, return_counts=True)
    categories = ranks // 16**5
    quant = np.empty(len(ranks))
    for cat in range(len(GROUPS)):
        idx = np.flatnonzero(categories == cat)
        if len(idx):
            mass = freq[idx]
            quant[idx] = (mass.cumsum() - mass * 0.5) / mass.sum()
    return quant[inv]


def straight_info(holes, board, category):
    """Union the distinct rank outs from all 6 hole pairs and all legal 2-board pairs.

    Every counted future straight uses the new card as its third board card.
    Existing straights and stronger holdings do not receive draw credit.
    """
    ranks = tuple(c // 4 for c in board)
    table = np.zeros((13, 13), dtype=np.uint16)
    top = set()
    for window in WINDOWS:
        sw = set(window)
        for r1, r2 in combinations(window, 2):
            extra = sw - {r1, r2}
            if extra <= set(ranks):
                high = window[-1]
                if not top or high > max(x[0] for x in top):
                    top = {(high, r1, r2)}
                elif high == max(x[0] for x in top):
                    top.add((high, r1, r2))
            for a, b in combinations(ranks, 2):
                if a == b or not {a, b} <= extra:
                    continue
                (need,) = extra - {a, b}
                table[r1, r2] |= 1 << need
                table[r2, r1] |= 1 << need
    hr = holes // 4
    outs = np.zeros(len(holes), dtype=np.uint16)
    for a, b in PAIR_SLOTS:
        outs |= table[hr[:, a], hr[:, b]]
    count = np.bitwise_count(outs) * 4
    for r in ranks:
        count -= ((outs >> r) & 1).astype(np.uint8)
    for i in range(4):
        count -= ((outs >> hr[:, i]) & 1).astype(np.uint8)
    count[category >= 4] = 0
    top_ranks = {r for _, a, b in top for r in (a, b)}
    blockers = np.isin(hr, list(top_ranks)).sum(axis=1, dtype=np.uint8)
    return count, blockers


def suit_info(holes, board):
    fd = np.zeros(len(holes), dtype=bool)
    nut_fd = fd.copy()
    nut_blocker = fd.copy()
    for s in range(4):
        visible = [c for c in board if c % 4 == s]
        if len(visible) < 2:
            continue
        nut = next(c for c in range(48 + s, s - 1, -4) if c not in visible)
        owns = (holes == nut).any(axis=1)
        nut_blocker |= owns
        if len(visible) == 2:
            draw = (holes % 4 == s).sum(axis=1) >= 2
            fd |= draw
            nut_fd |= draw & owns
    return fd, nut_fd, nut_blocker


@dataclass
class Street:
    category: np.ndarray
    rank_fraction: np.ndarray
    straight_outs: np.ndarray
    flush_draw: np.ndarray
    nut_draw: np.ndarray
    flush_blocker: np.ndarray
    straight_blockers: np.ndarray
    board_match: np.ndarray
    strength: np.ndarray
    draw_score: np.ndarray
    blocker_score: np.ndarray


def street_features(cards, board, table):
    scores = np.maximum.reduce([table[cards[:, a], cards[:, b]] for a, b in PAIR_SLOTS])
    category = (scores // 16**5).astype(np.uint8)
    # A rank percentile conditioned on the *street board* must not use river dead cards.
    # Use the effective kicker values directly, normalized across faces (deuce through ace).
    kick1 = ((scores // 16**4) % 16).astype(float)
    kick2 = ((scores // 16**3) % 16).astype(float)
    frac = (kick1 - 2) / 12 * 0.8 + np.maximum(0, kick2 - 2) / 12 * 0.2
    outsiders, st_block = straight_info(cards, board, category)
    fd, nfd, flush_block = suit_info(cards, board)
    dup = {r for r, n in Counter(c // 4 for c in board).items() if n >= 2}
    paired_board_block = np.isin(cards // 4, list(dup)).any(axis=1)
    p = PARAMETERS["postflop"]
    strength = (
        np.take(p["made_score"], category) + frac * p["within_category_rank_weight"]
    )
    draw = (
        np.minimum(outsiders, p["straight_out_cap"]) * p["straight_out_weight"]
        + fd * p["fd_weight"]
        + nfd * p["nut_fd_bonus"]
    )
    # Lower straights and flushes can still redraw. This deliberately conservative draw
    # proxy only credits flush improvement when the current hand is below flush strength.
    draw -= (category >= 5) * (fd * p["fd_weight"] + nfd * p["nut_fd_bonus"])
    blocker = (
        flush_block.astype(float)
        + np.minimum(2, st_block) * 0.35
        + paired_board_block * 0.5
    ) * p["blocker_weight"]
    return Street(
        category,
        frac,
        outsiders,
        fd,
        nfd,
        flush_block,
        st_block,
        paired_board_block,
        strength,
        draw,
        blocker,
    ), scores


def pre_strength(cards):
    p = STRUCTURE
    ranks = cards // 4
    suits = cards % 4
    rc = np.stack([(ranks == r).sum(axis=1) for r in range(13)], axis=1)
    sc = np.stack([(suits == s).sum(axis=1) for s in range(4)], axis=1)
    window = np.maximum.reduce([(rc[:, w] > 0).sum(axis=1) for w in WINDOWS])
    nut_suit = np.logical_or.reduce(
        [((cards == 48 + s).any(axis=1) & (sc[:, s] >= 2)) for s in range(4)]
    )
    pairs = rc >= 2
    highest_pair = np.max(np.where(pairs, np.arange(2, 15), 0), axis=1)
    return (
        p["intercept"]
        + p["broadway"] * (ranks >= 8).sum(axis=1)
        + p["suited_pair"] * (sc >= 2).sum(axis=1)
        + p["suited_ace"] * nut_suit
        + p["straight_window_rank"] * (window - 2)
        + p["top_pair_rank"] * highest_pair / 14
        + p["aces"] * pairs[:, 12]
        + p["second_pair"] * (pairs.sum(axis=1) >= 2)
        + p["three_of_rank"] * (rc.max(axis=1) >= 3)
        + p["monotone_penalty"] * (sc.max(axis=1) >= 3)
    )


@lru_cache(maxsize=2)
def _board_features(board):
    if len(board) != 5 or len(set(board)) != 5:
        raise ValueError("Expected 5 distinct river board cards")
    board_ids = tuple(DECK.index(c) for c in board)
    available = [c for c in range(52) if c not in board_ids]
    hands = np.array(available, dtype=np.uint8)[templates()]
    tables = pair_scores(board_ids, available)
    flop, _ = street_features(hands, board_ids[:3], tables[0])
    turn, _ = street_features(hands, board_ids[:4], tables[1])
    river, scores = street_features(hands, board_ids, tables[2])
    # Co-locate similar histories, kicker strength and blocker patterns. The final hash
    # orders equivalent feature rows without favoring a particular pair of side cards.
    kicker_bin = np.minimum(15, (percentile(scores) * 16).astype(np.uint8))
    fb = river.flush_blocker.astype(np.uint8)
    sb = np.minimum(river.straight_blockers, 2).astype(np.uint8)

    def draw_bin(st):
        straight = np.select(
            [st.straight_outs > 8, st.straight_outs >= 5, st.straight_outs > 0],
            [3, 2, 1],
            0,
        )
        flush = np.where(st.nut_draw, 3, np.where(st.flush_draw, 2, 0))
        return np.maximum(straight, flush).astype(np.uint8)

    bits = (kicker_bin, flop.category, turn.category, draw_bin(flop), draw_bin(turn))
    order_code = np.zeros(len(hands), dtype=np.uint64)
    for bit in (3, 2, 1, 0):
        for feat in bits:
            order_code = order_code * 2 + ((feat >> bit) & 1)
        if bit == 2:
            order_code = (order_code * 2 + fb) * 3 + sb
            order_code = order_code * 2 + river.board_match
    code = np.zeros(len(hands), dtype=np.uint64)
    for i in range(4):
        code = code * 52 + hands[:, i]
    mix = code.copy()
    mix ^= mix >> np.uint64(30)
    mix *= np.uint64(0xBF58476D1CE4E5B9)
    mix ^= mix >> np.uint64(27)
    mix *= np.uint64(0x94D049BB133111EB)
    mix ^= mix >> np.uint64(31)
    ordering = np.lexsort((mix, order_code, river.category))
    return (
        hands,
        code,
        pre_strength(hands),
        flop,
        turn,
        river.category,
        ordering,
        scores,
    )


def board_features(board):
    return _board_features(board)[:-1]


def logistic(x):
    return 1 / (1 + np.exp(-x))


def actions_for(history, player):
    actions = []
    raises = 0
    last_street = None
    for move in history:
        if move.street == "River":
            break
        if move.street != last_street:
            raises = 0
        last_street = move.street
        if move.name == player and move.action in (
            "check",
            "bet",
            "raise",
            "call",
            "fold",
        ):
            actions.append((move.street, move.action, raises))
        if move.action in ("bet", "raise"):
            raises += 1
    return actions


def likelihood(pre, flop, turn, actions, profile, parameters=PARAMETERS):
    result = np.ones(len(pre))
    p = parameters["profiles"][profile]
    a = parameters["preflop"]
    b = parameters["postflop"]
    for street, mv, raises in actions:
        if street == "Preflop":
            enter = logistic(
                a["enter_intercept"]
                + pre * a["decision_slope"]
                + p["entry_shift"]
                - raises * a["prior_raise_enter_penalty"]
            )
            aggressive = logistic(
                a["raise_intercept"]
                + pre * a["raise_structure_slope"]
                + p["thin_aggression_shift"]
                - raises * a["prior_raise_aggression_penalty"]
            )
            if mv == "check":
                prob = 1 - aggressive
            elif mv == "raise":
                prob = enter * aggressive
            elif mv == "call":
                prob = enter * (1 - aggressive)
            elif mv == "fold":
                prob = 1 - enter
            else:
                raise ValueError(f"Unsupported preflop action {mv}")
        else:
            st = flop if street == "Flop" else turn
            draw = st.draw_score * (b["turn_draw_discount"] if street == "Turn" else 1)
            strength = st.strength
            bluff = p["trash_bluff_shift"] * (st.category <= 1)
            if mv in ("bet", "check"):
                aggressive = logistic(
                    b["bet_intercept"]
                    + strength * b["bet_strength_slope"]
                    + draw * b["bet_draw_slope"]
                    + st.blocker_score
                    + p["thin_aggression_shift"]
                    + bluff
                )
                floor = b["slowplay_probability_floor"]
                aggressive = floor + (1 - 2 * floor) * aggressive
                prob = aggressive if mv == "bet" else 1 - aggressive
            else:
                pressure = max(0, raises - 1) * b["facing_raise_penalty"]
                stay = logistic(
                    b["continue_intercept"]
                    + strength * b["continue_strength_slope"]
                    + draw * b["continue_draw_slope"]
                    + p["continuation_shift"]
                    - pressure
                )
                attack = logistic(
                    b["raise_intercept"]
                    + strength * b["raise_strength_slope"]
                    + draw * b["raise_draw_slope"]
                    + st.blocker_score
                    + p["thin_aggression_shift"]
                    + bluff
                    - pressure
                )
                prob = {
                    "call": stay * (1 - attack),
                    "raise": stay * attack,
                    "fold": 1 - stay,
                }[mv]
        result *= prob
    return result


def weighted_groups(hands, category, ordering, weights, hero_index, count, strata=None):
    full = weights.sum()
    if not full:
        raise ValueError("No supported holdings")
    ordered = ordering[(weights[ordering] > 0) & (ordering != hero_index)]
    cats = (category if strata is None else strata)[ordered]
    boundaries = np.r_[0, np.flatnonzero(np.diff(cats)) + 1, len(ordered)]
    sections = np.split(ordered, boundaries[1:-1])
    budget = count - int(hero_index is not None)
    if budget < len(sections) or budget > len(ordered):
        raise ValueError("Representative count outside supported interval")
    masses = np.array([weights[rows].sum() for rows in sections])
    sizes = np.array([len(rows) for rows in sections])
    slots = np.ones(len(sections), dtype=int)
    while slots.sum() < budget:
        avail = slots < sizes
        extra = budget - slots.sum()
        target = np.where(avail, masses, 0)
        target = extra * target / target.sum()
        add = np.minimum(sizes - slots, np.floor(target).astype(int))
        if add.sum() == 0:
            winners = np.argsort(-np.where(avail, target, -1), kind="stable")[
                : min(extra, avail.sum())
            ]
            add[winners] = 1
        slots += add
    ids, totals = [], []
    for rows, ns in zip(sections, slots, strict=True):
        cumulative = np.cumsum(weights[rows])
        edges = np.r_[
            0,
            np.searchsorted(cumulative, cumulative[-1] * np.arange(1, ns) / ns) + 1,
            len(rows),
        ]
        # Enforce a representative for every slot, including heavy single holdings.
        edges = np.maximum.accumulate(edges - np.arange(ns + 1)) + np.arange(ns + 1)
        edges = np.minimum(edges, len(rows) - ns + np.arange(ns + 1))
        cs = np.r_[0.0, cumulative]
        left = cs[edges[:-1]]
        w = cs[edges[1:]] - left
        picks = np.searchsorted(cumulative, left + w * 0.5)
        ids.extend(rows[picks])
        totals.extend(w)
    if hero_index is not None:
        ids.append(hero_index)
        totals.append(weights[hero_index])
    return [
        {"cards": [DECK[c] for c in hands[i]], "weight": float(w)}
        for i, w in zip(ids, totals, strict=True)
    ], np.bincount(category[ids], weights=totals, minlength=9) / full


def composition(array):
    return {name: float(value) for name, value in zip(GROUPS, array, strict=True)}


def seat_ranges(
    board,
    hero_cards,
    history,
    player,
    counts,
    *,
    parameters=PARAMETERS,
    reference_cards=(),
    stratify=False,
    profiles=PROFILES,
):
    """Build a player's three arrival priors. All amounts are unnormalized relative weights."""
    board = tuple(board)
    counts = tuple(sorted({int(k) for k in counts}))
    hands, codes, pre, flop, turn, category, ordering, scores = _board_features(board)
    hero_index = None
    if hero_cards:
        if (
            len(hero_cards) != 4
            or len(set(hero_cards)) != 4
            or set(board).intersection(hero_cards)
        ):
            raise ValueError("Expected four distinct Hero cards outside the board")
        key = 0
        for card in sorted(DECK.index(c) for c in hero_cards):
            key = key * 52 + card
        hero_index = int(np.searchsorted(codes, key))
        if hero_index == len(codes) or codes[hero_index] != key:
            raise ValueError("Illegal Hero holding")
    actions = actions_for(history, player)
    strata = category
    if stratify:
        # Keep both showdown outcome and flush-suit ownership intact inside
        # each quantile. Preserve card compatibility with the queried hand.
        suit, occurrences = Counter(c[1] for c in board).most_common(1)[0]
        suited = (
            (hands % 4 == SUITS.index(suit)).any(axis=1) if occurrences >= 3 else False
        )
        strata = category.astype(np.int16) * 2 + suited
        if reference_cards:
            ids = sorted(DECK.index(c) for c in reference_cards)
            code = 0
            for card in ids:
                code = code * 52 + card
            index = np.searchsorted(codes, code)
            if index == len(codes) or codes[index] != code:
                raise ValueError("Illegal reference holding")
            result = np.sign(scores - scores[index]) + 1
            clashes = np.isin(hands, ids).any(axis=1)
            strata = (strata * 3 + result) * 2 + clashes
        ordering = ordering[np.argsort(strata[ordering], kind="stable")]
    output = {k: {} for k in counts}
    for profile in profiles:
        prior = likelihood(pre, flop, turn, actions, profile, parameters)
        tiers, counts_per_tier = np.unique(prior, return_counts=True)
        lost_mass = np.cumsum(tiers * counts_per_tier)
        cutoff = tiers[
            min(
                np.searchsorted(
                    lost_mass,
                    prior.sum() * parameters["profiles"][profile]["tail_mass"],
                    side="right",
                ),
                len(tiers) - 1,
            )
        ]
        weights = np.where(prior >= cutoff, prior, 0)
        probe = bool(hero_index is not None and weights[hero_index] == 0)
        if probe:
            weights[hero_index] = prior[hero_index]
        weights_sum = weights.sum()
        actual = composition(
            np.bincount(category, weights=weights, minlength=9) / weights_sum
        )
        meta = {
            "model": "street-likelihood-v3" if stratify else "street-likelihood-v2",
            "universe_count": len(hands),
            "support_count": int(np.count_nonzero(weights)),
            "parameters": parameters,
            "grouping_strata": (
                "river category, flush-suit ownership, outcome and card compatibility against reference"
                if stratify and reference_cards
                else "river category and flush-suit ownership"
                if stratify
                else "river category"
            ),
            "profile": profile,
            "observed_actions": actions,
            "tail_cutoff": float(cutoff),
            "discarded_mass_fraction": float(1 - weights_sum / prior.sum()),
            "hero_probe": probe,
            "composition_full": actual,
            "hero_probe_note": "If below the cutoff, keep the queried hand alone at its original likelihood.",
        }
        for count, profile_rows in output.items():
            entries, represented = weighted_groups(
                hands, category, ordering, weights, hero_index, count, strata
            )
            profile_rows[profile] = {
                "entries": entries,
                "metadata": {
                    **meta,
                    "representatives": len(entries),
                    "composition_represented": composition(represented),
                },
            }
    return output
