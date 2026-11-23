"""Descriptive PLO reports and paged, original-hand evidence."""

from __future__ import annotations

import math
import re
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from queue import Empty, Queue
from threading import Thread

from .analysis import wilson
from .hover_help import CONFIDENCE_HELP, HoverHelp, tree_help
from .importers import SCALE
from .statistics import BITS, DEFS, sizing
from .storage import cohort, connect

DIMENSIONS = (
    "Overall",
    "Position",
    "Stack depth",
    "Stake",
    "Room",
    "Hand class",
    "Chosen sizing",
    "Faced sizing",
    "Opponent",
    "Session",
    "Trends",
    "Day",
    "Month",
)
SIZE_ORDER = [sizing(x) for x in (0, 0.4, 0.6, 0.9, 1.1, None)]
DEPTH_ORDER = ("<40 BB", "40–<80 BB", "80–<150 BB", "150+ BB")
ALIASES = {
    "Sessions": "Session",
    "Leak Trends": "Trends",
    "By Street": "Street",
    "Hand Class": "Hand class",
    "Stack Depth": "Stack depth",
    "Vs Opponent": "Opponent",
    "By Sizing": "Faced sizing",
    "Time period": "Month",
}


def timestamp(value):
    value = (value or "").strip().replace("/", "-")
    if len(value) < 16:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class Group:
    key: str
    x: int = 0
    n: int = 0
    hands: int = 0
    known_net: int = 0
    net: float = 0
    net_sq: float = 0
    result_hands: int = 0
    previous: float | None = None
    previous_net: float | None = None
    chosen_mean: float | None = None
    chosen_sq_mean: float | None = None

    @property
    def rate(self):
        return self.x / self.n if self.n else None

    @property
    def bb100(self):
        return 100 * self.net / self.known_net if self.known_net else None

    @property
    def net_interval(self):
        n = self.known_net
        if n < 2:
            return None
        variance = max(0.0, (self.net_sq - self.net * self.net / n) / (n - 1))
        if not variance:
            return None
        half = 196 * math.sqrt(variance / n)
        return self.bb100 - half, self.bb100 + half


class Reports:
    """A snapshot belonging to one open report window and one selected Hero."""

    def __init__(self, db, room):
        self.db = db
        self.room = room
        cohort(db, room)
        db.create_function("lf_size", 1, sizing, deterministic=True)
        db.create_function(
            "lf_depth",
            1,
            lambda d: DEPTH_ORDER[d] if d in range(4) else "Unknown",
            deterministic=True,
        )
        db.execute("""CREATE TEMP TABLE roster AS SELECT c.hand,c.hero,h.room,h.number,h.day,
            f.stamp,f.bb,f.currency,p.pos,p.tags,p.net,'' AS session
            FROM cohort c JOIN hands h ON h.id=c.hand JOIN facts f ON f.hand=c.hand
            JOIN players p ON p.hand=c.hand AND p.name=c.hero""")
        db.execute("CREATE UNIQUE INDEX roster_id ON roster(hand)")
        # A hand-level result belongs to a named opponent only when that player
        # was Hero's sole opponent at the deal, including when the pot was split.
        db.execute("""CREATE TEMP VIEW duels AS SELECT r.hand,min(s.name) AS rival
            FROM roster r JOIN seats s ON s.hand=r.hand AND s.name<>r.hero
            GROUP BY r.hand HAVING count(*)=1""")
        self.has_sessions = False
        self.groups = {}
        self.metric = ""
        self.dimension = ""
        self.coverage = (0, 0, 0)

    def sessions(self):
        if self.has_sessions:
            return
        updates = []
        previous = {}
        starts = {}
        # SQLite's julianday orders offset-qualified timestamps on one UTC
        # clock. Keep local timestamps on their own clock when offsets are absent.
        for r in self.db.execute(
            "SELECT hand,room,stamp FROM roster ORDER BY room,julianday(replace(stamp,'/','-')),hand"
        ):
            current = timestamp(r["stamp"])
            room = r["room"]
            if current is None:
                key = "Unknown time · " + room
            else:
                utc = current.utcoffset() is not None
                if utc:
                    current = current.astimezone(UTC)
                clock = (room, utc)
                last = previous.get(clock)
                if last is None or current - last > timedelta(minutes=45):
                    starts[clock] = current.strftime("%Y-%m-%d %H:%M:%S") + (
                        " UTC" if utc else ""
                    )
                key = starts[clock] + " · " + room
                previous[clock] = current
            updates.append((key, r["hand"]))
        self.db.executemany("UPDATE roster SET session=? WHERE hand=?", updates)
        self.has_sessions = True

    def report(self, metric, dimension="Position", start="", end=""):
        dimension = ALIASES.get(dimension, dimension)
        if dimension not in DIMENSIONS or metric not in DEFS:
            raise ValueError("Unknown metric or breakdown.")
        for value in (start, end):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Use dates in YYYY-MM-DD format.")
        if start and end and start > end:
            raise ValueError("Start date must precede end date.")
        self.metric, self.dimension = metric, dimension
        if dimension in {"Trends", "Session"}:
            self.sessions()
        expr = {
            "Overall": "'All selected hands'",
            "Position": "r.pos",
            "Room": "r.room",
            "Stake": f"r.room || ' · ' || r.currency || ' ' || printf('%g',r.bb/{float(SCALE)}) || ' BB'",
            "Hand class": "tag.value",
            "Stack depth": "lf_depth(o.depth)",
            "Chosen sizing": "lf_size(o.chosen)",
            "Faced sizing": "lf_size(o.faced)",
            "Opponent": "r.room || ' · ' || coalesce(nullif(o.opp,''),duels.rival)",
            "Session": "r.session",
            "Trends": "r.session",
            "Month": "CASE WHEN date(r.day) IS NULL THEN 'Unknown date' ELSE substr(r.day,1,7) END",
            "Day": "COALESCE(date(r.day),'Unknown date')",
        }[dimension]
        extra_join = (
            "JOIN json_each(r.tags) AS tag" if dimension == "Hand class" else ""
        )
        extra_where = ""
        if dimension == "Opponent":
            extra_join = "LEFT JOIN duels ON duels.hand=r.hand AND o.ref LIKE 'Seat %'"
            extra_where = "AND (o.opp<>'' OR duels.rival IS NOT NULL)"
        window = "(?='' OR r.day>=?) AND (?='' OR r.day<=?)"
        dates = (start, start, end, end)
        self.dates = dates
        db = self.db
        known, total = db.execute(
            "SELECT coalesce(sum(r.tags!='[\"Unknown\"]'),0),count(*) FROM roster r WHERE "
            + window,
            dates,
        ).fetchone()
        db.execute("DROP TABLE IF EXISTS temp.members")
        db.execute(
            f"""CREATE TEMP TABLE members AS SELECT {expr} AS bucket,r.hand,o.ref,
                ((o.hits & ?)<>0) AS hit,r.net,o.chosen FROM roster r
                CROSS JOIN obs o ON o.hand=r.hand AND o.actor=r.hero {extra_join}
                WHERE (o.eligible & ?)<>0 AND {window} {extra_where}""",
            (BITS[metric], BITS[metric], *dates),
        )
        db.execute("CREATE INDEX member_group ON members(bucket,hand,ref)")
        self.groups = {}
        is_size = metric.endswith("_SIZE")
        total_chosen = db.execute("SELECT count(*) FROM members").fetchone()[0]
        for bucket, x, n, hands, mean, mean_sq in db.execute(
            "SELECT bucket,sum(hit),count(*),count(DISTINCT hand),avg(chosen),avg(chosen*chosen) FROM members GROUP BY bucket"
        ):
            # Chosen-size frequencies use all opening bets on the chosen street as denominator.
            self.groups[bucket] = Group(
                bucket,
                x,
                total_chosen if is_size and dimension == "Chosen sizing" else n,
                hands,
            )
            if is_size and dimension != "Chosen sizing":
                self.groups[bucket].chosen_mean = mean
                self.groups[bucket].chosen_sq_mean = mean_sq
        if dimension in {"Session", "Trends"}:
            query = f"""SELECT r.session,count(*),count(r.net),sum(r.net),sum(r.net*r.net)
                      FROM roster r WHERE {window} GROUP BY r.session"""
            outcomes = db.execute(query, dates)
        elif metric == "NET":
            outcomes = db.execute("""SELECT bucket,count(*),count(net),sum(net),sum(net*net)
                FROM members GROUP BY bucket""")
        else:
            outcomes = []
        for bucket, hands, reconciled, net, net_sq in outcomes:
            g = self.groups.setdefault(bucket, Group(bucket))
            g.result_hands, g.known_net, g.net, g.net_sq = (
                hands,
                reconciled,
                net or 0.0,
                net_sq or 0.0,
            )
        if dimension == "Trends":
            last = {}
            for key in sorted(self.groups):
                if key.startswith("Unknown"):
                    continue
                session_start, room = key.rsplit(" · ", 1)
                clock = (room, session_start.endswith(" UTC"))
                g = self.groups[key]
                g.previous, g.previous_net = last.get(clock, (None, None))
                last[clock] = (g.chosen_mean if is_size else g.rate), g.bb100
        evidence_hands = db.execute(
            "SELECT count(DISTINCT hand) FROM members"
        ).fetchone()[0]
        self.coverage = (known, total, evidence_hands)
        return sorted(self.groups.values(), key=self.sort_key)

    def sort_key(self, g):
        if self.dimension in {"Trends", "Session", "Day", "Month"}:
            return g.key
        if self.dimension in {"Faced sizing", "Chosen sizing"}:
            return SIZE_ORDER.index(g.key)
        if self.dimension == "Stack depth":
            return DEPTH_ORDER.index(g.key)
        return -g.n, g.key

    def page(self, key, page=0, scope="All contributing", size=100):
        if key not in self.groups or page < 0:
            raise ValueError("Unknown group or page.")
        params = (key, size, page * size)
        if (
            scope == "All eligible bets"
            and self.metric.endswith("_SIZE")
            and self.dimension == "Chosen sizing"
        ):
            n = self.db.execute("SELECT count(*) FROM members").fetchone()[0]
            query = """SELECT r.*,m.ref,m.bucket=? AS hit FROM members m JOIN roster r ON r.hand=m.hand
                     ORDER BY r.stamp,r.hand,m.ref LIMIT ? OFFSET ?"""
            return list(self.db.execute(query, params)), n
        if scope == "All session hands" and self.dimension in {"Trends", "Session"}:
            # Date bounds may cut a session; retain exactly the report's time window.
            bounds = getattr(self, "dates", ("", "", "", ""))
            cond = "r.session=? AND (?='' OR r.day>=?) AND (?='' OR r.day<=?)"
            select = """FROM roster r JOIN seats s ON s.hand=r.hand AND s.name=r.hero
                      JOIN obs o ON o.hand=r.hand AND o.actor=r.hero AND (o.eligible & ?)<>0 WHERE """
            args = (BITS["NET"], key, *bounds)
            n = self.db.execute("SELECT count(*) " + select + cond, args).fetchone()[0]
            query = (
                "SELECT r.*,o.ref,(r.net IS NOT NULL) AS hit "
                + select
                + cond
                + " ORDER BY r.stamp,r.hand LIMIT ? OFFSET ?"
            )
            return list(self.db.execute(query, (*args, size, page * size))), n
        cond = "m.bucket=?"
        if scope == "Numerator only":
            cond += " AND m.hit"
        n = self.db.execute(
            "SELECT count(*) FROM members m WHERE " + cond, (key,)
        ).fetchone()[0]
        query = """SELECT r.*,m.ref,m.hit FROM members m JOIN roster r ON r.hand=m.hand WHERE """
        return list(
            self.db.execute(
                query + cond + " ORDER BY r.stamp,r.hand,m.ref LIMIT ? OFFSET ?", params
            )
        ), n

    def source(self, hand):
        r = self.db.execute(
            """SELECT i.path,o.locator,o.raw FROM origins o JOIN imports i ON i.id=o.source
            JOIN roster r ON r.hand=o.hand WHERE o.hand=? ORDER BY i.id,o.rowid LIMIT 1""",
            (hand,),
        ).fetchone()
        if not r:
            raise LookupError("The source record is missing.")
        return r["path"], r["locator"], zlib.decompress(r["raw"]).decode()


def source_span(text, ref):
    kind, number = ref.split()
    if kind == "Line":
        lines = text.splitlines(keepends=True)
        i = int(number) - 1
        if not 0 <= i < len(lines):
            return None
        begin = sum(map(len, lines[:i]))
        return begin, begin + len(lines[i].rstrip("\r\n"))
    if kind == "Seat":
        pattern = rf"^Seat {int(number)}: [^\r\n]+"
        if "<session" in text and "<game" in text:
            pattern = rf"""<player\b[^>]*\bseat\s*=\s*["']{int(number)}["'][^>]*>"""
    else:
        pattern = rf"""<action\b[^>]*\bno\s*=\s*["']{int(number)}["'][^>]*>"""
    match = re.search(pattern, text, re.MULTILINE)
    return match.span() if match else None


def pct(value):
    return f"{100 * value:.1f}%" if value is not None else "—"


def signed(value, unit=""):
    return f"{value:+.1f}{unit}" if value is not None else "—"


def summary_values(g, metric, dimension):
    size = metric.endswith("_SIZE")
    counts = (
        f"{g.x:,}/{g.n:,}" if metric != "NET" else f"{g.known_net:,}/{g.result_hands:,}"
    )
    rate = pct(g.rate)
    ci = wilson(g.rate, g.n) if g.n else None
    bounds = " – ".join(pct(v) for v in ci) if ci else "—"
    value = g.rate
    if size and dimension != "Chosen sizing":
        rate = pct(g.chosen_mean)
        value = g.chosen_mean
        counts = f"{g.n:,} bets"
        bounds = "—"
        if g.n > 1 and g.chosen_mean is not None:
            half = 1.96 * math.sqrt(
                max(0.0, g.chosen_sq_mean - g.chosen_mean**2) / (g.n - 1)
            )
            if half:
                bounds = f"{pct(max(0.0, g.chosen_mean - half))} – {pct(g.chosen_mean + half)}"
    if metric == "NET":
        rate = signed(g.bb100)
        bounds = (
            " – ".join(signed(v) for v in g.net_interval) if g.net_interval else "—"
        )
    delta = (
        100 * (value - g.previous)
        if value is not None and g.previous is not None
        else None
    )
    outcome_change = (
        g.bb100 - g.previous_net
        if g.bb100 is not None and g.previous_net is not None
        else None
    )
    return (
        g.key,
        counts,
        f"{g.hands:,}",
        rate,
        bounds,
        signed(outcome_change if metric == "NET" else delta),
        signed(g.net) if g.known_net else "—",
        signed(g.bb100),
        f"{g.known_net:,}/{g.result_hands:,}" if g.result_hands else "—",
        signed(outcome_change),
    )


class ExplorerWindow:
    """The SQL snapshot runs on its own worker so imports and window repaints stay responsive."""

    def __init__(self, parent, path, room, dimension):
        import tkinter as tk
        from tkinter import ttk

        from .ui import table

        self.tk, self.ttk = tk, ttk
        self.win = tk.Toplevel(parent)
        self.win.title(f"Reports · {room} · PLO Hero Leak Finder")
        self.win.geometry("1130x720")
        self.win.minsize(760, 520)
        self.closed = False
        self.requests, self.replies = Queue(), Queue()
        self.ticket = 0
        self.rendered = None
        self.path, self.room = path, room
        self.win.bind("<Destroy>", self.destroyed, add="+")
        outer = ttk.Frame(self.win, padding=14)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="Cash game reports", font=("Segoe UI", 16, "bold")).pack(
            side="left"
        )
        ttk.Label(head, text=f"Selected Hero · {room} · PLO4", padding=(14, 0)).pack(
            side="left"
        )
        initial = ALIASES.get(dimension, dimension)
        dim = "Overall" if initial == "Street" or initial not in DIMENSIONS else initial
        default = {
            "Opponent": "THREEBET",
            "Chosen sizing": "FLOP_SIZE",
            "Faced sizing": "RIVERF",
        }.get(dim, "VPIP")
        if initial == "Street":
            default = "FCB"
        self.street = tk.StringVar(
            value="Flop" if initial == "Street" else "All streets"
        )
        self.metric = tk.StringVar(value=DEFS[default].label)
        self.dimension = tk.StringVar(value=dim)
        self.start, self.end = tk.StringVar(), tk.StringVar()
        self.explain, self.message = (
            tk.StringVar(),
            tk.StringVar(value="Reading local library…"),
        )
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(12, 5))

        def combo(parent, var, values, width, callback, label):
            ttk.Label(parent, text=label).pack(side="left", padx=(0, 4))
            box = ttk.Combobox(
                parent, textvariable=var, values=values, state="readonly", width=width
            )
            box.pack(side="left", padx=(0, 10))
            box.bind("<<ComboboxSelected>>", callback)
            return box

        combo(
            controls,
            self.street,
            ("All streets", "Preflop", "Flop", "Turn", "River", "Showdown", "Results"),
            12,
            self.street_changed,
            "Street",
        )
        self.metric_box = combo(
            controls,
            self.metric,
            tuple(
                d.label
                for d in DEFS.values()
                if self.street.get() in ("All streets", d.street)
            ),
            36,
            self.metric_changed,
            "Statistic",
        )
        combo(controls, self.dimension, DIMENSIONS, 14, self.run_report, "Group by")
        dates = ttk.Frame(outer)
        dates.pack(fill="x", pady=(3, 8))
        for label, v in (("From", self.start), ("Through", self.end)):
            ttk.Label(dates, text=label).pack(side="left", padx=(0, 4))
            entry = ttk.Entry(dates, textvariable=v, width=12)
            entry.pack(side="left", padx=(0, 8))
            entry.bind("<Return>", self.run_report)
        ttk.Button(dates, text="Apply dates", command=self.run_report).pack(side="left")
        ttk.Label(dates, text="YYYY-MM-DD, inclusive", padding=(8, 0)).pack(side="left")
        self.review_button = ttk.Button(dates, text="View hands…", command=self.drill)
        self.review_button.pack(side="right")
        self.tree = table(
            outer,
            [
                ("name", "Group", 265),
                ("counts", "Actions / opportunities", 125),
                ("hands", "Hands", 60),
                ("rate", "Rate", 65),
                ("ci", "95% interval", 130),
                ("delta", "Δ previous (pp)", 109),
                ("net", "Net BB", 72),
                ("bb100", "BB/100", 65),
                ("verified", "Result coverage", 107),
                ("change", "Δ BB/100", 70),
            ],
        )
        self.tree.bind("<Double-1>", self.drill)
        self.tree.bind("<Return>", self.drill)
        self.table_help = tree_help(self.tree, self.column_help)
        self.statistic_help = HoverHelp(
            self.metric_box, lambda _: self.column_help("metric", "")
        )
        ttk.Label(
            outer, textvariable=self.message, wraplength=1050, justify="left"
        ).pack(anchor="w", pady=(6, 4))
        ttk.Separator(outer).pack(fill="x", pady=4)
        ttk.Label(
            outer, textvariable=self.explain, wraplength=1035, justify="left"
        ).pack(anchor="w", fill="x")
        self.worker = Thread(target=self.work_loop, daemon=True)
        self.worker.start()
        self.timer = self.win.after(50, self.poll)
        self.run_report()

    def destroyed(self, event):
        if event.widget == self.win:
            self.closed = True
            self.win.after_cancel(self.timer)
            self.requests.put(None)

    def work_loop(self):
        db = None
        try:
            db = connect(self.path)
            db.execute("BEGIN")
            reports = Reports(db, self.room)
            while True:
                job = self.requests.get()
                if job is None:
                    return
                ticket, kind, args = job
                if self.closed:
                    return
                try:
                    if kind == "report":
                        result = (reports.report(*args), reports.coverage)
                    elif kind == "page":
                        result = reports.page(*args)
                    else:
                        result = reports.source(*args)
                    self.replies.put((ticket, kind, result, None))
                except (
                    ValueError,
                    LookupError,
                    OSError,
                    sqlite3.Error,
                    zlib.error,
                ) as exc:
                    self.replies.put((ticket, kind, None, str(exc)))
        except (OSError, sqlite3.Error) as exc:
            self.replies.put((None, "startup", None, str(exc)))
        finally:
            if db is not None:
                db.close()

    def send(self, kind, *args):
        self.ticket += 1
        self.requests.put((self.ticket, kind, args))

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                ticket, kind, result, error = self.replies.get_nowait()
                if ticket is not None and ticket != self.ticket:
                    continue
                if error:
                    self.message.set(error)
                    if kind in {"page", "source"} and getattr(self, "viewer", None):
                        self.viewer.info.set(error)
                elif kind == "report":
                    self.show_report(*result)
                elif getattr(self, "viewer", None) and self.viewer.win.winfo_exists():
                    if kind == "page":
                        self.viewer.show_page(*result)
                    elif kind == "source":
                        self.viewer.show_source(*result)
        except Empty:
            pass
        self.timer = self.win.after(50, self.poll)

    def street_changed(self, _=None):
        options = [
            v for v in DEFS.values() if self.street.get() in ("All streets", v.street)
        ]
        self.metric_box.configure(values=[v.label for v in options])
        if self.metric.get() not in [v.label for v in options]:
            self.metric.set(options[0].label)
        self.metric_changed()

    def metric_changed(self, _=None):
        key = next(k for k, d in DEFS.items() if d.label == self.metric.get())
        if key.endswith("_SIZE"):
            self.dimension.set("Chosen sizing")
        self.run_report()

    def run_report(self, _=None):
        key = next(k for k, d in DEFS.items() if d.label == self.metric.get())
        d = DEFS[key]
        self.pending = (key, self.dimension.get())
        self.table_help.hide()
        self.statistic_help.hide()
        self.rendered = None
        self.explain.set(
            f"{d.label}\nNumerator · {d.numerator}\nDenominator · {d.denominator}\nFields · {d.fields}\nScope · {d.exclusions}"
        )
        self.tree.delete(*self.tree.get_children())
        self.review_button.state(["disabled"])
        self.message.set("Calculating observed statistics…")
        self.send(
            "report",
            key,
            self.dimension.get(),
            self.start.get().strip(),
            self.end.get().strip(),
        )

    def column_help(self, column, _row):
        key, dim = self.rendered or self.pending
        metric = DEFS[key]
        definition = f"{metric.label}\n{metric.numerator} / {metric.denominator}."
        mean_size = key.endswith("_SIZE") and dim != "Chosen sizing"
        if column == "ci":
            subject = (
                "This row estimates realized BB/100 from verified results."
                if key == "NET"
                else "This row estimates the mean bet size."
                if mean_size
                else "This row estimates the frequency using a Wilson interval. Repeated play can widen uncertainty."
            )
            return f"{subject}\n\n{CONFIDENCE_HELP}"
        if column in {"metric", "counts", "rate"}:
            return definition
        return {
            "name": "Session start and poker room. A gap of more than 45 minutes starts a new session."
            if dim in {"Trends", "Session"}
            else f"Results grouped by {dim.lower()}.",
            "hands": "Distinct hands that contributed to the selected statistic in this row.",
            "delta": "Change in BB/100 since the previous session in the same room."
            if key == "NET"
            else "Change from the previous session in the same room, in percentage points.",
            "net": "Total big blinds won or lost in hands with a verified result.",
            "verified": "Hands with a verified result divided by all hands in this result group.",
            "bb100": "Net big blinds per 100 hands with a verified result.",
            "change": "Change in BB/100 since the previous session in the same room.",
        }.get(column)

    def show_report(self, groups, coverage):
        self.rendered = self.pending
        key, dim = self.rendered
        self.groups = groups
        size = key.endswith("_SIZE")
        heading = (
            "BB/100"
            if key == "NET"
            else ("Mean % pot" if size and dim != "Chosen sizing" else "Frequency")
        )
        self.tree.heading("rate", text=heading)
        self.tree.heading(
            "counts",
            text="Verified / dealt"
            if key == "NET"
            else (
                "Bets / all bets"
                if size and dim == "Chosen sizing"
                else "Events / eligible"
            ),
        )
        self.tree.heading(
            "delta", text="Δ BB/100" if key == "NET" else "Δ previous (pp)"
        )
        cols = ["name", "counts", "hands", "rate", "ci"]
        if dim == "Trends":
            cols.append("delta")
        if key == "NET" or dim in ("Session", "Trends"):
            cols.extend(["net", "verified"])
        if dim in ("Session", "Trends") and key != "NET":
            cols.append("bb100")
        if dim == "Trends" and key != "NET":
            cols.append("change")
        self.tree.configure(displaycolumns=cols)
        for i, g in enumerate(groups):
            self.tree.insert("", "end", iid=str(i), values=summary_values(g, key, dim))
        known, total, eligible = coverage
        notes = [
            f"{eligible:,} distinct contributing hands · {total:,} selected Hero hands."
        ]
        if not groups:
            notes.append("No eligible decisions in this view.")
        if dim == "Hand class":
            notes.append(f"Known starting cards {known:,}/{total:,}. Tags overlap.")
        if dim in ("Trends", "Session"):
            notes.append(
                "Room-local sessions split after more than 45 minutes idle. Outcomes use all verified hands in the date window."
            )
        if dim == "Trends":
            notes.append(
                "Changes compare successive sessions in the same room. Different stakes and opponents can affect them."
            )
        if dim == "Opponent":
            notes.append(
                "Actions use their identified direct opponent. Hand-level rates and realized results use only hands dealt heads-up."
            )
        if dim == "Stack depth":
            notes.append(
                "Hand metrics use starting depth. Decision metrics use effective depth before the action."
            )
        if key == "FFC":
            notes.append(
                "The external denominator audit remains open. Descriptive use only."
            )
        if key == "NET":
            notes.append(
                "Realized BB/100 uses verified settlements. Interval is an approximate per-hand sampling range."
            )
        elif size and dim != "Chosen sizing":
            notes.append(
                "Mean bet / pre-bet pot. Range uses the standard error across observed bets."
            )
        else:
            notes.append(
                "Frequency intervals are descriptive Wilson 95% intervals. Repeat play can widen uncertainty."
            )
        self.message.set(" ".join(notes))
        if groups:
            self.tree.selection_set("0")
            self.review_button.state(["!disabled"])

    def drill(self, _=None):
        selected = self.tree.selection()
        if not selected or self.rendered is None:
            return
        self.viewer = SourceWindow(self, self.groups[int(selected[0])].key)


class SourceWindow:
    def __init__(self, owner, key):
        from tkinter.scrolledtext import ScrolledText

        from .ui import table

        self.owner, self.key = owner, key
        tk, ttk = owner.tk, owner.ttk
        self.win = tk.Toplevel(owner.win)
        self.win.transient(owner.win)
        self.win.grab_set()
        self.win.title("Contributing hands · " + key)
        self.win.geometry("1030x690")
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill="both", expand=True)
        metric, dim = owner.rendered
        ttk.Label(
            outer,
            text=f"{DEFS[metric].label}   ·   {key}",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=7)
        self.scope = tk.StringVar(value="All contributing")
        options = ["All contributing", "Numerator only"]
        if metric.endswith("_SIZE"):
            options = ["All contributing"]
        if metric.endswith("_SIZE") and dim == "Chosen sizing":
            options = ["Selected size only", "All eligible bets"]
            self.scope.set(options[0])
        if dim in {"Trends", "Session"}:
            options.append("All session hands")
        select = ttk.Combobox(
            bar, textvariable=self.scope, values=options, state="readonly", width=20
        )
        select.pack(side="left")
        select.bind("<<ComboboxSelected>>", lambda _: self.load(0))
        self.info = tk.StringVar()
        ttk.Label(bar, textvariable=self.info).pack(side="left", padx=10)
        self.next = ttk.Button(
            bar, text="Next", command=lambda: self.load(self.page + 1)
        )
        self.next.pack(side="right")
        self.prev = ttk.Button(
            bar, text="Previous", command=lambda: self.load(self.page - 1)
        )
        self.prev.pack(side="right", padx=5)
        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True)
        top, lower = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(top, weight=2)
        panes.add(lower, weight=3)
        self.tree = table(
            top,
            [
                ("room", "Room", 95),
                ("number", "Hand", 125),
                ("name", "Recorded Hero", 160),
                ("time", "Recorded time", 155),
                ("ref", "Evidence", 95),
                ("result", "In numerator", 100),
            ],
            6,
        )
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Double-1>", lambda _: self.open_table())
        self.tree.bind("<Return>", lambda _: self.open_table())
        self.origin = tk.StringVar()
        sourcebar = ttk.Frame(lower)
        sourcebar.pack(fill="x", pady=5)
        self.table_button = ttk.Button(
            sourcebar, text="Open Hero table", command=self.open_table
        )
        self.table_button.pack(side="right", padx=(5, 0))
        ttk.Label(sourcebar, textvariable=self.origin, wraplength=740).pack(side="left")
        self.raw = ScrolledText(lower, wrap="word", font=("Consolas", 10))
        self.raw.pack(fill="both", expand=True)
        self.raw.configure(state="disabled")
        self.raw.tag_configure("anchor", background="#ffe396", foreground="#243135")
        self.rows = []
        self.load(0)

    def load(self, page):
        self.page = max(0, page)
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        self.prev.state(["disabled"])
        self.table_button.state(["disabled"])
        self.next.state(["disabled"])
        self.info.set("Reading contributing hands…")
        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.configure(state="disabled")
        self.origin.set("")
        self.owner.send("page", self.key, self.page, self.scope.get())

    def show_page(self, rows, total):
        self.rows = rows
        for i, r in enumerate(rows):
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    r["room"],
                    r["number"],
                    r["hero"],
                    r["stamp"],
                    r["ref"],
                    "Yes" if r["hit"] else "No",
                ),
            )
        start = self.page * 100 + 1 if rows else 0
        self.info.set(f"{start}–{self.page * 100 + len(rows)} of {total:,}")
        self.prev.state(["!disabled" if self.page else "disabled"])
        self.next.state(
            ["!disabled" if self.page * 100 + len(rows) < total else "disabled"]
        )
        self.tree.heading(
            "result",
            text="Verified result"
            if self.scope.get() == "All session hands"
            or self.owner.rendered[0] == "NET"
            else "In numerator",
        )
        if rows:
            self.tree.selection_set("0")

    def select(self, _):
        if not self.tree.selection():
            return
        self.row = self.rows[int(self.tree.selection()[0])]
        self.table_button.state(
            [
                "!disabled"
                if self.row["ref"].startswith(("Action ", "Line "))
                else "disabled"
            ]
        )
        self.origin.set("Loading hand history…")
        self.owner.send("source", self.row["hand"])

    def open_table(self):
        if self.tree.selection():
            row = self.rows[int(self.tree.selection()[0])]
            if row["ref"].startswith(("Action ", "Line ")):
                from .table_ui import open_table

                return open_table(
                    self.win, self.owner.path, row["hand"], row["ref"], hero=row["hero"]
                )

    def show_source(self, path, locator, text):
        ref = self.row["ref"]
        self.origin.set(f"{path}   ·   {locator}   ·   {ref}")
        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", text)
        span = source_span(text, ref)
        if span:
            begin, end = span
            self.raw.tag_add("anchor", f"1.0+{begin}c", f"1.0+{end}c")
            self.raw.see(f"1.0+{begin}c")
        self.raw.configure(state="disabled")


def open_explorer(parent, dbpath, room, dimension="Position"):
    return ExplorerWindow(parent, dbpath, room, dimension)
