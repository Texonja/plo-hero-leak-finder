"""Source censoring, chronological cutoffs and supported decision changes."""

import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

from leakfinder.demo import load_demo
from leakfinder.opponent_history import (
    Observation,
    OpponentHistory,
    apply_history,
    betting_limits,
    clock,
    context,
    heads_up_price,
    observations,
    river_opponent,
)
from leakfinder.postflop_value import bet_size
from leakfinder.range_review import build_range_review
from leakfinder.replay_view import load_snapshot
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import advice_at


class HistoricalRiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = connect(":memory:")
        cls.addClassCleanup(cls.db.close)
        initialize(cls.db)
        load_demo(cls.db)

    def take(self, room, number, ref):
        hid, hero = self.db.execute(
            "SELECT id,hint FROM hands WHERE room=? AND number=?", (room, number)
        ).fetchone()
        return load_snapshot(self.db, hid, hero, ref)

    def stub(self, snap, outcome, day, category=None, size=None, key=0):
        opp = river_opponent(snap)
        date = clock(snap.stamp) - timedelta(days=day)
        if outcome == "bet":
            wager = next(
                a.amount
                for a in snap.history
                if a.street == "River" and a.action == "bet"
            )
            pot = snap.pot - wager
        else:
            wager, pot = size, snap.pot
        return Observation(
            5000 + key,
            f"test-{key}",
            snap.room,
            opp.name,
            "Action 1",
            date,
            date + timedelta(minutes=1),
            context(snap, opp.name, wager, pot),
            outcome,
            category,
            category == 0,
        )

    def test_same_room_identity_stake_and_completed_prior_only(self):
        s = self.take("CoinPoker", "9000002844", "Line 32")
        core = self.stub(s, "bet", 1, 0)
        current = clock(s.stamp)
        rows = [
            core,
            replace(core, room="iPoker"),
            replace(core, actor="different"),
            replace(core, end=current),
            replace(core, end=current + timedelta(seconds=1)),
            replace(core, number=s.number),
            replace(core, case=(*core.case[:-1], 2)),
        ]
        result = OpponentHistory(rows).sample(s)
        self.assertEqual(result.comparable, (core,))
        self.assertEqual(result.broader, 2)

    def test_unknown_cards_widen_historical_bounds(self):
        s = self.take("CoinPoker", "9000002844", "Line 32")
        rows = [
            self.stub(s, "bet", 1, cat, key=i)
            for i, cat in enumerate((0, 5, None, None))
        ]
        sample = OpponentHistory(rows).sample(s)
        air, rival, shown, weak = betting_limits(sample, 5)
        self.assertEqual((air, shown, weak), ((0.25, 0.75), 2, 1))
        self.assertLess(rival[0], 0.25)
        self.assertEqual(rival[1], 1.0)
        self.assertEqual(heads_up_price(s), 0.25)

    def test_direction_changes_require_prior_data_and_a_clear_price(self):
        b = self.take("iPoker", "9000014007", "Action 16")
        native = advice_at(b)
        for snap, cat, change, base in (
            (b, 5, "fold", replace(native, choices=("call",))),
            (b, 0, "call", native),
        ):
            records = [self.stub(snap, "bet", 1 + i % 4, cat, key=i) for i in range(60)]
            self.assertNotEqual(base.choices, (change,))
            self.assertEqual(
                apply_history(snap, base, OpponentHistory(records[:10])), base
            )
            adjusted = apply_history(snap, base, OpponentHistory(records))
            self.assertEqual(adjusted.choices, (change,))
            self.assertFalse(adjusted.rankable)
            self.assertIsNone(adjusted.difference("raise", 12))
            unknown = [replace(row, category=None, air=False) for row in records]
            self.assertEqual(apply_history(snap, base, OpponentHistory(unknown)), base)

    def test_sample_supported_fold_rate_can_support_bluff_sizing(self):
        s = self.take("iPoker", "9000009995", "Action 13")
        opp = river_opponent(s)
        s = replace(
            s, hero_cards=("Ac", "Qd", "9s", "5d"), board=("2h", "Kc", "7h", "4c", "8c")
        )
        hero = next(x for x in s.seats if x.is_hero)
        size = bet_size(s, hero, [opp], 65)
        base = advice_at(s)
        self.assertEqual(base.choices, ("check", "bet"))
        rows = [
            self.stub(s, "fold", 1 + i % 4, size=size.amount, key=i) for i in range(60)
        ]
        adjusted = apply_history(s, base, OpponentHistory(rows))
        self.assertEqual(adjusted.choices, ("bet",))
        self.assertFalse(adjusted.rankable)
        self.assertIsNone(adjusted.difference("check", 0))
        raises = [replace(row, outcome="raise") for row in rows]
        self.assertEqual(apply_history(s, base, OpponentHistory(raises)), base)

    def test_both_real_imports_and_range_output(self):
        rows = tuple(observations(self.db, "Both"))
        self.assertEqual({r.room for r in rows}, {"CoinPoker", "iPoker"})
        self.assertTrue(all(r.end >= r.started for r in rows))
        self.assertTrue(any(r.outcome == "fold" for r in rows))
        self.assertTrue(any(r.outcome == "bet" and r.category is None for r in rows))
        with closing(self.db.execute("SELECT hand FROM cohort")) as cursor:
            next(cursor)
            self.assertEqual(tuple(observations(self.db, "Both", prepared=True)), rows)
            self.assertTrue(cursor.fetchall())
        for room, number, ref in (
            ("CoinPoker", "9000002844", "Line 32"),
            ("iPoker", "9000010026", "Action 17"),
        ):
            snap = self.take(room, number, ref)
            hero = next(s for s in snap.seats if s.is_hero)
            info = build_range_review(
                snap, hero, river_opponent(snap), OpponentHistory(rows)
            )
            self.assertIn("Preflop ·", info.text)
            self.assertIn("River ·", info.text)
            self.assertEqual(len(info.models), 3)
            self.assertEqual(info.guide, info.history)
        snap = self.take("CoinPoker", "9000005623", "Line 36")
        hero = next(s for s in snap.seats if s.is_hero)
        info = build_range_review(
            snap, hero, river_opponent(snap), OpponentHistory(rows)
        )
        self.assertFalse(
            info.models
        )  # An assumed bet/fold policy is unsuitable for nuts.
