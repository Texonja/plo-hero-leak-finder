import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leakfinder.critique import critique_for
from leakfinder.demo import SAMPLES, load_demo
from leakfinder.engine import replay
from leakfinder.importers import Excluded, Record, decode
from leakfinder.replay_view import playback_frames, snapshot_from_record
from leakfinder.statistics import BITS, observations
from leakfinder.storage import (
    connect,
    file_digest,
    import_file_with_source,
    import_revision,
    initialize,
)
from tests.test_engine import decoded, paired


class FreeCheckFoldTests(unittest.TestCase):
    def test_replay_and_stats_on_both_networks_and_every_street(self):
        check_through = [("A", "check", 0), ("B", "check", 0)]
        free_fold = [("A", "check", 0), ("B", "fold", 0)]
        limp_to_bb = [("C", "fold", 0), ("A", "call", 0.25), ("B", "fold", 0)]
        deals = [("Preflop", paired([], pf=limp_to_bb))]
        for i, street in enumerate(("Flop", "Turn", "River")):
            deals.append((street, paired([check_through] * i + [free_fold])))
        for street, pair in deals:
            for room, raw in zip(("CoinPoker", "iPoker"), pair):
                with self.subTest(street=street, room=room):
                    record = Record(
                        room,
                        raw,
                        "Text hand 1" if room == "CoinPoker" else "XML game 1",
                    )
                    hand = decode(record)
                    decisions, events = replay(hand)
                    event = events[street][-1]
                    self.assertEqual(
                        (event.player, event.action, event.owed, event.amount),
                        ("B", "fold", 0, 0),
                    )
                    snap = snapshot_from_record(record, "B", event.ref)
                    self.assertEqual(
                        critique_for(snap, event, None).rule, "fold_free_check"
                    )
                    frames = playback_frames(hand, events, "B")
                    frame = next(
                        f for f in frames if f.step and f.step.ref == event.ref
                    )
                    self.assertFalse(next(s.live for s in frame.seats if s.name == "B"))
                    self.assertEqual(frame.pot, snap.pot)
                    self.assertFalse(any(d.event.ref == event.ref for d in decisions))
                    masks = [
                        (row[-2], row[-1])
                        for row in observations(hand, decisions, events)
                        if row[1] == event.ref
                    ]
                    if street != "Preflop":
                        self.assertEqual(masks, [(BITS[street.upper() + "_AGG"], 0)])

    def test_paid_folds_remain_folds_to_cbet_and_bad_actions_remain_excluded(self):
        for hand in decoded(paired([[("A", "bet", 1), ("B", "fold", 0)]])):
            ds, evs = replay(hand)
            event = evs["Flop"][-1]
            obs = next(
                row for row in observations(hand, ds, evs) if row[1] == event.ref
            )
            self.assertGreater(event.owed, 0)
            self.assertTrue(obs[-2] & obs[-1] & BITS["FFC"])
        for hand in decoded(paired([[("B", "fold", 0)]])):
            with self.assertRaisesRegex(Excluded, "Action order"):
                replay(hand)
        txt, xml = paired([[("A", "check", 0), ("B", "fold", 0.25)]])
        with self.assertRaisesRegex(Excluded, "Unknown CoinPoker action"):
            decode(Record("CoinPoker", txt, "Text hand 1"))
        with self.assertRaisesRegex(Excluded, "Unexpected chip payment"):
            replay(decode(Record("iPoker", xml, "XML game 1")))
        for hand in decoded(paired([[("B", "fold", 0)]])):
            # The preflop raiser has no chips left after the call. No one
            # can take another betting action as the board runs out.
            hand.stacks["A"] = 3 * hand.bb
            with self.assertRaisesRegex(Excluded, "No betting option"):
                replay(hand)

    def test_retry_only_legacy_rows_preserving_summary_and_source(self):
        paid_fold, _ = paired([[("A", "bet", 1), ("B", "fold", 0)]], number="101")
        free_fold, _ = paired([[("A", "check", 0), ("B", "fold", 0)]], number="102")
        bad_tail, _ = paired(
            [[("A", "check", 0), ("B", "fold", 0), ("A", "bet", 1)]], number="103"
        )
        db = connect(":memory:")
        self.addCleanup(db.close)
        initialize(db)

        def old_replay(hand):
            if hand.number in {"102", "103"}:
                raise Excluded("Fold with no price")
            return replay(hand)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.txt"
            path.write_text(f"{paid_fold}\n{free_fold}\n{bad_tail}", encoding="utf-8")
            with patch("leakfinder.storage.replay", side_effect=old_replay):
                result, source = import_file_with_source(db, path)
            self.assertEqual(result, {"new hands": 1, "excluded": 2})
            before = import_revision(db)
            with (
                patch(
                    "leakfinder.storage.file_digest",
                    side_effect=[file_digest(path), "different"],
                ),
                self.assertRaisesRegex(ValueError, "changed during import"),
            ):
                import_file_with_source(db, path)
            self.assertEqual(import_revision(db), before)
            self.assertEqual(
                import_file_with_source(db, path),
                ({"new hands": 1, "excluded": 1}, source),
            )
            self.assertNotEqual(import_revision(db), before)
            self.assertEqual(
                [tuple(r) for r in db.execute("SELECT locator,reason FROM rejected")],
                [("Text hand 3", "Action order or inactive player")],
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM origins WHERE source=?", (source,)
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                json.loads(db.execute("SELECT summary FROM imports").fetchone()[0]),
                {"new hands": 2, "excluded": 1},
            )
            self.assertEqual(
                import_file_with_source(db, path), ({"already imported": 1}, source)
            )

    def test_moved_demo_retries_matching_bytes_without_duplicating_sources(self):
        db = connect(":memory:")
        self.addCleanup(db.close)
        initialize(db)
        cp, _ = paired([[("A", "check", 0), ("B", "fold", 0)]])
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "old"
            current = Path(directory) / "new"
            previous.mkdir()
            current.mkdir()
            old = previous / "CoinPoker-HeroDemo.txt"
            old.write_text(cp, encoding="utf-8")
            with patch(
                "leakfinder.storage.replay", side_effect=Excluded("Fold with no price")
            ):
                _, source = import_file_with_source(db, old)
            old.rename(current / old.name)
            # Seed an unrelated imported demo source that does not require retry.
            with db:
                db.execute(
                    "INSERT INTO imports(path,digest,summary) VALUES(?,?,?)",
                    (str(previous / "iPoker-HeroDemo.sql"), "old", "{}"),
                )
            supplement = "iPoker-HeroDemo-Review.xml"
            (current / supplement).write_bytes((SAMPLES / supplement).read_bytes())
            with patch("leakfinder.demo.SAMPLES", current):
                load_demo(db)
                load_demo(db)
            self.assertEqual(
                db.execute("SELECT count(*) FROM imports").fetchone()[0], 3
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM origins WHERE source=?", (source,)).fetchone()[0], 1
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM hands").fetchone()[0], 5
            )
