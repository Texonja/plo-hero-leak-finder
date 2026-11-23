"""Cross-check the live decision inputs, paired estimation and review policy."""

import itertools
import json
import math
import random
import statistics
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from leakfinder.importers import Record, decode
from leakfinder.replay_view import snapshot_from_record
from leakfinder.vendor.primitives import best_plo_hand
from pilots.btn_co import run_pilot as pilot


def scenario(name, call, raise_ev, se=0.01):
    return {
        "name": name,
        "call": call,
        "raise": raise_ev,
        "call_se": se,
        "raise_se": se,
        "diff_se": se,
    }


class PilotTests(unittest.TestCase):
    def test_delivered_report_is_consistent_with_replicates_and_replay(self):
        published = json.loads(
            (pilot.HERE / "results.json").read_text(encoding="utf-8")
        )
        sources = json.loads((pilot.HERE / "hands.json").read_text(encoding="utf-8"))[
            "hands"
        ]
        raw = {(h["room"], h["id"]): h for h in sources}
        seen = set()
        for row in published["hands"]:
            key = (row["room"], row["id"])
            self.assertNotIn(key, seen)
            seen.add(key)
            by_name = {s["name"]: s for s in row["scenarios"]}
            self.assertEqual(list(by_name), [x for x, _ in pilot.SCENARIOS])
            self.assertEqual(
                row["signal"], pilot.rank_hand(raw[key], row["scenarios"])["signal"]
            )
            base = by_name["Base model"]
            free = by_name["No rake"]
            for s in by_name.values():
                runs = s["replicates"]
                self.assertEqual(len({r["seed"] for r in runs}), len(pilot.SEEDS))
                self.assertEqual(
                    sum(r["trials"] for r in runs), published["timing"]["samples"]
                )
                for r in runs:
                    self.assertAlmostEqual(r["threebet_to"], raw[key]["threebet_to"])
                    self.assertAlmostEqual(
                        r["difference_ev"], r["threebet_ev"] - r["call_ev"]
                    )
                    for node in ["call_events", "threebet_events"]:
                        self.assertLessEqual(r[node]["multiway_flop"], r[node]["flop"])
                        self.assertLessEqual(
                            r[node]["flop"] + r[node]["won_preflop"], r["trials"]
                        )
            # With identical policies, cards and seeds, removing rake can only
            # increase gross receipts. A single changed action would break the pairing.
            for b, f in zip(base["replicates"], free["replicates"], strict=True):
                for field in ("call_ev", "threebet_ev"):
                    self.assertGreaterEqual(f[field] + 1e-10, b[field])
                for field in ("seed", "call_events", "threebet_events"):
                    self.assertEqual(f[field], b[field])
        self.assertEqual(seen, set(raw))

    def test_published_snapshots_rebuild_at_source_action(self):
        data = json.loads((pilot.HERE / "hands.json").read_text(encoding="utf-8"))
        self.assertEqual([s["selected"] for s in data["strata"]], [3, 2, 5])
        self.assertEqual(len({(h["room"], h["id"]) for h in data["hands"]}), 10)
        for h in data["hands"]:
            record = Record(h["room"], h["source"], h["source_locator"], h["id"])
            hero = decode(record).hint
            s = snapshot_from_record(record, hero, h["action_ref"])
            with self.subTest(room=h["room"], number=h["id"]):
                self.assertEqual(list(s.hero_cards), h["cards"])
                self.assertEqual(s.board, ())
                self.assertAlmostEqual(s.pot / s.bb, h["pot"])
                self.assertAlmostEqual(s.to_call / s.bb, h["open_to"])
                active = {a.position: a for a in s.seats if a.live}
                self.assertEqual(
                    [active[p].stack / s.bb for p in data["positions"]], h["stacks"]
                )
                self.assertEqual(
                    [active[p].committed / s.bb for p in data["positions"]],
                    h["committed"],
                )
                self.assertEqual(
                    [a.action for a in s.history[-3:]], ["fold", "fold", "raise"]
                )

    def test_pool_matches_individual_trial_data(self):
        a, b = [-4, 10, -4, 10], [5, 0, -1]
        runs = [
            {
                "trials": len(x),
                "m": statistics.mean(x),
                "se": statistics.stdev(x) / math.sqrt(len(x)),
            }
            for x in (a, b)
        ]
        m, se = pilot.pool(runs, "m", "se")
        self.assertAlmostEqual(m, statistics.mean(a + b))
        self.assertAlmostEqual(se, statistics.stdev(a + b) / math.sqrt(len(a + b)))

    def test_review_requires_separation_for_actual_action(self):
        hand = {"room": "iPoker", "id": "123", "action": "call"}
        result = pilot.rank_hand(hand, [scenario("A", 1, 2), scenario("B", 1.1, 2.3)])
        self.assertEqual(
            (result["signal"], result["alternative"]), ("consistent", "raise")
        )
        self.assertEqual(
            pilot.rank_hand(hand, [scenario("A", 1, 2), scenario("B", 1.1, 0.4)])[
                "signal"
            ],
            "sensitive",
        )
        self.assertEqual(
            pilot.rank_hand(hand, [scenario("A", 1, 2), scenario("B", 1, 1.01)])[
                "signal"
            ],
            "exploratory",
        )
        self.assertEqual(
            pilot.rank_hand(hand, [scenario("A", 2, -1), scenario("B", 2, 1)])[
                "signal"
            ],
            "none",
        )
        hand.update(action="raise", actual_to=8, threebet_to=12)
        self.assertEqual(pilot.rank_hand(hand, [scenario("A", 1, 2)])["signal"], "size")

    def test_uncertainty_in_action_comparison_uses_paired_difference(self):
        s = scenario("A", 1, 1.7, 0.9)
        s["diff_se"] = 0.03
        self.assertEqual(pilot.contrast(s, "raise", "call"), (0.7, 0.03))
        self.assertEqual(pilot.contrast(s, "call", "raise"), (-0.7, 0.03))

    def test_solver_receives_only_inputs_available_at_decision(self):
        h = json.loads((pilot.HERE / "hands.json").read_text(encoding="utf-8"))[
            "hands"
        ][0]

        def args_for(hand):
            reply = json.dumps({"trials": 12, "threebet_to": hand["threebet_to"]})
            with patch.object(
                pilot.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, reply, ""),
            ) as child:
                pilot.simulate(Path("sim"), hand, {}, 12, 123)
                return child.call_args.args

        self.assertEqual(
            args_for(h),
            args_for(
                h
                | {
                    "action": "fold",
                    "actual_to": 999,
                    "source": "changed board and outcome",
                }
            ),
        )

    def test_native_omaha_evaluator_against_existing_reference(self):
        exe = pilot.HERE / ("sim.exe" if pilot.os.name == "nt" else "sim")
        if not exe.exists():
            self.skipTest("Compile the optional pilot CPU engine to cross-check it")
        rng = random.Random(37013)
        deck = [r + s for r in "23456789TJQKA" for s in "cdhs"]
        pairs = [
            ("AsAhKdQc", "2s3h4d5c6s"),
            ("AsKhQhJh", "2s4s5s7s8s"),
            ("Ac2c3d4d", "3c4c5cKhKd"),
            ("AhAcKsQd", "AdAsKh8c2d"),
        ]
        for _ in range(240):
            deal = rng.sample(deck, 9)
            pairs.append(("".join(deal[:4]), "".join(deal[4:])))
        req = "".join(f"{h} {b}\n" for h, b in pairs)
        scores = list(
            map(
                int,
                subprocess.run(
                    [str(exe), "--eval-batch"],
                    input=req,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.split(),
            )
        )
        ref = []
        for hole, board in pairs:
            cards = lambda s: [s[i : i + 2] for i in range(0, len(s), 2)]
            c, kicks, _, _ = best_plo_hand(cards(hole), cards(board))
            ref.append((c, tuple(kicks)))
        self.assertEqual(len(scores), len(ref))
        order = sorted(range(len(scores)), key=ref.__getitem__)
        for i, j in itertools.pairwise(order):
            self.assertEqual(
                (scores[i] > scores[j]) - (scores[i] < scores[j]),
                (ref[i] > ref[j]) - (ref[i] < ref[j]),
            )
        self.assertEqual([s >> 20 for s in scores], [c for c, _ in ref])
