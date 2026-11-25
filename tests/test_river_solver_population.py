"""Outcome audit keeps recorded calls, inferred intent and net winnings distinct."""

import sqlite3
import unittest
from types import SimpleNamespace

from pilots.river_solver.population import (
    FOUR,
    RAISE,
    Call,
    river_calls,
    run,
    showdown,
    source_money,
    stats,
)


class RiverCallAuditTests(unittest.TestCase):
    def test_missing_rake_explains_gap_while_source_reported_net_is_preserved(self):
        hand = SimpleNamespace(
            room="iPoker",
            reported_pot=None,
            spent={"A": 5_000_000, "B": 5_000_000},
            final_commits={"A": 5_000_000, "B": 5_000_000},
            survivors={"A", "B"},
            bb=1_000_000,
        )
        record = SimpleNamespace(
            locator="source",
            raw="<session><game><general><players>"
            '<player name="A" win="9.50" bet="5"/>'
            '<player name="B" win="0" bet="5"/>'
            "</players></general></game></session>",
        )
        missing = source_money(record, hand, "A", {})
        self.assertEqual(missing["status"], "payout_conservation_gap_no_rake_fields")
        self.assertEqual(missing["residual_bb"], 0.5)
        self.assertEqual(missing["raw_net_bb"], 4.5)
        self.assertIsNone(missing["raw_agrees_verified"])
        record.raw = record.raw.replace('win="9.50"', 'rakeamount="0.50" win="9.50"')
        verified = source_money(record, hand, "A", {"A": 4_500_000, "B": -5_000_000})
        self.assertEqual(verified["status"], "verified")
        self.assertIs(verified["raw_agrees_verified"], True)

    def test_confirmed_alias_keeps_saved_profile_and_excludes_ambiguous_seats(self):
        from leakfinder.storage import initialize

        db = sqlite3.connect(":memory:")
        try:
            initialize(db)
            with db:
                db.execute("INSERT INTO aliases VALUES('iPoker','Current')")
                db.execute(
                    "INSERT INTO hands(id,room,number,hint,conflict) VALUES(1,'iPoker','1','Past',0)"
                )
                db.executemany(
                    "INSERT INTO seats VALUES(1,?)", [("Past",), ("Current",)]
                )
            report = run(db, [("iPoker", "Past")])
            self.assertEqual(report["hands_replayed"], 0)
            self.assertEqual(report["ambiguous_multiple_alias_seats_excluded"], 1)
            self.assertEqual(report["selected_alias_counts_by_room"], {"iPoker": 2})
            self.assertEqual(
                db.execute("SELECT name FROM main.aliases").fetchall(), [("Current",)]
            )
        finally:
            db.close()

    def test_overcalls_follow_last_price_and_a_raise_is_not_a_first_bet(self):
        def move(who, action):
            return SimpleNamespace(player=who, action=action)

        river = [
            move("v", "bet"),
            move("w", "call"),
            move("h", "call"),
            move("x", "raise"),
            move("v", "fold"),
            move("w", "call"),
            move("h", "call"),
        ]
        wet = ["As", "Ks", "7s", "6s", "3c"]
        calls = list(river_calls(river, wet, "h"))
        self.assertEqual(
            [(key, aggressor.player) for key, _, aggressor in calls],
            [(FOUR, "v"), (RAISE, "x")],
        )
        self.assertEqual(
            [
                key
                for key, _, _ in river_calls(river, ["As", "Ks", "7s", "6s", "3s"], "h")
            ],
            [RAISE],
        )

    def test_losing_value_air_board_pair_tie_and_unknown(self):
        board = ["As", "Ks", "7s", "6s", "3c"]
        hero = ["Qs", "Js", "4h", "5d"]
        self.assertEqual(
            showdown(board, hero, ["Ad", "Ac", "Jd", "8h"]), ("weaker", False)
        )
        self.assertEqual(
            showdown(board, hero, ["Qd", "Th", "2c", "9c"]), ("weaker", True)
        )
        self.assertEqual(showdown(board, hero, []), ("unknown", False))
        paired = ["As", "Ac", "7s", "6s", "3c"]
        self.assertEqual(
            showdown(paired, hero, ["Qd", "Th", "2c", "9c"]), ("weaker", True)
        )
        self.assertEqual(
            showdown(
                ["8c", "7s", "4h", "2d", "Js"],
                ["6d", "5d", "Qh", "Tc"],
                ["6s", "5s", "Ad", "Ah"],
            ),
            ("tie", False),
        )

    def test_duplicate_hand_counts_multiway_and_verified_money(self):
        calls = [
            [
                Call("stronger", False, False, -12.5, None, False),
                Call("weaker", True, True, -12.5, 6.25, True),
            ],
            [Call("unknown", False, True, None, None, False)],
        ]
        data = stats(calls)
        self.assertEqual(
            (
                data["hands"],
                data["call_decisions"],
                data["multiple_matching_calls_in_hand"],
            ),
            (2, 3, 1),
        )
        self.assertEqual(
            data["by_last_matching_call"],
            {"stronger": 0, "weaker": 1, "tie": 0, "unknown": 1},
        )
        self.assertEqual(data["all_call_decisions_by_showdown"]["stronger"], 1)
        self.assertEqual(data["whole_hand_net_bb"], -12.5)
        self.assertEqual(data["realized_closing_call_vs_fold_bb"], 6.25)
        self.assertEqual(data["weak_air_proxy_subset_of_weaker"], 1)
