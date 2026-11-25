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
    def wait_for_playback(self, dialog):
        finished = tk.BooleanVar(master=self.root)
        deadline = self.root.after(4500, finished.set, True)

        def check():
            if dialog.play_timer is None:
                finished.set(True)
            else:
                self.root.after(10, check)

        self.root.after(10, check)
        self.root.wait_variable(finished)
        self.root.after_cancel(deadline)

    def test_transport_steps_through_real_actions_and_returns_to_the_decision(self):
        for room, ref, actor, expected in (
            ("CoinPoker", "Line 25", "Hero", "calls 0.5"),
            ("iPoker", "Action 5", "BigNeonMax", "calls 1.75"),
        ):
            with self.subTest(room=room):
                dlg = open_table(self.root, self.path, self.ids[room], ref)
                self.root.update()
                dlg.forward.invoke()
                frame = dlg.frames[dlg.cursor]
                self.assertEqual((frame.step.ref, frame.step.name), (ref, actor))
                self.assertIn(expected, dlg.timeline.get("1.0", "end"))
                self.assertTrue(dlg.canvas.find_withtag("wager"))
                self.assertTrue(dlg.canvas.find_withtag("moving"))
                dlg.back.invoke()
                self.assertEqual(dlg.frames[dlg.cursor].pot, dlg.snapshot.pot)
                dlg.street_buttons["Flop"].invoke()
                self.assertEqual(dlg.frames[dlg.cursor].phase, "deal")
                self.assertEqual(len(dlg.frames[dlg.cursor].board), 3)
                self.assertFalse(dlg.outcome_panel.winfo_manager())
                self.assertNotIn("TURN", dlg.timeline.get("1.0", "end"))
                dlg.reveal.invoke()
                self.assertIsNone(dlg.cursor)
                self.assertEqual(dlg.snapshot.ref, ref)
                self.assertFalse(dlg.revealed)
                dlg.play.invoke()
                self.assertEqual(dlg.cursor, 0)
                self.assertIsNotNone(dlg.play_timer)
                dlg.play.invoke()
                self.assertIsNone(dlg.play_timer)
                with patch.dict("leakfinder.table_ui.DELAYS", {"2×": 1}):
                    dlg.speed.set("2×")
                    dlg.play.invoke()
                    # Run the real Tk timer chain through settlement.
                    self.wait_for_playback(dlg)
                self.assertEqual(dlg.frames[dlg.cursor].phase, "result")
                self.assertTrue(dlg.outcome_panel.winfo_manager())
                self.assertIn("collected", dlg.outcome_summary.cget("text"))
                self.assertEqual(dlg.forward["state"], "disabled")
                dlg.play.invoke()
                dlg.close()
                self.assertIsNone(dlg.play_timer)

    def test_all_in_replay_deals_remaining_streets_before_payout(self):
        with closing(connect(self.path)) as db:
            load_demo(db)
            ids = dict(db.execute("SELECT number,id FROM hands"))
        for number, ref, river in (
            ("9000005616", "Line 29", "3h"),
            ("9000008019", "Action 14", "2h"),
        ):
            with self.subTest(ref=ref):
                dlg = open_table(self.root, self.path, ids[number], ref)
                dlg.street_buttons["River"].invoke()
                self.assertEqual(dlg.frames[dlg.cursor].board[-1], river)
                self.assertFalse(dlg.outcome_panel.winfo_manager())
                self.assertNotIn("collected", dlg.timeline.get("1.0", "end"))
                dlg.forward.invoke()
                self.assertIn("HeroDemo collected", dlg.outcome_summary.cget("text"))
                dlg.back.invoke()
                self.assertFalse(dlg.outcome_panel.winfo_manager())
                dlg.close()

    def test_amount_switch_updates_the_table_history_and_revealed_outcome(self):
        for room, ref, price, pot, stack, bb_price, bb_pot, bb_stack in (
            (
                "CoinPoker",
                "Line 25",
                "0.5 USDT",
                "1.2 USDT",
                "25.7 USDT",
                "2 BB",
                "4.8 BB",
                "102.8 BB",
            ),
            (
                "iPoker",
                "Action 5",
                "1.75 EUR",
                "2.5 EUR",
                "50 EUR",
                "3.5 BB",
                "5 BB",
                "100 BB",
            ),
        ):
            with self.subTest(room=room):
                dlg = open_table(self.root, self.path, self.ids[room], ref)
                self.root.update()

                def table_text():
                    return [
                        dlg.canvas.itemcget(i, "text")
                        for i in dlg.canvas.find_all()
                        if dlg.canvas.type(i) == "text"
                    ]

                labels = table_text()
                self.assertIn(f"Pot  {pot}", labels)
                self.assertTrue(any(t.startswith(f"To call  {price}") for t in labels))
                self.assertIn(
                    f"{next(s.position for s in dlg.snapshot.seats if s.is_hero)}  ·  {stack}",
                    labels,
                )
                dlg.reveal.invoke()
                self.assertIn(
                    f"Played: call · {price} paid", dlg.timeline.get("1.0", "end")
                )

                dlg.amount_unit.set("BB")
                with patch(
                    "leakfinder.table_ui.advice_at",
                    side_effect=AssertionError("Recomputing advice"),
                ):
                    dlg.amount_unit.event_generate("<<ComboboxSelected>>")
                labels = table_text()
                self.assertIn(f"Pot before decision  {bb_pot}", labels)
                self.assertIn(f"To call  {bb_price}", labels)
                self.assertTrue(any(t.endswith(f"  ·  {bb_stack}") for t in labels))
                self.assertIn(
                    f"Played: call · {bb_price} paid", dlg.timeline.get("1.0", "end")
                )
                self.assertIn(" BB", dlg.outcome_summary.cget("text"))
                self.assertTrue(dlg.revealed)
                self.assertEqual(dlg.snapshot.ref, ref)

                dlg.reveal.invoke()
                self.assertIn(f"Pot  {bb_pot}", table_text())
                self.assertIn("3 BB" if room == "CoinPoker" else "3.5 BB", table_text())
                self.assertNotIn("HAND OUTCOME", dlg.timeline.get("1.0", "end"))
                dlg.close()

    def test_amount_switch_during_playback_keeps_frame_and_converts_payout(self):
        dlg = open_table(self.root, self.path, self.ids["CoinPoker"], "Line 25")
        self.addCleanup(dlg.close)
        self.root.update()
        frame = next(
            i for i, f in enumerate(dlg.frames) if f.step and f.step.action == "raise"
        )
        dlg.seek(frame)
        dlg.play.invoke()
        timer = dlg.play_timer
        self.assertIsNotNone(timer)
        dlg.amount_unit.set("BB")
        dlg.amount_unit.event_generate("<<ComboboxSelected>>")
        self.assertEqual((dlg.cursor, dlg.play_timer), (frame, timer))
        self.assertEqual(dlg.play.cget("text"), "Ⅱ Pause")
        timeline = dlg.timeline.get("1.0", "end")
        self.assertIn("raises to 3 BB (+2.6 BB)", timeline)
        self.assertIn("raises to 3 BB (+2.6 BB)", dlg.status.cget("text"))
        self.assertIn(
            "In middle  0.8 BB   ·   In front  4 BB",
            [
                dlg.canvas.itemcget(i, "text")
                for i in dlg.canvas.find_all()
                if dlg.canvas.type(i) == "text"
            ],
        )

        dlg.seek(len(dlg.frames) - 1)
        summary = dlg.outcome_summary.cget("text")
        self.assertIn("Hero collected 6.4 BB", summary)
        self.assertIn("Hero +3.24 BB", summary)
        labels = {
            dlg.canvas.itemcget(i, "text")
            for i in dlg.canvas.find_all()
            if dlg.canvas.type(i) == "text"
        }
        self.assertIn("+6.4 BB", labels)
        self.assertIn("Final pot  6.8 BB", labels)
        self.assertIn("Rake and fees  0.4 BB", labels)
        self.assertIn("receives refund 6.8 BB", dlg.timeline.get("1.0", "end"))
        dlg.amount_unit.set("Currency")
        dlg.amount_unit.event_generate("<<ComboboxSelected>>")
        summary = dlg.outcome_summary.cget("text")
        self.assertIn("Hero collected 1.6 USDT", summary)
        self.assertIn("Hero +0.81 USDT", summary)
        self.assertEqual(dlg.frames[dlg.cursor].phase, "result")

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
                        self.assertIn("Hypothetical bet/fold scenario", data)
                        self.assertIn("insufficient for a personal sizing recommendation", data)
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

    def test_early_all_in_reveals_final_board_without_a_river_decision(self):
        with closing(connect(self.path)) as db:
            load_demo(db)
            ids = dict(db.execute("SELECT number,id FROM hands"))
        cases = (
            (
                "9000005616",
                "Line 29",
                "Flop",
                "8d Tc 9h 2h 3h",
                "HeroDemo collected 27.42 USDT",
                "Ad Qd 8s 8c",
            ),
            (
                "9000008019",
                "Action 14",
                "Turn",
                "2c Jd Kc Ad 2h",
                "HeroDemo collected 65.76 EUR",
                "7d Ac 6h 3c",
            ),
        )
        for number, ref, street, board, payout, cards in cases:
            with self.subTest(street=street):
                dialog = open_table(self.root, self.path, ids[number], ref)
                snapshot = dialog.snapshot
                self.assertEqual(snapshot.street, street)
                self.assertNotIn("River", {item.street for item in dialog.options})
                self.assertFalse(dialog.outcome_panel.winfo_manager())
                self.assertNotIn(board, dialog.timeline.get("1.0", "end"))
                dialog.reveal.invoke()
                self.root.update()
                timeline = dialog.timeline.get("1.0", "end")
                self.assertIn(f"Final board · {board}", timeline)
                self.assertIn(payout, dialog.outcome_summary.cget("text"))
                self.assertIn(cards, dialog.outcome_cards.cget("text"))
                self.assertEqual(dialog.snapshot, snapshot)
                self.assertTrue(
                    any(
                        dialog.canvas.type(item) == "text"
                        and dialog.canvas.itemcget(item, "text") == "FINAL BOARD"
                        for item in dialog.canvas.find_all()
                    )
                )
                dialog.reveal.invoke()
                self.assertNotIn(board, dialog.timeline.get("1.0", "end"))
                self.assertFalse(dialog.outcome_panel.winfo_manager())
                dialog.close()

    def test_reveal_from_flop_includes_later_betting_streets(self):
        with closing(connect(self.path)) as db:
            load_demo(db)
            hid = db.execute(
                "SELECT id FROM hands WHERE number='9000010026'"
            ).fetchone()[0]
        dialog = open_table(self.root, self.path, hid, "Action 9")
        self.addCleanup(dialog.close)
        dialog.reveal.invoke()
        timeline = dialog.timeline.get("1.0", "end").split("HAND OUTCOME\n", 1)[1]
        self.assertLess(timeline.index("TURN\n"), timeline.index("RIVER\n"))
        self.assertIn("Final board · 5c Jc 8s 4s 9h", timeline)
        dialog.next.invoke()
        self.assertFalse(dialog.outcome_panel.winfo_manager())
        self.assertNotIn("HAND OUTCOME", dialog.timeline.get("1.0", "end"))

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
        self.assertTrue(dialog.amount_unit.winfo_ismapped())

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
