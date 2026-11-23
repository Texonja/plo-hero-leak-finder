"""Exhaustive PLO4 card facts and a regression from the review screen."""

import unittest
from collections import Counter
from itertools import combinations, pairwise

from leakfinder.statistics import tags
from leakfinder.strategy_guide import advice_at, preflop_class
from tests.test_strategy_guide import view


class PocketAudit(unittest.TestCase):
    def test_two_pairs_stay_out_of_unvalidated_action_rules(self):
        for cards in ("8c 6s 6d 8h", "8c 6c 6h 8h", "Ac Ad Ah As"):
            advice = advice_at(view(cards, "UTG"))
            self.assertFalse(advice.choices)
            self.assertNotIn("unpaired", advice.why.lower())
        self.assertEqual(advice_at(view("Ah Ac Kh Kc", "UTG")).choices, ("raise",))
        self.assertNotIn("larger open", advice_at(view("Jh 9d Ac 3c", "CO")).why)

    def test_every_legal_four_card_holding(self):
        ranks = "23456789TJQKA"
        deck = [r + s for r in ranks for s in "cdhs"]
        windows = [set("A2345")] + [set(ranks[i : i + 5]) for i in range(9)]
        sequence = "A23456789TJQKA"
        spans = {
            n: [set(sequence[i : i + n]) for i in range(len(sequence) - n + 1)]
            for n in range(4, 8)
        }
        shapes = {
            (4,): "Four of a suit",
            (1, 3): "Three of a suit",
            (2, 2): "Double suited",
            (1, 1, 2): "Single suited",
            (1, 1, 1, 1): "Rainbow",
        }
        totals = Counter()
        for cards in combinations(deck, 4):
            rc = Counter(c[0] for c in cards)
            sc = Counter(c[1] for c in cards)
            labels = set(tags(cards))
            shape = shapes[tuple(sorted(sc.values()))]
            group = preflop_class(cards, 2.5)
            pairs = sum(n == 2 for n in rc.values())
            expected = {shape}
            if len(rc) < 4:
                expected.add("Paired")
            if pairs == 2:
                expected.add("Double paired")
            if max(rc.values()) >= 3:
                expected.add("Trips or quads")
            if rc["A"] >= 2:
                expected.add("AAxx")
            if rc["A"] == 2 and shape == "Double suited" and set(rc) <= set("TJQKA"):
                expected.add("Premium AAxx")
            if any(r == "A" and sc[s] >= 2 for r, s in cards):
                expected.add("Suited ace")
            if len(rc) == 4:
                for width, name in (
                    (4, "Four-card rundown"),
                    (5, "One total gap"),
                    (6, "Two total gaps"),
                ):
                    if any(rc.keys() <= w for w in spans[width]):
                        expected.add(name)
                        break
                else:
                    low = sorted("A23456789TJQK".index(r) for r in rc)
                    high = sorted(ranks.index(r) for r in rc)
                    if all(
                        v[-1] - v[0] >= 8
                        and sum(b == a + 1 for a, b in pairwise(v)) <= 1
                        for v in (low, high)
                    ):
                        expected.add("Disconnected")
            self.assertEqual(labels, expected, cards)
            if group == "rainbow":
                self.assertTrue(len(sc) == len(rc) == 4, cards)
                self.assertFalse(any(rc.keys() <= w for w in windows), cards)
            if group == "connected_ds":
                self.assertTrue(
                    shape == "Double suited"
                    and any(len(w & rc.keys()) >= 3 for w in windows),
                    cards,
                )
            if pairs == 2 and shape == "Rainbow":
                self.assertIn(group, ("aces", "other_aces", "double_pair"), cards)
                totals["double-paired rainbow"] += 1
            totals["hands"] += 1
        self.assertEqual(totals, {"hands": 270725, "double-paired rainbow": 468})
