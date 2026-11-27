import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from leakfinder import luck
from leakfinder.demo import SAMPLES
from leakfinder.importers import sources
from leakfinder.storage import connect, import_file, initialize, set_profile
from tests.test_library import original


class LuckLibrary(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = connect(self.root / "library.sqlite3")
        self.addCleanup(self.db.close)
        initialize(self.db)
        set_profile(self.db, {"iPoker": ["BigNeonMax"], "CoinPoker": ["HeroDemo"]})

    def ingest(self, file, raw):
        path = self.root / file
        path.write_text(raw, encoding="utf8")
        return import_file(self.db, path)

    def test_incremental_conflicts_and_filters(self):
        h = original("ip-41.hh")
        self.ingest("first.xml", h)
        self.assertEqual(luck.refresh(self.db), 1)
        self.ingest(
            "second.xml",
            h.replace("9000000041", "9000005041").replace("2026-04-19", "2026-04-20"),
        )
        self.assertEqual(luck.refresh(self.db), 1)
        self.ingest("duplicate.xml", h)
        with patch.object(luck, "recorded", side_effect=AssertionError):
            self.assertEqual(luck.refresh(self.db), 0)
        self.assertEqual(
            luck.report(
                self.db,
                "iPoker",
                ("EUR", 1000000),
                "2026-04-19",
                "2026-04-19",
                "BigNeonMax",
            )["hands"],
            1,
        )
        for args in [
            ("CoinPoker",),
            ("Both", ("USD", 1000000)),
            ("Both", None, "", "", "Nobody"),
        ]:
            self.assertEqual(luck.report(self.db, *args)["hands"], 0)
        self.ingest("conflict.xml", h.replace(">C7<", ">D7<"))
        self.assertEqual(luck.refresh(self.db), 1)
        updated = luck.report(self.db)
        self.assertEqual(updated["hands"], 1)
        self.assertEqual(updated["excluded"], {"Conflicting source copies": 1})
        self.db.execute("DELETE FROM luck_hand_v1")
        self.db.commit()
        self.assertEqual(luck.refresh(self.db), 2)
        self.assertEqual(luck.report(self.db), updated)

    def test_warm_histogram_only_unpacks_changed_deal(self):
        from leakfinder.demo import load_demo

        load_demo(self.db)
        h = (
            original("ip-41.hh")
            .replace("BigNeonMax", "HeroDemo")
            .replace("9000000041", "9989000041")
        )
        self.ingest("first.xml", h)
        luck.refresh(self.db)
        luck.report(self.db)
        self.ingest("second.xml", h.replace("9989000041", "9989005041"))
        self.assertEqual(luck.refresh(self.db), 1)
        with patch.object(luck, "unpack", wraps=luck.unpack) as unpack:
            self.assertEqual(luck.report(self.db)["hands"], 642)
            self.assertEqual(unpack.call_count, 1)
            self.assertEqual(luck.report(self.db)["hands"], 642)
            self.assertEqual(unpack.call_count, 1)
        self.ingest("conflict.xml", h.replace(">C7<", ">D7<"))
        self.assertEqual(luck.refresh(self.db), 1)
        with patch.object(luck, "unpack", wraps=luck.unpack) as unpack:
            self.assertEqual(luck.report(self.db)["hands"], 641)
            self.assertEqual(unpack.call_count, 1)

    def test_coin_summary_disagrees_with_board(self):
        hand = next(sources(SAMPLES / "CoinPoker-HeroDemo.txt")).raw
        self.ingest(
            "broken.txt",
            hand.replace("Board [ 7h 2h Td 4c Qd ]", "Board [ 7h 2h Td 4c Qc ]"),
        )
        luck.refresh(self.db)
        self.assertEqual(
            luck.report(self.db)["excluded"], {"Inconsistent summary board": 1}
        )

    def test_no_showdown_selection_or_opponent_removal(self):
        raw = original("ip-41.hh")
        self.ingest("plain.xml", raw)
        self.ingest(
            "shown.xml",
            raw.replace("9000000041", "9000005041").replace("X X X X", "D2 D3 D4 D5"),
        )
        self.ingest(
            "hidden.xml",
            raw.replace("9000000041", "9000005042").replace("SA C3 HA D10", "X X X X"),
        )
        luck.refresh(self.db)
        self.assertEqual(
            self.db.execute("SELECT count(DISTINCT data) FROM luck_hand_v1").fetchone()[
                0
            ],
            1,
        )
        report = luck.report(self.db)
        self.assertEqual(report["hands"], 2)
        self.assertEqual(report["excluded"], {"Missing four dealt Hero cards": 1})

    def test_scoped_run_twice_and_incomplete_and_legacy_exclusions(self):
        a = next(sources(SAMPLES / "CoinPoker-HeroDemo.txt")).raw
        self.ingest("twice.txt", a + "\nHand was run twice\n")
        self.ingest(
            "incomplete.txt",
            a.split("*** SUMMARY ***")[0].replace(
                a.split("#")[1].split(":")[0], "9990099999", 1
            ),
        )
        h = original("ip-41.hh")
        self.ingest("invalid.xml", h.replace(">C7<", ">SK<"))
        # An unrepresentable stake in an already rejected record is unknown.
        # Capturing its descriptor must not roll back the import.
        from leakfinder.importers import Record
        from leakfinder.luck_exclusions import descriptor

        invalid = h.replace("<bigblind>", "<bigblind>999999999999999999999").replace(
            "€1,00</bigblind>", "</bigblind>"
        )
        self.assertIsNone(
            descriptor(Record("iPoker", invalid, "XML game 1"), "Invalid blind")[-1]
        )
        # Simulate a database made before filter metadata existed.
        self.db.execute("DROP TABLE luck_reject_meta")
        self.db.commit()
        luck.refresh(self.db)
        rows = luck.report(self.db, "CoinPoker")["import_excluded"]
        self.assertEqual(rows, {"Multiple runouts": 1, "Incomplete CoinPoker hand": 1})
        self.assertEqual(
            luck.report(self.db, "iPoker")["import_excluded"],
            {"Invalid or duplicate card": 1},
        )
        self.assertEqual(
            luck.report(self.db, "CoinPoker", ("EUR", 1000000))["import_excluded"], {}
        )
        self.assertEqual(
            luck.report(self.db, start="2100-01-01")["import_excluded"], {}
        )

    def test_examples_and_folded_flop_filter(self):
        from leakfinder.replay_view import available_actions
        from leakfinder.river_holdings import CARD_ID

        h = original("ip-41.hh")
        self.ingest("hand.xml", h)
        luck.refresh(self.db)
        # A deliberately contrived surplus within a small hand-verifiable fixture.
        with patch.object(
            luck.Tally,
            "row",
            autospec=True,
            return_value={
                "check_parser": True,
                "difference": 1,
            },
        ):
            examples = luck.report(self.db)["examples"]
        self.assertTrue(examples)
        self.assertTrue(
            all(
                available_actions(self.db, x[0])
                for group in examples.values()
                for x in group
            )
        )
        self.assertTrue(all(len(group) <= 10 for group in examples.values()))
        ids = lambda s: tuple(CARD_ID[c] for c in s.split())
        b = luck.encode(ids("As 7s Jd 3c"), ids("Ks 4s 9h 6s 5h"), False)
        normal = [i for i, _, _ in luck.unpack(b)]
        all_dealt = [i for i, _, _ in luck.unpack(b, True)]
        self.assertNotIn(8, normal)
        self.assertIn(8, all_dealt)
        self.assertIn(10, normal)
