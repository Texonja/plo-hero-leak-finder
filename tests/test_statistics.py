import gzip
import json
import unittest

from leakfinder.engine import replay
from leakfinder.importers import coin, ipoker
from leakfinder.statistics import BITS, DEFS, observations, settlements, sizing, tags

from tests.test_engine import FIX, decoded, original, paired


class DescriptiveStatsTests(unittest.TestCase):
    def test_extended_source_actions_both_networks(self):
        # Native action labels and original line/action numbers in the audited source files.
        marks = {
            "CoinPoker": {
                1: {"RIVERF": 38},
                3: {"FCB": 32, "DEFEND": 28, "RIVERC": 39},
                6: {"COLD": 22, "FFC": 28},
                11: {"SQUEEZE": 27, "CALL3": 30},
                15: {"DELAY": 35},
                16: {"TURN_XR": 36},
                18: {"TURNF": 35},
                75: {"RIVER_BARREL": 41},
                113: {"FOLD3": 26},
                132: {"FLOP_XR": 30},
                140: {"FOURBET": 30},
                145: {"FOLD_TO_RIVER_PROBE": 38},
            },
            "iPoker": {
                11: {"DEFEND": 6, "FOLD3": 7},
                24: {"FCB": 7, "FFC": 8},
                41: {"FOLD_TO_RIVER_PROBE": 10, "RIVERF": 10},
                50: {"COLD": 6},
                65: {"TURNF": 10},
                73: {"SQUEEZE": 6},
                90: {"RIVERC": 16},
                93: {"CALL3": 8},
                101: {"FLOP_XR": 11},
                115: {"DELAY": 12},
                230: {"TURN_XR": 11},
                278: {"FOURBET": 9},
                427: {"RIVER_BARREL": 14},
            },
        }
        corpus = json.loads(
            gzip.decompress((FIX / "street-hands.json.gz").read_bytes())
        )
        for room, num, raw in corpus:
            with self.subTest(room=room, number=num):
                h = coin(raw) if room == "CoinPoker" else ipoker(raw, True)
                d, ev = replay(h)
                obs = list(observations(h, d, ev))
                for k, no in marks.get(room, {}).get(num, {}).items():
                    ref = ("Line " if room == "CoinPoker" else "Action ") + str(no)
                    (row,) = [row for row in obs if row[1] == ref]
                    self.assertTrue(row[-2] & BITS[k])
                    self.assertTrue(row[-1] & BITS[k])
                # A hand contributes at most once per dealt player to VPIP and PFR.
                self.assertEqual(
                    sum(bool(row[-2] & BITS["VPIP"]) for row in obs), len(h.seats)
                )
                self.assertEqual(
                    sum(bool(row[-1] & BITS["PFR"]) for row in obs),
                    len({e.player for e in ev["Preflop"] if e.action == "raise"}),
                )

    def test_walks_in_vpip_denominator_without_voluntary_money(self):
        h = ipoker(original("ip", 1), True)
        ds, ev = replay(h)
        rows = [r for r in observations(h, ds, ev) if r[-2] & BITS["VPIP"]]
        self.assertEqual(len(rows), 4)
        self.assertFalse(any(r[-1] & BITS["VPIP"] for r in rows))

    def test_reconciled_profit_both_networks(self):
        h = coin(original("coin", 1))
        replay(h)
        self.assertAlmostEqual(settlements(h)[h.hint], 1.96)
        h = ipoker(original("ip", 1), True)
        replay(h)
        results = settlements(h)
        self.assertAlmostEqual(results[h.hint], -0.5)
        self.assertAlmostEqual(sum(results.values()), 0)
        h.awards[next(n for n, v in h.awards.items() if v > 0)] += 1
        self.assertEqual(settlements(h), {})

    def test_turn_and_river_sizing_use_bettors_pre_action_pot(self):
        for h in decoded(
            paired(
                [
                    [("A", "bet", 1.5), ("B", "call", 1.5)],
                    [("A", "bet", 3), ("B", "call", 3)],
                    [("A", "bet", 6), ("B", "fold", 0)],
                ]
            )
        ):
            ds, ev = replay(h)
            rows = list(observations(h, ds, ev))
            (size_row,) = [r for r in rows if r[-2] & BITS["RIVER_SIZE"]]
            (fold_row,) = [r for r in rows if r[-2] & BITS["RIVERF"]]
            self.assertEqual(size_row[4], 0.5)
            self.assertEqual(fold_row[5], 0.5)
            self.assertEqual(sizing(0.5), ">33–50% pot")
            self.assertEqual(sizing(None), "No bet")

    def test_tags_overlap_and_wheel_connectivity(self):
        self.assertTrue(
            {"AAxx", "Premium AAxx", "Paired", "Double suited", "Suited ace"}
            <= set(tags("Ac As Kc Qs".split()))
        )
        self.assertIn("Four-card rundown", tags("Ac 2h 3s 4d".split()))
        self.assertIn("One total gap", tags("4d 5c 7h 8s".split()))
        self.assertIn("Two total gaps", tags("4d 5c 7h 9s".split()))
        self.assertIn("Disconnected", tags("2d 6c 9h Ks".split()))
        self.assertEqual(tags([]), ["Unknown"])

    def test_all_metrics_have_complete_definitions(self):
        self.assertEqual(len(set(BITS.values())), len(DEFS))
        self.assertTrue(
            all(
                d.numerator and d.denominator and d.exclusions and d.fields
                for d in DEFS.values()
            )
        )
        self.assertIn("unresolved", DEFS["FFC"].denominator)
