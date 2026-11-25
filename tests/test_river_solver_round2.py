import os
import unittest
from collections import Counter
from unittest import mock

try:
    import numpy as np
except ModuleNotFoundError as exc:
    if exc.name != "numpy":
        raise
    raise unittest.SkipTest("Install NumPy to run the standalone river solver tests") from exc

from pilots.river_solver.model import Game
from pilots.river_solver.report import SETS, classify
from pilots.river_solver.solver import solve
from tests.test_river_solver import small


class RoundTwoTests(unittest.TestCase):
    def test_constructed_critique_boundaries_match_real_application(self):
        from pilots.river_solver.cases import case, read_demo

        demo = read_demo()
        families = (
            ("river_call_large_raise", 10026, (False, True, True, True)),
            ("river_call_four_flush_board", 4202, (False, True, True, False)),
        )
        for family, number, expected in families:
            for index, active in enumerate(expected, 200):
                with self.subTest(family=family, variant=index):
                    _, _, audit = case(family, number, index, demo[number])
                    self.assertEqual(audit["source"], "constructed")
                    self.assertEqual(audit["rule_active"], active)

    def test_active_denominator_includes_inconclusive(self):
        self.assertEqual(
            classify(Counter(supports=8, close=1, indeterminate=1)), SETS[0]
        )
        self.assertEqual(classify(Counter(supports=9, opposes=1)), SETS[2])
        self.assertEqual(
            classify(Counter(supports=3, opposes=4, indeterminate=3)), SETS[1]
        )
        self.assertEqual(
            classify(Counter(supports=1, opposes=3, indeterminate=6)), SETS[2]
        )
        self.assertEqual(classify(Counter(supports=4, opposes=4, close=2)), SETS[2])

    def test_accelerated_terminals_match_dense_card_removal(self):
        if not os.getenv("RIVER_TERMINAL_LIBRARY"):
            # Default installation uses the original numerical backend.
            self.assertIsNone(Game(small()).products)
            return
        game = Game(small())
        for seat, op in enumerate(game.products):
            width = len(game.hands[1 - seat])
            probes = [np.arange(width, dtype=float), *np.eye(width)]
            for reach in probes:
                np.testing.assert_allclose(
                    op(reach, False), game.legal[seat] @ reach, atol=1e-12
                )
                np.testing.assert_allclose(
                    op(reach, True), game.show[seat] @ reach, atol=1e-12
                )
        fast, _ = solve(small(), iterations=300)
        with mock.patch.dict(os.environ, {"RIVER_TERMINAL_LIBRARY": ""}):
            plain, _ = solve(small(), iterations=300)
        np.testing.assert_allclose(fast["game_ev_bb"], plain["game_ev_bb"], atol=1e-10)
        np.testing.assert_allclose(
            fast["best_response_ev_bb"], plain["best_response_ev_bb"], atol=1e-10
        )
