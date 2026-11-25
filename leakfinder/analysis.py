"""Observed frequencies standardized to Hero's room and public-context mix."""

import json
import sqlite3
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from math import sqrt
from statistics import NormalDist

from .bet_sizing import MATCH_LABELS
from .engine import METRICS
from .storage import cohort

Z95 = NormalDist().inv_cdf(0.975)
# Two predeclared metrics, each inspected in two rooms and a combined view.
Z_FAMILY = NormalDist().inv_cdf(1 - 0.05 / (2 * 6))


def wilson(p, n, z=Z95):
    if not n:
        return None
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0, center - half), min(1, center + half)


@dataclass
class Cell:
    h: int = 0
    hx: int = 0
    p: int = 0
    px: int = 0
    days: dict = field(default_factory=dict)


@dataclass
class Finding:
    node: str
    cells: dict
    selected: set
    hero: int
    pool: int
    counts: tuple
    high_coverage: bool
    gap: float | None = None
    expected: float | None = None
    interval: tuple | None = None
    adjusted: tuple | None = None
    status: str = "Exploratory"
    note: str = ""
    blocks: tuple = (0, 0)


def cached_comparison(db, room, revision):
    row = db.execute(
        "SELECT payload FROM review_comparison_cache WHERE room=? AND revision=?",
        (room, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        rows, eligible, total = json.loads(zlib.decompress(row[0]))
        result = {}
        for data in rows:
            data["cells"] = {k: Cell(**v) for k, v in data["cells"].items()}
            data["selected"] = set(data["selected"])
            for k in ("counts", "blocks", "interval", "adjusted"):
                if data[k] is not None:
                    data[k] = tuple(data[k])
            result[data["node"]] = Finding(**data)
        return result, eligible, total
    except (ValueError, TypeError, KeyError, zlib.error):
        return None


def comparison_revision(room, generation, last_hand, aliases):
    return json.dumps((4, room, generation, last_hand, aliases), separators=(",", ":"))


def migrate_v4_comparisons(db):
    """Reuse only comparison caches that matched the pre-upgrade library."""
    from .storage import import_revision

    source = import_revision(db)
    aliases = db.execute("SELECT room,name FROM aliases ORDER BY room,name").fetchall()
    aliases = [tuple(row) for row in aliases]
    last_hand = db.execute("SELECT coalesce(max(id),0) FROM hands").fetchone()[0]
    for room, revision in db.execute(
        "SELECT room,revision FROM review_comparison_cache"
    ).fetchall():
        if revision == json.dumps((3, (room, source, aliases)), separators=(",", ":")):
            db.execute(
                "UPDATE review_comparison_cache SET revision=? WHERE room=?",
                (comparison_revision(room, 0, last_hand, aliases), room),
            )


def save_comparison(db, room, revision, findings, eligible, total):
    rows = [
        vars(f)
        | {
            "cells": {k: vars(c) for k, c in f.cells.items()},
            "selected": sorted(f.selected),
        }
        for f in findings.values()
    ]
    blob = zlib.compress(
        json.dumps((rows, eligible, total), separators=(",", ":")).encode(), 1
    )
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        db.execute("PRAGMA busy_timeout=100")
        with db:
            db.execute(
                "INSERT OR REPLACE INTO review_comparison_cache VALUES(?,?,?)",
                (room, revision, blob),
            )
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode & 255 not in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            raise
    finally:
        db.execute(f"PRAGMA busy_timeout={timeout}")


def summarize(node, cells, room):
    nh = sum(c.h for c in cells.values())
    np = sum(c.p for c in cells.values())
    strong = {key for key, c in cells.items() if c.h and c.p >= 20}
    strong_h = sum(cells[key].h for key in strong)
    robust = bool(nh and strong_h / nh >= 0.8)
    selected = strong if robust else {key for key, c in cells.items() if c.h and c.p}
    used = [cells[key] for key in sorted(selected)]
    H, X, P, Y = (sum(getattr(c, k) for c in used) for k in ("h", "hx", "p", "px"))
    f = Finding(node, cells, selected, nh, np, (X, H, Y, P), robust)
    if not H:
        f.note = "No matching pool context. Raw opportunities are available for review."
        return f
    expected = sum(c.h * c.px / c.p for c in used) / H
    hero = X / H
    gap = hero - expected
    f.expected, f.gap = expected, gap
    hs, ps = set(), set()
    infl = defaultdict(float)
    room_gaps = defaultdict(float)
    for key in sorted(selected):
        c = cells[key]
        pc = c.px / c.p
        room_gaps[json.loads(key)[0]] += c.h * (c.hx / c.h - pc)
        for day, (hx, h, px, p) in c.days.items():
            if h:
                hs.add(day)
            if p:
                ps.add(day)
            infl[day] += (hx - h * (pc + gap) - c.h / c.p * (px - p * pc)) / H
    f.blocks = (len(hs), len(ps))
    day_count = len(infl)
    se = (
        sqrt(sum(x * x for x in infl.values()) * day_count / (day_count - 1))
        if day_count > 1
        else 0
    )
    effective_pool = H * H / sum(c.h * c.h / c.p for c in used)

    def interval(z):
        hl, hu = wilson(hero, H, z)
        pl, pu = wilson(expected, effective_pool, z)
        return (max(-1, min(hl - pu, gap - z * se)), min(1, max(hu - pl, gap + z * se)))

    f.interval = interval(Z95)
    f.adjusted = interval(Z_FAMILY)
    reasons = []
    if not METRICS[node].confirmatory:
        reasons.append("exploratory node")
    if not robust:
        reasons.append("sparse matched contexts")
    if H < 50 or P < 200:
        reasons.append("small matched sample")
    if min(f.blocks) < 20 or any(not day or len(day) != 10 for day in infl):
        reasons.append("limited day coverage")
    if room == "Both" and (
        len(room_gaps) != 2 or min(room_gaps.values()) < 0 < max(room_gaps.values())
    ):
        reasons.append("one room represented or room directions differ")
    if not (f.adjusted[0] > 0.05 or f.adjusted[1] < -0.05):
        reasons.append("adjusted interval crosses the review margin")
    f.status = "Exploratory" if reasons else "Supported"
    f.note = (
        ", ".join(reasons)
        if reasons
        else "Audited decision; matched data support a frequency difference."
    )
    return f


def _add_cohort(db, by_node, room, after_hand):
    cohort(db, room, after_hand=after_hand)
    for r in db.execute("""SELECT d.node,d.cell,h.day,d.actor=c.hero AS ishero,
            sum(d.success) AS x,count(*) AS n FROM cohort c
            JOIN hands h ON h.id=c.hand JOIN decisions d ON d.hand=h.id
            GROUP BY d.node,d.cell,h.day,ishero"""):
        cell = by_node[r["node"]].setdefault(r["cell"], Cell())
        day = cell.days.setdefault(r["day"], [0, 0, 0, 0])
        if r["ishero"]:
            cell.h += r["n"]
            cell.hx += r["x"]
            day[0] += r["x"]
            day[1] += r["n"]
        else:
            cell.p += r["n"]
            cell.px += r["x"]
            day[2] += r["x"]
            day[3] += r["n"]
    eligible = db.execute("SELECT count(*) FROM cohort").fetchone()[0]
    total = db.execute(
        "SELECT count(*) FROM hands WHERE id>? AND (?='Both' OR room=?)",
        (after_hand, room, room),
    ).fetchone()[0]
    return eligible, total


def _findings(by_node, room):
    results = [
        summarize(node, by_node[node], room) for node in METRICS if node in by_node
    ]
    results.sort(
        key=lambda f: (
            f.status != "Supported",
            -min(abs(v) for v in f.adjusted)
            if f.status == "Supported"
            else list(METRICS).index(f.node),
        )
    )
    return results


def analyze(db, room="Both"):
    by_node = defaultdict(dict)
    eligible, total = _add_cohort(db, by_node, room, 0)
    return _findings(by_node, room), eligible, total


def update_comparison(db, room, revision):
    """Extend population cells for new hands, including new actions on an existing day."""
    row = db.execute(
        "SELECT revision FROM review_comparison_cache WHERE room=?", (room,)
    ).fetchone()
    if row:
        try:
            version, prior_room, generation, last, aliases = json.loads(row[0])
            _, _, current, high, current_aliases = json.loads(revision)
            append = (
                version == 4
                and prior_room == room
                and aliases == current_aliases
                and 0 <= generation <= current
                and 0 <= last <= high
            )
        except (ValueError, TypeError):
            append = False
        if append:
            # A conflict, recovered Hero identity, or enriched copy can change an
            # earlier contribution. Rebuild the comparison in that case.
            amended = db.execute(
                """SELECT 1 FROM hand_changes c JOIN hands h ON h.id=c.hand
                   WHERE c.revision>? AND c.hand<=? AND (?='Both' OR h.room=?) LIMIT 1""",
                (generation, last, room, room),
            ).fetchone()
            prior = None if amended else cached_comparison(db, room, row[0])
            if prior is not None:
                findings, eligible, total = prior
                cells = defaultdict(dict, {n: f.cells for n, f in findings.items()})
                new_eligible, new_total = _add_cohort(db, cells, room, last)
                return (
                    _findings(cells, room),
                    eligible + new_eligible,
                    total + new_total,
                )
    return analyze(db, room)


def describe(key):
    room, currency, bb, ante, n, pos, opp, depth, pot, texture, size, rights, behind = (
        json.loads(key)
    )
    parts = [
        room,
        f"{currency} {bb / 1e6:g} BB",
        f"{n}-handed",
        pos + (f" vs {opp}" if opp else ""),
        ("<40", "40–<80", "80–<150", "150+")[depth] + " BB",
        pot,
        texture,
        MATCH_LABELS.get(size, size),
    ]
    if ante:
        parts.append(f"antes {ante / 1e6:g}")
    if pot == "pre":
        parts.append(f"{behind} to act")
    if not rights:
        parts.append("raising closed")
    return " · ".join(filter(None, parts))


def evidence(db, f, side, scope, page=0, cell_key="", limit=100):
    # Same keys and cohort as the aggregate. A temporary table avoids a parameter per cell.
    db.execute("CREATE TEMP TABLE IF NOT EXISTS matched(cell TEXT PRIMARY KEY)")
    db.execute("DELETE FROM matched")
    db.executemany("INSERT INTO matched VALUES(?)", ((k,) for k in f.selected))
    where = """FROM decisions d JOIN cohort c ON d.hand=c.hand JOIN hands h ON h.id=d.hand
        WHERE d.node=? AND ((d.actor=c.hero)=?) AND (?='All opportunities' OR d.cell IN (SELECT cell FROM matched))
        AND (?='' OR d.cell=?)"""
    args = (f.node, int(side == "Hero"), scope, cell_key, cell_key)
    count = db.execute(
        "SELECT count(*),count(DISTINCT d.hand) " + where, args
    ).fetchone()
    rows = db.execute(
        "SELECT d.*,h.room,h.number,h.day,c.hero "
        + where
        + " ORDER BY h.day,h.id,d.ref LIMIT ? OFFSET ?",
        (*args, limit, page * limit),
    ).fetchall()
    return rows, tuple(count)
