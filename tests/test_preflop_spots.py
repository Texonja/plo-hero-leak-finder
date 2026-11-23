import unittest
from dataclasses import replace

from leakfinder.preflop_spots import review_common, threebet_price_note
from tests.test_strategy_guide import move, view


def threebet_position(hero, raiser, cards):
    folds = [
        move(p, "fold")
        for p in ("UTG", "HJ", "CO", "BTN", "SB", "BB")
        if p not in {hero, raiser}
    ]
    actions = [move(hero, "raise", 3.5, to=3.5), move(raiser, "raise", 12, to=12)]
    return view(cards, hero, prior=actions + folds, owed=8.5, pot=17)


class PreflopSpotTests(unittest.TestCase):
    def test_first_in_uses_position_and_separates_wheel_from_high_connectivity(self):
        snap = view("Qs 3h Qh 2h", "BTN")
        self.assertEqual(
            review_common(snap, move("BTN", "fold")).rule, "rfi_supported_hand"
        )
        self.assertIsNone(
            review_common(view("Qs 3h Qh 2h", "UTG"), move("UTG", "fold"))
        )
        self.assertIsNone(review_common(view("As Th 3s 2h", "CO"), move("CO", "fold")))
        self.assertIsNone(
            review_common(view("As 4s Jc 2c", "UTG"), move("UTG", "fold"))
        )

    def test_early_small_pairs_and_steal_threshold(self):
        early = view("7c 4s 7h 4h", "UTG", pot=1.5)
        issue = review_common(early, move("UTG", "raise", 3.5))
        self.assertEqual(issue.rule, "rfi_thin_early_raise")
        self.assertIn("70.0%", issue.reason)
        self.assertIsNone(
            review_common(view("7c 4s 7h 4h", "BTN"), move("BTN", "raise", 3.5))
        )
        self.assertIsNone(
            review_common(view("7c 8s 7h 8h", "UTG"), move("UTG", "fold"))
        )

    def test_3bet_call_checks_price_position_and_prior_calls(self):
        cards = "7c 4s 7h 4h"
        oop = threebet_position("CO", "BTN", cards)
        issue = review_common(oop, move("CO", "call", 8.5))
        self.assertEqual(issue.rule, "threebet_thin_oop_call")
        self.assertIn("33.3%", issue.reason)
        self.assertIn("out of position", threebet_price_note(oop))
        ip = threebet_position("BTN", "BB", cards)
        self.assertIn("in position", threebet_price_note(ip))
        self.assertIsNone(review_common(ip, move("BTN", "call", 8.5)))
        self.assertIsNone(review_common(replace(oop, pot=31), move("CO", "call", 8.5)))
        with_caller = replace(oop, history=(*oop.history, move("SB", "call", 12)))
        self.assertFalse(threebet_price_note(with_caller))
        self.assertIsNone(review_common(with_caller, move("CO", "call", 8.5)))

    def test_non_ace_fourbet_uses_committed_fraction(self):
        snap = threebet_position("CO", "BTN", "Jh 9c Kd Qd")
        issue = review_common(snap, move("CO", "raise", 34))
        self.assertEqual(issue.rule, "threebet_committing_fourbet")
        self.assertIn("40% of your effective", issue.reason)
        self.assertIsNone(review_common(snap, move("CO", "raise", 17)))
        self.assertIsNone(
            review_common(
                replace(snap, hero_cards=("Ah", "9c", "Kd", "Qd")),
                move("CO", "raise", 34),
            )
        )
