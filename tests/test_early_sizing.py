"""Validate history-aware sizing on native hand snapshots and controlled outcomes."""

import unittest
from dataclasses import replace
from datetime import timedelta

from leakfinder.demo import load_demo
from leakfinder.early_sizing import Estimate, historical_estimate, supported_sizing
from leakfinder.opponent_history import (
    Observation,
    OpponentHistory,
    Sample,
    clock,
    context,
    early_observations,
    postflop_opponent,
)
from leakfinder.postflop_value import bet_size
from leakfinder.range_review import build_range_review
from leakfinder.replay_view import _ledger, _source_records, load_snapshot
from leakfinder.storage import cohort, connect, initialize
from tests.test_replay_view import coin, ip


def controls(snap, outcomes):
    """Use controlled response counts for each bucket of a real replayed decision."""
    hero = next(s for s in snap.seats if s.is_hero)
    opponent = postflop_opponent(snap)
    start = clock(snap.stamp)
    rows = []
    for percent, counts in zip((33, 50, 85), outcomes, strict=True):
        amount = bet_size(snap, hero, [opponent], percent).amount
        case = context(snap, opponent.name, amount, snap.pot)
        for action, n in zip(("fold", "call", "raise"), counts, strict=True):
            for _ in range(n):
                i = len(rows)
                date = start - timedelta(days=1 + i % 4)
                rows.append(
                    Observation(
                        i,
                        f"control-{i}",
                        snap.room,
                        opponent.name,
                        "Test",
                        date,
                        date + timedelta(minutes=1),
                        case,
                        action,
                        street=snap.street,
                    )
                )
    return tuple(rows)


def review(snap, rows):
    hero = next(s for s in snap.seats if s.is_hero)
    return build_range_review(
        snap, hero, postflop_opponent(snap), OpponentHistory(rows)
    )


class EarlySizingTests(unittest.TestCase):
    def test_folds_calls_and_raises_contribute_distinct_payoffs(self):
        snap = coin(30, "Hero", "Line 28")
        row = controls(snap, ((1, 0, 0), (0, 0, 0), (0, 0, 0)))[0]
        for action, gain in (("fold", 65), ("call", 0), ("raise", -85)):
            with self.subTest(action=action):
                sample = Sample((replace(row, outcome=action),), 1)
                result = historical_estimate(100, 50, 35, (0.425,) * 3, sample)
                self.assertAlmostEqual(result.gain, gain)
                self.assertLessEqual(result.low, gain)
                self.assertGreaterEqual(result.high, gain)

    def test_real_target_snapshots_in_both_rooms_and_streets_react_to_controls(self):
        cases = (
            coin(30, "Hero", "Line 28"),
            coin(30, "Hero", "Line 31"),
            ip(601, "BigNeonMax", "Action 10"),
            ip(41, "BigNeonMax", "Action 8"),
        )
        for snap in cases:
            with self.subTest(room=snap.room, street=snap.street):
                large_fold = controls(snap, ((1, 4, 115), (1, 4, 115), (100, 15, 5)))
                result = review(snap, large_fold)
                self.assertEqual(
                    result.historical_comparison,
                    ("bet 33%", "bet in the >62.5–87.5% pot group"),
                )
                self.assertIn("fold 100 · call 15 · raise 5", result.text)
                self.assertEqual(result.text.count("fold 100 · call 15 · raise 5"), 1)
                self.assertEqual(len(result.matched_sources), 360)
                earlier = clock(snap.stamp) - timedelta(days=7)
                one_date = tuple(replace(r, started=earlier) for r in large_fold)
                sparse = review(snap, one_date)
                self.assertEqual(sparse.historical_comparison, ())
                self.assertEqual(sparse.history, sparse.guide)
                self.assertIn("Hypothetical bet/fold scenario", sparse.text)
                self.assertEqual(
                    review(snap, large_fold[0:29]).historical_comparison,
                    (),
                )
                self.assertEqual(
                    review(snap, controls(snap, ((5, 110, 5),) * 3)).history, "check"
                )
                self.assertEqual(
                    review(snap, controls(snap, ((5, 5, 110),) * 3)).history, "check"
                )

    def test_replacement_needs_both_groups_and_nonoverlapping_estimates(self):
        baseline = Estimate(33, 5, 1, 9)
        for other in (Estimate(40, 80, 70, 90), Estimate(85, 12, 8, 20)):
            self.assertEqual(supported_sizing(100, 33, (baseline, other))[0], 33)
        self.assertEqual(supported_sizing(100, 33, (Estimate(85, 12, 11, 20),))[0], 33)
        self.assertEqual(
            supported_sizing(100, 33, (baseline, Estimate(85, 19, 12, 20)))[0], 85
        )
        self.assertEqual(supported_sizing(100, 0, (Estimate(33, 3, 0, 6),))[0], 0)
        self.assertEqual(supported_sizing(100, 0, (Estimate(33, 8, 4, 12),))[0], 33)

    def test_shared_responses_do_not_select_one_price_within_a_group(self):
        small = Estimate(33, 5, 1, 9)
        # Only the 85% scenario passes. Both sizes use the same historical
        # folds, calls and raises, so the history cannot single out 85%.
        same_history = (Estimate(67, 8, 0, 13), Estimate(85, 19, 14, 22))
        self.assertEqual(supported_sizing(100, 33, (small, *same_history))[0], 33)
        self.assertEqual(supported_sizing(100, 67, same_history)[0], 67)

    def test_opponent_history_matches_rounded_three_quarter_bets_in_both_rooms(self):
        for snap in (coin(30, "Hero", "Line 28"), ip(41, "BigNeonMax", "Action 8")):
            with self.subTest(room=snap.room):
                template = controls(snap, ((0, 0, 0), (0, 0, 0), (30, 0, 0)))
                opp = postflop_opponent(snap)
                rows = tuple(
                    replace(
                        row,
                        case=context(
                            snap, opp.name, round(snap.pot * fraction), snap.pot
                        ),
                    )
                    for row, fraction in zip(template, (0.749, 0.751) * 15, strict=True)
                )
                history = OpponentHistory(rows)
                sample = history.sample(snap, round(0.75 * snap.pot))
                self.assertEqual(len(sample.comparable), 30)
                self.assertTrue(sample.ready)
                self.assertFalse(history.sample(snap, round(0.5 * snap.pot)).comparable)

    def test_real_response_references_match_replayed_action_on_both_networks(self):
        db = connect(":memory:")
        self.addCleanup(db.close)
        initialize(db)
        load_demo(db)
        cohort(db)
        for room in ("CoinPoker", "iPoker"):
            for street in ("Flop", "Turn"):
                with self.subTest(room=room, street=street):
                    rows = tuple(
                        r
                        for r in early_observations(
                            db, room, None, street, prepared=True
                        )
                        if r.outcome != "bet"
                    )
                    self.assertTrue(rows)
                    self.assertTrue(
                        all((r.room, r.street) == (room, street) for r in rows)
                    )
                    for action in sorted({r.outcome for r in rows}):
                        row = next(r for r in rows if r.outcome == action)
                        _, events = _ledger(next(_source_records(db, row.hand)))
                        self.assertEqual(events[row.ref].action, action)
                        snap = load_snapshot(db, row.hand, row.actor, row.ref)
                        prefix = [a for a in snap.history if a.street == street]
                        self.assertEqual([a.action for a in prefix], ["check", "bet"])
                        self.assertEqual(prefix[0].name, row.actor)
