"""Integration checks using original hand rows and explicit manufactured scenarios."""

import json
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from leakfinder.analysis import Cell, analyze, evidence, summarize, wilson
from leakfinder.statistics import BITS
from leakfinder.storage import connect, import_file, initialize, profile, set_profile
from leakfinder.vendor.hm2_backup import HM2BackupError

FIX = Path(__file__).parent / "fixtures"
COLS = [
    "handhistory_id",
    "gamenumber",
    "pokersite_id",
    "handhistory",
    "filename",
    "handtimestamp",
    "gametype_id",
    "tourneynumber",
]


def original(name):
    return (FIX / name).read_bytes().decode("utf-8")


def repaired_session(raw):
    return ET.fromstring(
        raw if raw.strip().endswith("</session>") else raw.strip() + "\n</session>"
    )


def as_copy(records, columns=COLS):
    lines = ["COPY handhistories (" + ", ".join(columns) + ") FROM stdin;"]
    for rid, number, raw in records:
        escaped = (
            raw.replace("\\", "\\\\")
            .replace("\t", r"\t")
            .replace("\r", r"\r")
            .replace("\n", r"\n")
        )
        values = {
            "handhistory_id": str(rid),
            "gamenumber": number,
            "pokersite_id": "4",
            "handhistory": escaped,
            "filename": "session.xml",
            "handtimestamp": r"\N",
            "gametype_id": "3",
            "tourneynumber": r"\N",
        }
        lines.append("\t".join(values[column] for column in columns))
    return "\n".join([*lines, r"\.", ""])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.library = self.folder / "personal.sqlite3"
        self.db = connect(self.library)
        self.addCleanup(self.db.close)
        initialize(self.db)

    def save(self, name, data):
        path = self.folder / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def test_two_xml_games_match_hm2_sql_and_zip_and_keep_each_origin(self):
        r1, r2 = original("ip-1.hh"), original("ip-601.hh")
        root = repaired_session(r1)
        root.append(repaired_session(r2).find("game"))
        xml = self.save(
            "original-session.xml",
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
        )
        dump = as_copy(
            [(1, "9000000001", r1), (601, "9000000601", r2)], list(reversed(COLS))
        )
        sql = self.save("reordered.sql", dump)
        backup = self.folder / "packed.pghembk"
        with ZipFile(backup, "w", ZIP_DEFLATED) as archive:
            archive.writestr("db/HoldemManager2.sql", dump)
        self.assertEqual(import_file(self.db, xml)["new hands"], 2)
        for source in (sql, backup):
            self.assertEqual(import_file(self.db, source)["duplicate origins"], 2)
        self.assertEqual(
            self.db.execute("select count(*) from origins").fetchone()[0], 6
        )
        self.assertEqual(
            self.db.execute("select sum(conflict) from hands").fetchone()[0], 0
        )
        self.assertEqual(import_file(self.db, backup), {"already imported": 1})
        set_profile(self.db, {"iPoker": ["BigNeonMax"]})
        fs, eligible, total = analyze(self.db, "iPoker")
        self.assertEqual((eligible, total), (2, 2))
        f = next(f for f in fs if f.node == "BET_IP_VS_MISSED_FLOP_CB")
        self.assertEqual((f.hero, f.status), (1, "Exploratory"))
        rows, counts = evidence(self.db, f, "Hero", "All opportunities")
        self.assertEqual(counts, (1, 1))
        self.assertEqual(
            (rows[0]["number"], rows[0]["action"], rows[0]["ref"]),
            ("9000000601", "check", "Action 10"),
        )
        for origin in self.db.execute(
            "select raw from origins where hand=?", (rows[0]["hand"],)
        ):
            raw = zlib.decompress(origin["raw"]).decode()
            self.assertRegex(raw, r'<action\s+[^>]*no="10"[^>]*type="4"')

    def test_room_namespaces_identity_conflict_and_cached_evidence(self):
        cp = self.save("coin.txt", original("coin-1.hh"))
        raw = original("ip-1.hh")
        ip = self.save("iPoker.xml", ET.tostring(repaired_session(raw)))
        for path in (cp, ip):
            self.assertEqual(import_file(self.db, path)["new hands"], 1)
        set_profile(self.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})
        self.assertEqual(analyze(self.db)[1:], (2, 2))
        self.assertEqual(analyze(self.db, "CoinPoker")[1:], (1, 1))
        self.assertEqual(analyze(self.db, "iPoker")[1:], (1, 1))
        set_profile(
            self.db, {"CoinPoker": ["Hero", "ab163095"], "iPoker": ["BigNeonMax"]}
        )
        self.assertEqual(analyze(self.db)[1:], (1, 2))
        set_profile(self.db, {"CoinPoker": ["ab163095"], "iPoker": ["bigneonmax"]})
        self.assertEqual(analyze(self.db)[1:], (0, 2))
        self.db.commit()
        changed = raw.replace(
            "<nickname>BigNeonMax</nickname>", "<nickname>OddVillain</nickname>"
        )
        second = self.save("alternate.xml", ET.tostring(repaired_session(changed)))
        self.assertEqual(import_file(self.db, second)["conflicting copies"], 1)
        set_profile(self.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})
        self.assertEqual(analyze(self.db)[1:], (1, 2))
        self.assertEqual(
            profile(self.db), {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]}
        )
        ip.unlink()
        cp.unlink()
        self.assertEqual(
            self.db.execute("select count(*) from origins").fetchone()[0], 3
        )
        self.assertTrue(
            all(
                zlib.decompress(r[0])
                for r in self.db.execute("select raw from origins")
            )
        )

    def test_duplicate_reconciled_settlements_disagree(self):
        raw = original("coin-30.hh")
        a = self.save("original.txt", raw)
        b = self.save(
            "different-fee.txt",
            raw.replace("₮1.60", "₮1.59").replace("Rake ₮0.08", "Rake ₮0.09"),
        )
        self.assertEqual(import_file(self.db, a)["new hands"], 1)
        self.assertEqual(import_file(self.db, b)["conflicting copies"], 1)
        set_profile(self.db, {"CoinPoker": ["Hero"]})
        self.assertEqual(analyze(self.db, "CoinPoker")[1:], (0, 1))

    def test_richer_settlement_origin_restores_realized_result(self):
        raw = original("coin-30.hh")
        poor = self.save(
            "no-collection.txt", raw.replace("Hero collected ₮1.60 from pot", "")
        )
        good = self.save("complete.txt", raw)
        self.assertEqual(import_file(self.db, poor)["new hands"], 1)
        self.assertIsNone(
            self.db.execute("SELECT net FROM players WHERE name='Hero'").fetchone()[0]
        )
        self.assertEqual(import_file(self.db, good)["duplicate origins"], 1)
        self.assertAlmostEqual(
            self.db.execute("SELECT net FROM players WHERE name='Hero'").fetchone()[0],
            3.24,
        )
        self.assertTrue(
            self.db.execute(
                "SELECT hits FROM obs WHERE actor='Hero' AND (eligible & ?) != 0",
                (BITS["NET"],),
            ).fetchone()[0]
            & BITS["NET"]
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM origins").fetchone()[0], 2
        )

    def test_partial_dump_failure_rolls_back_valid_first_row(self):
        raw = original("ip-41.hh")
        good = as_copy([(41, "9000000041", raw)])
        malformed = self.save(
            "broken.sql", good.replace("\n\\.\n", "\ntoo\tfew\n\\.\n")
        )
        with self.assertRaises(HM2BackupError):
            import_file(self.db, malformed)
        for table in ("imports", "hands", "origins", "decisions"):
            self.assertEqual(
                self.db.execute(f"select count(*) from {table}").fetchone()[0], 0
            )
        malformed.write_text(good, encoding="utf-8")
        self.assertEqual(import_file(self.db, malformed)["new hands"], 1)

    def test_concurrent_reimport_is_idempotent(self):
        path = self.save("native.txt", original("coin-30.hh"))

        def ingest(_):
            db = connect(self.library)
            try:
                return import_file(db, path)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(ingest, range(2)))
        self.assertCountEqual(results, [{"new hands": 1}, {"already imported": 1}])
        self.assertEqual(self.db.execute("select count(*) from hands").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("select count(*) from origins").fetchone()[0], 1
        )


def cell(h, hx, p, px, day_count=20):
    """Evenly distributed integer counts. Tests which need 20 days use divisible totals."""
    days = {}
    for i in range(day_count):
        counts = [v // day_count + (i < v % day_count) for v in (hx, h, px, p)]
        days[f"2026-05-{i + 1:02}"] = counts
    return Cell(h, hx, p, px, days)


class MatchedReportTests(unittest.TestCase):
    def test_combined_weights_and_opposite_directions(self):
        cells = {
            json.dumps(["CoinPoker"]): cell(80, 64, 200, 80),
            json.dumps(["iPoker"]): cell(20, 0, 200, 120),
        }
        f = summarize("THREEBET", cells, "Both")
        self.assertEqual(f.counts, (64, 100, 200, 400))
        self.assertAlmostEqual(f.expected, 0.44)
        self.assertAlmostEqual(f.gap, 0.20)
        self.assertEqual(f.status, "Exploratory")
        self.assertIn("directions differ", f.note)
        self.assertLess(f.interval[0], f.gap)
        self.assertGreater(f.interval[1], f.gap)

    def test_audited_high_evidence_node_can_be_supported(self):
        cells = {json.dumps(["CoinPoker"]): cell(2000, 1700, 8000, 1200)}
        f = summarize("THREEBET", cells, "CoinPoker")
        self.assertEqual(f.status, "Supported")
        self.assertGreater(f.adjusted[0], 0.05)
        self.assertEqual(summarize("OPEN", cells, "CoinPoker").status, "Exploratory")
        self.assertEqual(summarize("THREEBET", cells, "Both").status, "Exploratory")

    def test_sparse_and_unsupported_cells_are_visible(self):
        cells = {
            json.dumps(["CoinPoker", "A"]): cell(4, 0, 1, 0, 1),
            json.dumps(["CoinPoker", "B"]): cell(6, 1, 0, 0, 1),
        }
        f = summarize("THREEBET", cells, "CoinPoker")
        self.assertEqual((f.hero, f.pool, f.counts), (10, 1, (0, 4, 0, 1)))
        self.assertFalse(f.high_coverage)
        self.assertEqual(f.status, "Exploratory")
        self.assertEqual(f.gap, 0)
        self.assertLess(f.interval[0], 0)
        self.assertGreater(f.interval[1], 0)
        only = summarize(
            "THREEBET", {json.dumps(["CoinPoker"]): cell(3, 1, 0, 0, 1)}, "Both"
        )
        self.assertEqual(only.hero, 3)
        self.assertEqual(only.selected, set())
        self.assertIsNone(only.gap)
        self.assertIsNone(wilson(0.5, 0))
