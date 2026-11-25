import unittest
from contextlib import closing

from leakfinder.demo import load_demo
from leakfinder.early_range import early_ranges
from leakfinder.opponent_history import postflop_opponent
from leakfinder.replay_view import load_snapshot
from leakfinder.storage import connect, initialize
from leakfinder.turn_lines import river_branch
from tests.test_early_sizing import review
from tests.test_replay_view import coin, ip


class DecisionSensitivityTests(unittest.TestCase):
    def test_call_decision_uses_public_river_and_range_not_hidden_hand(self):
        # Two equally likely hands on the SAME public river. Hero must make
        # one decision vs the bet, even though only one holding is beaten.
        rows = ((1, 0, 0, 0.5, 0.5), (1, 0, 1, 0.5, 0))
        self.assertEqual(river_branch(rows, (1,) * 5, 100, 100, 1, (1, 1)), (1, 50))
        self.assertEqual(river_branch(rows, (1,) * 5, 100, 100, 1, (0, 1)), (1, 50))
        self.assertEqual(river_branch(rows, (1,) * 5, 100, 0, 1, (1, 1)), (1, 50))

    def test_turn_runouts_and_actions_on_both_networks(self):
        for snap in (
            coin(30, "Hero", "Line 31"),
            ip(41, "BigNeonMax", "Action 8"),
            ip(601, "BigNeonMax", "Action 12"),
        ):
            point = early_ranges(
                snap, postflop_opponent(snap), count=64, current_only=True
            )[0].steps[-1]
            self.assertAlmostEqual(sum(w for _, _, _, w, _ in point.river_mass), 1)
            self.assertAlmostEqual(
                sum(e for _, _, _, _, e in point.river_mass), point.equity
            )
            output = review(snap, ()).text
            self.assertIn("WHAT WOULD CHANGE THIS DECISION?", output)
            if snap.to_call:
                self.assertIn("call vs fold", output)
            else:
                self.assertIn("bet/call", output)
                self.assertIn("More turn check-raises", output)

    def test_river_price_and_modeled_threshold_both_sources(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            load_demo(db)
            for number, ref, price in (
                ("9000002844", "Line 32", "25.0%"),
                ("9000010026", "Action 17", "29.5%"),
            ):
                hid, hero = db.execute(
                    "SELECT id,hint FROM hands WHERE number=?", (number,)
                ).fetchone()
                snap = load_snapshot(db, hid, hero, ref)
                out = review(snap, ()).text
                self.assertIn("WHAT WOULD CHANGE THIS DECISION?", out)
                self.assertIn("Break-even showdown equity " + price, out)
                if number == "9000010026":
                    self.assertIn("opening bets cannot establish", out)
                    self.assertNotIn("The guide lists Manual review", out)
                else:
                    self.assertIn("0 shown and 0 unknown", out)
                self.assertIn("Modeled betting range", out)
