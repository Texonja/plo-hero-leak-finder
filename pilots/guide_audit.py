"""Replay the active Hero aliases and check the delivered recommendations offline."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from leakfinder.demo import load_demo
from leakfinder.review_queue import index_guides
from leakfinder.storage import cohort, connect, initialize


def inconsistencies(event, snap, advice):
    board_lengths = {"Preflop": 0, "Flop": 3, "Turn": 4, "River": 5}
    if (event.ref, event.street, event.player, event.pot) != (
        snap.ref,
        snap.street,
        snap.hero,
        snap.pot,
    ):
        yield "source decision or pot mismatch"
    cards = (*snap.hero_cards, *snap.board)
    if len(snap.board) != board_lengths[snap.street] or len(cards) != len(set(cards)):
        yield "invalid cards at decision"
    if any(a.ref == event.ref for a in snap.history):
        yield "current action in decision history"
    if not advice:
        return
    choices = set(advice.choices)
    legal = {"fold", "call"} if snap.to_call else {"check"}
    if snap.street != "Preflop" and not snap.to_call:
        legal.add("bet")
    if snap.can_raise and (snap.to_call or snap.street == "Preflop"):
        legal.add("raise")
    if choices - legal:
        yield "action unavailable at decision"
    if "bet" in choices and not any(
        s.live and s.stack > 0 and not s.is_hero for s in snap.seats
    ):
        yield "bet with no respondent"
    if advice.size:
        hero = next(s for s in snap.seats if s.is_hero)
        if "bet" not in choices or not 0 < advice.size.amount <= min(
            snap.pot, hero.stack
        ):
            yield "bet size outside action or pot/stack bounds"
    if not advice.choices or not advice.rankable:
        if advice.difference(event.action, event.amount):
            yield "conditional advice marked as a difference"


def audit_library(db, room="Both", *, examples=False):
    """Counts include agreements and manual decisions. Examples are local and opt-in."""
    stats = defaultdict(Counter)
    errors = Counter()
    error_examples = {}

    def inspect(event, snap, advice):
        row = stats[(snap.room, snap.street)]
        row["decisions"] += 1
        if advice and advice.can_compare(snap):
            row["rankable"] += 1
            if advice.difference(event.action, event.amount):
                row["differences"] += 1
        else:
            row["review_only"] += 1
        for error in inconsistencies(event, snap, advice):
            errors[error] += 1
            if examples:
                error_examples.setdefault(error, (snap.room, snap.number, event.ref))

    cohort(db, room)
    index = index_guides(db, room, audit=inspect)
    ranked = Counter((i.room, i.street) for i in index.items)
    differences = Counter((i.room, i.street) for i in index.items if i.difference)
    rank_ids = {(i.hand, i.ref) for i in index.items}
    if any((spot.hand, spot.ref) in rank_ids for spot in index.study):
        errors["study-only decision in ranked opportunities"] += 1
    counts = []
    for (room_name, street), row in stats.items():
        if (ranked[room_name, street], differences[room_name, street]) != (
            row["rankable"],
            row["differences"],
        ):
            errors["index denominator or difference mismatch"] += 1
        counts.append(
            dict(
                room=room_name,
                street=street,
                decisions=row["decisions"],
                rankable=row["rankable"],
                differences=row["differences"],
                review_only=row["review_only"],
            )
        )
    return dict(counts=counts, violations=dict(errors), examples=error_examples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--library", type=Path, help="existing local SQLite library (read-only)"
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="include one source hand ID per failed check",
    )
    args = parser.parse_args()
    db = (
        sqlite3.connect(args.library.resolve().as_uri() + "?mode=ro", uri=True)
        if args.library
        else connect(":memory:")
    )
    with closing(db):
        db.row_factory = sqlite3.Row
        if not args.library:
            initialize(db)
            load_demo(db)
        report = audit_library(db, examples=args.examples)
    print(json.dumps(report, indent=2))
    return int(bool(report["violations"]))


if __name__ == "__main__":
    raise SystemExit(main())
