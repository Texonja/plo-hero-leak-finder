"""One local SQLite library. File imports commit atomically."""

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from pathlib import Path

from .engine import METRICS, replay
from .importers import Excluded, Record, decode, sources
from .statistics import observations, settlements, tags

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY, path TEXT, digest TEXT, summary TEXT, UNIQUE(path,digest));
CREATE TABLE IF NOT EXISTS review_batch(source INTEGER PRIMARY KEY);
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
    db.execute("PRAGMA journal_mode=WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2):
        raise ValueError("This library belongs to a different version.")
    db.executescript(SCHEMA)
    with db:
        if version == 1:
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
                db.execute("DELETE FROM decisions WHERE hand=?", (row["id"],))
                store_facts(db, row["id"], hand, ds, events)
        db.execute("PRAGMA user_version=2")


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


def import_file(db, path, progress=lambda n: None):
    return import_file_with_source(db, path, progress)[0]


def import_file_with_source(db, path, progress=lambda n: None):
    """Return both the existing summary format and the exact committed source ID."""
    path = Path(path).resolve()
    digest = file_digest(path)
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM imports WHERE path=? AND digest=?", (str(path), digest)
        ).fetchone()
        if existing:
            return {"already imported": 1}, existing["id"]
        src = db.execute(
            "INSERT INTO imports(path,digest) VALUES(?,?)", (str(path), digest)
        ).lastrowid
        count = Counter()
        for i, record in enumerate(sources(path), 1):
            if i % 500 == 0:
                progress(i)
            try:
                hand = decode(record)
                decisions, events = replay(hand)
            except (Excluded, ET.ParseError, ValueError) as exc:
                reason = str(exc)
                count["excluded"] += 1
                db.execute(
                    "INSERT INTO rejected VALUES(?,?,?,?)",
                    (src, record.locator, reason, record.expected),
                )
                continue
            fp = hand.fingerprint()
            old = db.execute(
                "SELECT * FROM hands WHERE room=? AND number=?",
                (hand.room, hand.number),
            ).fetchone()
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
                    db.execute("UPDATE hands SET conflict=1 WHERE id=?", (hid,))
                    count["conflicting copies"] += 1
                else:
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
                count["new hands"] += 1
            db.execute(
                "INSERT INTO origins VALUES(?,?,?,?)",
                (hid, src, record.locator, zlib.compress(record.raw.encode(), 1)),
            )
        if not count:
            raise ValueError("No hand histories found in the file.")
        if file_digest(path) != digest:
            raise ValueError(
                "The input changed during import. Select a completed session file."
            )
        db.execute("UPDATE imports SET summary=? WHERE id=?", (json.dumps(count), src))
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


def cohort(db, room="Both"):
    db.execute("DROP TABLE IF EXISTS temp.cohort")
    db.execute("CREATE TEMP TABLE cohort(hand INTEGER PRIMARY KEY,hero TEXT)")
    db.execute(
        """INSERT INTO cohort
        SELECT h.id,min(s.name) FROM hands h JOIN seats s ON s.hand=h.id
        JOIN aliases a ON a.room=h.room AND a.name=s.name
        WHERE h.conflict=0 AND (?='Both' OR h.room=?) GROUP BY h.id
        HAVING count(*)=1 AND (h.hint='' OR h.hint=min(s.name))""",
        (room, room),
    )
