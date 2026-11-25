import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from leakfinder import analysis
from leakfinder.storage import (
    connect,
    data_revision,
    import_file,
    initialize,
    set_profile,
)

from tests.test_library import original, repaired_session


class IncrementalComparisonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)
        set_profile(self.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})

    def add(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return import_file(self.db, path)

    def key(self, room):
        db = self.db
        aliases = list(
            map(tuple, db.execute("SELECT room,name FROM aliases ORDER BY room,name"))
        )
        high = db.execute("SELECT coalesce(max(id),0) FROM hands").fetchone()[0]
        return analysis.comparison_revision(room, data_revision(db), high, aliases)

    def warm(self, room):
        rows, eligible, total = analysis.analyze(self.db, room)
        analysis.save_comparison(
            self.db, room, self.key(room), {f.node: f for f in rows}, eligible, total
        )

    def test_append_merges_same_day_and_matches_full_analysis_in_each_room(self):
        self.add("first.txt", original("coin-30.hh"))
        rooms = ("Both", "CoinPoker", "iPoker")
        for room in rooms:
            self.warm(room)
        self.add("day.txt", original("coin-30.hh").replace("9000000030", "9400000030"))
        self.add(
            "day.xml",
            ElementTree.tostring(
                repaired_session(original("ip-601.hh")), encoding="unicode"
            ),
        )
        for room in rooms:
            with self.subTest(room=room):
                expected = analysis.analyze(self.db, room)
                with patch.object(
                    analysis, "analyze", side_effect=AssertionError("full scan")
                ):
                    self.assertEqual(
                        analysis.update_comparison(self.db, room, self.key(room)),
                        expected,
                    )

    def test_conflicted_old_hand_and_profile_switch_drop_previous_contributions(self):
        source = original("coin-30.hh")
        self.add("first.txt", source)
        self.warm("Both")
        counts = self.add(
            "conflict.txt",
            source.replace("₮1.60", "₮1.59").replace("Rake ₮0.08", "Rake ₮0.09"),
        )
        self.assertEqual(counts.get("conflicting copies"), 1)
        self.assertEqual(
            analysis.update_comparison(self.db, "Both", self.key("Both")), ([], 0, 1)
        )
        self.db.commit()
        self.add("second.txt", original("coin-1.hh"))
        self.warm("Both")
        set_profile(self.db, {})
        self.assertEqual(
            analysis.update_comparison(self.db, "Both", self.key("Both")), ([], 0, 2)
        )

    def test_only_current_v4_cache_is_promoted(self):
        from leakfinder.storage import import_revision

        self.add("first.txt", original("coin-30.hh"))
        room = "Both"
        rows, eligible, total = analysis.analyze(self.db, room)
        aliases = list(
            map(
                tuple,
                self.db.execute("SELECT room,name FROM aliases ORDER BY room,name"),
            )
        )
        old = json.dumps(
            (3, (room, import_revision(self.db), aliases)), separators=(",", ":")
        )
        findings = {f.node: f for f in rows}
        analysis.save_comparison(self.db, room, old, findings, eligible, total)
        analysis.save_comparison(self.db, "CoinPoker", "outdated", {}, 0, 0)
        analysis.migrate_v4_comparisons(self.db)
        key = analysis.comparison_revision(room, 0, 1, aliases)
        self.assertEqual(
            analysis.cached_comparison(self.db, room, key), (findings, eligible, total)
        )
        self.assertIsNone(
            analysis.cached_comparison(
                self.db,
                "CoinPoker",
                analysis.comparison_revision("CoinPoker", 0, 1, aliases),
            )
        )
