"""Check base-model preflop event frequencies on public hands outside the ten-hand pilot.

Run: python validate_behavior.py PATH/TO/release-corpus.sqlite3
The corpus must have the same selected Hero aliases used for select_hands.py.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from leakfinder.analysis import wilson
from leakfinder.engine import replay
from leakfinder.importers import Record, decode
from leakfinder.storage import cohort
from pilots.btn_co.run_pilot import BASE, simulate
from pilots.btn_co.select_hands import PUBLIC, extract

HERE = Path(__file__).resolve().parent
ROOMS = ("CoinPoker", "iPoker")
QUERY = """SELECT h.room,h.number,c.hero,o.locator,o.raw,d.ref
    FROM decisions d JOIN cohort c ON c.hand=d.hand AND c.hero=d.actor
    JOIN hands h ON h.id=d.hand JOIN origins o ON o.rowid=(
       SELECT min(p.rowid) FROM origins p JOIN imports i ON i.id=p.source
       WHERE p.hand=h.id AND i.digest=CASE h.room WHEN 'CoinPoker' THEN ? WHEN 'iPoker' THEN ? END)
    WHERE d.node='THREEBET' AND h.hint=c.hero AND json_extract(d.cell,'$[4]')=6
    AND json_extract(d.cell,'$[5]')='BTN' AND json_extract(d.cell,'$[6]')='CO'"""


def labels(hand):
    raw = decode(
        Record(hand["room"], hand["source"], hand["source_locator"], hand["id"])
    )
    _, streets = replay(raw)
    pf = streets["Preflop"]
    (event,) = (e for e in pf if e.ref == hand["action_ref"])
    after = pf[event.idx :]
    remaining = set(raw.seats.values()) - {e.player for e in pf if e.action == "fold"}
    if len(remaining) > 1 and len(raw.boards.get("Flop", ())) != 3:
        raise ValueError("Missing public flop for multiple players.")
    if hand["action"] == "call":
        return "call_events", {
            "multiway_flop": len(remaining) >= 3,
            "flop": len(remaining) > 1,
        }
    if event.amount != min(event.pot + 2 * event.owed, event.remaining[event.player]):
        return None
    immediate = len(after) == 3 and {
        e.player for e in after if e.action == "fold"
    } == set(event.live) - {event.player}
    return "threebet_events", {
        "immediate_folds": immediate,
        "fourbet": any(e.action == "raise" for e in after),
        "won_preflop": remaining == {event.player},
    }


def collect(library, omitted):
    counts, cases, seen = Counter(), [], set()
    with closing(
        sqlite3.connect(library.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        cohort(db)
        for row in db.execute(QUERY, tuple(PUBLIC[room]["sha256"] for room in ROOMS)):
            found = extract(row)
            if not found:
                continue
            _, hand = found
            key = hand["room"], hand["id"]
            if key in seen:
                raise ValueError("Duplicate public opportunity.")
            seen.add(key)
            if key in omitted:
                continue
            action = hand["action"]
            if action == "fold":
                counts["fold"] += 1
                continue
            outcome = labels(hand)
            if outcome is None:
                counts["raise_other_size"] += 1
            else:
                branch, observed = outcome
                cases.append((hand, branch, observed))
                counts[branch] += 1
    if (
        len(omitted) != 10
        or not omitted <= seen
        or len(seen) != 210
        or counts
        != {
            "fold": 151,
            "raise_other_size": 6,
            "threebet_events": 6,
            "call_events": 37,
        }
    ):
        raise ValueError(
            f"Public cohort differs from the pinned validation population: {len(seen)}, {counts}."
        )
    return sorted(cases, key=lambda case: (case[0]["room"], case[0]["id"])), counts


def predict(case, executable, trials):
    hand, branch, observed = case
    key = f"BTN-CO/holdout-1|{hand['room']}|{hand['id']}"
    seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")
    counts = simulate(executable, hand, {}, trials, seed)[branch]
    # The simulation caps raises at a four-bet. After BTN makes the three-bet
    # a call or four-bet precludes BTN winning preflop in that model.
    predicted = {
        k: counts["won_preflop" if k == "immediate_folds" else k] for k in observed
    }
    return {
        "room": hand["room"],
        "id": hand["id"],
        "ref": hand["action_ref"],
        "locator": hand["source_locator"],
        "action": hand["action"],
        "ante_per_seat_bb": hand["ante_per_seat"],
        "seed": seed,
        "observed": observed,
        "simulated_event_counts": predicted,
    }


def summarize(rows, trials):
    metrics = {}
    for name in ("multiway_flop", "flop", "immediate_folds", "fourbet", "won_preflop"):
        relevant = [r for r in rows if name in r["observed"]]
        n = len(relevant)
        x = sum(r["observed"][name] for r in relevant)
        observed = x / n
        metrics[name] = {
            "hands": n,
            "observed_events": x,
            "observed_rate": round(observed, 6),
            "observed_wilson95": [round(v, 6) for v in wilson(observed, n)],
            "mean_predicted_rate": round(
                sum(r["simulated_event_counts"][name] / trials for r in relevant) / n, 6
            ),
        }
    return metrics


def validation_note(groups):
    multi = groups["combined"]["multiway_flop"]
    wins = groups["combined"]["immediate_folds"]
    ip = groups["iPoker"]["multiway_flop"]
    return (
        f"After a BTN call, at least three players saw the flop in {multi['observed_events']}/{multi['hands']} hands "
        f"({100 * multi['observed_rate']:.1f}%, model {100 * multi['mean_predicted_rate']:.1f}%). "
        f"On iPoker this happened in {ip['observed_events']}/{ip['hands']} hands "
        f"({100 * ip['observed_rate']:.1f}%, model {100 * ip['mean_predicted_rate']:.1f}%). "
        f"A pot-sized 3-bet won immediately in {wins['observed_events']}/{wins['hands']} hands without antes "
        f"({100 * wins['observed_rate']:.1f}%, model {100 * wins['mean_predicted_rate']:.1f}%). "
        "This holdout checks forecasts of later actions. Action EV requires a separate validation."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--pilot", type=Path, default=HERE / "hands.json")
    parser.add_argument("--exe", type=Path, default=HERE / "sim.exe")
    parser.add_argument("--trials", type=int, default=12000)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path, default=HERE / "validation.json")
    args = parser.parse_args()
    if args.trials < 10000:
        parser.error("Use at least 10000 trials per hand.")
    omitted = {
        (h["room"], h["id"])
        for h in json.loads(args.pilot.read_text(encoding="utf-8"))["hands"]
    }
    cases, counts = collect(args.library, omitted)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        rows = list(
            executor.map(
                lambda case: predict(case, args.exe.resolve(), args.trials), cases
            )
        )
    groups = {
        room: summarize([r for r in rows if r["room"] == room], args.trials)
        for room in ROOMS
    }
    groups["combined"] = summarize(rows, args.trials)
    report = {
        "schema_version": 1,
        "note_en": validation_note(groups),
        "scope": "Public holdout after removing the ten pilot hands. Conditional on the recorded BTN action. Each observed hand has equal weight. Observed intervals use marginal Wilson 95%; they do not adjust for repeated players or sessions. This compares future behavior only and does not validate EV or select or rank actions.",
        "definitions": {
            "multiway_flop": "Three or more live players after all preflop action, unconditional on BTN seeing a flop.",
            "flop": "Two or more live players after preflop.",
            "immediate_folds": "All three opponents immediately fold to BTN's exact pot three-bet. In the capped simulator this equals won_preflop; each event is measured separately in observed histories.",
            "fourbet": "Any preflop re-raise after BTN's exact pot three-bet.",
            "won_preflop": "Only BTN remains when preflop action ends, including any later win after a re-raise.",
        },
        "pilot_keys": sorted(omitted),
        "eligible_before_pilot_exclusion": sum(counts.values()) + len(omitted),
        "holdout_action_counts": dict(sorted(counts.items())),
        "public_sources": PUBLIC,
        "simulation": {
            "settings": BASE,
            "profile": "base",
            "ante": "Original per-seat ante in BB for each hand.",
            "trials_per_hand": args.trials,
            "seed_rule": "First four bytes (unsigned big-endian) of SHA256(UTF8('BTN-CO/holdout-1|room|id')).",
            "sim_cpp_sha256": hashlib.sha256(
                (HERE / "sim.cpp").read_bytes()
            ).hexdigest(),
        },
        "groups": groups,
        "cases": rows,
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(groups))


if __name__ == "__main__":
    main()
