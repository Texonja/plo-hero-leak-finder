import unittest
from dataclasses import replace
from itertools import combinations
from types import SimpleNamespace

from leakfinder.replay_view import ReplayAction, ReplaySeat, ReplaySnapshot
from leakfinder.sb_reference import compare_sb, fold_weights, hand_key, sb_reference


def unopened():
    positions = ("UTG", "HJ", "CO", "BTN", "SB", "BB")
    seats = tuple(
        ReplaySeat(i, p, p, 200 - c, i >= 4, False, i == 4, i == 3, c)
        for i, (p, c) in enumerate(zip(positions, (0, 0, 0, 0, 1, 2)))
    )
    steps = tuple(
        ReplayAction("Preflop", p, action, n, str(i), None, False)
        for i, (p, action, n) in enumerate(
            [("SB", "posts_sb", 1), ("BB", "posts_bb", 2)]
            + [(p, "fold", 0) for p in positions[:4]]
        )
    )
    return ReplaySnapshot(
        "iPoker",
        "1",
        "7",
        "Preflop",
        "SB",
        ("9s", "7c", "4h", "2d"),
        (),
        seats,
        steps,
        3,
        1,
        1,
        2,
        "EUR",
        "dealt",
        True,
        sb=1,
    )


class ReferenceTests(unittest.TestCase):
    def test_classes_cover_every_exact_plo4_holding(self):
        deck = [r + s for r in "23456789TJQKA" for s in "cdhs"]
        keys = {hand_key(hand) for hand in combinations(deck, 4)}
        self.assertEqual(len(keys), 16432)
        self.assertEqual(keys, set(fold_weights()))

    def test_root_action_weight_and_missing_continuation_split(self):
        snap = unopened()
        self.assertEqual(sb_reference(snap), (1.0, 100))
        self.assertIsNone(compare_sb(snap, SimpleNamespace(action="fold")))
        self.assertEqual(
            compare_sb(snap, SimpleNamespace(action="raise")).alternative, "fold"
        )
        ds = replace(snap, hero_cards=("As", "Ks", "Ah", "Kh"))
        self.assertEqual(sb_reference(ds), (0.0, 100))
        self.assertIn(
            "complete or raise",
            compare_sb(ds, SimpleNamespace(action="fold")).alternative,
        )
        self.assertIsNone(compare_sb(ds, SimpleNamespace(action="call")))
        self.assertEqual(sb_reference(replace(ds, room="CoinPoker")), sb_reference(ds))

    def test_reject_unmatched_trees_and_depth(self):
        snap = unopened()
        for changed in (
            replace(snap, street="Flop"),
            replace(snap, sb=0),
            replace(snap, pot=4),
            replace(snap, seats=snap.seats[1:]),
            replace(snap, history=snap.history[:-1]),
            replace(
                snap,
                history=(*snap.history, replace(snap.history[-1], action="posts_ante")),
            ),
            replace(snap, seats=(*snap.seats[:-1], replace(snap.seats[-1], stack=150))),
        ):
            self.assertIsNone(sb_reference(changed))
