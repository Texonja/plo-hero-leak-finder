import re
import unittest
from dataclasses import replace

from leakfinder.demo import load_demo
from leakfinder.replay_view import hero_decisions
from leakfinder.river_critique import review_river
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import advice_at
from leakfinder.vendor.primitives import best_plo_hand


class RiverCritiqueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = connect(":memory:")
        initialize(cls.db)
        load_demo(cls.db)
        cls.addClassCleanup(cls.db.close)

    def decision(self, room, number, ref):
        row = self.db.execute("SELECT id,hint FROM hands WHERE room=? AND number=?", (room, number)).fetchone()
        return next((e, s) for e, s in hero_decisions(self.db, *row, "River") if e.ref == ref)

    def flag(self, snap, event):
        return review_river(snap, event, advice_at(snap))

    def verify_witness(self, snap, text, stronger=False):
        cards = re.search(r"((?:[2-9TJQKA][cdhs] ){3}[2-9TJQKA][cdhs]) \(", text).group(1).split()
        self.assertEqual(len({*snap.hero_cards, *snap.board, *cards}), 13)
        theirs = best_plo_hand(cards, snap.board)[:2]
        ours = best_plo_hand(snap.hero_cards, snap.board)[:2]
        self.assertTrue(theirs > ours if stronger else theirs < ours)

    def test_real_calls_on_both_networks_with_independent_witness_and_price(self):
        for room, number, ref, rule in (
            ("iPoker", "9000010026", "Action 17", "river_call_large_raise"),
            ("CoinPoker", "9000004202", "Line 41", "river_call_four_flush_board"),
        ):
            with self.subTest(room=room):
                e, s = self.decision(room, number, ref)
                self.assertEqual(e.action, "call")
                critique = self.flag(s, e)
                self.assertEqual(critique.rule, rule)
                self.assertEqual(critique.alternative, "Compare calling with folding")
                self.assertIn(f"{s.to_call / (s.pot + s.to_call):.1%}", critique.reason)
                self.verify_witness(s, critique.reason, stronger=True)
                self.verify_witness(s, critique.counterargument)
                if room == "CoinPoker":
                    self.assertIn("flush)", critique.reason)
                else:
                    self.assertIn("Q-T straights", critique.counterargument)
                self.assertIsNone(self.flag(s, replace(e, action="fold")))
                cheap = replace(s, to_call=1, price_to_match=1)
                self.assertIsNone(self.flag(cheap, e))
                self.assertIsNone(self.flag(replace(s, price_to_match=s.to_call + 1), e))

    def test_real_top_two_checkback_compares_a_smaller_value_bet(self):
        e, s = self.decision("CoinPoker", "9000002872", "Line 35")
        c = self.flag(s, e)
        self.assertEqual(c.rule, "river_check_back_top_two")
        guide = advice_at(s)
        self.assertIn(f"{guide.size.amount / s.bb:.2f} BB", c.reason)
        self.assertIn(f"{guide.size.amount / s.pot:.0%} pot", c.reason)
        self.verify_witness(s, c.reason)
        self.assertIsNone(self.flag(replace(s, history=tuple(a for a in s.history if a.street != "Turn")), e))
        self.assertIsNone(self.flag(replace(s, history=s.history[:-1]), e))  # First to speak may trap.

    def test_checked_lines_and_a_small_set_fold_are_only_conditional_reviews(self):
        cases = (
            ("CoinPoker", "9000009999", "Line 39", "river_broadway_checked_to", "King-high straight"),
            ("CoinPoker", "9000008585", "Line 44", "river_trips_board_boat", "BTN can still act"),
            ("iPoker", "9000014007", "Action 16", "river_set_small_lead", "20.0%"),
        )
        for room, number, ref, rule, evidence in cases:
            with self.subTest(number=number):
                event, snap = self.decision(room, number, ref)
                review = self.flag(snap, event)
                self.assertEqual(review.rule, rule)
                self.assertIn(evidence, review.reason)
                self.assertTrue(review.counterargument)
                self.assertFalse(advice_at(snap).can_compare(snap))
                other = "bet" if event.action == "check" else "call"
                self.assertIsNone(self.flag(snap, replace(event, action=other, amount=snap.to_call or snap.bb)))
                no_line = replace(snap, history=tuple(a for a in snap.history if a.street != "Turn"))
                self.assertIsNone(self.flag(no_line, event))

    def test_split_only_bluff_prices_better_folds_and_tying_folds_differently(self):
        e, s = self.decision("CoinPoker", "9000002872", "Line 35")
        s = replace(s, hero_cards=("2c", "3h", "4d", "5s"), board=("Jh", "8s", "8d", "Kc", "Ah"))
        amount = min(seat.stack for seat in s.seats if seat.live) // 2
        amount = min(amount, s.pot)
        idea = self.flag(s, replace(e, action="bet", amount=amount))
        self.assertEqual(idea.rule, "river_split_only_bluff")
        self.assertIn(f"{amount / (s.pot + amount):.2%}", idea.reason)
        self.assertIn("half the pot", idea.reason)
        self.assertIsNone(self.flag(s, e))

    def test_matched_history_can_defend_the_played_call_or_challenge_a_fold(self):
        e, s = self.decision("iPoker", "9000010026", "Action 17")
        history = replace(advice_at(s), title="River · opponent history", choices=("call",),
                          why="Previous comparable bets clear the call price.")
        self.assertIsNone(review_river(s, e, history))
        c = review_river(s, replace(e, action="fold"), history)
        self.assertEqual(c.rule, "river_fold_against_prior_bets")
        self.assertEqual(c.reason, history.why)
        self.assertIsNone(review_river(s, replace(e, action="raise"), history))

    def test_fold_history_is_size_specific_and_does_not_challenge_an_actual_bet(self):
        e, s = self.decision("CoinPoker", "9000002872", "Line 35")
        history = replace(advice_at(s), title="River · bluff supported by fold history", choices=("bet",),
                          why="Observed fold frequency clears the price at this size.")
        self.assertEqual(review_river(s, e, history).rule, "river_check_against_prior_folds")
        self.assertIsNone(review_river(s, replace(e, action="bet"), history))

    def test_a_different_lower_flush_price_and_river_raise(self):
        e, s = self.decision("CoinPoker", "9000002844", "Line 32")
        self.assertIsNone(self.flag(s, e))  # Both prior streets checked through.
        river_bet = s.history[-1]
        turn = [a for a in s.history if a.street == "Turn"]
        new_history = tuple(a for a in s.history if a.street not in ("Turn", "River")) + (
            replace(turn[0], action="bet", amount=45), replace(turn[1], action="call", amount=45), river_bet,
        )
        s = replace(s, hero_cards=("5d", "4d", "7s", "3c"), to_call=100, price_to_match=100,
                    pot=200, bb=1, history=new_history)
        c = self.flag(s, e)
        self.assertEqual(c.rule, "river_low_flush_second_bet")
        self.verify_witness(s, c.reason, stronger=True)
        self.verify_witness(s, c.counterargument)
        self.assertIsNone(self.flag(replace(s, history=s.history[:-1] + (replace(river_bet, action="raise"),)), e))
        pair = replace(s, hero_cards=("As", "Ad", "9h", "3c"), board=("2h", "8s", "Kd", "5d", "Qh"))
        c = review_river(pair, e, None)
        self.assertEqual(c.rule, "river_pair_second_barrel")
        self.verify_witness(pair, c.reason, stronger=True)
        self.verify_witness(pair, c.counterargument)
        paired = replace(pair, board=("2h", "8s", "Kd", "5d", "Kh"))
        self.assertIn("shared board pair", review_river(paired, e, None).reason)
        no_barrel = replace(pair, history=tuple(a for a in pair.history if a.street != "Turn"))
        self.assertIsNone(review_river(no_barrel, e, None))

    def test_flat_fold_and_raise_with_a_single_beating_hole_pair(self):
        e, original = self.decision("CoinPoker", "9000002844", "Line 32")
        s = replace(original, hero_cards=("As", "Ah", "2d", "4s"), board=("Qc", "Qd", "7s", "8h", "Ac"))
        for action, rule in (("call", "river_flat_value"), ("fold", "river_fold_near_nuts")):
            c = self.flag(s, replace(e, action=action))
            self.assertEqual(c.rule, rule)
            self.assertIn("Qh", c.reason)
            self.assertIn("Qs", c.reason)
            self.verify_witness(s, c.reason)
        self.assertIsNone(self.flag(s, replace(e, action="raise")))
        self.assertIsNone(self.flag(replace(s, can_raise=False), e))
        short = tuple(seat if seat.is_hero else replace(seat, stack=1) for seat in s.seats)
        self.assertIsNone(self.flag(replace(s, seats=short), e))

    def test_a_small_value_bet_with_four_beating_pairs_requires_the_action_to_close(self):
        e, original = self.decision("CoinPoker", "9000002872", "Line 35")
        s = replace(original, hero_cards=("Ks", "Js", "Td", "2c"), board=("Kc", "Jc", "6h", "Qs", "Jd"))
        c = self.flag(s, e)
        self.assertEqual(c.rule, "river_check_back_value")
        self.assertIn("4 available", c.reason)
        self.verify_witness(s, c.reason)
        self.assertIsNone(self.flag(replace(s, history=s.history[:-1]), e))
        # A weak boat on the same board can run into many larger boats.
        self.assertIsNone(self.flag(replace(s, hero_cards=("6d", "6s", "Qd", "2c")), e))

    def test_pot_sized_unblocked_bluff_needs_folds_with_rake_and_showdown_caveats(self):
        e, original = self.decision("CoinPoker", "9000002872", "Line 35")
        s = replace(original, hero_cards=("8d", "7s", "3d", "2h"), board=("Ah", "Qc", "6c", "5s", "Kc"))
        bet = replace(e, action="bet", amount=s.pot)
        c = self.flag(s, bet)
        self.assertEqual(c.rule, "river_large_bluff_unblocked_flush")
        self.assertIn("50.0% folds", c.reason)
        self.verify_witness(s, c.reason, stronger=True)
        self.assertIsNone(self.flag(s, replace(bet, amount=s.pot // 3)))
        self.assertIsNone(self.flag(replace(s, hero_cards=("8c", "7s", "3d", "2h")), bet))
        self.assertIsNone(self.flag(replace(s, history=tuple(a for a in s.history if a.street != "Turn")), bet))

    def test_tiny_nut_bet_compares_relative_call_volume_at_legal_effective_size(self):
        e, original = self.decision("CoinPoker", "9000002872", "Line 35")
        s = replace(original, hero_cards=("Ac", "Kc", "9h", "7h"), board=("3c", "6s", "8c", "Qc", "Js"))
        small = replace(e, action="bet", amount=s.pot // 10)
        c = review_river(s, small, None)
        self.assertEqual(c.rule, "river_small_nut_bet")
        self.assertIn("20% as many calls", c.reason)
        self.verify_witness(s, c.reason)
        self.assertIsNone(self.flag(s, replace(small, amount=s.pot // 2)))
        covered = replace(s, seats=tuple(seat if seat.is_hero else replace(seat, stack=small.amount) for seat in s.seats))
        self.assertIsNone(self.flag(covered, small))

    def test_multiway_even_with_all_in_opponent_is_excluded(self):
        e, s = self.decision("CoinPoker", "9000004202", "Line 41")
        extra = next(a for a in s.seats if not a.live)
        multi = replace(s, seats=tuple(replace(a, live=True, all_in=True, stack=0) if a == extra else a for a in s.seats))
        self.assertIsNone(self.flag(multi, e))
