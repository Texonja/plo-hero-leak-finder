"""Integration checks using original hand rows and explicit manufactured scenarios."""

import json
import os
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from leakfinder.analysis import Cell, analyze, evidence, summarize, wilson
from leakfinder.engine import replay
from leakfinder.replay_view import hero_decisions
from leakfinder.review_queue import prepare_hands, remember_batch
from leakfinder.statistics import BITS
from leakfinder.storage import (
    connect,
    data_revision,
    import_file,
    import_file_with_source,
    initialize,
    profile,
    set_profile,
)
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

    def test_two_xml_games_match_hm2_sql_and_zip_and_keep_distinct_sources(self):
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
        zip_dump = self.folder / "sql-export.zip"
        with ZipFile(zip_dump, "w", ZIP_DEFLATED) as archive:
            archive.writestr("docs/README.txt", "Saved HM2 hand histories")
            archive.writestr("backup/export.sql", dump)
        self.assertEqual(import_file(self.db, xml)["new hands"], 2)
        self.assertEqual(import_file(self.db, backup), {"duplicate origins": 2})
        self.assertEqual(import_file(self.db, sql), {"known hands skipped": 2})
        self.assertEqual(import_file(self.db, zip_dump), {"known hands skipped": 2})
        self.assertEqual(
            self.db.execute("select count(*) from origins").fetchone()[0], 4
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

    def test_nested_native_zip_round_trip_and_deduplication(self):
        coin_1 = original("coin-1.hh")
        coin_30 = original("coin-30.hh")
        xml = ET.tostring(repaired_session(original("ip-601.hh")))
        packed = self.folder / "sessions.zip"
        with ZipFile(packed, "w", ZIP_DEFLATED) as archive:
            archive.writestr("CoinPoker/2025/01/01/Session.TXT", coin_1)
            archive.writestr("CoinPoker/2025/01/02/Session.TXT", coin_30)
            archive.writestr("iPoker/2025/02/03/session.XML", xml)
            archive.writestr("iPoker/copy.xml", xml)
            archive.writestr("readme.txt", "Archived histories")
            archive.writestr("notes.sql", "SELECT 1;")

        loose = self.save("before-zip.txt", coin_1)
        self.assertEqual(import_file(self.db, loose), {"new hands": 1})
        before = data_revision(self.db)
        with patch("leakfinder.storage.replay", wraps=replay) as checked:
            counts, source = import_file_with_source(self.db, packed)
            self.assertEqual(checked.call_count, 2)
        self.assertEqual(counts, {"new hands": 2, "known hands skipped": 2})
        remember_batch(self.db, [source], after_revision=before)
        set_profile(self.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})
        self.assertEqual(prepare_hands(self.db, "Both", "Latest import")[1:3], (2, 3))
        self.db.commit()

        archive_revision = data_revision(self.db)
        with patch("leakfinder.storage.replay", wraps=replay) as checked:
            self.assertEqual(import_file(self.db, packed), {"already imported": 1})
            self.assertEqual(
                import_file(self.db, self.save("after-zip.XML", xml)),
                {"known hands skipped": 1},
            )
            self.assertEqual(
                import_file(self.db, self.save("after-zip.txt", coin_30)),
                {"known hands skipped": 1},
            )
            checked.assert_not_called()
        self.assertEqual(data_revision(self.db), archive_revision)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM origins").fetchone()[0], 3
        )

        rows = list(
            self.db.execute(
                "SELECT hand,locator FROM origins WHERE source=? ORDER BY hand",
                (source,),
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertIn("CoinPoker/2025/01/02/Session.TXT", rows[0]["locator"])
        self.assertIn("iPoker/2025/02/03/session.XML", rows[1]["locator"])
        packed.unlink()
        self.assertTrue(
            list(hero_decisions(self.db, rows[0]["hand"], "Hero", "All streets"))
        )
        self.assertTrue(
            list(hero_decisions(self.db, rows[1]["hand"], "BigNeonMax", "All streets"))
        )

    def test_malformed_xml_in_zip_does_not_block_valid_sibling(self):
        packed = self.folder / "partially-completed.zip"
        with ZipFile(packed, "w", ZIP_DEFLATED) as archive:
            archive.writestr("old/incomplete.xml", "<session><game")
            archive.writestr("new/complete.txt", original("coin-30.hh"))
        self.assertEqual(import_file(self.db, packed), {"excluded": 1, "new hands": 1})
        row = self.db.execute("SELECT locator,reason FROM rejected").fetchone()
        self.assertIn("old/incomplete.xml", row["locator"])
        self.assertTrue(row["reason"])

    def test_zip_without_supported_histories_does_not_create_import(self):
        packed = self.folder / "empty-of-histories.zip"
        with ZipFile(packed, "w", ZIP_DEFLATED) as archive:
            archive.writestr("readme.txt", "No histories here")
            archive.writestr("avatars/avatar.jpg", b"")
        with self.assertRaisesRegex(ValueError, "No hand histories found"):
            import_file(self.db, packed)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0
        )

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

    def test_growing_coin_file_replays_only_new_hand_and_latest_has_only_additions(
        self,
    ):
        a, b = original("coin-30.hh"), original("coin-heads-up.hh")
        path = self.save("daily.txt", a)
        _, first = import_file_with_source(self.db, path)
        before = data_revision(self.db)
        remember_batch(self.db, [first])
        self.save("daily.txt", a + "\n\n" + b)
        with patch("leakfinder.storage.replay", wraps=replay) as checked:
            counts, second = import_file_with_source(self.db, path)
        self.assertEqual(counts, {"known hands skipped": 1, "new hands": 1})
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM origins").fetchone()[0], 2
        )
        remember_batch(self.db, [first, second], after_revision=before)
        set_profile(self.db, {"CoinPoker": ["Hero"]})
        self.assertEqual(prepare_hands(self.db, "Both", "Latest import")[1:3], (1, 2))
        self.assertEqual(
            self.db.execute("SELECT hand FROM review_hands").fetchone()[0], 2
        )
        self.db.commit()
        after = data_revision(self.db)
        with patch(
            "leakfinder.storage.sources",
            side_effect=AssertionError("parsed identical file"),
        ):
            moved = self.save("moved.txt", path.read_bytes())
            self.assertEqual(
                import_file_with_source(self.db, moved),
                ({"already imported": 1}, second),
            )
            self.assertEqual(import_file(self.db, path), {"already imported": 1})
        self.assertEqual(data_revision(self.db), after)

    def test_growing_native_xml_with_revised_session_totals(self):
        root = repaired_session(original("ip-601.hh"))
        path = self.save("growing.xml", ET.tostring(root, encoding="utf-8"))
        import_file(self.db, path)
        root.find("general/gamecount").text = "26"
        root.find("general/duration").text = "01:20:02"
        root.append(repaired_session(original("ip-1.hh")).find("game"))
        self.save("growing.xml", ET.tostring(root, encoding="utf-8"))
        with patch("leakfinder.storage.replay", wraps=replay) as checked:
            counts = import_file(self.db, path)
        self.assertEqual(counts, {"new hands": 1, "known hands skipped": 1})
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM origins").fetchone()[0], 2
        )

    def test_later_copy_supplies_dealt_evidence_despite_same_showdown_cards(self):
        rich = repaired_session(original("ip-601.hh"))
        poor = repaired_session(original("ip-601.hh"))
        pre = poor.find('game/round[@no="1"]')
        pocket = pre.find('cards[@player="BigNeonMax"]')
        pre.remove(pocket)
        poor.find('game/round[@no="3"]').append(pocket)
        import_file(self.db, self.save("first.xml", ET.tostring(poor)))
        before = data_revision(self.db)
        self.assertFalse(list(hero_decisions(self.db, 1, "BigNeonMax", "All streets")))
        import_file(self.db, self.save("more.xml", ET.tostring(rich)))
        self.assertTrue(list(hero_decisions(self.db, 1, "BigNeonMax", "All streets")))
        self.assertGreater(data_revision(self.db), before)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM origins").fetchone()[0], 2
        )

    def test_same_size_rewrite_with_restored_mtime_still_detected(self):
        raw = original("coin-30.hh")
        path = self.save("session.txt", raw)
        import_file(self.db, path)
        stat = path.stat()
        self.save("session.txt", raw.replace("9000000030", "9000000031"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(import_file(self.db, path), {"new hands": 1})

    def test_duplicate_hm2_text_with_different_container_id_is_checked(self):
        raw = original("ip-601.hh")
        import_file(self.db, self.save("valid.sql", as_copy([(1, "9000000601", raw)])))
        wrong = self.save("wrong.sql", as_copy([(1, "9000000602", raw)]))
        self.assertEqual(import_file(self.db, wrong), {"excluded": 1})
        self.assertEqual(
            self.db.execute("SELECT reason FROM rejected").fetchone()[0],
            "Container and hand ID conflict",
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
