"""Small filterable descriptors for records the regular hand importer rejected."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile

from .importers import money, sources
from .vendor.common import parse_header
from .vendor.hm2_backup import HM2BackupError

TABLE = """CREATE TABLE IF NOT EXISTS luck_reject_meta(
    source INTEGER, locator TEXT, original TEXT, reason TEXT, room TEXT, number TEXT,
    hero TEXT, day TEXT, currency TEXT, bb INTEGER, PRIMARY KEY(source,locator)) WITHOUT ROWID"""


def descriptor(record, reason):
    room, raw = record.room, record.raw
    number, hero, day, cur, bb = record.expected, "", "", "", None
    try:
        if room == "iPoker":
            if record.locator.startswith("HM2") and raw.rstrip().endswith("</game>"):
                raw += "</session>"
            root = ET.fromstring(raw)
            game = root.find("game")
            if game is not None:
                number = game.get("gamecode") or number
                day = game.findtext("general/startdate", "")
            hero = root.findtext("general/nickname", "").strip()
            cur = root.findtext("general/tablecurrency") or root.findtext(
                "general/currency", ""
            )
            blind = root.findtext("general/bigblind") or root.find(
                "game/round[@no='0']/action[@type='2']"
            )
            if isinstance(blind, ET.Element):
                blind = blind.get("sum")
            bb = money(blind) if blind else None
        elif room == "CoinPoker":
            meta = parse_header(raw.splitlines()[0])
            if meta:
                number, day = meta["hand_id"], meta["dt"]
                cur, bb = "USDT", money(str(meta["bb"])) if meta["bb"] else None
            found = re.search(r"^Dealt to (.+?) \[", raw, re.MULTILINE)
            hero = found[1] if found else ""
            if reason == "Straddle or multiple runouts":
                reason = (
                    "Multiple runouts"
                    if re.search(
                        r"\*\*\* (FIRST|SECOND|THIRD) |Hand was run (twice|three)",
                        raw,
                        re.IGNORECASE,
                    )
                    else "Straddle"
                )
    except (ET.ParseError, ValueError, IndexError, ArithmeticError):
        pass
    if bb is not None and not 0 < bb <= 2**63 - 1:
        bb = None
    return reason, room, number, hero, day[:10].replace("/", "-"), cur, bb


def remember(db, source, record, reason):
    db.execute(TABLE)
    db.execute(
        "INSERT OR REPLACE INTO luck_reject_meta VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source, record.locator, reason, *descriptor(record, reason)),
    )


def backfill(db, progress=lambda n: None):
    """Older databases retained only source/locator/reason for rejected records.
    Read an original only if its saved digest matches. Never guess its owner.
    """
    from .storage import file_digest

    db.execute(TABLE)
    missing = db.execute("""SELECT DISTINCT r.source,i.path,i.digest FROM rejected r
        JOIN imports i ON i.id=r.source LEFT JOIN luck_reject_meta m
        ON m.source=r.source AND m.locator=r.locator AND m.original=r.reason
        WHERE m.source IS NULL ORDER BY r.source""").fetchall()
    for index, (src, path, digest) in enumerate(missing):
        if index % 100 == 0:
            progress(index)
        try:
            path = Path(path)
            if not path.is_file() or file_digest(path) != digest:
                continue
            wanted = dict(
                db.execute("SELECT locator,reason FROM rejected WHERE source=?", (src,))
            )
            rows = []
            for record in sources(path):
                reason = wanted.get(record.locator)
                if reason:
                    rows.append(
                        (src, record.locator, reason, *descriptor(record, reason))
                    )
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO luck_reject_meta VALUES(?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
        except (OSError, BadZipFile, HM2BackupError, ET.ParseError, ValueError):
            continue


def counts(db, room, stake, start, end, alias):
    args = (
        room,
        room,
        start,
        start,
        end,
        end,
        None if stake is None else stake[0],
        *(stake or (None, None)),
        alias,
        alias,
    )
    scoped = dict(
        db.execute(
            """WITH unique_hands AS (
        SELECT m.reason, row_number() OVER (
            PARTITION BY m.room,coalesce(nullif(m.number,''),m.source || '/' || m.locator)
            ORDER BY m.source,m.locator) AS occurrence
        FROM rejected r JOIN luck_reject_meta m ON m.source=r.source AND m.locator=r.locator
        AND m.original=r.reason JOIN aliases a ON a.room=m.room AND a.name=m.hero
        WHERE (?='Both' OR m.room=?) AND (?='' OR m.day>=?) AND (?='' OR m.day<>''
        AND m.day<=?) AND (? IS NULL OR (m.currency=? AND m.bb=?)) AND (?='' OR m.hero=?)
        AND NOT EXISTS(SELECT 1 FROM hands h WHERE h.room=m.room AND h.number=m.number))
        SELECT reason,count(*) FROM unique_hands WHERE occurrence=1 GROUP BY reason""",
            args,
        )
    )
    unknown = dict(
        db.execute(
            """SELECT r.reason,count(*) FROM rejected r
        LEFT JOIN luck_reject_meta m ON m.source=r.source AND m.locator=r.locator AND m.original=r.reason
        WHERE m.source IS NULL OR (
            (m.hero='' OR EXISTS (SELECT 1 FROM aliases a WHERE a.room=m.room AND a.name=m.hero))
            AND (?='Both' OR m.room=?) AND (?='' OR m.hero='' OR m.hero=?)
            AND (?='' OR m.day='' OR m.day>=?) AND (?='' OR m.day='' OR m.day<=?)
            AND (? IS NULL OR ((m.currency='' OR m.currency=?) AND (m.bb IS NULL OR m.bb=?)))
            AND (m.hero='' OR ((?<>'' OR ?<>'') AND m.day='')
                 OR (? IS NOT NULL AND (m.bb IS NULL OR m.currency='')))
            AND NOT EXISTS (SELECT 1 FROM hands h WHERE h.room=m.room AND h.number=m.number))
        GROUP BY r.reason""",
            (
                room,
                room,
                alias,
                alias,
                start,
                start,
                end,
                end,
                None if stake is None else stake[0],
                *(stake or (None, None)),
                start,
                end,
                None if stake is None else stake[0],
            ),
        )
    )
    return scoped, unknown
