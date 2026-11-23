"""Flop/turn audit against public histories and fully enumerated counterexamples."""

import unittest
from collections import Counter
from dataclasses import replace
from itertools import combinations

from leakfinder.demo import SAMPLES
from leakfinder.early_postflop import highest_flush_draw, next_draws
from leakfinder.importers import SCALE, sources
from leakfinder.opponent_history import OpponentHistory, postflop_opponent
from leakfinder.postflop_value import line_context
from leakfinder.range_review import build_range_review
from leakfinder.replay_view import _ledger, _snapshot, snapshot_from_record
from leakfinder.river_holdings import CARD_ID, UNIT, _score, _scores
from leakfinder.strategy_guide import advice_at
from leakfinder.vendor.primitives import best_plo_hand, eval_5
from tests.test_strategy_guide import move, view


def spot(hole, board, *, owed=0, pot=100):
    street = "Flop" if len(board.split()) == 3 else "Turn"
    prior = [move(p, "fold") for p in ("UTG", "HJ", "CO", "SB")]
    prior.append(move("BB", "bet" if owed else "check", owed, street))
    return view(hole, street=street, board=board, prior=prior, owed=owed, pot=pot)


def equity(hole, villain, board):
    """Exact showdown share against one hypothetical holding; no further betting."""
    own, other, visible = (
        tuple(CARD_ID[c] for c in holding.split()) for holding in (hole, villain, board)
    )
    excluded = set(own + other + visible)
    assert len(excluded) == len(own + other + visible)
    deck = sorted(set(range(52)) - excluded)
    wins = ties = total = 0
    for runout in combinations(deck, 5 - len(visible)):
        scores = _scores(visible + runout)
        left, right = (
            max(_score(a, b, *scores) for a, b in combinations(cards, 2))
            for cards in (own, other)
        )
        wins += left > right
        ties += left == right
        total += 1
    return (wins + ties / 2) / total


class EarlyAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = {}
        cls.snapshots = {}
        for file in (
            SAMPLES / "CoinPoker-HeroDemo.txt",
            SAMPLES / "iPoker-HeroDemo.sql",
        ):
            for record in sources(file):
                hand, events = _ledger(record)
                cls.records[hand.room, hand.number] = record
                for ref, event in events.items():
                    if event.player == hand.hint and event.street in {"Flop", "Turn"}:
                        snap = _snapshot(record, hand, events, hand.hint, ref)
                        if snap.hero_cards:
                            cls.snapshots[hand.room, hand.number, ref] = snap

    def test_public_corpus_agrees_on_all_made_hands_and_legal_actions(self):
        counts = Counter()
        for key, snap in self.snapshots.items():
            with self.subTest(key=key):
                counts[snap.room, snap.street] += 1
                rank, kickers, *_ = best_plo_hand(snap.hero_cards, snap.board)
                scores = _scores(tuple(CARD_ID[c] for c in snap.board))
                actual = max(
                    _score(CARD_ID[a], CARD_ID[b], *scores)
                    for a, b in combinations(snap.hero_cards, 2)
                )
                expected = rank * UNIT + sum(
                    k << (16 - 4 * i) for i, k in enumerate(kickers)
                )
                self.assertEqual(actual, expected)
                allowed = {"call", "fold"} if snap.to_call else {"check"}
                if snap.can_raise:
                    allowed.add("raise" if snap.to_call else "bet")
                advice = advice_at(snap)
                self.assertTrue(set(advice.choices) <= allowed)
        self.assertEqual(
            counts,
            {
                ("CoinPoker", "Flop"): 99,
                ("CoinPoker", "Turn"): 53,
                ("iPoker", "Flop"): 86,
                ("iPoker", "Turn"): 54,
            },
        )

    def test_public_capped_prices_and_future_board_independence(self):
        cases = (
            (
                "CoinPoker",
                "9000005614",
                "Line 40",
                32.59,
                33.72,
                71.86,
                "Line 36",
                "[5h]",
                "[2c]",
            ),
            (
                "iPoker",
                "9000008034",
                "Action 13",
                33.25,
                54,
                108,
                "Action 11",
                ">CJ</cards>",
                ">DT</cards>",
            ),
        )
        for room, number, ref, call, uncapped, pot, flop_ref, old, new in cases:
            with self.subTest(room=room):
                snap = self.snapshots[room, number, ref]
                self.assertEqual(
                    (snap.to_call, snap.price_to_match, snap.pot),
                    tuple(round(v * SCALE) for v in (call, uncapped, pot)),
                )
                paid_pot = pot + call - (uncapped - call)
                advice = advice_at(snap)
                self.assertIn(f"{call / paid_pot:.1%}", advice.why)
                self.assertFalse(advice.choices)
                record = self.records[room, number]
                changed = replace(record, raw=record.raw.replace(old, new))
                self.assertNotEqual(record.raw, changed.raw)
                # A different turn must have no effect on the preceding flop decision.
                before = self.snapshots[room, number, flop_ref]
                after = snapshot_from_record(changed, before.hero, flop_ref)
                self.assertEqual(before, after)
                self.assertEqual(advice_at(before), advice_at(after))

    def test_released_check_only_flags_get_hand_review_on_both_rooms(self):
        keys = (
            ("CoinPoker", "9000001431", "Line 32"),
            ("CoinPoker", "9000002857", "Line 28"),
            ("CoinPoker", "9000004202", "Line 34"),
            ("CoinPoker", "9000004222", "Line 32"),
            ("iPoker", "9000008006", "Action 11"),
            ("iPoker", "9000011994", "Action 14"),
        )
        for key in keys:
            with self.subTest(key=key):
                snap = self.snapshots[key]
                hand, events = _ledger(self.records[key[:2]])
                event = events[key[2]]
                self.assertEqual(
                    (event.player, event.street, event.action),
                    (hand.hint, "Flop", "bet"),
                )
                advice = advice_at(snap)
                self.assertFalse(advice.choices)
                self.assertIn("fold", advice.why)
                self.assertIsNone(advice.difference("bet", event.amount))

    def test_later_revealed_opponent_cards_never_enter_flop_advice(self):
        cases = (
            ("CoinPoker", "9000000003", "Line 32", "Jh 9h Jc 3d", "Kh 9h Kc 3d"),
            ("iPoker", "9000000073", "Action 10", "SJ C5 D10 C8", "X X X X"),
        )
        for room, number, ref, old, new in cases:
            with self.subTest(room=room):
                record = self.records[room, number]
                changed = replace(record, raw=record.raw.replace(old, new))
                original, _ = _ledger(record)
                alternate, _ = _ledger(changed)
                self.assertNotEqual(original.cards, alternate.cards)
                before = self.snapshots[room, number, ref]
                after = snapshot_from_record(changed, before.hero, ref)
                self.assertEqual(before, after)
                self.assertEqual(advice_at(before), advice_at(after))

    def test_current_nuts_can_be_freerolled_or_far_ahead(self):
        hole, board = "Th Jd 2c 3h", "7s 8s 9d"
        snap = spot(hole, board, owed=50, pot=100)
        self.assertEqual(equity(hole, "Ts Js 9s 9c", board), 303 / 1640)
        self.assertEqual(equity(hole, "4c 5h 7d 6d", board), 756 / 820)
        advice = advice_at(snap)
        self.assertIn("current nut straight", advice.why)
        self.assertIn("33.3%", advice.why)
        self.assertFalse(advice.choices)
        royal = spot("As Ks 3h 4c", "Qs Js Ts", owed=50, pot=100)
        self.assertIn("call", advice_at(royal).choices)

    def test_high_card_and_combo_draw_prices_need_a_range(self):
        hole, board = "As Qc 8h 3d", "Ks 9d 5c 2h"
        self.assertEqual(equity(hole, "9s 9h Ah 4s", board), 3 / 40)
        self.assertEqual(equity(hole, "Qh Jc 7s 6c", board), 24 / 40)
        snap = spot(hole, board, owed=40, pot=100)
        self.assertFalse(advice_at(snap).choices)
        hole, board = "As Ks 7h 8h", "5s 6s Qd 2h"
        snap = spot(hole, board, owed=20, pot=80)
        draws = next_draws(snap.hero_cards, snap.board)
        self.assertEqual((draws.nuts, draws.total), (8, 15))
        self.assertLess(equity(hole, "2s 7s 8s Qc", board), 0.20)
        self.assertGreater(equity(hole, "Jh Jc 3h 4c", board), 0.20)
        self.assertFalse(advice_at(snap).choices)

    def test_checked_to_the_nuts_excludes_bet_fold_sizing(self):
        snap = spot("As Ks 3h 4c", "Qs Js Ts")
        hero = next(s for s in snap.seats if s.is_hero)
        result = build_range_review(
            snap, hero, postflop_opponent(snap), OpponentHistory()
        )
        self.assertFalse(result.historical_comparison)
        self.assertFalse(result.models)
        self.assertIn("current nuts", result.text)

    def test_boat_and_flush_draw_counts_can_share_river_cards(self):
        snap = spot("As Ks Jd Jh", "Js 6s Qc 4d", owed=20, pot=80)
        why = advice_at(snap).why
        self.assertIn("10 cards can make a full house or quads", why)
        self.assertIn("overlap", why)
        for river in ("Qs", "4s"):
            ranks = {
                eval_5(h + b)[0]
                for h in combinations(snap.hero_cards, 2)
                for b in combinations(snap.board + (river,), 3)
            }
            self.assertTrue({5, 6} <= ranks)

    def test_live_flush_blocker_pocket_trips_and_previous_street_pot(self):
        board = tuple("As 5s 8c 2d".split())
        self.assertTrue(highest_flush_draw(("Ks", "3s", "6d", "7h"), board))
        self.assertFalse(highest_flush_draw(("Qs", "3s", "6d", "7h"), board))
        self.assertFalse(highest_flush_draw(("Ks", "3d", "6d", "7h"), board))
        triple = spot("Ah Ad Ac 4c", "As 9d 2c")
        why = advice_at(triple).why
        self.assertIn("lower sets", why)
        self.assertNotIn("some Ax", why)
        self.assertIn(
            "some Ax",
            advice_at(replace(triple, hero_cards=("Ah", "Ad", "Ks", "4c"))).why,
        )
        turn = spot("Ah Ad Ac 4c", "As 9d 2c 5d", owed=10)
        current = turn.history[-1]
        for amount, large in ((4, False), (8, True)):
            prior = (
                move("BTN", "bet", amount, "Flop"),
                move("BB", "call", amount, "Flop"),
            )
            snap = replace(
                turn, pot=10 + 2 * amount + current.amount, history=(*prior, current)
            )
            hero = next(s for s in snap.seats if s.is_hero)
            self.assertEqual(
                line_context(snap, hero, [postflop_opponent(snap)], [current])[3], large
            )

    def test_multiway_value_targets_follow_board_and_hero_strength(self):
        pair = view("Kh Qh 9s 8s", street="Flop", board="5d Ac Ad", owed=0, pot=10)
        self.assertIn("the board's pair", advice_at(pair).why)
        self.assertIn("trips or a full house", advice_at(pair).why)
        self.assertNotIn("sets", advice_at(pair).why.lower())
        high = replace(pair, board=("5d", "Tc", "2d"))
        self.assertIn("high card", advice_at(high).why)
        self.assertNotIn("thin value", advice_at(high).why)
        turn = view("2h 4c Jd Ad", street="Turn", board="8c Qc Tc 4s", owed=0, pot=10)
        self.assertIn("two cards of the board's long suit", advice_at(turn).why)
        self.assertIn("a flush", advice_at(turn).why)
        self.assertNotIn("thin value", advice_at(turn).why)

    def test_rainbow_boards_have_different_draws_and_quads_can_induce(self):
        connected = spot("Qh Qc Td 3s", "Qd 7h 5c")
        self.assertIn("straight draws", advice_at(connected).why)
        unconnected = replace(connected, board=("Qd", "7h", "2c"))
        self.assertIn(
            "Neither a straight nor a flush can complete", advice_at(unconnected).why
        )
        quads = spot("Qh Qd Kc 9s", "Qs Qc 8h")
        self.assertIn("can induce a bet", advice_at(quads).why)

    def test_overpair_can_value_bet_after_a_missed_cbet_with_few_outs(self):
        prior = [move(p, "fold") for p in ("UTG", "CO", "SB", "BB")]
        prior.extend(
            (
                move("HJ", "raise", 3),
                move("BTN", "call", 3),
                move("HJ", "check", street="Flop"),
            )
        )
        snap = view(
            "Ac Ad 9h 5s", street="Flop", board="Jh 8c 4h", prior=prior, owed=0, pot=8
        )
        why = advice_at(snap).why
        self.assertIn("overpair", why)
        self.assertIn("weaker pairs call", why)
