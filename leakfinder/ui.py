"""Offline desktop review workspace."""

import argparse
import json
import os
import stat
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .analysis import (
    cached_comparison,
    comparison_revision,
    save_comparison,
    update_comparison,
)
from .demo import demo_path, load_demo
from .engine import METRICS
from .home import BG, INK, MUTED, NAV, NAV_ACTIVE, ORANGE, HandReview
from .hover_help import CONFIDENCE_HELP, tree_help
from .replay_view import load_snapshot
from .review import Review
from .review_queue import (
    build_queue,
    cached_guides,
    guide_revision,
    has_saved_batch,
    latest_batch_rooms,
    preview_decisions,
    remember_batch,
    review_selection,
    save_guides,
    update_guides,
)
from .storage import (
    cohort,
    connect,
    data_revision,
    import_file_with_source,
    initialize,
    profile,
    set_profile,
)

REPORTS = [
    ("By position", "Position"),
    ("By street", "Street"),
    ("By sizing", "Faced sizing"),
    ("By hand class", "Hand class"),
    ("By stack depth", "Stack depth"),
    ("By stake", "Stake"),
    ("By poker room", "Room"),
    ("Sessions", "Session"),
    ("Leak trends", "Trends"),
    ("Vs opponent", "Opponent"),
    ("By time period", "Month"),
]
HISTORY_EXTENSIONS = frozenset({".txt", ".xml", ".sql", ".pghembk", ".zip"})


def hand_history_files(folder):
    """Find supported files in a folder tree without following directory links."""

    def walk_error(error):
        raise error

    for parent, directories, filenames in os.walk(folder, onerror=walk_error):
        root = Path(parent)
        directories[:] = sorted(
            (
                name
                for name in directories
                if not getattr((root / name).lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ),
            key=str.casefold,
        )
        for name in sorted(filenames, key=str.casefold):
            path = root / name
            if path.suffix.lower() in HISTORY_EXTENSIONS and not path.is_symlink():
                yield path


def pct(x):
    return "—" if x is None else f"{100 * x:.1f}%"


def interval(bounds):
    return (
        "—"
        if bounds is None
        else " to ".join(f"{x * 100:+.1f}" for x in bounds) + " pp"
    )


def readonly(widget, content):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")


def table(parent, cols, heights=10):
    outer = ttk.Frame(parent)
    outer.pack(fill="both", expand=True)
    tv = ttk.Treeview(
        outer,
        show="headings",
        columns=[k for k, _, _ in cols],
        height=heights,
        selectmode="browse",
    )
    scroll = ttk.Scrollbar(outer, orient="vertical", command=tv.yview)
    horiz = ttk.Scrollbar(outer, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=scroll.set, xscrollcommand=horiz.set)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)
    tv.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    horiz.grid(row=1, column=0, sticky="ew")
    for k, title, w in cols:
        tv.heading(k, text=title, anchor="w")
        tv.column(
            k,
            width=w,
            minwidth=min(65, w),
            stretch=k in {"name", "context", "source"},
            anchor="w",
        )
    return tv


def open_library(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        initialize(db)
    except Exception:
        db.close()
        raise
    return db


class Workspace:
    def __init__(self, root, path, start_demo=False):
        self.root, self.personal_path = root, Path(path).resolve()
        self.is_demo = start_demo
        self.path = (
            demo_path(self.personal_path) if self.is_demo else self.personal_path
        )
        self.db = open_library(self.path)
        self.busy = False
        self.results = {}
        self.queue = None
        self.library_counts = (0, 0)
        self.room_results = {}
        self.review_selections = {}
        self.preview_ready = False
        self.guide_index = None
        self.view = "home"
        self.jobs = Queue()
        self.room = tk.StringVar(value="Both")
        self.street = tk.StringVar(value="All streets")
        self.scope = tk.StringVar(
            value="Latest import" if has_saved_batch(self.db) else "All hands"
        )
        self.status = tk.StringVar(value="Ready")
        self.hero_text = tk.StringVar()
        self.detail = tk.StringVar()
        root.title(f"PLO Hero Leak Finder · {__version__}")
        width = max(860, min(1220, root.winfo_screenwidth() - 40))
        height = max(535, min(770, root.winfo_screenheight() - 95))
        root.geometry(f"{width}x{height}")
        root.minsize(860, 535)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=INK)
        style.configure(
            "Treeview", background="white", fieldbackground="white", rowheight=27
        )
        style.configure(
            "Treeview.Heading",
            background="#dce5ed",
            foreground=INK,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#d2e6f7")],
            foreground=[("selected", INK)],
        )
        style.configure("TButton", padding=(11, 6))
        style.configure(
            "Primary.TButton",
            background=ORANGE,
            foreground="#2e271f",
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
            padding=(16, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#dededa"), ("active", "#ffbc82")],
        )
        style.configure(
            "Review.TButton",
            background="#ffdfc0",
            foreground=INK,
            padding=(12, 5),
            borderwidth=0,
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Review.TButton", background=[("active", "#ffd4ac")])
        self.make_menus()
        title = tk.Frame(root, bg="#213a50")
        title.pack(fill="x")
        brand = tk.Frame(title, bg=ORANGE, padx=17, pady=9)
        brand.pack(side="left")
        logo = tk.Canvas(brand, bg=ORANGE, width=34, height=32, highlightthickness=0)
        logo.pack(side="left", padx=(0, 9))
        logo.create_polygon(2, 7, 17, 2, 25, 25, 10, 30, fill="#385065")
        logo.create_rectangle(14, 4, 31, 28, fill="#ffead2", outline="#15293a", width=2)
        logo.create_polygon(22, 10, 26, 16, 22, 22, 18, 16, fill="#15293a")
        tk.Label(
            brand,
            text="PLO Hero Leak Finder",
            font=("Segoe UI", 16, "bold"),
            bg=ORANGE,
            fg=INK,
        ).pack(side="left")
        self.library_text = tk.StringVar()
        tk.Label(
            title,
            textvariable=self.library_text,
            font=("Segoe UI", 10),
            bg="#dbe5ed",
            fg=INK,
            padx=13,
            pady=4,
        ).pack(side="right", padx=16)
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        nav = tk.Frame(shell, bg=NAV, width=166)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self.nav_items = {}

        def nav_section(label):
            tk.Label(
                nav,
                text=label.upper(),
                bg=NAV,
                fg=MUTED,
                font=("Segoe UI", 8, "bold"),
                anchor="w",
                padx=16,
            ).pack(fill="x", pady=(12, 4))

        def navitem(label, cmd):
            band = tk.Frame(nav, bg=NAV)
            band.pack(fill="x")
            marker = tk.Frame(band, bg=NAV, width=3)
            marker.pack(side="left", fill="y")
            b = tk.Button(
                band,
                text=label,
                command=cmd,
                bg=NAV,
                fg=INK,
                activebackground=NAV_ACTIVE,
                activeforeground=INK,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10),
                anchor="w",
                padx=13,
                pady=3,
                highlightthickness=0,
            )
            b.pack(fill="x")
            self.nav_items[label] = b, marker

        nav_section("Workspace")
        navitem("Hand review", lambda: self.show_view("home"))
        navitem("Population review", lambda: self.show_view("population"))
        nav_section("Cash reports")
        for title, dimension in REPORTS:
            navitem(title, lambda d=dimension: self.explore(d))
        self.container = ttk.Frame(shell, padding=(18, 12))
        self.container.pack(fill="both", expand=True)
        bar = ttk.Frame(self.container)
        bar.pack(fill="x")
        self.heading = tk.StringVar(value="Hands to review")
        ttk.Label(bar, textvariable=self.heading, font=("Segoe UI", 20, "bold")).pack(
            side="left"
        )
        self.import_btn = ttk.Button(
            bar,
            text="Import hands…",
            style="Primary.TButton",
            command=self.choose_files,
        )
        self.import_btn.pack(side="right")
        self.hero_btn = ttk.Button(
            bar, text="Hero aliases…", command=self.personal_hero
        )
        self.hero_btn.pack(side="right", padx=7)
        ttk.Label(self.container, textvariable=self.hero_text, foreground=MUTED).pack(
            anchor="w", pady=(3, 12)
        )
        controls = ttk.Frame(self.container)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="Room").pack(side="left")
        self.room_box = ttk.Combobox(
            controls,
            textvariable=self.room,
            values=("Both", "CoinPoker", "iPoker"),
            state="readonly",
            width=13,
        )
        self.room_box.pack(side="left", padx=(6, 16))
        self.room_box.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        ttk.Label(controls, text="Street").pack(side="left")
        self.street_box = ttk.Combobox(
            controls,
            textvariable=self.street,
            values=("All streets", "Preflop", "Flop", "Turn", "River"),
            state="readonly",
            width=12,
        )
        self.street_box.pack(side="left", padx=6)
        self.street_box.bind(
            "<<ComboboxSelected>>", lambda _: self.refresh(filters_only=True)
        )
        ttk.Button(controls, text="Import history", command=self.diagnostics).pack(
            side="right"
        )
        self.scope_controls = ttk.Frame(controls)
        ttk.Label(self.scope_controls, text="Hands").pack(side="left")
        self.scope_box = ttk.Combobox(
            self.scope_controls,
            textvariable=self.scope,
            values=("Latest import", "Last 5 sessions", "All hands"),
            state="readonly",
            width=13,
        )
        self.scope_box.pack(side="left", padx=6)
        self.scope_box.bind(
            "<<ComboboxSelected>>", lambda _: self.refresh(filters_only=True)
        )
        self.notice = tk.StringVar()
        self.notice_label = ttk.Label(
            self.container, textvariable=self.notice, foreground=INK, wraplength=650
        )
        self.footer = ttk.Frame(self.container)
        self.footer.pack(side="bottom", fill="x", pady=(7, 0))
        ttk.Separator(self.footer).pack(fill="x", pady=(0, 5))
        ttk.Label(
            self.footer, textvariable=self.status, foreground=MUTED, wraplength=680
        ).pack(anchor="w")
        self.body = ttk.Frame(self.container)
        self.body.pack(fill="both", expand=True)
        self.home = HandReview(self.body, self.open_item)
        self.population = ttk.Frame(self.body)
        book = ttk.Notebook(self.population)
        book.pack(fill="both", expand=True)
        self.book = book
        self.tables = {}
        for label in ("Exploratory", "Supported"):
            pane = ttk.Frame(book, padding=9)
            book.add(pane, text=label)
            text = (
                "Observed decisions, including sparse or unmatched situations."
                if label == "Exploratory"
                else "Audited decisions with a supported difference from the matched pool."
            )
            ttk.Label(pane, text=text).pack(anchor="w", pady=(0, 8))
            tv = table(
                pane,
                [
                    ("name", "Decision", 235),
                    ("h", "Hero x/n¹", 87),
                    ("p", "Pool x/n¹", 93),
                    ("hero_rate", "Matched Hero", 94),
                    ("ref", "Matched pool", 93),
                    ("gap", "Gap", 77),
                    ("int", "95% gap interval", 142),
                    ("cov", "Coverage", 75),
                ],
            )
            tv.bind("<<TreeviewSelect>>", lambda _, t=tv: self.select(t))
            tv.bind("<Double-1>", lambda _, t=tv: self.open_selected(t))
            tv.bind("<Return>", lambda _, t=tv: self.open_selected(t))
            tree_help(tv, self.population_help)
            self.tables[label] = tv
        book.bind("<<NotebookTabChanged>>", lambda _: self.select(self.current_table()))
        ttk.Label(
            self.population,
            text="¹ Counts include all eligible choices. Matched rates use Hero’s context mix. A frequency difference calls for hand review.",
            foreground=MUTED,
        ).pack(anchor="w", pady=(8, 2))
        bottom = ttk.Frame(self.population)
        bottom.pack(fill="x")
        ttk.Label(
            bottom, textvariable=self.detail, wraplength=815, justify="left"
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            bottom,
            text="Review hands →",
            command=lambda: self.open_selected(self.current_table()),
        ).pack(side="right", padx=(8, 0))
        self.show_view("home")
        self.update_profile()
        self.refresh()
        self.poll_timer = self.root.after(100, self.poll)

    def population_help(self, column, _row):
        return {
            "name": "The decision and action measured by this statistic.",
            "h": "Hero actions divided by all eligible Hero opportunities.",
            "p": "Opponent actions divided by all eligible opponent opportunities.",
            "hero_rate": "Hero's action frequency in matched contexts.",
            "ref": "The matched opponent frequency, weighted to Hero's mix of contexts.",
            "gap": "Matched Hero frequency minus matched opponent frequency, in percentage points.",
            "int": CONFIDENCE_HELP
            + " Here the estimated value is the matched frequency gap.",
            "cov": "Share of Hero's eligible opportunities covered by the matched comparison.",
        }.get(column)

    def make_menus(self):
        bar = tk.Menu(self.root)
        self.menu = bar

        def add_menu(title, entries):
            menu = tk.Menu(bar, tearoff=False)
            for label, callback in entries:
                if label is None:
                    menu.add_separator()
                else:
                    menu.add_command(label=label, command=callback)
            bar.add_cascade(label=title, menu=menu)
            return menu

        add_menu(
            "Library",
            [
                ("My hands", lambda: self.switch_library(False)),
                ("HeroDemo", lambda: self.switch_library(True)),
            ],
        )
        self.import_menu = add_menu(
            "Import",
            [
                ("Import files…", self.choose_files),
                ("Import folder…", self.choose_folder),
                (None, None),
                ("Import history", self.diagnostics),
            ],
        )
        add_menu("Player", [("Personal Hero aliases…", self.personal_hero)])
        add_menu(
            "Reports",
            [
                ("Hand review", lambda: self.show_view("home")),
                ("Population review", lambda: self.show_view("population")),
            ]
            + [(n, lambda d=d: self.explore(d)) for n, d in REPORTS]
            + [
                (None, None),
                ("BTN vs CO · public CPU pilot", self.open_btn_co_pilot),
                (
                    "BTN vs CO · hand guide",
                    lambda: self.open_btn_co_pilot("BTN-CO-guide.html"),
                ),
                (
                    "PLO4 preflop and postflop guide",
                    lambda: self.open_btn_co_pilot("PLO4-guide.html"),
                ),
                (
                    "Opponent range audit",
                    lambda: self.open_btn_co_pilot("PLO4-opponent-range-audit.html"),
                ),
                (
                    "Flop and turn range audit",
                    lambda: self.open_btn_co_pilot("PLO4-flop-turn-range.html"),
                ),
                (
                    "Flop and turn opponent history audit",
                    lambda: self.open_btn_co_pilot("PLO4-flop-turn-history.html"),
                ),
            ],
        )
        self.root.configure(menu=bar)
        self.root.bind("<Control-o>", lambda _: self.choose_files())

    def open_btn_co_pilot(self, filename="BTN-vs-CO.html"):
        report = Path(__file__).resolve().parents[1] / "pilots" / "btn_co" / filename
        if not report.is_file() or not webbrowser.open(report.as_uri()):
            messagebox.showinfo(
                "BTN vs CO", f"Open the local report:\n{report}", parent=self.root
            )

    def show_view(self, view):
        self.view = view
        self.home.pack_forget()
        self.population.pack_forget()
        self.scope_controls.pack_forget()
        if view == "home":
            self.heading.set("Hands to review")
            if not self.is_demo:
                self.scope_controls.pack(side="left", padx=(12, 0))
            self.home.pack(fill="both", expand=True)
        else:
            self.heading.set("Population review")
            self.population.pack(fill="both", expand=True)
        active = "Hand review" if view == "home" else "Population review"
        for label, (button, marker) in self.nav_items.items():
            button.configure(bg="#ffd7ae" if label == active else NAV)
            marker.configure(bg=INK if label == active else NAV)

    def import_notice(self, text):
        self.notice.set(text)
        self.notice_label.pack_forget()
        if text:
            self.notice_label.pack(before=self.body, fill="x", pady=(0, 8))

    def open_item(self, item):
        if self.busy and not (self.preview_ready and item.status == "Recorded"):
            return
        from .table_ui import open_table

        return open_table(self.root, self.path, item.hand, item.ref, hero=item.hero)

    def explore(self, dimension):
        if self.busy:
            return
        from .explorer import open_explorer

        return open_explorer(self.root, self.path, self.room.get(), dimension=dimension)

    def switch_library(self, demo, refresh=True):
        if self.busy:
            return False
        if self.is_demo != demo:
            path = demo_path(self.personal_path) if demo else self.personal_path
            db = open_library(path)
            self.db.close()
            self.db, self.path, self.is_demo = db, path, demo
            self.room.set("Both")
            self.street.set("All streets")
            self.results = {}
            self.scope.set("Latest import" if has_saved_batch(self.db) else "All hands")
            self.library_counts = (0, 0)
            self.room_results.clear()
            self.review_selections.clear()
            self.guide_index = None
            self.queue = None
            self.home.clear("Finding Hero decisions…")
            self.import_notice("")
            self.show_view("home")
            self.paint()
            self.update_profile()
        if refresh:
            self.refresh()
        return True

    def personal_hero(self):
        if self.switch_library(False, refresh=False):
            self.choose_hero()

    def current_table(self):
        return self.tables[self.book.tab(self.book.select(), "text")]

    def update_profile(self):
        aliases = profile(self.db)
        self.library_text.set(
            "Library   /   HeroDemo sample"
            if self.is_demo
            else "Library   /   My hands"
        )
        self.hero_text.set(
            "Active player    HeroDemo   ·   Sanitized sample hands"
            if self.is_demo
            else "Active player    "
            + "    |    ".join(
                f"{r}  ·  " + (", ".join(names) or "choose alias")
                for r, names in aliases.items()
            )
        )

    def work(self, fn, done):
        if self.busy:
            return
        self.busy = True
        self.import_btn.state(["disabled"])
        self.hero_btn.state(["disabled"])
        self.room_box.configure(state="disabled")
        self.street_box.configure(state="disabled")
        self.scope_box.configure(state="disabled")
        for label in ("Library", "Import", "Player", "Reports"):
            self.menu.entryconfigure(label, state="disabled")
        current_path = self.path

        def run():
            try:
                with_db = connect(current_path)
                try:
                    result = fn(with_db)
                finally:
                    with_db.close()
                self.jobs.put(("done", done, result))
            except Exception as exc:  # noqa: BLE001 - deliver worker failures to Tk
                self.jobs.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                msg = self.jobs.get_nowait()
                if msg[0] == "review-preview":
                    self.preview_ready = True
                    self.home.preview(msg[1])
                    continue
                if msg[0] == "progress":
                    self.status.set(msg[1])
                    if self.preview_ready:
                        self.home.summary.set("Recent decisions · " + msg[1])
                    continue
                self.preview_ready = False
                self.busy = False
                self.import_btn.state(["!disabled"])
                self.hero_btn.state(["!disabled"])
                self.room_box.configure(state="readonly")
                self.street_box.configure(state="readonly")
                self.scope_box.configure(
                    state="readonly" if has_saved_batch(self.db) else "disabled"
                )
                for label in ("Library", "Import", "Player", "Reports"):
                    self.menu.entryconfigure(label, state="normal")
                if msg[0] == "error":
                    self.status.set("Operation failed · " + msg[1])
                    if self.view == "home":
                        self.home.clear("The analysis could not finish. " + msg[1])
                else:
                    msg[1](msg[2])
        except Empty:
            pass
        self.poll_timer = self.root.after(100, self.poll)

    def close(self):
        self.root.after_cancel(self.poll_timer)
        self.db.close()

    def choose_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Import completed hand histories",
            filetypes=[
                ("Hand histories", "*.txt *.xml *.pghembk *.sql *.zip"),
                ("All files", "*.*"),
            ],
        )
        if paths:
            self.import_paths(paths)

    def choose_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(
            parent=self.root, title="Select a folder of completed hand histories"
        )
        if not folder:
            return
        self.import_paths(hand_history_files(folder))

    def import_paths(self, paths):
        if not self.switch_library(False, refresh=False):
            return
        self.show_view("home")
        if self.queue is None:
            self.home.clear("Reading the selected files…")
        self.import_notice("")
        previous_status = self.status.get()
        self.status.set("Checking the selected files…")

        def run(db):
            before = data_revision(db)
            sources, failures = [], []
            totals = {}
            files = list(paths)
            last_progress = 0.0
            for index, path in enumerate(files, 1):
                current = f"File {index:,} of {len(files):,} · {Path(path).name}"
                try:
                    now = time.monotonic()
                    if now - last_progress >= 0.1 or index == len(files):
                        self.jobs.put(("progress", current))
                        last_progress = now
                    counts, source_id = import_file_with_source(
                        db,
                        path,
                        lambda n, label=current: self.jobs.put(
                            ("progress", f"{label} · {n:,} source hands read")
                        ),
                    )
                    if counts.get("new hands"):
                        sources.append(source_id)
                    for key, value in counts.items():
                        totals[key] = totals.get(key, 0) + value
                except Exception as exc:  # noqa: BLE001 - isolate each file in a batch
                    failures.append(f"{Path(path).name} · {exc}")
            remember_batch(db, sources, after_revision=before)
            return bool(sources), totals, failures, len(files)

        def done(result):
            added, totals, failures, file_count = result
            if added:
                self.home.show_hands = None
                self.home.show_critiques = False
                self.home.differences.set(False)
                self.scope.set("Latest import")
                self.room.set("Both")
                self.street.set("All streets")
                msg = "Import complete    " + "   ·   ".join(
                    f"{n:,} {key}" for key, n in totals.items()
                )
            elif totals:
                msg = "No new hands. " + "   ·   ".join(
                    f"{n:,} {key}" for key, n in totals.items()
                )
            elif not file_count:
                msg = "No supported hand histories were found in this folder or its subfolders."
            else:
                msg = "Import failed. Showing the previous library selection."
            if failures:
                msg += f"   ·   {len(failures)} failed file(s)"
                self.show_import_errors(failures)
            self.import_notice(msg)
            cached = self.room_results.get(self.room.get())
            aliases = profile(self.db)
            names = tuple((r, n) for r in sorted(aliases) for n in aliases[r])
            if (
                not added
                and self.queue is not None
                and cached
                and cached[0] == (self.room.get(), data_revision(self.db), names)
            ):
                self.status.set(previous_status)
            elif added and any(not aliases[r] for r in latest_batch_rooms(self.db)):
                self.home.clear(
                    "Select your Hero screen name to see the imported decisions."
                )
                self.choose_hero()
            else:
                self.refresh()

        self.work(run, done)

    def choose_hero(self):
        if self.busy:
            return
        win = tk.Toplevel(self.root)
        win.title("Hero screen names")
        win.transient(self.root)
        win.grab_set()
        box = ttk.Frame(win, padding=18)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Hero screen names", font=("Georgia", 17)).pack(anchor="w")
        ttk.Label(
            box,
            text="Enter one exact alias per line for each room. Only hands with one matching Hero are analyzed.",
            wraplength=520,
        ).pack(anchor="w", pady=6)
        saved = profile(self.db)
        inputs = {}
        for room in saved:
            ttk.Label(box, text=room, font=("Segoe UI", 11, "bold")).pack(
                anchor="w", pady=(9, 3)
            )
            hints = [
                r[0]
                for r in self.db.execute(
                    "SELECT hint,count(*) n FROM hands WHERE room=? AND hint<>'' GROUP BY hint ORDER BY n DESC LIMIT 30",
                    (room,),
                )
            ]
            widget = tk.Text(box, height=3, width=60, font=("Segoe UI", 10))
            widget.pack(fill="x")
            initial = saved[room] or (hints if len(hints) == 1 else [])
            widget.insert("1.0", "\n".join(initial))
            inputs[room] = widget
            if hints:
                select = ttk.Combobox(box, values=hints, state="readonly")
                select.set("Choose a detected name")
                select.pack(fill="x")

                def add(_, combo=select, entry=widget):
                    vals = entry.get("1.0", "end-1c").splitlines()
                    if combo.get() not in vals:
                        entry.insert("end", ("\n" if vals else "") + combo.get())

                select.bind("<<ComboboxSelected>>", add)

        def save():
            set_profile(
                self.db,
                {
                    r: [
                        s.strip() for s in w.get("1.0", "end").splitlines() if s.strip()
                    ]
                    for r, w in inputs.items()
                },
            )
            win.destroy()
            self.update_profile()
            self.refresh()

        ttk.Button(
            box, text="Apply Hero selection", style="Primary.TButton", command=save
        ).pack(anchor="e", pady=(12, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy(), self.refresh()))

    def refresh(self, filters_only=False):
        if self.busy:
            return
        room = self.room.get()
        street, scope = self.street.get(), self.scope.get()
        self.status.set(
            "Finding hands to review…"
            if filters_only
            else "Matching decision contexts…"
        )
        if self.view == "home":
            self.home.clear("Finding Hero decisions…")

        def done(result):
            (
                findings,
                e,
                t,
                self.queue,
                pockets,
                key,
                revision,
                guide,
                selection_key,
                selection,
            ) = result
            self.review_selections[room, scope] = selection_key, selection
            self.room_results[room] = key, findings, e, t
            self.guide_index = revision, guide
            self.results = findings
            self.library_counts = e, t
            self.scope.set(self.queue.scope)
            action = None
            if not t:
                if self.queue.scope == "Latest import":
                    empty = "This import has no accepted PLO4 hands for the selected room. Import history lists the files and any excluded records."
                    action = ("Import history", self.diagnostics)
                else:
                    empty = "No imported hands for this room yet."
                    action = ("Import hands…", self.choose_files)
            elif not e:
                empty = "No hands match this Hero selection. Check the screen name for each room in the imported histories."
                action = ("Select Hero…", self.personal_hero)
            elif self.queue.scope == "Latest import" and not self.queue.hand_count:
                empty = "This import contains no eligible hands for the selected Hero and room. Import history lists excluded records."
                action = ("Import history", self.diagnostics)
            else:
                empty = "No reviewable Hero decision fits these filters. The reports also include hands with no decision on the selected street."
            self.home.paint(self.queue, pockets, empty, action)
            self.paint()
            coverage = f"{t:,} imported hands   ·   {e:,} eligible Hero hands"
            if t != e:
                coverage += f"   ·   {t - e:,} outside this profile or conflicting"
            self.status.set(coverage)

        demo = self.is_demo
        previous_comparison = self.room_results.get(room)
        cached_index = self.guide_index
        previous_selection = self.review_selections.get((room, scope))

        def read(db):
            if demo:
                load_demo(db)
            db.execute("BEGIN")
            # Imports commit atomically. Reuse a room comparison on a street/scope
            # change only while both its source data and Hero profile still match.
            key = (
                room,
                data_revision(db),
                tuple(
                    tuple(row)
                    for row in db.execute(
                        "SELECT room,name FROM aliases ORDER BY room,name"
                    )
                ),
            )
            revision = guide_revision(key[1:])
            batch = tuple(
                r[0]
                for r in db.execute("SELECT source FROM review_batch ORDER BY source")
            )
            selection_key = (key, batch)
            if previous_selection and previous_selection[0] == selection_key:
                selection = previous_selection[1]
            else:
                selection = review_selection(db, room, scope)
                if cached_index is None and selection[1] > 1000:
                    sample = preview_decisions(db, street)
                    if sample:
                        self.jobs.put(("review-preview", sample))
            high = db.execute("SELECT coalesce(max(id),0) FROM hands").fetchone()[0]
            population_key = comparison_revision(room, key[1], high, key[2])
            comparison = (
                previous_comparison[1:]
                if previous_comparison and previous_comparison[0] == key
                else cached_comparison(db, room, population_key)
            )
            if comparison is None:
                items, e, t = update_comparison(db, room, population_key)
                findings = {f.node: f for f in items}
            else:
                findings, e, t = comparison
            guide = (
                cached_index[1]
                if cached_index and cached_index[0] == revision
                else cached_guides(db, "Both", revision)
            )
            needs_index = guide is None
            if needs_index:
                self.jobs.put(("progress", "Updating the hand review index…"))
                cohort(db, "Both")
                guide = update_guides(
                    db,
                    "Both",
                    revision,
                    lambda n: self.jobs.put(
                        ("progress", f"Updating the review index · {n:,} hands checked")
                    ),
                )
            queue = build_queue(
                db,
                room,
                findings=findings,
                street=street,
                scope=scope,
                guide_index=guide,
                selection=selection,
            )
            pockets = {}
            for item in queue.items:
                if item.cards:
                    continue
                snapshot = load_snapshot(db, item.hand, item.hero, item.ref)
                hero = next(seat for seat in snapshot.seats if seat.is_hero)
                pockets[item.hand] = snapshot.hero_cards or (), hero.position
            db.commit()
            if comparison is None:
                save_comparison(db, room, population_key, findings, e, t)
            if needs_index:
                save_guides(db, "Both", revision, guide)
            return (
                findings,
                e,
                t,
                queue,
                pockets,
                key,
                revision,
                guide,
                selection_key,
                selection,
            )

        self.work(read, done)

    def paint(self):
        for tv in self.tables.values():
            tv.delete(*tv.get_children())
        for f in self.results.values():
            m = METRICS[f.node]
            if self.street.get() not in ("All streets", m.street):
                continue
            hx = sum(c.hx for c in f.cells.values())
            px = sum(c.px for c in f.cells.values())
            vals = [
                m.label,
                f"{hx:,}/{f.hero:,}",
                f"{px:,}/{f.pool:,}",
                pct(f.counts[0] / f.counts[1] if f.counts[1] else None),
                pct(f.expected),
                f"{100 * f.gap:+.1f} pp" if f.gap is not None else "—",
                interval(f.interval),
                pct(f.counts[1] / f.hero if f.hero else None),
            ]
            self.tables[f.status].insert("", "end", iid=f.node, values=vals)
        for tv in self.tables.values():
            if tv.get_children():
                tv.selection_set(tv.get_children()[0])
        self.select(self.current_table())

    def select(self, tv):
        selected = tv.selection()
        if not selected or selected[0] not in self.results:
            self.detail.set(
                "Import completed histories and select Hero."
                if not self.results
                else "No observations in this view."
            )
            return
        f = self.results[selected[0]]
        x, n, px, p = f.counts
        self.detail.set(
            f"Matched Hero {x:,}/{n:,} · matched pool {px:,}/{p:,} raw · {f.blocks[0]}/{f.blocks[1]} Hero/pool days. {f.note}"
        )

    def open_selected(self, tv):
        if self.busy or not tv.selection():
            return
        return Review(
            self.root, self.path, self.room.get(), self.results[tv.selection()[0]]
        )

    def show_import_errors(self, failures):
        win = tk.Toplevel(self.root)
        win.title(f"Import details · {len(failures):,} failed files")
        box = ScrolledText(
            win, width=74, height=min(15, len(failures) + 1), wrap="word"
        )
        box.pack(fill="both", expand=True, padx=8, pady=8)
        readonly(box, "\n".join(failures))

    def diagnostics(self):
        win = tk.Toplevel(self.root)
        win.title("Local import details")
        win.geometry("760x480")
        text = ScrolledText(win, wrap="word", font=("Segoe UI", 10))
        text.pack(fill="both", expand=True)
        lines = [f"Local library   {self.path}", ""]
        for r in self.db.execute("SELECT * FROM imports ORDER BY id DESC"):
            lines.extend(
                [
                    r["path"],
                    ", ".join(
                        f"{v:,} {k}" for k, v in json.loads(r["summary"]).items()
                    ),
                ]
            )
            for reason, n in self.db.execute(
                "SELECT reason,count(*) FROM rejected WHERE source=? GROUP BY reason ORDER BY count(*) DESC",
                (r["id"],),
            ):
                lines.append(f"    {n:,}   {reason}")
            lines.append("")
        readonly(text, "\n".join(lines))


def report_error(exc_type, error, tb, directory, root=None):
    heading = f"PLO Hero Leak Finder {__version__}"
    message = str(error) or exc_type.__name__
    log = directory / "errors.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size >= 1024 * 1024:
            log.replace(log.with_suffix(".previous.log"))
        with log.open("a", encoding="utf-8") as stream:
            print(datetime.now().isoformat(timespec="seconds"), heading, file=stream)
            traceback.print_exception(exc_type, error, tb, file=stream)
        message += f"\n\nLocal error log\n{log}"
    except OSError:
        pass
    try:
        messagebox.showerror(heading, message, parent=root)
    except tk.TclError:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, heading, 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Offline PLO4 Hero Leak Finder")
    default = (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
        / "PLO Hero Leak Finder"
        / "library.sqlite3"
    )
    parser.add_argument(
        "--library", type=Path, help="Open the supplied personal SQLite library"
    )
    args = parser.parse_args()
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda kind, err, tb: report_error(
            kind, err, tb, default.parent, root
        )
        app = Workspace(
            root,
            args.library or default,
            start_demo=args.library is None and not default.exists(),
        )
        try:
            root.deiconify()
            root.mainloop()
        finally:
            app.close()
    except Exception as exc:
        if root is not None:
            with suppress(tk.TclError):
                root.withdraw()
        try:
            report_error(type(exc), exc, exc.__traceback__, default.parent, root)
        finally:
            if root is not None:
                with suppress(tk.TclError):
                    root.destroy()
        raise SystemExit(1) from exc
