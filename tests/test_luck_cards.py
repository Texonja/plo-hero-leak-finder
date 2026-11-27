import math
import random
import unittest
from itertools import combinations

from leakfinder.luck import Tally, add_payload, encode, exact_percentile
from leakfinder.luck_cards import (
    DEALS,
    DENOM,
    FLOPS,
    NAMES,
    PAIR_HITS,
    _draws,
    _suit_groups,
    deal_counts,
    events,
)
from leakfinder.river_holdings import CARD_ID, PRIMES, UNIT, _five_ranks


def ids(text):
    return tuple(CARD_ID[c] for c in text.split())


def masks(h, b):
    ranks = tuple(sorted(c // 4 for c in h))
    return _draws(
        ranks,
        _suit_groups(h),
        {r for r in ranks if ranks.count(r) >= 2},
        sum(1 << c for c in h),
        b,
    )


def five(cards):
    score = _five_ranks()[math.prod(PRIMES[c // 4] for c in cards)] // UNIT
    if len({c % 4 for c in cards}) == 1:
        return 8 if score == 4 else 5
    return score


def best(h, b):
    return max(five(p + t) for p in combinations(h, 2) for t in combinations(b, 3))


class LuckCards(unittest.TestCase):
    def test_preflop_and_pocket_pairs(self):
        self.assertEqual(DEALS, 270725)
        counts = deal_counts()
        self.assertEqual(
            counts,
            (
                DEALS - math.comb(13, 4) * 256,
                sum(math.comb(4, k) * math.comb(48, 4 - k) for k in (2, 3, 4)),
                6 * math.comb(13, 2) ** 2,
                11 * 256,
                4 * math.comb(13, 4),
            ),
        )
        self.assertAlmostEqual(100 * counts[0] / DEALS, 32.39, places=2)
        self.assertAlmostEqual(100 * counts[1] / DEALS, 2.571, places=3)
        for idx, (hand, unseen) in enumerate(
            [("As Ah 3c 6d", 46), ("As Ah 4c 4d", 44)]
        ):
            row = {i: (n, hit) for i, n, hit in events(ids(hand), ids("Ac Kd 7d"))}[
                5 + idx
            ]
            self.assertEqual(row, (FLOPS - math.comb(unseen, 3), 1))
            self.assertEqual(row[0], PAIR_HITS[idx])

    def test_nine_outs_and_blockers(self):
        h, b = ids("As 7s Jd 3c"), ids("Ks 4s 9h")
        self.assertEqual(masks(h, b)[0].bit_count(), 9)
        self.assertEqual(masks(h, b + ids("2d"))[0].bit_count(), 9)
        self.assertEqual(DENOM[10], 45)
        self.assertEqual(DENOM[13], 44)
        self.assertAlmostEqual(1 - (1 - 9 / 45) * (1 - 9 / 44), 4 / 11)
        x = ids("As 7s Js 3c")
        outs = masks(x, b)[0]
        self.assertEqual(outs.bit_count(), 8)
        self.assertFalse(any(outs & (1 << c) for c in (*x, *b)))

    def test_outs_vs_exhaustive_two_plus_three(self):
        rng = random.Random(543)
        cases = [([*ids("8s 8d 7s Ac")], [*ids("8h 5s 6s")])]
        for j in range(100):
            cards = rng.sample(range(52), 7 + j % 2)
            cases.append((cards[:4], cards[4:]))
        for h, b in cases:
            for i, mask in enumerate(masks(h, b)):
                if mask is None:
                    continue
                target = (5, 4, 6)[i]
                truth = sum(
                    1 << c
                    for c in set(range(52)).difference(h, b)
                    if best(h, (*b, c)) >= target
                )
                self.assertEqual(mask, truth, (h, b, i))

    def test_all_flop_combinations(self):
        for desc in ("As Ah Ks Kh", "8c 7c 6h 5h", "2c 2d 2h 8h"):
            h = ids(desc)
            deck = sorted(set(range(52)).difference(h))
            expected = [0] * 3
            for flop in combinations(deck, 3):
                strength = best(h, flop)
                fd = any(
                    sum(c % 4 == s for c in h) >= 2
                    and sum(c % 4 == s for c in flop) >= 2
                    for s in range(4)
                )
                expected[0] += fd or strength >= 6
                expected[1] += strength >= 4
                expected[2] += strength >= 2
            for i, n, _ in events(h, tuple(deck[:3])):
                if 7 <= i <= 9:
                    self.assertEqual(n, expected[i - 7], (desc, i))

    def test_turn_fold_selection_bias(self):
        # Enumerate every physical turn/river pair once. Hero folds missed
        # turns; other players continue so the river is still recorded.
        h, b = ids("As 7s Jd 3c"), ids("Ks 4s 9h")
        all_turn, survivors, after_fold = Tally(), Tally(), 0
        deck = set(range(52)).difference(h, b)
        all_river = Tally()
        for turn in deck:
            for river in deck - {turn}:
                rows = {i: (w, hit) for i, w, hit in events(h, (*b, turn, river))}
                if 13 in rows:
                    rw, rhit = rows[13]
                    all_river.add(rw, 44, rhit, 1)
                w, hit = rows[10]
                all_turn.add(w, 45, hit, 1)
                if hit:
                    survivors.add(w, 45, hit, 1)
                elif 13 in rows:
                    after_fold += 1
        self.assertGreater(after_fold, 0)
        self.assertAlmostEqual(all_turn.row()["z"], 0)
        self.assertAlmostEqual(all_river.row()["z"], 0)
        self.assertGreater(survivors.row()["z"], 4)

    def test_exact_mid_cdf_and_alerts(self):
        self.assertAlmostEqual(exact_percentile({(1, 2): 1, (1, 4): 1}, 1), 62.5)
        t = Tally()
        t.add(0, 45, 1, 1)
        self.assertTrue(t.row()["check_parser"])
        self.assertEqual(t.row()["status"], "Small sample")
        self.assertIsNone(Tally().row()["percentile"])

    def test_200000_uniform_deals(self):
        rng = random.Random(20261126)
        tallies = [Tally() for _ in NAMES]
        for _ in range(200000):
            cards = rng.sample(range(52), 9)
            add_payload(tallies, encode(cards[:4], cards[4:], True))
        for name, t in zip(NAMES, tallies):
            z = t.row()["z"]
            print(f"{name.encode('ascii', 'replace').decode()}: n={t.n}, z={z:.4f}")
            self.assertLess(abs(z), 4, name)
