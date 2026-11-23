"""Check chronological flop and turn ranges against recorded decisions."""

import unittest
from datetime import timedelta
from math import comb

from leakfinder.early_range import FLOP_RUNOUTS, _sample, early_ranges
from leakfinder.opponent_history import (
    Observation,
    OpponentHistory,
    clock,
    context,
    postflop_opponent,
)
from leakfinder.river_holdings import CARD_ID, DECK
from leakfinder.vendor.primitives import best_plo_hand

from tests.test_replay_view import coin, ip


class EarlyRangeTests(unittest.TestCase):
    def test_real_hands_have_no_future_cards_or_retroactive_flop_changes(self):
        for flop, turn in (
            (coin(30, "Hero", "Line 28"), coin(30, "Hero", "Line 31")),
            (ip(601, "BigNeonMax", "Action 10"), ip(601, "BigNeonMax", "Action 12")),
        ):
            with self.subTest(room=turn.room):
                opponent = postflop_opponent(turn)
                pre, on_flop, on_turn = early_ranges(turn, opponent, count=128)
                self.assertEqual(
                    (pre, on_flop), early_ranges(flop, opponent, count=128)
                )
                self.assertEqual(
                    [s.board for s in (pre, on_flop, on_turn)],
                    [(), flop.board, turn.board],
                )
                self.assertEqual(
                    [s.legal_total for s in (pre, on_flop, on_turn)],
                    [comb(48, 4), comb(45, 4), comb(44, 4)],
                )
                self.assertEqual(
                    (on_flop.runouts_per_hand, on_turn.runouts_per_hand),
                    (FLOP_RUNOUTS, 40),
                )
                for stage in (pre, on_flop, on_turn):
                    refs = [
                        a.ref
                        for a in turn.history
                        if a.name == opponent.name
                        and a.street == stage.street
                        and a.action in {"check", "call", "bet", "raise"}
                    ]
                    self.assertEqual(len(stage.steps), 1 + len(refs))
                    for checkpoint, ref in zip(stage.steps[1:], refs, strict=True):
                        self.assertTrue(checkpoint.action.endswith(f"[{ref}]"))
                        self.assertNotIn(f"[{turn.ref}]", checkpoint.action)
                        self.assertAlmostEqual(sum(p for _, p in checkpoint.classes), 1)
                        self.assertLessEqual(checkpoint.effective_n, 128 + 1e-9)
                self.assertEqual(on_flop.steps[0].equity, on_flop.steps[1].equity)
                if turn.to_call:
                    self.assertLess(on_turn.steps[-1].equity, on_turn.steps[0].equity)

    def test_all_turn_runouts_match_original_omaha_evaluator(self):
        snap = ip(601, "BigNeonMax", "Action 12")
        hero = tuple(map(CARD_ID.__getitem__, snap.hero_cards))
        board = tuple(map(CARD_ID.__getitem__, snap.board))
        candidates = _sample(hero, board, 32)
        for candidate in candidates[:3]:
            self.assertFalse(set(candidate.cards) & set(hero + board))
            rival = [DECK[i] for i in candidate.cards]
            counts = [0, 0, 0]
            for i, card in enumerate(DECK):
                if i not in hero + board + candidate.cards:
                    table = [*snap.board, card]
                    own = best_plo_hand(list(snap.hero_cards), table)[:2]
                    other = best_plo_hand(rival, table)[:2]
                    counts[1 + (other > own) - (other < own)] += 1
            self.assertEqual(sum(counts), 40)
            self.assertEqual(candidate.outcomes, tuple(counts))

    def test_history_excludes_other_streets_rooms_and_future_records(self):
        snap = ip(601, "BigNeonMax", "Action 12")
        rival = postflop_opponent(snap)
        bet = snap.history[-1].amount
        match = context(snap, rival.name, bet, snap.pot - bet)
        start = clock(snap.stamp)
        day = timedelta(days=1)
        rows = (
            Observation(
                3,
                "3",
                snap.room,
                rival.name,
                "Action 11",
                start - 2 * day,
                start - day,
                match,
                "bet",
                street="Turn",
            ),
            Observation(
                4,
                "4",
                snap.room,
                rival.name,
                "Action 11",
                start - 2 * day,
                start - day,
                match,
                "bet",
                street="Flop",
            ),
            Observation(
                5,
                "5",
                "CoinPoker",
                rival.name,
                "Line 32",
                start - 2 * day,
                start - day,
                match,
                "bet",
                street="Turn",
            ),
            Observation(
                6,
                "6",
                snap.room,
                rival.name,
                "Action 11",
                start - day,
                start + day,
                match,
                "bet",
                street="Turn",
            ),
            Observation(
                7,
                snap.number,
                snap.room,
                rival.name,
                "Action 11",
                start - 2 * day,
                start - day,
                match,
                "bet",
                street="Turn",
            ),
        )
        sample = OpponentHistory(rows).sample(snap)
        self.assertEqual((sample.comparable, sample.broader), ((rows[0],), 1))
