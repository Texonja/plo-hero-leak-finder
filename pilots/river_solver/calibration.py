"""Fit arrival likelihoods to shown opponent actions in the private, read-only cohort."""

import argparse
import hashlib
import json
import sqlite3
import zlib
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

from leakfinder.analysis import wilson
from leakfinder.engine import replay
from leakfinder.importers import Record, decode
from leakfinder.statistics import settled_chips
from leakfinder.storage import cohort

from .ranges import DECK, PARAMETERS, packed_rank, pre_strength, street_features
from .run import dump

FITS = {
    "bet": ("postflop", "bet_intercept", "bet_strength_slope", "bet_draw_slope"),
    "raise": (
        "postflop",
        "raise_intercept",
        "raise_strength_slope",
        "raise_draw_slope",
    ),
    "preflop": (
        "preflop",
        "raise_intercept",
        "raise_structure_slope",
        "prior_raise_aggression_penalty",
    ),
}
CONTINUE = ("continue_intercept", "continue_strength_slope", "continue_draw_slope")
CATEGORIES = (
    "high card",
    "pair",
    "two pair",
    "trips",
    "straight",
    "flush",
    "full house+",
)
KEYS = (
    "river_set_small_lead",
    "river_broadway_checked_to",
    "river_check_back_value",
    "river_check_back_top_two",
    "river_call_four_flush_board",
    "river_call_large_raise",
    "value_size_40",
    "value_size_55",
)
ACTIVE = {"bet", "call", "raise", "check"}
# P is the river bettor / raiser, C is the caller. Earlier players are
# projected out, but the order of both finalists is retained.
LINES = {
    KEYS[0]: (
        ("Pbet", "Craise", "Pcall"),
        ("Pcheck", "Ccheck"),
        "lead",
        "small",
        (3,),
        False,
    ),
    KEYS[1]: (
        ("Ccheck", "Pcheck"),
        ("Ccheck", "Pcheck"),
        "after check",
        "small",
        (3,),
        False,
    ),
    KEYS[2]: (
        ("Ccheck", "Pcheck"),
        ("Ccheck", "Pcheck"),
        "after check",
        "any",
        (0, 1, 2, 3, 4, 5),
        None,
    ),
    KEYS[3]: (
        ("Ccheck", "Pcheck"),
        ("Ccheck", "Pbet", "Ccall"),
        "after check",
        "small",
        (0, 1, 2),
        False,
    ),
    KEYS[4]: (
        ("Pcheck", "Cbet", "Pcall"),
        ("Pcheck", "Ccheck"),
        "lead",
        "large",
        (4,),
        None,
    ),
    KEYS[5]: (
        ("Ccheck", "Pcheck"),
        ("Cbet", "Pcall"),
        "raise",
        "raise",
        (0, 1, 2, 3, 4, 5),
        None,
    ),
    KEYS[6]: (
        ("Pcheck", "Ccheck"),
        ("Pcheck", "Ccheck"),
        "lead",
        "small",
        (0, 1, 2, 3, 4, 5),
        False,
    ),
    KEYS[7]: (
        ("Pcheck", "Ccheck"),
        ("Pbet", "Ccall"),
        "lead",
        "middle",
        (0, 1, 2, 3, 4, 5),
        None,
    ),
}


def hand_hash(room, number):
    return hashlib.sha256(f"{room}/{number}".encode()).digest()


def positions(hand):
    seats = [hand.seats[n] for n in sorted(hand.seats)]
    j = seats.index(hand.seats[hand.button])
    return seats[j + 1 :] + seats[: j + 1]


def feats(cards, board):
    ids = np.array([[DECK.index(c) for c in cards]], dtype=np.uint8)
    board_ids = tuple(DECK.index(c) for c in board)
    scores = np.zeros((52, 52), dtype=np.int32)
    for a, b in combinations(ids[0], 2):
        scores[a, b] = max(
            packed_rank([DECK[q] for q in (a, b, *triple)])
            for triple in combinations(board_ids, 3)
        )
    return street_features(ids, board_ids, scores)[0]


def events_for_fit(hand, streets, hero, shown, key):
    for index, street in enumerate(("Preflop", "Flop", "Turn")):
        raises = 0
        features = {}
        for e in streets[street]:
            who = e.player
            if who != hero and who in shown and e.action in ACTIVE:
                cards = shown[who]
                if index == 0:
                    # Conditional among voluntary continuations with a legal raise.
                    if e.action in ("call", "raise") and e.can_raise:
                        x = pre_strength(
                            np.array([[DECK.index(c) for c in cards]], dtype=np.uint8)
                        )[0]
                        yield (
                            "preflop",
                            key,
                            [1.0, x, -float(raises)],
                            0.0,
                            int(e.action == "raise"),
                        )
                else:
                    family = (
                        "bet"
                        if e.action in ("bet", "check") and not e.owed
                        else "raise"
                        if e.action in ("call", "raise") and e.owed and e.can_raise
                        else None
                    )
                    if family:
                        if who not in features:
                            features[who] = feats(cards, hand.boards[street])
                        s = features[who]
                        d = s.draw_score[0] * (
                            PARAMETERS["postflop"]["turn_draw_discount"]
                            if index == 2
                            else 1
                        )
                        offset = s.blocker_score[0]
                        if family == "raise":
                            offset -= (
                                max(0, raises - 1)
                                * PARAMETERS["postflop"]["facing_raise_penalty"]
                            )
                        yield (
                            family,
                            key,
                            [1.0, s.strength[0], d],
                            float(offset),
                            int(e.action in ("bet", "raise")),
                        )
            if e.action in ("bet", "raise"):
                raises += 1


def played_line(events, p, c):
    return tuple(
        ("P" if e.player == p else "C") + e.action
        for e in events
        if e.action in ACTIVE and e.player in (p, c)
    )


def called_rivers(hand, streets, hero):
    river = streets["River"]
    if not river or len(river[0].live) != 2:
        return
    ranks = Counter(c[0] for c in hand.boards["River"])
    suits = max(Counter(c[1] for c in hand.boards["River"]).values())
    paired = len(ranks) < 5
    bet = None
    for i, e in enumerate(river):
        if e.action in ("bet", "raise"):
            bet = (i, e)
        if e.action != "call" or not bet:
            continue
        j, aggressor = bet
        p, c = aggressor.player, e.player
        if p == hero or c == p:
            continue
        route = (
            "raise"
            if aggressor.action == "raise"
            else "lead"
            if j == 0
            else "after check"
            if j == 1 and river[0].action == "check"
            else "other"
        )
        fraction = aggressor.amount / aggressor.pot
        band = (
            "raise"
            if route == "raise"
            else "small"
            if fraction <= 0.4 + 1e-10
            else "middle"
            if fraction <= 0.8 + 1e-10
            else "large"
        )
        flop, turn = (played_line(streets[st], p, c) for st in ("Flop", "Turn"))
        for key, (f, t, r, b, s, pr) in LINES.items():
            if (
                flop == f
                and turn == t
                and route == r
                and (b == "any" or band == b)
                and suits in s
                and (pr is None or paired == pr)
            ):
                yield key, p, c, river[: i + 1], band, e


def sanitize(hand, streets, seq, bettor, caller, token, band):
    order = [x for x in positions(hand) if x in (bettor, caller)]
    names = dict(zip(order, ("OOP", "IP"), strict=True))
    first = seq[0]
    cap = min(first.remaining[p] for p in order) / hand.bb
    return {
        "reference": token,
        "river": [
            {
                "action": e.action,
                "seat": names[e.player],
                "amount_bb": e.amount / hand.bb,
            }
            for e in seq
        ],
        "board": hand.boards["River"],
        "pot_bb": first.pot / hand.bb,
        "effective_stack_bb": cap,
        "bettor": names[bettor],
        "caller": names[caller],
        "size_bucket": band,
        "history": [
            {"street": st, "name": names.get(e.player, "Other"), "action": e.action}
            for st in ("Preflop", "Flop", "Turn")
            for e in streets[st]
        ],
    }


def install_aliases(db, aliases):
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("BEGIN")
    if aliases:
        db.execute(
            "CREATE TEMP TABLE aliases(room TEXT,name TEXT,PRIMARY KEY(room,name)) WITHOUT ROWID"
        )
        db.execute("INSERT INTO temp.aliases SELECT room,name FROM main.aliases")
        for alias in aliases:
            room, name = alias.split(":", 1)
            names = db.execute(
                "SELECT DISTINCT hint FROM hands WHERE room=? AND lower(hint)=lower(?) AND conflict=0",
                (room, name),
            ).fetchall()
            if len(names) != 1:
                raise ValueError("Historical alias lacks a unique POV spelling")
            db.execute(
                "INSERT OR IGNORE INTO temp.aliases VALUES(?,?)", (room, names[0][0])
            )
    cohort(db, "Both")


def scan(db, aliases, out, *, empirical_only=False):
    from leakfinder.vendor.primitives import best_plo_hand

    install_aliases(db, aliases)
    rows = db.execute("""SELECT c.hero,h.room,h.number,h.cards,o.locator,o.raw FROM cohort c
        JOIN hands h ON h.id=c.hand JOIN origins o ON o.rowid=(SELECT min(p.rowid) FROM origins p WHERE p.hand=c.hand)
        ORDER BY c.hand""")
    observations = defaultdict(list)
    counts = {k: Counter() for k in KEYS}
    chosen = {k: {} for k in KEYS}
    reserve = {k: {} for k in KEYS}
    preflop_lines = {k: Counter() for k in KEYS}
    size_groups = {k: Counter() for k in KEYS}
    texture_groups = {k: Counter() for k in KEYS}
    payoffs = defaultdict(list)
    checked = 0
    for hero, room, number, pockets, locator, raw in rows:
        if str(number) in {
            str(9000000000 + n)
            for n in (4202, 10026, 14007, 2872, 9999, 5983, 1429, 95, 8585)
        }:
            continue
        h = decode(Record(room, zlib.decompress(raw).decode(), locator, number))
        _, streets = replay(h)
        checked += 1
        if checked % 30000 == 0:
            print("Library hands", checked, flush=True)
        if len(h.boards.get("River", ())) != 5:
            continue
        digest = hand_hash(room, number)
        group = int.from_bytes(digest[:8], "big")
        # A shown card source and survival are required. Folded hole cards
        # occasionally present in an export are outside this design.
        shown = {
            n: c
            for n, c in json.loads(pockets).items()
            if n in h.survivors and len(c) == 4
        }
        if not empirical_only and any(n != hero for n in shown):
            for family, g, x, o, y in events_for_fit(h, streets, hero, shown, group):
                observations[family].append((g, *x, o, y))
        river = streets["River"]
        for family, p, c, seq, band, call in called_rivers(h, streets, hero):
            counts[family]["called"] += 1
            preflop_lines[family][" ".join(played_line(streets["Preflop"], p, c))] += 1
            size_groups[family][band] += 1
            board = h.boards["River"]
            suited = max(Counter(card[1] for card in board).values())
            paired = len({card[0] for card in board}) < 5
            texture_groups[family][
                f"{suited}-suit/{'paired' if paired else 'unpaired'}"
            ] += 1
            pocket = shown.get(p)
            if pocket:
                category = min(6, best_plo_hand(pocket, h.boards["River"])[0])
                counts[family][CATEGORIES[category]] += 1
            else:
                counts[family]["not_shown"] += 1
            if c == hero and call is river[-1]:
                net = settled_chips(h)
                if hero in net:
                    payoffs[family].append((h.awards.get(hero, 0) - call.amount) / h.bb)
                else:
                    counts[family]["unverified"] += 1
            ref = group
            subset = chosen[family] if ref % 10 >= 7 else reserve[family]
            if len(subset) < 20 or ref < max(subset):
                subset[ref] = sanitize(h, streets, seq, p, c, digest[8:14].hex(), band)
                subset[ref]["fit_partition"] = "holdout" if ref % 10 >= 7 else "train"
                if len(subset) > 20:
                    del subset[max(subset)]
    if empirical_only:
        saved = json.loads((out / "calibration.json").read_text(encoding="utf8"))
        fitted, models = saved["fitted"], saved["calibrated_parameters"]
    else:
        fitted, models = fit(observations)
    empirical = {}
    samples = {}
    for k, c in counts.items():
        sample = [x for _, x in sorted(chosen[k].items())]
        if not sample:
            # A one-hand line can have no holdout examples. Preserve its
            # measurement and explicitly mark the lack of independence.
            sample = [x for _, x in sorted(reserve[k].items())]
        samples[k] = sample
        n = c["called"] - c["not_shown"]
        empirical[k] = {
            "called": c["called"],
            "shown": n,
            "unknown_cards": c["not_shown"],
            "categories": {
                name: {
                    "n": c[name],
                    "rate": c[name] / n if n else None,
                    "wilson95": wilson(c[name] / n, n) if n else None,
                }
                for name in CATEGORIES
            },
            "local_closing_calls": money_summary(payoffs[k], c["unverified"]),
            "sample_size": len(sample),
            "line": LINES[k],
            "preflop_lines": dict(preflop_lines[k]),
            "river_size_groups": dict(size_groups[k]),
            "river_texture_groups": dict(texture_groups[k]),
        }
    dump(
        out / "calibration.json",
        {
            "hands": checked,
            "fitted": fitted,
            "baseline_parameters": PARAMETERS,
            "calibrated_parameters": models,
            "families": empirical,
            "fit_conditioning": "shown surviving non-Hero hands only",
            "split": "first eight bytes of SHA-256(room/hand), modulo ten; 0–6 train, 7–9 holdout",
            "fit_uncertainty": "95% marginal hand-cluster sandwich intervals, conditional on shown cards",
            "remaining_assumptions": {
                "preflop": sorted(
                    set(PARAMETERS["preflop"]) - set(FITS["preflop"][1:])
                ),
                "postflop": sorted(
                    set(PARAMETERS["postflop"])
                    - set(FITS["bet"][1:] + FITS["raise"][1:])
                ),
                "additional": [
                    "preflop structure",
                    "profile adjustments",
                    "tail pruning",
                ],
            },
        },
    )
    dump(out / "validation-sample.json", samples)


def money_summary(a, unverified):
    n = len(a)
    return {
        "n": n,
        "sum_bb": sum(a) if n else None,
        "average_bb": sum(a) / n if n else None,
        "positive": sum(x > 0 for x in a),
        "negative": sum(x < 0 for x in a),
        "trimmed_mean_bb": sum(sorted(a)[1:-1]) / (n - 2) if n > 2 else None,
        "unverified": unverified,
    }


def fit(observations):
    model = deepcopy(PARAMETERS)
    report = {}
    for family, rows in observations.items():
        street, *names = FITS[family]
        groups = np.asarray([r[0] for r in rows], dtype=np.uint64)
        values = np.asarray([r[1:] for r in rows])
        x, offset, y = values[:, :3], values[:, 3], values[:, 4]
        train = groups % 10 < 7
        floor = (
            PARAMETERS["postflop"]["slowplay_probability_floor"]
            if family == "bet"
            else 0.0
        )
        baseline = np.array([PARAMETERS[street][k] for k in names])

        def f(beta, x, offset, y, floor=floor):
            sig = expit(x @ beta + offset)
            p = np.clip(floor + (1 - 2 * floor) * sig, 1e-12, 1 - 1e-12)
            slope = (1 - 2 * floor) * sig * (1 - sig)
            loss = -y * np.log(p) - (1 - y) * np.log1p(-p)
            score = x * ((p - y) * slope / (p * (1 - p)))[:, None]
            return loss, score, slope * slope / (p * (1 - p))

        def target(beta, features=x[train], offsets=offset[train], labels=y[train]):
            loss, score, _ = f(beta, features, offsets, labels)
            return loss.mean(), score.mean(axis=0)

        result = minimize(
            target, baseline, jac=True, method="BFGS", options={"gtol": 1e-9}
        )
        if not result.success and np.linalg.norm(result.jac) > 1e-6:
            raise ValueError(result.message)
        beta = result.x
        _, score, info = f(beta, x[train], offset[train], y[train])
        _, inv = np.unique(groups[train], return_inverse=True)
        cluster = np.stack(
            [np.bincount(inv, weights=score[:, i]) for i in range(len(names))], axis=1
        )
        bread = np.linalg.pinv(x[train].T @ (x[train] * info[:, None]))
        se = np.sqrt(np.diag(bread @ (cluster.T @ cluster) @ bread))
        coefficients = {
            k: {
                "old": float(b),
                "fit": float(v),
                "ci95": [float(v - 1.96 * s), float(v + 1.96 * s)],
            }
            for k, b, v, s in zip(names, baseline, beta, se, strict=True)
        }
        for k, v in zip(names, beta, strict=True):
            model[street][k] = float(v)
        report[family] = {
            "section": street,
            "coefficients": coefficients,
            "train_actions": int(train.sum()),
            "holdout_actions": int((~train).sum()),
            "train_hands": len(np.unique(groups[train])),
            "holdout_hands": len(np.unique(groups[~train])),
            "holdout_log_loss_old": float(
                f(baseline, x[~train], offset[~train], y[~train])[0].mean()
            ),
            "holdout_log_loss_fit": float(
                f(beta, x[~train], offset[~train], y[~train])[0].mean()
            ),
        }
    if set(report) != set(FITS):
        raise ValueError("Missing fitted family")
    return report, model


def main():
    p = argparse.ArgumentParser()
    p.add_argument("library", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--alias", action="append", default=[])
    p.add_argument(
        "--empirical-only",
        action="store_true",
        help="Reuse the fitted coefficients and regenerate the library-line counts",
    )
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.library.resolve().as_uri() + "?mode=ro", uri=True) as db:
        scan(db, args.alias, args.out, empirical_only=args.empirical_only)


if __name__ == "__main__":
    main()
