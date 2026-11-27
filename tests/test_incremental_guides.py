"""An incremental review must produce the same decisions and ordering as a clean replay."""

import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from leakfinder.importers import sources
from leakfinder.opponent_history import MIN_CASES, observations
from leakfinder.review_queue import (
    cached_guides,
    guide_revision,
    index_guides,
    save_guides,
    update_guides,
)
from leakfinder.storage import (
    cohort,
    connect,
    data_revision,
    import_file,
    import_revision,
    initialize,
    set_profile,
)

from tests.test_library import original


class IncrementalGuideTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.folder = Path(work.name)
        self.db = connect(self.folder / "library.db")
        self.addCleanup(self.db.close)
        initialize(self.db)
        set_profile(self.db, {"iPoker": ["BigNeonMax"], "CoinPoker": ["HeroDemo"]})

    def ingest(self, filename, raw):
        path = self.folder / filename
        path.write_text(raw, encoding="utf-8")
        return import_file(self.db, path)

    def aliases(self):
        return [
            tuple(row)
            for row in self.db.execute(
                "SELECT room,name FROM aliases ORDER BY room,name"
            )
        ]

    def refresh(self):
        cohort(self.db)
        revision = guide_revision((data_revision(self.db), self.aliases()))
        stats = {}
        result = update_guides(self.db, "Both", revision, stats=stats)
        self.assertEqual(result, index_guides(self.db, "Both"))
        self.db.commit()
        save_guides(self.db, "Both", revision, result)
        return result, stats

    def test_backfilled_opponent_observation_and_later_conflict(self):
        late = original("ip-41.hh")
        early = late.replace("9000000041", "9000900041").replace(
            "2026-04-19", "2026-04-18"
        )
        self.ingest("early.xml", early)
        _, first = self.refresh()
        self.assertEqual(first["replayed"], 1)
        self.ingest("late.xml", late)
        _, daily = self.refresh()
        self.assertEqual(daily, {"replayed": 1, "reused": 1, "history_updated": 0})
        oldest = early.replace("9000900041", "9000900042").replace(
            "2026-04-18", "2026-04-17"
        )
        self.ingest("backfill.xml", oldest)
        _, backfill = self.refresh()
        self.assertEqual(backfill, {"replayed": 3, "reused": 0, "history_updated": 2})
        # Same hand, different river card: the old observation must disappear.
        self.assertEqual(
            self.ingest("conflict.xml", oldest.replace(">C7<", ">D7<")),
            {"conflicting copies": 1},
        )
        _, conflict = self.refresh()
        self.assertEqual(conflict, {"replayed": 2, "reused": 0, "history_updated": 2})
        _, rerun = self.refresh()
        self.assertEqual(rerun, {"replayed": 0, "reused": 2, "history_updated": 0})
        set_profile(self.db, {"CoinPoker": ["HeroDemo"]})
        empty, _ = self.refresh()
        self.assertFalse(any((empty.items, empty.study, empty.critiques)))

    def test_adding_and_removing_an_alias_reuses_other_hands(self):
        first = original("ip-41.hh")
        second = (
            first.replace("9000000041", "9000900041")
            .replace("2026-04-19", "2026-04-20")
            .replace("BigNeonMax", "OtherAlias")
        )
        self.ingest("first.xml", first)
        self.ingest("second.xml", second)
        baseline, _ = self.refresh()

        set_profile(self.db, {"iPoker": ["BigNeonMax", "OtherAlias"]})
        _, added = self.refresh()
        self.assertEqual(added, {"replayed": 1, "reused": 1, "history_updated": 0})

        set_profile(self.db, {"iPoker": ["BigNeonMax"]})
        result, removed = self.refresh()
        self.assertEqual(removed, {"replayed": 0, "reused": 1, "history_updated": 0})
        self.assertEqual(result, baseline)

        # Two configured aliases seated in the same hand make Hero ambiguous.
        rival = self.db.execute(
            "SELECT s.name FROM cohort q JOIN seats s ON s.hand=q.hand WHERE s.name<>q.hero"
        ).fetchone()[0]
        set_profile(self.db, {"iPoker": ["BigNeonMax", rival]})
        ambiguous, _ = self.refresh()
        self.assertFalse(any(ambiguous.counts.values()))

    def test_session_bridge_relabels_old_guide_items_without_replay(self):
        demo = Path(__file__).resolve().parents[1] / "demo" / "CoinPoker-HeroDemo.txt"
        raw = next(
            record.raw for record in sources(demo) if "#9000009980:" in record.raw
        )
        for number, start, end in (
            ("1", "08:00:00", "08:01:30"),
            ("2", "09:00:00", "09:01:30"),
            ("3", "08:30:00", "08:31:30"),
        ):
            self.ingest(
                number + ".txt",
                raw.replace("9000009980", number)
                .replace("08:40:00", start)
                .replace("08:41:30", end),
            )
            result, stats = self.refresh()
            self.assertTrue(result.items)
            self.assertEqual(stats["replayed"], 1)
            self.assertEqual(
                len({r.session for r in result.items}), 2 if number == "2" else 1
            )
        self.assertEqual(stats["reused"], 2)

    def test_thirtieth_prior_observation_updates_a_later_river_suggestion(self):
        demo = Path(__file__).resolve().parents[1] / "demo" / "CoinPoker-HeroDemo.txt"
        template = next(r.raw for r in sources(demo) if "#9000000003:" in r.raw)

        def copy(number, day):
            return template.replace("9000000003", str(number)).replace(
                "2026/01/01", f"2026/01/{day}"
            )

        # Vary only the hand number and timestamp on this public called river bet.
        self.ingest(
            "sessions.txt",
            "\n".join(
                [copy(10000 + i, 20 + i % 3) for i in range(29)] + [copy(20000, 25)]
            ),
        )
        before, _ = self.refresh()
        unfiltered = list(observations(self.db, "Both", prepared=True))
        self.assertEqual(len(unfiltered), MIN_CASES)
        self.assertEqual(
            list(observations(self.db, "Both", prepared=True, minimum=MIN_CASES + 1)),
            [],
        )
        self.assertEqual(
            list(observations(self.db, "Both", prepared=True, minimum=MIN_CASES)),
            unfiltered,
        )
        self.ingest("older-session.txt", copy(10029, 24))
        after, stats = self.refresh()

        def explanation(index):
            return next(
                s.explanation
                for s in index.study
                if s.number == "20000" and s.ref == "Line 39"
            )

        self.assertNotIn("30 of 30", explanation(before))
        self.assertIn("30 of 30 comparable bets", explanation(after))
        self.assertIn("supports a call", explanation(after))
        self.assertEqual(stats, {"replayed": 2, "reused": 29, "history_updated": 1})

    def test_v4_guide_rebuilds_once_then_extends_after_first_new_import(self):
        self.ingest("original.xml", original("ip-41.hh"))
        cohort(self.db)
        old = index_guides(self.db, "Both")
        v4_key = json.dumps(
            (9, (import_revision(self.db), self.aliases())), separators=(",", ":")
        )
        save_guides(self.db, "Both", v4_key, old)
        # Model the v4 database immediately before upgrade.
        with self.db:
            self.db.execute("DELETE FROM hand_changes")
            self.db.execute("PRAGMA user_version=4")
        initialize(self.db)
        initial = guide_revision((0, self.aliases()))
        self.assertIsNone(cached_guides(self.db, "Both", initial))
        _, rebuilt = self.refresh()
        self.assertEqual(rebuilt["replayed"], 1)
        self.ingest(
            "next.xml",
            original("ip-41.hh")
            .replace("9000000041", "9000900001")
            .replace("2026-04-19", "2026-04-20"),
        )
        _, stats = self.refresh()
        self.assertEqual(stats["replayed"], 1)
        self.assertEqual(stats["reused"], 1)

    def test_reopen_noop_and_invalid_watermark(self):
        self.ingest("session.xml", original("ip-41.hh"))
        baseline, _ = self.refresh()
        key = guide_revision((data_revision(self.db), self.aliases()))
        with closing(connect(self.folder / "library.db")) as reopened:
            initialize(reopened)
            cohort(reopened)
            with patch(
                "leakfinder.review_queue.hero_decisions", side_effect=AssertionError
            ):
                self.assertEqual(update_guides(reopened, "Both", key), baseline)
            for broken in (
                '{"old":"format"}',
                '[10,["oops",[]]]',
                "not json",
                "[9,[0,[]]]",
            ):
                with self.subTest(broken=broken):
                    reopened.execute(
                        "UPDATE review_guide_cache SET revision=?", (broken,)
                    )
                    stats = {}
                    self.assertEqual(
                        update_guides(reopened, "Both", key, stats=stats), baseline
                    )
                    self.assertEqual(stats["replayed"], 1)


if __name__ == "__main__":
    unittest.main()
