"""Read-only audit of recorded river calls in the selected local Hero profile."""

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from leakfinder.analysis import wilson
from leakfinder.critique import critique_for
from leakfinder.engine import replay
from leakfinder.importers import Record, decode, money
from leakfinder.opponent_history import (
    OpponentHistory,
    apply_history,
    observations,
    river_opponent,
)
from leakfinder.replay_view import _alternate_cards, _snapshot, _source_records
from leakfinder.statistics import settled_chips
from leakfinder.storage import cohort
from leakfinder.strategy_guide import advice_at
from leakfinder.vendor.primitives import best_plo_hand

RAISE = "river_call_large_raise"
FOUR = "river_call_four_flush_board"
ORDERS = ("stronger", "weaker", "tie", "unknown")


def river_calls(river, board, hero):
    """Keep the aggressor that set the price, including after intervening calls."""
    aggressor = None
    four = max(Counter(c[1] for c in board).values(), default=0) == 4
    for event in river:
        if event.action in {"bet", "raise"}:
            aggressor = event
        elif (
            event.player == hero
            and event.action == "call"
            and aggressor is not None
            and (aggressor.action == "raise" or four)
        ):
            yield RAISE if aggressor.action == "raise" else FOUR, event, aggressor


def showdown(board, hero_cards, opponent_cards):
    if len(hero_cards) != 4 or len(opponent_cards) != 4:
        return "unknown", False
    if len({*board, *hero_cards, *opponent_cards}) != 13:
        raise ValueError("Incompatible showdown cards")
    hero = best_plo_hand(hero_cards, board)[:2]
    other = best_plo_hand(opponent_cards, board)[:2]
    order = "stronger" if other > hero else "weaker" if other < hero else "tie"
    high_or_board_pair = other[0] == 0 or (
        other[0] == 1
        and all("23456789TJQKA".index(c[0]) + 2 != other[1][0] for c in opponent_cards)
    )
    return order, order == "weaker" and high_or_board_pair


@dataclass
class Call:
    order: str
    air: bool
    heads_up: bool
    whole_net_bb: float | None
    incremental_bb: float | None
    rule: bool
    source: dict | None = None
    source_incremental_bb: float | None = None


def source_money(record, hand, hero, net):
    """Describe the room's final player fields separately from reconciliation."""
    if hand.room != "iPoker":
        return None
    raw = record.raw.strip()
    if record.locator.startswith("HM2") and raw.endswith("</game>"):
        raw += "</session>"
    players = ET.fromstring(raw).findall("game/general/players/player")
    bets = {
        p.attrib["name"].strip(): money(p.get("bet"))
        for p in players
        if p.get("bet") is not None
    }
    wins = {
        p.attrib["name"].strip(): money(p.get("win"))
        for p in players
        if p.get("win") is not None
    }
    rake = [
        money(p.get("rakeamount")) for p in players if p.get("rakeamount") is not None
    ]
    # Same unmatched-chip convention as the verified settlement. Keep raw win-bet intact.
    ranked = sorted(hand.final_commits.items(), key=lambda item: -item[1])
    refund = (ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)) if ranked else 0
    gross = sum(hand.spent.values()) - refund
    residual = gross - sum(wins.values()) - sum(rake)
    if hero in net:
        status = "verified"
    elif not wins or not set(wins) <= set(hand.spent):
        status = "missing_or_invalid_awards"
    elif any(v > 0 and n not in hand.survivors for n, v in wins.items()):
        status = "award_to_non_survivor"
    elif bets != hand.spent:
        status = "recorded_bets_missing_or_disagree_with_replay"
    elif refund and ranked[0][0] not in hand.survivors:
        status = "unattributable_return"
    elif hand.reported_pot is not None and gross != hand.reported_pot:
        status = "reported_pot_mismatch"
    elif residual == 0:
        raise ValueError("Unexplained settlement rejection")
    elif not rake:
        status = "payout_conservation_gap_no_rake_fields"
    elif len(rake) != len(players):
        status = "payout_conservation_gap_partial_rake_fields"
    else:
        status = "payout_conservation_gap_with_all_rake_fields"
    raw_net = wins[hero] - bets[hero] if hero in bets and hero in wins else None
    return {
        "status": status,
        "awarded_bb": wins[hero] / hand.bb if hero in wins else None,
        "raw_net_bb": raw_net / hand.bb if raw_net is not None else None,
        "raw_agrees_verified": raw_net == net[hero]
        if hero in net and raw_net is not None
        else None,
        "hero_bet_matches_replay": bets.get(hero) == hand.spent.get(hero),
        "all_bets_match_replay": bets == hand.spent,
        "all_players_have_win_and_bet": len(wins) == len(bets) == len(players),
        "all_players_have_rakeamount": len(rake) == len(players),
        "no_rake_fields": len(rake) == 0,
        "positive_residual": residual > 0,
        "residual_bb": residual / hand.bb,
    }


def source_summary(calls):
    sources = [c.source for c in calls if c.source is not None]
    unverified = [s for s in sources if s["status"] != "verified"]
    nets = [s["raw_net_bb"] for s in sources if s["raw_net_bb"] is not None]
    inc = [
        c.source_incremental_bb for c in calls if c.source_incremental_bb is not None
    ]
    checked = [s for s in sources if s["raw_agrees_verified"] is not None]
    return {
        "hands_with_source_net": len(nets),
        "source_reported_whole_hand_net_bb": round(sum(nets), 6) if nets else None,
        "closing_hu_hands_with_source_award": len(inc),
        "source_reported_closing_call_vs_fold_bb": round(sum(inc), 6) if inc else None,
        "raw_net_compared_to_verified_net": len(checked),
        "raw_net_matches_verified": sum(s["raw_agrees_verified"] for s in checked),
        "status_counts": dict(Counter(s["status"] for s in sources)),
        "all_seats_report_win_and_bet": sum(
            s["all_players_have_win_and_bet"] for s in sources
        ),
        "hero_bet_matches_independent_action_ledger": sum(
            s["hero_bet_matches_replay"] for s in sources
        ),
        "all_bets_match_independent_action_ledger": sum(
            s["all_bets_match_replay"] for s in sources
        ),
        "unverified_with_all_seats_reporting_rake": sum(
            s["all_players_have_rakeamount"] for s in unverified
        ),
        "unverified_with_no_rake_fields": sum(s["no_rake_fields"] for s in unverified),
        "unverified_with_positive_residual": sum(
            s["positive_residual"] for s in unverified
        ),
        "unverified_sum_residual_bb": round(
            sum(s["residual_bb"] for s in unverified), 6
        ),
    }


def stats(hands):
    """Report one line per distinct hand and retain all decision counts separately."""
    all_calls = [call for calls in hands for call in calls]
    last = [calls[-1] for calls in hands]
    orders = Counter(c.order for c in last)
    deals = len(last)
    known = deals - orders["unknown"]
    air = sum(c.air for c in last)
    net = [c.whole_net_bb for c in last if c.whole_net_bb is not None]
    inc = [c.incremental_bb for c in last if c.incremental_bb is not None]
    leave_one_out = (
        [(sum(inc) - x) / (len(inc) - 1) for x in (max(inc), min(inc))]
        if len(inc) > 1
        else None
    )
    return {
        "hands": deals,
        "source_reported": source_summary(last),
        "call_decisions": len(all_calls),
        "multiple_matching_calls_in_hand": sum(len(calls) > 1 for calls in hands),
        "by_last_matching_call": {k: orders[k] for k in ORDERS},
        "all_call_decisions_by_showdown": {
            k: sum(c.order == k for c in all_calls) for k in ORDERS
        },
        "last_call_heads_up": sum(c.heads_up for c in last),
        "last_call_multiway": sum(not c.heads_up for c in last),
        "shown_and_comparable": known,
        "weak_air_proxy_subset_of_weaker": air,
        "weaker_outside_proxy": orders["weaker"] - air,
        "weak_air_pct_of_comparable": (100 * air / known) if known else None,
        "weak_air_wilson95_pct": [100 * x for x in wilson(air / known, known)]
        if known
        else None,
        "whole_hand_settled": len(net),
        "whole_hand_net_bb": round(sum(net), 6) if net else None,
        "closing_hu_settled": len(inc),
        "realized_closing_call_vs_fold_bb": round(sum(inc), 6) if inc else None,
        "realized_closing_call_vs_fold_mean_bb": round(sum(inc) / len(inc), 6)
        if inc
        else None,
        "realized_closing_call_positive": sum(x > 0 for x in inc),
        "realized_closing_call_negative": sum(x < 0 for x in inc),
        "realized_closing_call_zero": sum(x == 0 for x in inc),
        "trimmed_mean_omit_best_and_worst_bb": round(
            sum(sorted(inc)[1:-1]) / (len(inc) - 2), 6
        )
        if len(inc) > 2
        else None,
        "unverified_whole_hand_count": deals - len(net),
        "unverified_closing_hu_count": sum(
            c.source_incremental_bb is not None and c.incremental_bb is None
            for c in last
        ),
        "mean_bb_omitting_one_min_max": [round(x, 6) for x in leave_one_out]
        if leave_one_out
        else None,
    }


def run(db, confirmed_aliases=()):
    # SQLite TEMP stays in RAM. All reads use one consistent WAL snapshot.
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("BEGIN")
    saved_aliases = dict(
        db.execute("SELECT room,count(*) FROM main.aliases GROUP BY room")
    )
    if confirmed_aliases:
        db.execute(
            "CREATE TEMP TABLE aliases(room TEXT,name TEXT, PRIMARY KEY(room,name)) WITHOUT ROWID"
        )
        db.execute("INSERT INTO temp.aliases SELECT room,name FROM main.aliases")
        for room, name in confirmed_aliases:
            names = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT hint FROM hands WHERE room=? AND lower(hint)=lower(?) AND conflict=0",
                    (room, name),
                )
            ]
            if len(names) != 1:
                raise ValueError(
                    "A requested historical alias lacks a unique recorded POV spelling"
                )
            db.execute(
                "INSERT OR IGNORE INTO temp.aliases VALUES(?,?)", (room, names[0])
            )
    cohort(db, "Both")
    rooms = {
        r: n
        for r, n in db.execute(
            "SELECT h.room,count(*) FROM cohort c JOIN hands h ON h.id=c.hand GROUP BY h.room"
        )
    }
    aliases = dict(db.execute("SELECT room,count(*) FROM aliases GROUP BY room"))
    totals = dict(db.execute("SELECT room,count(*) FROM hands GROUP BY room"))
    rows = db.execute("""
        SELECT c.hand,c.hero,h.room,h.number,h.cards,o.locator,o.raw
        FROM cohort c JOIN hands h ON c.hand=h.id JOIN origins o ON o.rowid=(
            SELECT min(p.rowid) FROM origins p WHERE p.hand=c.hand)
        ORDER BY c.hand
    """)
    groups = {RAISE: defaultdict(list), FOUR: defaultdict(list)}
    samples = {}
    replayed = 0
    river_hands = 0
    overlap = 0
    history = None
    for hid, hero, room, number, merged, locator, raw in rows:
        record = Record(room, zlib.decompress(raw).decode(), locator, number)
        hand = decode(record)
        _, streets = replay(hand)
        replayed += 1
        river = streets["River"]
        board = hand.boards.get("River", ())
        if len(board) != 5 or not river:
            continue
        river_hands += any(e.player == hero for e in river)
        targets = list(river_calls(river, board, hero))
        if not targets:
            continue
        overlap += len({rule for rule, _, _ in targets}) > 1
        pockets = json.loads(merged)
        net = settled_chips(hand)
        source = source_money(record, hand, hero, net)
        events = {e.ref: e for seq in streets.values() for e in seq}
        for rule, event, aggressor in targets:
            opponent = aggressor.player
            at_showdown = {hero, opponent} <= hand.survivors
            order, air = (
                showdown(board, pockets.get(hero, ()), pockets.get(opponent, ()))
                if at_showdown
                else ("unknown", False)
            )
            hu = len(event.live) == 2
            terminal = event is river[-1] and hu and at_showdown
            snapshot = _snapshot(record, hand, events, hero, event.ref)
            if snapshot.hero_cards is None:
                copies = iter(_source_records(db, hid))
                next(copies)
                snapshot = _alternate_cards(snapshot, copies, event, hero)
            matched = False
            if snapshot.hero_cards:
                advice = advice_at(snapshot)
                if advice and river_opponent(snapshot):
                    if history is None:
                        history = OpponentHistory(
                            observations(db, "Both", prepared=True)
                        )
                    advice = apply_history(snapshot, advice, history)
                critique = critique_for(snapshot, event, advice)
                matched = bool(critique and critique.rule == rule)
                if matched:
                    samples.setdefault(
                        rule,
                        {"title": critique.title, "alternative": critique.alternative},
                    )
            groups[rule][hid].append(
                Call(
                    order,
                    air,
                    hu,
                    net[hero] / hand.bb if hero in net else None,
                    (hand.awards.get(hero, 0) - event.amount) / hand.bb
                    if terminal and hero in net
                    else None,
                    matched,
                    source,
                    source["awarded_bb"] - event.amount / hand.bb
                    if terminal
                    and source is not None
                    and source["awarded_bb"] is not None
                    else None,
                )
            )
    if replayed != sum(rooms.values()):
        raise ValueError("At least one eligible hand has no canonical source record")
    return {
        "library_format_version": db.execute("PRAGMA user_version").fetchone()[0],
        "scope": "saved profile plus explicitly confirmed historical aliases"
        if confirmed_aliases
        else "saved active profile",
        "saved_profile_alias_counts_by_room": saved_aliases,
        "conflicting_library_hands_by_room": dict(
            db.execute(
                "SELECT room,count(*) FROM hands WHERE conflict<>0 GROUP BY room"
            )
        ),
        "ambiguous_multiple_alias_seats_excluded": db.execute("""SELECT count(*) FROM (
            SELECT h.id FROM hands h JOIN seats s ON h.id=s.hand JOIN aliases a ON a.room=h.room AND a.name=s.name
            WHERE h.conflict=0 GROUP BY h.id HAVING count(*)>1)""").fetchone()[0],
        "all_imported_unique_hands_by_room": totals,
        "selected_alias_counts_by_room": aliases,
        "hero_hands_in_this_scope_by_room": rooms,
        "hands_replayed": replayed,
        "hands_with_any_hero_river_action": river_hands,
        "hands_in_both_broad_groups": overlap,
        "definitions": {
            "source_reported": "Read personal win and bet directly from the canonical iPoker XML. Source-reported whole-hand net is (win minus bet) / BB without an inferred fee or refund adjustment. For closing heads-up calls the separate source-reported call-minus-fold is win / BB minus the call. Agreement with fully reconciled records is checked in exact chip units, and all players' bet fields are compared to the betting action ledger. An unexplained positive residual means matched pot exceeds listed awards plus declared rake. It cannot establish the fee paid by Hero; only the original verified subtotal passes settlement conservation.",
            "selection": "All dates and rooms in the active personal library. The application's room-local alias, exactly-one-seat, no-conflict and compatible Hero-hint filters are applied. One canonical source per room/hand number. Compatible stored pocket evidence may supplement that source. No public demo or external archive was added.",
            "groups": "A recorded Hero call is assigned by the most recent river aggressor. A raise includes reraises, short all-ins and cold overcalls. For a bet the board must contain exactly four cards in one suit. Multiway hands are retained for descriptive counts. A hand can enter both groups if the river action qualifies separately.",
            "showdown": "Compare the last matching call's aggressor with Hero using exactly two private and three board cards, including kickers. Require four known cards for each player and both surviving to showdown. An unshown holding or no shared showdown is unknown. Other opponents can win multiway. Every qualifying decision is also counted separately.",
            "weak_air_proxy": "Among shown weaker aggressors, count high card or a lone pair entirely on the board without a pocket card of that rank. This conservative visible category overlaps weaker and cannot identify intent, all bluffs or the frequency in uncalled bets. Stronger/slower value can be overplayed, and made hands may bluff.",
            "results": "Whole-hand net is the verified settlement after returns/declared fees divided by that hand's big blind, counted once per group hand. Realized closing-call minus fold uses only verified heads-up terminal calls, including split awards, and equals the award credited to Hero less the incremental call. It compares outcomes for the recorded deal. It is not a range EV.",
            "matched": "The subset actually triggers the named critique when running advice_at, the normal chronological opponent-history adjustment and critique_for on the pre-decision snapshot. Showdown and awards are inspected separately. If a hand has multiple firings, use its last firing.",
            "selection_bias": "Called, shown holdings describe the players and bet sizes Hero selected for a call. Unknown cards, differing stakes, repeated players and the omitted folds constrain inference. The Wilson interval for the descriptive proxy assumes independent hands and can understate uncertainty.",
        },
        "groups": {
            rule: {
                "all_calls": stats(list(group.values())),
                "critique_matched": stats(
                    [
                        matched
                        for calls in group.values()
                        if (matched := [c for c in calls if c.rule])
                    ]
                ),
                "observed_app_output": samples.get(rule),
            }
            for rule, group in groups.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--confirmed-alias",
        nargs=2,
        action="append",
        default=[],
        metavar=("ROOM", "NAME"),
        help="Additional owner-confirmed room alias, only in the in-memory audit scope.",
    )
    args = parser.parse_args()
    if args.library.resolve() == args.output.resolve():
        parser.error("Choose a separate aggregate output.")
    db = sqlite3.connect(args.library.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = run(db, args.confirmed_alias)
    finally:
        db.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding="utf8"
    )


if __name__ == "__main__":
    main()
