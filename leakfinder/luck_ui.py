"""Offline cash report for deals and single-board runouts."""

import threading
import tkinter as tk
from contextlib import closing
from queue import Empty, Queue
from tkinter import ttk

from . import luck
from .home import INK, MUTED
from .replay_view import available_actions, chip_text
from .storage import connect, profile
from .table_ui import open_table

COLUMNS = (
    ("name", "Statistic", 275),
    ("n", "Opportunities", 88),
    ("observed", "Observed", 68),
    ("expected", "Expected", 79),
    ("difference", "Difference", 77),
    ("pp", "Δ pp", 64),
    ("sd", "Std. dev.", 66),
    ("z", "z", 58),
    ("percentile", "Percentile", 72),
    ("status", "Interpretation", 162),
)
HINTS = (
    "Any repeated rank, including trips and quads.",
    "Two, three or four aces in the four-card deal.",
    "Exactly two cards in each of two suits.",
    "Four consecutive distinct ranks. An ace can be high or low.",
    "All four hole cards have the same suit.",
    "Exactly one pocket pair. The flop contains one or both remaining cards of its rank.",
    "Two distinct pocket pairs. The flop hits at least one of their ranks.",
    "At least two hole cards of one suit. The flop gives a direct flush draw, a flush or a stronger made hand.",
    "PLO4 straight, flush, full house, quads or straight flush on the flop.",
    "Four different hole ranks. The best legal flop hand is at least two pair.",
    "One card can make a flush. Count all unseen cards making a flush or better.",
    "One card can complete a straight. Count all unseen cards making a straight or better.",
    "Starting from a set or two pair, count cards making a full house, quads or a straight flush.",
)


def fmt(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


class LuckWindow:
    def __init__(self, parent, path, room):
        from .ui import table

        self.path = path
        self.win = tk.Toplevel(parent)
        self.win.title("Luck and variance · PLO Hero Leak Finder")
        self.win.geometry(
            f"{min(1220, self.win.winfo_screenwidth() - 40)}x{min(765, self.win.winfo_screenheight() - 90)}"
        )
        self.messages = Queue()
        self.closed = threading.Event()

        def destroyed(event):
            if event.widget == self.win:
                self.closed.set()
                self.win.after_cancel(self.timer)

        self.win.bind("<Destroy>", destroyed)
        self.pending = False
        self.busy = False
        self.result = None
        body = ttk.Frame(self.win, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Luck and variance", font=("Segoe UI", 19, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            body,
            text="Luck tells you nothing about the quality of your play. This report has no effect on advice.",
        ).pack(anchor="w")
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=7)

        def combo(label, values, initial, width):
            ttk.Label(controls, text=label).pack(side="left")
            var = tk.StringVar(self.win, value=initial)
            widget = ttk.Combobox(
                controls, state="readonly", textvariable=var, values=values, width=width
            )
            widget.pack(side="left", padx=(3, 9))
            return var, widget

        self.room, _ = combo("Room", ("Both", "CoinPoker", "iPoker"), room, 10)
        self.alias, self.alias_box = combo(
            "Hero", ("All selected",), "All selected", 15
        )
        self.stake, self.stake_box = combo(
            "Big blind", ("All stakes",), "All stakes", 12
        )
        self.start, self.end = tk.StringVar(self.win), tk.StringVar(self.win)
        for label, var in (("From", self.start), ("Through", self.end)):
            ttk.Label(controls, text=label).pack(side="left")
            ttk.Entry(controls, textvariable=var, width=11).pack(side="left", padx=3)
        self.apply = ttk.Button(controls, text="Apply filters", command=self.run)
        self.apply.pack(side="left", padx=6)
        options = ttk.Frame(body)
        options.pack(fill="x")
        self.all_flops = tk.BooleanVar(self.win)
        ttk.Checkbutton(
            options,
            variable=self.all_flops,
            text="Include flops dealt after a preflop fold",
            command=self.run,
        ).pack(side="left")
        self.chart_on = tk.BooleanVar(self.win)
        ttk.Checkbutton(
            options,
            variable=self.chart_on,
            text="Show z-score chart",
            command=self.chart,
        ).pack(side="left", padx=14)
        self.status = tk.StringVar(self.win)
        ttk.Label(body, textvariable=self.status, foreground=MUTED).pack(
            anchor="w", pady=3
        )
        area = ttk.Frame(body)
        area.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            area, width=205, background="white", highlightthickness=0
        )
        self.canvas.bind("<Configure>", lambda _: self.draw())
        grid = ttk.Frame(area)
        grid.pack(side="left", fill="both", expand=True)
        self.table = table(grid, COLUMNS, heights=19)
        self.table.bind("<Configure>", lambda _: self.win.after_idle(self.draw))
        previous_scroll = self.table.cget("yscrollcommand")

        def scroll(first, last):
            if previous_scroll:
                self.table.tk.call(previous_scroll, first, last)
            self.win.after_idle(self.draw)

        self.table.configure(yscrollcommand=scroll)
        self.table.tag_configure("warning", background="#ffe0bd", foreground=INK)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.detail())
        self.hint = tk.StringVar(self.win)
        ttk.Label(body, textvariable=self.hint, wraplength=1080).pack(
            anchor="w", pady=4
        )
        self.samples = ttk.Frame(body)
        self.samples.pack(fill="x")
        ttk.Label(
            body,
            text="With this many statistics, some will fall far from expectation by chance. "
            "Unusual begins at |z| ≥ 3. Check parser begins at |z| ≥ 4 or an impossible event. "
            "Under 30 expected hits, the percentile uses the exact mid-distribution and the sample is small.",
            wraplength=1100,
            foreground=MUTED,
        ).pack(anchor="w")
        self.exclusions = tk.StringVar(self.win)
        ttk.Label(body, textvariable=self.exclusions, wraplength=1110).pack(
            anchor="w", pady=4
        )
        self.stakes = {}
        self.run()
        self.timer = self.win.after(80, self.poll)

    def run(self):
        if self.busy:
            self.pending = True
            return
        self.busy = True
        self.apply.state(["disabled"])
        self.status.set("Checking the local luck index…")
        args = (
            self.room.get(),
            self.stakes.get(self.stake.get()),
            self.start.get().strip(),
            self.end.get().strip(),
            "" if self.alias.get() == "All selected" else self.alias.get(),
            self.all_flops.get(),
        )

        def progress(n):
            if self.closed.is_set():
                raise InterruptedError()
            if n and n % 4096 == 0:
                self.messages.put(("status", f"Indexed {n:,} hands…"))

        def work():
            try:
                with closing(connect(self.path)) as db:

                    def rejection_progress(n):
                        if self.closed.is_set():
                            raise InterruptedError()
                        self.messages.put(
                            (
                                "status",
                                f"Checking exclusions from older imports · {n:,} files…",
                            )
                        )

                    updated = luck.refresh(
                        db, progress, rejection_progress=rejection_progress
                    )
                    aliases = sorted(
                        {n for names in profile(db).values() for n in names}
                    )
                    stakes = {
                        f"{chip_text(bb)} {currency}": (currency, bb)
                        for bb, currency in db.execute(
                            "SELECT DISTINCT bb,currency FROM facts ORDER BY currency,bb"
                        )
                    }
                    result = luck.report(db, *args)
                self.messages.put(("report", (result, updated, aliases, stakes, args)))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if self.closed.is_set():
            return
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == "status":
                    self.status.set(data)
                    continue
                self.busy = False
                self.apply.state(["!disabled"])
                if kind == "error":
                    self.status.set(f"Could not load report. {data}")
                else:
                    self.show(*data)
                if self.pending:
                    self.pending = False
                    self.run()
        except Empty:
            pass
        self.timer = self.win.after(100, self.poll)

    def show(self, result, updated, aliases, stakes, args):
        self.result = result
        self.stakes = stakes
        self.alias_box.configure(values=["All selected", *aliases])
        self.stake_box.configure(values=["All stakes", *stakes])
        self.status.set(
            f"{result['hands']:,} Hero hands · {updated:,} hands calculated · "
            + ("All dealt flops" if args[-1] else "Flops seen by Hero")
            + " · Every recorded turn and river counts, including after a fold."
        )
        self.table.delete(*self.table.get_children())
        for i, (name, r) in enumerate(result["rows"].items()):
            status = r["status"] + (" · Check parser" if r["check_parser"] else "")
            self.table.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    name,
                    fmt(r["n"], 0),
                    fmt(r["observed"], 0),
                    fmt(r["expected"]),
                    fmt(r["difference"]),
                    fmt(r["pp"]),
                    fmt(r["sd"]),
                    fmt(r["z"]),
                    fmt(r["percentile"]),
                    status,
                ),
                tags=("warning",)
                if r["check_parser"] or r["status"] == "Unusual"
                else (),
            )
        self.table.selection_set("0")
        excluded = list(result["excluded"].items()) + list(
            result["import_excluded"].items()
        )
        text = "Excluded from this selection · " + (
            ", ".join(f"{k} ({v:,})" for k, v in excluded) or "0"
        )
        if result["outside_hero_selection"]:
            text += (
                f" · Outside selected Hero seats: {result['outside_hero_selection']:,}"
            )
        unknown = result["unattributed_import_records"]
        if unknown:
            text += (
                " · Import records with insufficient filter information · "
                + ", ".join(f"{k} ({v:,})" for k, v in unknown.items())
            )
        self.exclusions.set(text)
        self.detail()
        self.chart()

    def detail(self):
        selection = self.table.selection()
        if not selection or not self.result:
            return
        i = int(selection[0])
        name = luck.NAMES[i]
        self.hint.set(HINTS[i if i < 10 else 10 + (i - 10) % 3])
        for child in self.samples.winfo_children():
            child.destroy()
        for index, (hid, _, number, hero) in enumerate(
            self.result["examples"].get(name, ())
        ):
            ttk.Button(
                self.samples,
                text=f"Replay #{number}",
                command=lambda h=hid, n=hero: self.replay(h, n),
            ).grid(row=index // 5, column=index % 5)

    def replay(self, hid, hero):
        with closing(connect(self.path)) as db:
            options = available_actions(db, hid)
        action = next((o for o in options if o.name == hero), next(iter(options), None))
        open_table(
            self.win,
            self.path,
            hid,
            action.ref if action else None,
            hero=hero,
            actor=action.name if action else hero,
        )

    def chart(self):
        if self.chart_on.get():
            self.canvas.pack(side="right", fill="y")
            self.draw()
        else:
            self.canvas.pack_forget()

    def draw(self):
        if self.closed.is_set() or not self.chart_on.get() or not self.result:
            return
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        rows = list(self.result["rows"].values())
        scale = max(4, max(abs(r["z"] or 0) for r in rows))

        def x(z):
            return w / 2 + z * (w / 2 - 16) / scale

        for z in (-3, 0, 3):
            c.create_line(
                x(z), 19, x(z), h - 8, fill="#a8b4be", dash=(3, 2) if z else ()
            )
            c.create_text(x(z), 10, text=str(z))
        for i, row in enumerate(rows):
            z = row["z"]
            box = self.table.bbox(str(i)) if self.table.exists(str(i)) else ()
            if z is not None and box:
                y = self.table.winfo_rooty() - c.winfo_rooty() + box[1] + box[3] / 2
                c.create_rectangle(
                    x(0),
                    y - 4,
                    x(z),
                    y + 4,
                    fill="#ed811e" if abs(z) >= 3 else "#37738a",
                    outline="",
                )
