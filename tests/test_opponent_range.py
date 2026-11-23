import unittest
from dataclasses import replace

from leakfinder.opponent_range import condition_range
from leakfinder.replay_view import ReplayAction
from leakfinder.river_holdings import DECK
from leakfinder.vendor.primitives import best_plo_hand

from tests.test_replay_view import coin, ip


def rival(snap):
    return next(s for s in snap.seats if s.live and s.name != snap.hero)


class RiverRangeTests(unittest.TestCase):
    def test_actual_coin_and_ipoker_legal_hands_and_normalized_stages(self):
        for view in [coin(1, "Hero", "Line 37"), ip(41, "BigNeonMax", "Action 10")]:
            opponent = rival(view)
            with self.subTest(room=view.room):
                result = condition_range(view, opponent, 384)
                self.assertEqual(result, condition_range(view, opponent.name, 384))
                self.assertEqual(result.legal_total, 123410)
                self.assertEqual(result.sample_size, 384)
                self.assertEqual(sum(result.unweighted_counts.values()), 384)
                self.assertEqual(
                    [s.street for s in result.stages],
                    ["Preflop", "Flop", "Turn", "River"],
                )
                refs = [a.ref for a in view.history if a.name == opponent.name]
                self.assertTrue(any(s.actions for s in result.stages))
                for s in result.stages:
                    self.assertAlmostEqual(s.stronger + s.tied + s.weaker, 1)
                    self.assertAlmostEqual(sum(s.categories.values()), 1)
                    self.assertEqual(len(s.by_strength), 5)
                    for sum_col, share in zip(
                        map(sum, zip(*s.by_strength)), (s.weaker, s.tied, s.stronger)
                    ):
                        self.assertAlmostEqual(sum_col, share)
                    if s.current_relation:
                        self.assertAlmostEqual(sum(s.current_relation), 1.0)
                    self.assertEqual(
                        sum(s.category_counts.values()), result.sample_size
                    )
                    self.assertGreater(s.effective_n, 0)
                    self.assertLessEqual(s.effective_n, result.sample_size + 1e-8)
                    self.assertEqual(len(s.rules), len(s.actions))
                    for a in s.actions:
                        self.assertTrue(any(f"[{ref}]" in a for ref in refs))
                        self.assertNotIn("[" + view.ref + "]", a)
                    for role, e in s.examples.items():
                        self.assertEqual(len(set(e.cards)), 4)
                        self.assertFalse(
                            set(e.cards) & set(view.board + view.hero_cards)
                        )
                        rank, kicks, *_ = best_plo_hand(e.cards, view.board)
                        hero_cat, hero_kicks, *_ = best_plo_hand(
                            view.hero_cards, view.board
                        )
                        self.assertEqual((rank, tuple(kicks)), (e.category, e.kickers))
                        if role == "value":
                            self.assertGreater((rank, kicks), (hero_cat, hero_kicks))
                        elif role in ("weak_call", "bluff"):
                            self.assertLess((rank, kicks), (hero_cat, hero_kicks))
                        if role == "bluff":
                            self.assertLessEqual(rank, 1)
                river = result.stages[-1]
                for current, final in zip(
                    river.current_relation, (river.weaker, river.tied, river.stronger)
                ):
                    self.assertAlmostEqual(current, final)

    def test_prior_streets_have_no_knowledge_of_later_actions(self):
        v = coin(1, "Hero", "Line 37")
        opp = rival(v)
        past = tuple(a for a in v.history if a.street != "River")
        pot = sum(-a.amount if a.action == "return" else a.amount for a in past)
        clean = replace(v, history=past, pot=pot)
        before = condition_range(clean, opp, 512)

        def wager(fraction):
            size = round(pot * fraction)
            move = ReplayAction(
                "River", opp.name, "bet", size, "synthetic-priced-bet", None, False
            )
            return condition_range(
                replace(clean, history=past + (move,), pot=pot + size), opp, 512
            )

        small, large = wager(0.25), wager(0.8)
        self.assertEqual(before.stages[:3], large.stages[:3])
        self.assertEqual(small.stages[:3], large.stages[:3])
        self.assertEqual(before.stages[-2].stronger, before.stages[-1].stronger)
        self.assertIn("80%", large.stages[-1].actions[0])
        self.assertNotEqual(small.stages[-1].stronger, large.stages[-1].stronger)
        self.assertEqual(
            small.stages[-1].category_counts, large.stages[-1].category_counts
        )

    def test_intensity_has_identical_cards_and_different_concentration(self):
        s = coin(1, "Hero", "Line 37")
        softer, sharper = [
            condition_range(s, rival(s), 384, intensity=x) for x in (0.75, 1.25)
        ]
        self.assertEqual(softer.unweighted_counts, sharper.unweighted_counts)
        self.assertEqual(
            softer.stages[-1].category_counts, sharper.stages[-1].category_counts
        )
        self.assertGreater(
            softer.stages[-1].effective_n, sharper.stages[-1].effective_n
        )
        self.assertIn("1.25", " ".join(sharper.stages[0].rules))

    def test_street_cards_and_draws_in_complete_four_card_holdings(self):
        from itertools import combinations

        from leakfinder.opponent_range import BoardFeatures
        from leakfinder.river_holdings import CARD_ID

        hand = tuple(sorted(CARD_ID[c] for c in ["Ks", "Kh", "As", "9c"]))
        flop = tuple(CARD_ID[c] for c in ["Js", "8s", "7d"])
        turn = flop + (CARD_ID["Qs"],)
        for board, draw in [(flop, True), (turn, False)]:
            f = BoardFeatures(board).evaluate(hand, tuple(combinations(hand, 2)))
            self.assertEqual(f.draw, draw)
            rank, _, *_ = best_plo_hand(
                [DECK[c] for c in hand], [DECK[c] for c in board]
            )
            self.assertEqual(f.score // 16**5, rank)

    def test_validation_and_scope(self):
        v = coin(1, "Hero", "Line 37")
        self.assertIsNone(condition_range(v, v.hero))
        self.assertIsNone(condition_range(replace(v, hero_cards=None), rival(v)))
        self.assertIsNone(condition_range(replace(v, street="Turn"), rival(v)))
        with self.assertRaises(ValueError):
            condition_range(v, rival(v), 999999)
