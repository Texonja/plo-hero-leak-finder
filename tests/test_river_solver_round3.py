import unittest
from collections import defaultdict

try:
    from pilots.river_solver.calibration import money_summary
except ModuleNotFoundError as exc:
    if exc.name not in {"numpy", "scipy"}:
        raise
    raise unittest.SkipTest("Install the standalone river pilot requirements") from exc

from pilots.river_solver.cases import read_demo
from pilots.river_solver.model import rank
from pilots.river_solver.ranges import seat_ranges


class RiverRoundThreeTests(unittest.TestCase):
    def test_demo_shows_neutral_critique_and_sizing(self):
        demo = read_demo()
        for number in (4202, 10026):
            with self.subTest(number=number):
                advice = demo[number][2]
                self.assertEqual(
                    advice["critique"]["alternative"], "Compare calling with folding"
                )
        for number in (5983, 1429, 95):
            with self.subTest(number=number):
                advice = demo[number][2]
                self.assertIsNone(advice["size_bb"])
                self.assertIn(
                    "Compare checking with a small to larger value bet.",
                    advice["explanation"],
                )
        self.assertEqual(demo[2872][2]["critique"]["rule"], "river_check_back_top_two")

    def test_exact_showdown_strata_against_hero(self):
        import numpy as np

        from pilots.river_solver.ranges import DECK, _board_features

        board = ("Qd", "6d", "5d", "Qs", "2d")
        hero = ("Qh", "8s", "8h", "Jh")
        reference = rank(hero, board)
        weights = seat_ranges(
            board,
            (),
            (),
            "Villain",
            (500,),
            reference_cards=hero,
            stratify=True,
            profiles=("middle",),
        )
        entries = weights[500]["middle"]["entries"]
        hsum = defaultdict(float)
        for item in entries:
            h = tuple(item["cards"])
            key = (
                rank(h, board) > reference,
                rank(h, board) == reference,
                any(c.endswith("d") for c in h),
                bool(set(h) & set(hero)),
            )
            hsum[key] += item["weight"]
        hands, _, *_, scores = _board_features(board)
        ids = np.asarray([DECK.index(c) for c in hero])
        hidx = np.argmax(np.isin(hands, ids).all(axis=1))
        expected = defaultdict(int)
        flags = zip(
            scores > scores[hidx],
            scores == scores[hidx],
            (hands % 4 == 1).any(axis=1),
            np.isin(hands, ids).any(axis=1),
            strict=True,
        )
        for key in flags:
            expected[key] += 1
        self.assertEqual(set(hsum), set(expected))
        for k, n in expected.items():
            self.assertAlmostEqual(hsum[k], n, places=6)

    def test_trim_excludes_both_extremes(self):
        r = money_summary([-10.0, 0.0, 3.0, 40.0], 5)
        self.assertEqual(r["trimmed_mean_bb"], 1.5)
        self.assertEqual(r["unverified"], 5)
