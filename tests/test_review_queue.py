"""The Home review queue uses committed source identities, recorded choices and honest evidence."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from leakfinder.analysis import analyze, summarize
from leakfinder.importers import STREETS
from leakfinder.review_queue import (
    GuideIndex,
    ReviewItem,
    build_queue,
    cached_guides,
    guide_revision,
    has_saved_batch,
    index_guides,
    rank_guides,
    remember_batch,
    save_guides,
)
from leakfinder.storage import (
    cohort,
    connect,
    import_file_with_source,
    initialize,
    set_profile,
)
from tests.test_library import as_copy, cell, original


class QueueTests(unittest.TestCase):
    def test_rank_prefers_one_example_of_repeated_actions_then_clear_and_close_spots(
        self,
    ):
        base = ReviewItem(
            1,
            "iPoker",
            "1",
            "Hero",
            "GUIDE",
            "Preflop",
            "Action 4",
            "fold",
            "First in · SB",
            "",
            "",
            "Guide",
            difference="action",
            pattern="first in",
        )
        rows = (
            replace(base, hand=2, priority=3),
            replace(base, hand=3, priority=99, close=True, pattern=""),
            replace(base, hand=4, priority=99, difference="size", pattern=""),
            replace(base, hand=5, priority=1, pattern="isolated"),
            base,
        )
        index = GuideIndex(
            {
                street: ((1, 2), (4, 1)) if street == "Preflop" else ()
                for street in STREETS
            },
            rows,
        )
        guide, count = rank_guides(index, "Preflop")
        self.assertEqual([r.hand for r in guide], [2, 5, 4, 3, 1])
        self.assertEqual((guide[0].repeat_count, count), (2, 3))
        self.assertIn("Close decision", guide[3].review_reason)
        filtered, count = rank_guides(index, "Preflop", {1, 4})
        self.assertEqual([(r.hand, r.repeat_count) for r in filtered], [(1, 1), (4, 0)])
        self.assertEqual(count, 3)

    def test_index_roundtrip_projects_room_and_batch_without_new_replays(self):
        coin, ip = self.both()
        cohort(self.db)
        index = index_guides(self.db, "Both")
        revision = guide_revision((ip, tuple(self.aliases["iPoker"])))
        save_guides(self.db, "Both", revision, index)
        self.assertIsNone(cached_guides(self.db, "Both", guide_revision((ip + 1, ()))))
        reloaded = cached_guides(self.db, "Both", revision)
        self.assertEqual(reloaded, index)
        remember_batch(self.db, [coin])
        queue = build_queue(self.db, "Both", street="Flop", guide_index=reloaded)
        self.assertEqual(queue.hand_count, 1)
        self.assertTrue(all(item.room == "CoinPoker" for item in queue.guide_items))
        all_ip = build_queue(self.db, "iPoker", scope="All hands", guide_index=reloaded)
        self.assertEqual(all_ip.hand_count, 1)
        self.assertTrue(all(item.room == "iPoker" for item in all_ip.guide_items))

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "library.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.aliases = {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]}
        set_profile(self.db, self.aliases)

    def ingest(self, name, raw):
        path = self.folder / name
        path.write_text(raw, encoding="utf-8")
        return import_file_with_source(self.db, path)

    def both(self):
        coin = self.ingest("coin.txt", original("coin-30.hh"))[1]
        ip = self.ingest(
            "ip.sql", as_copy([(601, "9000000601", original("ip-601.hh"))])
        )[1]
        return coin, ip

    def test_room_profiles_street_scope_and_source_actions(self):
        coin, ip = self.both()
        self.assertFalse(has_saved_batch(self.db))
        all_ = build_queue(self.db, street="Flop")
        self.assertEqual(
            (all_.scope, all_.hand_count, all_.library_hands), ("All hands", 2, 2)
        )
        self.assertCountEqual(
            [(r.room, r.number, r.ref, r.hero, r.action, r.node) for r in all_.items],
            [
                (
                    "CoinPoker",
                    "9000000030",
                    "Line 28",
                    "Hero",
                    "check",
                    "BET_IP_VS_MISSED_FLOP_CB",
                ),
                (
                    "iPoker",
                    "9000000601",
                    "Action 10",
                    "BigNeonMax",
                    "check",
                    "BET_IP_VS_MISSED_FLOP_CB",
                ),
            ],
        )
        for r in all_.items:
            self.assertEqual((r.street, r.status), ("Flop", "Exploratory"))
            self.assertIn("checked", r.reason)
            self.assertIn("0 of 1", r.evidence)
            self.assertNotIn("showdown", r.reason.lower())
        self.assertEqual(remember_batch(self.db, [ip, ip]), 1)
        self.assertTrue(has_saved_batch(self.db))
        latest = build_queue(self.db, street="Turn")
        self.assertEqual(
            (
                latest.scope,
                latest.hand_count,
                latest.library_hands,
                latest.source_count,
            ),
            ("Latest import", 1, 2, 1),
        )
        self.assertEqual(
            [(r.ref, r.action, r.node) for r in latest.items],
            [("Action 12", "fold", "TURNF")],
        )
        self.assertIn("One observed hand", latest.items[0].evidence)
        self.assertEqual(build_queue(self.db, "CoinPoker").items, ())
        self.assertEqual(
            build_queue(self.db, "CoinPoker", scope="All hands").hand_count, 1
        )
        self.assertEqual(remember_batch(self.db, [coin, ip]), 2)
        self.assertEqual(build_queue(self.db).hand_count, 2)
        self.assertEqual(build_queue(self.db).items, build_queue(self.db).items)
        set_profile(self.db, {"CoinPoker": ["Hero", "cec82e0e"], "iPoker": ["Other"]})
        self.assertEqual(build_queue(self.db).hand_count, 0)
        set_profile(self.db, self.aliases)
        self.db.commit()
        reopened = connect(self.path)
        try:
            initialize(reopened)
            self.assertEqual(build_queue(reopened).source_count, 2)
            self.assertEqual(build_queue(reopened).items, build_queue(self.db).items)
        finally:
            reopened.close()

    def test_duplicates_path_revisions_and_excluded_batches(self):
        original_text = original("coin-30.hh")
        counts, first = self.ingest("session.txt", original_text)
        self.assertEqual(counts, {"new hands": 1})
        _, second = self.ingest("session.txt", original("coin-heads-up.hh"))
        self.assertNotEqual(first, second)
        counts, old = self.ingest("session.txt", original_text)
        self.assertEqual((counts, old), ({"already imported": 1}, first))
        counts, copy = self.ingest("copy.txt", original_text)
        self.assertEqual(counts, {"duplicate origins": 1})
        remember_batch(self.db, [old, copy])
        latest = build_queue(self.db)
        self.assertEqual(
            (latest.hand_count, latest.library_hands, latest.source_count), (1, 2, 2)
        )
        self.assertEqual([r.number for r in latest.items], ["9000000030"])
        self.assertEqual(remember_batch(self.db, []), 0)
        self.assertEqual(build_queue(self.db), latest)
        counts, bad = self.ingest(
            "invalid.txt", original_text.replace("Hero: checks", "Hero: calls ₮150")
        )
        self.assertEqual(counts, {"excluded": 1})
        remember_batch(self.db, [bad])
        latest = build_queue(self.db)
        self.assertEqual(
            (latest.scope, latest.hand_count, latest.library_hands, latest.items),
            ("Latest import", 0, 2, ()),
        )
        self.assertEqual(len(build_queue(self.db, scope="All hands").items), 2)

    def test_supported_node_needs_an_aligned_action_and_a_matching_cell(self):
        _, src = self.ingest("coin.txt", original("coin-heads-up.hh"))
        remember_batch(self.db, [src])
        findings = {f.node: f for f in analyze(self.db, "CoinPoker")[0]}
        key = next(iter(findings["THREEBET"].cells))
        # Manufactured high-evidence library; the Home example itself is
        # a real recorded BB fold and can only illustrate a lower raise rate.
        for hx, px, expect in ((200, 7200, "Supported"), (1800, 800, "Exploratory")):
            findings["THREEBET"] = summarize(
                "THREEBET", {key: cell(2000, hx, 8000, px)}, "CoinPoker"
            )
            self.assertEqual(findings["THREEBET"].status, "Supported")
            item = build_queue(self.db, "CoinPoker", findings=findings, limit=1).items[
                0
            ]
            self.assertEqual(
                (item.ref, item.action, item.status), ("Line 13", "fold", expect)
            )
            if expect == "Supported":
                self.assertIn("8,000", item.evidence)
                self.assertIn("Adjusted range", item.evidence)
        other = json.loads(key)
        other[7] += 1
        findings["THREEBET"] = summarize(
            "THREEBET",
            {
                key: cell(1, 0, 0, 0),
                json.dumps(other, separators=(",", ":")): cell(2000, 200, 8000, 7200),
            },
            "CoinPoker",
        )
        self.assertEqual(findings["THREEBET"].status, "Supported")
        item = build_queue(self.db, "CoinPoker", findings=findings, limit=1).items[0]
        self.assertEqual(item.status, "Exploratory")
        self.assertIn("No matching opponent", item.evidence)

    def test_exploratory_and_unsupported_definitions_never_become_supported(self):
        self.both()
        self.ingest("coin-first.txt", original("coin-1.hh"))
        self.assertFalse(
            any(i.status == "Supported" for i in build_queue(self.db).items)
        )
        for street in ("Preflop", "Flop", "Turn", "River"):
            chosen = build_queue(self.db, street=street, limit=50)
            self.assertEqual(len(chosen.items), len({i.hand for i in chosen.items}))
            self.assertTrue(all(i.street == street for i in chosen.items))
            self.assertTrue(
                all(
                    i.node not in ("FFC", "NET", "SHOW", "WONSHOW")
                    and not i.ref.startswith("Seat")
                    for i in chosen.items
                )
            )
        self.assertEqual(build_queue(self.db, limit=0).items, ())

    def test_matched_tiny_sample_uses_plain_language(self):
        self.ingest("coin.txt", original("coin-heads-up.hh"))
        findings = {f.node: f for f in analyze(self.db, "CoinPoker")[0]}
        key = next(iter(findings["THREEBET"].cells))
        findings["THREEBET"] = summarize(
            "THREEBET", {key: cell(3, 0, 1, 1, 1)}, "CoinPoker"
        )
        item = build_queue(self.db, "CoinPoker", findings=findings).items[0]
        self.assertEqual(item.status, "Exploratory")
        self.assertIn("you raised on 0 of 3", item.evidence)
        self.assertIn("1 opponent decision", item.evidence)
        self.assertIn("too small", item.evidence)
