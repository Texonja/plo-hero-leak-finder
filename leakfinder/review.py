"""Evidence window backed by a read snapshot of the selected cohort."""

import json
import re
import tkinter as tk
import zlib
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .analysis import describe, evidence, wilson
from .engine import METRICS, replay
from .importers import SCALE, Record, decode
from .storage import cohort, connect
from .vendor.primitives import best_plo_hand, classify_known_hand


class Review:
    def __init__(self, parent, path, room, f):
        from .ui import interval, pct, table

        self.pct = pct
        self.f = f
        self.path = path
        self.db = connect(path)
        self.db.execute("BEGIN")
        cohort(self.db, room)
        win = tk.Toplevel(parent)
        self.win = win
        win.title(METRICS[f.node].label + " · Source hands")
        win.geometry("1160x780")
        win.minsize(800, 480)
        win.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text=METRICS[f.node].label, font=("Segoe UI", 16, "bold")
        ).pack(anchor="w")
        x, n, px, p = f.counts
        hrate = f"{x}/{n} ({pct(x / n)})" if n else "—"
        ttk.Label(
            outer,
            text=f"{f.status} · {room} · Matched Hero {hrate} · Standardized pool {pct(f.expected)} · 95% gap {interval(f.interval)}",
        ).pack(anchor="w", pady=(3, 0))
        ax = sum(c.hx for c in f.cells.values())
        raw = wilson(ax / f.hero, f.hero) if f.hero else None
        rawtext = " to ".join(pct(v) for v in raw) if raw else "—"
        adjusted = (
            f" · Family-adjusted gap {interval(f.adjusted)}"
            if METRICS[f.node].confirmatory and f.adjusted
            else ""
        )
        ttk.Label(
            outer,
            text=f"All Hero actions {ax}/{f.hero} · Wilson 95% {rawtext} · Matched pool counts {px}/{p}{adjusted} · {f.note}",
            wraplength=1050,
        ).pack(anchor="w", pady=(1, 6))
        self.side = tk.StringVar(value="Hero")
        self.scope = tk.StringVar(
            value="Matched" if f.selected else "All opportunities"
        )
        self.page = 0
        self.cell = ""
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        for v, opts in (
            (self.side, ["Hero", "Pool"]),
            (self.scope, ["Matched", "All opportunities"]),
        ):
            combo = ttk.Combobox(
                bar, textvariable=v, values=opts, state="readonly", width=17
            )
            combo.pack(side="left", padx=(0, 6))
            combo.bind("<<ComboboxSelected>>", self.reset)
        ttk.Button(bar, text="Contexts…", command=self.contexts).pack(side="left")
        ttk.Button(bar, text="Clear context", command=self.clear_cell).pack(
            side="left", padx=6
        )
        self.pageinfo = tk.StringVar()
        self.forward = ttk.Button(bar, text="Next", command=lambda: self.move(1))
        self.forward.pack(side="right")
        self.back = ttk.Button(bar, text="Previous", command=lambda: self.move(-1))
        self.back.pack(side="right", padx=6)
        ttk.Label(bar, textvariable=self.pageinfo).pack(side="right", padx=8)
        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True, pady=7)
        top = ttk.Frame(panes)
        lower = ttk.Frame(panes)
        panes.add(top, weight=2)
        panes.add(lower, weight=3)
        self.tv = table(
            top,
            [
                ("room", "Room", 94),
                ("number", "Hand ID", 115),
                ("date", "Date", 90),
                ("actor", "Actor", 105),
                ("action", "Action", 70),
                ("ref", "Source", 80),
                ("context", "Matched context", 430),
            ],
            6,
        )
        self.tv.bind("<<TreeviewSelect>>", self.show_hand)
        self.tv.bind("<Double-1>", lambda _: self.open_table())
        self.tv.bind("<Return>", lambda _: self.open_table())
        self.anno = tk.StringVar()
        sourcebar = ttk.Frame(lower)
        sourcebar.pack(fill="x")
        self.table_button = ttk.Button(
            sourcebar, text="Open Hero table", command=self.open_table
        )
        self.table_button.pack(side="right", padx=(5, 0))
        ttk.Label(sourcebar, text="Recorded source").pack(side="left", padx=(0, 5))
        self.source = ttk.Combobox(sourcebar, state="readonly", width=96)
        self.source.pack(side="left", fill="x", expand=True)
        self.source.bind("<<ComboboxSelected>>", self.show_origin)
        ttk.Label(lower, textvariable=self.anno, wraplength=1060, justify="left").pack(
            anchor="w", pady=(5, 3)
        )
        self.raw = ScrolledText(
            lower,
            wrap="none",
            font=("Consolas", 10),
            background="#fcfdff",
            foreground="#233b50",
        )
        xs = ttk.Scrollbar(lower, orient="horizontal", command=self.raw.xview)
        xs.pack(side="bottom", fill="x")
        self.raw.configure(xscrollcommand=xs.set)
        self.raw.pack(fill="both", expand=True)
        self.raw.tag_configure("focus", background="#ffe598", foreground="#1b332d")
        self.load()

    def close(self):
        self.db.close()
        self.win.destroy()

    def open_table(self):
        if self.side.get() == "Hero" and self.tv.selection():
            from .table_ui import open_table

            row = self.rows[int(self.tv.selection()[0])]
            return open_table(
                self.win, self.path, row["hand"], row["ref"], hero=row["hero"]
            )

    def move(self, delta):
        self.page += delta
        self.load()

    def reset(self, _=None):
        self.page = 0
        self.load()

    def clear_cell(self):
        self.cell = ""
        self.reset()

    def contexts(self):
        from .ui import table

        win = tk.Toplevel(self.win)
        win.title("Decision contexts")
        win.geometry("950x450")
        ttk.Label(
            win, text="Double-click a context to see its opportunities.", padding=8
        ).pack(anchor="w")
        tv = table(
            win,
            [
                ("context", "Context", 540),
                ("h", "Hero x/n", 95),
                ("p", "Pool x/n", 95),
                ("match", "In comparison", 105),
            ],
        )
        keys = sorted(
            self.f.cells, key=lambda k: (-self.f.cells[k].h, -self.f.cells[k].p, k)
        )
        for i, k in enumerate(keys):
            c = self.f.cells[k]
            tv.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    describe(k),
                    f"{c.hx}/{c.h}",
                    f"{c.px}/{c.p}",
                    "Yes" if k in self.f.selected else "No",
                ),
            )

        def pick(_):
            if not tv.selection():
                return
            self.cell = keys[int(tv.selection()[0])]
            self.scope.set("All opportunities")
            win.destroy()
            self.reset()

        tv.bind("<Double-1>", pick)
        tv.bind("<Return>", pick)

    def load(self):
        self.rows, (total, hands) = evidence(
            self.db, self.f, self.side.get(), self.scope.get(), self.page, self.cell
        )
        self.tv.delete(*self.tv.get_children())
        for i, r in enumerate(self.rows):
            self.tv.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    r["room"],
                    r["number"],
                    r["day"],
                    r["actor"],
                    r["action"],
                    r["ref"],
                    describe(r["cell"]),
                ),
            )
        start = self.page * 100 + 1 if total else 0
        self.pageinfo.set(
            f"{start}–{self.page * 100 + len(self.rows)} / {total:,} choices · {hands:,} hands"
        )
        self.back.state(["!disabled" if self.page else "disabled"])
        self.forward.state(
            ["!disabled" if self.page * 100 + len(self.rows) < total else "disabled"]
        )
        self.table_button.state(
            ["!disabled" if self.rows and self.side.get() == "Hero" else "disabled"]
        )
        if self.rows:
            self.tv.selection_set("0")
        else:
            self.anno.set("No contributing decisions in this selection.")
            self.source.configure(values=())
            self.source.set("")
            self.raw.configure(state="normal")
            self.raw.delete("1.0", "end")
            self.raw.configure(state="disabled")

    def show_hand(self, _=None):
        if not self.tv.selection():
            return
        r = self.rows[int(self.tv.selection()[0])]
        self.origins = self.db.execute(
            """SELECT i.path,o.locator,o.raw FROM origins o JOIN imports i ON i.id=o.source
            WHERE hand=? ORDER BY o.rowid""",
            (r["hand"],),
        ).fetchall()
        self.source.configure(
            values=[f"{row['path']} · {row['locator']}" for row in self.origins]
        )
        self.source.current(0)
        self.show_origin()

    def show_origin(self, _=None):
        if not self.tv.selection() or self.source.current() < 0:
            return
        r = self.rows[int(self.tv.selection()[0])]
        origin = self.origins[self.source.current()]
        text = zlib.decompress(origin["raw"]).decode()
        h = decode(Record(r["room"], text, origin["locator"]))
        _, events = replay(h)
        st = METRICS[r["node"]].street
        if self.source.current() == 0:
            e = next(e for e in events[st] if e.ref == r["ref"])
            self.street_index = e.idx
        else:
            e = next(e for e in events[st] if e.idx == self.street_index)
        pocket = json.loads(
            self.db.execute(
                "SELECT cards FROM hands WHERE id=?", (r["hand"],)
            ).fetchone()[0]
        ).get(e.player, [])
        board = h.boards.get(st, [])
        tag = "Cards unknown"
        if pocket:
            cl = (
                best_plo_hand(pocket, board)[2]
                if board
                else classify_known_hand(pocket)["hand_class"]
            ).replace("_", " ")
            source = (
                "recorded Hero" if e.player == h.hint else "retrospective known cards"
            )
            if e.player not in h.cards:
                source += " in another source copy"
            tag = f"{source}  {' '.join(pocket)} · {cl}"
        self.anno.set(
            f"{e.ref} · {e.player} {e.action} · Pot {e.pot / SCALE:g} {h.currency} · To call {e.owed / SCALE:g} · Board {' '.join(board) or 'preflop'} · {tag}"
        )
        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", text)
        if e.ref.startswith("Line "):
            anchor = e.ref.split()[1] + ".0"
            end = anchor + " lineend"
        else:
            num = e.ref.split()[1]
            match = re.search(
                r"""<action\b[^>]*\bno\s*=\s*["']"""
                + re.escape(num)
                + r"""["'][^>]*>""",
                text,
            )
            anchor = f"1.0+{match.start()}c" if match else "1.0"
            end = f"1.0+{match.end()}c" if match else anchor
        self.raw.tag_add("focus", anchor, end)
        self.raw.see(anchor)
        self.raw.configure(state="disabled")
