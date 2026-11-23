"""The guide is a set of authored hypotheses. Tests verify identification and scope."""

import itertools
import json
import unittest
from dataclasses import replace
from pathlib import Path

from leakfinder.importers import Record, decode
from leakfinder.preflop_guide import GROUPS, at_decision, hand_group
from leakfinder.replay_view import snapshot_from_record
from pilots.btn_co.guide_report import build


class GuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(
            (
                Path(__file__).resolve().parents[1] / "pilots/btn_co/hands.json"
            ).read_text(encoding="utf-8")
        )
        cls.snapshots = {}
        for h in cls.data["hands"]:
            rec = Record(h["room"], h["source"], h["source_locator"], h["id"])
            cls.snapshots[h["id"]] = snapshot_from_record(
                rec, decode(rec).hint, h["action_ref"]
            )

    def test_examples_are_invariant_under_card_order_and_suit_renaming(self):
        for key, group in GROUPS.items():
            size = 2 if key == "small_open" else 3.5
            cards = group.example.split()
            for suits in itertools.permutations("cdhs"):
                rename = dict(zip("cdhs", suits, strict=True))
                variant = [c[0] + rename[c[1]] for c in reversed(cards)]
                self.assertEqual(hand_group(variant, size), key, variant)
            for perm in itertools.permutations(cards):
                self.assertEqual(hand_group(perm, size), key)

    def test_suit_owner_and_price_change_the_rule(self):
        self.assertEqual(hand_group(["As", "Kd", "8s", "7h"], 3.5), "ak_link")
        self.assertEqual(
            hand_group(["Ad", "Ks", "8s", "7h"], 3.5), "ak_link_lower_suit"
        )
        self.assertEqual(hand_group(["Ks", "Jc", "Qc", "3h"], 2), "small_open")
        self.assertEqual(hand_group(["Ks", "Jc", "Qc", "3h"], 3.5), "review")
        self.assertEqual(hand_group(["As", "Ac", "Ah", "3s"], 3), "review")
        self.assertEqual(hand_group(["As", "2d", "3h", "5c"], 3), "review")
        with self.assertRaises(ValueError):
            hand_group(["As", "As", "Kd", "9s"], 3.5)

    def test_original_hero_decisions_from_both_networks(self):
        expected = {
            "9000006096": "kings",
            "9000004955": "bare_ace",
            "9000004808": "rainbow",
            "9000002519": "ak_ds",
            "9000002679": "queens",
            "9000010639": "weak_suit",
            "9000002888": "review",
            "9000012000": "small_open",
            "9000008520": "bare_ace",
            "9000006911": "bare_ace",
        }
        self.assertEqual(set(self.snapshots), set(expected))
        for n, s in self.snapshots.items():
            with self.subTest(number=n):
                self.assertEqual(at_decision(s), GROUPS[expected[n]])

    def test_refuses_different_decisions_and_missing_information(self):
        s = self.snapshots["9000006911"]
        live = tuple(
            replace(p, stack=s.bb * 20) if p.position == "SB" else p for p in s.seats
        )
        for changed in [
            replace(s, hero_cards=None),
            replace(s, street="Flop"),
            replace(s, board=("Ah",)),
            replace(s, history=s.history[:-1]),
            replace(s, pot=s.pot + s.bb),
            replace(s, seats=live),
            replace(
                s, seats=tuple(replace(p, is_hero=p.position == "CO") for p in s.seats)
            ),
        ]:
            self.assertIsNone(at_decision(changed))

    def test_report_has_all_class_anchors_and_exact_hand_references(self):
        html = build(self.data)
        for key in GROUPS:
            self.assertIn(f'id="g-{key}"', html)
        for h in self.data["hands"]:
            self.assertIn(f"#{h['id']}", html)
            self.assertIn(h["action_ref"], html)
        self.assertEqual(html.count("<details"), 10)
