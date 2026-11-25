"""Local, authored advice from cards and actions available before a PLO4 decision."""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from . import preflop_guide
from .early_postflop import early_advice, highest_flush_draw
from .importers import STREETS
from .opponent_history import heads_up_price
from .postflop_value import (
    STRAIGHTS,
    BetSize,
    bet_size,
    line_context,
    paired_targets,
    value_plan,
    weaker_straights,
)
from .preflop_spots import (
    POSTFLOP_ORDER,
    opening_comparison,
    opening_edges,
    stack_context,
    threebet_price_note,
)
from .river_guide import advise_river
from .river_holdings import nuts
from .sb_reference import reference_choices, sb_reference
from .statistics import tags
from .vendor.common import RANK_VALUE
from .vendor.primitives import best_plo_hand, classify_known_hand

LABELS = {
    "fold": "fold",
    "call": "call",
    "raise": "raise",
    "bet": "bet",
    "check": "check",
}
FORCED = {"posts_ante", "posts_sb", "posts_bb"}


@dataclass(frozen=True)
class Advice:
    title: str
    choices: tuple[str, ...]
    why: str
    raise_label: str = "raise"
    priority: int = 1
    size: BetSize | None = None
    rankable: bool = True

    @property
    def play(self):
        labels = LABELS | {"raise": self.raise_label}
        if self.size:
            labels["bet"] = self.size.label
        return " or ".join(labels[a] for a in self.choices)

    def difference(self, action, amount):
        if not self.choices or not self.rankable:
            return None
        if action not in self.choices:
            return "action"
        return None

    def can_compare(self, snap):
        """An action comparison needs at least one legal alternative outside the guide."""
        if not self.rankable or not self.choices:
            return False
        legal = {"fold", "call"} if snap.to_call else {"check"}
        if snap.can_raise:
            legal.add("raise" if snap.to_call or snap.street == "Preflop" else "bet")
        return bool(legal.difference(self.choices))


def preflop_class(cards, open_bb):
    """Prefer concrete overlapping strengths over a scalar hand ranking."""
    group = preflop_guide.hand_group(cards, open_bb)
    if group != "review":
        return group
    counts = Counter(c[0] for c in cards)
    suits = Counter(c[1] for c in cards)
    flags = classify_known_hand(cards)
    straight_window = max(flags["connected_window"], sum(r in counts for r in "A2345"))
    ace_suit = any(c[0] == "A" and suits[c[1]] >= 2 for c in cards)
    if counts["A"] == 2:
        return "other_aces"
    if max(counts.values()) == 4:
        return "quads"
    if max(counts.values()) == 3:
        return "trips"
    if flags["pairs"] == 2:
        return "double_pair"
    if ace_suit and flags["broadways"] >= 3:
        return "suited_broadway"
    if flags["double_suited"] and straight_window >= 3:
        return "connected_ds"
    if flags["pairs"] and len(suits) == 4:
        return "rainbow_pair"
    if len(suits) == 4 and flags["broadways"] < 2 and straight_window < 3:
        return "rainbow"
    return "review"


PREMIUM = {"aces", "other_aces"}
STRUCTURE_ONLY = {"connected_ds", "suited_broadway"}
WEAK = {"weak_suit", "rainbow"}
PREFLOP_CLASSES = {
    "double_pair": "Two starting pairs offer two ranks on which to make a set.",
    "rainbow_pair": "One starting pair and four different suits. Hero cannot make a flush.",
    "quads": "All four hole cards share a rank. Only two can play.",
    "other_aces": "Aces are the highest starting pair. Weak side cards reduce flexibility as the pot grows.",
    "trips": "A third card of the same rank takes up a hole-card slot and removes an out to a set.",
    "suited_broadway": "A suited ace and at least three high cards give you more ways to continue on high boards.",
    "connected_ds": "Two cards in each suit and at least three distinct ranks within one five-rank straight window.",
    "review": "The price, position and action are more informative than a general rule for this hand structure.",
}


@lru_cache(maxsize=1820)
def straight_support(ranks):
    """Straight windows use distinct hole ranks and allow the ace at either end."""
    present = set(ranks)
    links = [present & set(w) for w in STRAIGHTS]
    best = max(map(len, links))
    names = {v: r for r, v in RANK_VALUE.items()}
    parts = []
    if best >= 3:
        group = max(sorted(w, reverse=True) for w in links if len(w) == best)
        parts.append(
            "Ranks sharing a straight window: "
            + "-".join(names[v] for v in group)
            + "."
        )
    isolated = [
        n for n in sorted(present) if not any(n in w and len(w) >= 2 for w in links)
    ]
    if isolated and len(present) > 1:
        parts.append(
            "No straight link to another hole rank: "
            + ", ".join(names[v] for v in isolated)
            + "."
        )
    return " ".join(parts)


def preflop_advice(snap, hero, seats):
    if snap.board:
        return None
    voluntary = [a for a in snap.history if a.action not in FORCED]
    raises = [a for a in voluntary if a.action == "raise"]
    calls = [a for a in voluntary if a.action == "call"]
    faced = raises[-1] if raises else None
    if faced and (not faced.raised_to or faced.name not in {s.name for s in seats}):
        return None
    size = faced.raised_to / snap.bb if faced else 0
    hand = preflop_class(snap.hero_cards, size if len(raises) == 1 else None)
    by_name = {s.name: s for s in seats}
    own = [a.action for a in voluntary if a.name == hero.name]
    pos = hero.position
    in_blind = pos in {"SB", "BB"}
    pot_odds = snap.to_call / (snap.pot + snap.to_call)
    pos_label = f"{pos} vs {by_name[faced.name].position}" if faced else pos
    explanation = PREFLOP_CLASSES.get(hand) or preflop_guide.GROUPS[hand].title + "."
    ranks = tuple(sorted({RANK_VALUE[c[0]] for c in snap.hero_cards}))
    if hand == "double_pair":
        link = (
            "The two ranks can work together in a straight."
            if any(set(ranks) <= window for window in STRAIGHTS)
            else "The two ranks cannot make a straight together."
        )
        # Four cards match the pairs, so the unconditional chance of seeing
        # at least one of those ranks on the flop is 1 - C(44,3)/C(48,3).
        explanation += (
            f" {link} Before assigning opponent cards, 23.4% of flops contain at least "
            "one of the four remaining cards matching either pair. A lower set can lose to a higher set."
        )
    # The statistical spread tag can coexist with three highly connected ranks.
    # Describe the actual straight links for an individual decision instead.
    labels = [tag for tag in tags(snap.hero_cards) if tag != "Disconnected"]
    explanation = " ".join(
        filter(
            None,
            (
                " · ".join(labels) + ".",
                straight_support(ranks),
                explanation,
            ),
        )
    )
    aggression = {0: "raise", 1: "3-bet", 2: "4-bet"}.get(
        len(raises), f"{len(raises) + 2}-bet"
    )

    def advice(spot, choices, why, priority=1):
        legal = tuple(
            a
            for a in choices
            if (a != "raise" or snap.can_raise)
            and (a not in {"fold", "call"} or snap.to_call > 0)
            and (a != "check" or snap.to_call == 0)
        )
        # A missing raise option changes strategic context. Keep these cases for study.
        if choices and legal != choices:
            return Advice(
                f"{spot} · {pos_label}",
                (),
                "Check which players are still in the hand and which actions are legal.",
            )
        return Advice(f"{spot} · {pos_label}", legal, why, aggression, priority)

    if any(s.all_in for s in seats) or snap.to_call >= hero.stack:
        return advice(
            "Short stack or all-in",
            (),
            "Check the price, effective stack and possible side pots. The opening guidelines assume enough stack depth to play.",
        )
    if not 3 <= len(snap.seats) <= 6:
        return advice(
            "Table size review",
            (),
            explanation
            + " The action guide covers three to six dealt-in players. Heads-up and full-ring ranges need separate review.",
        )
    depth, depth_note = stack_context(snap, hero, seats, raises + calls)
    if (depth in {"outside", "mixed"} and hand == "double_pair" and len(raises) == 2
            and raises[0].name == hero.name and raises[1].name != hero.name and snap.to_call):
        opponent = by_name[faced.name]
        remaining = min(hero.stack - snap.to_call, opponent.stack)
        other = [s.position for s in seats if s.live and s.name not in {hero.name, opponent.name}]
        where = (f" Other live players: {', '.join(other)}." if other else
                 f" You would be {'in' if POSTFLOP_ORDER.index(pos) > POSTFLOP_ORDER.index(opponent.position) else 'out of'} position.")
        return advice(
            "Double-paired 3-bet defense", (),
            f"{explanation} {opponent.position} 3-bet to {size:g} BB. Calling costs "
            f"{snap.to_call / snap.bb:g} BB into {snap.pot / snap.bb:g} BB ({pot_odds:.1%} "
            f"of the pot after your call), leaving {remaining / snap.bb:g} BB effective "
            f"against the raiser.{where} A low set can face a higher set. Compare implied "
            f"odds with the risk of losing a larger pot. {depth_note}",
        )
    if depth == "outside":
        return advice(
            "Effective stack review",
            (),
            explanation + " " + depth_note,
        )
    antes = [a for a in snap.history if a.action == "posts_ante"]
    if antes and (
        len(antes) != len(snap.seats)
        or len({a.name for a in antes}) != len(antes)
        or len({a.amount for a in antes}) != 1
        or 100 * antes[0].amount > 16 * snap.bb
    ):
        return advice(
            "Ante structure review",
            (),
            explanation
            + " These antes change the price of entering the pot beyond the guide's scope.",
        )
    reference = sb_reference(snap)
    if reference is not None:
        fold, _ = reference
        choices = reference_choices(fold)
        note = f"The six-max 100 BB reference folds {fold:.1%} for this exact hand class. Continuing includes both completing and raising. The model assumes 5% rake capped at 1 BB. Actual rake and blind play can change the decision."
        if not choices:
            note += " The model mixes folding and continuing. Review both lines."
        return advice("SB first-in solver reference", choices, note)

    note = " " + depth_note if depth_note else ""
    if not calls and not raises and pos in {"UTG", "HJ", "CO", "BTN"}:
        comparison = opening_comparison(
            snap.hero_cards, pos, depth == "mixed", hand in PREMIUM
        )
        if comparison:
            choices, reason = comparison
            return advice("First in", choices, reason + note)
    if depth == "mixed":
        return advice(
            "Mixed stack depths",
            (),
            explanation
            + note
            + " The ordinary-depth guide does not compare marginal lines here.",
        )
    if (
        len(raises) == 1
        and not calls
        and not own
        and 2 <= size <= 3.5
        and pos in {"CO", "BTN"}
        and hand not in PREMIUM | WEAK | {"highrun"}
        and not (
            len(snap.seats) == 6
            and pos == "BTN"
            and by_name[faced.name].position == "CO"
        )
    ):
        strong, weak = opening_edges(snap.hero_cards, pos)
        if strong or weak:
            line = (
                "Compare calling with a 3-bet"
                if strong
                else "Compare folding with defending"
            )
            return advice(
                "Facing an open",
                (),
                f"{strong or weak}. {by_name[faced.name].position} opened to {size:g} BB. Calling costs {pot_odds:.1%} of the pot after calling. {line} against this opener's range, accounting for rake, future betting and players behind.",
            )
    if (
        not raises
        and not calls
        and pos not in {"SB", "BB"}
        and hand in {"suited_broadway", "run"}
        and "Double suited" in labels
        and len(ranks) == 4
        and ranks[0] >= 9
        and ranks[-1] == 14
        and (ranks[2] - ranks[0] == 2 or ranks[3] - ranks[1] == 2)
    ):
        return advice(
            "First in",
            (),
            explanation
            + " Raising is a reasonable starting point with a nut suit, a second suit and three consecutive high ranks. Frequent 3-bets behind or high rake reduce the appeal of marginal opens.",
        )
    if hand == "trips" and len(raises) == 1 and calls and not own and pos == "SB":
        trip_rank = Counter(RANK_VALUE[c[0]] for c in snap.hero_cards).most_common(1)[
            0
        ][0]
        if (
            trip_rank <= 6
            and ranks[-1] <= 10
            and ranks[-1] - ranks[0] >= 5
            and pot_odds >= 0.30
        ):
            return advice(
                "Facing a raise with callers or limpers",
                (),
                explanation
                + " Folding is a reasonable starting point. Only one card of the tripled rank remains, the two ranks cannot make a straight together and the SB faces a substantial price out of position. A counter-bluff needs specific evidence that the opponents will fold.",
            )
    if hand == "double_pair":
        price_note = threebet_price_note(snap)
        if price_note:
            return advice(
                "Double-paired 3-bet defense",
                (),
                f"{explanation} {price_note} A call can benefit from both set ranks. "
                "Compare how much equity you realize against overpairs, higher sets and draws.",
            )
        if not raises and not calls:
            return advice(
                "Double-paired opening review",
                (),
                f"{explanation} Opening can win the blinds or build a pot when you hit. "
                "A frequent 3-bet behind or high rake makes a marginal open less attractive.",
            )
        return advice("Double-paired hand review", (), explanation)
    if hand in {"rainbow_pair", "quads", "trips"}:
        if hand == "rainbow_pair" and pos == "BTN" and not raises and not calls:
            return advice(
                "Paired button opening review",
                (),
                explanation + " Position favors opening. Weigh the side-card connections, "
                "the risk of a higher set and how often the blinds 3-bet or fold.",
            )
        return advice(
            "Hand structure review",
            (),
            explanation + " No validated action rule for this group.",
        )
    if hand in STRUCTURE_ONLY:
        suits = Counter(c[1] for c in snap.hero_cards)
        tops = [
            max(RANK_VALUE[r] for r, s in snap.hero_cards if s == suit)
            for suit, count in suits.items()
            if count >= 2
        ]
        if hand == "connected_ds" and len(ranks) == 3 and max(ranks) <= 9:
            note = "The small pair uses a slot that could connect another rank. A low set or flush can be dominated."
            if snap.to_call:
                note += " Folding is reasonable."
        elif max(tops) <= 9:
            note = "Both suits have low top cards, so higher flushes are a concern."
            if snap.to_call:
                note += " Folding is reasonable."
        elif hand == "connected_ds" and max(ranks) >= 12 and ranks[-2] <= 7:
            if ranks[-1] == 14 and ranks[0] <= 5:
                note = (
                    "The ace and a low card can play together in a wheel (A-2-3-4-5). "
                    "One ace-high suit can make the nut flush. Low-end straights and "
                    "the other flush suit can be dominated."
                )
            else:
                note = "The high card has little straight support from the low cards."
                if snap.to_call:
                    note += " Folding is reasonable."
        elif max(suits.values()) == 3:
            note = "The suited ace can make the nut flush, but the third card of its suit removes a flush out."
        elif faced and "A" not in {c[0] for c in snap.hero_cards}:
            note = f"Neither suit can use an ace from your hand. Weigh the {by_name[faced.name].position} raising range, the players behind and the rake before continuing."
        elif faced:
            note = f"{by_name[faced.name].position} raised. Weigh the price of calling, which hands might continue against a reraise and who can still act."
        elif calls:
            note = "Consider the cost of seeing the flop and which limpers would continue against an isolation raise."
        else:
            note = "The suits and straight potential give this hand some playability. Opening also depends on the players behind, stack depth and rake."
        if calls and not raises and not own and snap.to_call:
            limpers = ", ".join(by_name[a.name].position for a in calls)
            acted = {a.name for a in voluntary}
            pending = [s.position for s in seats if s.live and s.name != hero.name and s.name not in acted]
            note += (f" After the limp from {limpers}, overlimping costs {snap.to_call / snap.bb:g} BB "
                     f"into {snap.pot / snap.bb:g} BB.")
            if pending:
                note += f" {', '.join(pending)} can still act."
            if 14 in tops and len(tops) == 2 and ranks[0] <= 5 and ranks[-2] <= 7:
                note += (" Overlimping is reasonable with a nut suit at this price. "
                         "An isolation raise gains appeal if it gets heads-up. Frequent raises behind reduce the value of limping.")
        if faced and snap.to_call:
            note += f" Calling costs {snap.to_call / snap.bb:g} BB, or {pot_odds:.1%} of the pot after calling, before rake and later betting. Equity against the opponent's range is unknown."
        return advice("Hand structure review", (), explanation + " " + note)
    if not raises and not calls:
        if hand in PREMIUM or (hand == "highrun" and pos in {"CO", "BTN"}):
            if pos == "SB" and hand == "other_aces":
                return advice(
                    "First in from the small blind",
                    (),
                    explanation + " Completing or raising depends on how BB responds.",
                )
            return advice(
                "First in",
                ("raise",),
                explanation + " Open for value and initiative.",
            )
        if hand in WEAK:
            if pos in {"SB", "BTN", "CO"}:
                return advice(
                    "First in",
                    (),
                    explanation
                    + " A steal also depends on how often the remaining players fold or reraise.",
                )
            return advice(
                "First in",
                ("fold",),
                explanation
                + " Open with a stronger combination when more players are still to act.",
            )
        if pos != "SB" and hand == "kings":
            return advice(
                "First in",
                (),
                explanation
                + " An open-raise can isolate and build a pot with kings. A limp may induce "
                "a raise but can invite several players into the pot. Folding can save "
                "chips when reraises and rake make these side cards hard to play.",
            )
        return advice(
            "First in",
            (),
            explanation
            + " Consider the ranks and suits together, who can still raise, and whether Hero will have position.",
        )
    if not raises:
        if hand == "aces":
            return advice(
                "Facing limpers",
                ("raise",),
                explanation + " Raise for value against weaker hands that can call.",
            )
        if not snap.to_call:
            return advice(
                "Big blind option after limps",
                (),
                explanation
                + " Checking takes a free flop. An isolation raise depends on the limpers' ranges and how they respond.",
            )
        return advice(
            "Facing limpers",
            (),
            explanation
            + " The price and the number of limpers determine whether isolating or calling is worthwhile.",
        )
    if len(raises) == 1 and not calls and not own:
        if not 2 <= size <= 3.5:
            return advice(
                "Opening size review",
                (),
                explanation
                + f" The open is {size:g} BB. The single-open action rules cover 2–3.5 BB.",
            )
        legacy = preflop_guide.at_decision(snap)
        if legacy:
            actions = {"fold": "fold", "call": "call", "3-bet": "raise"}
            choices = (
                ()
                if legacy.play == "Manual review"
                else tuple(actions[a] for a in legacy.play.lower().split(" or "))
            )
            return advice(
                "Facing an open",
                choices,
                explanation + " " + legacy.why,
            )
        if (
            len(snap.seats) == 6
            and pos == "BTN"
            and by_name[faced.name].position == "CO"
        ):
            return advice(
                "Facing a CO open on BTN",
                (),
                explanation
                + " This prefix is outside the six-handed BTN-versus-CO pilot's price, depth or blind conditions.",
            )
        if hand in PREMIUM:
            return advice(
                "Facing an open",
                ("raise",) if hand == "aces" else ("raise", "call"),
                explanation
                + " Aces often benefit from building the pot against a single opener.",
            )
        if hand in WEAK and not in_blind:
            return advice(
                "Facing an open",
                ("fold",),
                explanation
                + " Consider the price of defending and the risk of domination against this open.",
            )
        opener = by_name[faced.name].position
        return advice(
            "Defending a blind" if in_blind else "Facing an open",
            (),
            explanation
            + f" {opener} opened to {size:g} BB. Calling costs {snap.to_call / snap.bb:g} BB, or {pot_odds:.1%} of the pot after calling. Compare that price with equity realization against this opener, rake and the players behind.",
        )
    if len(raises) == 1:
        after_open = voluntary[voluntary.index(faced) + 1 :]
        cold_calls = [a for a in after_open if a.action == "call"]
        if (
            hand == "rainbow"
            and ranks[-1] <= 12
            and pos == "SB"
            and not own
            and len(cold_calls) == len(calls) == 1
            and pot_odds >= 0.25
        ):
            return advice(
                "Open with a cold-caller",
                (),
                explanation
                + " Folding is a reasonable starting point against an open and a call. The SB will play out of position with limited straight coverage and no flush potential. A squeeze bluff would require enough folds from both opponents.",
            )
        if (
            hand == "aces"
            and not own
            and cold_calls
            and len(cold_calls) == len(calls)
            and 2 <= size <= 3.5
        ):
            return advice(
                "Squeeze or isolate after a call",
                ("raise",),
                explanation
                + " Raise for value and reduce the chance of a large multiway flop.",
            )
        return advice(
            "Open with callers",
            (),
            explanation + " Check who called or limped and who can still raise.",
        )

    if len(raises) == 2:
        spot = "Facing a 3-bet after opening" if "raise" in own else "Facing a 3-bet"
    else:
        spot = f"Facing a {len(raises) + 1}-bet"
    extra = [a for a in voluntary if a.action == "call" and a.name != hero.name]
    raiser = by_name[faced.name]
    effective_vs_raiser = min(
        hero.stack + hero.committed, raiser.stack + raiser.committed
    )
    max_raise_to = hero.committed + snap.to_call + snap.pot + snap.to_call
    simple_three_bet = (
        len(raises) == 2 and own == ["raise"] and not extra and len(seats) == 2
    )
    if (
        simple_three_bet
        and hand == "aces"
        and 45 <= effective_vs_raiser / snap.bb <= 110
        and 3 * max_raise_to >= effective_vs_raiser
        and snap.can_raise
    ):
        return advice(
            spot,
            ("raise",),
            explanation
            + " A pot-sized value 4-bet can commit at least a third of the effective stack against this single 3-bettor.",
            2,
        )
    if simple_three_bet and hand in PREMIUM:
        return advice(
            spot,
            ("call", "raise") if snap.can_raise else ("call",),
            explanation
            + " Choose between calling and reraising based on pot size, side cards and the effective stack.",
        )
    if simple_three_bet and hand in WEAK and pot_odds >= 0.30:
        return advice(
            spot,
            ("fold",),
            explanation
            + " This price calls for a hand that retains value against a stronger range.",
        )
    return advice(
        spot,
        (),
        explanation
        + f" Calling costs {snap.to_call / snap.bb:g} BB ({pot_odds:.1%} of the pot after calling). Check how much remains, the raiser's range and who can still raise.",
    )


def next_card_draws(hole, board):
    board_suits = Counter(c[1] for c in board)
    hole_suits = Counter(c[1] for c in hole)
    flush_draws = {
        s for s, count in board_suits.items() if count == 2 and hole_suits[s] >= 2
    }
    nut_flush_draw = highest_flush_draw(hole, board)
    used = Counter(RANK_VALUE[c[0]] for c in hole + board)
    ranks = {RANK_VALUE[c[0]] for c in board}
    straight_outs = set()
    for a, b in combinations({RANK_VALUE[c[0]] for c in hole}, 2):
        for straight in STRAIGHTS:
            if a not in straight or b not in straight:
                continue
            need = straight - ranks - {a, b}
            if len(need) == 1:
                straight_outs.update(need)
    return bool(flush_draws), nut_flush_draw, sum(4 - used[r] for r in straight_outs)


def postflop_advice(snap, hero, seats):
    street = snap.street
    cards, board = tuple(sorted(snap.hero_cards)), tuple(sorted(snap.board))
    if len(board) != {"Flop": 3, "Turn": 4, "River": 5}[street]:
        return None
    active = len(seats)
    opponents = [s for s in seats if s.name != hero.name]
    facing = bool(snap.to_call)
    price = snap.to_call / (snap.pot + snap.to_call)
    actions = [a for a in snap.history if a.street == street]
    before = [a for a in snap.history if a.action == "raise" and a.street == "Preflop"]
    raiser = before[-1].name if before else None
    bets = [a for a in actions if a.action in {"bet", "raise"}]
    pos = hero.position
    heads_up = active == 2
    label = f"{street} · {pos}"
    if facing:
        label += " facing a bet" if len(bets) == 1 else " facing a raise"
    elif heads_up and street == "Flop" and raiser == hero.name:
        label += " · c-bet opportunity"
    elif heads_up and street in {"Turn", "River"}:
        previous = STREETS[STREETS.index(street) - 1]
        line = [(a.name, a.action) for a in snap.history if a.street == previous]
        aggressors = [name for name, action in line if action in {"bet", "raise"}]
        if aggressors == [hero.name] and line[-1][1] == "call":
            label += " · barrel opportunity"
        elif (
            street == "Turn"
            and raiser == hero.name
            and len(line) == 2
            and all(action == "check" for _, action in line)
        ):
            label += " · delayed c-bet opportunity"
    elif (
        actions
        and actions[-1].action == "check"
        and heads_up
        and actions[-1].name == raiser
    ):
        label += " · raiser checked"

    def advice(choices, why, priority=1, size=None):
        return Advice(label, choices, why, priority=priority, size=size)

    rank, kicks, _, _ = best_plo_hand(cards, board)
    paired = len({c[0] for c in board}) < len(board)
    flush_on_board = max(Counter(c[1] for c in board).values()) >= 3
    draw, nut_draw, straight_outs = (
        (False, False, 0) if street == "River" else next_card_draws(cards, board)
    )
    allin = any(s.all_in for s in seats)
    best = rank >= 4 and nuts(cards, board, rank, tuple(kicks))
    top_set = (
        rank == 3
        and not paired
        and kicks[0] == max(RANK_VALUE[c[0]] for c in board)
        and sum(RANK_VALUE[c[0]] == kicks[0] for c in cards) >= 2
    )
    board_ranks = {RANK_VALUE[c[0]] for c in board}
    straight_possible = any(len(board_ranks & window) >= 3 for window in STRAIGHTS)
    if allin and not heads_up:
        return advice(
            (),
            "A player is already all-in. Consider which part of the pot you can win and who can still bet.",
        )
    if street in {"Flop", "Turn"} and facing:
        if snap.price_to_match > snap.to_call:
            if not heads_up or any(
                seat.committed > hero.committed + snap.to_call
                for seat in snap.seats
                if not seat.live
            ):
                return advice(
                    (),
                    "The stack cap creates different pot entitlements. Count only chips Hero can win before comparing the price with equity.",
                )
            price = heads_up_price(snap)
        cost = f"The call costs {price:.1%} of the contestable pot after calling, before rake."
        if best and rank == 4:
            lower = weaker_straights(cards, board, kicks[0])
            targets = (
                f"Lower straights made with {' or '.join(lower)} can be in the betting range."
                if lower else
                "Only this straight is possible here. The weaker part of the betting range could include sets, two pair or draws."
            )
            return advice(
                (),
                f"You have the current nut straight. {cost} {targets} A player sharing your straight may have extra ways to win on the remaining cards. Compare both players' redraws.",
            )
        if (allin or snap.to_call == hero.stack) and not (
            best or (top_set and not flush_on_board and not straight_possible)
        ):
            return advice(
                (),
                f"{cost} An all-in call depends on showdown equity against the opponent's range. Count ties, redraws and the cards still to come.",
            )
        if not heads_up and top_set:
            high = max(s.committed for s in seats)
            pending = [s.position for s in opponents if s.stack and s.committed < high]
            action = (f"{' and '.join(pending)} can still respond after a call." if pending
                      else "A call closes this street.")
            return advice(
                (),
                f"You have top set. {cost} {action} Compare the combined draws and how much of the pot Hero can win.",
            )
    if street == "River" and not best:
        river = advise_river(snap, hero, opponents)
        if river is not None:
            return Advice(
                river.title,
                river.choices,
                river.why,
                priority=river.priority,
                size=river.size,
                rankable=river.rankable,
            )
        if facing or not (top_set and not flush_on_board and not straight_possible):
            return advice(
                (),
                "Verify the final betting line and the pot Hero can win before choosing an action.",
            )
    if (
        street in {"Flop", "Turn"}
        and not best
        and not (top_set and not flush_on_board and not straight_possible)
    ):
        early = early_advice(snap, hero, opponents, rank, kicks)
        if early is not None:
            return Advice(
                early.title,
                early.choices,
                early.why,
                priority=early.priority,
                size=early.size,
            )
    if not facing:
        if not snap.can_raise:
            return None
        if best or (top_set and not flush_on_board and not straight_possible):
            choices, why, size = value_plan(
                snap,
                hero,
                opponents,
                actions,
                rank,
                kicks,
                cards,
                board,
                paired,
                flush_on_board,
            )
            return advice(choices, why, 3, size)
        if nut_draw and not paired:
            context, _, _, large_call = line_context(snap, hero, opponents, actions)
            if street == "Turn" and large_call and straight_possible:
                checked = {a.name for a in actions if a.action == "check"}
                checked_to_hero = all(s.name in checked for s in opponents)
                check = (
                    "Checking back sees the river for free."
                    if checked_to_hero
                    else "A check leaves players behind you the option to bet."
                )
                return advice(
                    (),
                    f"{context} Straights can call or raise. {check} A semibluff needs enough folds from hands currently ahead.".strip(),
                )
            continues = "Sets, two pair and pair-plus-draw hands can call."
            if straight_possible:
                continues = (
                    "Made straights, sets and draws with additional equity can call."
                )
            return advice(
                ("check", "bet"),
                f"{context} Checking keeps the pot small. A semibluff can fold out some better made hands while keeping a chance to make the nut flush. {continues}".strip(),
                size=bet_size(snap, hero, opponents, 33),
            )
        if (
            not heads_up
            and rank <= 1
            and not nut_draw
            and (flush_on_board or straight_outs <= 4)
        ):
            context, _, _, _ = line_context(snap, hero, opponents, actions)
            hand = (
                "the board's pair"
                if paired
                else "one pair"
                if rank == 1
                else "high card"
            )
            if flush_on_board:
                stronger = "Opponents holding two cards of the board's long suit can already have a flush."
                if paired:
                    stronger += " The paired board also allows full houses."
            elif paired:
                stronger = "Opponents can already hold trips or a full house."
            elif straight_possible:
                stronger = "Straights, sets and two pair can continue."
            else:
                stronger = "Sets, two pair and strong draws can continue."
            draw_note = " Your weaker flush draw can be dominated." if draw else ""
            plan = (
                "Checking keeps the pot smaller. A bet as a bluff needs enough better hands to fold across all remaining opponents."
                if rank == 0 or paired or flush_on_board
                else "Checking keeps the pot smaller. Before betting for protection or thin value, identify worse pair-plus-draw hands that can call and hands with live equity that may fold."
            )
            return advice(
                (),
                f"{context} You hold {hand} in a multiway pot.{draw_note} {stronger} {plan}".strip(),
            )
        if (
            heads_up
            and rank == 0
            and not draw
            and straight_outs < 4
            and (paired or flush_on_board)
        ):
            return advice(
                (),
                "Check by default. For a bluff, identify which strong hands you block and which hands the opponent actually folds.",
            )
        return advice(
            (),
            "Compare which weaker hands call, which better hands fold and how the texture affects both ranges.",
        )

    if best:
        context, _, _, _ = line_context(snap, hero, opponents, actions)
        if not snap.can_raise:
            board_note = ("The final board is complete." if street == "River" else
                          "The remaining board can change the relative strength of both hands.")
            return advice(
                ("call",),
                f"{context} You face a bet with the current nuts. {board_note} "
                "Your available actions are call and fold.".strip(),
                4,
            )
        targets, _ = (
            paired_targets(cards, board, rank, kicks)
            if paired and rank >= 6
            else ([], [])
        )
        if targets:
            value = (
                f" Before raising, consider whether {' or '.join(targets)} can call."
            )
        else:
            value = " A value raise needs a weaker made hand that fits the line and can call."
        return advice(
            ("call", "raise") if snap.can_raise else ("call",),
            f"{context} The opponent bet and you currently hold the nuts. Calling keeps bluffs in the pot.{value}".strip(),
            4,
        )
    if top_set and not flush_on_board and not straight_possible:
        return advice(
            ("call", "raise") if snap.can_raise else ("call",),
            ("Top set is strong on this board. Call or raise for value depending on the bet size and remaining stack."
             if snap.can_raise else
             "Top set is currently the best hand. Compare the price of calling with the draws that can overtake you."),
            3,
        )
    if nut_draw and not paired:
        return advice(
            (),
            f"You have the nut flush draw. Check for extra outs and consider how much more you can win when you hit. Calling costs {price:.0%} of the pot after calling.",
        )
    return advice(
        (),
        f"Estimate your clean outs and how often you will have to pay again. Calling requires about {price:.0%} equity before rake and future betting.",
    )


def advice_at(snap):
    if (
        not snap.hero_cards
        or len(snap.hero_cards) != 4
        or snap.pot <= 0
        or snap.bb <= 0
    ):
        return None
    seats = [seat for seat in snap.seats if seat.live]
    hero = next((seat for seat in seats if seat.is_hero), None)
    if hero is None or len(seats) < 2:
        return None
    if snap.street == "Preflop":
        return preflop_advice(snap, hero, seats)
    return postflop_advice(snap, hero, seats)
