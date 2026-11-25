import unittest
from itertools import combinations

from leakfinder.river_holdings import DECK, river_holdings
from leakfinder.vendor.primitives import best_plo_hand, eval_5


def reference(cards, board):
    cat, kick, *_ = best_plo_hand(cards, board)
    return cat, tuple(kick)


class RiverHoldingTests(unittest.TestCase):
    def test_complete_hand_categories_and_kickers_against_reference(self):
        for hero, board in [
            ("Ah Ad Kh 7c", "Ac Ks Kc 6c 5c"),
            ("Jh 9c 2s 3d", "5s 6s Ts Ks Js"),
            ("Ac 2c 8d 9d", "3s 4d 6h 2s 5c"),
            ("Qc Qh Qd Qs", "Jd Jc Jh Js Ad"),
            ("2c 3h 4d 5s", "Jh 8s 8d Kc Ah"),
        ]:
            with self.subTest(board=board):
                hero, board = hero.split(), board.split()
                result = river_holdings(hero, board)
                own = reference(hero, board)
                self.assertEqual((result.hero_category, result.hero_kickers), own)
                self.assertEqual(
                    result.hero_turn_category, reference(hero, board[:4])[0]
                )
                groups = [{}, {}, {}]
                available = sorted(set(DECK).difference(hero, board))
                scores = {p: reference(p, board) for p in combinations(available, 2)}
                for hand in combinations(available, 4):
                    rank = max(scores[p] for p in combinations(hand, 2))
                    group = groups[(rank >= own) + (rank > own)]
                    group[rank[0]] = max(group.get(rank[0], rank), rank)
                for cases, expected in zip(
                    (result.weaker, result.ties, result.stronger), groups
                ):
                    self.assertEqual(
                        {(x.category, x.kickers) for x in cases}, set(expected.values())
                    )
                    for case in cases:
                        self.assertEqual(
                            reference(case.holding, board), (case.category, case.kickers)
                        )
                        self.assertEqual(
                            reference(case.holding, board[:4]),
                            (case.turn_category, case.turn_kickers),
                        )
                        self.assertEqual(len(set(case.holding) | set(hero + board)), 13)
                highest = max(rank for group in groups for rank in group.values())
                self.assertEqual((result.river_made, result.river_kickers), highest)

    def test_draws_have_actual_unblocked_turn_outs_and_miss_on_the_river(self):
        hero, board = ["Ah", "Kh", "Jc", "9d"], ["2s", "3s", "Th", "Jh", "6s"]
        result = river_holdings(hero, board)
        self.assertTrue(result.missed_flush)
        self.assertTrue(result.missed_straight)
        for examples, target in [
            (result.missed_flush, (5, 8)),
            (result.missed_straight, (4, 8)),
        ]:
            for ex in examples:
                self.assertLess(
                    (ex.category, ex.kickers),
                    (result.hero_category, result.hero_kickers),
                )
                self.assertLess(ex.category, 4)
                self.assertLess(ex.turn_category, 4)
                self.assertTrue(ex.turn_outs)
                blocked = set(hero + board + list(ex.holding))
                self.assertFalse(blocked.intersection(ex.turn_outs))
                for out in set(DECK) - blocked:
                    hits = any(
                        eval_5(list(ex.cards) + list(two) + [out])[0] in target
                        for two in combinations(board[:4], 2)
                    )
                    self.assertEqual(hits, out in ex.turn_outs, (ex, out))
        self.assertTrue(result.flush_blocker_bluffs)
        for ex in result.flush_blocker_bluffs:
            self.assertIn("As", ex.cards)
            self.assertEqual(sum(c[1] == "s" for c in ex.cards), 1)
            self.assertLessEqual(ex.category, 1)
        hero[0] = "As"
        self.assertFalse(river_holdings(hero, board).flush_blocker_bluffs)
        self.assertTrue(
            any("As" in text for text in river_holdings(hero, board).blockers)
        )

    def test_draw_identity_uses_the_actual_river_card(self):
        hero = ["Ah", "Kh", "Qc", "Qd"]
        turn = ["2s", "3s", "Th", "Jh"]
        self.assertFalse(river_holdings(hero, turn + ["7d"]).flush_blocker_bluffs)
        self.assertFalse(
            river_holdings(hero, ["2s", "3s", "7s", "Jh", "Th"]).flush_blocker_bluffs
        )
        self.assertNotEqual(
            river_holdings(hero, turn + ["7s"]),
            river_holdings(hero, ["2s", "3s", "7s", "Jh", "Th"]),
        )

    def test_nut_royal_and_duplicate_checks(self):
        r = river_holdings(["As", "Ks", "7c", "6d"], ["Qs", "Js", "Ts", "4d", "3c"])
        self.assertEqual(r.hero_category, 8)
        self.assertFalse(r.stronger)
        self.assertFalse(r.ties)
        with self.assertRaises(ValueError):
            river_holdings(["As", "Ks", "Qs", "6d"], ["Qs", "Js", "Ts", "4d", "3c"])
        with self.assertRaises(ValueError):
            river_holdings((), ())
