"""Protect the flop review against overcalling and overflagging."""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from leakfinder.demo import SAMPLES
from leakfinder.flop_critique import flop_cbet_note, review_flop
from leakfinder.importers import sources
from leakfinder.replay_view import _ledger, _snapshot
from tests.test_early_audit import equity, spot
from tests.test_strategy_guide import move


def decision(action, amount=0):
    return SimpleNamespace(action=action, amount=amount)


def preflop_raiser(snap, name):
    return replace(snap, history=(move(name, "raise", 3, to=3), *snap.history))


class FlopCritiqueTests(unittest.TestCase):
    def test_call_review_distinguishes_true_cbet_and_keeps_price_on_fold(self):
        snap = spot("8h 6c 5s 3d", "Kh 8d 2s", owed=75, pot=175)
        self.assertIsNone(review_flop(snap, decision("call", 75)))
        snap = preflop_raiser(snap, "BB")
        self.assertIsNone(review_flop(snap, decision("call", 75)))  # Little left on the turn.
        snap = replace(snap, seats=tuple(replace(s, stack=300) for s in snap.seats))
        result = review_flop(snap, decision("call", 75))
        self.assertEqual(result.rule, "flop-cbet-call-thin")
        self.assertIn("30.0%", result.reason)
        self.assertIsNone(review_flop(snap, decision("fold")))
        self.assertIn("30.0%", flop_cbet_note(snap))
        with_nut_backdoor = replace(snap, hero_cards=("8h", "Ah", "5s", "3d"))
        self.assertIsNone(review_flop(with_nut_backdoor, decision("call", 75)))
        top_pair = replace(snap, hero_cards=("Kd", "6c", "5s", "3d"))
        self.assertIsNone(review_flop(top_pair, decision("call", 75)))

    def test_all_in_call_uses_upper_bound_without_future_betting(self):
        snap = preflop_raiser(spot("Ks 8d Jh Tc", "Kh 8h 2h", owed=65, pot=165), "BB")
        seats = tuple(
            replace(player, stack=0, all_in=True) if player.name == "BB" else player
            for player in snap.seats
        )
        snap = replace(snap, seats=seats)
        result = review_flop(snap, decision("call", 65))
        self.assertEqual(result.rule, "flop-cbet-stackoff-bound")
        self.assertIn("820 possible", result.reason)
        self.assertIn("a flush or better", result.reason)
        self.assertIn("No further betting", result.reason)
        self.assertIn("820 runouts", result.reason)
        self.assertIn("frequency of that hand", result.reason)
        self.assertEqual(result.alternative, "Compare call and fold by range")
        self.assertLess(equity("Ks 8d Jh Tc", "Ah Qh 5c 3s", "Kh 8h 2h"), 65 / 230)
        self.assertGreater(equity("Ks 8d Jh Tc", "Kc 5d 4c 3s", "Kh 8h 2h"), 65 / 230)

    def test_middle_set_value_and_check_back_counterexamples(self):
        snap = preflop_raiser(spot("8d 8s Kc Tc", "Ah 8h 3d", pot=20), "BTN")
        result = review_flop(snap, decision("check"))
        self.assertEqual(result.rule, "flop-value-after-check")
        self.assertIn("middle set", result.title)
        self.assertIn("higher set", result.counterargument)
        for hand, board in (
            ("Js Jd 8c 8d", "Jc Th 8h"),  # A straight already beats top set.
            ("Ad 8s Qh 9c", "Ah 8c 7c"),  # Two pair without a nut redraw.
            ("As Ad 7c 4c", "Ah 9h 3h"),  # Top set on a monotone board.
        ):
            with self.subTest(hand=hand):
                snap = preflop_raiser(spot(hand, board, pot=20), "BTN")
                self.assertIsNone(review_flop(snap, decision("check")))

    def test_sizing_prices_and_blockers(self):
        wet = spot("Kc Kh Jc 8c", "Kd 6c 2c", pot=20)
        sizing = review_flop(wet, decision("bet", 5))
        self.assertEqual(sizing.rule, "flop-size-wet-value")
        self.assertIn("16.7%", sizing.reason)
        self.assertIn("25.0%", sizing.reason)
        self.assertIn("hold 3 cards", sizing.counterargument)
        self.assertIsNone(review_flop(wet, decision("bet", 10)))
        dry = preflop_raiser(spot("Ac As 5h Th", "9c 9d 3s", pot=30), "BTN")
        sizing = review_flop(dry, decision("bet", 30))
        self.assertEqual(sizing.rule, "flop-size-large-dry-paired")
        self.assertIn("33% pot", sizing.alternative)
        self.assertIsNone(review_flop(dry, decision("bet", 7)))
        under = replace(dry, hero_cards=("8c", "8h", "5h", "Ts"))
        self.assertEqual(review_flop(under, decision("bet", 30)).rule, "flop-size-underpair-dry-paired")

    def test_underpair_sizing_requires_a_large_cbet_after_a_check_on_high_paired_rainbow(self):
        snap = preflop_raiser(spot("6c 6s 4c 4h", "7d 3c 7s", pot=25), "BTN")
        idea = review_flop(snap, decision("bet", 20))
        self.assertIn("checking back", idea.alternative)
        self.assertIn("20 BB into 25 BB", idea.reason)
        self.assertIsNone(review_flop(snap, decision("bet", 19)))
        for board in ("3s 3c 7d", "7d 3d 7s", "7d 3c 6d"):
            self.assertIsNone(review_flop(replace(snap, board=tuple(board.split())), decision("bet", 20)))
        self.assertIsNone(review_flop(snap, decision("check")))

    def test_neutral_note_on_actual_fold_both_rooms(self):
        actual = (
            ("CoinPoker-HeroDemo.txt", "9000000027", "Line 30"),
            ("iPoker-HeroDemo.sql", "9000004004", "Action 9"),
        )
        for filename, number, ref in actual:
            with self.subTest(filename=filename):
                rec = next(
                    r
                    for r in sources(SAMPLES / filename)
                    if r.expected == number
                    or (not r.expected and f"Hand #{number}:" in r.raw)
                )
                hand, events = _ledger(rec)
                self.assertEqual(hand.number, number)
                snap = _snapshot(rec, hand, events, hand.hint, ref)
                self.assertEqual(events[ref].action, "fold")
                self.assertIn("continuation bet", flop_cbet_note(snap))
                self.assertIn(f"{snap.to_call / snap.bb:g} BB", flop_cbet_note(snap))
                self.assertIsNone(review_flop(snap, events[ref]))
