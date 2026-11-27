"""Exercise closing-action and effective-stack boundaries without personal histories."""

import unittest
from dataclasses import replace

from leakfinder.critique import critique_for, stack_context
from leakfinder.strategy_guide import advice_at
from tests.test_strategy_guide import move, view


def evaluate(snap, action, amount):
    event = move(snap.hero, action, amount, street=snap.street)
    return critique_for(snap, event, advice_at(snap))


def blind_spot(cards, position, entries, stacks=None):
    paid = {"BB": 1, "SB": 0.5}
    for action in entries:
        paid[action.name] = paid.get(action.name, 0) + action.amount
    price = max(paid.values()) - paid[position]
    snap = view(cards, position, prior=entries, owed=price, pot=sum(paid.values()))
    seats = tuple(
        replace(
            seat,
            committed=paid.get(seat.name, 0),
            stack=(stacks or {}).get(seat.name, 120) - paid.get(seat.name, 0),
        )
        for seat in snap.seats
    )
    return replace(snap, seats=seats)


def hu_flop(hole, board, facing_raise):
    if facing_raise:
        pre = [move("CO", "raise", 3, to=3), move("BTN", "call", 3)]
        flop = [move("CO", "bet", 5, street="Flop"), move("BTN", "raise", 16, to=16, street="Flop")]
        call, pot = 11, 28.5
    else:
        pre = [move("CO", "raise", 3, to=3), move("BTN", "raise", 10, to=10), move("CO", "call", 7)]
        flop = [move("CO", "check", street="Flop"), move("BTN", "bet", 16, street="Flop")]
        call, pot = 16, 37.5
    pre = [
        move("UTG", "fold"),
        move("HJ", "fold"),
        *pre[:2],
        move("SB", "fold"),
        move("BB", "fold"),
        *pre[2:],
    ]
    snap = view(hole, "CO", "Flop", board, [*pre, *flop], owed=call, pot=pot)
    seats = tuple(
        replace(
            seat,
            stack=78 if seat.name == "BTN" else 100,
            committed=sum(a.amount for a in flop if a.name == seat.name),
        )
        for seat in snap.seats
    )
    return replace(snap, seats=seats)


class StackAndFieldTests(unittest.TestCase):
    def test_closed_aces_and_legal_pot_size(self):
        entries = [
            move("UTG", "fold"),
            move("HJ", "raise", 3, to=3),
            move("CO", "call", 3),
            move("BTN", "call", 3),
            move("SB", "fold"),
        ]
        cards = "Ad 6c Ac 3s"
        spot = blind_spot(cards, "BB", entries)
        for room in ("iPoker", "CoinPoker"):
            result = evaluate(replace(spot, room=room), "call", 2)
            self.assertEqual(result.rule, "aa_closing_squeeze")
            self.assertIn("4 players", result.reason)
            self.assertIn("15.5 BB", result.reason)
        for alt in (
            replace(spot, can_raise=False),
            replace(spot, hero_cards=("Ad", "Ac", "Ah", "3s")),
            blind_spot(cards, "BB", entries, {"BB": 4}),
            blind_spot(cards, "BB", entries, {"CO": 5, "HJ": 6}),
            blind_spot(cards, "BB", entries, dict.fromkeys(("BB", "CO", "HJ", "BTN"), 500)),
            blind_spot(cards, "BB", [move("UTG", "call", 1), *entries[1:]]),
        ):
            self.assertIsNone(evaluate(alt, "call", alt.to_call))
        self.assertIsNone(evaluate(spot, "raise", 14.5))

    def test_deep_opponent_has_position_only_if_acting_after_big_blind(self):
        entries = [
            move("UTG", "fold"),
            move("HJ", "fold"),
            move("CO", "raise", 3, to=3),
            move("BTN", "call", 3),
            move("SB", "call", 2.5),
        ]
        cards = "Ac Ah 9s 4c"
        deep_sb = blind_spot(cards, "BB", entries, {"BB": 260, "SB": 300, "BTN": 85})
        self.assertEqual(evaluate(deep_sb, "call", 2).rule, "aa_closing_squeeze")
        deep_btn = blind_spot(cards, "BB", entries, {"BB": 260, "SB": 85, "BTN": 300})
        self.assertIsNone(evaluate(deep_btn, "call", 2))
        self.assertEqual(
            stack_context(deep_sb),
            "4-way · Stacks behind (BB) · Hero 259 · CO 117 · BTN 82 · SB 297",
        )

    def test_blind_backraise_needs_a_big_increment_opponents_and_weak_suit(self):
        entries = [
            move("UTG", "call", 1),
            move("HJ", "call", 1),
            move("CO", "fold"),
            move("BTN", "fold"),
            move("SB", "call", 0.5),
            move("BB", "raise", 4, to=5),
            move("UTG", "call", 4),
            move("HJ", "call", 4),
        ]
        spot = blind_spot("8s 6s 4c 3h", "SB", entries)
        self.assertEqual(evaluate(spot, "raise", 24).rule, "low_blind_backraise")
        self.assertIsNone(evaluate(spot, "call", 4))
        self.assertIsNone(evaluate(spot, "raise", 9))
        for cards in ("8s 6s 4h 3h", "7s 6s 5h 4c", "As 6s 4h 3c", "8h 8s 6s 4c"):
            self.assertIsNone(evaluate(replace(spot, hero_cards=tuple(cards.split())), "raise", 24))
        short = blind_spot("8s 6s 4c 3h", "SB", entries, {"SB": 11})
        self.assertIsNone(evaluate(short, "raise", 10))

    def test_heads_up_call_examples_transfer_to_other_cards(self):
        facing_raise = hu_flop("Ah 8h 3d 2d", "Qh Jc 7d", True)
        gutshot = hu_flop("6s 6c 5s 3c", "Kc 5d 7h", False)
        for spot, rule in (
            (facing_raise, "flop_raise_call_no_draw"),
            (gutshot, "flop_cbet_call_low_gutshot"),
        ):
            self.assertEqual(evaluate(spot, "call", spot.to_call).rule, rule)
            self.assertIsNone(evaluate(spot, "fold", 0))
            for name in (spot.hero, "BTN"):
                seats = tuple(
                    replace(seat, stack=spot.to_call + 1 if seat.is_hero else 1)
                    if seat.name == name
                    else seat
                    for seat in spot.seats
                )
                self.assertIsNone(evaluate(replace(spot, seats=seats), "call", spot.to_call))
            seats = tuple(replace(s, live=True) if s.name == "BB" else s for s in spot.seats)
            self.assertIsNone(evaluate(replace(spot, seats=seats), "call", spot.to_call))
            self.assertIsNone(evaluate(replace(spot, pot=4 * spot.pot), "call", spot.to_call))
        self.assertIsNone(
            evaluate(replace(facing_raise, hero_cards=("Ah", "Kh", "3d", "2d")), "call", 11)
        )
        self.assertIsNone(
            evaluate(replace(gutshot, hero_cards=("8s", "6s", "5s", "3c")), "call", 16)
        )
        self.assertIsNone(
            evaluate(replace(gutshot, hero_cards=("Ac", "6c", "6s", "3h")), "call", 16)
        )

    def test_in_position_and_three_players_on_flop_do_not_trigger_raise_review(self):
        spot = hu_flop("Ah 8h 3d 2d", "Qh Jc 7d", True)
        pre = [a for a in spot.history if a.street == "Preflop"]
        check_raised = [move("BB", "check", street="Flop"), *spot.history[-2:]]
        # Hero bets in position and the blind check-raises.
        pre_hu = [move("CO", "raise", 3, to=3), move("BTN", "fold"), move("BB", "call", 2)]
        check_raised[-1] = replace(check_raised[-1], name="BB")
        seats = tuple(
            replace(s, live=True, stack=78, committed=16)
            if s.name == "BB"
            else replace(s, live=False, committed=0)
            if s.name == "BTN"
            else s
            for s in spot.seats
        )
        ip = replace(spot, seats=seats, history=tuple([*pre_hu, *check_raised]))
        self.assertIsNone(evaluate(ip, "call", 11))
        # BB saw a three-way flop and folded to the raise before Hero acts again.
        pre = [replace(a, action="call", amount=2) if a.name == "BB" else a for a in pre]
        flop = [
            move("BB", "check", street="Flop"),
            *spot.history[-2:],
            move("BB", "fold", street="Flop"),
        ]
        multi = replace(spot, history=tuple([*pre, *flop]))
        self.assertIsNone(evaluate(multi, "call", 11))
        self.assertEqual(stack_context(spot), "Heads-up · Stacks behind (BB) · Hero 100 · BTN 78")
