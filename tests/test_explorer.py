import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

from leakfinder.explorer import DEFS, Reports, open_explorer, source_span
from leakfinder.statistics import BITS
from leakfinder.storage import connect, import_file, initialize, set_profile

from tests.test_engine import original


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.library = self.path / "report.sqlite3"
        self.db = connect(self.library)
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.coin = original("coin", 30)
        self.put("history.txt", self.coin)
        xml = original("ip", 601).strip()
        self.put(
            "session.xml", xml if xml.endswith("</session>") else xml + "</session>"
        )
        set_profile(self.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})

    def put(self, name, text):
        target = self.path / name
        target.write_text(text, encoding="utf-8")
        self.assertEqual(import_file(self.db, target), {"new hands": 1})

    def test_original_sources_for_hand_and_action_metrics(self):
        reports = Reports(self.db, "Both")
        groups = reports.report("BET_IP_VS_MISSED_FLOP_CB", "Room")
        self.assertEqual(
            {g.key: (g.x, g.n) for g in groups}, {"iPoker": (0, 1), "CoinPoker": (0, 1)}
        )
        for g in groups:
            rows, n = reports.page(g.key)
            self.assertEqual(n, 1)
            _, _, text = reports.source(rows[0]["hand"])
            start, end = source_span(text, rows[0]["ref"])
            self.assertRegex(text[start:end], r'Hero: checks|type="4"')
        groups = reports.report("VPIP", "Room")
        for g in groups:
            rows, n = reports.page(g.key)
            self.assertEqual((g.x, g.n, n), (1, 1, 1))
            _, _, text = reports.source(rows[0]["hand"])
            start, end = source_span(text, rows[0]["ref"])
            self.assertIn(rows[0]["hero"], text[start:end])

    def test_all_metric_selectors_and_overlapping_classes(self):
        reports = Reports(self.db, "Both")
        for key in DEFS:
            with self.subTest(metric=key):
                groups = reports.report(key, "Room")
                for g in groups:
                    rows, n = reports.page(g.key)
                    self.assertEqual(g.n, n)
                    self.assertEqual(g.x, sum(r["hit"] for r in rows))
                    for row in rows:
                        _, _, source = reports.source(row["hand"])
                        self.assertIsNotNone(source_span(source, row["ref"]))
        known, total, _ = reports.coverage
        self.assertEqual((known, total), (2, 2))
        groups = reports.report("VPIP", "Hand class")
        self.assertGreater(sum(g.n for g in groups), 2)
        self.assertEqual(reports.coverage, (2, 2, 2))

    def test_session_splits_midnight_and_room_scoped_deltas(self):
        for i, stamp in enumerate(("13:45:00", "14:31:00"), 1):
            self.put(
                f"c{i}.txt",
                self.coin.replace("9000000030", f"93000000{i}").replace(
                    "13:00:00", stamp
                ),
            )
        reports = Reports(self.db, "Both")
        groups = reports.report("VPIP", "Trends")
        coin = [g for g in groups if g.key.endswith("CoinPoker")]
        ip = [g for g in groups if g.key.endswith("iPoker")]
        self.assertEqual([g.n for g in coin], [2, 1])
        self.assertEqual([g.previous for g in coin], [None, 1.0])
        self.assertIsNone(ip[0].previous)
        self.assertEqual([g.result_hands for g in coin], [2, 1])
        reports.report("FOLD_TO_RIVER_PROBE", "Session", "2026-01-01", "2026-01-01")
        for key, g in reports.groups.items():
            self.assertEqual(g.n, 0)
            self.assertEqual(
                reports.page(key, scope="All session hands")[1], g.result_hands
            )
            self.assertNotIn("iPoker", key)

    def test_size_bins_have_full_denominator_and_exact_subset(self):
        target = self.db.execute(
            "SELECT id FROM hands WHERE room='CoinPoker'"
        ).fetchone()[0]
        bit = BITS["FLOP_SIZE"]
        self.db.executemany(
            "INSERT INTO obs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                (target, "Hero", f"Test {i}", "Opponent", 1, f, None, bit, bit)
                for i, f in enumerate((0.25, 0.25, 0.8))
            ),
        )
        r = Reports(self.db, "CoinPoker")
        groups = r.report("FLOP_SIZE", "Chosen sizing")
        self.assertEqual([(g.x, g.n) for g in groups], [(2, 3), (1, 3)])
        rows, n = r.page(groups[0].key, scope="All eligible bets")
        self.assertEqual((sum(v["hit"] for v in rows), n), (2, 3))
        self.assertEqual(r.page(groups[0].key)[1], 2)
        (overall,) = r.report("FLOP_SIZE", "Overall")
        self.assertAlmostEqual(overall.chosen_mean, 1.3 / 3)

    def test_offset_times_use_utc_while_unqualified_times_keep_separate_sessions(self):
        for i in range(2):
            self.put(f"other-{i}.txt", self.coin.replace("9000000030", f"91500000{i}"))
        stamps = (
            "2026-01-01 13:00:00+02:00",
            "2026-01-01 11:10:00+00:00",
            "2026-01-01 11:12:00",
        )
        hands = self.db.execute(
            "SELECT id FROM hands WHERE room='CoinPoker' ORDER BY id"
        ).fetchall()
        self.db.executemany(
            "UPDATE facts SET stamp=? WHERE hand=?",
            [(stamp, row[0]) for stamp, row in zip(stamps, hands, strict=True)],
        )
        groups = Reports(self.db, "CoinPoker").report("VPIP", "Trends")
        self.assertEqual(sorted(g.n for g in groups), [1, 2])
        self.assertEqual(next(g.n for g in groups if " UTC" in g.key), 2)
        self.assertTrue(all(g.previous is None for g in groups))

    def test_unknown_net_and_direct_opponent_scope(self):
        self.db.execute("UPDATE players SET net=NULL WHERE name='BigNeonMax'")
        self.db.execute(
            "UPDATE obs SET hits=(hits & ~?) WHERE actor='BigNeonMax'", (BITS["NET"],)
        )
        reports = Reports(self.db, "Both")
        groups = reports.report("NET", "Room")
        ip = next(g for g in groups if g.key == "iPoker")
        self.assertEqual((ip.result_hands, ip.known_net, ip.bb100), (1, 0, None))
        self.assertEqual(reports.report("NET", "Opponent"), [])
        groups = reports.report("BET_IP_VS_MISSED_FLOP_CB", "Opponent")
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g.bb100 is None and " · " in g.key for g in groups))
        with self.assertRaises(ValueError):
            reports.report("VPIP", "Month", "2026-03-02", "2026-02-01")

    def test_dealt_heads_up_net_and_rates_have_exact_rival_and_hand(self):
        fixtures = Path(__file__).parent / "fixtures"
        self.put(
            "duel.txt", (fixtures / "coin-heads-up.hh").read_text(encoding="utf-8")
        )
        self.put("duel.xml", original("ip", 41))
        reports = Reports(self.db, "Both")
        for key in ("NET", "VPIP", "PFR"):
            with self.subTest(metric=key):
                groups = reports.report(key, "Opponent")
                self.assertEqual(sum(g.n for g in groups), 2)
                rivals = {}
                for g in groups:
                    rows, n = reports.page(g.key)
                    self.assertEqual(n, 1)
                    row = rows[0]
                    (rival,) = self.db.execute(
                        "SELECT name FROM seats WHERE hand=? AND name<>?",
                        (row["hand"], row["hero"]),
                    ).fetchone()
                    self.assertEqual(g.key, row["room"] + " · " + rival)
                    self.assertTrue(row["ref"].startswith("Seat "))
                    _, _, raw = reports.source(row["hand"])
                    self.assertIsNotNone(source_span(raw, row["ref"]))
                    rivals[row["room"]] = g.net
                if key == "NET":
                    self.assertEqual(rivals, {"CoinPoker": -1.16, "iPoker": -3.0})

    def test_desktop_drilldown_smoke(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        window = open_explorer(root, self.library, "Both", "Position")

        def until(pred):
            for _ in range(300):
                root.update()
                if pred():
                    return
                time.sleep(0.01)
            self.fail("Report window did not finish its background request")

        until(lambda: bool(window.tree.get_children()))
        window.drill()
        until(lambda: bool(window.viewer.raw.tag_ranges("anchor")))
        self.assertTrue(window.viewer.raw.get("1.0", "end-1c").strip())
        window.win.destroy()
        window.worker.join(5)
        self.assertFalse(window.worker.is_alive())
