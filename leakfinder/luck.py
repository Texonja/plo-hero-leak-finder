"""Descriptive PLO4 luck report. No decision-guide dependency."""

import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import NormalDist

from . import luck_exclusions, luck_totals
from .importers import Record, decode
from .luck_cards import DENOM, NAMES, PAIR_HITS, deal_counts, events
from .river_holdings import CARD_ID

# Five flop combination counts fit in 15 bits. Each one-card count fits in 6.
PACK = struct.Struct("<B5H6B")
MISSING = (65535,) * 5 + (255,) * 6
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS luck_hand_v1(hand INTEGER PRIMARY KEY,revision INTEGER, reason TEXT, data BLOB)",
    "CREATE TABLE IF NOT EXISTS luck_cursor_v1(id INTEGER PRIMARY KEY,revision INTEGER,hand INTEGER)",
)


def encode(hole, board, saw_flop):
    bits = 0 if saw_flop else 32
    values = list(MISSING)
    for i, ways, hit in events(hole, board):
        if i < 5:
            bits |= hit << i
        else:
            values[i - 5] = ways * 2 + hit
    return PACK.pack(bits, *values)


def unpack(payload, all_flops=False):
    bits, *values = PACK.unpack(payload)
    for i, ways in enumerate(deal_counts()):
        yield i, ways, (bits >> i) & 1
    for j, value in enumerate(values):
        if value != MISSING[j] and (j >= 5 or all_flops or not bits & 32):
            yield j + 5, value // 2, value % 2


def recorded(db, hid, room, hero, conflict):
    if conflict:
        return "Conflicting source copies", None
    if not hero:
        return "Missing dealt Hero identity", None
    for locator, raw in db.execute(
        "SELECT locator,raw FROM origins WHERE hand=? ORDER BY rowid", (hid,)
    ):
        text = zlib.decompress(raw).decode()
        hand = decode(Record(room, text, locator))
        board = max(hand.boards.values(), key=len, default=())
        if (
            len(board) < 5
            and len(
                set(hand.stacks) - {t.name for t in hand.tokens if t.kind == "folds"}
            )
            > 1
        ):
            return "Incomplete runout", None
        if room == "CoinPoker":
            streets = re.findall(
                r"^\*\*\* (FLOP|TURN|RIVER) \*\*\*", text, re.MULTILINE
            )
            if streets != ["FLOP", "TURN", "RIVER"][: len(streets)]:
                return "Repeated or inconsistent board streets", None
            summary = re.findall(r"^Board\s*\[([^\]]*)\]", text, re.MULTILINE)
            if len(summary) > 1 or (summary and summary[0].split() != list(board)):
                return "Inconsistent summary board", None
        if (
            hand.hint != hero
            or hero not in hand.cards
            or (room == "iPoker" and not hand.ipoker_hero_dealt)
        ):
            continue
        saw = not any(
            t.street == "Preflop" and t.kind == "folds" and t.name == hero
            for t in hand.tokens
        )
        return "", encode(
            tuple(CARD_ID[c] for c in hand.cards[hero]),
            tuple(CARD_ID[c] for c in board),
            saw,
        )
    return "Missing four dealt Hero cards", None


def refresh(db, progress=lambda n: None, batch=512, rejection_progress=lambda n: None):
    """Bootstrap legacy hands by ID. Thereafter consume revision/hand keysets.
    Commit each bounded batch with its cursor so an interrupted build resumes.
    """
    for stmt in SCHEMA + luck_totals.SCHEMA:
        db.execute(stmt)
    with db:
        db.execute("INSERT OR IGNORE INTO luck_cursor_v1 VALUES(1,0,0)")
    count = 0
    high = db.execute("SELECT coalesce(max(id),0) FROM hands").fetchone()[0]
    target = db.execute(
        "SELECT coalesce(max(revision),0) FROM hand_changes"
    ).fetchone()[0]
    while True:
        with db:
            db.execute("BEGIN IMMEDIATE")
            last = db.execute(
                "SELECT coalesce(max(hand),0) FROM luck_hand_v1"
            ).fetchone()[0]
            rev, key = db.execute(
                "SELECT revision,hand FROM luck_cursor_v1 WHERE id=1"
            ).fetchone()
            if last < high:
                rows = db.execute(
                    """SELECT h.id,h.room,h.hint,h.conflict,coalesce(c.revision,0)
                    FROM hands h LEFT JOIN hand_changes c ON c.hand=h.id
                    WHERE h.id>? AND h.id<=? ORDER BY h.id LIMIT ?""",
                    (last, high, batch),
                ).fetchall()
            else:
                changes = db.execute(
                    """SELECT revision,hand FROM hand_changes
                    WHERE (revision,hand)>(?,?) AND revision<=?
                    ORDER BY revision,hand LIMIT ?""",
                    (rev, key, target, batch),
                ).fetchall()
                if not changes:
                    break
                rows = []
                for revision, hid in changes:
                    row = db.execute(
                        """SELECT h.id,h.room,h.hint,h.conflict,? FROM hands h
                        LEFT JOIN luck_hand_v1 l ON l.hand=h.id
                        WHERE h.id=? AND (l.revision IS NULL OR l.revision<>?)""",
                        (revision, hid, revision),
                    ).fetchone()
                    if row:
                        rows.append(row)
                db.execute(
                    "UPDATE luck_cursor_v1 SET revision=?,hand=? WHERE id=1",
                    tuple(changes[-1]),
                )
            for hid, room, hint, conflict, revision in rows:
                try:
                    reason, payload = recorded(db, hid, room, hint, conflict)
                except (ValueError, KeyError, ET.ParseError, zlib.error) as exc:
                    reason, payload = str(exc), None
                db.execute(
                    "INSERT OR REPLACE INTO luck_hand_v1 VALUES(?,?,?,?)",
                    (hid, revision, reason, payload),
                )
            if db.execute("SELECT 1 FROM luck_view_v1 LIMIT 1").fetchone():
                db.executemany(
                    "INSERT OR IGNORE INTO luck_dirty_v1 VALUES(?)",
                    ((r[0],) for r in rows),
                )
            count += len(rows)
        progress(count)
    luck_exclusions.backfill(db, rejection_progress)
    return count


@dataclass
class Tally:
    n: int = 0
    hits: int = 0
    probs: Counter = field(default_factory=Counter)
    inconsistent: bool = False

    def add(self, ways, denominator, hits, count):
        self.n += count
        self.hits += hits
        self.probs[(ways, denominator)] += count
        self.inconsistent |= not (0 <= ways <= denominator and 0 <= hits <= count)
        self.inconsistent |= (ways == 0 and hits > 0) or (
            ways == denominator and hits < count
        )

    def row(self):
        expected = math.fsum((w / d) * n for (w, d), n in self.probs.items())
        sd = math.sqrt(
            max(
                0,
                math.fsum(
                    (w / d) * (1 - w / d) * n for (w, d), n in self.probs.items()
                ),
            )
        )
        diff = self.hits - expected
        z = diff / sd if sd else None
        check = self.inconsistent or (z is not None and abs(z) >= 4)
        small = expected < 30
        percentile = None
        if self.n and not self.inconsistent:
            if small:
                percentile = exact_percentile(self.probs, self.hits)
            elif sd:
                percentile = 100 * NormalDist().cdf(z)
            else:
                percentile = 50.0
        return {
            "n": self.n,
            "observed": self.hits,
            "expected": expected,
            "difference": diff,
            "pp": 100 * diff / self.n if self.n else None,
            "sd": sd,
            "z": z,
            "percentile": percentile,
            "method": "exact mid-CDF" if small else "normal",
            "status": (
                "No opportunities"
                if not self.n
                else "Small sample"
                if small
                else "Unusual"
                if z is not None and abs(z) >= 3
                else "Within range"
            ),
            "check_parser": check,
        }


def exact_percentile(probs, hits):
    """Poisson-binomial mid-CDF. Only coefficients up to the observation are needed."""
    mass = [1.0] + [0.0] * hits
    for (w, d), n in probs.items():
        p = w / d
        for _ in range(n):
            for k in range(hits, 0, -1):
                mass[k] = (1 - p) * mass[k] + p * mass[k - 1]
            mass[0] *= 1 - p
    return min(100.0, max(0.0, 100 * (math.fsum(mass[:-1]) + 0.5 * mass[-1])))


def add_payload(tallies, payload, count=1, all_flops=False):
    for i, ways, hit in unpack(payload, all_flops):
        add_bin(tallies, i, ways, hit, count)


def add_bin(tallies, i, ways, hit, count):
    t = tallies[i]
    t.add(ways, DENOM[i], count * hit, count)
    if i in (5, 6) and ways != PAIR_HITS[i - 5]:
        t.inconsistent = True
    if i in (10, 11, 13, 14) and ways == 0:
        t.inconsistent = True
    if i >= 10:
        combined = tallies[16 + (i - 10) % 3]
        combined.add(ways, DENOM[i], count * hit, count)
        combined.inconsistent |= t.inconsistent


def report(db, room="Both", stake=None, start="", end="", alias="", all_flops=False):
    start = date.fromisoformat(start).isoformat() if start else ""
    end = date.fromisoformat(end).isoformat() if end else ""
    if start and end and start > end:
        raise ValueError("Start date is after the end date.")
    # A selection, its additive histogram and replay examples share a snapshot.
    with db:
        db.execute("BEGIN IMMEDIATE")
        luck_totals.update(db, unpack, room, stake, start, end, alias, all_flops)
        return _summary(db, room, stake, start, end, alias, all_flops)


def _summary(db, room, stake, start, end, alias, all_flops):
    tallies = [Tally() for _ in NAMES]
    for i, ways, hit, n in db.execute("SELECT metric,ways,hit,n FROM luck_bins_v1"):
        add_bin(tallies, i, ways, hit, n)
    rows = {label: t.row() for label, t in zip(NAMES, tallies)}
    flagged = {i for i, row in enumerate(rows.values()) if row["check_parser"]}
    examples = defaultdict(list)
    if flagged:
        for hid, room_name, number, hero, payload in db.execute(
            """SELECT h.id,h.room,h.number,h.hint,r.data FROM luck_roster_v1 r
               JOIN hands h ON h.id=r.hand WHERE r.reason='' ORDER BY r.hand"""
        ):
            if all(len(examples[NAMES[i]]) == 10 for i in flagged):
                break
            seen = set()
            for j, ways, hit in unpack(payload, all_flops):
                ids = [j] if j < 10 else [j, 16 + (j - 10) % 3]
                for i in flagged.intersection(ids) - seen:
                    label = NAMES[i]
                    impossible = (ways == 0 and hit) or (ways == DENOM[j] and not hit)
                    bad_count = j in (5, 6) and ways != PAIR_HITS[j - 5]
                    if len(examples[label]) < 10 and (
                        impossible
                        or bad_count
                        or bool(hit) == (rows[label]["difference"] >= 0)
                    ):
                        examples[label].append((hid, room_name, number, hero))
                        seen.add(i)
    excluded = dict(
        db.execute(
            "SELECT reason,count(*) FROM luck_roster_v1 WHERE reason<>'' GROUP BY reason"
        )
    )
    rejected, unknown = luck_exclusions.counts(db, room, stake, start, end, alias)
    outside = excluded.pop(luck_totals.OUTSIDE, 0)
    return {
        "hands": tallies[0].n,
        "rows": rows,
        "excluded": excluded,
        "import_excluded": rejected,
        "outside_hero_selection": outside,
        "unattributed_import_records": unknown,
        "examples": dict(examples),
    }
