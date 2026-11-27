"""Qualitative river decisions using made-hand classes and the recorded line."""

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
    street = f"River · {hero.position}" + (
        f" vs {opponent.position}" if heads_up or faced else " · multiway"
    )
    rank = result.hero_category
    stronger, weaker = result.stronger, result.weaker
    less_of_category = tuple(h for h in weaker if h.category == rank)
    value_below = tuple(h for h in less_of_category if h.category >= 1) + tuple(
        h for h in weaker if max(rank - 1, 1) <= h.category < rank
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
    def advice(suffix, choices, why, priority=1, size=None, rankable=False):
        return RiverAdvice(
            f"{street} · {suffix}",
            choices,
            why,
            priority,
            size,
            rankable,
        )

    study = river_line_review(snap, hero, opponents, result)
    if study:
        action, idea = study
        size = bet_size(snap, hero, opponents, 33) if action == "check" else None
        return advice(idea.title, ("bet", "check") if size else ("call", "fold"),
                      idea.reason, size=size)

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
            note = "Hero can only tie or lose."
            if not heads_up:
                note += " A split requires a tie with every remaining opponent."
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
        if not heads_up:
            return advice("multiway review", (), f"{line} {price_text}")
        removal = _barrel_blockers(snap, opponent, price, rank).strip() if not raised else ""
        if snap.can_raise and result.stronger_pair_count == 1:
            removal = "Only one hole-card pair beats you. A value raise needs calls from worse."
        return advice("facing a raise" if raised else "call or fold review", (), removal)

    if not snap.can_raise:
        return None
    if not weaker:
        ending = (
            "Hero can only tie or lose at showdown."
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
            plan,
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
            "A small value bet can get called by lower straights. Higher straights can raise.",
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
        broadway_board = sum(r in "TJQKA" for r in ranks) >= 4
        bottom_set = rank == 3 and result.hero_kickers[0] == min(RANK_VALUE[r] for r in ranks)
        straights = tuple(h for h in stronger if h.category == 4)
        if broadway_board and bottom_set and straights:
            return advice(
                "bottom set on a four-Broadway board", (),
                "Bottom set can check back on this four-Broadway board. Straights can call a bet or raise.",
            )
        return advice(
            "thin value",
            ("check", "bet"),
            "Check back if weaker made hands will fold. A small bet can get value when they call.",
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
                f"{flush_top} blocks the highest ordinary flush. A bluff targets better hands below a flush. With negligible showdown value, this size needs around {breakeven:.0%} folds to break even as a pure bluff.",
                size=size,
            )
    if rank >= 4 and made_below:
        return advice(
            "thin value review",
            (),
            "A value bet needs calls from weaker made hands.",
        )
    return advice("check or bet review", (), "")
