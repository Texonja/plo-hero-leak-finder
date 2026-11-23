"""Counterexamples to ranking weak preflop structures; check every delivered street."""

import unittest
from contextlib import closing
from itertools import combinations

from leakfinder.demo import load_demo
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import Advice, advice_at, nuts
from leakfinder.vendor.primitives import best_plo_hand, eval_5
from pilots.guide_audit import audit_library
from tests.test_strategy_guide import move, view


class GuideRevisionTests(unittest.TestCase):
    def test_allowing_every_legal_action_cannot_support_a_comparison(self):
        snap = view("As Ks Ah Kh", street="Turn", board="Qs Js Ts 3c", owed=0)
        self.assertFalse(Advice("Value", ("check", "bet"), "").can_compare(snap))
        self.assertTrue(Advice("Value", ("bet",), "").can_compare(snap))

    def test_a_straight_window_and_two_suits_do_not_make_a_compulsory_open(self):
        pockets = (
            "2d 3s Kd 6s",
            "Kd 3c 4d 5c",
            "5c 4h 9c 2h",
            "Th 3c Jc 7h",
            "6d 2s 2d 3s",
            "3s Kd 9s Jd",
            "Jh 9s 7s 5h",
            "3s 9s 4c 7c",
            "3h Kh 7s 4s",
            "7h Jh 5d Td",
            "8d 5s Qd 9s",
            "4d 5c 3d Kc",
            "3c 8d 7d Tc",
            "8h 2c 4c 5h",
            "4h 8s 6h 2s",
        )
        for pocket in pockets:
            with self.subTest(pocket=pocket):
                guide = advice_at(view(pocket, "CO", pot=1.5))
                self.assertIsNone(guide.difference("fold", 0))
        guide = advice_at(view("Ks As 2s Tc", "UTG", pot=1.5))
        self.assertIsNone(guide.difference("fold", 0))
        self.assertEqual(advice_at(view("Ah Kh As Ks", "CO")).choices, ("raise",))

    def test_btn_pot_odds_do_not_establish_a_call_or_an_error(self):
        for pos, pocket, action in (
            ("CO", "8c 3d 6c 2d", "fold"),
            ("HJ", "6c 5d 8c Jd", "raise"),
        ):
            with self.subTest(opener=pos):
                guide = advice_at(
                    view(
                        pocket,
                        "BTN",
                        prior=(move(pos, "raise", 3.5, to=3.5),),
                        owed=3.5,
                        pot=5,
                    )
                )
                self.assertIsNone(
                    guide.difference(action, 12 if action == "raise" else 0)
                )
                self.assertIn("41.2%", guide.why)

    def test_nut_check_matches_direct_combinations_with_blockers_and_ties(self):
        for board, hole in (
            ("7s 8s 9d", "Th Jd 2c 3h"),
            ("7s 8s 9s 2c", "As Ks Ah Kh"),
            ("Qh Qd 4s 4h 2s", "Qc 4d Ac As"),
            ("Qs Js Ts 2d 3c", "As Ks 4c 5c"),
        ):
            board, hole = tuple(board.split()), tuple(hole.split())
            rank, kicks, _, _ = best_plo_hand(hole, board)
            remaining = [
                r + s
                for r in "23456789TJQKA"
                for s in "cdhs"
                if r + s not in (*board, *hole)
            ]
            target = (rank, tuple(kicks))

            def better(cards, target=target):
                category, tie, _ = eval_5(cards)
                return (category, tuple(tie)) > target

            expected = not any(
                better((*p, *b))
                for p in combinations(remaining, 2)
                for b in combinations(board, 3)
            )
            self.assertEqual(nuts(hole, board, *target), expected)

    def test_real_demo_sweep_covers_both_rooms_and_manual_decisions(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            load_demo(db)
            result = audit_library(db)
        self.assertEqual(result["violations"], {})
        self.assertEqual(len(result["counts"]), 8)
        for row in result["counts"]:
            self.assertEqual(row["decisions"], row["rankable"] + row["review_only"])
            self.assertLessEqual(row["differences"], row["rankable"])
