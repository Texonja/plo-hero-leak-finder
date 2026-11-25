import json
import sqlite3
import unittest
from contextlib import closing

from leakfinder.analysis import describe
from leakfinder.bet_sizing import bet_size_key
from leakfinder.demo import load_demo
from leakfinder.engine import replay
from leakfinder.statistics import observations, sizing
from leakfinder.storage import connect, initialize
from tests.test_engine import decoded, paired


class BetSizingTests(unittest.TestCase):
    def test_each_inclusive_boundary_in_matching_and_reports(self):
        for percent, lower, upper, lower_key, upper_key in [
            (33, "≤33% pot", ">33–50% pot", "bet-0", "bet-1"),
            (50, ">33–50% pot", ">50–75% pot", "bet-1", "bet-2"),
            (75, ">50–75% pot", ">75–100% pot", "bet-2", "bet-3"),
            (100, ">75–100% pot", ">100% pot", "bet-3", "bet-4"),
        ]:
            for offset, label, key in [
                (-1, lower, lower_key),
                (0, lower, lower_key),
                (1, upper, upper_key),
            ]:
                with self.subTest(percent=percent, offset=offset):
                    fraction = (percent * 100 + offset) / 10000
                    self.assertEqual(sizing(fraction), label)
                    self.assertEqual(bet_size_key(fraction), key)
        self.assertEqual(sizing(None), "No bet")

    def test_recorded_half_pot_and_one_cent_more_in_both_adapters(self):
        for amount, expected_key, label in [
            (1.50, "bet-1", ">33–50% pot"),
            (1.51, "bet-2", ">50–75% pot"),
        ]:
            for hand in decoded(
                paired([[("A", "check", 0), ("B", "bet", amount), ("A", "fold", 0)]])
            ):
                with self.subTest(room=hand.room, amount=amount):
                    decisions, events = replay(hand)
                    (response,) = [
                        d
                        for d in decisions
                        if d.node == "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB"
                    ]
                    key = json.loads(response.context)[10]
                    (row,) = [
                        r
                        for r in observations(hand, decisions, events)
                        if r[1] == response.event.ref
                    ]
                    self.assertEqual(key, expected_key)
                    self.assertEqual(sizing(row[5]), label)
                    self.assertIn(label, describe(response.context))

    def test_v2_demo_upgrade_atomic_idempotent_and_matches_fresh_import(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            load_demo(db)
            query = "SELECT rowid,cell FROM decisions ORDER BY rowid"
            fresh = [tuple(r) for r in db.execute(query)]
            origins = [
                tuple(r)
                for r in db.execute(
                    "SELECT source,locator,raw FROM origins ORDER BY rowid"
                )
            ]
            restored_rooms = set()
            old = []
            priced = db.execute("""SELECT h.room,d.rowid,d.cell,o.faced FROM decisions d
                JOIN hands h ON h.id=d.hand
                LEFT JOIN obs o ON o.hand=d.hand AND o.actor=d.actor AND o.ref=d.ref
                WHERE d.cell LIKE '%"bet-%'""")
            for row in priced:
                cell = json.loads(row["cell"])
                old_key = "bet-" + str(
                    sum(
                        row["faced"] >= threshold
                        for threshold in (0.34, 0.55, 0.80, 1.01)
                    )
                )
                if old_key != cell[10]:
                    restored_rooms.add(row["room"])
                    cell[10] = old_key
                    old.append((json.dumps(cell, separators=(",", ":")), row["rowid"]))
            self.assertEqual(restored_rooms, {"CoinPoker", "iPoker"})
            with db:
                db.executemany("UPDATE decisions SET cell=? WHERE rowid=?", old)
                db.execute(
                    "INSERT INTO review_comparison_cache VALUES('Both','v2',X'FF')"
                )
                db.execute(
                    "INSERT INTO review_guide_cache VALUES('Both','guide',X'AA')"
                )
                db.execute("PRAGMA user_version=2")
            # A failed migration must keep both the old contexts and the cache
            # version until the entire upgrade can be retried.
            db.execute("""CREATE TEMP TRIGGER fail_clear BEFORE DELETE ON review_comparison_cache
                BEGIN SELECT RAISE(ABORT,'injected migration failure'); END""")
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "injected migration failure"
            ):
                initialize(db)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                db.execute(
                    "SELECT cell FROM decisions WHERE rowid=?", (old[0][1],)
                ).fetchone()[0],
                old[0][0],
            )
            db.execute("DROP TRIGGER fail_clear")
            initialize(db)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual([tuple(r) for r in db.execute(query)], fresh)
            self.assertEqual(
                [
                    tuple(r)
                    for r in db.execute(
                        "SELECT source,locator,raw FROM origins ORDER BY rowid"
                    )
                ],
                origins,
            )
            self.assertIsNone(
                db.execute("SELECT 1 FROM review_comparison_cache").fetchone()
            )
            self.assertEqual(
                db.execute("SELECT payload FROM review_guide_cache").fetchone()[0],
                b"\xaa",
            )
            with db:
                db.execute(
                    "INSERT INTO review_comparison_cache VALUES('Both','v3',X'FF')"
                )
            before = db.total_changes
            initialize(db)
            self.assertEqual(db.total_changes, before)
