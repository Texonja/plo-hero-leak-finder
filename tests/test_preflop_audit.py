"""Preflop guide boundaries and opposite-action regressions on public replay prefixes."""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from leakfinder.importers import Record, decode
from leakfinder.preflop_guide import at_decision, hand_group
from leakfinder.replay_view import snapshot_from_record
from leakfinder.statistics import tags
from leakfinder.strategy_guide import advice_at, preflop_class
from tests.test_strategy_guide import move, view


class PreflopAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "pilots/btn_co/hands.json"
        cls.original = {}
        for hand in json.loads(path.read_text(encoding="utf-8"))["hands"]:
            rec = Record(
                hand["room"], hand["source"], hand["source_locator"], hand["id"]
            )
            cls.original[hand["id"]] = snapshot_from_record(
                rec, decode(rec).hint, hand["action_ref"]
            )

    def assert_manual(self, snap):
        finding = advice_at(snap)
        self.assertEqual(finding.choices, ())
        for action in ("fold", "call", "check", "raise"):
            self.assertIsNone(finding.difference(action, snap.bb))
        return finding

    def test_ten_original_prefixes_and_alternative_actions_in_both_rooms(self):
        ranked = {"9000004808": "fold", "9000010639": "fold"}
        self.assertEqual(
            {s.room for s in self.original.values()}, {"CoinPoker", "iPoker"}
        )
        for number, snap in self.original.items():
            with self.subTest(number=number):
                pilot = at_decision(snap)
                self.assertIsNotNone(pilot)
                if number in ranked:
                    self.assertEqual(pilot.play.lower(), ranked[number])
                    self.assertEqual(advice_at(snap).choices, (ranked[number],))
                    self.assertIsNone(advice_at(snap).difference("fold", 0))
                    self.assertEqual(
                        advice_at(snap).difference("call", snap.to_call), "action"
                    )
                else:
                    self.assertEqual(pilot.play, "Manual review")
                    self.assert_manual(snap)

    def test_suit_quality_tripled_ranks_and_adjacent_ranks(self):
        for number in ("9000006096", "9000006911"):
            base = self.original[number]
            for cards, group, action in (
                ("As Ah Ks Kh", "aces", "raise"),
                ("As Ah 2s 7s", "other_aces", None),
                ("As Ah Ad Ks", "trips", None),
                ("2s 2h 2d Ks", "trips", None),
                ("Qs Js Th 9h", "highrun", "raise"),
                ("9s 8s 7h 6h", "run", None),
                ("Qd 7h 6s 4c", "rainbow", "fold"),
                ("Qd 7h 6s 5c", "review", None),
                ("Ad 5h 4s 9c", "review", None),
                ("As 2s 5d 9d", "connected_ds", None),
                ("Ah Qh Qs 2s", "queens", None),
                ("Ah Qh Qs Ts", "queens", None),
                ("As Kh Ts 4d", "ak_split", None),
            ):
                with self.subTest(room=base.room, cards=cards):
                    snap = replace(base, hero_cards=tuple(cards.split()))
                    self.assertEqual(
                        preflop_class(snap.hero_cards, snap.to_call / snap.bb), group
                    )
                    if action:
                        self.assertIn(action, advice_at(snap).choices)
                    else:
                        self.assert_manual(snap)
            self.assertNotIn(
                "disconnected low",
                at_decision(replace(base, hero_cards=("As", "Kh", "Ts", "4d"))).title,
            )

    def test_limpers_and_blind_options_depend_on_action_order(self):
        sb_limp = [move("SB", "call", 0.5)]
        self.assertEqual(
            advice_at(view("As Ah Ks Kh", "BB", prior=sb_limp, owed=0)).choices,
            ("raise",),
        )
        for cards in ("7c 7d Kh Tc", "Qd 7h 6s 4c", "As Ah 2s 7s"):
            self.assertIn(
                "free flop",
                self.assert_manual(view(cards, "BB", prior=sb_limp, owed=0)).why,
            )
        opened = move("CO", "raise", 3, to=3)
        after = [opened, move("BTN", "call", 3)]
        before = [move("HJ", "call", 1), opened]
        self.assertEqual(
            advice_at(
                view("As Ah Ks Kh", "SB", prior=after, owed=2.5, pot=7.5)
            ).choices,
            ("raise",),
        )
        self.assert_manual(view("As Ah Ks Kh", "SB", prior=before, owed=2.5, pot=5.5))
        self.assert_manual(view("As Ah Ks Kh", "HJ", prior=before, owed=2, pot=5.5))

    def test_stack_table_and_ante_scope_on_both_real_prefixes(self):
        for number in ("9000004808", "9000010639"):
            base = self.original[number]
            self.assertEqual(advice_at(base).choices, ("fold",))
            for depth in (18, 39, 151):
                seats = tuple(
                    replace(s, stack=depth * base.bb - s.committed) for s in base.seats
                )
                self.assert_manual(replace(base, seats=seats))
            # 50 BB fits the general guide but falls outside the BTN/CO pilot.
            middle = tuple(
                replace(s, stack=50 * base.bb - s.committed) for s in base.seats
            )
            self.assert_manual(replace(base, seats=middle))
            for depth in (60, 150):
                inside = tuple(
                    replace(s, stack=depth * base.bb - s.committed) for s in base.seats
                )
                self.assertEqual(
                    advice_at(replace(base, seats=inside)).choices, ("fold",)
                )
            self.assertIsNone(at_decision(replace(base, can_raise=False)))
            self.assert_manual(replace(base, can_raise=False))
            altered = tuple(
                replace(a, amount=a.amount - 1) if a.action == "raise" else a
                for a in base.history
            )
            self.assertIsNone(at_decision(replace(base, history=altered)))
            self.assert_manual(replace(base, history=altered))
            self.assertIsNone(advice_at(replace(base, board=("As",))))
            oversized_ante = tuple(
                replace(a, amount=base.bb) if a.action == "posts_ante" else a
                for a in base.history
            )
            if not any(a.action == "posts_ante" for a in base.history):
                oversized_ante = (move(base.hero, "posts_ante", base.bb), *base.history)
            self.assert_manual(replace(base, history=oversized_ante))
            self.assertIsNone(
                at_decision(replace(base, hero_cards=(*base.hero_cards, "As")))
            )
        normal = view("As Ah Ks Kh", "SB")
        hu = replace(
            normal,
            seats=tuple(
                replace(s, position="BTN/SB") if s.is_hero else s
                for s in normal.seats
                if s.position in {"SB", "BB"}
            ),
        )
        self.assertIn("Table size", self.assert_manual(hu).title)

    def test_price_position_and_cold_fourbet_do_not_inherit_single_raiser_rule(self):
        opener = move("HJ", "raise", 2.5, to=2.5)
        bb = view("Ks Qs Jd Th", "BB", prior=[opener], owed=1.5, pot=4)
        advice = self.assert_manual(bb)
        self.assertIn("HJ opened to 2.5 BB", advice.why)
        self.assertIn("27.3%", advice.why)
        self.assertEqual(
            advice_at(replace(bb, hero_cards=("As", "Ah", "Ks", "Kh"))).play, "3-bet"
        )
        large = move("HJ", "raise", 5, to=5)
        self.assert_manual(view("As Ah Ks Kh", "BB", prior=[large], owed=4, pot=7))
        opens = [move("UTG", "fold"), move("HJ", "fold"), move("CO", "raise", 3, to=3)]
        folds = [move("SB", "fold"), move("BB", "fold")]
        standard = [*opens, move("BTN", "raise", 12, to=12), *folds]
        small = [*opens, move("BTN", "raise", 5, to=5), *folds]
        self.assertEqual(
            advice_at(view("As Ah Ks Kh", "CO", prior=standard, owed=9, pot=16.5)).play,
            "4-bet",
        )
        self.assertEqual(
            advice_at(view("As Ah Ks Kh", "CO", prior=small, owed=2, pot=9.5)).choices,
            ("call", "raise"),
        )
        cold = [*opens, move("BTN", "raise", 12, to=12), move("SB", "raise", 40, to=40)]
        self.assert_manual(view("As Ah Ks Kh", "BB", prior=cold, owed=39, pot=56))

    def test_open_size_boundary_and_blind_steal_exceptions(self):
        cards = ("Ks", "Jc", "Qc", "3h")
        for size in (2, 2.5):
            self.assertEqual(hand_group(cards, size), "small_open")
        for size in (0, 1.9, 2.51):
            self.assertEqual(hand_group(cards, size), "review")
        self.assertEqual(advice_at(view("Qd 7h 6s 4c", "UTG")).choices, ("fold",))
        for pos in ("CO", "BTN", "SB"):
            self.assert_manual(view("Qd 7h 6s 4c", pos))

    def test_broadway_plus_wheel_card_describes_the_actual_action(self):
        cards = "Ad Kh Jd 2h"
        self.assertIn("Disconnected", tags(cards.split()))
        first_in = self.assert_manual(view(cards, "SB"))
        facing = self.assert_manual(
            view(cards, "SB", prior=[move("CO", "raise", 3, to=3)], owed=2.5, pot=4.5)
        )
        limped = self.assert_manual(
            view(cards, "SB", prior=[move("CO", "call", 1)], owed=0.5, pot=2.5)
        )
        for guide in (first_in, facing, limped):
            self.assertNotIn("Disconnected", guide.why)
            self.assertIn("A-K-J", guide.why)
        self.assertIn("Opening", first_in.why)
        self.assertIn("CO raised", facing.why)
        self.assertIn("35.7%", facing.why)
        self.assertIn("limpers", limped.why)
        self.assertNotIn("Opening", facing.why)
        self.assertNotIn("Opening", limped.why)

    def test_study_defaults_depend_on_structure_and_price_and_do_not_rank(self):
        for cards in ("Ad Td Js 9s", "Ah Qh Jc Tc"):
            first_in = self.assert_manual(view(cards, "UTG"))
            self.assertIn("Raising is a reasonable starting point", first_in.why)
        for cards in ("Ad Td Js 9c", "Ad Kh Jd 2h"):
            self.assertNotIn(
                "Raising is a reasonable starting point",
                self.assert_manual(view(cards, "UTG")).why,
            )
        iso = [move("HJ", "call", 1), move("CO", "raise", 4.5, to=4.5)]
        low_trips = view("3c 3s 8s 3d", "SB", prior=iso, owed=4, pot=7)
        self.assertIn(
            "Folding is a reasonable starting point", self.assert_manual(low_trips).why
        )
        for alternative in (
            replace(low_trips, hero_cards=("Ac", "As", "8s", "Ad")),
            replace(low_trips, hero_cards=("3c", "3s", "5s", "3d")),
            replace(low_trips, pot=11),
        ):
            self.assertNotIn(
                "Folding is a reasonable starting point",
                self.assert_manual(alternative).why,
            )
        callers = [move("HJ", "raise", 3.5, to=3.5), move("CO", "call", 3.5)]
        weak = view("Jh 8s 4c 2d", "SB", prior=callers, owed=3, pot=8.5)
        self.assertIn(
            "Folding is a reasonable starting point", self.assert_manual(weak).why
        )
        for alternative in (
            replace(weak, pot=12),
            replace(weak, hero_cards=("Jh", "8s", "4h", "2s")),
        ):
            self.assertNotIn(
                "Folding is a reasonable starting point",
                self.assert_manual(alternative).why,
            )
