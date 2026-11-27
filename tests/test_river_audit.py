"""Counterexamples to assigning a single river action from a made-hand category."""

import unittest
from contextlib import closing
from dataclasses import replace
from itertools import combinations

from leakfinder.demo import load_demo
from leakfinder.opponent_history import OpponentHistory, apply_history, context, heads_up_price
from leakfinder.postflop_value import bet_size
from leakfinder.replay_view import hero_decisions, load_snapshot
from leakfinder.river_guide import _history
from leakfinder.river_holdings import DECK, river_holdings
from leakfinder.range_review import river_price_note
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import advice_at
from leakfinder.vendor.primitives import best_plo_hand


class RiverAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)
        load_demo(self.db)

    def snapshot(self, room, number, ref):
        with closing(
            self.db.execute(
                "SELECT id,hint FROM hands WHERE room=? AND number=?", (room, number)
            )
        ) as cursor:
            hid, hero = cursor.fetchone()
        return load_snapshot(self.db, hid, hero, ref)

    def test_demo_opening_and_closing_blind_defense(self):
        def first(room, number):
            row = self.db.execute("SELECT id,hint FROM hands WHERE room=? AND number=?", (room, number)).fetchone()
            return next(hero_decisions(self.db, *row, "Preflop"))[1]
        aa = advice_at(first("CoinPoker", "9000007003"))
        self.assertEqual(aa.choices, ("raise",))
        self.assertIn("value 4-bet", aa.why)
        weak = advice_at(first("CoinPoker", "9000005631"))
        self.assertEqual(weak.choices, ("fold",))
        self.assertNotIn("steal", weak.why)
        defense = first("iPoker", "9000008031")
        why = advice_at(defense).why
        self.assertIn("Calling is a reasonable defense", why)
        self.assertIn("33.3%", why)
        self.assertNotIn("Folding is reasonable", why)

    def test_a_river_blocker_is_not_automatically_a_missed_draw(self):
        snap = self.snapshot("CoinPoker", "9000005612", "Line 31")
        why = advice_at(snap).why
        self.assertNotIn("Ad 5s 2c 3c", why)
        self.assertFalse(any(ex.holding == ("Ad", "5s", "2c", "3c")
                             for ex in river_holdings(snap.hero_cards, snap.board).missed_straight))
        turn = snap.board[:4]
        hole = ("Ad", "5s", "2c", "3c")
        for river in set(DECK) - set(snap.hero_cards + turn + hole):
            self.assertNotIn(best_plo_hand(hole, (*turn, river))[0], (4,5,8))

    def test_checked_through_nut_straight_and_non_nut_boats_offer_size_comparison(self):
        for room, number, ref, text in (
            ("iPoker", "9000005983", "Action 13", "weaker straights"),
            ("iPoker", "9000000095", "Action 13", "weaker calls"),
            ("CoinPoker", "9000001429", "Line 40", "weaker calls"),
        ):
            snap = self.snapshot(room, number, ref)
            guide = advice_at(snap)
            self.assertEqual(guide.choices[0], "bet")
            self.assertIn(text, guide.why)
            self.assertIn("Compare checking", guide.why)
            self.assertIsNone(guide.size)

    def test_demo_bottom_set_on_four_broadway_cards_has_no_default_bet_size(self):
        snap = self.snapshot("iPoker", "9000012016", "Action 13")
        guide = advice_at(snap)
        self.assertIn("bottom set", guide.why.lower())
        self.assertIn("straight", guide.why.lower())
        self.assertEqual(guide.choices, ())
        self.assertIsNone(guide.size)
        self.assertFalse(guide.can_compare(snap))

    def test_weakest_possible_four_card_holding_can_only_split(self):
        from unittest.mock import Mock
        snap = replace(
            self.snapshot("CoinPoker", "9000002844", "Line 32"),
            hero_cards=("2c", "3h", "4d", "5s"),
            board=("Jh", "8s", "8d", "Kc", "Ah"),
        )
        guide = advice_at(snap)
        self.assertIn("only tie or lose", guide.why)
        self.assertIn("50.0%", guide.why)
        self.assertFalse(guide.rankable)
        self.assertNotIn("Possible bluffs", guide.why)
        history = Mock()
        history.sample.return_value.ready = True
        self.assertEqual(apply_history(snap, guide, history), guide)
        note = river_price_note(snap, OpponentHistory(()))
        self.assertIn("50.0% splits", note)
        self.assertNotIn("bluff-catcher", note)
        check = advice_at(replace(snap, to_call=0, price_to_match=0, can_raise=True))
        self.assertIn("no weaker caller", check.why)

    def test_closing_multiway_call_uses_only_possible_showdown_shares(self):
        snap = self.snapshot("iPoker", "9000005984", "Action 16")
        facing = max(s.committed for s in snap.seats)
        seats = tuple(
            replace(s, committed=facing) if s.live and not s.is_hero else s
            for s in snap.seats
        )
        guide = advice_at(replace(snap, seats=seats,
                                 hero_cards=("2c", "3h", "4d", "5s"),
                                 board=("Jh", "8s", "8d", "Kc", "Ah")))
        self.assertIn("Your call closes the betting", guide.why)
        split_price = sum(s.live for s in seats) * snap.to_call / (snap.to_call + snap.pot)
        self.assertIn(f"{split_price:.1%}", guide.why)
        self.assertFalse(guide.rankable)

    def test_a_missed_flush_and_straight_draw_is_one_bluff_example(self):
        snap = replace(self.snapshot("CoinPoker", "9000002844", "Line 32"),
                       hero_cards=("As", "4d", "Kh", "3s"),
                       board=("Qs", "Th", "Kc", "4s", "2d"))
        holdings = river_holdings(snap.hero_cards, snap.board)
        self.assertEqual(holdings.missed_flush[0].holding, holdings.missed_straight[0].holding)
        self.assertNotIn(holdings.missed_flush[0].text, advice_at(snap).why)

    def test_barrels_and_missed_draw_removal_respect_prior_action(self):
        from leakfinder.river_guide import _barrel_blockers
        from tests.test_strategy_guide import view, move
        prior = [move("BB", "bet", 5, "Turn"), move("BTN", "call", 5, "Turn"),
                 move("BB", "bet", 20, "River")]
        snap = view("Ts 6c 7h 5h", "BTN", street="River", board="4c Kh Js Ah 3d",
                    prior=prior, owed=20, pot=40)
        bettor = next(s for s in snap.seats if s.name == "BB")
        note = _barrel_blockers(snap, bettor, 1/3, 0)
        self.assertIn("turn 50% pot, river 100% pot", note)
        self.assertIn("7h 5h", note)
        self.assertIn("lose to a bluff", note)
        self.assertEqual(_barrel_blockers(replace(snap, board=(*snap.board[:4], "3h")), bettor, 1/3, 0), "")
        self.assertEqual(_barrel_blockers(replace(snap, history=tuple(prior[2:])), bettor, 1/3, 0), "")

    def test_no_four_card_example_printed_as_both_weaker_value_and_bluff(self):
        import re
        snap = replace(self.snapshot("CoinPoker", "9000002844", "Line 32"),
                       hero_cards=("9c", "4d", "8h", "2d"), board=("Th", "6s", "6h", "Jc", "Ts"))
        guide = advice_at(snap).why
        self.assertNotRegex(guide, r"(?:[2-9TJQKA][cdhs] ){3}[2-9TJQKA][cdhs]")
        self.assertFalse(advice_at(snap).choices)

    def test_identical_public_line_can_favor_either_call_or_fold(self):
        # Each side of each example is a complete legal four-card holding.
        for room, number, ref, weaker, stronger in (
            ("CoinPoker", "9000002844", "Line 32", "Jd Td 9h 8h", "Ad Kd 6c 7c"),
            ("iPoker", "9000010026", "Action 17", "Ac Ad Kh Kd", "Tc 7c As 2d"),
        ):
            with self.subTest(room=room):
                snap = self.snapshot(room, number, ref)
                own = best_plo_hand(snap.hero_cards, snap.board)[:2]
                for packet, expected_weak in ((weaker, True), (stronger, False)):
                    cards = packet.split()
                    self.assertEqual(len({*cards, *snap.board, *snap.hero_cards}), 13)
                    their = best_plo_hand(cards, snap.board)[:2]
                    self.assertNotEqual(their, own)
                    self.assertEqual(their < own, expected_weak)
                price = heads_up_price(snap)
                for win, positive in ((0.15, False), (0.5, True)):
                    call_gain = win * snap.pot - (1 - win) * snap.to_call
                    self.assertEqual(call_gain > 0, positive)
                    self.assertEqual(win > price, positive)
                guide = advice_at(snap)
                self.assertFalse(guide.choices)
                for action in ("raise", "fold", "call"):
                    self.assertIsNone(guide.difference(action, snap.to_call))
                self.assertFalse(guide.rankable)

    def test_short_call_uses_contestable_pot_or_defers_in_multiway_spot(self):
        snap = self.snapshot("CoinPoker", "9000002844", "Line 32")
        short = replace(snap, price_to_match=100, to_call=10, pot=120, bb=1)
        self.assertEqual(heads_up_price(short), 0.25)
        self.assertFalse(advice_at(short).choices)
        multi = self.snapshot("iPoker", "9000005984", "Action 16")
        side = advice_at(replace(multi, price_to_match=100, to_call=10, pot=120))
        self.assertIn("side pot", side.title)
        self.assertEqual(side.choices, ())
        self.assertNotIn("%", side.why)

    def test_tiny_raise_cannot_decide_a_bluff_catch(self):
        snap = self.snapshot("iPoker", "9000010026", "Action 17")
        snap = replace(snap, pot=99, to_call=1, price_to_match=1, bb=1)
        guide = advice_at(snap)
        self.assertAlmostEqual(heads_up_price(snap), .01)
        self.assertEqual(guide.choices, ())
        self.assertIsNone(guide.difference("call", 1))
        self.assertIsNone(guide.difference("fold", 0))

    def test_value_raise_review_requires_exactly_one_beating_pair_and_legal_raise(self):
        source = self.snapshot("CoinPoker", "9000002844", "Line 32")
        board = ("Qc", "Qd", "7s", "8h", "Ac")
        for cards, unique in (
            (("As", "Ah", "2d", "4s"), True),
            (("7c", "7d", "2d", "4s"), False),
        ):
            own = best_plo_hand(cards, board)[:2]
            remaining = set(DECK).difference(cards, board)
            better = [
                pair
                for pair in combinations(remaining, 2)
                if best_plo_hand(pair, board)[:2] > own
            ]
            self.assertEqual(
                river_holdings(cards, board).stronger_pair_count, len(better)
            )
            self.assertEqual(len(better) == 1, unique)
            for legal in (True, False):
                guide = advice_at(
                    replace(source, hero_cards=cards, board=board, can_raise=legal)
                )
                self.assertEqual(
                    "Only one hole-card pair" in guide.why, unique and legal
                )
                if unique and legal:
                    self.assertIn("value raise needs calls from worse", guide.why)
                    self.assertFalse(guide.rankable)

    def test_multiway_line_keeps_each_check_bet_and_pending_player(self):
        for room, number, ref, first, bettor in (
            ("iPoker", "9000005984", "Action 16", "SB", "BB"),
            ("CoinPoker", "9000000024", "Line 43", "HJ", "CO"),
        ):
            with self.subTest(room=room):
                snap = self.snapshot(room, number, ref)
                why = advice_at(snap).why
                self.assertIn(f"{first} checked the river.", why)
                self.assertIn(f"{bettor} bet ", why)
                self.assertIn(f"{first} can still act behind you.", why)
                hid = self.db.execute(
                    "SELECT id FROM hands WHERE room=? AND number=?", (room, number)
                ).fetchone()[0]
                last_bet = next(
                    a for a in snap.history if a.street == "River" and a.action == "bet"
                )
                before = load_snapshot(self.db, hid, last_bet.name, last_bet.ref)
                players = [s for s in before.seats if s.live and not s.is_hero]
                actions = [a for a in before.history if a.street == "River"]
                line, *_ = _history(before, players, actions)
                self.assertIn(f"{first} checked the river.", line)
                self.assertIn("BTN can still act after you", line)
                self.assertNotIn(f"{bettor} bet ", line)

    def test_history_keeps_three_suit_and_four_suit_boards_and_all_ins_separate(self):
        snap = self.snapshot("CoinPoker", "9000002844", "Line 32")
        actor = next(s.name for s in snap.seats if s.live and not s.is_hero)
        initial = context(snap, actor, 25, 100)
        four_flush = replace(snap, board=("Ks", "Ad", "9d", "8d", "2d"))
        self.assertNotEqual(initial, context(four_flush, actor, 25, 100))
        short = replace(
            snap,
            seats=tuple(
                replace(s, stack=0, committed=25, all_in=True) if s.name == actor else s
                for s in snap.seats
            ),
        )
        self.assertNotEqual(initial, context(short, actor, 25, 100))

    def test_size_illustration_uses_only_money_opponent_can_match(self):
        snap = self.snapshot("iPoker", "9000009995", "Action 13")
        hero = next(s for s in snap.seats if s.is_hero)
        opp = next(s for s in snap.seats if s.live and not s.is_hero)
        # Billing one full blind would double the risk when the caller has half.
        snap = replace(snap, bb=100, pot=500)
        size = bet_size(snap, hero, [replace(opp, stack=50)], 65)
        self.assertEqual((size.amount, size.contestable), (100, 50))
        self.assertAlmostEqual(size.contestable / (snap.pot + size.contestable), 1 / 11)
