"""Board- and line-specific reasons for an opening postflop bet."""

from collections import Counter
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement

from .importers import STREETS
from .vendor.common import RANK_VALUE

STRAIGHTS = tuple(frozenset(range(low, low + 5)) for low in range(2, 11)) + (
    frozenset({14, 2, 3, 4, 5}),
)
RANK_LABEL = {v: r for r, v in RANK_VALUE.items()}
STREET_LABEL = {"Preflop": "Preflop", "Flop": "On the flop", "Turn": "On the turn"}


@dataclass(frozen=True)
class BetSize:
    amount: int
    pot: int
    bb: int
    effective: int

    @property
    def label(self):
        return f"bet about {self.amount / self.pot:.0%} pot ({self.amount / self.bb:.2f} BB)"

    @property
    def contestable(self):
        return min(self.amount, self.effective)

    def far_from(self, observed):
        # The policy uses coarse sizes. Ignore rounding and nominal money that
        # could not be matched by anyone still in the hand.
        return (
            4 * abs(min(observed, self.effective) - min(self.amount, self.effective))
            >= self.pot
        )


def bet_size(snap, hero, opponents, percent):
    effective = max(seat.stack for seat in opponents)
    ceiling = min(hero.stack, snap.pot, max(snap.bb, effective))
    amount = min(ceiling, max(snap.bb, round(snap.pot * percent / 100)))
    return BetSize(amount, snap.pot, snap.bb, effective)


def line_context(snap, hero, opponents, actions):
    """Describe the actual line and collect each surviving player's last-street evidence."""
    names = {s.name: s.position for s in snap.seats}
    live = {s.name for s in opponents}
    checks = [names[a.name] for a in actions if a.action == "check" and a.name in live]
    prefix = f"{' and '.join(checks)} checked. " if checks else ""
    last = STREETS[STREETS.index(snap.street) - 1]
    prior = [
        a
        for a in snap.history
        if a.street == last and a.action in {"raise", "bet", "call", "check"}
    ]
    aggressions = [a for a in prior if a.action in {"raise", "bet"}]
    wager = aggressions[-1] if aggressions else None
    if wager:
        responses = prior[prior.index(wager) + 1 :]
        callers = [
            names[a.name] for a in responses if a.action == "call" and a.name in live
        ]
        amount = wager.raised_to if wager.raised_to is not None else wager.amount
        actor = "you" if wager.name == hero.name else names[wager.name]
        verb = "raised to" if wager.action == "raise" else "bet"
        prefix += f"{STREET_LABEL[last]} {actor} {verb} {amount / snap.bb:g} BB."
        if callers:
            prefix += f" {' and '.join(callers)} called."
        if any(a.name == hero.name and a.action == "call" for a in responses):
            prefix += " You called."
        contested = last != "Preflop"
        start_pot = snap.pot - sum(
            -a.amount if a.action == "return" else a.amount
            for a in snap.history
            if a.street in {last, snap.street}
        )
        large_call = bool(
            callers
            and wager.action == "bet"
            and start_pot > 0
            and amount >= 0.65 * start_pot
        )
        return prefix, bool(callers and contested), contested, large_call
    if last != "Preflop" and prior and all(a.action == "check" for a in prior):
        return prefix + f"{last} checked through.", False, False, False
    return prefix.strip(), False, False, False


def paired_targets(hole, board, rank, kicks):
    # Full houses are suit-independent. Each rank pair must be possible with
    # the remaining cards and combine with exactly three from this board.
    counts = Counter(c[0] for c in (*hole, *board))
    ranks = [r for r in reversed(RANK_VALUE) if counts[r] < 4]
    triples = set(combinations(sorted(c[0] for c in board), 3))
    boats = []
    for a, b in combinations_with_replacement(ranks, 2):
        if a == b and counts[a] > 2:
            continue
        strongest = (0, ())
        for triple in triples:
            pattern = Counter((a, b, *triple))
            shape = sorted(pattern.values())
            if shape in ([2, 3], [1, 4]):
                order = tuple(
                    RANK_VALUE[r]
                    for r in sorted(pattern, key=pattern.get, reverse=True)
                )
                strongest = max(strongest, (6 if shape == [2, 3] else 7, order))
        if strongest[0] == 6 and strongest < (rank, tuple(kicks)):
            boats.append(a + b)
    board_counts = Counter(c[0] for c in board)
    paired = [r for r, n in board_counts.items() if n >= 2]
    trips = [r for r in paired if board_counts[r] == 2 and counts[r] < 4]
    blocked = [r for r in paired if counts[r] == 4 and any(c[0] == r for c in hole)]
    overpairs = [
        r * 2
        for r in ranks
        if counts[r] <= 2 and RANK_VALUE[r] > max(RANK_VALUE[c[0]] for c in board)
    ]
    parts = []
    if boats:
        parts.append("weaker full houses")
    if trips and rank >= 6:
        parts.append("trips")
    if rank >= 6 and board_counts.most_common(1)[0][1] == 2:
        one = max(
            (r for r, n in board_counts.items() if n == 1 and counts[r] < 4),
            key=RANK_VALUE.get,
            default=None,
        )
        if one:
            parts.append(
                f"some {one}x with another playable card"
                if one != "T"
                else "some Tx with another playable card"
            )
        if overpairs and len(parts) < 3:
            parts.append("overpairs")
    return parts, blocked


def weaker_straights(hole, board, hero_high):
    used = Counter(RANK_VALUE[c[0]] for c in hole + board)
    triples = {frozenset(t) for t in combinations({RANK_VALUE[c[0]] for c in board}, 3)}
    pairs = {}
    for straight in STRAIGHTS:
        high = 5 if straight == STRAIGHTS[-1] else max(straight)
        for triple in triples:
            if triple <= straight:
                pair = tuple(sorted(straight - triple, reverse=True))
                if all(used[r] < 4 for r in pair):
                    pairs[pair] = max(high, pairs.get(pair, 0))
    weaker = sorted(
        (p for p, high in pairs.items() if high < hero_high),
        key=lambda p: (pairs[p], p),
        reverse=True,
    )
    return ["".join(RANK_LABEL[r] for r in pair) for pair in weaker[:2]]


def value_plan(
    snap, hero, opponents, actions, rank, kicks, hole, board, paired, flush_on_board
):
    context, called, contested, large_call = line_context(
        snap, hero, opponents, actions
    )
    river = snap.street == "River"
    multiway = len(opponents) > 1
    bet_or_check = ("bet", "check")
    compare_river_sizes = (" Compare checking with a small to larger value bet. "
                           "The price changes which weaker hands can pay.")
    if paired and rank >= 6:
        targets, blocked = paired_targets(hole, board, rank, kicks)
        if not targets:
            return (
                (),
                "A value bet needs a weaker hand that can call. The targets are limited here.",
                None,
            )
        denial = (
            f" You hold all the off-board cards of rank {', '.join(blocked)}, removing "
            f"{', '.join(rank + 'x' for rank in blocked)} from the opponents' ranges."
            if blocked
            else ""
        )
        value = (
            f"{context}{denial} Target value calls from {' or '.join(targets)}.".strip()
        )
        if blocked and not river and not contested:
            fraction = 25 if snap.street == "Flop" else 33
            choices = ("check", "bet") if snap.street == "Flop" else bet_or_check
            checked = {a.name for a in actions if a.action == "check"}
            last_to_act = all(s.name in checked or s.all_in for s in opponents)
            check_line = (
                "Checking back ends this street. A free card may give a weaker hand a reason to pay you later."
                if last_to_act else
                "Checking gives the players behind a chance to bluff. A free card may improve a weaker hand."
            )
            return (
                choices,
                value + " A small bet gives weaker hands a price to call. " + check_line,
                bet_size(snap, hero, opponents, fraction),
            )
        if river and blocked:
            fraction = 50 if large_call else 35 if called else 30
            return (
                bet_or_check,
                value
                + " Use a smaller value bet because the weaker part of the opponent's line struggles to call a large bet. A bigger size makes sense if they call large bets with full houses often enough.",
                bet_size(snap, hero, opponents, fraction),
            )
        fraction = 50 if called else 40
        if river and not multiway and fraction == 40:
            return bet_or_check, value + compare_river_sizes, None
        return (
            bet_or_check,
            value
            + " A moderate bet targets those made hands. Whether they call depends on the opponent and the price.",
            bet_size(snap, hero, opponents, fraction),
        )

    if rank >= 5 and flush_on_board and not paired:
        target = "weaker made flushes"
        fraction = 50 if called and not multiway else 40
        reason = "A medium bet gets value from weaker flushes and gives them a better price than a pot-sized bet."
    elif rank == 4 and river and not paired:
        weaker = weaker_straights(hole, board, kicks[0])
        if not weaker:
            return (
                (),
                "A value bet depends on whether a set or two pair can call on this connected river.",
                None,
            )
        target = "weaker straights and occasional sets"
        fraction = 50 if called else 40
        checked = {a.name for a in actions if a.action == "check"}
        if all(s.name in checked or s.all_in for s in opponents):
            reason = "Bet for value when a lower straight or set can pay. Checking back ends the hand and collects no extra chips."
        else:
            reason = ("Lead for value against hands that can call but would check behind. "
                      "Checking to induce needs an opponent who will bet often enough when checked to.")
    elif rank in (3, 4) and not river and not paired:
        two_tone = Counter(c[1] for c in board).most_common(1)[0][1] == 2
        if rank == 3:
            used = Counter(c[0] for c in hole + board)
            pairs = [
                c[0] * 2
                for c in board
                if RANK_VALUE[c[0]] < kicks[0] and used[c[0]] <= 2
            ]
            target = (
                "lower sets and two pair"
                if pairs
                else "two pair"
            )
            if not two_tone:
                top = RANK_LABEL[kicks[0]]
                if used[top] < 4:
                    target += f", and with a small bet some {top}x with backup"
                board_ranks = {RANK_VALUE[c[0]] for c in board}
                has_straight_draw = any(
                    len(board_ranks & window) >= 2 for window in STRAIGHTS
                )
                texture = (
                    "There is no immediate flush draw, but straight draws can pay to see the next card."
                    if has_straight_draw
                    else "Neither a straight nor a flush can complete on the next card."
                )
                return (
                    bet_or_check,
                    f"{context} Target value calls from {target}. {texture} A smaller value bet is one option if weaker hands would fold to a larger bet.".strip(),
                    bet_size(snap, hero, opponents, 40),
                )
        else:
            weaker = weaker_straights(hole, board, kicks[0])
            target = (
                "weaker straights and sets"
                if weaker
                else "sets and two pair"
            )
        if two_tone:
            target += ", as well as flush draws"
        fraction = 50 if multiway else 65
        reason = "Weaker made hands and draws can call. Betting charges them for the next card. Checking keeps weaker hands in and avoids a raise."
    else:
        return (
            (),
            "Before betting, identify enough weaker hands that fit the line and can still call.",
            None,
        )
    if river and not multiway and fraction == 40:
        return bet_or_check, f"{context} Target value calls from {target}.".strip() + compare_river_sizes, None
    return (
        bet_or_check,
        f"{context} Target value calls from {target}. {reason}".strip(),
        bet_size(snap, hero, opponents, fraction),
    )
