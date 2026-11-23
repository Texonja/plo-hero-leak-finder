"""Read-only visual replayer for an individual decision in a locally imported hand."""

import json
import math
import tkinter as tk
import zlib
from contextlib import closing
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.font import Font
from tkinter.scrolledtext import ScrolledText

from .engine import replay
from .home import BG, INK, MUTED, NAV, NAV_ACTIVE, ORANGE, SUITS
from .importers import Record, decode
from .opponent_history import (
    OpponentHistory,
    apply_history,
    early_observations,
    observations,
    postflop_opponent,
    river_opponent,
)
from .range_review import build_range_review, river_price_note
from .replay_view import available_actions, chip_text, load_snapshot, river_result
from .storage import connect
from .strategy_guide import advice_at

NAVY = "#0c1b2b"
EDGE = "#35516a"
FELT = "#a0beba"
PAPER = "#f6f7f7"
VERBS = {
    "posts_ante": "posts ante",
    "posts_sb": "posts small blind",
    "posts_bb": "posts big blind",
    "check": "checks",
    "call": "calls",
    "fold": "folds",
    "bet": "bets",
    "raise": "raises",
    "return": "receives refund",
}


def _read_hand(room, row):
    return decode(Record(room, zlib.decompress(row["raw"]).decode(), row["locator"]))


def open_table(parent, dbpath, hand_id, action_ref, hero=None, actor=None):
    """Open the clicked opportunity. hand_id is the local hands.id, ref its source anchor."""
    try:
        return TableDialog(parent, dbpath, hand_id, action_ref, hero, actor)
    except (ValueError, LookupError) as exc:
        messagebox.showinfo("Table replay", str(exc), parent=parent)
        return None


def _selected_decision(db, hand_id, ref, hero, actor):
    row = db.execute("SELECT room,hint FROM hands WHERE id=?", (hand_id,)).fetchone()
    if row is None:
        raise ValueError("This hand is unavailable in the selected library.")
    aliases = {
        r[0]
        for r in db.execute(
            "SELECT s.name FROM seats s JOIN aliases a ON a.name=s.name "
            "WHERE s.hand=? AND a.room=?",
            (hand_id, row["room"]),
        )
    }
    # Multiple aliases at one table do not identify which one was the user's seat.
    profile_name = hero or (next(iter(aliases)) if len(aliases) == 1 else row["hint"])
    if ref and not ref.startswith("Seat "):
        if not actor:
            actor = next(
                (
                    option.name
                    for option in available_actions(db, hand_id, None)
                    if option.ref == ref
                ),
                None,
            )
        name = actor or profile_name
        options = available_actions(db, hand_id, name)
        for index, opt in enumerate(options):
            if opt.ref == ref:
                return name, profile_name, options, index, ""
        raise ValueError(
            f"The selected source reference ({ref}) has no replayable decision."
        )
    if ref and not actor:
        source = db.execute(
            "SELECT locator,raw FROM origins WHERE hand=? ORDER BY rowid LIMIT 1",
            (hand_id,),
        ).fetchone()
        if source:
            hand = _read_hand(row["room"], source)
            actor = hand.seats.get(int(ref.split()[1]))
    name = actor or profile_name
    if not name:
        raise ValueError("Select a player to replay their decisions.")
    options = available_actions(db, hand_id, name)
    if not options:
        raise ValueError(f"{name} has no recorded voluntary decision in this hand.")
    return name, profile_name, options, 0, "First recorded decision"


def _size_dialog(win, preferred_width, preferred_height, min_width, min_height):
    available_width = max(1, win.winfo_screenwidth() - 40)
    available_height = max(1, win.winfo_screenheight() - 95)
    width = min(preferred_width, available_width)
    height = min(preferred_height, available_height)
    x = (win.winfo_screenwidth() - width) // 2
    y = max(8, (win.winfo_screenheight() - height - 55) // 2)
    win.minsize(min(min_width, width), min(min_height, height))
    win.geometry(f"{width}x{height}+{x}+{y}")


def _label(parent, text="", size=10, fg=INK, bg=BG, bold=False, **options):
    return tk.Label(
        parent,
        text=text,
        background=bg,
        foreground=fg,
        font=("Segoe UI", size, "bold" if bold else "normal"),
        **options,
    )


def _button(parent, text, command, accent=False):
    return tk.Button(
        parent,
        text=text,
        command=command,
        relief="flat",
        bd=0,
        padx=13,
        pady=6,
        cursor="hand2",
        highlightthickness=1,
        highlightbackground=EDGE,
        background=ORANGE if accent else NAV,
        foreground=INK,
        activebackground="#ffd1a4" if accent else NAV_ACTIVE,
        activeforeground=INK,
        disabledforeground=MUTED,
        font=("Segoe UI", 9, "bold" if accent else "normal"),
    )


class TableDialog:
    def __init__(self, parent, dbpath, hand_id, ref, hero=None, actor=None):
        self.path = dbpath
        self.hand_id = hand_id
        self.opponent_histories = {}
        with closing(connect(dbpath)) as db:
            self.actor, self.profile, self.options, self.index, self.entry_label = (
                _selected_decision(db, hand_id, ref, hero, actor)
            )
            self.snapshot = load_snapshot(
                db, self.hand_id, self.actor, self.options[self.index].ref
            )
        self.win = tk.Toplevel(parent, background=BG)
        self.win.title(f"Hand {self.snapshot.number} · Table replay")
        _size_dialog(self.win, 1090, 670, 860, 535)
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(1, weight=1)
        self._name_font = Font(root=self.win, family="Segoe UI", size=10, weight="bold")
        self.win.bind("<Escape>", self._escape)
        head = tk.Frame(self.win, bg=BG, padx=18, pady=12)
        head.grid(row=0, column=0, sticky="ew")
        _label(head, "Table replay", 18, bold=True).pack(side="left")
        self.subtitle = _label(head, size=10, fg=MUTED)
        self.subtitle.pack(side="left", padx=15, pady=(5, 0))
        _button(head, "Full hand history…", self.open_source).pack(side="right")
        self.range_button = _button(head, "Range and History…", self.open_ranges)
        self.range_button.pack(side="right", padx=(0, 6))
        content = tk.Frame(self.win, bg=EDGE)
        content.grid(row=1, column=0, sticky="nsew", padx=14)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            content, background=NAVY, highlightthickness=0, width=620, height=390
        )
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        self.canvas.bind("<Configure>", self.draw)
        sidebar = tk.Frame(content, background=PAPER, width=252)
        sidebar.grid(row=0, column=1, sticky="nsew", padx=(0, 1), pady=1)
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        _label(sidebar, "Action history", 13, fg=INK, bg=PAPER, bold=True).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 0)
        )
        _label(sidebar, "Before the selected decision", 9, fg=MUTED, bg=PAPER).grid(
            row=1, column=0, sticky="w", padx=14, pady=(1, 8)
        )
        frame = tk.Frame(sidebar, bg=PAPER)
        frame.grid(row=2, column=0, sticky="nsew", padx=(14, 4), pady=(0, 10))
        self.timeline = tk.Text(
            frame,
            width=26,
            bg=PAPER,
            fg=INK,
            font=("Segoe UI", 10),
            relief="flat",
            bd=0,
            highlightthickness=0,
            wrap="word",
            selectbackground=NAV_ACTIVE,
            selectforeground=INK,
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.timeline.yview)
        self.timeline.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.timeline.pack(side="left", fill="both", expand=True)
        self.timeline.tag_configure(
            "street",
            foreground="#285f63",
            spacing1=7,
            spacing3=3,
            font=("Segoe UI", 9, "bold"),
        )
        self.timeline.tag_configure(
            "active",
            foreground=INK,
            background="#ffcd98",
            spacing1=6,
            font=("Segoe UI", 10, "bold"),
        )
        self.timeline.tag_configure("amount", foreground=MUTED)
        self.outcome_panel = tk.Frame(self.win, bg=PAPER, padx=12, pady=5)
        self.outcome_panel.grid(row=2, column=0, sticky="ew", padx=14, pady=(5, 0))
        self.outcome_panel.grid_remove()
        self.outcome_summary = _label(
            self.outcome_panel, bold=True, bg=PAPER, justify="left"
        )
        self.outcome_summary.pack(anchor="w")
        self.outcome_cards = _label(
            self.outcome_panel, size=9, bg=PAPER, justify="left"
        )
        self.outcome_cards.pack(anchor="w")
        self.outcome_panel.bind("<Configure>", self._wrap_outcome)
        controls = tk.Frame(self.win, bg=BG, padx=16, pady=10)
        controls.grid(row=3, column=0, sticky="ew")
        self.status = _label(controls, size=10, fg=MUTED, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.prev = _button(controls, "‹ Previous decision", lambda: self.move(-1))
        self.prev.pack(side="left", padx=(6, 4))
        self.next = _button(controls, "Next decision ›", lambda: self.move(1))
        self.next.pack(side="left", padx=4)
        self.reveal = _button(
            controls, "Reveal played action", self.toggle_played, True
        )
        self.reveal.pack(side="left", padx=(4, 0))
        self.revealed = False
        self.result = None
        self._refresh()

    def _wrap_outcome(self, event):
        for label in (self.outcome_summary, self.outcome_cards):
            label.configure(wraplength=max(260, event.width - 24))

    def _opponent_history(self, snap):
        opponent = postflop_opponent(snap)
        if opponent is None:
            return OpponentHistory()
        key = snap.room, opponent.name, snap.street
        if key not in self.opponent_histories:
            with closing(connect(self.path)) as db:
                rows = (
                    observations(db, snap.room, opponent.name)
                    if snap.street == "River"
                    else early_observations(db, snap.room, opponent.name, snap.street)
                )
                self.opponent_histories[key] = OpponentHistory(rows)
        return self.opponent_histories[key]

    def open_ranges(self):
        snap = self.snapshot
        hero = next(s for s in snap.seats if s.is_hero)
        opponents = [s for s in snap.seats if s.live and s.name != hero.name]
        if (
            snap.street not in {"Flop", "Turn", "River"}
            or not snap.hero_cards
            or not opponents
            or self.actor != self.profile
        ):
            return None
        history = self._opponent_history(snap)
        win = tk.Toplevel(self.win, background=BG)
        win.title(f"Range and History · {snap.room} #{snap.number}")
        _size_dialog(win, 920, 710, 660, 470)
        controls = ttk.Frame(win, padding=(12, 8))
        controls.pack(fill="x")
        ttk.Label(controls, text="Opponent").pack(side="left")
        picker = ttk.Combobox(
            controls,
            state="readonly",
            width=35,
            values=[f"{s.position} · {s.name}" for s in opponents],
        )
        picker.pack(side="left", padx=7)
        picker.current(0)
        bet = next(
            (
                a.name
                for a in reversed(snap.history)
                if a.street == snap.street and a.action in {"bet", "raise"}
            ),
            None,
        )
        if bet:
            picker.current(
                next((i for i, seat in enumerate(opponents) if seat.name == bet), 0)
            )
        text = ScrolledText(
            win, wrap="word", bg=PAPER, fg=INK, font=("Segoe UI", 10), padx=12, pady=8
        )
        text.pack(fill="both", expand=True, padx=12)
        source_controls = ttk.Frame(win, padding=12)
        source_controls.pack(fill="x")
        ttk.Label(source_controls, text="Past hand").pack(side="left")
        evidence = ttk.Combobox(source_controls, state="readonly", width=45)
        evidence.pack(side="left", padx=7)
        source_rows = ()

        def open_evidence():
            if evidence.current() >= 0:
                row = source_rows[evidence.current()]
                linked = open_table(win, self.path, row.hand, row.ref, actor=row.actor)
                if linked:
                    linked.open_source()

        source_button = ttk.Button(
            source_controls, text="Open source…", command=open_evidence
        )
        source_button.pack(side="left")

        def display(_=None):
            nonlocal source_rows
            report = build_range_review(
                snap, hero, opponents[picker.current()], history
            )
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", report.text)
            text.configure(state="disabled")
            source_rows = report.sources
            evidence.configure(
                values=[
                    f"{'Matched spot' if (row.hand, row.ref) in report.matched_sources else 'Broader sample'} · #{row.number} · {row.ref} · {row.outcome}"
                    + (" · " + " ".join(row.cards) if row.cards else "")
                    for row in source_rows
                ]
            )
            if source_rows:
                evidence.current(0)
            else:
                evidence.set("")
            source_button.configure(state="normal" if source_rows else "disabled")

        picker.bind("<<ComboboxSelected>>", display)
        display()
        return win

    def _escape(self, event):
        if event.widget.winfo_toplevel() == self.win:
            self.close()

    def close(self):
        self.win.destroy()

    def _refresh(self):
        snap = self.snapshot
        eligible = (
            snap.street in {"Flop", "Turn", "River"}
            and snap.hero_cards
            and self.actor == self.profile
        )
        self.range_button.configure(state="normal" if eligible else "disabled")
        self.range_button.configure(
            text="What would change this decision?"
            if eligible and snap.street in {"Turn", "River"}
            else "Range and History…"
        )
        self.subtitle.configure(
            text=f"{snap.room}   ·   Hand #{snap.number}   ·   PLO4"
        )
        self.status.configure(
            text=f"{self.entry_label + '   ·   ' if self.entry_label else ''}{snap.street}   ·   {snap.ref}"
        )
        self.prev.configure(state="normal" if self.index else "disabled")
        self.next.configure(
            state="normal" if self.index + 1 < len(self.options) else "disabled"
        )
        label = "action & outcome" if snap.street == "River" else "played action"
        self.reveal.configure(text=f"{'Hide' if self.revealed else 'Reveal'} {label}")
        self.outcome_panel.grid_remove()
        box = self.timeline
        box.configure(state="normal")
        box.delete("1.0", "end")
        last = ""
        for step in snap.history:
            if step.street != last:
                box.insert("end", step.street.upper() + "\n", "street")
                last = step.street
            box.insert(
                "end",
                f"{step.name}  {VERBS.get(step.action, step.action.replace('_', ' '))}",
            )
            if step.raised_to is not None and step.action == "raise":
                box.insert("end", " to " + chip_text(step.raised_to), "amount")
            elif step.amount:
                box.insert("end", " " + chip_text(step.amount), "amount")
            if step.all_in:
                box.insert("end", "  ·  all-in", "amount")
            box.insert("end", "\n")
        if last != snap.street:
            box.insert("end", snap.street.upper() + "\n", "street")
        box.insert("end", f"{self.actor} to act\n", "active")
        if self.revealed:
            box.insert("end", self.played_text + "\n", "active")
        suggestion = advice_at(snap) if self.actor == self.profile else None
        if eligible and suggestion and river_opponent(snap):
            suggestion = apply_history(snap, suggestion, self._opponent_history(snap))
        if suggestion is not None:
            box.insert("end", "\nGUIDE · " + suggestion.title.upper() + "\n", "street")
            box.insert(
                "end",
                (suggestion.play.capitalize() or "Manual review") + "\n",
                "active",
            )
            box.insert("end", suggestion.why + "\nStudy recommendation.", "amount")
            if (
                eligible
                and snap.street == "River"
                and snap.to_call
                and river_opponent(snap)
                and not any(
                    a.street == "River" and a.action == "fold" for a in snap.history
                )
            ):
                box.insert(
                    "end",
                    "\n\n" + river_price_note(snap, self._opponent_history(snap)),
                    "amount",
                )
        if self.revealed and snap.street == "River":
            self._show_outcome(box, snap)
        box.configure(state="disabled")
        box.see("end")
        self.draw()

    def _show_outcome(self, box, snap):
        result = self.result
        summary = "Showdown" if result.showdown else "Hand ended by folds"
        if result.awards:
            payouts = " · ".join(
                f"{name} collected {chip_text(amount)} {snap.currency}"
                for name, amount in result.awards
            )
            summary += f" · {payouts}"
            if not result.verified:
                summary += " (source-reported)"
        else:
            summary += " · Payout unavailable in the source"
        if result.net_bb is not None and self.actor == self.profile:
            summary += f" · Hero {result.net_bb:+.2f} BB"
        holdings = " · ".join(
            f"{name}: {' '.join(cards) if cards else 'cards unrecorded'}"
            for name, cards in result.opponents
        )
        self.outcome_summary.configure(text=summary)
        self.outcome_cards.configure(
            text=f"Opponent cards · {holdings}" if holdings else ""
        )
        self.outcome_panel.grid()
        box.insert("end", "\nHAND OUTCOME\n", "street")
        for event in result.subsequent:
            amount = f" · {chip_text(event.amount)} paid" if event.amount else ""
            box.insert("end", f"{event.player} {VERBS[event.action]}{amount}\n")
        box.insert("end", summary + "\n" + holdings, "amount")

    def move(self, delta):
        index = self.index + delta
        if not 0 <= index < len(self.options):
            return
        with closing(connect(self.path)) as db:
            snapshot = load_snapshot(
                db, self.hand_id, self.actor, self.options[index].ref
            )
        self.snapshot = snapshot
        self.index = index
        self.revealed = False
        self.entry_label = ""
        self._refresh()

    def toggle_played(self):
        if not self.revealed:
            with closing(connect(self.path)) as db:
                row = db.execute(
                    """SELECT o.locator,o.raw,h.cards FROM origins o
                       JOIN hands h ON h.id=o.hand WHERE o.hand=?
                       ORDER BY o.rowid LIMIT 1""",
                    (self.hand_id,),
                ).fetchone()
            hand = _read_hand(self.snapshot.room, row)
            _, streets = replay(hand)
            event = next(
                e for e in streets[self.snapshot.street] if e.ref == self.snapshot.ref
            )
            amount = f" · {chip_text(event.amount)} paid" if event.amount else ""
            self.played_text = f"Played: {event.action}{amount}"
            if self.snapshot.street == "River":
                if row["cards"]:
                    hand.cards.update(json.loads(row["cards"]))
                self.result = river_result(
                    hand, streets["River"], self.actor, self.snapshot.ref
                )
        self.revealed = not self.revealed
        self._refresh()

    def open_source(self):
        from .explorer import source_span

        with closing(connect(self.path)) as db:
            rows = db.execute(
                "SELECT i.path, o.locator, o.raw FROM origins o JOIN imports i ON i.id=o.source "
                "WHERE o.hand=? ORDER BY o.rowid",
                (self.hand_id,),
            ).fetchall()
        if not rows:
            return None
        win = tk.Toplevel(self.win, background=BG)
        win.title(f"Full hand history · {self.snapshot.room} #{self.snapshot.number}")
        _size_dialog(win, 900, 610, 640, 400)
        bar = tk.Frame(win, bg=BG, padx=12, pady=8)
        bar.pack(fill="x")
        _label(bar, "Source copy", fg=MUTED).pack(side="left", padx=(0, 8))
        combo = ttk.Combobox(
            bar,
            state="readonly",
            values=[f"{Path(r['path']).name}   ·   {r['locator']}" for r in rows],
        )
        combo.pack(side="left", fill="x", expand=True)
        copy_path = _label(win, size=9, fg=MUTED, anchor="w")
        copy_path.pack(fill="x", padx=12, pady=(0, 6))
        text = ScrolledText(
            win,
            bg=PAPER,
            fg=INK,
            selectbackground=NAV_ACTIVE,
            selectforeground=INK,
            relief="flat",
            font=("Consolas", 10),
            wrap="word",
            padx=10,
            pady=8,
        )
        text.pack(fill="both", expand=True)
        text.tag_configure("focus", background=ORANGE, foreground=NAVY)
        snap = self.snapshot
        # A duplicate source can add lines or use different XML action numbers.
        # Equivalent hands preserve the replay index of each street decision.
        _, streets = replay(_read_hand(snap.room, rows[0]))
        street_index = next(
            i for i, e in enumerate(streets[snap.street]) if e.ref == snap.ref
        )

        def show(_=None):
            row = rows[combo.current()]
            raw = zlib.decompress(row["raw"]).decode()
            copy_path.configure(text=row["path"])
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", raw)
            _, actions = replay(_read_hand(snap.room, row))
            span = source_span(raw, actions[snap.street][street_index].ref)
            if span:
                first, last = (f"1.0+{v}c" for v in span)
                text.tag_add("focus", first, last)
                text.see(first)
            text.configure(state="disabled")

        combo.bind("<<ComboboxSelected>>", show)
        combo.current(0)
        show()
        return win

    def card(self, x, y, value, width, height):
        c = self.canvas
        if value is None:
            c.create_rectangle(
                x, y, x + width, y + height, fill="#93b0ad", outline="#688e89"
            )
            return
        rank = value[:-1]
        suit, color = SUITS[value[-1]]
        c.create_rectangle(
            x + 1, y + 2, x + width + 1, y + height + 2, fill="#122f34", outline=""
        )
        c.create_rectangle(
            x, y, x + width, y + height, fill="#f6f5ec", outline="#e8e9e5"
        )
        c.create_text(
            x + width * 0.48,
            y + height * 0.32,
            text="10" if rank == "T" else rank,
            fill=color,
            font=("Segoe UI", max(9, int(width * 0.38)), "bold"),
        )
        c.create_text(
            x + width * 0.50,
            y + height * 0.72,
            text=suit,
            fill=color,
            font=("Segoe UI Symbol", max(8, int(width * 0.36))),
        )

    def draw(self, _=None):
        c, snap = self.canvas, self.snapshot
        w, h = c.winfo_width(), c.winfo_height()
        if w < 100 or h < 100:
            return
        c.delete("all")
        cx, cy = w / 2, h / 2
        c.create_oval(
            46, 48, w - 46, h - 43, fill="#182b3d", outline="#273e50", width=3
        )
        c.create_oval(53, 55, w - 53, h - 50, fill=FELT, outline="#709592", width=2)
        c.create_oval(64, 66, w - 64, h - 61, outline="#88aaa6")
        c.create_text(
            cx,
            cy - 70,
            text=snap.street.upper(),
            fill=INK,
            font=("Segoe UI", 10, "bold"),
        )
        cw, ch, gap = 40, 53, 5
        left = cx - (5 * cw + 4 * gap) / 2
        for i in range(5):
            self.card(
                left + i * (cw + gap),
                cy - 49,
                snap.board[i] if i < len(snap.board) else None,
                cw,
                ch,
            )
        c.create_text(
            cx,
            cy + 22,
            text=f"Pot  {chip_text(snap.pot)} {snap.currency}",
            fill=NAVY,
            font=("Segoe UI", 15, "bold"),
        )
        price = (
            f"To call  {chip_text(snap.to_call)}" if snap.to_call else "No bet to call"
        )
        c.create_text(
            cx,
            cy + 46,
            text=f"{price}   ·   {chip_text(snap.bb)} big blind",
            fill=INK,
            font=("Segoe UI", 10),
        )
        seats = sorted(snap.seats, key=lambda s: s.seat)
        focal = next(i for i, s in enumerate(seats) if s.name == self.actor)
        seats = seats[focal:] + seats[:focal]
        shown = (
            dict(self.result.opponents)
            if self.revealed and snap.street == "River"
            else {}
        )
        half = min(74, max(51, (w - 60) / 9))
        for i, s in enumerate(seats):
            a = math.pi / 2 + i * math.tau / len(seats)
            x = cx + (cx - half - 9) * math.cos(a)
            y = cy + (cy - 56) * math.sin(a)
            active = i == 0
            fill = ORANGE if active else BG if s.live else "#b4c3cd"
            edge = ORANGE if active else "#698593" if s.live else EDGE
            fg = INK
            if cards := shown.get(s.name):
                for ci, card in enumerate(cards):
                    self.card(x - 49 + ci * 25, y - 56, card, 23, 33)
            elif s.live and not active:
                for ci in range(4):
                    card_x = x - 17 + ci * 7
                    c.create_rectangle(
                        card_x,
                        y - 34,
                        card_x + 12,
                        y - 19,
                        fill="#294e64",
                        outline="#89a6ad",
                    )
            c.create_rectangle(
                x - half,
                y - 22,
                x + half,
                y + 22,
                fill=fill,
                outline=edge,
                width=2 if active else 1,
            )
            font = self._name_font
            name = s.name
            if font.measure(name) > 2 * half - 12:
                while len(name) > 1 and font.measure(name + "…") > 2 * half - 12:
                    name = name[:-1]
                name += "…"
            c.create_text(x, y - 8, text=name, fill=fg, font=font)
            state = (
                "Folded" if not s.live else "All-in" if s.all_in else chip_text(s.stack)
            )
            c.create_text(
                x,
                y + 10,
                text=f"{s.position}  ·  {state}",
                fill=INK,
                font=("Segoe UI", 9),
            )
            if s.is_button:
                c.create_oval(
                    x + half - 9,
                    y - 29,
                    x + half + 7,
                    y - 13,
                    fill="#e4e4d3",
                    outline=NAVY,
                )
                c.create_text(
                    x + half - 1,
                    y - 21,
                    text="D",
                    fill=NAVY,
                    font=("Segoe UI", 8, "bold"),
                )
            if active:
                hero = "HERO" if self.profile == self.actor else "PLAYER"
                c.create_rectangle(
                    x - half, y + 21, x + half, y + 41, fill=ORANGE, outline=ORANGE
                )
                c.create_text(
                    x,
                    y + 31,
                    text=(
                        "OUTCOME SHOWN"
                        if self.revealed and snap.street == "River"
                        else f"{hero} TO ACT"
                    ),
                    fill=NAVY,
                    font=("Segoe UI", 9, "bold"),
                )
                cards = snap.hero_cards
                cw, ch, gap = 32, 43, 3
                if cards:
                    x0 = x - (len(cards) * cw + (len(cards) - 1) * gap) / 2
                    for ci, card in enumerate(cards):
                        self.card(x0 + ci * (cw + gap), y - 70, card, cw, ch)
                else:
                    c.create_rectangle(
                        x - half, y - 48, x + half, y - 23, fill=BG, outline=EDGE
                    )
                    c.create_text(
                        x,
                        y - 36,
                        text="Pocket cards unavailable",
                        fill=INK,
                        font=("Segoe UI", 9),
                    )
