import json
import unittest
from dataclasses import replace
from pathlib import Path

from leakfinder.engine import replay
from leakfinder.importers import Record, decode
from leakfinder.preflop_critique import review_preflop
from leakfinder.replay_view import snapshot_from_record
from leakfinder.strategy_guide import advice_at
from tests.test_strategy_guide import move, view


def review(snap, action, amount=0):
    return review_preflop(snap, move(snap.hero, action, amount), advice_at(snap))


class PreflopCritiqueTests(unittest.TestCase):
    def test_original_btn_vs_co_choices_on_both_networks(self):
        corpus = Path(__file__).resolve().parents[1] / "pilots/btn_co/hands.json"
        rooms = set()
        for row in json.loads(corpus.read_text(encoding="utf-8"))["hands"]:
            rec = Record(row["room"], row["source"], row["source_locator"], row["id"])
            hand = decode(rec)
            event = next(
                e for e in replay(hand)[1]["Preflop"] if e.ref == row["action_ref"]
            )
            snap = snapshot_from_record(rec, hand.hint, event.ref)
            with self.subTest(room=snap.room, ref=event.ref, cards=snap.hero_cards):
                self.assertEqual(event.action, row["action"])
                self.assertIsNone(review_preflop(snap, event, advice_at(snap)))
                # Change only the pocket and action to exercise both adapters on the same line.
                weak = replace(snap, hero_cards=("Qh", "9d", "5s", "2c"))
                cold_call = review(weak, "call", weak.to_call)
                if row["id"] in {"9000002888", "9000012000"}:
                    # Deep effective stack against a live blind, or a 2 BB open.
                    self.assertIsNone(cold_call)
                else:
                    self.assertEqual(cold_call.rule, "preflop_weak_cold_call")
                self.assertIsNone(review(weak, "fold"))
            rooms.add(snap.room)
        self.assertEqual(rooms, {"CoinPoker", "iPoker"})

    def test_passed_raises_only_with_strength_across_all_four_cards(self):
        for cards in ("As 9c Js Tc", "As Jh Kh Th", "Kh Kc Qd Jd", "Qs Js Ts 9s"):
            snap = view(cards, "CO")
            with self.subTest(cards=cards):
                # Four spades remove too many flush cards to qualify as double-suited.
                if cards == "Qs Js Ts 9s" or cards == "As Jh Kh Th":
                    self.assertIsNone(review(snap, "fold"))
                else:
                    self.assertEqual(
                        review(snap, "fold").rule, "preflop_fold_supported_open"
                    )
                self.assertIsNone(review(snap, "raise", 3))
        for cards in ("8h 2c 4c 5h", "6d 2s 2d 3s", "Kh Kc 7d 2h", "As Kd Ts 2s"):
            with self.subTest(cards=cards):
                self.assertIsNone(review(view(cards, "CO"), "fold"))

    def test_one_limper_check_is_free_but_can_give_up_value(self):
        prior = [move(p, "fold") for p in ("UTG", "HJ", "CO")]
        prior += [move("BTN", "call", 1), move("SB", "fold")]
        snap = view("Qd Ks Jd Kc", "BB", prior=prior, owed=0, pot=2.5)
        idea = review(snap, "check")
        self.assertEqual(idea.rule, "preflop_checked_supported_limp")
        self.assertIn("BTN", idea.reason)
        self.assertIn("free flop", idea.counterargument)
        self.assertIsNone(review(snap, "raise", 3.5))
        self.assertIsNone(
            review(replace(snap, hero_cards=("Kh", "Kc", "7h", "2c")), "check")
        )
        multiway = [move("CO", "call", 1), move("BTN", "call", 1)]
        self.assertIsNone(
            review(view("Qd Ks Jd Kc", "BB", prior=multiway, owed=0), "check")
        )

    def test_price_line_and_connectivity_matter(self):
        opened = [move("HJ", "raise", 3, to=3)]
        costly = view("8c 6c 3d 2d", "BTN", prior=opened, owed=3, pot=4.5)
        problem = review(costly, "call", 3)
        self.assertEqual(problem.rule, "preflop_weak_cold_call")
        self.assertIn("40.0%", problem.reason)
        self.assertIsNone(review(costly, "fold"))
        self.assertEqual(review(costly, "raise", 10).rule, "preflop_weak_reraise")
        self.assertIsNone(
            review(replace(costly, hero_cards=("8c", "7c", "6d", "5d")), "raise", 10)
        )
        self.assertIsNone(
            review(view("8c 6c 3d 2d", "BB", prior=opened, owed=2, pot=4.5), "call", 2)
        )
        self.assertIsNone(review(replace(costly, pot=9), "call", 3))
        self.assertIsNone(review(replace(costly, can_raise=False), "call", 3))
        for changes in ({"stack": 20}, {"all_in": True}):
            seats = tuple(
                replace(s, **changes) if s.name == "HJ" else s for s in costly.seats
            )
            self.assertIsNone(review(replace(costly, seats=seats), "call", 3))

    def test_threebet_defense_and_opposite_line(self):
        prior = [move(p, "fold") for p in ("UTG", "HJ")]
        prior += [
            move("CO", "raise", 3, to=3),
            move("BTN", "raise", 10.5, to=10.5),
            move("SB", "fold"),
            move("BB", "fold"),
        ]
        snap = view("2h 5s 9d Qc", "CO", prior=prior, owed=7.5, pot=15)
        idea = review(snap, "call", 7.5)
        self.assertEqual(idea.rule, "preflop_weak_threebet_defense")
        self.assertIn("33.3%", idea.reason)
        self.assertIsNone(review(snap, "fold"))
        self.assertIsNone(review(replace(snap, to_call=3.5), "call", 3.5))

    def test_early_entry_is_not_applied_to_steals(self):
        for action in ("call", "raise"):
            self.assertEqual(
                review(view("Qd 9h 5s 2c", "UTG"), action).rule,
                "preflop_early_weak_entry",
            )
            self.assertIsNone(review(view("Qd 9h 5s 2c", "CO"), action))
        self.assertIsNone(review(view("Qd 9h 5s 2c", "UTG"), "fold"))

    def test_fivebet_caps_uncalled_chips_in_conditional_price(self):
        prior = [move(p, "fold") for p in ("UTG", "HJ")]
        prior += [
            move("CO", "raise", 3, to=3),
            move("BTN", "raise", 10, to=10),
            move("SB", "fold"),
            move("BB", "fold"),
            move("CO", "raise", 27, to=30),
        ]
        snap = view("Kh Kc Qs Jc", "BTN", prior=prior, owed=20, pot=41.5)
        seats = tuple(replace(s, stack=50) if s.name == "CO" else s for s in snap.seats)
        snap = replace(snap, seats=seats)
        idea = review(snap, "raise", 90)
        self.assertEqual(idea.rule, "preflop_nonace_fivebet")
        self.assertIn("another 70 BB", idea.reason)
        self.assertIn("43.3%", idea.reason)
        self.assertIsNone(review(snap, "call", 20))
        self.assertIsNone(
            review(replace(snap, hero_cards=("Ah", "Ac", "Qs", "Jc")), "raise", 90)
        )
        partial = review(snap, "raise", 49)
        self.assertIn("21 BB effective", partial.reason)
        self.assertNotIn("showdown share", partial.reason)

    def test_opener_can_reraise_after_a_cold_threebet_call(self):
        prior = [move("UTG", "raise", 3, to=3), move("HJ", "raise", 9, to=9)]
        snap = view("Qh 9c 2s 3s", "BTN", prior=prior, owed=9, pot=13.5)
        idea = review(snap, "call", 9)
        self.assertEqual(idea.rule, "preflop_cold_threebet_exposed")
        self.assertIn("UTG opener", idea.reason)
        self.assertIsNone(
            review(replace(snap, hero_cards=("Qh", "9h", "2s", "3s")), "call", 9)
        )
        self.assertIsNone(review(snap, "fold"))

    def test_blind_fold_needs_small_price_closed_action_and_supported_cards(self):
        prior = [move(p, "fold") for p in ("UTG", "HJ", "CO", "BTN")]
        prior += [move("SB", "raise", 1.5, to=2)]
        snap = view("Ad Jd Ts 4c", "BB", prior=prior, owed=1, pot=3)
        idea = review(snap, "fold")
        self.assertEqual(idea.rule, "preflop_discounted_bb_fold")
        self.assertIn("25.0%", idea.reason)
        self.assertIn("You would have postflop position", idea.reason)
        self.assertIsNone(review(snap, "call", 1))
        self.assertIsNone(review(replace(snap, to_call=2), "fold"))
        self.assertIsNone(
            review(replace(snap, hero_cards=("Ad", "Jh", "2c", "7h")), "fold")
        )

    def test_overlimp_needs_aces_position_and_a_single_limp(self):
        snap = view("As Ah 7c 9h", "BTN", prior=[move("CO", "call", 1)])
        self.assertEqual(review(snap, "call", 1).rule, "preflop_overlimped_aces")
        self.assertIsNone(review(snap, "raise", 3.5))
        self.assertIsNone(
            review(replace(snap, hero_cards=("As", "Ah", "Ad", "9h")), "call", 1)
        )
