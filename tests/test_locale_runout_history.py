"""Regression cases for localized iPoker money, runout validity and effective bets."""

import tempfile
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from leakfinder.engine import replay
from leakfinder.importers import Excluded, coin, ipoker, money
from leakfinder.opponent_history import OpponentHistory, early_observations
from leakfinder.replay_view import load_snapshot
from leakfinder.statistics import BITS
from leakfinder.storage import connect, import_file, initialize, set_profile
from tests.test_engine import paired


class MoneyAndRunoutTests(unittest.TestCase):
    def test_repair_restores_stack_and_verified_payout_without_moving_latest_import(
        self,
    ):
        _, xml = paired(
            [[("A", "check", 0), ("B", "bet", 1), ("A", "fold", 0)]],
            pf=[("C", "fold", 0), ("A", "call", .25), ("B", "check", 0)],
        )
        xml = xml.replace(",", ".")
        # Both contribute 500.50. The last flop bet is returned to B.
        xml = xml.replace("€0.25", "€250.25").replace("€0.50", "€500.50")
        xml = xml.replace('sum="€1.00"', 'sum="€500.50"')
        xml = xml.replace('chips="€50.00"', 'chips="€1250.00"')
        xml = xml.replace('name="C" chips="€1250.00"', 'name="C" chips="€1,250"')
        for name, bet, win in (
            ("A", "500.50", "0"),
            ("B", "1001.00", "1,001"),
            ("C", "0", "0"),
        ):
            xml = xml.replace(
                f'name="{name}" chips=',
                f'bet="€{bet}" win="€{win}" name="{name}" chips=',
            )
        with tempfile.TemporaryDirectory() as tmp, closing(connect(":memory:")) as db:
            initialize(db)
            path = Path(tmp) / "game.xml"
            path.write_text(xml, encoding="utf8")
            # The legacy parser accepts the payout as 1.001 but cannot reconcile it.
            with patch("leakfinder.importers.ipoker_money_reader", return_value=money):
                self.assertEqual(import_file(db, path), {"new hands": 1})
            self.assertEqual(
                db.execute("SELECT count(net) FROM players").fetchone()[0], 0
            )
            delta = [tuple(r) for r in db.execute("SELECT * FROM import_delta")]
            with db:
                db.execute("PRAGMA user_version=5")
            initialize(db)
            self.assertEqual(
                [tuple(r) for r in db.execute("SELECT * FROM import_delta")], delta
            )
            self.assertEqual(
                dict(db.execute("SELECT name,net FROM players")),
                {"A": -1.0, "B": 1.0, "C": 0.0},
            )
            self.assertTrue(
                all(s == money("1250") for s in ipoker(xml).stacks.values())
            )

    def test_grouping_and_fractional_precision_follow_each_records_money_fields(self):
        _, base = paired([[("A", "bet", 1), ("B", "fold", 0)]])
        for decimal, grouping in ((",", "."), (".", ",")):
            with self.subTest(decimal=decimal):
                xml = base.replace(",", decimal)
                xml = xml.replace(
                    f'chips="€50{decimal}00"', f'chips="€1{grouping}250"', 1
                )
                xml = xml.replace(
                    'dealer="1"',
                    f'win="€1{grouping}234" rakeamount="€0{decimal}125" dealer="1"',
                )
                hand = ipoker(xml)
                self.assertEqual(hand.stacks["A"], money("1250"))
                self.assertEqual(hand.awards["C"], money("1234"))
                self.assertEqual(hand.fees, money("0.125"))
                fractional = xml.replace(f"€1{grouping}250", f"€1{decimal}250")
                self.assertEqual(ipoker(fractional).stacks["A"], money("1.25"))
                grouped = xml.replace(
                    f"€1{grouping}250", f"€1{grouping}234{grouping}567"
                )
                self.assertEqual(ipoker(grouped).stacks["A"], money("1234567"))

    def test_no_guess_from_a_nickname_or_conflicting_amounts(self):
        _, xml = paired([], pf=[("C", "fold", 0), ("A", "fold", 0)])
        import re

        xml = re.sub(r"€\d+,\d+", "€1", xml)
        xml = xml.replace('chips="€1"', 'chips="€1,234"', 1).replace(">B<", ">1.25<")
        with self.assertRaisesRegex(Excluded, "Ambiguous iPoker chip separators"):
            ipoker(xml)
        mixed = xml.replace("<smallblind>€1<", "<smallblind>€0.10<").replace(
            "<bigblind>€1<", "<bigblind>€0,20<"
        )
        with self.assertRaisesRegex(Excluded, "Ambiguous iPoker chip separators"):
            ipoker(mixed)

    def test_called_all_in_needs_a_full_board_but_uncontested_pot_does_not(self):
        for boards in ([], [[]], [[], []], [[], [], []]):
            txt, xml = paired(boards)
            for covering in (False, True):
                for hand in (coin(txt), ipoker(xml)):
                    with self.subTest(
                        room=hand.room, covering=covering, boards=len(boards)
                    ):
                        hand.stacks["A"] = money("1.50")
                        if not covering:
                            hand.stacks["B"] = money("1.50")
                        if len(boards) < 3:
                            with self.assertRaisesRegex(Excluded, "Incomplete runout"):
                                replay(hand)
                        else:
                            replay(hand)
                            self.assertEqual(hand.survivors, {"A", "B"})
        for hand in (
            coin(paired([], pf=[("C", "fold", 0), ("A", "fold", 0)])[0]),
            ipoker(paired([], pf=[("C", "fold", 0), ("A", "fold", 0)])[1]),
        ):
            replay(hand)
            self.assertEqual(hand.survivors, {"B"})

    def test_upgrade_repairs_retained_sources_and_recovers_previously_rejected_file_once(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(":memory:")) as db:
            initialize(db)

            def ingest(name, raw):
                path = Path(tmp) / name
                path.write_text(raw, encoding="utf8")
                return import_file(db, path)

            _, raw = paired([[("A", "bet", 2), ("B", "fold", 0)]], number="5001")
            grouped = raw.replace(",", ".").replace(
                'chips="€50.00"', 'chips="€1,250"', 1
            )
            with patch("leakfinder.importers.ipoker_money_reader", return_value=money):
                self.assertEqual(ingest("old.xml", grouped), {"excluded": 1})
            _, valid = paired([[], [], []], number="5002")
            valid = valid.replace('chips="€50,00"', 'chips="€1,50"')
            _, cut = paired([[]], number="5002")
            cut = cut.replace('chips="€50,00"', 'chips="€1,50"')
            self.assertEqual(ingest("runout.xml", valid), {"new hands": 1})
            with db:
                db.execute("UPDATE origins SET raw=?", (zlib.compress(cut.encode()),))
                db.execute("PRAGMA user_version=5")
            initialize(db)
            self.assertEqual(db.execute("SELECT count(*) FROM hands").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM obs").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM rejected WHERE reason='Incomplete runout'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(import_file(db, Path(tmp) / "old.xml"), {"new hands": 1})
            self.assertEqual(
                import_file(db, Path(tmp) / "old.xml"), {"already imported": 1}
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM amount_retry").fetchone()[0], 0
            )


class EffectiveHistoryTests(unittest.TestCase):
    def test_prebet_observations_and_facing_snapshots_match_only_equally_committing_bets(
        self,
    ):
        for street in ("Flop", "Turn"):
            for short in ("caller", "bettor"):
                with (
                    self.subTest(street=street, short=short),
                    tempfile.TemporaryDirectory() as tmp,
                    closing(connect(":memory:")) as db,
                ):
                    initialize(db)
                    set_profile(db, {"CoinPoker": ["B"]})
                    for day, size in enumerate(
                        (1.5, 1.45, 3 if short == "caller" else 1.5, 1.5), 1
                    ):
                        rounds = (
                            [[("A", "check", 0), ("B", "check", 0)]]
                            if street == "Turn"
                            else []
                        )
                        txt, _ = paired(
                            rounds + [[("A", "bet", size), ("B", "fold", 0)]],
                            number=str(2000 + day),
                        )
                        name = "B" if short == "caller" else "A"
                        txt = txt.replace(
                            f"{name} (₮50 in chips)", f"{name} (₮3 in chips)"
                        )
                        txt += "\nGame ended: 2026/03/01 12:05:00 CET"
                        path = Path(tmp) / f"s{day}.txt"
                        path.write_text(
                            txt.replace("2026/03/01", f"2026/03/{day:02}"),
                            encoding="utf8",
                        )
                        self.assertEqual(import_file(db, path), {"new hands": 1})
                    rows = tuple(early_observations(db, "CoinPoker", "A", street))
                    self.assertEqual(len(rows), 4)
                    target = next(r for r in rows if r.number == "2004")
                    ref = db.execute(
                        "SELECT ref FROM obs WHERE hand=? AND actor='B' AND faced IS NOT NULL AND (eligible & ?)<>0",
                        (target.hand, BITS[street.upper() + "_AGG"]),
                    ).fetchone()[0]
                    snap = load_snapshot(db, target.hand, "B", ref)
                    sample = OpponentHistory(rows).sample(snap)
                    self.assertEqual(
                        {r.number for r in sample.comparable}, {"2001", "2003"}
                    )
                    self.assertTrue(all(r.case[-1] == "bet-1" for r in rows))
