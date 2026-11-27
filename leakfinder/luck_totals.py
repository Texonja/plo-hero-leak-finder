"""Incremental histograms for the current report selection."""

import json
from collections import Counter

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS luck_view_v1(id INTEGER PRIMARY KEY, filters TEXT)",
    "CREATE TABLE IF NOT EXISTS luck_dirty_v1(hand INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS luck_roster_v1(hand INTEGER PRIMARY KEY, reason TEXT, data BLOB)",
    "CREATE INDEX IF NOT EXISTS luck_roster_reason ON luck_roster_v1(reason)",
    "CREATE TABLE IF NOT EXISTS luck_bins_v1(metric INTEGER,ways INTEGER,hit INTEGER,n INTEGER,"
    "PRIMARY KEY(metric,ways,hit)) WITHOUT ROWID",
)
OUTSIDE = "Outside selected Hero seats"


def _adjust(db, unpack, all_flops, sign):
    changes = Counter()
    for data, count in db.execute("""SELECT r.data,count(*) FROM luck_dirty_v1 d
        CROSS JOIN luck_roster_v1 r ON r.hand=d.hand WHERE r.reason='' GROUP BY r.data"""):
        for metric, ways, hit in unpack(data, all_flops):
            changes[metric, ways, hit] += sign * count
    db.executemany(
        """INSERT INTO luck_bins_v1 VALUES(?,?,?,?)
        ON CONFLICT(metric,ways,hit) DO UPDATE SET n=n+excluded.n""",
        ((*key, n) for key, n in changes.items()),
    )


def update(db, unpack, room, stake, start, end, alias, all_flops):
    names = [
        tuple(row)
        for row in db.execute("SELECT room,name FROM aliases ORDER BY room,name")
    ]
    key = json.dumps((names, room, stake, start, end, alias, all_flops))
    present = db.execute("SELECT filters FROM luck_view_v1 WHERE id=1").fetchone()
    if present is None or present[0] != key:
        db.execute("DELETE FROM luck_roster_v1")
        db.execute("DELETE FROM luck_bins_v1")
        db.execute("INSERT OR IGNORE INTO luck_dirty_v1 SELECT hand FROM luck_hand_v1")
        db.execute("INSERT OR REPLACE INTO luck_view_v1 VALUES(1,?)", (key,))
    if not db.execute("SELECT 1 FROM luck_dirty_v1 LIMIT 1").fetchone():
        return
    _adjust(db, unpack, all_flops, -1)
    db.execute(
        "DELETE FROM luck_roster_v1 WHERE hand IN (SELECT hand FROM luck_dirty_v1)"
    )
    # Count all active aliases first. Selecting one of two aliases at the same
    # table cannot disambiguate a hand that is already ambiguous in the library.
    db.execute(
        """INSERT INTO luck_roster_v1
        SELECT h.id, CASE WHEN count(a.name)=0 OR (?<>'' AND sum(a.name=?)=0) THEN ?
             WHEN count(a.name)>1 THEN 'Multiple Hero aliases seated'
             WHEN h.hint<>'' AND h.hint<>min(a.name) THEN 'Different dealt Hero'
             ELSE l.reason END, l.data
        FROM luck_dirty_v1 d CROSS JOIN luck_hand_v1 l ON l.hand=d.hand
        JOIN hands h ON h.id=l.hand JOIN facts f ON f.hand=h.id
        JOIN seats s ON s.hand=h.id LEFT JOIN aliases a ON a.room=h.room AND a.name=s.name
        WHERE (?='Both' OR h.room=?) AND (?='' OR h.day>=?) AND (?='' OR h.day<=?)
        AND (? IS NULL OR (f.currency=? AND f.bb=?)) GROUP BY h.id""",
        (
            alias,
            alias,
            OUTSIDE,
            room,
            room,
            start,
            start,
            end,
            end,
            None if stake is None else stake[0],
            *(stake or (None, None)),
        ),
    )
    _adjust(db, unpack, all_flops, 1)
    db.execute("DELETE FROM luck_dirty_v1")
    db.execute("DELETE FROM luck_bins_v1 WHERE n=0")
