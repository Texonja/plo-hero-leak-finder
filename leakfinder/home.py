"""The post-session hand shortlist. Each entry opens at its recorded decision."""

import tkinter as tk
from tkinter import ttk

from .review_queue import REVIEW_PAGE_SIZE

BG = "#edf2f6"
INK = "#192f43"
MUTED = "#516577"
NAV = "#dbe5ed"
NAV_ACTIVE = "#c7d7e4"
ORANGE = "#ff7900"
PLAYED_RED = "#a51c30"
SUGGESTED_GREEN = "#18603a"
LINE = "#cbd7e0"
WHITE = "#ffffff"
SUITS = {
    "s": ("♠", "#24364b"),
    "h": ("♥", "#ad3b46"),
    "d": ("♦", "#3066a2"),
    "c": ("♣", "#23715c"),
}


class HandReview(ttk.Frame):
    def __init__(self, parent, open_hand):
        super().__init__(parent)
        self.open_hand = open_hand
        self.page = 0
        self.differences = tk.BooleanVar(value=False)
        self.current = None
        self.related_pattern = ""
        self.return_page = 0
        self.controls = ttk.Frame(self)
        self.controls.pack(fill="x", pady=(0, 4))
        self.mode = ttk.Checkbutton(
            self.controls,
            variable=self.differences,
            command=self._change_mode,
            text="Guide differences only",
        )
        self.mode.pack(side="left")
        self.back = ttk.Button(
            self.controls, text="Back to results", command=self._close_related
        )
        pager = ttk.Frame(self)
        pager.pack(fill="x", pady=(2, 8))
        self.summary = tk.StringVar(value="Finding Hero decisions…")
        summary_label = ttk.Label(
            pager,
            textvariable=self.summary,
            foreground=INK,
            font=("Segoe UI", 10, "bold"),
            wraplength=500,
        )
        summary_label.pack(side="left")
        pager.bind(
            "<Configure>",
            lambda event: summary_label.configure(
                wraplength=min(500, max(180, event.width - 290))
            ),
        )
        ttk.Style(self).configure(
            "ReviewPage.TButton", font=("Segoe UI", 11, "bold"), padding=(9, 4)
        )
        self.previous = ttk.Button(
            pager,
            text="◀ Previous",
            style="ReviewPage.TButton",
            command=lambda: self._move(-1),
        )
        self.following = ttk.Button(
            pager,
            text="Next ▶",
            style="ReviewPage.TButton",
            command=lambda: self._move(1),
        )
        self.page_text = tk.StringVar()
        self.page_number = ttk.Label(pager, textvariable=self.page_text)
        panel = ttk.Frame(self)
        panel.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(panel, bg=BG, highlightthickness=0, yscrollincrement=14)
        self.scroll = ttk.Scrollbar(panel, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.rows = tk.Frame(self.canvas, bg=BG)
        self.frame_id = self.canvas.create_window(0, 0, anchor="nw", window=self.rows)
        self.rows.bind(
            "<Configure>",
            lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._resize)
        self.bind("<Map>", lambda _: self.canvas.yview_moveto(0))
        self.wrapping = []
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.rows.bind("<MouseWheel>", self._wheel)

    def _resize(self, event):
        self.canvas.itemconfigure(self.frame_id, width=event.width)
        for label in self.wrapping:
            label.configure(wraplength=max(240, event.width - 205))

    def _wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(
                -int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units"
            )
        return "break"

    def clear(self, message):
        self.current = None
        self.mode.state(["disabled"])
        self._hide_pager()
        self.back.pack_forget()
        self._clear_cards(message)

    def _clear_cards(self, message):
        for child in self.rows.winfo_children():
            child.destroy()
        self.wrapping.clear()
        self.summary.set(message)
        self.canvas.yview_moveto(0)

    def paint(self, queue, pockets, empty, action=None):
        self.current = queue, pockets, empty, action
        self.mode.state(["!disabled"])
        self.page = 0
        self.related_pattern = ""
        self._render()

    def _change_mode(self):
        self.related_pattern = ""
        self.page = 0
        self._render()

    def _move(self, delta):
        self.page += delta
        self._render()

    def _open_related(self, pattern):
        self.related_pattern = pattern
        self.return_page = self.page
        self.page = 0
        self._render()

    def _close_related(self):
        self.related_pattern = ""
        self.page = self.return_page
        self._render()

    def _hide_pager(self):
        for widget in (self.previous, self.following, self.page_number):
            widget.pack_forget()

    def _render(self):
        if self.current is None:
            return
        queue, pockets, empty, action = self.current
        self._hide_pager()
        self.back.pack_forget()
        selected = queue.items
        heading = ""
        if self.related_pattern:
            self.back.pack(side="left", padx=(9, 0))
            hands = {}
            for item in queue.guide_items:
                if item.pattern == self.related_pattern:
                    hands.setdefault(item.hand, item)
            selected = tuple(hands.values())
            heading = "Related hands"
        elif self.differences.get():
            selected = queue.guide_items
            heading = "Differences"
            if queue.hand_count:
                empty = "No guide recommendations differ from the played action in this selection."
        total = len(selected)
        if heading and total:
            pages = (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE
            self.page = min(max(self.page, 0), pages - 1)
            start = self.page * REVIEW_PAGE_SIZE
            selected = selected[start : start + REVIEW_PAGE_SIZE]
            self.page_text.set(f"Page {self.page + 1:,} of {pages:,}")
            self.previous.pack(side="left", padx=(12, 4))
            self.page_number.pack(side="left", padx=5)
            self.following.pack(side="left", padx=(4, 0))
            self.previous.configure(state="normal" if self.page else "disabled")
            self.following.configure(
                state="normal" if self.page + 1 < pages else "disabled"
            )
        n = len(selected)
        basis = "latest import" if queue.scope == "Latest import" else "library"
        count = f"{queue.hand_count:,} Hero hand{'s' if queue.hand_count != 1 else ''} in {basis}"
        summary = f"{n} selected hand{'s' if n != 1 else ''}   ·   {count}"
        if queue.hand_count:
            summary += f"   ·   Guide · {len(queue.guide_items):,} differences in {queue.guide_choices:,} decisions with recommendations"
        if heading and n:
            summary = (
                f"{heading} {start + 1:,}–{start + n:,} of {total:,}   ·   {count}"
            )
        self._clear_cards(summary)
        if not n:
            box = tk.Frame(
                self.rows,
                bg=WHITE,
                padx=20,
                pady=20,
                highlightbackground=LINE,
                highlightthickness=1,
            )
            box.pack(fill="x", padx=(0, 6))
            tk.Label(
                box,
                text=empty,
                bg=WHITE,
                fg=INK,
                wraplength=460,
                justify="left",
                font=("Segoe UI", 11),
            ).pack(anchor="w")
            if action:
                ttk.Button(
                    box, text=action[0], command=action[1], style="Review.TButton"
                ).pack(anchor="w", pady=(12, 0))
        for item in selected:
            cards, pos = (
                (item.cards, item.position)
                if item.status == "Guide"
                else pockets[item.hand]
            )
            self._card(item, cards, pos)
        self.canvas.yview_moveto(0)

    def _card(self, item, cards, pos):
        frame = tk.Frame(
            self.rows,
            bg=WHITE,
            highlightbackground=LINE,
            highlightthickness=1,
            padx=15,
            pady=12,
            cursor="hand2",
        )
        frame.pack(fill="x", pady=(0, 7), padx=(0, 6))
        frame.columnconfigure(0, weight=1)
        meta = tk.Frame(frame, bg=WHITE)
        meta.grid(row=0, column=0, sticky="w")
        tk.Label(
            meta,
            text=item.street.upper(),
            bg="#e4edf4",
            fg="#334f68",
            padx=6,
            pady=1,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")
        identity = "  ·  ".join(
            s for s in (item.room, "#" + str(item.number), pos) if s
        )
        tk.Label(meta, text=identity, bg=WHITE, fg=MUTED, font=("Segoe UI", 9)).pack(
            side="left", padx=7
        )
        title = tk.Label(
            frame,
            text=item.title,
            bg=WHITE,
            fg=INK,
            justify="left",
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        )
        title.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        row = 2
        if item.played and item.suggested:
            comparison = tk.Frame(frame, bg=WHITE)
            comparison.grid(row=row, column=0, sticky="w", pady=(3, 0))
            for text, color in (
                ("Played", INK),
                (item.played, PLAYED_RED),
                ("  ·  Suggested", INK),
                (item.suggested, SUGGESTED_GREEN),
            ):
                tk.Label(
                    comparison,
                    text=text,
                    bg=WHITE,
                    fg=color,
                    font=("Segoe UI", 10, "normal" if color == INK else "bold"),
                ).pack(side="left")
            row += 1
        reason = tk.Label(
            frame,
            text=item.explanation or item.reason,
            bg=WHITE,
            fg=INK,
            justify="left",
            font=("Segoe UI", 10),
            anchor="w",
        )
        reason.grid(row=row, column=0, sticky="ew", pady=(2, 0))
        evidence = tk.Label(
            frame,
            text=f"Why review this first?  {item.review_reason}"
            if item.review_reason
            else item.evidence,
            bg=WHITE,
            fg=MUTED,
            justify="left",
            anchor="w",
            font=("Segoe UI", 9),
        )
        evidence.grid(row=row + 1, column=0, sticky="ew", pady=(5, 0))
        for label in (title, reason, evidence):
            label.configure(wraplength=max(240, self.canvas.winfo_width() - 205))
            self.wrapping.append(label)
        right = tk.Frame(frame, bg=WHITE)
        right.grid(row=0, column=1, rowspan=row + 2, sticky="ne", padx=(12, 0))
        pocket = tk.Frame(right, bg=WHITE)
        pocket.pack(anchor="e")
        if len(cards) == 4:
            for card in cards:
                suit, color = SUITS[card[-1].lower()]
                tk.Label(
                    pocket,
                    text=card[:-1] + suit,
                    fg=color,
                    bg="#f7f9fb",
                    width=3,
                    highlightthickness=1,
                    highlightbackground=LINE,
                    font=("Segoe UI", 11, "bold"),
                    pady=3,
                ).pack(side="left", padx=(2, 0))
        button = ttk.Button(
            right,
            text="Open table  ›",
            style="Review.TButton",
            command=lambda: self.open_hand(item),
        )
        button.pack(anchor="e", pady=(9 if cards else 0, 0))
        related = None
        if item.repeat_count > 1 and not self.related_pattern:
            related = ttk.Button(
                right,
                text=f"Related hands ({item.repeat_count:,})",
                command=lambda: self._open_related(item.pattern),
            )
            related.pack(anchor="e", pady=(5, 0))
        label = {"Guide": "Guide", "Supported": "Supported difference"}.get(
            item.status, "Exploratory"
        )
        if item.close:
            label = "Close decision"
        tk.Label(right, text=label, fg=MUTED, bg=WHITE, font=("Segoe UI", 8)).pack(
            anchor="e", pady=(5, 0)
        )

        def wire(widget):
            widget.bind("<MouseWheel>", self._wheel)
            if widget not in (button, related):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _: self.open_hand(item))
            for child in widget.winfo_children():
                wire(child)

        wire(frame)

        def reveal(_):
            top, bottom = frame.winfo_y(), frame.winfo_y() + frame.winfo_height()
            visible = self.canvas.canvasy(0)
            extent = max(1, self.rows.winfo_height())
            if top < visible:
                self.canvas.yview_moveto(top / extent)
            elif bottom > visible + self.canvas.winfo_height():
                self.canvas.yview_moveto((bottom - self.canvas.winfo_height()) / extent)

        button.bind("<FocusIn>", reveal)
