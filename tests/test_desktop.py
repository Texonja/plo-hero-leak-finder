"""Exercise desktop menus, profile isolation and source drill-through with real SQLite."""

import gc
import json
import time
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

from leakfinder.demo import demo_path
from leakfinder.home import PLAYED_RED, SUGGESTED_GREEN
from leakfinder.importers import Record, decode
from leakfinder.storage import profile, set_profile
from leakfinder.ui import Workspace
from tests.test_library import original, repaired_session


def child_widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from child_widgets(child)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        # Release any prior closed test interpreter on its creating thread,
        # before the next test allocates objects in an import worker.
        gc.collect()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.personal = Path(self.tmp.name) / "data" / "mine.sqlite3"
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.errors = []
        self.root.report_callback_exception = lambda cls, exc, tb: self.errors.append(
            str(exc)
        )
        self.app = Workspace(self.root, self.personal, start_demo=True)
        self.addCleanup(self.app.close)
        self.wait()

    def wait(self):
        end = time.monotonic() + 30
        while self.app.busy and time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy, "background UI task failed to finish")
        self.assertFalse(
            self.app.status.get().startswith("Operation failed"), self.app.status.get()
        )
        self.assertEqual(self.errors, [])

    def test_demo_uses_real_hands_from_both_rooms_and_marks_xml_action(self):
        self.assertTrue(self.app.is_demo)
        self.assertFalse(self.personal.exists())
        counts = dict(
            self.app.db.execute("SELECT room,count(*) FROM hands GROUP BY room")
        )
        self.assertEqual(counts, {"CoinPoker": 320, "iPoker": 320})
        self.assertEqual(
            profile(self.app.db), {"CoinPoker": ["HeroDemo"], "iPoker": ["HeroDemo"]}
        )
        self.app.room.set("iPoker")
        self.app.refresh()
        self.wait()
        tv = self.app.tables["Exploratory"]
        tv.selection_set("THREEBET")
        view = self.app.open_selected(tv)
        self.addCleanup(view.close)
        self.root.update()
        self.assertIn("HeroDemo", view.raw.get("1.0", "end"))
        self.assertIn("Action ", view.anno.get())
        self.assertTrue(view.raw.tag_ranges("focus"))
        self.assertIn("no=", view.raw.get(*view.raw.tag_ranges("focus")))

    def test_home_has_twelve_cards_and_filtering_reuses_the_index(self):
        self.assertEqual(len(self.app.queue.items), 12)
        home = self.app.home
        labels = [
            (w.cget("text"), w.cget("foreground"))
            for w in child_widgets(home.rows)
            if isinstance(w, tk.Label)
        ]
        self.assertTrue(any(color == PLAYED_RED for _, color in labels))
        self.assertTrue(any(color == SUGGESTED_GREEN for _, color in labels))
        self.assertTrue(any("Why review this first?" in text for text, _ in labels))
        original_view = home.current
        guides = {item.hand: item for item in self.app.queue.guide_items}
        examples = tuple(
            replace(item, pattern="test cluster", repeat_count=2)
            for item in list(guides.values())[:2]
        )
        home.paint(
            replace(self.app.queue, items=examples, guide_items=examples), {}, ""
        )
        related = next(
            w
            for w in child_widgets(home.rows)
            if isinstance(w, ttk.Button) and w.cget("text").startswith("Related hands")
        )
        related.invoke()
        self.assertIn("Related hands", home.summary.get())
        self.assertGreater(len(home.rows.winfo_children()), 1)
        home.back.invoke()
        self.assertNotIn("Related hands", home.summary.get())
        home.paint(*original_view)
        home.mode.invoke()
        self.assertIn("Differences 1–12", home.summary.get())
        self.assertIn("Page 1 of", home.page_text.get())
        self.assertTrue(home.previous.instate(["disabled"]))
        home.following.invoke()
        self.assertIn("Differences 13–", home.summary.get())
        self.assertIn("Page 2 of", home.page_text.get())
        with patch(
            "leakfinder.ui.index_guides",
            side_effect=AssertionError("rebuilt the guide index"),
        ):
            for room, street in (
                ("Both", "Preflop"),
                ("iPoker", "Flop"),
                ("Both", "River"),
            ):
                self.app.room.set(room)
                self.app.street.set(street)
                self.app.refresh(filters_only=True)
                self.wait()
                self.assertTrue(self.app.queue.items)
                self.assertTrue(
                    all(item.street == street for item in self.app.queue.items)
                )

    def test_menu_import_switches_to_personal_then_preserves_profile(self):
        raw = Path(__file__).parent / "fixtures" / "coin-30.hh"
        with (
            patch(
                "leakfinder.ui.filedialog.askopenfilenames", return_value=(str(raw),)
            ),
            patch("leakfinder.ui.messagebox.showinfo"),
            patch.object(
                self.app,
                "choose_hero",
                side_effect=lambda: set_profile(self.app.db, {"CoinPoker": ["Hero"]}),
            ),
        ):
            self.app.import_menu.invoke("Import files…")
            self.wait()
        self.assertFalse(self.app.is_demo)
        self.assertEqual(self.app.path, self.personal)
        self.assertEqual(
            self.app.db.execute("SELECT count(*) FROM hands").fetchone()[0], 1
        )
        self.assertEqual(profile(self.app.db)["CoinPoker"], ["Hero"])
        self.app.switch_library(True)
        self.wait()
        self.assertEqual(
            self.app.db.execute("SELECT count(*) FROM hands").fetchone()[0], 640
        )
        self.app.switch_library(False)
        self.wait()
        self.assertEqual(profile(self.app.db)["CoinPoker"], ["Hero"])
        self.assertTrue(demo_path(self.personal).is_file())
        self.assertIn("My hands", self.app.library_text.get())

    def test_cancel_import_leaves_demo_active(self):
        with patch("leakfinder.ui.filedialog.askopenfilenames", return_value=()):
            self.app.import_menu.invoke("Import files…")
        self.assertTrue(self.app.is_demo)
        self.assertFalse(self.personal.exists())

    def test_folder_menu_imports_histories_from_nested_archive(self):
        archive = Path(self.tmp.name) / "archive"
        day = archive / "2025" / "03" / "22"
        day.mkdir(parents=True)
        (day / "session.XML").write_bytes(
            ET.tostring(repaired_session(original("ip-601.hh")))
        )
        (day / "ignored.jpg").write_text("not a hand history", encoding="ascii")
        with (
            patch("leakfinder.ui.filedialog.askdirectory", return_value=str(archive)),
            patch("leakfinder.ui.messagebox.showinfo") as no_files,
            patch("leakfinder.ui.messagebox.showwarning") as warning,
            patch.object(
                self.app,
                "choose_hero",
                side_effect=lambda: set_profile(
                    self.app.db, {"iPoker": ["BigNeonMax"]}
                ),
            ),
        ):
            self.app.import_menu.invoke("Import folder…")
            self.wait()
        no_files.assert_not_called()
        warning.assert_not_called()
        self.assertFalse(self.app.is_demo)
        self.assertEqual(
            self.app.db.execute(
                "SELECT room,count(*) FROM hands GROUP BY room"
            ).fetchall()[0][:],
            ("iPoker", 1),
        )
        self.assertIn("1 new hands", self.app.notice.get())
        self.assertEqual(
            Path(self.app.db.execute("SELECT path FROM imports").fetchone()[0]),
            day / "session.XML",
        )

    def test_reports_open_local_pilot_without_switching_libraries(self):
        report = (
            Path(__file__).resolve().parents[1] / "pilots" / "btn_co" / "BTN-vs-CO.html"
        )
        menu = self.root.nametowidget(self.app.menu.entrycget("Reports", "menu"))
        with patch("leakfinder.ui.webbrowser.open", return_value=True) as opened:
            menu.invoke("BTN vs CO · public CPU pilot")
        opened.assert_called_once_with(report.as_uri())
        self.assertTrue(self.app.is_demo)

    def test_import_from_report_lands_on_latest_hero_hands_and_opens_exact_table(self):
        self.app.switch_library(False)
        self.wait()
        set_profile(self.app.db, {"CoinPoker": ["Hero"], "iPoker": ["BigNeonMax"]})
        self.app.show_view("population")
        xml = Path(self.tmp.name) / "session.xml"
        xml.write_bytes(ET.tostring(repaired_session(original("ip-601.hh"))))
        coin = Path(__file__).parent / "fixtures" / "coin-30.hh"
        self.app.import_paths([coin, xml])
        self.wait()
        self.assertEqual(self.app.view, "home")
        self.assertEqual(self.app.scope.get(), "Latest import")
        self.assertEqual(self.app.queue.hand_count, 2)
        self.assertEqual(
            {item.room for item in self.app.queue.items}, {"CoinPoker", "iPoker"}
        )
        self.assertIn("2 new hands", self.app.notice.get())
        self.app.street.set("Flop")
        self.app.refresh(filters_only=True)
        self.wait()
        self.assertTrue(self.app.queue.items)
        self.assertTrue(all(item.street == "Flop" for item in self.app.queue.items))
        for widget, item in zip(
            self.app.home.rows.winfo_children(), self.app.queue.items, strict=True
        ):
            button = next(
                w
                for w in child_widgets(widget)
                if isinstance(w, ttk.Button) and "Open table" in w.cget("text")
            )
            from leakfinder.table_ui import open_table

            dialogs = []

            def capture(*args, opened=dialogs, **kwargs):
                opened.append(open_table(*args, **kwargs))

            with patch("leakfinder.table_ui.open_table", side_effect=capture):
                button.invoke()
            dialog = dialogs[0]
            self.assertEqual(
                (dialog.snapshot.room, dialog.snapshot.ref, dialog.snapshot.hero),
                (item.room, item.ref, item.hero),
            )
            self.assertEqual(len(dialog.snapshot.board), 3)
            self.assertEqual(len(dialog.snapshot.hero_cards), 4)
            self.assertTrue(all(a.ref != item.ref for a in dialog.snapshot.history))
            dialog.win.destroy()

        self.app.import_paths([coin])
        self.wait()
        self.assertEqual(self.app.queue.hand_count, 1)
        self.assertEqual({item.room for item in self.app.queue.items}, {"CoinPoker"})
        self.assertEqual(self.app.queue.library_hands, 2)
        self.app.scope.set("All hands")
        self.app.refresh(filters_only=True)
        self.wait()
        self.assertEqual(self.app.queue.hand_count, 2)

    def test_first_import_confirms_detected_alias_then_shows_review_cards(self):
        coin = Path(__file__).parent / "fixtures" / "coin-30.hh"
        self.app.import_paths([coin])
        self.wait()
        dialog = next(
            w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)
        )
        entry = next(w for w in child_widgets(dialog) if isinstance(w, tk.Text))
        self.assertEqual(entry.get("1.0", "end-1c"), "Hero")
        self.assertFalse(profile(self.app.db)["CoinPoker"])
        save = next(w for w in child_widgets(dialog) if isinstance(w, ttk.Button))
        save.invoke()
        self.wait()
        self.assertEqual(profile(self.app.db)["CoinPoker"], ["Hero"])
        self.assertEqual(self.app.view, "home")
        self.assertEqual(self.app.queue.items[0].number, "9000000030")

    def test_position_report_receives_active_demo_room(self):
        self.app.room.set("CoinPoker")
        self.app.refresh()
        self.wait()
        report = self.app.explore("Position")
        try:
            deadline = time.monotonic() + 15
            while not report.tree.get_children() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.02)
            self.assertEqual(report.room, "CoinPoker")
            self.assertEqual(report.dimension.get(), "Position")
            self.assertTrue(report.tree.get_children(), report.message.get())
            self.assertIn("sampling uncertainty", report.column_help("ci", "0"))
            report.tree.update_idletasks()
            heading = next(
                x
                for x in range(4, report.tree.winfo_width())
                if report.tree.identify_region(x, 5) == "heading"
                and report.tree.column(report.tree.identify_column(x), "id") == "ci"
            )
            report.table_help.delay = 0
            report.table_help.motion(
                SimpleNamespace(x=heading, y=5, x_root=heading, y_root=5)
            )
            self.root.update()
            self.assertIsNotNone(report.table_help.popup)
            report.table_help.hide()
            self.assertEqual(self.errors, [])
        finally:
            report.win.destroy()
            report.worker.join(timeout=5)

    def test_import_shows_guide_difference_and_opens_exact_hero_action(self):
        self.app.switch_library(False)
        self.wait()
        path = Path(__file__).resolve().parents[1] / "pilots/btn_co/hands.json"
        hands = json.loads(path.read_text(encoding="utf-8"))["hands"]
        coin, ip = (
            next(h for h in hands if h["id"] == n) for n in ("9000006096", "9000006911")
        )
        coin_file = Path(self.tmp.name) / "guide-coin.txt"
        ip_file = Path(self.tmp.name) / "guide-ip.xml"
        coin_file.write_text(coin["source"], encoding="utf-8")
        ip_file.write_bytes(ET.tostring(repaired_session(ip["source"])))
        set_profile(
            self.app.db,
            {
                h["room"]: [
                    decode(
                        Record(h["room"], h["source"], h["source_locator"], h["id"])
                    ).hint
                ]
                for h in (coin, ip)
            },
        )
        self.app.import_paths([coin_file, ip_file])
        self.wait()
        self.assertEqual(self.app.queue.scope, "Latest import")
        self.assertEqual(self.app.queue.hand_count, 2)
        self.app.street.set("Preflop")
        self.app.refresh(filters_only=True)
        self.wait()
        item = self.app.queue.items[0]
        self.assertEqual(
            (item.room, item.number, item.status), ("iPoker", ip["id"], "Guide")
        )
        self.assertIn("Suggested fold", item.reason)
        self.app.home.mode.invoke()
        self.assertIn("Differences 1–1 of 1", self.app.home.summary.get())
        dlg = self.app.open_item(item)
        self.addCleanup(dlg.close)
        self.assertEqual(
            (dlg.snapshot.ref, dlg.snapshot.number), (ip["action_ref"], ip["id"])
        )
        self.assertIn("Fold", dlg.timeline.get("1.0", "end"))
        self.app.room.set("CoinPoker")
        self.app.refresh()
        self.wait()
        self.assertEqual(self.app.queue.guide_items, ())
