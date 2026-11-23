import unittest
from contextlib import closing
from dataclasses import replace

from leakfinder.demo import load_demo
from leakfinder.replay_view import load_snapshot
from leakfinder.review_queue import (
    GuideIndex,
    ReviewItem,
    build_queue,
    index_guides,
    prepare_hands,
    review_patterns,
)
from leakfinder.storage import connect, initialize


class PatternTests(unittest.TestCase):
    def test_rank_counts_opportunities_including_agreements(self):
        sample = ReviewItem(
            1,
            "iPoker",
            "1",
            "Hero",
            "GUIDE",
            "Preflop",
            "A",
            "fold",
            "First in",
            "",
            "",
            "Guide",
        )
        entries = [
            replace(
                sample,
                hand=i,
                pattern=name,
                session=str(i % 3),
                action="fold" if i < 19 else "raise",
                difference="action" if i < 19 else "",
            )
            for name, total in (("narrow", 25), ("wide", 250))
            for i in range(total)
        ]
        index = GuideIndex({}, entries)
        groups = review_patterns(index, "Preflop")
        self.assertEqual(
            [(g.key, g.differences, g.total, g.sessions) for g in groups],
            [("narrow", 19, 25, 3), ("wide", 19, 250, 3)],
        )
        self.assertFalse(groups[0].examples[1].difference)
        self.assertGreater(groups[0].interval[0], groups[1].interval[0])
        self.assertEqual(review_patterns(index, "River"), ())
        self.assertTrue(
            all(g.total == 3 for g in review_patterns(index, "Preflop", {1, 2, 20}))
        )

    def test_real_decisions_match_filtered_denominators_and_replay(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            load_demo(db)
            prepare_hands(db, "Both", "All hands")
            index = index_guides(db, "Both")
            for room in ("CoinPoker", "iPoker"):
                q = build_queue(
                    db,
                    room,
                    street="All streets",
                    scope="Last 5 sessions",
                    findings={},
                    guide_index=index,
                )
                self.assertEqual(
                    sum(g.differences for g in q.patterns), len(q.guide_items)
                )
                selected = {
                    row[0] for row in db.execute("SELECT hand FROM review_hands")
                }
                rows = [i for i in index.items if i.hand in selected]
                self.assertEqual(len(rows), q.guide_choices)
                self.assertTrue(rows)
                # The real sample can contain only agreements after an audit.
                self.assertEqual(bool(q.patterns), any(i.difference for i in rows))
                for item in rows[:2]:
                    snap = load_snapshot(db, item.hand, item.hero, item.ref)
                    self.assertEqual(
                        (snap.room, snap.street, snap.hero_cards),
                        (room, item.street, item.cards),
                    )
                self.assertLessEqual(len({i.session for i in rows}), 5)
                for group in q.patterns:
                    self.assertEqual(
                        group.total, sum(i.pattern == group.key for i in rows)
                    )
                    for i in group.examples[:2]:
                        s = load_snapshot(db, i.hand, i.hero, i.ref)
                        self.assertEqual(
                            (s.room, s.street, s.hero_cards), (room, i.street, i.cards)
                        )
