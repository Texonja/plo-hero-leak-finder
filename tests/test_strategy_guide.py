"""Validate guide choices, coverage, and source links independently of results."""

import unittest
from contextlib import closing
from dataclasses import replace

from leakfinder.demo import load_demo
from leakfinder.postflop_value import bet_size
from leakfinder.replay_view import (
    ReplayAction,
    ReplaySeat,
    ReplaySnapshot,
    hero_decisions,
    load_snapshot,
)
from leakfinder.review_queue import build_queue
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import advice_at, nuts
from leakfinder.vendor.primitives import best_plo_hand


def view(cards, pos="BTN", street="Preflop", board="", prior=(), owed=1, pot=2):
    names = ("UTG", "HJ", "CO", "BTN", "SB", "BB")
    folded = {a.name for a in prior if a.action == "fold"}
    return ReplaySnapshot(
        "iPoker",
        "1",
        "Test",
        street,
        pos,
        tuple(cards.split()),
        tuple(board.split()),
        tuple(
            ReplaySeat(
                i,
                n,
                n,
                90,
                n not in folded,
                False,
                n == pos,
                n == "BTN",
                sum(a.amount for a in prior if a.name == n and a.action == "raise"),
            )
            for i, n in enumerate(names)
        ),
        tuple(prior),
        pot,
        owed,
        owed,
        1,
        "EUR",
        "Test",
        True,
    )


def move(actor, action, amount=0, street="Preflop", to=None):
    return ReplayAction(street, actor, action, amount, "Test", to, False)


class StrategyTests(unittest.TestCase):
    def test_rfi_uses_position_and_covers_six_max(self):
        for pos in ("UTG", "HJ", "CO", "BTN", "SB"):
            s = view("As Ah Ks Kh", pos)
            self.assertEqual(advice_at(s).choices, ("raise",))
            self.assertIn(pos, advice_at(s).title)
        s = view("Kh Kc 7c 5s", "UTG")
        self.assertEqual(advice_at(s).choices, ("fold", "raise"))
        sb_limp = move("SB", "call", 0.5)
        self.assertEqual(
            advice_at(view("Qd 7h 6s 4c", "BB", prior=[sb_limp], owed=0)).choices,
            ("check",),
        )

    def test_open_3bet_and_squeeze_use_action_prefix_and_raise_rights(self):
        opens = [
            move("UTG", "fold"),
            move("HJ", "fold"),
            move("CO", "raise", 3.5, to=3.5),
        ]
        self.assertEqual(
            advice_at(view("Jh 9d Ac 3c", prior=opens, owed=3.5, pot=5)).choices,
            ("fold",),
        )
        squeeze = opens + [move("BTN", "call", 3.5)]
        guide = advice_at(view("As Ah Ks Kh", "SB", prior=squeeze, owed=3, pot=8.5))
        self.assertIn("Squeeze", guide.title)
        reraised = opens + [
            move("BTN", "raise", 12, to=12),
            move("SB", "fold"),
            move("BB", "fold"),
        ]
        s = view("As Ah Ks Kh", "CO", prior=reraised, owed=8.5, pot=17)
        self.assertEqual(advice_at(s).play, "4-bet")
        self.assertEqual(advice_at(replace(s, can_raise=False)).play, "call")

    def test_postflop_uses_only_current_board_and_legal_choices(self):
        folds = [move(n, "fold") for n in ("UTG", "HJ", "CO", "SB")]
        royal = view(
            "As Ks Ah Kh",
            street="River",
            board="Qs Js Ts 3c 4d",
            prior=folds,
            owed=0,
            pot=10,
        )
        self.assertEqual(advice_at(royal).choices, ("bet", "check"))
        facing = replace(
            royal,
            to_call=6,
            pot=16,
            can_raise=False,
            history=(*folds, move("BB", "bet", 6, "River")),
        )
        self.assertEqual(advice_at(facing).choices, ("call",))
        air = replace(
            facing,
            hero_cards=("2h", "6d", "7c", "8d"),
            to_call=9,
            pot=19,
            history=(*folds, move("BB", "bet", 9, "River")),
        )
        self.assertEqual(advice_at(air).choices, ("fold",))
        for street, board in (("Flop", "Qs Js Ts"), ("Turn", "Qs Js Ts 3c")):
            snap = replace(royal, street=street, board=tuple(board.split()))
            self.assertIn("bet", advice_at(snap).choices)
        self.assertIsNone(advice_at(replace(royal, hero_cards=None)))

    def test_ace_flush_is_not_nuts_when_a_straight_flush_is_possible(self):
        hero, board = (
            ("As", "Ks", "Ah", "Kh"),
            ("7s", "8s", "9s", "2c", "3d"),
        )
        rank, kicks, _, _ = best_plo_hand(hero, board)
        self.assertEqual(rank, 5)
        self.assertFalse(nuts(hero, board, rank, tuple(kicks)))

    def test_opening_size_caps_and_comparison_use_contestable_amount(self):
        snap = view(
            "As Ks Ah Kh", street="River", board="Qs Js Ts 3c 4d", owed=0, pot=10
        )
        hero = next(s for s in snap.seats if s.is_hero)
        opp = next(s for s in snap.seats if s.position == "BB")
        size = bet_size(snap, hero, [replace(opp, stack=2)], 65)
        self.assertEqual(size.amount, 2)
        self.assertFalse(size.far_from(7))
        self.assertEqual(bet_size(snap, replace(hero, stack=1), [opp], 65).amount, 1)
        self.assertEqual(bet_size(snap, hero, [opp], 65).amount, 6)

    def test_demo_uses_both_rooms_and_guide_items_open_at_original_action(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            load_demo(db)
            queue = build_queue(db, scope="All hands", findings={})
            self.assertEqual(queue.hand_count, 640)
            self.assertTrue(
                queue.items and all(i.status == "Guide" for i in queue.items)
            )
            self.assertEqual(
                {i.room for i in queue.guide_items}, {"CoinPoker", "iPoker"}
            )
            self.assertGreater(queue.guide_choices, len(queue.guide_items))
            hid, hero = db.execute(
                "SELECT id,hint FROM hands WHERE room='CoinPoker' AND number='9000005623'"
            ).fetchone()
            seen = {}
            for e, s in hero_decisions(db, hid, hero, "All streets"):
                seen[s.street] = (e, s, advice_at(s))
            e, _, flop = seen["Flop"]
            self.assertEqual(flop.choices, ("check", "bet"))
            self.assertIsNone(flop.difference(e.action, e.amount))
            self.assertIn("QQ", flop.why)
            self.assertIn("removing Jx", flop.why)
            self.assertIn("25%", flop.play)
            self.assertNotIn("55", flop.why)
            e, _, turn = seen["Turn"]
            self.assertIn("full house (QQ, 55)", turn.why)
            self.assertIn("Flop checked through", turn.why)
            self.assertIsNone(turn.difference(e.action, e.amount))
            e, _, river = seen["River"]
            self.assertIn("SB called", river.why)
            self.assertIn("35%", river.play)
            self.assertEqual(river.difference(e.action, e.amount), "size")
            items = [i for i in queue.guide_items if i.hand == hid]
            self.assertEqual(
                [i.street for i in items if i.street != "Preflop"], ["River"]
            )
            for i in queue.guide_items:
                snap = load_snapshot(db, i.hand, i.hero, i.ref)
                self.assertEqual(
                    (snap.room, snap.number, snap.ref), (i.room, i.number, i.ref)
                )
                self.assertEqual(snap.hero_cards, i.cards)
                actual, _ = next(
                    (e, s)
                    for e, s in hero_decisions(db, i.hand, i.hero, i.street)
                    if e.ref == i.ref
                )
                self.assertTrue(
                    advice_at(snap).difference(actual.action, actual.amount)
                )

            river_cases = (
                ("CoinPoker", "9000002844", "Line 32", "call", "25.0%", None),
                ("CoinPoker", "9000001429", "Line 40", "bet", "55%", 0.55),
                ("CoinPoker", "9000002860", "Line 23", "check", "33%", 0.33),
                (
                    "iPoker",
                    "9000005984",
                    "Action 16",
                    "fold",
                    "before the other players act",
                    None,
                ),
                ("iPoker", "9000010026", "Action 17", "fold", "29.5%", None),
                ("iPoker", "9000014007", "Action 16", "fold", "20.0%", None),
            )
            for room, number, ref, action, phrase, fraction in river_cases:
                hid, hero = db.execute(
                    "SELECT id,hint FROM hands WHERE room=? AND number=?",
                    (room, number),
                ).fetchone()
                snap = load_snapshot(db, hid, hero, ref)
                advice = advice_at(snap)
                with self.subTest(room=room, number=number, ref=ref):
                    self.assertEqual(advice.choices[0], action)
                    self.assertIn(phrase, f"{advice.play} {advice.why}")
                    if fraction is not None:
                        self.assertAlmostEqual(
                            advice.size.amount / snap.pot, fraction, places=3
                        )
