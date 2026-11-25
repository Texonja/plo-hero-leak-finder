"""The post-session hand shortlist. Each entry opens at its recorded decision."""

import json
import tkinter as tk
from tkinter import ttk

from .critique import evidence_label
from .hover_help import HoverHelp
from .preflop_guide import GROUPS
from .review_queue import REVIEW_PAGE_SIZE
from .strategy_guide import PREFLOP_CLASSES

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
CRITIQUE_SPOTS = {
    "Flop c-bet defense": ("flop-cbet-",),
    "Flop value & sizing": ("flop-value-", "flop-size-"),
    "First-in opens": (
        "rfi_",
        "sb_fold_reference",
        "preflop_early_weak_entry",
        "preflop_fold_supported_open",
    ),
    "Facing a 3-bet": (
        "threebet_",
        "preflop_weak_threebet_defense",
        "preflop_cold_threebet_exposed",
    ),
}


class HandReview(ttk.Frame):
    def __init__(self, parent, open_hand):
        super().__init__(parent)
        self.open_hand = open_hand
        self.page = 0
        self.differences = tk.BooleanVar(value=False)
        self.current = None
        self.related_pattern = ""
        self.show_hands = None
        self.show_critiques = False
        self.show_study = False
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
        self.critique_button = ttk.Button(
            self.controls, text="Action critiques", command=self._toggle_critiques
        )
        self.critique_button.pack(side="left", padx=(8, 0))
        self.switch = ttk.Button(
            self.controls, text="Selected hands", command=self._switch
        )
        self.switch.pack(side="left", padx=8)
        self.study_button = ttk.Button(
            self.controls, text="Study spots", command=self._toggle_study
        )
        self.study_button.pack(side="left")
        self.river_calls_only = tk.BooleanVar(value=False)
        self.river_calls_filter = ttk.Checkbutton(
            self.controls, text="Large river calls after a turn call",
            variable=self.river_calls_only, command=self._filter_spots,
        )
        self.critique_spot = ttk.Combobox(
            self.controls,
            state="readonly",
            width=21,
            values=("All spots", *CRITIQUE_SPOTS),
        )
        self.critique_spot.current(0)
        self.critique_spot.bind("<<ComboboxSelected>>", self._filter_spots)
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
        self.critique_button.state(["disabled"])
        self.switch.state(["disabled"])
        self.study_button.state(["disabled"])
        self._hide_pager()
        self.back.pack_forget()
        self.critique_spot.pack_forget()
        self.river_calls_filter.pack_forget()
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
        self.critique_button.state(["!disabled"] if queue.critiques else ["disabled"])
        self.show_critiques = self.show_critiques and bool(queue.critiques)
        self.switch.state(["!disabled"] if queue.patterns else ["disabled"])
        self.study_button.state(["!disabled"] if queue.study else ["disabled"])
        self.show_study = self.show_study and bool(queue.study)
        self.page = 0
        self.related_pattern = ""
        self._render()

    def preview(self, items):
        self.clear("Recent decisions · preparing the complete review")
        for item in items:
            self._card(item, item.cards, item.position)

    def _change_mode(self):
        self.show_study = False
        self.show_critiques = False
        self.related_pattern = ""
        self.page = 0
        self._render()

    def _switch(self):
        self.show_hands = (
            not self._hands_view() if not self.differences.get() else False
        )
        self.differences.set(False)
        self._change_mode()

    def _toggle_study(self):
        self.show_critiques = False
        self.show_study = not self.show_study
        self.river_calls_only.set(False)
        self.differences.set(False)
        self.related_pattern = ""
        self.page = 0
        self._render()

    def _hands_view(self):
        return (
            bool(self.current[0].critiques)
            if self.show_hands is None
            else self.show_hands
        )

    def _toggle_critiques(self):
        self.show_critiques = not self.show_critiques
        self.critique_spot.current(0)
        self.differences.set(False)
        self.show_study = False
        self.related_pattern = ""
        self.page = 0
        self._render()

    def _filter_spots(self, _event=None):
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
        self.river_calls_filter.pack_forget()
        selected = queue.items
        heading = ""
        pattern_page = False
        self.critique_button.configure(
            text="Back to review"
            if self.show_critiques
            else f"Action critiques ({len(queue.critiques):,})"
        )
        self.study_button.configure(
            text="Back to review" if self.show_study else "Study spots"
        )
        self.switch.configure(
            text="Review patterns"
            if self._hands_view() or self.differences.get()
            else "Selected hands"
        )
        if self.show_critiques:
            selected = queue.critiques
            heading = "Action critiques"
            self.critique_spot.pack(side="left", padx=(8, 0))
            spot = self.critique_spot.get()
            if spot in CRITIQUE_SPOTS:
                selected = tuple(
                    i for i in selected if i.node.startswith(CRITIQUE_SPOTS[spot])
                )
                empty = f"No action critiques for {spot.lower()} in the current room, street and hand selection."
        elif self.show_study:
            selected = queue.study
            heading = "Study spots"
            self.river_calls_filter.pack(side="left", padx=8)
            if self.river_calls_only.get():
                selected = tuple(spot for spot in selected if spot.large_river_call)
                heading = "Large river calls after a turn call"
                empty = "No study spots match this betting line in the selected hands and street."
        elif self.related_pattern:
            self.back.pack(side="left", padx=(9, 0))
            group = next(g for g in queue.patterns if g.key == self.related_pattern)
            selected = group.examples
            heading = "Comparable decisions"
        elif self.differences.get():
            selected = queue.guide_items
            heading = "Differences"
            if queue.hand_count:
                empty = "The guide found no differences among the actions it could compare. Other decisions remain open for review."
                if queue.critiques:
                    empty += " Open Action critiques for specific alternatives to your recorded plays."
                elif queue.study:
                    empty += " Open Study spots to inspect decisions outside those comparisons."
        elif queue.patterns and not self._hands_view():
            selected = queue.patterns
            heading = "Review patterns"
            pattern_page = True
        if not self.show_critiques:
            self.critique_spot.pack_forget()
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
        basis = {
            "Latest import": "latest import",
            "Last 5 sessions": "last 5 sessions per room",
        }.get(queue.scope, "library")
        count = f"{queue.hand_count:,} Hero hand{'s' if queue.hand_count != 1 else ''} in {basis}"
        summary = f"{n} selected hand{'s' if n != 1 else ''}   ·   {count}"
        if heading and n:
            summary = (
                f"{heading} {start + 1:,}–{start + n:,} of {total:,}   ·   {count}"
            )
        if pattern_page:
            summary += "\nRepeated across sessions first, then by the lower end of the difference-rate interval."
        if self.show_study:
            summary += "\nOrdered by pot size before Hero acted, largest first. These decisions require assumptions about ranges or future play."
            if self.river_calls_only.get():
                summary += "\nSame opponent bet both streets. Hero called at least 10 BB and two thirds of the opening river pot."
        if queue.hand_count and not self.related_pattern:
            total_indexed = queue.guide_choices + len(queue.study)
            if total_indexed:
                summary += (
                    f"\nGuide compared {queue.guide_choices:,} of {total_indexed:,} indexed decisions "
                    f"({queue.guide_choices / total_indexed:.1%}) · {len(queue.guide_items):,} guide differences."
                    f"  Action critiques: {len(queue.critiques):,}."
                )
        if self.show_critiques:
            summary += "\nOne per rule first: solver comparisons, equity bounds, river value, then other critiques. Larger pots first within a rule. EV is unevaluated."
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
            if self.differences.get() and queue.critiques:
                ttk.Button(
                    box,
                    text=f"See all {len(queue.critiques):,} action critiques",
                    command=self._toggle_critiques,
                ).pack(anchor="w", pady=(8, 0))
                for item in queue.critiques[:3]:
                    self._card(item, item.cards, item.position)
            elif self.differences.get() and queue.study:
                ttk.Button(
                    box, text="See study spots", command=self._toggle_study
                ).pack(anchor="w", pady=(8, 0))
                item = queue.study[0].card()
                self._card(item, item.cards, item.position)
        for item in selected:
            if pattern_page:
                self._pattern_card(item)
                continue
            if self.show_study:
                item = item.card()
            cards, pos = (
                (item.cards, item.position) if item.cards else pockets[item.hand]
            )
            self._card(item, cards, pos)
        self.canvas.yview_moveto(0)

    def _pattern_card(self, group):
        item = group.examples[0]
        box = tk.Frame(
            self.rows,
            bg=WHITE,
            padx=14,
            pady=10,
            highlightthickness=1,
            highlightbackground=LINE,
        )
        box.pack(fill="x", pady=(0, 6), padx=(0, 6))
        box.columnconfigure(0, weight=1)
        context = json.loads(group.key)
        depth = ("under 40", "40–<80", "80–<150", "150+")[context["depth"]]
        family = context["family"]
        if isinstance(family, str):
            structure = (
                GROUPS[family].title
                if family in GROUPS
                else PREFLOP_CLASSES[family].split(".")[0]
            )
        else:
            structure = (
                "High card",
                "One pair",
                "Two pair",
                "Trips",
                "Straight",
                "Flush",
                "Full house",
                "Quads",
                "Straight flush",
            )[family[0]]
        tk.Label(
            box,
            text=f"{item.street.upper()}   ·   {item.room}   ·   {depth} BB effective",
            bg=WHITE,
            fg=MUTED,
        ).grid(row=0, column=0, sticky="w")
        title = tk.Label(
            box,
            text=item.title.removesuffix(" · bet size") + "   ·   " + structure,
            bg=WHITE,
            fg=INK,
            anchor="w",
            justify="left",
            font=("Segoe UI", 11, "bold"),
        )
        title.grid(row=1, column=0, sticky="ew")
        self.wrapping.append(title)
        title.configure(wraplength=max(240, self.canvas.winfo_width() - 205))
        lo, hi = group.interval
        tk.Label(
            box,
            text=f"Guide differences in {group.differences:,} of {group.total:,} comparable decisions ({group.differences / group.total:.0%})"
            f"   ·   {group.total - group.differences:,} followed the guide",
            bg=WHITE,
            fg=INK,
        ).grid(row=2, column=0, sticky="w")
        interval_label = tk.Label(
            box,
            text=("Single example   ·   " if group.total == 1 else "")
            + f"Descriptive 95% interval {lo:.0%}–{hi:.0%}   ·   Differences in {group.difference_sessions} of {group.sessions} dated sessions",
            bg=WHITE,
            fg=MUTED,
        )
        interval_label.grid(row=3, column=0, sticky="w")
        HoverHelp(
            interval_label,
            lambda _: (
                "Wilson interval for how often the played decision differs from the guide. Decisions in one session can be correlated. This does not measure the probability of a mistake."
            ),
        )
        HoverHelp(
            title,
            lambda _: (
                "Matched on room, stakes, positions, earlier action, stack and price bands, hand structure and guide options. Postflop also matches board texture."
            ),
        )
        example = tk.Frame(box, bg=WHITE)
        example.grid(row=4, column=0, sticky="w")
        for label, color in (
            ("Example · Played", INK),
            (item.played, PLAYED_RED),
            ("  Suggested", INK),
            (item.suggested, SUGGESTED_GREEN),
        ):
            tk.Label(
                example,
                text=label,
                bg=WHITE,
                fg=color,
                font=("Segoe UI", 9, "normal" if color == INK else "bold"),
            ).pack(side="left")
        ttk.Button(
            box, text="Open example ›", command=lambda: self.open_hand(item)
        ).grid(row=3, column=1, rowspan=2, padx=6)
        ttk.Button(
            box,
            text="Compare decisions ›",
            command=lambda: self._open_related(group.key),
        ).grid(row=1, column=1, rowspan=2, padx=6)
        for widget in (box, *box.winfo_children(), *example.winfo_children()):
            widget.bind("<MouseWheel>", self._wheel, add="+")

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
                (item.played, PLAYED_RED if item.difference else INK),
                (
                    "  ·  Alternative"
                    if item.status == "Critique"
                    else "  ·  Suggested",
                    INK,
                ),
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
        if item.pattern and item.repeat_count > 1 and not self.related_pattern:
            related = ttk.Button(
                right,
                text=f"Related hands ({item.repeat_count:,})",
                command=lambda: self._open_related(item.pattern),
            )
            related.pack(anchor="e", pady=(5, 0))
        label = {
            "Guide": "Guide",
            "Supported": "Supported difference",
            "Recorded": "Recent hand",
            "Study": "Study only",
            "Critique": evidence_label(item.node),
        }.get(item.status, "Exploratory")
        if item.status == "Guide" and not item.difference:
            label = "Within guide"
        elif item.close:
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
