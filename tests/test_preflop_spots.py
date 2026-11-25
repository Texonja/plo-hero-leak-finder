import unittest
from dataclasses import replace

from leakfinder.preflop_spots import review_common, threebet_price_note
from leakfinder.strategy_guide import advice_at
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

    def test_low_double_pairs_can_make_two_sets_and_are_not_graded_as_folds(self):
        early = view("7c 4s 7h 4h", "UTG", pot=1.5)
        self.assertIsNone(review_common(early, move("UTG", "raise", 3.5)))
        note = advice_at(early)
        self.assertFalse(note.can_compare(early))
        self.assertIn("23.4%", note.why)
        self.assertIn("work together in a straight", note.why)
        distant = replace(early, hero_cards=("9c", "9s", "2h", "2c"))
        self.assertIn("cannot make a straight together", advice_at(distant).why)
        mixed = replace(early, seats=tuple(
            replace(seat, stack=38) if seat.position == "CO" else seat
            for seat in early.seats
        ))
        self.assertIn("23.4%", advice_at(mixed).why)
        self.assertIn("outside 40", advice_at(mixed).why)
        self.assertIsNone(
            review_common(view("7c 4s 7h 4h", "BTN"), move("BTN", "raise", 3.5))
        )
        self.assertIsNone(
            review_common(view("7c 8s 7h 8h", "UTG"), move("UTG", "fold"))
        )

    def test_3bet_call_checks_price_position_and_prior_calls(self):
        pairs = threebet_position("CO", "BTN", "7c 4s 7h 4h")
        self.assertIsNone(review_common(pairs, move("CO", "call", 8.5)))
        study = advice_at(pairs)
        self.assertIn("33.3%", study.why)
        self.assertIn("23.4%", study.why)
        self.assertIn("out of position", study.why)
        self.assertFalse(study.can_compare(pairs))
        cards = "7c 4s Jh 2d"
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

    def test_button_high_pair_without_suits_and_isolated_side_cards_is_study_only(self):
        bare = view("Qs Qh 6d 3c", "BTN")
        self.assertIsNone(review_common(bare, move("BTN", "fold")))
        self.assertIn("blinds", advice_at(bare).why)
        connected = view("Qs Qh Jd Tc", "BTN")
        self.assertEqual(
            review_common(connected, move("BTN", "fold")).rule, "rfi_supported_hand"
        )

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
