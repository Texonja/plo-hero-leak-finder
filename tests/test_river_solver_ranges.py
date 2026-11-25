"""Independent checks of full-deck likelihood, blockers and group accounting."""

import unittest
from types import SimpleNamespace

try:
    import numpy as np
except ModuleNotFoundError as exc:
    if exc.name != "numpy":
        raise
    raise unittest.SkipTest("Install NumPy to run the standalone river range tests") from exc

from leakfinder.vendor.primitives import best_plo_hand
from pilots.river_solver.model import GROUPS
from pilots.river_solver.ranges import (
    DECK,
    PROFILES,
    actions_for,
    board_features,
    likelihood,
    seat_ranges,
)


def locate(features, hole):
    code = 0
    for c in sorted(DECK.index(c) for c in hole):
        code = 52 * code + c
    return int(np.searchsorted(features[1], code))


def action(street, who, verb):
    return SimpleNamespace(street=street, name=who, action=verb)


class FullRangesTests(unittest.TestCase):
    board = ("7s", "8h", "2d", "Qc", "5s")
    hero = ("9c", "Tc", "6s", "5h")
    log = (
        action("Preflop", "Villain", "raise"),
        action("Preflop", "H", "call"),
        action("Flop", "Villain", "bet"),
        action("Flop", "H", "raise"),
        action("Flop", "Villain", "call"),
        action("Turn", "Villain", "check"),
        action("Turn", "H", "check"),
        action("River", "Villain", "bet"),
    )

    @classmethod
    def setUpClass(cls):
        cls.features = board_features(cls.board)
        cls.output = seat_ranges(cls.board, cls.hero, cls.log, "H", (500, 1500))

    def test_all_legal_holdings_and_project_evaluator(self):
        hands, codes, _, flop, turn, river, _ = self.features
        self.assertEqual(len(hands), 178365)
        self.assertTrue(np.all(np.diff(codes) > 0))
        for i in (0, 1, 43, 1200, 24315, 82449, 178364):
            h = [DECK[c] for c in hands[i]]
            self.assertEqual(len({*h, *self.board}), 9)
            for category, k in ((flop.category, 3), (turn.category, 4), (river, 5)):
                self.assertEqual(category[i], best_plo_hand(h, self.board[:k])[0])

    def test_group_sizes_mass_and_hero_singleton(self):
        _, _, pre, flop, turn, _, _ = self.features
        for n in (500, 1500):
            for profile, item in self.output[n].items():
                meta = item["metadata"]
                entries = item["entries"]
                self.assertEqual(len(entries), n)
                self.assertEqual(len({tuple(e["cards"]) for e in entries}), n)
                self.assertGreater(len({c for e in entries for c in e["cards"]}), 40)
                self.assertEqual(set(meta["composition_full"]), set(GROUPS))
                self.assertAlmostEqual(sum(meta["composition_full"].values()), 1)
                np.testing.assert_allclose(
                    list(meta["composition_full"].values()),
                    list(meta["composition_represented"].values()),
                    atol=1e-12,
                )
                weights = likelihood(
                    pre, flop, turn, actions_for(self.log, "H"), profile
                )
                h = next(e for e in entries if set(e["cards"]) == set(self.hero))
                self.assertAlmostEqual(
                    h["weight"], weights[locate(self.features, self.hero)]
                )
                self.assertAlmostEqual(
                    sum(e["weight"] for e in entries),
                    weights.sum() * (1 - meta["discarded_mass_fraction"]),
                    places=7,
                )

    def test_distinct_support_and_repeatability(self):
        seen = []
        for p in PROFILES:
            seen.append(self.output[500][p]["metadata"]["support_count"])
        self.assertLess(seen[0], seen[1])
        self.assertLess(seen[1], seen[2])
        again = seat_ranges(self.board, self.hero, self.log, "H", (500,))
        self.assertEqual(self.output[500], again[500])
        holdings = [
            {tuple(e["cards"]) for e in again[500][p]["entries"]} for p in PROFILES
        ]
        self.assertNotEqual(holdings[0], holdings[1])
        self.assertNotEqual(holdings[1], holdings[2])

    def test_wrap_against_brute_next_card(self):
        idx = locate(self.features, self.hero)
        for street, n in ((self.features[3], 3), (self.features[4], 4)):
            seen = self.board[:n]
            count = sum(
                best_plo_hand(self.hero, (*seen, c))[0] in (4, 8)
                for c in DECK
                if c not in (*seen, *self.hero)
            )
            self.assertEqual(count, 20)
            self.assertEqual(street.straight_outs[idx], count)

    def test_no_future_cards_in_arrival_likelihood(self):
        old = self.features
        new = board_features((*self.board[:4], "Ah"))
        i = locate(old, self.hero)
        j = locate(new, self.hero)
        line = actions_for(self.log, "H")
        self.assertEqual(
            line, [("Preflop", "call", 1), ("Flop", "raise", 1), ("Turn", "check", 0)]
        )
        for p in PROFILES:
            self.assertEqual(
                likelihood(*old[2:5], line, p)[i], likelihood(*new[2:5], line, p)[j]
            )
            for street in ("Flop", "Turn"):
                bare = [(street, "bet", 0)]
                self.assertAlmostEqual(
                    likelihood(*old[2:5], bare, p)[i]
                    + likelihood(*old[2:5], [(street, "check", 0)], p)[i],
                    1,
                )
                self.assertAlmostEqual(
                    sum(
                        float(likelihood(*old[2:5], [(street, a, 1)], p)[i])
                        for a in ("fold", "raise", "call")
                    ),
                    1,
                )
