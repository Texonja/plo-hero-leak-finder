"""Counterexamples to assigning a single river action from a made-hand category."""

import unittest
from contextlib import closing
from dataclasses import replace
from itertools import combinations

from leakfinder.demo import load_demo
from leakfinder.opponent_history import context, heads_up_price
from leakfinder.postflop_value import bet_size
from leakfinder.replay_view import load_snapshot
from leakfinder.river_guide import _history
from leakfinder.river_holdings import DECK, river_holdings
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
                self.assertIn(f"{price:.1%}", guide.why)
                for action in ("raise", "fold", "call"):
                    self.assertIsNone(guide.difference(action, snap.to_call))
                self.assertFalse(guide.rankable)

    def test_short_call_uses_contestable_pot_or_defers_in_multiway_spot(self):
        snap = self.snapshot("CoinPoker", "9000002844", "Line 32")
        short = replace(snap, price_to_match=100, to_call=10, pot=120, bb=1)
        self.assertEqual(heads_up_price(short), 0.25)
        self.assertIn("25.0%", advice_at(short).why)
        multi = self.snapshot("iPoker", "9000005984", "Action 16")
        side = advice_at(replace(multi, price_to_match=100, to_call=10, pot=120))
        self.assertIn("side pot", side.title)
        self.assertEqual(side.choices, ())
        self.assertNotIn("%", side.why)

    def test_tiny_raise_cannot_decide_a_bluff_catch(self):
        snap = self.snapshot("iPoker", "9000010026", "Action 17")
        snap = replace(snap, pot=99, to_call=1, price_to_match=1, bb=1)
        guide = advice_at(snap)
        self.assertIn("1.0%", guide.why)
        self.assertIn("raises specifically", guide.why)
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
                    for card in better[0]:
                        self.assertIn(card, guide.why)
                    self.assertIn("value raise if worse hands pay", guide.why)
                    self.assertIn("only better hands continue", guide.why)
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
