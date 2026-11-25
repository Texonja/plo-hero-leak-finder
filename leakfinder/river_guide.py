"""Qualitative river decisions with explicit hand examples and a call price."""

from collections import Counter
from dataclasses import dataclass

from .postflop_value import BetSize, bet_size
from .river_critique import river_line_review
from .river_holdings import river_holdings
from .vendor.common import RANK_VALUE


@dataclass(frozen=True)
class RiverAdvice:
    title: str
    choices: tuple[str, ...]
    why: str
    priority: int = 1
    size: BetSize | None = None
    rankable: bool = False


def _examples(holdings, count=2):
    return ", ".join(h.text for h in holdings[:count])


def _barrel_blockers(snap, bettor, price, rank):
    """Price and removal effects for a bluff-catcher facing successive opening bets."""
    if rank > 2 or price < 0.25:
        return ""
    running = snap.pot
    sized = {}
    for action in reversed(snap.history):
        running -= -action.amount if action.action == "return" else action.amount
        if action.street in {"Flop", "Turn", "River"} and action.action in {"bet", "raise"}:
            sized.setdefault(action.street, []).append((action, running))
    barrels = []
    for street in ("River", "Turn", "Flop"):
        actions = sized.get(street, ())
        if len(actions) != 1:
            break
        action, pot = actions[0]
        if action.name != bettor.name or action.action != "bet" or pot <= 0:
            break
        barrels.append(f"{street.lower()} {action.amount / pot:.0%} pot")
    if len(barrels) < 2:
        return ""
    missed = []
    for suit in "cdhs":
        held = [c for c in snap.hero_cards if c[1] == suit]
        if (len(held) >= 2 and sum(c[1] == suit for c in snap.board[:4]) == 2
                and snap.board[-1][1] != suit):
            missed.append(" ".join(held))
    if not missed:
        return ""
    note = (f" {bettor.position}'s bets were {', '.join(reversed(barrels))}. "
            f"Your {' and '.join(missed)} remove cards from missed flush draws, leaving fewer natural bluffs.")
    if rank == 0:
        note += " With high card you can even lose to a bluff. Folding is reasonable without enough bets from hands you actually beat."
    else:
        note += " Folding is reasonable if this line contains too few weaker bets to meet the call price."
    return note


def _history(snap, opponents, river_actions):
    """Summarize public actions through the decision and identify players left to act."""
    seats = {s.name: s.position for s in snap.seats}
    opponent_names = {s.name for s in opponents}
    raises = [a for a in snap.history if a.street == "Preflop" and a.action == "raise"]
    role = []
    if raises:
        role.append(f"The last preflop raiser was {seats[raises[-1].name]}.")
    pressure = checked = 0
    for street in ("Flop", "Turn"):
        actions = [
            a
            for a in snap.history
            if a.street == street
            and a.action in {"check", "bet", "call", "raise", "fold"}
        ]
        theirs = [a for a in actions if a.name in opponent_names]
        if len(opponents) > 1 and actions:
            role.append(
                street
                + " · "
                + ", ".join(f"{seats[a.name]} {a.action}" for a in actions)
                + "."
            )
        elif theirs:
            role.append(
                f"{opponents[0].position} {street.lower()} "
                + " / ".join(a.action for a in theirs)
                + "."
            )
        pressure += any(a.action in {"bet", "raise", "call"} for a in theirs)
        checked += bool(actions) and all(a.action == "check" for a in actions)
    for action in river_actions:
        if action.name not in opponent_names:
            continue
        pos = seats[action.name]
        if action.action == "check":
            role.append(f"{pos} checked the river.")
        elif action.action in {"bet", "call"}:
            verb = "bet" if action.action == "bet" else "called"
            role.append(f"{pos} {verb} {action.amount / snap.bb:g} BB on the river.")
        elif action.action == "raise":
            total = action.raised_to or action.amount
            role.append(f"{pos} raised to {total / snap.bb:g} BB on the river.")
    if len(opponents) > 1 and not snap.to_call:
        acted = {a.name for a in river_actions}
        pending = [s.position for s in opponents if s.name not in acted and s.stack]
        if pending:
            role.append(
                "If you check, " + " and ".join(pending) + " can still act after you."
            )
        else:
            role.append("Checking back closes the action.")
    return " ".join(role), pressure, checked


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
    line, pressure, checked = _history(snap, opponents, actions)
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
    value_below = tuple(h for h in less_of_category if h.category >= 1) + tuple(
        h for h in weaker if max(rank - 1, 1) <= h.category < rank
    )
    missed_groups = (result.missed_flush, result.missed_straight)
    shown_made = {frozenset(h.holding) for h in value_below[:2]} if snap.to_call else set()
    overlap = any(frozenset(h.holding) in shown_made for group in missed_groups for h in group[:1])
    missed = []
    used = set(shown_made)
    for group in missed_groups:
        for h in group:
            key = frozenset(h.holding)
            if key not in used:
                missed.append(h)
                used.add(key)
                break
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
    candidates_label = " Each example is a legal four-card holding. Its frequency in this line is unknown."
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

    def advice(suffix, choices, why, priority=1, size=None, rankable=False):
        return RiverAdvice(
            f"{street} · {suffix}",
            choices,
            f"{line} {why}".strip(),
            priority,
            size,
            rankable,
        )

    study = river_line_review(snap, hero, opponents, result)
    if study:
        action, idea = study
        size = bet_size(snap, hero, opponents, 33) if action == "check" else None
        return advice(idea.title, ("bet", "check") if size else ("call", "fold"),
                      f"{idea.reason} {idea.counterargument}", size=size)

    if snap.to_call:
        if faced is None:
            return None
        # In a heads-up short all-in the opponent's overhang is returned.
        call_pot = snap.pot - (snap.price_to_match - snap.to_call if heads_up else 0)
        short_call = snap.price_to_match > snap.to_call
        if (not heads_up and (short_call or any(s.all_in for s in opponents))) or (
            short_call and any(a.action == "fold" for a in actions)
        ):
            return advice(
                "side pot",
                (),
                "Determine which part of the pot you can win before calculating the call price.",
            )
        if call_pot <= 0:
            return None
        price = snap.to_call / (call_pot + snap.to_call)
        raised = faced.action == "raise"
        price_text = f"Calling costs {snap.to_call / snap.bb:g} BB. The raw threshold is {price:.1%} equity before rake"
        price_text += "."
        behind = []
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
                price_text += f" This uses the current pot before the other players act. {' and '.join(behind)} can still act behind you."
            else:
                price_text += " Your call closes the betting."
        if not stronger:
            return advice(
                "continue",
                ("call", "raise") if snap.can_raise else ("call",),
                f"Your hand wins or ties against every legal holding. {price_text}",
                3,
                rankable=True,
            )
        if not weaker:
            if not result.ties:
                note = "Every legal four-card holding beats Hero. Calling cannot recover any chips."
                if snap.can_raise:
                    note += " A bluff raise needs enough folds to recover the extra chips at risk."
                return advice("no showdown equity", () if snap.can_raise else ("fold",),
                              f"{price_text} {note}", 3, rankable=not snap.can_raise)
            note = f"Hero can only tie or lose. One legal holding that ties is {_examples(result.ties, 1)}."
            if not heads_up:
                note += " For a share, all opponents at showdown need simultaneously possible tying hands. The example checks one opponent."
            if heads_up or not behind:
                players = opponent_count + 1
                split_price = players * price
                if split_price >= 1:
                    note += " Even an awarded split every time cannot make a call profitable before rake."
                else:
                    note += f" Breaking even before rake requires a {players}-way split at least {split_price:.1%} of the time."
            else:
                note += " Later actions can change the pot and the number sharing it."
            if snap.can_raise:
                note += " Raising would depend on getting better hands to fold."
            return advice("split or lose", (), f"{price_text} {note}")
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
            else "" if overlap else "No additional missed-draw bluff was identified among the examples."
        )
        if overlap:
            bluff += " A weaker made hand shown above can also bluff after missing a draw. Count that holding once."
        blocker_example = next((h for h in result.flush_blocker_bluffs if frozenset(h.holding) not in used), None)
        if blocker_example:
            bluff += f" A separate possible bluff uses a flush blocker: {blocker_example.text}."
        pure = ""
        if heads_up and (raised or rank <= 2):
            pure = f" If you only beat bluffs and ties are impossible, at least {price:.1%} of their bets must be bluffs."
        evidence = (
            " ".join(s for s in (price_text + pure, made, below, split, bluff) if s)
            + blocked
            + candidates_label
        )

        if not heads_up:
            return advice(
                "multiway review",
                (),
                evidence
                + " More than one range can win this pot. Estimate your equity against all live players.",
            )
        comparison = (
            f"If there are no ties, calling gains over folding when Hero wins more than {price:.1%} of the time. "
            "Count weaker value bets as wins and each tie as half a win. Folding gains over calling below that threshold."
        )
        if raised:
            comparison += " Estimate those shares among raises specifically. An opponent who bluffs opening bets can still raise only for value."
        elif pressure:
            comparison += " The earlier bet or call helps identify plausible hands. Their betting frequencies remain unknown."
            comparison += _barrel_blockers(snap, opponent, price, rank)
        if snap.can_raise and result.stronger_pair_count == 1:
            pair = " ".join(stronger[0].cards)
            comparison += (
                f" Only one hole-card pair beats you: {pair}. A stronger four-card holding must contain both cards. "
                "Consider a value raise if worse hands pay. Calling can save chips if only better hands continue. "
                "The pair count gives no betting frequency."
            )
        elif snap.can_raise:
            comparison += " A raise also depends on which better hands fold and which worse hands continue."
        return advice(
            "facing a raise" if raised else "call or fold review",
            (),
            evidence + " " + comparison,
        )

    if not snap.can_raise:
        return None
    if not weaker:
        ending = (
            f"Hero can only tie or lose at showdown. One holding that ties is {_examples(result.ties, 1)}."
            if result.ties else "Every legal four-card holding beats Hero at showdown."
        )
        if not heads_up and result.ties:
            ending += " A split requires simultaneous ties with all opponents still contesting the pot."
        ending += " A value bet has no weaker caller."
        if heads_up and result.ties and any(a.name == opponent.name and a.action == "check" for a in actions):
            ending += (" Checking back keeps half the pot against a tie. Compared with checking, "
                       "folding out a better hand gains the full pot and folding out a tying hand gains half. "
                       "A call from a better hand costs the bet. Check back unless those folds justify the risk.")
        else:
            ending += " A bluff needs enough folds to justify the risk."
        return advice("bluff or check", (), ending)
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
        size = None if hint in (40, 55) else bet_size(snap, hero, opponents, hint)
        choices = ("check", "bet") if paired and rank == 5 else ("bet", "check")
        plan = "This size gives weaker hands room to call."
        if rank == 6 and pressure:
            plan = ("Bet for value against weaker boats and other made hands that can call. "
                    + ("Checking back gives up a last chance to collect from them." if checked_to else
                       "Checking to induce needs weaker hands that will actually bet."))
        if size is None:
            plan = ("Compare checking with a small to larger value bet. A small bet can keep weaker calls in. "
                    "A larger bet gains more if those hands keep calling.")
        return advice(
            "value bet or check",
            choices,
            f"Value targets include {_examples(made_below)}.{worry} {plan}"
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
        broadway_board = sum(r in "TJQKA" for r in ranks) >= 4
        bottom_set = rank == 3 and result.hero_kickers[0] == min(RANK_VALUE[r] for r in ranks)
        straights = tuple(h for h in stronger if h.category == 4)
        if broadway_board and bottom_set and straights:
            return advice(
                "bottom set on a four-Broadway board", (),
                f"Checking keeps the showdown value of bottom set. A holding such as {_examples(straights, 1)} "
                f"already has a straight. To bet for value, identify enough weaker calls such as {weak}. "
                "A checked street alone does not tell you how often those hands will call.",
            )
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
            breakeven = size.contestable / (snap.pot + size.contestable)
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
    blocker_example = next((h for h in result.flush_blocker_bluffs if frozenset(h.holding) not in used), None)
    if blocker_example:
        why += f" Another possible bluff uses a flush blocker, for example {blocker_example.text}."
    if rank >= 4 and made_below:
        return advice(
            "thin value review",
            (),
            why
            + " Check unless you can identify enough weaker calls and a size they will pay."
            + blockers,
        )
    return advice(
        "check or bet review",
        (),
        why
        + " Checking keeps your showdown value. A value bet needs enough weaker calls, and a bluff needs better hands that fold."
        + blockers,
    )
