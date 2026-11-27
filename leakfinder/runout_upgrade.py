"""One-time repair of accepted source records in pre-v6 libraries."""

import zlib

from .importers import (
    Excluded,
    Record,
    decode,
    ipoker_amount_fields,
    ipoker_money_reader,
    ipoker_session,
    money,
)
from .statistics import BITS


def needs_repair(room, text, locator):
    from .storage import POSSIBLE_GROUP

    if room == "iPoker":
        root = ipoker_session(text, locator.startswith("HM2"))
        meta, game = root.find("general"), root.find("game")
        if POSSIBLE_GROUP.search(text):
            read = ipoker_money_reader(meta, game)
            try:
                if any(read(v) != money(v) for v in ipoker_amount_fields(meta, game)):
                    return True
            except Excluded:
                return True
        # Previously accepted action streams can fold each dealt seat at most once.
        if game.find("round/cards[@type='River']") is not None:
            return False
        return (
            len(game.findall("general/players/player"))
            - len(game.findall("round/action[@type='0']"))
            > 1
        )
    hand = decode(Record(room, text, locator))
    return (
        not hand.boards.get("River")
        and len(hand.seats) - sum(t.kind == "folds" for t in hand.tokens) > 1
    )


def repair_records(db):
    from .storage import _import_record, data_revision

    # Rejected records have no retained raw text. Retry ledger-related iPoker
    # exclusions once when the original source is next selected for import.
    db.execute("""INSERT OR IGNORE INTO amount_retry
        SELECT source,locator FROM rejected
        WHERE (locator LIKE 'XML game %' OR locator LIKE 'HM2 row %')
        AND reason IN ('Invalid or missing chip amount', 'Missing hand ID or invalid blinds',
        'Unsupported forced post', 'Extra or short blind', 'Invalid uncalled return',
        'Street missing a decision', 'Action order or inactive player',
        'Ambiguous iPoker all-in amount', 'All-in and stack disagree',
        'Amount exceeds stack or invalid raise target', 'Call or check does not match live price',
        'Invalid aggression or raise rights', 'Above pot-limit size', 'Incomplete action sequence',
        'No betting option against all-in opponents', 'Incomplete runout')""")
    affected = set()
    for hid, room, locator, raw in db.execute(
        "SELECT o.hand,h.room,o.locator,o.raw FROM origins o JOIN hands h ON h.id=o.hand"
    ):
        if hid not in affected and needs_repair(
            room, zlib.decompress(raw).decode(), locator
        ):
            affected.add(hid)
    if not affected:
        return
    revision = data_revision(db) + 1
    tables = {
        r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    for hid in sorted(affected):
        room, number, old_fp = db.execute(
            "SELECT room,number,fingerprint FROM hands WHERE id=?", (hid,)
        ).fetchone()
        prior_river = db.execute(
            "SELECT 1 FROM obs WHERE hand=? AND (eligible & ?)<>0 LIMIT 1",
            (hid, BITS["RIVERC"]),
        ).fetchone()
        origins = db.execute(
            "SELECT source,locator,raw FROM origins WHERE hand=? ORDER BY rowid", (hid,)
        ).fetchall()
        for table in ("decisions", "obs", "players", "facts", "seats", "origins"):
            db.execute(f"DELETE FROM {table} WHERE hand=?", (hid,))
        db.execute("DELETE FROM hands WHERE id=?", (hid,))
        if "luck_hand_v1" in tables:
            db.execute("DELETE FROM luck_hand_v1 WHERE hand=?", (hid,))
            db.execute("INSERT OR IGNORE INTO luck_dirty_v1 VALUES(?)", (hid,))
        db.execute("INSERT OR REPLACE INTO hand_changes VALUES(?,?)", (hid, revision))
        for src, locator, raw in origins:
            record = Record(
                room,
                zlib.decompress(raw).decode(),
                locator,
                number if room == "iPoker" else "",
            )
            _import_record(db, src, record, revision, retry=True, restored_id=hid)
        restored = db.execute(
            "SELECT fingerprint,conflict FROM hands WHERE id=?", (hid,)
        ).fetchone()
        if restored is None:
            db.execute("DELETE FROM import_delta WHERE hand=?", (hid,))
        # The incremental guide handles retained records. Reset a room only if a
        # deleted record or its former river evidence could survive that update.
        if restored is None or (
            prior_river and (old_fp != restored[0] or restored[1] or len(origins) > 1)
        ):
            db.execute(
                "DELETE FROM review_guide_cache WHERE room IN (?, 'Both')", (room,)
            )
        if restored is None:
            db.execute(
                "DELETE FROM review_comparison_cache WHERE room IN (?, 'Both')", (room,)
            )
