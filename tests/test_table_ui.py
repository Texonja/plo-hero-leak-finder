"""Exercise the visual decision boundary, reveal controls and original source links."""

import json
import tkinter as tk
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from unittest.mock import patch

from leakfinder.demo import load_demo
from leakfinder.storage import connect, import_file, initialize, set_profile
from leakfinder.table_ui import open_table
from tests.test_engine import FIX, original


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class TableUITests(unittest.TestCase):
    def test_early_range_dialog_uses_the_replayed_decision_in_both_rooms(self):
        for room, ref, street, preceding in (
            ("CoinPoker", "Line 28", "FLOP", "Line 27"),
            ("iPoker", "Action 10", "FLOP", "Action 9"),
            ("iPoker", "Action 12", "TURN", "Action 11"),
        ):
            with self.subTest(room=room):
                dialog = open_table(self.root, self.path, self.ids[room], ref)
                try:
                    self.assertEqual(dialog.range_button["state"], "normal")
                    win = dialog.open_ranges()
                    content = next(
                        w for w in descendants(win) if isinstance(w, ScrolledText)
                    )
                    data = content.get("1.0", "end")
                    self.assertIn(f"SCENARIOS FOR {street}", data)
                    self.assertIn(f"[{preceding}]", data)
                    self.assertNotIn(f"[{ref}]", data)
                    self.assertIn("EARLIER HANDS AGAINST THIS OPPONENT", data)
                    if street == "FLOP":
                        self.assertIn(
                            "Model recommendation → recommendation with history", data
                        )
                        self.assertIn("Too few comparable responses", data)
                    self.assertEqual(str(content["state"]), "disabled")
                finally:
                    dialog.close()

    def test_river_range_dialog_opens_selected_decision(self):
        with closing(connect(self.path)) as db:
            load_demo(db)
            hid, hero = db.execute(
                "SELECT id,hint FROM hands WHERE room='CoinPoker' AND number='9000002844'"
            ).fetchone()
        dialog = open_table(self.root, self.path, hid, "Line 32", hero)
        self.assertEqual(dialog.range_button["state"], "normal")
        win = dialog.open_ranges()
        self.addCleanup(dialog.close)
        content = next(w for w in descendants(win) if isinstance(w, ScrolledText))
        text = content.get("1.0", "end")
        self.assertIn("CoinPoker #9000002844 · Line 32", text)
        self.assertIn("EARLIER HANDS AGAINST THIS OPPONENT", text)
        self.assertIn("MODELED RANGE FROM PREFLOP TO RIVER", text)
        self.assertEqual(str(content["state"]), "disabled")

    def test_river_outcome_reveals_source_awards_and_only_recorded_cards(self):
        with closing(connect(self.path)) as db:
            load_demo(db)
            cases = [
                (
                    db.execute(
                        "SELECT id FROM hands WHERE number=?", (number,)
                    ).fetchone()[0],
                    ref,
                    summary,
                    cards,
                )
                for number, ref, summary, cards in (
                    (
                        "9000002844",
                        "Line 32",
                        "HeroDemo collected 0.93 USDT",
                        "Jc 8s 6s 4s",
                    ),
                    (
                        "9000010026",
                        "Action 17",
                        "RunnerFore5t64 collected 23.75 EUR",
                        "2h Qs Td Ts",
                    ),
                    (
                        "9000005623",
                        "Line 36",
                        "Hand ended by folds",
                        "cards unrecorded",
                    ),
                )
            ]
        for hid, ref, summary, cards in cases:
            with self.subTest(ref=ref, summary=summary):
                dialog = open_table(self.root, self.path, hid, ref)
                self.assertEqual(dialog.reveal.cget("text"), "Reveal action & outcome")
                self.assertFalse(dialog.outcome_panel.winfo_manager())
                self.assertNotIn("HAND OUTCOME", dialog.timeline.get("1.0", "end"))
                dialog.reveal.invoke()
                self.assertEqual(dialog.outcome_panel.winfo_manager(), "grid")
                self.assertIn(summary, dialog.outcome_summary.cget("text"))
                self.assertIn(cards, dialog.outcome_cards.cget("text"))
                self.assertIn("Played:", dialog.timeline.get("1.0", "end"))
                self.assertIn("HAND OUTCOME", dialog.timeline.get("1.0", "end"))
                dialog.reveal.invoke()
                self.assertFalse(dialog.outcome_panel.winfo_manager())
                self.assertNotIn("HAND OUTCOME", dialog.timeline.get("1.0", "end"))
                dialog.close()

    def test_imported_hero_hand_shows_guide_before_played_action(self):
        data = json.loads(
            (
                Path(__file__).resolve().parents[1] / "pilots/btn_co/hands.json"
            ).read_text(encoding="utf-8")
        )
        hand = next(h for h in data["hands"] if h["id"] == "9000006096")
        source = Path(self.tmp.name) / "study.txt"
        source.write_text(hand["source"], encoding="utf-8")
        with closing(connect(self.path)) as db:
            import_file(db, source)
            hid, hero = db.execute(
                "SELECT id,hint FROM hands WHERE room=? AND number=?",
                (hand["room"], hand["id"]),
            ).fetchone()
        dlg = open_table(self.root, self.path, hid, hand["action_ref"], hero, hero)
        self.addCleanup(dlg.close)
        timeline = dlg.timeline.get("1.0", "end")
        self.assertIn("GUIDE · FACING AN OPEN · BTN VS CO\nManual review", timeline)
        self.assertIn("Calling keeps position", timeline)
        self.assertNotIn("Played:", timeline)
        dlg.next.invoke()
        self.assertIn("GUIDE · FLOP · BTN", dlg.timeline.get("1.0", "end"))
        self.assertNotIn("FACING AN OPEN", dlg.timeline.get("1.0", "end"))

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "study.sqlite3"
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        native = Path(self.tmp.name) / "native.xml"
        native.write_text(
            original("ip", 601).strip() + "\n</session>", encoding="utf-8"
        )
        with closing(connect(self.path)) as db:
            initialize(db)
            import_file(db, FIX / "coin-30.hh")
            import_file(db, native)
            set_profile(db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})
            self.ids = dict(db.execute("SELECT room,id FROM hands"))

    def test_coin_decision_is_hidden_until_revealed_and_navigation_resets(self):
        dialog = open_table(self.root, self.path, self.ids["CoinPoker"], "Line 28")
        self.root.update()
        snap = dialog.snapshot
        self.assertEqual((snap.ref, snap.board), ("Line 28", ("9c", "5s", "2d")))
        labels = {
            dialog.canvas.itemcget(i, "text")
            for i in dialog.canvas.find_all()
            if dialog.canvas.type(i) == "text"
        }
        self.assertIn("HERO TO ACT", labels)
        self.assertIn("Pot  1.7 USDT", labels)

        def timeline():
            return dialog.timeline.get("1.0", "end")

        self.assertIn("cec82e0e  checks", timeline())
        self.assertNotIn("Played:", timeline())
        self.assertNotIn("TURN", timeline())
        dialog.reveal.invoke()
        self.assertIn("Played: check", timeline())
        dialog.reveal.invoke()
        self.assertNotIn("Played:", timeline())
        dialog.next.invoke()
        self.assertEqual(dialog.snapshot.board, ("9c", "5s", "2d", "Jc"))
        self.assertNotIn("Played:", timeline())
        self.assertEqual(dialog.next["state"], "disabled")
        dialog.prev.invoke()
        self.assertEqual(dialog.snapshot.ref, "Line 28")
        self.assertNotIn("TURN", timeline())

    def test_dialog_and_controls_fit_laptop_screen(self):
        with (
            patch.object(tk.Toplevel, "winfo_screenwidth", return_value=1280),
            patch.object(tk.Toplevel, "winfo_screenheight", return_value=720),
        ):
            dialog = open_table(self.root, self.path, self.ids["CoinPoker"], "Line 28")
            self.root.update()
        self.assertLessEqual(dialog.win.winfo_height(), 625)
        self.assertGreaterEqual(dialog.win.winfo_rooty(), 0)
        self.assertLessEqual(
            dialog.reveal.winfo_rooty() + dialog.reveal.winfo_height(), 720 - 40
        )
        self.assertTrue(dialog.reveal.winfo_ismapped())

    def test_ipoker_seat_fallback_and_complete_source_only_on_request(self):
        dialog = open_table(self.root, self.path, self.ids["iPoker"], "Seat 1")
        self.assertEqual(dialog.entry_label, "First recorded decision")
        self.assertEqual(dialog.snapshot.ref, "Action 5")
        self.assertEqual(dialog.snapshot.hero_cards, ("Kc", "8c", "Jh", "9d"))
        self.assertFalse(dialog.snapshot.board)
        self.assertFalse(
            any(isinstance(w, ScrolledText) for w in descendants(dialog.win))
        )
        dialog.next.invoke()
        source = dialog.open_source()
        original_text = next(
            w for w in descendants(source) if isinstance(w, ScrolledText)
        )
        self.assertIn('no="10"', original_text.get(*original_text.tag_ranges("focus")))
        self.assertIn('type="Turn"', original_text.get("1.0", "end"))
        self.assertEqual(str(original_text["state"]), "disabled")

    def test_shifted_duplicate_source_highlights_equivalent_action(self):
        second = Path(self.tmp.name) / "similar.txt"
        second.write_text(
            original("coin", 30).replace("*** HOLE CARDS ***", "\n*** HOLE CARDS ***"),
            encoding="utf-8",
        )
        with closing(connect(self.path)) as db:
            import_file(db, second)
        dialog = open_table(self.root, self.path, self.ids["CoinPoker"], "Line 28")
        source = dialog.open_source()
        combo = next(w for w in descendants(source) if isinstance(w, ttk.Combobox))
        text = next(w for w in descendants(source) if isinstance(w, ScrolledText))
        self.assertEqual(len(combo["values"]), 2)
        combo.current(1)
        combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(text.get(*text.tag_ranges("focus")), "Hero: checks")
