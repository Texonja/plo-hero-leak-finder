"""Conservative flop and turn comparisons using only the visible action prefix."""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from .postflop_value import STRAIGHTS, BetSize, bet_size, line_context
from .vendor.common import RANK_VALUE


@dataclass(frozen=True)
class EarlyAdvice:
    title: str
    choices: tuple[str, ...]
    why: str
    priority: int = 1
    size: BetSize | None = None


@dataclass(frozen=True)
class Draws:
    straight: int
    flush: int
    both: int
    nuts: int

    @property
    def total(self):
        return self.straight + self.flush - self.both


@lru_cache(maxsize=4096)
def _straight_pairs(board_ranks):
    """A legal three-board-card straight names exactly two missing hole ranks."""
    pairs = {}
    for window in STRAIGHTS:
        high = 5 if window == STRAIGHTS[-1] else max(window)
        for triple in combinations(window.intersection(board_ranks), 3):
            pair = tuple(sorted(window.difference(triple)))
            pairs[pair] = max(high, pairs.get(pair, 0))
    return tuple(pairs.items())


@lru_cache(maxsize=4096)
def next_draws(hole, board):
    """Count physical next cards, with overlapping straight/flush paths counted once.

    For the confirmed-nuts subset, conservatively exclude paired boards, and
    exclude straights on three-flush boards. On a flush completion check the
    highest possible opposing flush and every legal opposing straight flush.
    This is an immediate board property, not an equity estimate.
    """
    occupied = set(hole + board)
    cards_by_suit = {s: {RANK_VALUE[c[0]] for c in hole if c[1] == s} for s in "cdhs"}
    board_by_suit = {s: {RANK_VALUE[c[0]] for c in board if c[1] == s} for s in "cdhs"}
    hr = {RANK_VALUE[c[0]] for c in hole}
    br = frozenset(RANK_VALUE[c[0]] for c in board)
    used = Counter(RANK_VALUE[c[0]] for c in occupied)
    board_paired = len(br) != len(board)
    monotone = any(len(values) >= 3 for values in board_by_suit.values())
    straight = flush = both = nuts = 0
    for rank_label, rank in RANK_VALUE.items():
        if used[rank] == 4:
            continue
        possible = _straight_pairs(br | {rank})
        made = max(
            (high for (a, b), high in possible if a in hr and b in hr), default=0
        )
        safe_board = not board_paired and rank not in br
        best = False
        if made and safe_board:
            best = not any(
                high > made and used[a] + (a == rank) < 4 and used[b] + (b == rank) < 4
                for (a, b), high in possible
            )
        for suit, suited_board in board_by_suit.items():
            if rank_label + suit in occupied:
                continue
            suited_hole = cards_by_suit[suit]
            completes = len(suited_board) == 2
            made_flush = completes and len(suited_hole) >= 2
            straight += bool(made)
            flush += made_flush
            both += bool(made and made_flush)
            if best and not monotone and not completes:
                nuts += 1
            elif made_flush and safe_board:
                suited = suited_board | {rank}
                live = sorted(
                    set(RANK_VALUE.values()) - suited - suited_hole, reverse=True
                )
                if (len(live) < 2 or max(suited_hole) > live[0]) and not any(
                    a in live and b in live
                    for (a, b), _ in _straight_pairs(frozenset(suited))
                ):
                    nuts += 1
    return Draws(straight, flush, both, nuts)


def _line(snap, hero, actions, heads_up):
    """Label a decision only when the required actors and actions occurred."""
    street = snap.street
    pfa = next(
        (
            a.name
            for a in reversed(snap.history)
            if a.street == "Preflop" and a.action == "raise"
        ),
        None,
    )
    wagers = [a for a in actions if a.action in {"bet", "raise"}]
    flop = [
        a
        for a in snap.history
        if a.street == "Flop" and a.action in {"fold", "check", "bet", "call", "raise"}
    ]
    checked = len(flop) == 2 and all(a.action == "check" for a in flop)
    if wagers:
        last = wagers[-1]
        if last.action == "raise":
            checked_before = any(
                a.name == last.name and a.action == "check" for a in actions
            )
            return (
                "facing a check-raise"
                if checked_before and len(wagers) == 2
                else "facing a raise"
            )
        own_check = any(a.name == hero.name and a.action == "check" for a in actions)
        if street == "Flop" and last.name == pfa:
            return "facing a c-bet after checking" if own_check else "facing a c-bet"
        if own_check:
            return "facing a bet after checking"
        if (
            heads_up
            and street == "Turn"
            and checked
            and pfa == hero.name
            and flop[-1].name == pfa
        ):
            return "facing a turn probe"
        return "facing a bet"
    if heads_up and not any(a.action == "fold" for a in actions):
        if street == "Flop" and hero.name == pfa:
            return "c-bet opportunity"
        if (
            street == "Flop"
            and actions
            and actions[-1].name == pfa
            and actions[-1].action == "check"
        ):
            return "stab after a missed c-bet"
        if street == "Turn" and checked:
            if hero.name == pfa:
                return "delayed c-bet after a checked-through flop"
            if not actions and flop[-1].name == pfa:
                return "turn probe after a check-back"
            if actions and actions[-1].name == pfa:
                return "turn stab after a checked-through flop"
        if street == "Turn" and pfa == hero.name:
            bets = [a for a in flop if a.action in {"bet", "raise"}]
            if (
                len(bets) == 1
                and bets[0].name == hero.name
                and flop[-1].action == "call"
                and all(a.action != "fold" for a in flop)
            ):
                return "double barrel after a called c-bet"
    return "opening bet" if heads_up else "multiway pot"


def _draw_note(draws, street):
    if draws.total == 0:
        return "No next card immediately makes a straight or flush for Hero."
    total = draws.total
    unknown = 45 if street == "Flop" else 44
    note = f"Of {unknown} unseen cards, {total} can make a straight or flush on the next street."
    note += (
        f" At least {draws.nuts} make the verified current nuts or a tie."
        if draws.nuts
        else " Zero of those cards are verified to make the nuts here."
    )
    note += (
        " The count is based on the unseen deck before weighting the opponent's range."
    )
    if draws.both:
        note += (
            f" Number of cards that complete both draws {draws.both}. Each counts once."
        )
    if street == "Flop":
        note += " The river can still change the result."
    return note


@lru_cache(maxsize=4096)
def _boat_cards(hole, board):
    """Number of unseen physical cards letting two hole cards make a boat or quads."""
    used = Counter(hole + board)
    hole_pairs = tuple(set(combinations(hole, 2)))
    total = 0
    for rank, count in used.items():
        if count >= 4:
            continue
        triples = set(combinations((*board, rank), 3))
        if any(
            sorted(Counter(h + t).values()) in ([2, 3], [1, 4])
            for h in hole_pairs
            for t in triples
        ):
            total += 4 - count
    return total


def early_advice(snap, hero, opponents, rank, kicks):
    """The caller preserves existing current-nuts/top-set and side-pot handling."""
    if snap.street not in {"Flop", "Turn"} or any(s.all_in for s in (hero, *opponents)):
        return None
    hole, board = tuple(sorted(snap.hero_cards)), tuple(sorted(snap.board))
    heads_up = len(opponents) == 1
    actions = [a for a in snap.history if a.street == snap.street]
    line = _line(snap, hero, actions, heads_up)
    title = f"{snap.street} · {hero.position} · {line}"
    facing = snap.to_call > 0
    raise_faced = facing and line in {"facing a check-raise", "facing a raise"}
    suits = Counter(c[1] for c in board)
    suited = next((s for s, n in suits.items() if n >= 3), None)
    paired = len({c[0] for c in board}) < len(board)
    pocket_suits = [c for c in hole if c[1] == suited]
    blocker = bool(
        len(pocket_suits) == 1
        and RANK_VALUE[pocket_suits[0][0]]
        == max(RANK_VALUE[r] for r in RANK_VALUE if r + suited not in board)
    )
    if not (facing or snap.can_raise) or not opponents:
        return None
    if not heads_up and not (suited and blocker or raise_faced):
        return None
    # Strong made hands have their own value policy. Explain pressure against
    # non-nut hands when a raise actually occurred.
    if rank >= 4:
        if not raise_faced:
            return None
        price = snap.to_call / (snap.pot + snap.to_call)
        return EarlyAdvice(
            title,
            (),
            f"After the raise, calling costs {price:.0%} of the pot after calling. Stronger made hands are possible. Compare the opponent's stronger value hands and semibluffs before building the pot.",
        )
    draws = next_draws(hole, board)
    note = _draw_note(draws, snap.street)
    if rank in (2, 3):
        hand_name = (
            "two pair"
            if rank == 2
            else (
                "a set"
                if sum(RANK_VALUE[c[0]] == kicks[0] for c in hole) == 2
                else "trips"
            )
        )
        boats = _boat_cards(
            tuple(sorted(c[0] for c in hole)), tuple(sorted(c[0] for c in board))
        )
        note = f"You currently hold {hand_name}. {note}"
        if boats:
            note += f" In addition, {boats} cards can make a full house or quads. Some of those hands can still be beaten."
    if draws.total and paired:
        note += " On this paired board, a completed straight or flush can lose to a full house."
    if blocker:
        note += f" {pocket_suits[0]} blocks the highest ordinary flush. You need two hole cards of that suit to make a flush."
    elif suited and len(pocket_suits) <= 1:
        note += " Using this suit for a flush requires two matching hole cards."
    if facing:
        price = snap.to_call / (snap.pot + snap.to_call)
        cost = f"Calling costs {price:.0%} of the pot after calling, before rake."
        checked_before_bet = line.endswith("after checking")
        if raise_faced:
            why = f"{cost} After prior aggression, weigh the opponent's value hands and semibluffs again before raising. {note}"
            if rank == 0 and not blocker and draws.total <= 4 and price >= 0.24:
                return EarlyAdvice(
                    title, ("fold",), why + " Fold high card at this price.", 2
                )
            return EarlyAdvice(title, (), why)
        if (
            heads_up
            and not paired
            and draws.nuts >= 7
            and draws.total >= 11
            and price <= 0.22
        ):
            if checked_before_bet and snap.can_raise:
                return EarlyAdvice(
                    title,
                    ("call", "raise"),
                    f"{cost} {note} Calling keeps the draw in play. A check-raise can work as a semibluff if some better made hands fold.",
                )
            return EarlyAdvice(
                title,
                ("call", "raise") if snap.can_raise else ("call",),
                f"{cost} {note} Calling keeps the price low. A semibluff raise needs some better hands to fold.",
            )
        if checked_before_bet or "probe" in line or draws.total:
            if rank == 0 and not blocker and draws.total <= 4 and price >= 0.28:
                return EarlyAdvice(
                    title,
                    ("fold",),
                    f"{cost} {note} Fold high card when the opponent's continuing range is this narrow.",
                    2,
                )
            why = f"{cost} {note}"
            if checked_before_bet and snap.can_raise:
                why += " For a check-raise, identify worse hands that call for value or better hands that would fold to a semibluff."
            return EarlyAdvice(title, (), why)
        return None

    if (
        snap.street == "Turn"
        and not paired
        and any(
            suits[c[1]] == 2
            and c[0] == "A"
            and sum(other[1] == c[1] for other in hole) >= 2
            for c in hole
        )
        and _straight_pairs(frozenset(RANK_VALUE[c[0]] for c in board))
        and line_context(snap, hero, opponents, actions)[3]
    ):
        # Preserve the focused check after a large called flop bet.
        return None
    if blocker and rank <= 2:
        return EarlyAdvice(
            title,
            ("check", "bet") if heads_up and not paired else (),
            f"{note} Checking controls the pot. A bluff needs a weaker flush or another hand that beats yours to fold. A target size depends on identifying those hands.",
        )
    if heads_up and not paired and draws.nuts >= 7 and draws.total >= 11:
        return EarlyAdvice(
            title,
            ("check", "bet"),
            f"{note} A bet can fold out some better pairs and retain equity when called. The effective stack limits the semibluff size. Checking keeps the pot smaller.",
            size=bet_size(snap, hero, opponents, 50),
        )
    if not heads_up:
        return None
    board_ranks = sorted({RANK_VALUE[c[0]] for c in board}, reverse=True)
    top_two = rank == 2 and not paired and list(kicks[:2]) == board_ranks[:2]
    top_pair = rank == 1 and not paired and kicks[0] >= board_ranks[0]
    straight_on_board = bool(_straight_pairs(frozenset(board_ranks)))
    if top_two and not suited and not straight_on_board:
        return EarlyAdvice(
            title,
            ("bet", "check"),
            "You have top two pair. Lower two pair or a pair with a draw can call. A set is ahead. A moderate value bet gives those weaker hands a price to continue. Checking controls the pot.",
            size=bet_size(snap, hero, opponents, 50),
        )
    if (
        top_pair
        and not suited
        and not straight_on_board
        and line in {"c-bet opportunity", "delayed c-bet after a checked-through flop"}
    ):
        hand = "Overpair" if kicks[0] > board_ranks[0] else "Top pair"
        return EarlyAdvice(
            title,
            ("check", "bet"),
            f"{hand} has showdown value. A small bet can get called by a worse pair with ways to improve. Further aggression makes one pair harder to continue with. {note}",
            size=bet_size(snap, hero, opponents, 33),
        )
    if (
        rank <= 1
        and draws.total <= 4
        and (
            suited
            or line.startswith(("turn probe", "stab after", "turn stab", "delayed"))
        )
    ):
        return EarlyAdvice(
            title,
            ("check",),
            f"{note} Check with so few immediate ways to make a straight or flush. Taking the initiative alone offers little reason to bluff.",
        )
    if draws.total and line != "opening bet":
        return EarlyAdvice(
            title,
            (),
            f"{note} A semibluff bet depends on which hands the opponent folds. Checking preserves the price of the next card. Sizing depends on an estimate of that range.",
        )
    return None
