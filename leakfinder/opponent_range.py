"""Retrospective PLO4 range scenarios for a single opponent on the river.

The same uniform sample of complete, legal four-card holdings is carried
through the entire line. Hero and the five final board cards are removed even
at earlier checkpoints; this is a river review, not a live preflop prediction.
The action rules see only the cards on that street and the candidate's own
cards. Positive compatibility multipliers are authored assumptions, not
opponent frequencies. Nothing is eliminated on the basis of an action.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from math import comb
from random import Random

from .river_holdings import (
    CARD_ID,
    DECK,
    PAIR_INDEX,
    UNIT,
    WINDOWS,
    _kickers,
    _label,
    _score,
    _scores,
    _straight_out_masks,
)

STREETS = ("Preflop", "Flop", "Turn", "River")
CLASSES = (
    "high card",
    "pair",
    "two pair",
    "three of a kind",
    "straight",
    "flush",
    "full house",
    "quads",
    "straight flush",
)
PRE = ("poorly connected", "partly connected", "coordinated or high pair", "premium")
POST = ("little showdown value", "marginal", "medium", "strong", "nuts")
LIMITS = (
    "The same uniform sample of full PLO4 holdings excludes all five known board cards. "
    "Each action adjusts the weights according to the model. "
    "Shares represent model weights. Actual opponent frequencies need separate historical evidence. "
    "Every comparison against Hero uses the final river board. "
    "The sample can miss rare holdings. "
    "The model considers each opponent separately in a multiway pot."
)
STRENGTH_DEFINITIONS = (
    "Little showdown value includes high card and pairs below the highest board rank.",
    "Marginal includes top pair, overpairs, two pair and trips on a paired board.",
    (
        "Medium includes trips on an unpaired board, a straight when a flush or full house is possible, "
        "and a flush on a paired board."
    ),
    "Strong includes a full house or better, a flush on an unpaired board and a straight when a flush or full house is impossible.",
    "The nuts are the strongest rank available on the board. This category takes precedence.",
)


@dataclass(frozen=True)
class RangeExample:
    cards: tuple[str, ...]
    category: int
    kickers: tuple[int, ...]
    role: str
    reason: str

    @property
    def text(self):
        return f"{' '.join(self.cards)} ({_label(self.category, self.kickers)})"


@dataclass(frozen=True)
class RangeStage:
    street: str
    actions: tuple[str, ...]
    reason: str
    rules: tuple[str, ...]
    effective_n: float
    categories: dict[str, float]
    category_counts: dict[str, int]
    stronger: float
    tied: float
    weaker: float
    draw_share: float | None
    examples: dict[str, RangeExample]
    by_strength: tuple[tuple[float, ...], ...]
    current_relation: (
        tuple[float, float, float] | None
    )  # weaker, tied, stronger on that street.


@dataclass(frozen=True)
class RiverRange:
    opponent: str
    position: str
    multiway: bool
    sample_size: int
    legal_total: int
    stages: tuple[RangeStage, ...]
    unweighted_counts: dict[str, int]
    intensity: float
    limitations: str = LIMITS
    strength_definitions: tuple[str, ...] = STRENGTH_DEFINITIONS

    @property
    def summary(self):
        prev, end = self.stages[-2:]
        return (
            f"{self.position}, {self.sample_size:,} of {self.legal_total:,} legal PLO4 holdings. "
            f"The weighted share of holdings that beat Hero on the river is {prev.stronger:.0%} after the turn "
            f"and {end.stronger:.0%} after the observed river actions."
        )


def _pre_band(cards):
    ranks = [c // 4 for c in cards]
    suits = Counter(c % 4 for c in cards)
    counts = Counter(ranks)
    pair = max((r for r, n in counts.items() if n == 2), default=-1)
    paired_suits = sum(n >= 2 for n in suits.values())
    suited_ace = any(c // 4 == 12 and suits[c % 4] >= 2 for c in cards)
    connected = max(len(window.intersection(ranks)) for _, window in WINDOWS)
    if max(counts.values()) > 2:
        return 0
    if pair == 12 or (pair == 11 and paired_suits == 2):
        return 3
    if (
        pair >= 9
        and paired_suits
        or paired_suits == 2
        and connected >= 3
        or connected == 4
        and paired_suits
        and min(ranks) >= 4
    ):
        return 2
    return int(
        bool(
            suited_ace
            or paired_suits == 2
            or pair >= 0
            or connected >= 3
            and paired_suits
        )
    )


@dataclass(frozen=True, slots=True)
class Made:
    score: int
    band: int
    draw: bool
    blocker: bool


class BoardFeatures:
    def __init__(self, board):
        base, suits = _scores(board)
        deck = tuple(c for c in range(52) if c not in board)
        masks = _straight_out_masks(board, deck) if len(board) < 5 else None
        counts = Counter(c % 4 for c in board)
        self.flush_suits = {s for s, n in counts.items() if n >= 3}
        self.top_suited = {max(c for c in deck if c % 4 == s) for s in self.flush_suits}
        self.paired = len({c // 4 for c in board}) < len(board)
        self.high = max(c // 4 for c in board) + 2
        self.pairs = {}
        self.nuts = 0
        for a, b in combinations(deck, 2):
            score = _score(a, b, base, suits)
            drawing = bool(
                masks
                and (masks[PAIR_INDEX[a][b]] or (a % 4 == b % 4 and counts[a % 4] == 2))
            )
            self.pairs[a, b] = (score, drawing)
            self.nuts = max(self.nuts, score)

    def evaluate(self, cards, pairs):
        values = [self.pairs[p] for p in pairs]
        score = max(value for value, _ in values)
        cat = score // UNIT
        draw = cat < 4 and any(drawing for _, drawing in values)
        blocker = any(
            c in self.top_suited and sum(x % 4 == c % 4 for x in cards) == 1
            for c in cards
        )
        if score == self.nuts:
            band = 4
        elif (
            cat >= 6
            or (cat == 5 and not self.paired)
            or (cat == 4 and not self.paired and not self.flush_suits)
        ):
            band = 3
        elif cat >= 4 or (cat == 3 and not self.paired):
            band = 2
        elif cat >= 2 or cat == 1 and (score >> 16 & 15) >= self.high:
            band = 1
        else:
            band = 0
        return Made(score, band, draw, blocker)


@dataclass(frozen=True, slots=True)
class Holding:
    cards: tuple[int, ...]
    pre: int
    streets: tuple[Made, ...]
    relation: int
    current: tuple[int, ...]
    role: str
    detail: str


@lru_cache(maxsize=16)
def _candidates(hero, board, n):
    decks = [BoardFeatures(board[:k]) for k in (3, 4, 5)]
    own_pairs = tuple(combinations(hero, 2))
    own = tuple(d.evaluate(hero, own_pairs).score for d in decks)
    deck = tuple(c for c in range(52) if c not in hero and c not in board)
    # Sampling unique uniformly generated combinations is sampling without replacement.
    rng = Random("river-range-v1/" + "".join(DECK[c] for c in hero + board))
    seen = set()
    sample = []
    while len(sample) < n:
        cards = tuple(sorted(rng.sample(deck, 4)))
        if cards in seen:
            continue
        seen.add(cards)
        pairs = tuple(combinations(cards, 2))
        streets = tuple(d.evaluate(cards, pairs) for d in decks)
        turn, river = streets[1:]
        current = tuple((f.score > h) - (f.score < h) for f, h in zip(streets, own))
        relation = current[-1]
        category = river.score // UNIT
        if relation > 0 and (river.band >= 2 or category >= 2):
            role, detail = "value", "A made hand that beats Hero."
        elif relation < 0 and category <= 1 and (turn.draw or river.blocker):
            role = "bluff"
            detail = (
                "This had a turn draw that missed both a straight and a flush."
                if turn.draw
                else "This holds only one card of the flush suit, the highest available off the board."
            )
        elif relation < 0 and category >= 1:
            role, detail = "weak_call", "A weaker made hand with showdown value."
        elif relation == 0:
            role, detail = (
                "tie",
                "The best two hole cards and three board cards tie Hero.",
            )
        else:
            role, detail = "", ""
        sample.append(
            Holding(cards, _pre_band(cards), streets, relation, current, role, detail)
        )
    return tuple(sample)


@dataclass(frozen=True)
class Observation:
    street: str
    move: str
    text: str
    large: bool
    raises: int
    checkback: bool
    limpers: int


def _observations(snap, opponent):
    """Reconstruct the price from preceding transfers, independently of room."""
    history = snap.history
    pot = snap.pot - sum(
        -a.amount if a.action == "return" else a.amount for a in history
    )
    actions = []
    street = None
    limpers = 0
    for i, a in enumerate(history):
        if a.street != street:
            street, commits, price, raises, last_size = (
                a.street,
                defaultdict(int),
                0,
                0,
                None,
            )
        owed = max(0, price - commits[a.name])
        to = commits[a.name] + a.amount
        if a.action in {"bet", "raise"}:
            increment = to - price
            last_size = increment / (pot + owed) if pot + owed > 0 else None
            raises += 1
        if a.name == opponent and a.action in {"check", "call", "bet", "raise"}:
            bb = a.amount / snap.bb
            if a.action == "raise":
                desc = f"raise to {to / snap.bb:g} BB"
            elif a.action == "check":
                desc = "check"
            else:
                desc = f"{a.action} {bb:g} BB"
            if a.street == "Preflop" and a.action in {"raise", "call"}:
                role = ""
                if a.action == "raise":
                    role = (
                        ("isolation raise after a limp" if limpers else "first open")
                        if raises == 1
                        else f"{raises + 1}-bet"
                    )
                elif not raises:
                    role = "limp"
                else:
                    role = (
                        "call of an open"
                        if raises == 1
                        else f"call of a {raises + 1}-bet"
                    )
                desc = f"{role}, {desc}"
            if a.action != "check" and last_size is not None:
                if a.action == "raise":
                    desc += f" (increase of {last_size:.0%} of the pot after calling)"
                elif a.action == "bet":
                    desc += f" ({last_size:.0%} of the pot at the time)"
                else:
                    desc += f" (last bet or raise {last_size:.0%})"
            # A check at the current decision cannot close the street.
            closed = (
                a.street != snap.street
                and a.action == "check"
                and (i + 1 == len(history) or history[i + 1].street != a.street)
            )
            large = last_size is not None and last_size >= 2 / 3
            actions.append(
                Observation(
                    a.street,
                    a.action,
                    f"{desc} [{a.ref}]",
                    large,
                    raises,
                    closed,
                    limpers,
                )
            )
        if a.street == "Preflop" and a.action == "call" and not raises:
            limpers += 1
        if a.action != "posts_ante":
            commits[a.name] += -a.amount if a.action == "return" else a.amount
            price = max(commits.values())
        pot += -a.amount if a.action == "return" else a.amount
    return actions


@dataclass(frozen=True)
class Rule:
    label: str
    by_band: tuple[float, ...]
    draw_floor: float = 0
    blocker_floor: float = 0
    missed_floor: float = 0

    def explain(self, preflop):
        bands = PRE if preflop else POST
        parts = [f"{b} ×{w:g}" for b, w in zip(bands, self.by_band)]
        if self.draw_floor:
            parts.append(f"live draw at least ×{self.draw_floor:g}")
        if self.blocker_floor:
            parts.append(
                f"highest single card of a possible flush suit at least ×{self.blocker_floor:g}"
            )
        if self.missed_floor:
            parts.append(
                f"missed turn draw with at most a pair at least ×{self.missed_floor:g}"
            )
        return self.label + " (" + ", ".join(parts) + ")."


def _rule(a, position):
    large = a.large
    if a.street == "Preflop":
        tight_pos = position in {"UTG", "UTG+1", "HJ", "MP"}
        if a.move == "check":
            return Rule(
                "A free preflop check keeps the full distribution", (1, 1, 1, 1)
            )
        if a.move == "raise" and a.raises >= 2:
            return Rule(
                "Reraising favors premium and coordinated hands",
                (0.5, 0.75, 1.5, 2.5),
            )
        if a.move == "raise" and a.limpers:
            return Rule(
                "Isolating a limper favors coordination and allows a wider range in late position",
                (0.6, 0.9, 1.5, 1.85) if tight_pos else (0.8, 1, 1.35, 1.65),
            )
        if a.move == "raise" and tight_pos:
            return Rule(
                "An early-position open puts more weight on stronger groups",
                (0.5, 0.85, 1.5, 2),
            )
        if a.move == "raise":
            return Rule(
                "Late-position and blind opens allow wider ranges",
                (0.75, 1, 1.35, 1.75),
            )
        if a.raises >= 2:
            return Rule(
                "Calling a reraise favors connected hands",
                (0.5, 0.8, 1.6, 1.75),
            )
        if position in {"BB", "SB"} and a.raises <= 1 and not large:
            return Rule(
                "A cheap call from the blinds has little effect on the range",
                (0.85, 1, 1.15, 1.15),
            )
        if not a.raises:
            return Rule(
                "Limping slightly favors the middle groups", (0.85, 1.15, 1.2, 0.9)
            )
        return Rule("Calling an open favors coordinated hands", (0.7, 1, 1.45, 1.2))

    if a.move == "check":
        if not a.checkback:
            return Rule(
                "Checking with a player still to act leaves the weights unchanged",
                (1, 1, 1, 1, 1),
            )
        return Rule(
            "Checking back slightly increases the weight of medium showdown value",
            (1.0, 1.2, 1.2, 0.9, 0.85),
        )
    if a.move == "call":
        if large or a.raises >= 2:
            return Rule(
                "A larger call favors stronger made hands and live draws",
                (0.5, 0.85, 1.4, 1.65, 1.4),
                draw_floor=1.45 if a.street != "River" else 0,
            )
        return Rule(
            "Calling a smaller bet retains more hands with showdown value",
            (0.7, 1.25, 1.4, 1.25, 1.2),
            draw_floor=1.4 if a.street != "River" else 0,
        )
    polarized = large or a.move == "raise"
    if a.street == "River":
        if polarized:
            return Rule(
                "A large river bet or raise favors strong value hands and plausible bluffs",
                (0.5, 0.65, 0.85, 1.8, 2.25),
                blocker_floor=1.35,
                missed_floor=1.15,
            )
        return Rule(
            "A small river bet can include thinner value",
            (0.75, 1, 1.25, 1.55, 1.7),
            blocker_floor=1.15,
            missed_floor=1,
        )
    if polarized:
        return Rule(
            "A large bet or raise increases the weight of strong hands and semibluffs",
            (0.55, 0.75, 1.25, 1.65, 2),
            draw_floor=1.5,
            blocker_floor=1.0,
        )
    return Rule(
        "A small bet retains a wider range",
        (0.8, 1, 1.2, 1.4, 1.55),
        draw_floor=1.3,
        blocker_floor=1,
    )


def _multiplier(rule, candidate, street):
    if street == 0:
        return rule.by_band[candidate.pre]
    f = candidate.streets[street - 1]
    weight = rule.by_band[f.band]
    if f.draw:
        weight = max(weight, rule.draw_floor)
    if f.blocker:
        weight = max(weight, rule.blocker_floor)
    if street == 3 and candidate.streets[1].draw and f.score // UNIT <= 1:
        weight = max(weight, rule.missed_floor)
    return weight


def _checkpoint(candidates, weights, street, actions, rules, intensity):
    classes, counts = defaultdict(float), Counter()
    total, squared, drawn = sum(weights), sum(w * w for w in weights), 0.0
    outcomes = [0.0, 0.0, 0.0]
    current = [0.0, 0.0, 0.0]
    band_mass = [[0.0, 0.0, 0.0] for _ in POST]
    examples = {}
    for c, w in zip(candidates, weights):
        if street:
            made = c.streets[street - 1]
            name = CLASSES[made.score // UNIT]
            drawn += w * made.draw
            current[c.current[street - 1] + 1] += w
        else:
            name = PRE[c.pre]
        classes[name] += w
        counts[name] += 1
        outcomes[c.relation + 1] += w
        band_mass[c.streets[-1].band][c.relation + 1] += w
        if c.role:
            # Select the largest individual scenario weight, using a deterministic
            # tie break in favor of stronger value or marginal showdown holdings.
            rank = c.streets[-1].score
            priority = (w, -rank if c.role == "bluff" else rank)
            if c.role not in examples or priority > examples[c.role][0]:
                examples[c.role] = (priority, c)
    roles = {}
    for role, (_, candidate) in examples.items():
        river = candidate.streets[2].score
        roles[role] = RangeExample(
            tuple(DECK[c] for c in reversed(candidate.cards)),
            river // UNIT,
            _kickers(river),
            role,
            candidate.detail,
        )
    return RangeStage(
        STREETS[street],
        tuple(a.text for a in actions),
        " ".join(dict.fromkeys(rule.label + "." for rule in rules))
        if rules
        else "This player took no voluntary action on this street before the selected decision.",
        tuple(
            rule.explain(street == 0)
            + (
                f" Weights are raised to the power of {intensity:g}."
                if intensity != 1
                else ""
            )
            for rule in rules
        ),
        total * total / squared,
        {name: w / total for name, w in sorted(classes.items(), key=lambda kv: -kv[1])},
        dict(counts),
        outcomes[2] / total,
        outcomes[1] / total,
        outcomes[0] / total,
        drawn / total if street in (1, 2) else None,
        roles,
        tuple(tuple(v / total for v in band) for band in band_mass),
        tuple(v / total for v in current) if street else None,
    )


def condition_range(snap, opponent, sample_size=768, *, intensity=1.0):
    """Return a deterministic four-stage scenario at an eligible river decision.

    The opponent may be a live ReplaySeat or its exact screen name. Returns
    None for incomplete or out-of-scope evidence. The maximum is 4,096 unique
    candidates; all share and example fields are conditional on this sample.
    """
    if (
        snap.street != "River"
        or snap.bb <= 0
        or len(snap.hero_cards or ()) != 4
        or len(snap.board) != 5
    ):
        return None
    name = getattr(opponent, "name", opponent)
    seat = next(
        (s for s in snap.seats if s.name == name and s.live and s.name != snap.hero),
        None,
    )
    if seat is None:
        return None
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or not 32 <= sample_size <= 4096
    ):
        raise ValueError("Choose 32–4096 candidate holdings.")
    if isinstance(intensity, bool) or intensity not in (0.75, 1.0, 1.25):
        raise ValueError("Choose a compatibility exponent of 0.75, 1 or 1.25.")
    try:
        hero = tuple(sorted(CARD_ID[c] for c in snap.hero_cards))
        board = tuple(CARD_ID[c] for c in snap.board)
    except (KeyError, TypeError):
        return None
    if len(set(hero + board)) != 9:
        return None
    board = tuple(sorted(board[:3])) + board[3:]  # Keep the true turn and river.
    candidates = _candidates(hero, board, sample_size)
    observed = _observations(snap, name)
    weights = [1.0] * sample_size
    stages = []
    for i, street in enumerate(STREETS):
        actions = [a for a in observed if a.street == street]
        rules = [_rule(a, seat.position) for a in actions]
        for rule in rules:
            for j, candidate in enumerate(candidates):
                weight = _multiplier(rule, candidate, i)
                weights[j] *= weight if intensity == 1.0 else weight**intensity
        stages.append(_checkpoint(candidates, weights, i, actions, rules, intensity))
    raw = Counter(c.relation for c in candidates)
    return RiverRange(
        name,
        seat.position,
        sum(s.live for s in snap.seats) > 2,
        sample_size,
        comb(43, 4),
        tuple(stages),
        {"stronger": raw[1], "tied": raw[0], "weaker": raw[-1]},
        intensity,
    )
