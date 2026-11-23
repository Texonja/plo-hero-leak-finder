"""Select public PLO4 BTN-vs-CO opportunities without using Hero's choice or outcome.

Run with Python 3.11+ from the extracted application:
  python pilots/btn_co/select_hands.py PATH/TO/release-corpus.sqlite3 --output hands.json
Pass --verify-originals COINPOKER_TXT IPOKER_HM2 to compare the selected source
records with the pinned public files. Only those two source digests are eligible.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import zlib
from collections import defaultdict
from contextlib import closing
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from leakfinder.engine import replay
from leakfinder.importers import SCALE, Record, decode, sources
from leakfinder.storage import cohort, file_digest

PUBLIC = {
    "CoinPoker": {
        "filename": "cash_example.txt",
        "sha256": "4f038959acbb00cd082e020f2c782bede4e8b105968881b2afce85ce7c023d32",
        "url": "https://github.com/Texonja/coinpoker-population-strength-analyzer/blob/6e50528b7219356c72b470f904e589c0fecbe138/cash_example.txt",
    },
    "iPoker": {
        "filename": "database.pghembk",
        "sha256": "7d8604883954cb6d0e5b643dc973ce9c9fe9106c64694fcec64c7c273c333200",
        "url": "https://github.com/Texonja/plo-multiaccount-detector/blob/2956860995a19a0db6e6d3676d7fc88486fd93e0/database.pghembk",
    },
}
POSITIONS = ("CO", "BTN", "SB", "BB")
QUOTAS = {
    ("CoinPoker", Fraction(4, 25)): 3,
    ("CoinPoker", Fraction(0)): 2,
    ("iPoker", Fraction(0)): 5,
}
MODEL_FIELDS = [
    "cards",
    "pot",
    "open_to",
    "stacks",
    "committed",
    "threebet_to",
    "min_raise_to",
    "ante_per_seat",
    "dead_antes",
    "sb_in_bb",
    "blinds_to_act",
]


def extract(row):
    """Replay the native record and independently account for every predecision chip."""
    room, number, hero, locator, zipped, reference = row
    raw = zlib.decompress(zipped).decode("utf-8")
    hand = decode(Record(room, raw, locator, number))
    if hand.hint != hero or len(hand.cards.get(hero, ())) != 4:
        return None
    decisions, events = replay(hand)
    (decision,) = [
        d.event
        for d in decisions
        if d.node == "THREEBET" and d.event.ref == reference and d.event.player == hero
    ]
    actions = events["Preflop"][: decision.idx - 1]
    if [(hand.pos[e.player], e.action) for e in actions] != [
        ("UTG", "fold"),
        ("HJ", "fold"),
        ("CO", "raise"),
    ]:
        raise ValueError(
            "Stored opportunity disagrees with its six-seat opening sequence."
        )
    if len(hand.seats) != 6 or hand.pos[hero] != "BTN" or not decision.can_raise:
        raise ValueError("Unexpected decision role or raise rights.")
    seats = {pos: name for name, pos in hand.pos.items()}
    ordered = [seats[p] for p in POSITIONS]
    if set(ordered) != set(decision.live) or decision.players_behind != 2:
        raise ValueError("Both blinds must still have a live decision.")
    stacks = [decision.remaining[n] for n in ordered]
    if min(stacks) < 30 * hand.bb or actions[-1].allin:
        return None

    # Independently reconstruct the pot from decoded, ordered tokens ending at
    # the decision reference. No later public cards, awards or actions enter it.
    paid = defaultdict(int)
    ante = defaultdict(int)
    for token in hand.tokens:
        if token.ref == reference:
            break
        if token.kind == "posts_ante":
            ante[token.name] += token.amount
        elif token.kind in ("posts_sb", "posts_bb"):
            paid[token.name] += token.amount
        elif token.kind == "raises":
            paid[token.name] = token.amount
        elif token.kind != "folds":
            raise ValueError("Unexpected predecision contribution.")
    else:
        raise ValueError("Missing raw BTN action.")
    if len({ante[n] for n in hand.stacks}) != 1:
        return None
    ante_per_seat = Fraction(ante[hero], hand.bb)
    stratum = (room, ante_per_seat)
    if stratum not in QUOTAS:
        return None
    committed = [paid[n] for n in ordered]
    open_to = paid[seats["CO"]]
    if (
        sum(paid.values()) + sum(ante.values()) != decision.pot
        or [hand.stacks[n] - paid[n] - ante[n] for n in ordered] != stacks
        or committed != [open_to, 0, hand.sb, hand.bb]
        or decision.owed != open_to
        or open_to < 2 * hand.bb
    ):
        raise ValueError("Independent token ledger disagrees with normalized replay.")
    pot_to = committed[1] + min(stacks[1], decision.pot + 2 * decision.owed)
    minimum_to = min(committed[1] + stacks[1], open_to + (open_to - hand.bb))
    if not open_to < minimum_to <= pot_to:
        raise ValueError("Invalid ordinary pot-limit raise-to.")
    digest = hashlib.sha256(f"BTN-vs-CO/v1|{room}|{number}".encode()).hexdigest()

    def bb(amount):
        return amount / hand.bb

    return stratum, {
        "room": room,
        "id": number,
        "cards": list(hand.cards[hero]),
        "pot": bb(decision.pot),
        "open_to": bb(open_to),
        "stacks": [bb(s) for s in stacks],
        "committed": [bb(s) for s in committed],
        "threebet_to": bb(pot_to),
        "min_raise_to": bb(minimum_to),
        "ante_per_seat": float(ante_per_seat),
        "dead_antes": bb(sum(ante.values())),
        "sb_in_bb": bb(hand.sb),
        "blinds_to_act": ["SB", "BB"],
        "currency": hand.currency,
        "sb_currency": hand.sb / SCALE,
        "bb_currency": hand.bb / SCALE,
        "action": decision.action,
        "actual_to": None
        if decision.action == "fold"
        else bb(committed[1] + decision.amount),
        "open_ref": actions[-1].ref,
        "action_ref": reference,
        "source_locator": locator,
        "selection_sha256": digest,
        "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source": raw,
    }


def select(db):
    cohort(db)
    buckets = defaultdict(list)
    query = """SELECT h.room,h.number,c.hero,o.locator,o.raw,d.ref
        FROM decisions d JOIN cohort c ON c.hand=d.hand AND c.hero=d.actor
        JOIN hands h ON h.id=d.hand JOIN origins o ON o.rowid=(
           SELECT min(r.rowid) FROM origins r JOIN imports i ON i.id=r.source
           WHERE r.hand=h.id AND i.digest=CASE h.room WHEN 'CoinPoker' THEN ? WHEN 'iPoker' THEN ? END)
        WHERE d.node='THREEBET' AND h.hint=c.hero AND json_extract(d.cell,'$[4]')=6
        AND json_extract(d.cell,'$[5]')='BTN' AND json_extract(d.cell,'$[6]')='CO'"""
    for row in db.execute(
        query, tuple(PUBLIC[r]["sha256"] for r in ("CoinPoker", "iPoker"))
    ):
        found = extract(row)
        if found:
            stratum, hand = found
            buckets[stratum].append(hand)
    hands = []
    strata = []
    for (room, ante), n in QUOTAS.items():
        rows = sorted(
            buckets[room, ante], key=lambda h: (h["selection_sha256"], h["id"])
        )
        if len(rows) < n or len({h["id"] for h in rows}) != len(rows):
            raise ValueError(
                f"Insufficient or duplicate public {room} records for ante {ante}."
            )
        hands.extend(rows[:n])
        strata.append(
            {
                "room": room,
                "ante_per_seat": float(ante),
                "eligible": len(rows),
                "selected": n,
            }
        )
    return {
        "schema_version": 1,
        "positions": list(POSITIONS),
        "model_fields": MODEL_FIELDS,
        "units": "Betting amounts are big blinds. sb_currency and bb_currency are in the named source currency.",
        "selection": "Six dealt PLO4 seats, UTG and HJ fold, CO full open, known four-card BTN Hero with legal raise and both blinds unacted. All four live seats have at least 30 BB remaining. Ascending SHA-256 of UTF-8 BTN-vs-CO/v1|room|id within the fixed room/ante quotas. Selection excludes no hand based on the BTN action or later play. No upper stack cap.",
        "field_notes": "Arrays always use CO, BTN, SB, BB order. stacks are chips behind immediately before BTN acts. committed excludes antes. dead_antes covers all six seats. threebet_to is the pot-limit raise-to capped by BTN's remaining stack. actual_to is the recorded final street contribution for a call or raise and null on a fold. action, actual_to, source and all identity/trace metadata are outside the model input allowlist.",
        "public_sources": PUBLIC,
        "strata": strata,
        "hands": hands,
    }


def verify_originals(report, paths):
    for room, path in zip(("CoinPoker", "iPoker"), paths):
        if file_digest(path) != PUBLIC[room]["sha256"]:
            raise ValueError(f"Source does not match the pinned {room} public file.")
        expected = {
            h["source_locator"]: h for h in report["hands"] if h["room"] == room
        }
        for record in sources(path):
            hand = expected.pop(record.locator, None)
            if hand:
                if record.raw != hand["source"] or decode(record).number != hand["id"]:
                    raise ValueError(
                        "A selected source differs from the original hand."
                    )
                if not expected:
                    break
        if expected:
            raise ValueError("Original source is missing selected records.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("hands.json")
    )
    parser.add_argument(
        "--verify-originals",
        nargs=2,
        type=Path,
        metavar=("COINPOKER_TXT", "IPOKER_HM2"),
    )
    args = parser.parse_args()
    with closing(
        sqlite3.connect(args.library.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        report = select(db)
    if args.verify_originals:
        verify_originals(report, args.verify_originals)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(report["strata"])


if __name__ == "__main__":
    main()
