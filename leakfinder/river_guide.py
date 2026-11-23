"""Qualitative river decisions with explicit hand examples and a call price."""

from collections import Counter
from dataclasses import dataclass

from .postflop_value import BetSize, bet_size
from .river_holdings import river_holdings
from .vendor.common import RANK_VALUE


@dataclass(frozen=True)
class RiverAdvice:
    title: str
    choices: tuple[str, ...]
    why: str
    priority: int = 1
    size: BetSize | None = None


def _examples(holdings, count=2):
    return ", ".join(h.text for h in holdings[:count])


def _history(snap, opponent, river_actions):
    """Describe the relevant seat's two streets and prior role, before its river bet."""
    seats = {s.name: s.position for s in snap.seats}
    raises = [a for a in snap.history if a.street == "Preflop" and a.action == "raise"]
    role = []
    if raises:
        role.append(f"The last preflop raiser was {seats[raises[-1].name]}.")
    pressure = checked = 0
    called_raise = False
    for street in ("Flop", "Turn"):
        line = [a for a in snap.history if a.street == street]
        moves = [
            a.action for a in line if a.name == opponent.name and a.action != "return"
        ]
        verb = {"check": "check", "bet": "bet", "raise": "raise", "call": "call"}
        visible = [verb[a] for a in moves if a in verb]
        if visible:
            role.append(f"{opponent.position} {street.lower()} {' / '.join(visible)}.")
        pressure += any(a in {"bet", "raise", "call"} for a in moves)
        raise_at = max(
            (i for i, a in enumerate(line) if a.action == "raise"), default=len(line)
        )
        called_raise |= any(
            a.name == opponent.name and a.action == "call" for a in line[raise_at + 1 :]
        )
        checked += bool(line) and all(a.action == "check" for a in line)
    current = [
        a
        for a in river_actions
        if a.name == opponent.name and a.action in {"bet", "raise", "check"}
    ]
    if current:
        last = current[-1]
        if last.action == "check":
            role.append(f"{opponent.position} checked the river.")
        elif last.action == "bet":
            role.append(
                f"{opponent.position} bet {last.amount / snap.bb:g} BB on the river."
            )
        else:
            total = last.raised_to or last.amount
            role.append(
                f"{opponent.position} raised to {total / snap.bb:g} BB on the river."
            )
    return " ".join(role), pressure, checked, called_raise


def advise_river(snap, hero, opponents):
    result = river_holdings(snap.hero_cards, snap.board)
    opponent_count = len(opponents)
    heads_up = opponent_count == 1
    actions = [a for a in snap.history if a.street == "River"]
    aggression = [a for a in actions if a.action in {"bet", "raise"}]
    faced = aggression[-1] if aggression else None
    opponent = next(
        (s for s in opponents if faced and s.name == faced.name), opponents[0]
    )
    line, pressure, checked, called_raise = _history(snap, opponent, actions)
    own_bet = next((a for a in reversed(aggression) if a.name == hero.name), None)
    if own_bet:
        total = own_bet.raised_to or own_bet.amount
        line = f"You put in {total / snap.bb:g} BB on the river before the opponent responded. {line}"
    street = f"River · {hero.position}" + (
        f" vs {opponent.position}" if heads_up or faced else " · multiway"
    )
    rank = result.hero_category
    stronger, weaker = result.stronger, result.weaker
    less_of_category = tuple(h for h in weaker if h.category == rank)
    value_below = less_of_category + tuple(
        h for h in weaker if max(rank - 1, 1) <= h.category < rank
    )
    missed = (
        *result.missed_flush[:1],
        *result.missed_straight[:1],
        *result.flush_blocker_bluffs[:1],
    )
    suits = Counter(c[1] for c in snap.board)
    ranks = Counter(c[0] for c in snap.board)
    paired = max(ranks.values()) > 1
    flush = max(suits.values()) >= 3
    flush_suit = max(suits, key=suits.get)
    flush_pocket_high = max(
        (RANK_VALUE[c[0]] for c in snap.hero_cards if c[1] == flush_suit), default=0
    )
    flush_top = next(
        r + flush_suit for r in reversed(RANK_VALUE) if r + flush_suit not in snap.board
    )
    flush_block = (
        flush
        and flush_top in snap.hero_cards
        and sum(c[1] == flush_suit for c in snap.hero_cards) == 1
    )
    blocked = (" " + result.blockers[0]) if result.blockers else ""
    candidates_label = " Examples show two-card combinations from legal PLO4 holdings."
    texture = (6, 5, 4, 3, 2, 1, 7, 8, 0) if paired else (5, 4, 3, 2, 1, 6, 7, 8, 0)
    value_above = (
        sorted(
            (h for h in stronger if h.category >= max(rank, 2)),
            key=lambda h: (
                h.category >= 7,
                bool(pressure and h.turn_category < 2),
                texture.index(h.category),
            ),
        )
        or stronger
    )

    def advice(suffix, choices, why, priority=1, size=None):
        return RiverAdvice(
            f"{street} · {suffix}", choices, f"{line} {why}".strip(), priority, size
        )

    if snap.to_call:
        if faced is None:
            return None
        # In a heads-up short all-in the opponent's overhang is returned.
        call_pot = snap.pot - (snap.price_to_match - snap.to_call if heads_up else 0)
        if not heads_up and any(s.all_in for s in opponents):
            return advice(
                "side pot",
                (),
                "Determine which part of the pot you can win before calculating the call price.",
            )
        if call_pot <= 0:
            return None
        price = snap.to_call / (call_pot + snap.to_call)
        raised = len(aggression) > 1 or faced.action == "raise"
        small = price <= 0.22  # at most ~39% pot when facing one opening bet heads-up
        large = price >= 0.28
        price_text = f"Calling costs {snap.to_call / snap.bb:g} BB. The raw threshold is {price:.1%} equity before rake"
        price_text += (
            "." if heads_up else " in the current pot, before the other players act."
        )
        if not heads_up:
            order = sorted(snap.seats, key=lambda seat: seat.seat)
            start = order.index(opponent) + 1
            pending = order[start:] + order[:start]
            behind = [
                seat.position
                for seat in pending[pending.index(hero) + 1 :]
                if seat.live and seat.stack and seat.committed < opponent.committed
            ]
            if behind:
                price_text += f" {' and '.join(behind)} can still act behind you."
        if not stronger:
            return advice(
                "continue",
                ("call", "raise") if snap.can_raise else ("call",),
                f"Your hand wins or ties against every legal holding. {price_text}",
                3,
            )
        made = (
            f"Stronger value hands can include {_examples(value_above)}."
            if value_above
            else ""
        )
        below = (
            f"Examples of weaker made hands include {_examples(value_below)}. Check which of them bet."
            if value_below
            else ""
        )
        split = (
            f"You can tie with {_examples(result.ties, 1)}."
            if heads_up and rank >= 4 and result.ties
            else ""
        )
        bluff = (
            f"Possible bluffs include {_examples(missed, 3)}."
            if missed
            else "None of the reviewed pairs represents a clear missed draw that your hand beats."
        )
        pure = ""
        if heads_up and (raised or rank <= 2):
            pure = f" If you only beat bluffs and ties are impossible, at least {price:.1%} of their bets must be bluffs."
        evidence = (
            " ".join(s for s in (price_text + pure, made, below, split, bluff) if s)
            + blocked
            + candidates_label
        )

        if not heads_up:
            if rank <= 2 and (
                price >= 0.22 or any(a.action == "call" for a in actions)
            ):
                return advice(
                    "multiway fold",
                    ("fold",),
                    evidence
                    + " Fold a weak bluff catcher when multiple players remain.",
                    2,
                )
            return advice(
                "multiway review",
                (),
                evidence
                + " Before calling, consider the player who can still call or raise behind you.",
            )
        if raised:
            if rank <= 3 or (
                rank == 4 and (paired or flush or (pressure and price >= 0.26))
            ):
                return advice(
                    "facing a raise",
                    ("fold",),
                    evidence
                    + " After the earlier aggression, look for a specific reason to expect a bluff raise.",
                    3,
                )
            return advice(
                "facing a raise",
                (),
                evidence
                + " Assess how often stronger combinations actually raise. A list of missed draws offers weak evidence here.",
            )
        strong_flush = (
            rank == 5
            and flush_pocket_high >= 12
            and not paired
            and bool(less_of_category)
        )
        near_top = (
            rank >= 6 and len({h.category for h in stronger}) <= 2
        ) or strong_flush
        if (small and near_top and value_below) or (
            price <= 0.25 and checked == 2 and strong_flush
        ):
            return advice(
                "call a small bet",
                ("call",),
                evidence
                + " Call at this price when the bet can also include weaker value hands.",
                2,
            )
        if (
            small
            and rank in {2, 3, 4}
            and checked >= 1
            and not (paired and rank <= 2)
            and not flush
            and value_below
            and (missed or checked == 2)
        ):
            return advice(
                "call a small bet",
                ("call",),
                evidence
                + " The line allows thinner value bets. Lean toward calling at this price.",
                2,
            )
        if (large and rank <= 2 and (pressure >= 1 or not missed)) or (
            price >= 0.25 and rank == 4 and (flush or paired) and pressure >= 1
        ):
            return advice(
                "fold to a large bet",
                ("fold",),
                evidence
                + " The price and prior action favor folding unless there is a specific reason to expect enough bluffs.",
                2,
            )
        if (
            rank == 3
            and flush
            and called_raise
            and not any(c[1] == flush_suit for c in snap.hero_cards)
        ):
            return advice(
                "fold a set",
                ("fold",),
                evidence
                + " The opponent already called a raise. Fold the set to this river bet without a flush blocker.",
                2,
            )
        if rank <= 1 and not missed and not small:
            return advice(
                "fold",
                ("fold",),
                evidence
                + " This made hand needs a better price or clearer bluff candidates.",
                2,
            )
        return advice(
            "close call or fold",
            ("fold", "call"),
            evidence
            + " The mix of actual bluffs and value bets determines whether to call. Consider how often weaker value hands bet.",
        )

    if not snap.can_raise:
        return None
    checked_to = heads_up and any(
        a.action == "check" and a.name == opponent.name for a in actions
    )
    made_below = tuple(h for h in value_below if h.category >= max(rank - 1, 2))
    blockers = blocked + candidates_label
    if not stronger:
        return None  # Preserve the more specific existing nuts sizing policy.
    if (
        heads_up
        and rank >= 5
        and made_below
        and (rank >= 6 or (flush and flush_pocket_high >= (11 if paired else 12)))
        and (checked_to or checked >= 1)
    ):
        hint = (
            55
            if rank == 6 and pressure and less_of_category
            else (33 if paired else 40)
        )
        worry = f" Stronger combinations such as {_examples(value_above)} can call or raise."
        size = bet_size(snap, hero, opponents, hint)
        choices = ("check", "bet") if paired and rank == 5 else ("bet", "check")
        return advice(
            "value bet or check",
            choices,
            f"Value targets include {_examples(made_below)}.{worry} This size gives weaker hands room to call."
            + blockers,
            2,
            size,
        )
    if (
        heads_up
        and rank == 4
        and checked_to
        and checked
        and not paired
        and not flush
        and less_of_category
    ):
        return advice(
            "thin value with a straight",
            ("bet", "check"),
            f"Target weaker straights such as {_examples(less_of_category)}. Stronger straights such as {_examples(value_above)} can continue. Around one-third pot gives the weaker straights a price to call. Check behind when the preceding action rarely contains them."
            + blockers,
            size=bet_size(snap, hero, opponents, 35),
        )
    if (
        heads_up
        and rank in {2, 3}
        and checked >= 1
        and not flush
        and not paired
        and made_below
    ):
        weak = _examples(made_below)
        return advice(
            "thin value",
            ("check", "bet"),
            f"Checking preserves showdown value. For a thin value bet, target a call from {weak}. Stronger responses include {_examples(value_above)}. A small size leaves more room for weaker calls."
            + blockers,
            size=bet_size(snap, hero, opponents, 28),
        )
    if checked_to and checked and rank <= 1 and flush_block and not paired:
        folds = tuple(h for h in stronger if h.category in {1, 2, 3, 4})
        if folds:
            size = bet_size(snap, hero, opponents, 65)
            breakeven = size.amount / (snap.pot + size.amount)
            return advice(
                "check or bluff with a blocker",
                ("check", "bet"),
                f"{flush_top} blocks the highest ordinary flush. The bluff targets made hands such as {_examples(folds)}. Made flushes can call. With negligible showdown value, this size needs around {breakeven:.0%} folds to break even as a pure bluff."
                + blockers,
                size=size,
            )
    why = (
        f"Weaker hands that can call for value include {_examples(made_below)}."
        if made_below
        else "Credible calls from weaker hands are scarce."
    )
    if value_above:
        why += f" Stronger hands include {_examples(value_above)}."
    if missed:
        why += f" Missed draws such as {_examples(missed)} are more likely to fold than call a value bet."
    if rank >= 4 and made_below:
        return advice(
            "thin value review",
            (),
            why
            + " Check unless you can identify enough weaker calls and a size they will pay."
            + blockers,
        )
    return advice("check", ("check",), why + " Check by default." + blockers)
