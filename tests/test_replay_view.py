import gzip
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from leakfinder.importers import Record, money
from leakfinder.replay_view import (
    available_actions,
    chip_text,
    load_snapshot,
    snapshot_from_record,
)
from leakfinder.storage import connect, import_file, initialize
from tests.test_engine import FIX, original


def coin(number, hero, ref):
    return snapshot_from_record(
        Record("CoinPoker", original("coin", number), "Text hand 1"), hero, ref
    )


def ip(number, hero, ref):
    return snapshot_from_record(
        Record("iPoker", original("ip", number), "HM2 row 1"), hero, ref
    )


class ReplayViewTests(unittest.TestCase):
    def test_audited_source_decisions_conserve_chips_without_future_actions(self):
        audit = json.loads(gzip.decompress((FIX / "source-audit.json.gz").read_bytes()))
        counts = {"CoinPoker": 0, "iPoker": 0}
        for row in audit:
            record = Record(
                row["room"],
                row["raw"],
                "HM2 row 1" if row["room"] == "iPoker" else "Text hand 1",
            )
            for ref, actor, _ in row["expected"]:
                with self.subTest(room=row["room"], number=row["number"], ref=ref):
                    view = snapshot_from_record(record, actor, ref)
                    counts[row["room"]] += 1
                    self.assertEqual(
                        view.pot,
                        sum(
                            -a.amount if a.action == "return" else a.amount
                            for a in view.history
                        ),
                    )
                    self.assertEqual(
                        len(view.board),
                        {"Preflop": 0, "Flop": 3, "Turn": 4, "River": 5}[view.street],
                    )
                    self.assertTrue(
                        all(
                            int(a.ref.split()[1]) < int(ref.split()[1])
                            for a in view.history
                        )
                    )
                    self.assertEqual(sum(s.is_hero for s in view.seats), 1)
        self.assertEqual(counts, {"CoinPoker": 101, "iPoker": 86})

    def test_coin_preflop_antes_and_raises(self):
        v = coin(30, "Hero", "Line 25")
        self.assertEqual(
            (v.street, v.board, v.hero_cards), ("Preflop", (), ("Kh", "8h", "2h", "Ts"))
        )
        self.assertEqual(
            (v.pot, v.to_call, v.bb, v.currency),
            (money("1.20"), money("0.50"), money("0.25"), "USDT"),
        )
        seats = {s.name: s for s in v.seats}
        hero = seats["Hero"]
        self.assertEqual(
            (hero.seat, hero.position, hero.stack, hero.committed),
            (3, "BB", money("25.70"), money("0.25")),
        )
        self.assertTrue(hero.live and hero.is_hero and not hero.all_in)
        self.assertTrue(seats["f6d5add1"].is_button)
        self.assertFalse(seats["f6d5add1"].live)
        self.assertEqual(
            sum(a.amount for a in v.history if a.action == "posts_ante"), money("0.20")
        )
        self.assertEqual(
            (v.history[-1].action, v.history[-1].amount, v.history[-1].raised_to),
            ("raise", money("0.65"), money("0.75")),
        )
        self.assertNotIn(v.ref, [a.ref for a in v.history])
        self.assertNotIn("action", asdict(v))

    def test_coin_board_stack_and_later_uncalled_return(self):
        v = coin(30, "Hero", "Line 28")
        self.assertEqual(v.board, ("9c", "5s", "2d"))
        self.assertEqual(
            (v.pot, v.to_call, v.history[-1].ref), (money("1.70"), 0, "Line 27")
        )
        self.assertEqual(next(s.stack for s in v.seats if s.is_hero), money("25.20"))
        self.assertTrue(all(s.committed == 0 for s in v.seats))
        river = coin(1, "8e06a63e", "Line 38")
        self.assertEqual(river.board, ("7h", "2h", "Td", "4c", "Qd"))
        self.assertEqual(
            (river.pot, river.to_call, river.history[-1].action),
            (money("1.26"), money("0.42"), "bet"),
        )
        self.assertNotIn("return", [a.action for a in river.history])
        self.assertIsNone(river.hero_cards)

    def test_ipoker_source_board_and_canonical_currency(self):
        v = ip(601, "BigNeonMax", "Action 10")
        self.assertEqual(
            (v.hero_cards, v.board), (("Kc", "8c", "Jh", "9d"), ("4d", "7s", "5d"))
        )
        self.assertEqual(
            (v.pot, v.to_call, v.bb, v.currency),
            (money("4.25"), 0, money("0.50"), "EUR"),
        )
        self.assertEqual(next(s.stack for s in v.seats if s.is_hero), money("48.25"))
        self.assertEqual(
            (v.history[-1].name, v.history[-1].ref, v.history[-1].action),
            ("RookCheckback", "Action 9", "check"),
        )
        p = ip(4, "BigNeonMax", "Action 6")
        self.assertEqual(
            (p.history[-1].amount, p.history[-1].raised_to), (money("2.50"), money("3"))
        )

    def test_short_allin_call_and_retrospective_villain_cards(self):
        # iPoker places showdown-revealed villain pockets in the preflop XML.
        # Only BigNeonMax is the recorded point of view.
        v = ip(101, "MidnightReg", "Action 12")
        self.assertIsNone(v.hero_cards)
        self.assertEqual((v.to_call, v.price_to_match), (money("0.14"), money("11.25")))
        self.assertEqual(next(s.stack for s in v.seats if s.is_hero), money("0.14"))
        self.assertEqual(v.board, ("2d", "Ah", "5h"))
        self.assertEqual(
            (v.history[-1].name, v.history[-1].raised_to), ("BorisChecks", money("15"))
        )
        self.assertEqual(
            ip(101, "BigNeonMax", "Action 7").hero_cards, ("9d", "5d", "7s", "Tc")
        )

    def test_source_visibility_boundaries(self):
        raw = original("coin", 30).replace(
            "Dealt to Hero [Kh 8h 2h Ts]", "Dealt to Hero"
        )
        source = Record("CoinPoker", raw, "Text hand 1")
        self.assertIsNone(snapshot_from_record(source, "Hero", "Line 28").hero_cards)
        with self.assertRaisesRegex(ValueError, "selected seat"):
            snapshot_from_record(source, "Hero", "Line 27")
        with self.assertRaisesRegex(ValueError, "observed decision"):
            snapshot_from_record(source, "Hero", "Seat 3")
        raw = original("ip", 601)
        board = '<cards type="Flop">D4 S7 D5</cards>'
        after = '<action no="10" player="BigNeonMax" sum="€0,00" type="4"/>'
        raw = raw.replace(board, "").replace(after, after + board)
        view = snapshot_from_record(
            Record("iPoker", raw, "HM2 row 1"), "BigNeonMax", "Action 10"
        )
        self.assertEqual(view.board, ("4d", "7s", "5d"))
        pocket = '<cards player="BigNeonMax" type="Pocket">CK C8 HJ D9</cards>'
        pf_decision = '<action no="5" player="BigNeonMax" sum="€1,75" type="3"/>'
        late_in_round = raw.replace(pocket, "").replace(
            pf_decision, pf_decision + pocket
        )
        self.assertEqual(
            snapshot_from_record(
                Record("iPoker", late_in_round, "HM2 row 1"), "BigNeonMax", "Action 5"
            ).hero_cards,
            ("Kc", "8c", "Jh", "9d"),
        )
        # A later street's pocket disclosure lacks original dealt-card evidence.
        raw = raw.replace(pocket, "").replace(after, after + pocket)
        self.assertIsNone(
            snapshot_from_record(
                Record("iPoker", raw, "HM2 row 1"), "BigNeonMax", "Action 5"
            ).hero_cards
        )

    def test_original_reference_and_alternate_dealt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, connect(":memory:") as db:
            initialize(db)
            redacted = Path(tmp) / "redacted.txt"
            raw = original("coin", 30)
            redacted.write_text(
                raw.replace("Dealt to Hero [Kh 8h 2h Ts]", "Dealt to Hero"),
                encoding="utf-8",
            )
            import_file(db, redacted)
            # The compatible second copy has additional lines and different source refs.
            expanded = Path(tmp) / "folds.txt"
            expanded.write_text(
                raw.replace("*** HOLE CARDS ***", "*** HOLE CARDS ***\n\n"),
                encoding="utf-8",
            )
            import_file(db, expanded)
            h = db.execute("SELECT id FROM hands").fetchone()[0]
            choices = available_actions(db, h, "Hero")
            self.assertEqual(
                [a.ref for a in choices], ["Line 25", "Line 28", "Line 31"]
            )
            self.assertGreater(len(available_actions(db, h)), len(choices))
            v = load_snapshot(db, h, "Hero", choices[1].ref)
            self.assertEqual(
                (v.hero_cards, v.ref, v.history[-1].ref),
                (("Kh", "8h", "2h", "Ts"), "Line 28", "Line 27"),
            )
            self.assertIsNotNone(v.pocket_source)
            db.execute("UPDATE hands SET conflict=1")
            with self.assertRaisesRegex(ValueError, "accepted source"):
                load_snapshot(db, h, "Hero", "Line 28")

    def test_native_ipoker_database_navigation(self):
        with tempfile.TemporaryDirectory() as tmp, connect(":memory:") as db:
            initialize(db)
            native = Path(tmp) / "hands.xml"
            native.write_bytes((FIX / "ip-601.hh").read_bytes() + b"</session>")
            import_file(db, native)
            h = db.execute("SELECT id FROM hands").fetchone()[0]
            choices = available_actions(db, h, "BigNeonMax")
            self.assertEqual(
                [(a.ref, a.street, a.name) for a in choices],
                [
                    ("Action 5", "Preflop", "BigNeonMax"),
                    ("Action 10", "Flop", "BigNeonMax"),
                    ("Action 12", "Turn", "BigNeonMax"),
                ],
            )
            v = load_snapshot(db, h, "BigNeonMax", "Action 12")
            self.assertEqual(
                (v.board, v.to_call), (("4d", "7s", "5d", "4h"), money("3.18"))
            )

    def test_exact_chip_format(self):
        self.assertEqual(
            [chip_text(n) for n in (None, 0, 1, -1, 1000000, 1004000, 2**60)],
            [
                "Unknown",
                "0",
                "0.000001",
                "-0.000001",
                "1",
                "1.004",
                "1152921504606.846976",
            ],
        )
