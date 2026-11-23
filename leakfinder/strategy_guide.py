"""Local, authored advice from cards and actions available before a PLO4 decision."""

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from . import preflop_guide
from .early_postflop import early_advice
from .importers import STREETS
from .postflop_value import (
    STRAIGHTS,
    BetSize,
    bet_size,
    line_context,
    paired_targets,
    value_plan,
)
from .river_guide import advise_river
from .statistics import tags
from .vendor.common import RANK_VALUE
from .vendor.primitives import best_plo_hand, classify_known_hand, eval_5

LABELS = {
    "fold": "fold",
    "call": "call",
    "raise": "raise",
    "bet": "bet",
    "check": "check",
}
EARLY = {"UTG", "UTG1", "MP", "MP1", "MP2"}
FORCED = {"posts_ante", "posts_sb", "posts_bb"}
DECK = tuple(r + s for r in RANK_VALUE for s in "cdhs")


@dataclass(frozen=True)
class Advice:
    title: str
    choices: tuple[str, ...]
    why: str
    raise_label: str = "raise"
    priority: int = 1
    size: BetSize | None = None

    @property
    def play(self):
        labels = LABELS | {"raise": self.raise_label}
        if self.size:
            labels["bet"] = self.size.label
        return " or ".join(labels[a] for a in self.choices)

    def difference(self, action, amount):
        if not self.choices:
            return None
        if action not in self.choices:
            return "action"
        if action == "bet" and self.size and self.size.far_from(amount):
            return "size"
        return None


def preflop_class(cards, open_bb):
    """Prefer concrete overlapping strengths over a scalar hand ranking."""
    group = preflop_guide.hand_group(cards, open_bb)
    if group != "review":
        return group
    counts = Counter(c[0] for c in cards)
    suits = Counter(c[1] for c in cards)
    flags = classify_known_hand(cards)
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
    if flags["double_suited"] and flags["connected_window"] >= 3:
        return "connected_ds"
    if flags["pairs"] and len(suits) == 4:
        return "rainbow_pair"
    if (
        len(suits) == 4
        and flags["broadways"] < 2
        and flags["connected_window"] < 3
        and not ("A" in counts and sum(r in counts for r in "2345") >= 2)
    ):
        return "rainbow"
    return "review"


PREMIUM = {"aces", "other_aces"}
STRONG = {"queens", "highrun", "ak_ds", "suited_broadway"}
PLAYABLE = {"kings", "run", "ak_link", "connected_ds"}
FRINGE = {"ak_link_lower_suit", "ak_split", "ak_low", "small_open"}
WEAK = {"weak_suit", "rainbow", "trips"}
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
    voluntary = [a for a in snap.history if a.action not in FORCED]
    raises = [a for a in voluntary if a.action == "raise"]
    calls = [a for a in voluntary if a.action == "call"]
    faced = raises[-1] if raises else None
    size = faced.raised_to / snap.bb if faced else 0
    hand = preflop_class(snap.hero_cards, size if len(raises) == 1 else None)
    by_name = {s.name: s for s in seats}
    own = [a.action for a in voluntary if a.name == hero.name]
    pos = hero.position
    in_blind = pos in {"SB", "BB"}
    late = pos in {"CO", "BTN", "SB", "BTN/SB"}
    cheap = size <= 2.5
    pot_odds = snap.to_call / (snap.pot + snap.to_call)
    pos_label = f"{pos} vs {by_name[faced.name].position}" if faced else pos
    explanation = PREFLOP_CLASSES.get(hand) or preflop_guide.GROUPS[hand].title + "."
    ranks = tuple(sorted({RANK_VALUE[c[0]] for c in snap.hero_cards}))
    explanation = " ".join(
        filter(
            None,
            (
                " · ".join(tags(snap.hero_cards)) + ".",
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

    if hand in {"double_pair", "rainbow_pair", "quads"}:
        return advice(
            "Hand structure review",
            (),
            explanation + " No validated action rule for this group.",
        )
    if (
        hero.stack < 20 * snap.bb
        or any(s.all_in for s in seats)
        or snap.to_call >= hero.stack
    ):
        return advice(
            "Short stack or all-in",
            (),
            "Check the price, effective stack and possible side pots. The opening guidelines assume enough stack depth to play.",
        )
    if not raises and not calls:
        if hand in PREMIUM | STRONG or (late and hand in PLAYABLE):
            return advice(
                "First in",
                ("raise",),
                explanation + " Open for value and initiative.",
            )
        if hand in WEAK:
            if pos in {"SB", "BTN/SB"}:
                return advice(
                    "First in from the small blind",
                    ("fold", "call"),
                    explanation
                    + " Consider folding or completing, depending on the big blind.",
                )
            return advice(
                "First in",
                ("fold",),
                explanation
                + " Open with a stronger combination when more players are still to act.",
            )
        if hand in PLAYABLE | FRINGE | {"bare_ace"}:
            return advice(
                "First in",
                ("fold", "raise"),
                explanation
                + " Open wider from CO and BTN. Early position calls for stronger support across all four cards.",
            )
        return advice("First in", (), explanation)
    if not raises:
        if hand in PREMIUM | STRONG:
            return advice(
                "Facing limpers",
                ("raise",),
                explanation + " Raise for value against weaker hands that can call.",
            )
        if not snap.to_call:
            return advice(
                "Big blind option after limps",
                ("check", "raise") if hand in PLAYABLE else ("check",),
                explanation
                + " Checking takes a free flop. Stronger connected hands can raise to isolate.",
            )
        if hand in WEAK | {"bare_ace"}:
            return advice(
                "Facing limpers",
                ("fold",) if pos != "SB" else ("fold", "call"),
                explanation + " Prioritize nut potential in a large multiway pot.",
            )
        return advice(
            "Facing limpers",
            (),
            explanation
            + " The price and the number of limpers determine whether isolating or calling is worthwhile.",
        )
    if len(raises) == 1 and not calls and not own:
        legacy = preflop_guide.at_decision(snap)
        if legacy and legacy.play != "Manual review":
            actions = {"fold": "fold", "call": "call", "3-bet": "raise"}
            return advice(
                "Facing an open",
                tuple(actions[a] for a in legacy.play.lower().split(" or ")),
                explanation + " " + legacy.why,
            )
        if hand in PREMIUM:
            return advice(
                "Facing an open",
                ("raise",) if hand == "aces" else ("raise", "call"),
                explanation
                + " Aces often benefit from building the pot against a single opener.",
            )
        if hand in WEAK or hand == "bare_ace":
            choices = ("fold", "call") if pos == "BB" and cheap else ("fold",)
            return advice(
                "Defending a blind" if in_blind else "Facing an open",
                choices,
                explanation
                + " Consider the price of defending and the risk of domination against this open.",
            )
        if hand in STRONG | PLAYABLE | FRINGE:
            opener = by_name[faced.name].position
            if hand in STRONG and opener not in EARLY:
                choices = (
                    ("call", "raise") if pos != "SB" else ("raise", "call", "fold")
                )
            elif pos == "BB" and pot_odds <= 0.32:
                choices = ("call", "fold") if hand in FRINGE else ("call",)
            else:
                choices = (
                    ("fold", "call")
                    if in_blind or opener in EARLY or hand in FRINGE
                    else ("call",)
                )
            return advice(
                "Defending a blind" if in_blind else "Facing an open",
                choices,
                explanation
                + f" A CO or BTN open generally allows wider defense than an early-position open. Calling here requires about {pot_odds:.0%} raw equity before rake and future betting.",
            )
        return advice("Facing an open", (), explanation)
    if len(raises) == 1:
        if hand == "aces":
            return advice(
                "Squeeze or isolate after a call",
                ("raise",),
                explanation
                + " Raise for value and reduce the chance of a large multiway flop.",
            )
        if hand in WEAK | {"bare_ace"}:
            return advice(
                "Open with callers",
                ("fold",),
                explanation
                + " Choose hands with multiple ways to make the nuts in a multiway pot.",
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
    effective = (
        min(
            hero.stack + hero.committed,
            by_name[faced.name].stack + by_name[faced.name].committed,
        )
        / snap.bb
    )
    if (
        len(raises) == 2
        and "raise" in own
        and hand == "aces"
        and len(seats) == 2
        and 45 <= effective <= 110
        and snap.can_raise
        and not extra
    ):
        return advice(
            spot,
            ("raise",),
            explanation
            + " At this effective stack, consider a value 4-bet against a single 3-bettor.",
            2,
        )
    if hand in PREMIUM:
        return advice(
            spot,
            ("call", "raise") if snap.can_raise else ("call",),
            explanation
            + " Choose between calling and reraising based on pot size, side cards and the effective stack.",
        )
    if hand in WEAK | {"bare_ace"} and pot_odds >= 0.20:
        return advice(
            spot,
            ("fold",),
            explanation
            + " This price calls for a hand that retains value against a stronger range.",
        )
    if not extra and len(raises) == 2 and "raise" in own and hand in STRONG | PLAYABLE:
        return advice(
            spot,
            ("call", "fold"),
            explanation
            + f" Calling costs about {pot_odds:.0%} of the final pot. Fold more often out of position or against an early or tight 3-bet.",
        )
    return advice(
        spot,
        (),
        explanation + " Check how much remains after calling and who can still raise.",
    )


@lru_cache(maxsize=2048)
def nuts(hole, board, rank, kickers):
    """Check whether any legal opponent hole pair beats the current made hand."""
    board_triples = tuple(combinations(board, 3))
    remaining = (
        card for card in reversed(DECK) if card not in hole and card not in board
    )
    for first, second in combinations(remaining, 2):
        for triple in board_triples:
            values = (first, second, *triple)
            suited = all(c[1] == first[1] for c in values)
            counts = len({c[0] for c in values})
            # Straights/flushes have distinct ranks. Full houses/quads have two.
            if (not suited and counts not in {2, 5}) or (
                rank >= 5 and counts == 5 and not suited
            ):
                continue
            other, tiebreak, _ = eval_5(values)
            if (other, tuple(tiebreak)) > (rank, kickers):
                return False
    return True


def next_card_draws(hole, board):
    board_suits = Counter(c[1] for c in board)
    hole_suits = Counter(c[1] for c in hole)
    flush_draws = {
        s for s, count in board_suits.items() if count == 2 and hole_suits[s] >= 2
    }
    nut_flush_draw = any("A" + suit in hole for suit in flush_draws)
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
    pot_before_bet = (
        snap.pot - bets[0].amount
        if len(bets) == 1 and not any(a.action == "call" for a in actions)
        else 0
    )
    large = bool(pot_before_bet > 0 and bets[0].amount >= 0.65 * pot_before_bet)
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
        and sum(RANK_VALUE[c[0]] == kicks[0] for c in cards) == 2
    )
    board_ranks = {RANK_VALUE[c[0]] for c in board}
    straight_possible = any(len(board_ranks & window) >= 3 for window in STRAIGHTS)
    if allin and not heads_up:
        return advice(
            (),
            "A player is already all-in. Consider which part of the pot you can win and who can still bet.",
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
                return advice(
                    ("check",),
                    f"{context} The turn allows straights that can call or raise. Check the nut flush draw to try to see the river for free.".strip(),
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
        if not heads_up and rank <= 1 and not nut_draw and straight_outs <= 4:
            context, _, _, _ = line_context(snap, hero, opponents, actions)
            stronger = (
                "Straights, sets and two pair can continue."
                if straight_possible
                else "Sets, two pair and strong draws can continue."
            )
            hand = "one pair" if rank == 1 else "high card"
            draw_note = " Your weaker flush draw can be dominated." if draw else ""
            return advice(
                ("check",),
                f"{context} You hold {hand} in a multiway pot.{draw_note} {stronger} Checking preserves equity and avoids building a pot against these hands.".strip(),
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
            "Top set is strong on this board. Call or raise for value depending on the bet size and remaining stack.",
            3,
        )
    if large and rank == 0 and not draw and straight_outs < 4:
        return advice(
            ("fold",),
            f"You have high card and no strong draw. Calling requires about {price:.0%} equity before rake. Fold against this size.",
            2,
        )
    if street == "River" and not heads_up and rank <= 1 and price >= 0.22:
        return advice(
            ("fold",),
            f"Multiway, call with one pair or worse only when there is a good reason to expect a bluff. The raw call threshold is {price:.0%}.",
            2,
        )
    if street == "River":
        return advice(
            (),
            f"Calling requires about {price:.0%} pot equity before rake. Identify weaker value hands and bluffs that fit the preceding action and weigh them against better hands.",
        )
    if allin:
        return advice(
            (),
            f"Check equity against plausible all-in ranges and account for side pots. The amount to call is {price:.0%} of the pot after calling.",
        )
    if (
        heads_up
        and len(bets) == 1
        and nut_draw
        and not paired
        and straight_outs >= 6
        and price <= 0.22
    ):
        return advice(
            ("call", "raise") if snap.can_raise else ("call",),
            f"The nut flush draw and possible straight support continuing at this price ({price:.0%} of the pot after calling). Check for overlapping and unclean outs.",
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
