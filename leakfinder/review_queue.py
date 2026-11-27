"""Select actual Hero decisions for a short, source-linked post-session review."""

import json
import sqlite3
import zlib
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import NamedTuple

from .analysis import wilson
from .critique import action_critique, evidence_label, stack_context
from .early_postflop import next_draws
from .explorer import session_keys
from .importers import STREETS, Record
from .opponent_history import (
    MIN_CASES,
    OpponentHistory,
    apply_history,
    clock,
    finished_by,
    observations,
    river_opponent,
)
from .postflop_value import STRAIGHTS
from .replay_view import hero_decisions, record_stamp
from .statistics import BITS, tags
from .storage import cohort
from .strategy_guide import LABELS, advice_at, preflop_class
from .vendor.common import RANK_VALUE
from .vendor.primitives import best_plo_hand

REVIEW_PAGE_SIZE = 12
GUIDE_CACHE_VERSION = 16


@dataclass(frozen=True)
class ReviewItem:
    hand: int
    room: str
    number: str
    hero: str
    node: str
    street: str
    ref: str
    action: str
    title: str
    reason: str
    evidence: str
    status: str
    cards: tuple = ()
    position: str = ""
    played: str = ""
    suggested: str = ""
    explanation: str = ""
    difference: str = ""
    priority: int = 0
    close: bool = False
    pattern: str = ""
    repeat_count: int = 0
    review_reason: str = ""
    session: str = ""
    pot_bb: float = 0


@dataclass(frozen=True)
class GuidePattern:
    key: str
    differences: int
    sessions: int
    difference_sessions: int
    interval: tuple
    examples: tuple

    @property
    def total(self):
        return len(self.examples)


class StudySpot(NamedTuple):
    hand: int
    room: str
    number: str
    hero: str
    street: str
    ref: str
    action: str
    position: str
    title: str
    explanation: str
    pot_bb: float
    paid_bb: float
    cards: tuple
    large_river_call: bool = False

    def card(self):
        played = f"Played {self.action}"
        if self.paid_bb:
            played += f" ({self.paid_bb:g} BB put in)"
        return ReviewItem(
            self.hand,
            self.room,
            self.number,
            self.hero,
            "STUDY",
            self.street,
            self.ref,
            self.action,
            self.title,
            "",
            f"{played} · Pot before the decision: {self.pot_bb:g} BB",
            "Study",
            self.cards,
            self.position,
            explanation=self.explanation,
        )


@dataclass(frozen=True)
class ReviewQueue:
    scope: str
    hand_count: int
    library_hands: int
    items: tuple
    source_count: int
    guide_items: tuple = ()
    guide_choices: int = 0
    patterns: tuple = ()
    study: tuple = ()
    critiques: tuple = ()
    day: str = ""


@dataclass(frozen=True)
class GuideIndex:
    counts: dict
    items: tuple
    study: tuple = ()
    critiques: tuple = ()


def guide_revision(comparison_key):
    return json.dumps((GUIDE_CACHE_VERSION, comparison_key), separators=(",", ":"))


def _guide_key(revision):
    try:
        version, (watermark, aliases) = json.loads(revision)
    except (TypeError, ValueError):
        return None
    if (
        version == GUIDE_CACHE_VERSION
        and type(watermark) is int
        and watermark >= 0
        and isinstance(aliases, list)
        and all(
            isinstance(pair, list) and len(pair) == 2
            and all(isinstance(value, str) for value in pair)
            for pair in aliases
        )
    ):
        return watermark, aliases
    return None


def cached_guides(db, room, revision):
    row = db.execute(
        "SELECT payload FROM review_guide_cache WHERE room=? AND revision=?",
        (room, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        counts, entries, study, critiques = json.loads(zlib.decompress(row[0]))
        if set(counts) != set(STREETS):
            return None

        def restore(entries):
            return tuple(
                ReviewItem(**(i | {"cards": tuple(i["cards"])})) for i in entries
            )

        return GuideIndex(
            {
                street: tuple(tuple(row) for row in rows)
                for street, rows in counts.items()
            },
            restore(entries),
            tuple(StudySpot(*entry[:12], tuple(entry[12]), *entry[13:]) for entry in study),
            restore(critiques),
        )
    except (TypeError, ValueError, IndexError, KeyError, AttributeError, zlib.error):
        return None


def save_guides(db, room, revision, index):
    payload = zlib.compress(
        json.dumps(
            (
                index.counts,
                [vars(item) for item in index.items],
                index.study,
                [vars(item) for item in index.critiques],
            ),
            separators=(",", ":"),
        ).encode(),
        1,
    )
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        db.execute("PRAGMA busy_timeout=100")
        with db:
            db.execute(
                """INSERT INTO review_guide_cache VALUES(?,?,?)
                   ON CONFLICT(room) DO UPDATE SET revision=excluded.revision,
                                                   payload=excluded.payload""",
                (room, revision, payload),
            )
    except sqlite3.Error:
        # The guide remains available in memory if another window is writing.
        return
    finally:
        db.execute(f"PRAGMA busy_timeout={timeout}")


def _pattern(snap, advice, position):
    active = [seat for seat in snap.seats if seat.live]
    effective = (
        min(
            next(seat.stack + seat.committed for seat in active if seat.is_hero),
            max((s.stack + s.committed for s in active if not s.is_hero), default=0),
        )
        / snap.bb
    )
    ranks = Counter(RANK_VALUE[c[0]] for c in snap.board)
    texture = (
        sorted(ranks.values()),
        sorted(Counter(c[1] for c in snap.board).values()),
        max(len(window.intersection(ranks)) for window in STRAIGHTS),
    )
    positions = {seat.name: seat.position for seat in snap.seats}
    line = tuple(
        (a.street, positions[a.name], a.action)
        for a in snap.history
        if a.action in {"raise", "bet", "call", "check"}
    )
    if snap.board:
        family = [best_plo_hand(snap.hero_cards, snap.board)[0]]
        if len(snap.board) < 5:
            draws = next_draws(snap.hero_cards, snap.board)
            family.extend((bool(draws.flush), bool(draws.straight), bool(draws.nuts)))
    else:
        raises = [a for a in snap.history if a.action == "raise"]
        size = raises[0].raised_to / snap.bb if len(raises) == 1 else None
        family = preflop_class(snap.hero_cards, size)
    first_open = next(
        (
            a.raised_to / snap.bb
            for a in snap.history
            if a.street == "Preflop" and a.action == "raise"
        ),
        None,
    )
    context = {
        "room": snap.room,
        "street": snap.street,
        "currency": snap.currency,
        "blinds": (snap.sb, snap.bb),
        "table": len(snap.seats),
        "active": sorted(s.position for s in active),
        "hero": position,
        "spot": advice.title,
        "line": line,
        "family": family,
        "choices": advice.choices,
        "sizing": round(advice.size.amount / snap.pot, 2) if advice.size else None,
        "board": texture,
        "pocket": tags(snap.hero_cards) if not snap.board else None,
        "antes": sorted(a.amount for a in snap.history if a.action == "posts_ante"),
        "depth": sum(effective >= bound for bound in (40, 80, 150)),
        "open_band": sum(first_open > bound for bound in (2.5, 3.5, 5))
        if first_open is not None
        else None,
        "price": sum(
            snap.to_call > threshold * (snap.pot + snap.to_call)
            for threshold in (0.18, 0.24, 0.29, 0.35)
        )
        if snap.to_call
        else None,
    }
    return json.dumps(context, separators=(",", ":"))


def _sessions(db):
    rows = db.execute("""SELECT q.hand,h.room,f.stamp FROM cohort q
        JOIN hands h ON h.id=q.hand JOIN facts f ON f.hand=q.hand
        ORDER BY h.room,julianday(replace(f.stamp,'/','-')),q.hand""")
    return {hand: session for session, hand in session_keys(rows)}


def _profile_changes(db, room, previous_aliases):
    """Compare the old alias membership with the prepared current cohort."""
    return {
        hand for (hand,) in db.execute(
            """WITH old_aliases(room,name) AS (
                SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)
            ), old_cohort(hand,hero) AS MATERIALIZED (
                SELECT h.id,min(s.name) FROM hands h JOIN seats s ON s.hand=h.id
                JOIN old_aliases a ON a.room=h.room AND a.name=s.name
                WHERE h.conflict=0 AND (?='Both' OR h.room=?) GROUP BY h.id
                HAVING count(*)=1 AND (h.hint='' OR h.hint=min(s.name))
            )
            SELECT old.hand FROM old_cohort old
            LEFT JOIN cohort q ON q.hand=old.hand AND q.hero=old.hero WHERE q.hand IS NULL
            UNION
            SELECT q.hand FROM cohort q
            LEFT JOIN old_cohort old ON old.hand=q.hand AND old.hero=q.hero WHERE old.hand IS NULL""",
            (json.dumps(previous_aliases), room, room),
        )
    }


def _history_dependents(db, changed, previous, aliases):
    """Find cached river decisions that could now see different *prior* evidence."""
    river_hands = {
        entry.hand
        for rows in (previous.items, previous.study, previous.critiques)
        for entry in rows
        if entry.street == "River" and entry.hand not in changed
    }
    if not changed or not river_hands:
        return set()
    # Include now-conflicting hands: their evidence was visible before the import.
    # The original record provides the same session end bound used by observations().
    rows = db.execute(
        """WITH profile_aliases(room,name) AS (
            SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)
        )
        SELECT h.room,h.number,f.stamp,s.locator,s.raw,o.actor,o.opp
        FROM json_each(?) ids JOIN hands h ON h.id=ids.value
        JOIN facts f ON f.hand=h.id JOIN obs o ON o.hand=h.id
        JOIN origins s ON s.rowid=(SELECT min(rowid) FROM origins WHERE hand=h.id)
        WHERE (o.eligible & ?)<>0 AND EXISTS (
            SELECT 1 FROM profile_aliases a WHERE a.room=h.room AND a.name IN (o.actor,o.opp))""",
        (json.dumps(aliases), json.dumps(sorted(changed)), BITS["RIVERC"]),
    )
    cutoffs = {}
    for room, number, stamp, locator, raw, responder, bettor in rows:
        record = Record(room, zlib.decompress(raw).decode(), locator, number)
        end = finished_by(record, clock(record_stamp(record, stamp)))
        if end is not None:
            for name in (responder, bettor):
                key = room, name, end.tzinfo is not None
                cutoffs[key] = min(cutoffs.get(key, end), end)
    if not cutoffs:
        return set()

    affected = set()
    candidates = db.execute(
        """SELECT h.id,h.room,h.number,f.stamp,s.name
        FROM json_each(?) ids JOIN hands h ON h.id=ids.value JOIN facts f ON f.hand=h.id
        JOIN cohort c ON c.hand=h.id JOIN seats s ON s.hand=h.id AND s.name<>c.hero""",
        (json.dumps(sorted(river_hands)),),
    )
    for hid, room, number, stamp, name in candidates:
        if hid in affected:
            continue
        start = clock(stamp)
        aware = room == "CoinPoker" or (start is not None and start.tzinfo is not None)
        end = cutoffs.get((room, name, aware))
        if start is None or end is None:
            continue
        if room == "CoinPoker":
            # The facts table stores the local clock without its UTC offset.
            # Discard clearly earlier dates before reading the original zone.
            if start.date() < end.date() - timedelta(days=1):
                continue
            locator, raw = db.execute(
                "SELECT locator,raw FROM origins WHERE hand=? ORDER BY rowid LIMIT 1",
                (hid,),
            ).fetchone()
            record = Record(room, zlib.decompress(raw).decode(), locator, number)
            start = clock(record_stamp(record, stamp))
        if (
            start is not None
            and (start.tzinfo is None) == (end.tzinfo is None)
            and end < start
        ):
            affected.add(hid)
    return affected


def update_guides(db, room, revision, progress=None, *, stats=None, workload=None):
    """Extend a persisted index in this read snapshot. Save it after committing."""
    row = db.execute(
        "SELECT revision FROM review_guide_cache WHERE room=?", (room,)
    ).fetchone()
    previous = None
    current = _guide_key(revision)
    old = _guide_key(row[0]) if row else None
    if old and current and old[0] <= current[0]:
        previous = cached_guides(db, room, row[0])
    if stats is not None:
        stats.update(replayed=0, reused=0, history_updated=0)
    if previous is None:
        return _index_hands(db, room, progress, _sessions(db), stats=stats, workload=workload)
    changed = {
        r[0]
        for r in db.execute(
            """SELECT u.hand FROM hand_changes u
        JOIN hands h ON h.id=u.hand WHERE u.revision>? AND u.revision<=?
        AND (?='Both' OR h.room=?)""",
            (old[0], current[0], room, room),
        )
    }
    if old[1] != current[1]:
        changed.update(_profile_changes(db, room, old[1]))
    aliases = sorted(set(map(tuple, old[1] + current[1])))
    affected = _history_dependents(db, changed, previous, aliases)
    dirty = changed | affected
    if stats is not None:
        total = db.execute("""SELECT count(*) FROM cohort q JOIN hands h ON h.id=q.hand
            WHERE EXISTS (SELECT 1 FROM json_each(h.cards) c WHERE c.key=q.hero)""").fetchone()[
            0
        ]
        stats["history_updated"] = len(affected)
    if not dirty:
        if stats is not None:
            stats["reused"] = total
        return previous
    sessions = _sessions(db)
    fresh = _index_hands(db, room, progress, sessions, hands=dirty, stats=stats, workload=workload)
    if stats is not None:
        stats["reused"] = total - stats["replayed"]

    def merge(older, newer, key):
        kept = [entry for entry in older if entry.hand not in dirty]
        kept.extend(newer)
        return tuple(sorted(kept, key=key))

    counts = {
        street: tuple(
            sorted(
                [r for r in previous.counts[street] if r[0] not in dirty]
                + list(fresh.counts[street]),
                reverse=True,
            )
        )
        for street in STREETS
    }
    items = merge(previous.items, fresh.items, lambda item: -item.hand)
    # An older inserted hand can bridge two sessions even if no advice changes.
    items = tuple(
        replace(i, session=sessions.get(i.hand, ""))
        if i.session != sessions.get(i.hand, "")
        else i
        for i in items
    )
    study = merge(previous.study, fresh.study, lambda i: (-i.pot_bb, -i.hand))
    critiques = merge(previous.critiques, fresh.critiques, lambda i: -i.hand)
    return GuideIndex(counts, items, study, critiques)


def index_guides(db, room, progress=None, *, audit=None):
    """Replay each card-known Hero hand once across all streets."""
    return _index_hands(db, room, progress, _sessions(db), audit=audit)


def _large_river_call(snap, event):
    """Study filter for a turn call followed by a substantial heads-up river call."""
    if snap.street != "River" or event.action != "call" or snap.to_call < 10 * snap.bb:
        return False
    opponents = [s for s in snap.seats if s.live and not s.is_hero]
    if len(opponents) != 1:
        return False
    # The same opponent must have opened the betting on both streets. Other
    # prior checks are fine. A fold on the river changes the pricing context.
    opponent = opponents[0].name
    turn = [a for a in snap.history if a.street == "Turn"]
    river = [a for a in snap.history if a.street == "River"]
    if any(a.action == "fold" for a in river):
        return False
    turn_aggression = [a for a in turn if a.action in {"bet", "raise"}]
    river_aggression = [a for a in river if a.action in {"bet", "raise"}]
    if len(turn_aggression) != 1 or len(river_aggression) != 1:
        return False
    turn_bet, river_bet = turn_aggression[0], river_aggression[0]
    opening_pot = snap.pot - river_bet.amount
    return (
        turn_bet.action == river_bet.action == "bet"
        and turn_bet.name == river_bet.name == opponent
        and any(a.name == snap.hero and a.action == "call" for a in turn)
        and opening_pot > 0
        and 3 * snap.to_call >= 2 * opening_pot
    )


def _index_hands(db, room, progress, sessions, *, hands=None, audit=None, stats=None, workload=None):
    items = []
    study = []
    critiques = []
    covered = {street: [] for street in STREETS}
    history = None
    source = (
        "cohort q"
        if hands is None
        else "json_each(?) ids JOIN cohort q ON q.hand=ids.value"
    )
    eligible = f"""FROM {source}
        JOIN hands h ON h.id=q.hand
        WHERE EXISTS (SELECT 1 FROM json_each(h.cards) c WHERE c.key=q.hero)"""
    params = () if hands is None else (json.dumps(sorted(hands)),)
    if workload is not None:
        workload(db.execute(f"SELECT count(*) {eligible}", params).fetchone()[0])
    rows = db.execute(f"SELECT q.hand,q.hero {eligible} ORDER BY q.hand DESC", params)
    for index, (hid, hero) in enumerate(rows, 1):
        if stats is not None:
            stats["replayed"] += 1
        counts = Counter()
        for event, snap in hero_decisions(db, hid, hero, "All streets"):
            advice = advice_at(snap)
            if advice and river_opponent(snap):
                if history is None:
                    history = OpponentHistory(
                        observations(db, room, prepared=True, opponent_hands=hands, minimum=MIN_CASES)
                    )
                advice = apply_history(snap, advice, history)
            if audit is not None:
                audit(event, snap, advice)
            critique = action_critique(snap, event, advice)
            if critique:
                position = next(s.position for s in snap.seats if s.is_hero)
                played = event.action
                if event.amount:
                    played += f" ({event.amount / snap.bb:g} BB put in)"
                critiques.append(
                    ReviewItem(
                        hid,
                        snap.room,
                        snap.number,
                        hero,
                        critique.rule,
                        event.street,
                        event.ref,
                        event.action,
                        critique.title,
                        "",
                        stack_context(snap),
                        "Critique",
                        snap.hero_cards,
                        position,
                        played,
                        critique.alternative,
                        critique.reason,
                        pot_bb=snap.pot / snap.bb,
                        priority=critique.review_priority,
                    )
                )
            if advice is None:
                continue
            position = next(s.position for s in snap.seats if s.is_hero)
            if not advice.can_compare(snap):
                text = advice.why
                if advice.play:
                    text = f"Conditional line: {advice.play}. {text}"
                study.append(
                    StudySpot(
                        hid,
                        snap.room,
                        snap.number,
                        hero,
                        event.street,
                        event.ref,
                        event.action,
                        position,
                        advice.title,
                        text,
                        snap.pot / snap.bb,
                        event.amount / snap.bb,
                        snap.hero_cards,
                        _large_river_call(snap, event),
                    )
                )
                continue
            counts[event.street] += 1
            difference = advice.difference(event.action, event.amount)
            played = LABELS[event.action]
            if event.action == "raise":
                played = advice.raise_label
            if event.amount and event.action in {"bet", "call", "raise"}:
                pot_share = (
                    f" · {event.amount / snap.pot:.0%} pot"
                    if event.action == "bet"
                    else ""
                )
                played += f" ({event.amount / snap.bb:g} BB{pot_share})"
            title = advice.title
            if difference == "size":
                title += " · bet size"
            close = len(advice.choices) > 1
            items.append(
                ReviewItem(
                    hid,
                    snap.room,
                    snap.number,
                    hero,
                    "GUIDE",
                    event.street,
                    event.ref,
                    event.action,
                    title,
                    f"Played {played} · Suggested {advice.play}. {advice.why}"
                    if difference
                    else "",
                    "Study recommendation",
                    "Guide",
                    snap.hero_cards,
                    position,
                    played,
                    advice.play,
                    advice.why
                    if difference
                    else "The recorded action is within the guide's options for this decision.",
                    difference or "",
                    advice.priority,
                    close,
                    _pattern(snap, advice, position),
                    session=sessions.get(hid, ""),
                )
            )
        for street, count in counts.items():
            covered[street].append((hid, count))
        if progress and index % 500 == 0:
            progress(index)
    return GuideIndex(
        {street: tuple(rows) for street, rows in covered.items()},
        tuple(items),
        tuple(sorted(study, key=lambda s: -s.pot_bb)),
        tuple(critiques),
    )


def rank_critiques(index, street, selected):
    """Rotate rule families, with a documented benchmark first in each round."""
    groups = {}
    for item in index.critiques:
        if (street == "All streets" or item.street == street) and (
            selected is None or item.hand in selected
        ):
            groups.setdefault((item.street, item.node), []).append(item)
    rounds = []
    order = {"Solver reference": 0, "Guaranteed showdown share": 1, "River value review": 2}
    for family in groups.values():
        for rank, item in enumerate(
            sorted(family, key=lambda i: (-i.priority, -i.pot_bb, i.hand, i.ref))
        ):
            rounds.append(
                (
                    rank,
                    order.get(evidence_label(item.node), 3),
                    -item.pot_bb,
                    item.hand,
                    item.ref,
                    item,
                )
            )
    return tuple(row[-1] for row in sorted(rounds, key=lambda row: row[:-1]))


def rank_guides(index, street, hands=None):
    """Keep one representative per repeated pattern ahead of follow-up examples."""
    wanted = STREETS if street == "All streets" else (street,)
    covered = sum(
        count
        for label in wanted
        for hand, count in index.counts[label]
        if hands is None or hand in hands
    )
    items = [
        i
        for i in index.items
        if i.difference and i.street in wanted and (hands is None or i.hand in hands)
    ]
    patterns = {}
    for i in items:
        if i.pattern:
            patterns.setdefault(i.pattern, set()).add(i.hand)
    representatives, followups, seen = [], [], set()
    for i in sorted(items, key=lambda entry: -entry.priority):
        repeats = len(patterns.get(i.pattern, ()))
        if i.close:
            tier = 3
            why = "Close decision · The guide offers more than one action."
        elif i.difference == "size":
            tier = 2
            why = "Bet size difference · At least 25 percentage points of the pot."
        elif repeats > 1:
            tier = 0
            why = f"The guide differs in {repeats:,} comparable hands."
        else:
            tier = 1
            why = "Action difference · The guide favors one action here."
        item = replace(i, repeat_count=repeats, review_reason=why)
        entry = (tier, -repeats, -item.priority, item)
        if tier == 0 and item.pattern in seen:
            followups.append(entry)
        else:
            representatives.append(entry)
            if tier == 0:
                seen.add(item.pattern)
    return tuple(
        entry[3]
        for group in (representatives, followups)
        for entry in sorted(group, key=lambda item: item[:3])
    ), covered


def review_patterns(index, street, hands=None):
    """Use all guide-covered decisions in the selected scope for each denominator."""
    groups = {}
    for item in index.items:
        if (street == "All streets" or item.street == street) and (
            hands is None or item.hand in hands
        ):
            groups.setdefault(item.pattern, []).append(item)
    patterns = []
    for key, items in groups.items():
        different = [i for i in items if i.difference]
        if not key or not different:
            continue
        agreements = [i for i in items if not i.difference]
        actions = Counter((i.difference, i.action) for i in different)
        different.sort(key=lambda i: -actions[i.difference, i.action])
        examples = (*different[:1], *agreements[:1], *different[1:], *agreements[1:])

        def sessions(rows):
            return len(
                {
                    i.session
                    for i in rows
                    if i.session and not i.session.startswith("Unknown")
                }
            )

        x, n = len(different), len(items)
        patterns.append(
            GuidePattern(
                key, x, sessions(items), sessions(different), wilson(x / n, n), examples
            )
        )
    return tuple(
        sorted(
            patterns,
            key=lambda g: (
                g.difference_sessions < 2,
                g.differences < 2,
                -g.interval[0],
                -g.differences,
                g.key,
            ),
        )
    )


def has_saved_batch(db):
    return bool(db.execute("SELECT 1 FROM review_batch LIMIT 1").fetchone())


def remember_batch(db, source_ids, *, after_revision=None):
    """Remember the newly inserted hands from the last chooser operation with additions."""
    ids = sorted(set(source_ids))
    with db:
        sources = db.execute(
            """SELECT DISTINCT source FROM import_delta
               WHERE source IN (SELECT value FROM json_each(?)) AND revision>=? ORDER BY source""",
            (json.dumps(ids), 0 if after_revision is None else after_revision + 1),
        ).fetchall()
        if sources:
            db.execute("DELETE FROM review_batch")
            db.executemany("INSERT INTO review_batch(source) VALUES(?)", sources)
            db.execute(
                "INSERT OR REPLACE INTO review_batch_bound VALUES(1,?)",
                (0 if after_revision is None else after_revision + 1,),
            )
    return len(sources)


def latest_batch_rooms(db):
    return [
        r[0]
        for r in db.execute("""SELECT DISTINCT h.room FROM review_batch b
        JOIN import_delta d ON d.source=b.source
        JOIN hands h ON h.id=d.hand WHERE h.conflict=0
        AND d.revision>=coalesce((SELECT minimum FROM review_batch_bound WHERE id=1),0)""")
    ]


# First listed descriptive situation wins if several metrics describe one action.
# Hand results, showdown and the unresolved fold-to-c-bet denominator are ineligible.
def validate_review_day(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise ValueError("Enter a valid date in YYYY-MM-DD format.")


def prepare_hands(db, room, scope, *, day=""):
    cohort(db, room)
    library = db.execute("SELECT count(*) FROM cohort").fetchone()[0]
    source_count = db.execute("SELECT count(*) FROM review_batch").fetchone()[0]
    latest = scope == "Latest import" and source_count > 0
    db.execute("DROP TABLE IF EXISTS temp.review_hands")
    db.execute("CREATE TEMP TABLE review_hands(hand INTEGER PRIMARY KEY, hero TEXT)")
    if latest:
        db.execute("""INSERT OR IGNORE INTO review_hands
            SELECT c.hand,c.hero FROM review_batch b
            CROSS JOIN import_delta d ON d.source=b.source
            JOIN cohort c ON c.hand=d.hand
            WHERE d.revision>=coalesce((SELECT minimum FROM review_batch_bound WHERE id=1),0)""")
    elif scope == "Last 5 sessions":
        sessions = _sessions(db)
        recent = {}
        for session in sessions.values():
            if not session.startswith("Unknown"):
                recent.setdefault(session.split(" · ")[-1], {})[session] = None
        keep = {s for keys in recent.values() for s in list(keys)[-5:]}
        db.executemany(
            "INSERT INTO review_hands SELECT * FROM cohort WHERE hand=?",
            ((hid,) for hid, session in sessions.items() if session in keep),
        )
    elif scope == "By date":
        db.execute(
            """INSERT INTO review_hands
               SELECT c.hand,c.hero FROM cohort c JOIN hands h ON h.id=c.hand
               WHERE h.day=?""",
            (validate_review_day(day),),
        )
    else:
        db.execute("INSERT INTO review_hands SELECT * FROM cohort")
        return "All hands", library, library, source_count
    selected = db.execute("SELECT count(*) FROM review_hands").fetchone()[0]
    return scope, selected, library, source_count


def review_selection(db, room, scope, *, day=""):
    details = prepare_hands(db, room, scope, day=day)
    _, count, library, _ = details
    selected = (
        frozenset(r[0] for r in db.execute("SELECT hand FROM review_hands"))
        if room != "Both" or count != library
        else None
    )
    return (*details, selected)


def build_queue(
    db,
    room="Both",
    *,
    findings=None,
    limit=None,
    street="All streets",
    scope="Latest import",
    day="",
    guide_index=None,
    selection=None,
):
    """Filter Hero review items while retaining the room's population baseline."""
    db.execute("SAVEPOINT read_home_queue")
    try:
        return _read_queue(
            db, room, limit, street, scope, day, guide_index, selection
        )
    except BaseException:
        db.execute("ROLLBACK TO read_home_queue")
        raise
    finally:
        db.execute("RELEASE read_home_queue")


def _read_queue(db, room, limit, street, scope, day, guide_index, selection):
    if street not in ("All streets", *STREETS) or scope not in (
        "All hands",
        "Latest import",
        "Last 5 sessions",
        "By date",
    ):
        raise ValueError("Unknown review filter.")
    if limit is not None and limit < 0:
        raise ValueError("The review limit must be nonnegative.")
    day = validate_review_day(day) if scope == "By date" else ""
    actual_scope, count, library, source_count, selected = (
        selection or review_selection(db, room, scope, day=day)
    )
    if not count or limit == 0:
        return ReviewQueue(actual_scope, count, library, (), source_count, day=day)
    if guide_index is None:
        if selection is not None:
            cohort(db, room)
        guide_index = index_guides(db, room)
    guide, covered = rank_guides(guide_index, street, selected)
    critiques = rank_critiques(guide_index, street, selected)
    patterns = review_patterns(guide_index, street, selected)
    study = tuple(
        s
        for s in guide_index.study
        if (street == "All streets" or s.street == street)
        and (selected is None or s.hand in selected)
    )
    items = []
    used = set()
    for item in (*guide, *critiques):
        if item.hand not in used:
            items.append(item)
            used.add(item.hand)
        if limit is not None and len(items) == limit:
            break
    return ReviewQueue(
        actual_scope,
        count,
        library,
        tuple(items),
        source_count,
        guide,
        covered,
        patterns,
        study,
        critiques,
        day=day,
    )
