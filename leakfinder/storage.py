"""One local SQLite library. File imports commit atomically."""

import hashlib
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from .bet_sizing import bet_size_key
from .engine import METRICS, replay
from .importers import Excluded, Record, decode, sources
from .library_backup import backup_before_upgrade
from .statistics import observations, settlements, tags

LIBRARY_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY, path TEXT, digest TEXT, summary TEXT, UNIQUE(path,digest));
CREATE INDEX IF NOT EXISTS imports_digest ON imports(digest);
CREATE TABLE IF NOT EXISTS file_stamps(path TEXT PRIMARY KEY, stamp TEXT NOT NULL, source INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS hand_changes(hand INTEGER PRIMARY KEY,revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS hand_changes_revision ON hand_changes(revision);
CREATE TABLE IF NOT EXISTS import_delta(source INTEGER,hand INTEGER,revision INTEGER NOT NULL, PRIMARY KEY(source,hand)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS review_batch_bound(id INTEGER PRIMARY KEY CHECK(id=1),minimum INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_batch(source INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS import_recovery(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_guide_cache(room TEXT PRIMARY KEY, revision TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS review_comparison_cache(room TEXT PRIMARY KEY, revision TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS hands(id INTEGER PRIMARY KEY, room TEXT, number TEXT, day TEXT, hint TEXT, fingerprint TEXT, cards TEXT, conflict INTEGER DEFAULT 0, UNIQUE(room,number));
CREATE TABLE IF NOT EXISTS seats(hand INTEGER, name TEXT, PRIMARY KEY(hand,name));
CREATE TABLE IF NOT EXISTS origins(hand INTEGER, source INTEGER, locator TEXT, raw BLOB, PRIMARY KEY(source,locator));
CREATE INDEX IF NOT EXISTS origins_hand ON origins(hand);
CREATE TABLE IF NOT EXISTS decisions(hand INTEGER, node TEXT, actor TEXT, ref TEXT, action TEXT, success INTEGER, cell TEXT, PRIMARY KEY(hand,node,ref));
CREATE INDEX IF NOT EXISTS decision_node ON decisions(node,hand);
CREATE TABLE IF NOT EXISTS aliases(room TEXT,name TEXT, PRIMARY KEY(room,name));
CREATE TABLE IF NOT EXISTS rejected(source INTEGER,locator TEXT,reason TEXT,number TEXT);
CREATE INDEX IF NOT EXISTS rejected_import ON rejected(source);
CREATE TABLE IF NOT EXISTS excluded_records(digest BLOB PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS facts(hand INTEGER PRIMARY KEY,stamp TEXT,bb INTEGER,currency TEXT);
CREATE TABLE IF NOT EXISTS players(hand INTEGER,name TEXT,pos TEXT,tags TEXT,net REAL,PRIMARY KEY(hand,name));
CREATE TABLE IF NOT EXISTS obs(hand INTEGER,actor TEXT,ref TEXT,opp TEXT,depth INTEGER,chosen REAL,faced REAL,eligible INTEGER,hits INTEGER);
CREATE INDEX IF NOT EXISTS obs_hand_actor ON obs(hand,actor);
"""


def connect(path):
    db = sqlite3.connect(path, timeout=90)
    db.row_factory = sqlite3.Row
    return db


def initialize(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= LIBRARY_VERSION:
        raise ValueError("This library belongs to a different version.")
    if version == LIBRARY_VERSION:
        db.execute("PRAGMA journal_mode=WAL")
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == LIBRARY_VERSION:
            return
        if not 0 <= version < LIBRARY_VERSION:
            raise ValueError("This library belongs to a different version.")
        if version:
            backup_before_upgrade(db, version, LIBRARY_VERSION)
        # executescript() would commit the lock before creating these tables.
        for statement in SCHEMA.strip().splitlines():
            db.execute(statement)
        if version == 1:
            # These four derived tables can be rebuilt from the v1 original records.
            for table in ("decisions", "facts", "players", "obs"):
                db.execute(f"DELETE FROM {table}")
            for row in db.execute("""SELECT h.id,h.room,o.raw,o.locator FROM hands h JOIN origins o ON o.rowid=
                (SELECT min(rowid) FROM origins WHERE hand=h.id)"""):
                hand = decode(
                    Record(
                        row["room"],
                        zlib.decompress(row["raw"]).decode(),
                        row["locator"],
                    )
                )
                ds, events = replay(hand)
                store_facts(db, row["id"], hand, ds, events)
        elif version in (2, 3):
            # The exact fraction faced is already recorded alongside each
            # decision. Reclassify its context without replaying any histories.
            db.create_function("lf_bet_size_key", 1, bet_size_key, deterministic=True)
            db.execute("""UPDATE decisions AS d
                SET cell=json_set(d.cell,'$[10]',lf_bet_size_key(o.faced))
                FROM obs AS o
                WHERE o.hand=d.hand AND o.actor=d.actor AND o.ref=d.ref
                AND o.faced IS NOT NULL AND d.cell LIKE '%"bet-%'
                AND json_extract(d.cell,'$[10]') <> lf_bet_size_key(o.faced)""")
        if version in (1, 2, 3):
            db.execute("DELETE FROM review_comparison_cache")
        if version:
            # Only the previous selection needs reconstruction. Older releases
            # recorded a source for each copy, even when the hand was already known.
            db.execute("""INSERT OR IGNORE INTO import_delta
                SELECT o.source,o.hand,0 FROM review_batch b JOIN origins o ON o.source=b.source
                WHERE o.rowid=(SELECT min(p.rowid) FROM origins p WHERE p.hand=o.hand)""")
        if version == 4:
            from .analysis import migrate_v4_comparisons

            migrate_v4_comparisons(db)
        db.execute(f"PRAGMA user_version={LIBRARY_VERSION}")
    db.execute("PRAGMA journal_mode=WAL")


def store_facts(db, hid, h, decisions, events):
    net = settlements(h)
    db.execute(
        "INSERT INTO facts VALUES(?,?,?,?)",
        (hid, h.date.replace("/", "-"), h.bb, h.currency),
    )
    db.executemany(
        "INSERT INTO players VALUES(?,?,?,?,?)",
        (
            (hid, n, h.pos[n], json.dumps(tags(h.cards.get(n, []))), net.get(n))
            for n in h.stacks
        ),
    )
    db.executemany(
        "INSERT INTO obs VALUES(?,?,?,?,?,?,?,?,?)",
        ((hid, *row) for row in observations(h, decisions, events)),
    )
    db.executemany(
        "INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
        (
            (
                hid,
                d.node,
                d.event.player,
                d.event.ref,
                d.event.action,
                int(d.event.action == METRICS[d.node].success),
                d.context,
            )
            for d in decisions
        ),
    )


def file_digest(path):
    with open(path, "rb") as f:
        digest = hashlib.sha256()
        for chunk in iter(lambda: f.read(2**20), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _file_stamp(path):
    """Reuse a verified digest only while file identity and change time still match."""
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        changed = stat.st_ctime_ns
        if os.name == "nt":
            # Python's Windows st_ctime is the creation time. The file's actual
            # change time also changes when an editor restores the original mtime.
            import ctypes
            import msvcrt

            basic = (ctypes.c_int64 * 5)()
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
            if not ctypes.windll.kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)
            ):
                return None
            changed = basic[3] * 100
        if not stat.st_ino or not changed or changed % 1_000_000_000 == 0:
            return None  # Content hash fallback for low-resolution filesystems.
        return (
            f"{stat.st_dev}/{stat.st_ino}/{stat.st_size}/{stat.st_mtime_ns}/{changed}"
        )


def _remember_file(db, path, stamp, src):
    if stamp is not None and _file_stamp(path) == stamp:
        db.execute(
            "INSERT OR REPLACE INTO file_stamps VALUES(?,?,?)", (str(path), stamp, src)
        )


def data_revision(db):
    return db.execute("SELECT coalesce(max(revision),0) FROM hand_changes").fetchone()[
        0
    ]


def _existing_file(db, path, digest):
    # A plain text file renamed to .sql is interpreted as an HM2 dump.
    # Reuse the earlier result only if the same reader would handle it.
    hm2_extensions = {".sql", ".pghembk"}
    forced_hm2 = path.suffix.lower() in hm2_extensions
    for row in db.execute(
        """SELECT id,path,summary FROM imports WHERE digest=?
        ORDER BY (path=?) DESC,id""",
        (digest, str(path)),
    ):
        if (
            Path(row["path"]).suffix.lower() in hm2_extensions
        ) == forced_hm2 or zipfile.is_zipfile(path):
            return row
    return None


def _known_hand(db, record):
    number = record.expected
    if not number and record.room == "CoinPoker":
        header = re.match(r"CoinPoker Hand #(\d+):", record.raw)
        number = header[1] if header else ""
    if number:
        return db.execute(
            "SELECT * FROM hands WHERE room=? AND number=?", (record.room, number)
        ).fetchone()
    return None


def _same_record(db, hand_id, record):
    incoming = record.raw.rstrip("\r\n").encode()
    hm2 = record.locator.startswith("HM2")
    for locator, raw in db.execute(
        "SELECT locator,raw FROM origins WHERE hand=?", (hand_id,)
    ):
        if locator.startswith("HM2") != hm2:
            continue
        previous = zlib.decompress(raw).rstrip(b"\r\n")
        if previous == incoming:
            return True
        if record.locator.startswith("XML"):
            try:
                if _same_session_game(previous, incoming):
                    return True
            except (ET.ParseError, ValueError, OverflowError):
                # If the wrapper cannot be compared, let the regular parser
                # validate the complete record and retain its source evidence.
                continue
    return False


@lru_cache(maxsize=8)
def _session_signature(header):
    root = ET.fromstring(header.decode() + "</session>")
    general = root.find("general")
    if general is None:
        return header, None
    end = None
    # A growing native file rewrites these session totals for every old hand.
    # Preserve its original game XML (including card visibility and action refs).
    totals = {
        "gamecount",
        "bets",
        "wins",
        "chipsin",
        "chipsout",
        "awardpoints",
        "statuspoints",
        "ipoints",
    }
    for element in list(general):
        if element.tag in totals:
            general.remove(element)
    duration = general.find("duration")
    clock = (
        re.fullmatch(r"(\d{1,3}):([0-5]\d):([0-5]\d)", (duration.text or "").strip())
        if duration is not None
        else None
    )
    if clock:
        try:
            start = datetime.fromisoformat(
                general.findtext("startdate", "").replace("/", "-")
            )
        except ValueError:
            pass
        else:
            h, m, s = map(int, clock.groups())
            end = start + timedelta(hours=h, minutes=m, seconds=s)
            general.remove(duration)
    return ET.tostring(root), end


def _same_session_game(previous, incoming):
    parts = [re.split(rb"(?=<game\s)", raw, maxsplit=1) for raw in (previous, incoming)]
    if any(len(p) != 2 for p in parts):
        return False
    (old_header, old_game), (new_header, new_game) = parts
    if old_game != new_game:
        return False
    old, old_end = _session_signature(old_header)
    new, new_end = _session_signature(new_header)
    if old != new:
        return False
    if old_end == new_end:
        return True
    if old_end is None or new_end is None:
        return False
    # The earlier recorded bound is preferable for opponent-history chronology
    # if it actually covered the hand. Never discard a newly usable end time.
    game = ET.fromstring(old_game.decode().rsplit("</session>", 1)[0])
    try:
        dealt = datetime.fromisoformat(
            game.findtext("general/startdate", "").replace("/", "-")
        )
        return dealt <= old_end <= new_end
    except (ValueError, TypeError):
        return False


def _record_digest(record):
    metadata = (
        record.room,
        record.locator.startswith("HM2"),
        record.expected,
        record.skip,
    )
    digest = hashlib.sha256(json.dumps(metadata).encode() + b"\0")
    digest.update(record.raw.rstrip("\r\n").encode())
    return digest.digest()


def import_file(db, path, progress=lambda n: None):
    return import_file_with_source(db, path, progress)[0]


def import_revision(db):
    """A retry can recover hands without creating a new source."""
    source, revision = db.execute(
        "SELECT (SELECT max(id) FROM imports), (SELECT revision FROM import_recovery WHERE id=1)"
    ).fetchone()
    return source if revision is None else (source, revision)


def import_file_with_source(db, path, progress=lambda n: None, *, retry_source=None):
    """Return both the existing summary format and the exact committed source ID."""
    path = Path(path).resolve()
    stamp = _file_stamp(path)
    if stamp is not None and retry_source is None:
        unchanged = db.execute(
            """SELECT f.source FROM file_stamps f
            WHERE f.path=? AND f.stamp=? AND NOT EXISTS
            (SELECT 1 FROM rejected r WHERE r.source=f.source AND r.reason='Fold with no price')""",
            (str(path), stamp),
        ).fetchone()
        if unchanged:
            return {"already imported": 1}, unchanged[0]
    digest = file_digest(path)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if retry_source is None:
            existing = _existing_file(db, path, digest)
        else:
            # The sanitized demo can move with the installation. Require the
            # same file contents before reusing an earlier source's locators.
            existing = db.execute(
                "SELECT id,summary FROM imports WHERE id=? AND digest=?",
                (retry_source, digest),
            ).fetchone()
            if existing is None:
                raise ValueError("The original import does not match this file.")
        retry = None
        source_summary = Counter()
        if existing:
            src = existing["id"]
            retry = {
                row[0]
                for row in db.execute(
                    "SELECT locator FROM rejected WHERE source=? AND reason='Fold with no price'",
                    (src,),
                )
            }
            if not retry:
                _remember_file(db, path, stamp, src)
                return {"already imported": 1}, src
            source_summary.update(json.loads(existing["summary"]))
            source_summary["excluded"] -= len(retry)
        else:
            src = db.execute(
                "INSERT INTO imports(path,digest) VALUES(?,?)", (str(path), digest)
            ).lastrowid
        count = Counter()
        revision = data_revision(db) + 1
        for i, record in enumerate(sources(path), 1):
            if i % 500 == 0:
                progress(i)
            if retry is not None:
                if record.locator not in retry:
                    continue
                retry.remove(record.locator)
                db.execute(
                    "DELETE FROM rejected WHERE source=? AND locator=? AND reason='Fold with no price'",
                    (src, record.locator),
                )
            # Compare against the actual accepted source, including its carrier
            # rules. A reused game number alone cannot establish a duplicate.
            old = _known_hand(db, record)
            if old and not record.skip and _same_record(db, old["id"], record):
                count["known hands skipped"] += 1
                continue
            record_key = _record_digest(record) if record.raw else None
            if (
                retry is None
                and record_key is not None
                and db.execute(
                    "SELECT 1 FROM excluded_records WHERE digest=?", (record_key,)
                ).fetchone()
            ):
                count["previously excluded"] += 1
                continue
            try:
                hand = decode(record)
                decisions, events = replay(hand)
                fp = hand.fingerprint()
            except (Excluded, ET.ParseError, ValueError) as exc:
                reason = str(exc)
                count["excluded"] += 1
                db.execute(
                    "INSERT INTO rejected VALUES(?,?,?,?)",
                    (src, record.locator, reason, record.expected),
                )
                if record_key is not None and reason != "Fold with no price":
                    db.execute(
                        "INSERT OR IGNORE INTO excluded_records VALUES(?)",
                        (record_key,),
                    )
                continue
            changed = False
            if old:
                hid = old["id"]
                cards = json.loads(old["cards"])
                mismatch = old["fingerprint"] != fp or bool(
                    old["hint"] and hand.hint and old["hint"] != hand.hint
                )
                mismatch |= any(
                    name in cards and set(pocket) != set(cards[name])
                    for name, pocket in hand.cards.items()
                )
                merged = cards | hand.cards
                known = [card for pocket in merged.values() for card in pocket]
                mismatch |= len(known) != len(set(known))
                verified = settlements(hand)
                previous = {
                    r["name"]: r["net"]
                    for r in db.execute(
                        "SELECT name,net FROM players WHERE hand=? AND net IS NOT NULL",
                        (hid,),
                    )
                }
                mismatch |= bool(previous and verified and previous != verified)
                if mismatch or old["conflict"]:
                    changed = not old["conflict"]
                    db.execute("UPDATE hands SET conflict=1 WHERE id=?", (hid,))
                    count["conflicting copies"] += 1
                else:
                    # Even identical showdown cards may now be provably visible
                    # before a decision. Recheck this hand when adding an origin.
                    changed = True
                    if verified and not previous:
                        db.executemany(
                            "UPDATE players SET net=? WHERE hand=? AND name=?",
                            ((value, hid, n) for n, value in verified.items()),
                        )
                        db.executemany(
                            "UPDATE obs SET eligible=?,hits=? WHERE hand=? AND actor=? AND ref=?",
                            (
                                (eligible, hits, hid, n, ref)
                                for n, ref, _, _, _, _, eligible, hits in observations(
                                    hand, decisions, events
                                )
                                if ref.startswith("Seat ")
                            ),
                        )
                    cards.update(hand.cards)
                    db.execute(
                        "UPDATE hands SET cards=?,hint=? WHERE id=?",
                        (json.dumps(cards), old["hint"] or hand.hint, hid),
                    )
                    db.executemany(
                        "UPDATE players SET tags=? WHERE hand=? AND name=?",
                        (
                            (json.dumps(tags(hole)), hid, n)
                            for n, hole in hand.cards.items()
                        ),
                    )
                    count["duplicate origins"] += 1
            else:
                hid = db.execute(
                    "INSERT INTO hands(room,number,day,hint,fingerprint,cards) VALUES(?,?,?,?,?,?)",
                    (
                        hand.room,
                        hand.number,
                        hand.date[:10].replace("/", "-"),
                        hand.hint,
                        fp,
                        json.dumps(hand.cards),
                    ),
                ).lastrowid
                db.executemany(
                    "INSERT INTO seats VALUES(?,?)", ((hid, n) for n in hand.stacks)
                )
                store_facts(db, hid, hand, decisions, events)
                changed = True
                db.execute(
                    "INSERT INTO import_delta VALUES(?,?,?)", (src, hid, revision)
                )
                count["new hands"] += 1
            if changed:
                db.execute(
                    "INSERT OR REPLACE INTO hand_changes VALUES(?,?)", (hid, revision)
                )
            db.execute(
                "INSERT INTO origins VALUES(?,?,?,?)",
                (hid, src, record.locator, zlib.compress(record.raw.encode(), 1)),
            )
        if retry:
            raise ValueError(
                "Could not locate previously excluded hands in the original file."
            )
        if not count:
            raise ValueError("No hand histories found in the file.")
        if file_digest(path) != digest:
            raise ValueError(
                "The input changed during import. Select a completed session file."
            )
        _remember_file(db, path, stamp, src)
        if existing and count["excluded"] < sum(count.values()):
            db.execute(
                "INSERT INTO import_recovery VALUES(1,1) ON CONFLICT(id) "
                "DO UPDATE SET revision=revision+1"
            )
        db.execute(
            "UPDATE imports SET summary=? WHERE id=?",
            (json.dumps(source_summary + count), src),
        )
    return dict(count), src


def profile(db):
    return {
        room: [
            r[0]
            for r in db.execute(
                "SELECT name FROM aliases WHERE room=? ORDER BY name", (room,)
            )
        ]
        for room in ("CoinPoker", "iPoker")
    }


def set_profile(db, mapping):
    with db:
        db.execute("DELETE FROM aliases")
        db.executemany(
            "INSERT INTO aliases VALUES(?,?)",
            (
                (room, n)
                for room, names in mapping.items()
                for n in sorted(set(names))
                if n
            ),
        )


def cohort(db, room="Both", *, after_hand=0):
    db.execute("DROP TABLE IF EXISTS temp.cohort")
    db.execute("CREATE TEMP TABLE cohort(hand INTEGER PRIMARY KEY,hero TEXT)")
    db.execute(
        """INSERT INTO cohort
        SELECT h.id,min(s.name) FROM hands h JOIN seats s ON s.hand=h.id
        JOIN aliases a ON a.room=h.room AND a.name=s.name
        WHERE h.id>? AND h.conflict=0 AND (?='Both' OR h.room=?) GROUP BY h.id
        HAVING count(*)=1 AND (h.hint='' OR h.hint=min(s.name))""",
        (after_hand, room, room),
    )
