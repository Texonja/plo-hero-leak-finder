import unittest
from dataclasses import replace
from itertools import combinations
from pathlib import Path

from leakfinder.early_postflop import Draws, early_advice, next_draws
from leakfinder.importers import sources
from leakfinder.replay_view import snapshot_from_record
from leakfinder.vendor.common import RANK_VALUE
from leakfinder.vendor.primitives import best_plo_hand, eval_5
from tests.test_replay_view import coin, ip
from tests.test_strategy_guide import move, view


def guide(snapshot):
    hero = next(s for s in snapshot.seats if s.is_hero)
    opponents = [s for s in snapshot.seats if s.live and not s.is_hero]
    rank, kicks, _, _ = best_plo_hand(snapshot.hero_cards, snapshot.board)
    return early_advice(snapshot, hero, opponents, rank, kicks)


def oracle_counts(hole, board):
    # Deliberately different implementation: inspect every legal 2 + 3 hand
    # for every possible next card. One card may complete both paths.
    straight = flush = both = 0
    for rank in RANK_VALUE:
        for suit in "cdhs":
            new = rank + suit
            if new in (*hole, *board):
                continue
            kinds = {
                eval_5((*h, *b))[0]
                for h in combinations(hole, 2)
                for b in combinations((*board, new), 3)
            }
            can_straight = bool(kinds & {4, 8})
            can_flush = bool(kinds & {5, 8})
            straight += can_straight
            flush += can_flush
            both += can_straight and can_flush
    return straight, flush, both


class EarlyPostflopTests(unittest.TestCase):
    def test_actual_turn_probe_distinguishes_one_spade_from_a_flush(self):
        path = Path(__file__).resolve().parents[1] / "demo" / "CoinPoker-HeroDemo.txt"
        record = next(
            r for r in sources(path) if r.raw.startswith("CoinPoker Hand #9000000021:")
        )
        snap = snapshot_from_record(record, "HeroDemo", "Line 34")
        decision = guide(snap)
        self.assertIn("turn probe", decision.title)
        self.assertFalse(decision.choices)
        self.assertIn("requires two matching hole cards", decision.why)
        self.assertEqual(snap.to_call, 0)

    def test_bundled_ipoker_semibluff_and_response_to_turn_probe(self):
        file = Path(__file__).resolve().parents[1] / "demo" / "iPoker-HeroDemo.sql"
        refs = {"9000000068": "Action 5", "9000000067": "Action 8"}
        samples = {
            r.expected: snapshot_from_record(r, "HeroDemo", refs[r.expected])
            for r in sources(file)
            if r.expected in refs
        }
        flop = samples["9000000068"]
        self.assertEqual(next_draws(flop.hero_cards, flop.board), Draws(8, 9, 2, 7))
        self.assertEqual(guide(flop).choices, ("check", "bet"))
        self.assertEqual(guide(flop).size.amount * 2, flop.pot)
        faced = samples["9000000067"]
        self.assertIn("facing a turn probe", guide(faced).title)
        self.assertEqual(next_draws(faced.hero_cards, faced.board), Draws(4, 0, 0, 2))
        self.assertFalse(guide(faced).choices)

    def test_draw_counts_use_two_hole_cards_and_physical_overlap(self):
        cases = (
            ("As Kh Qc Jd", "2d 3h 4s 5c", Draws(0, 0, 0, 0)),
            ("Ks 2s Qd 6c", "As 7s 3h 9d", Draws(0, 9, 0, 7)),
            ("As Ks 7h 8h", "5s 6s Qd", Draws(8, 9, 2, 8)),
            ("Ad Ks Kh 2s", "3s 3h 7s", Draws(0, 9, 0, 0)),
        )
        for h, b, expected in cases:
            with self.subTest(h=h, b=b):
                hole, board = tuple(h.split()), tuple(b.split())
                draws = next_draws(hole, board)
                self.assertEqual(draws, expected)
                self.assertEqual(
                    (draws.straight, draws.flush, draws.both),
                    oracle_counts(hole, board),
                )

    def test_actual_coin_stab_and_change_on_turn(self):
        flop = coin(30, "Hero", "Line 28")
        turn = coin(30, "Hero", "Line 31")
        self.assertIn("after a missed c-bet", guide(flop).title)
        self.assertFalse(guide(flop).choices)
        self.assertIn("turn stab", guide(turn).title)
        self.assertEqual(next_draws(turn.hero_cards, turn.board), Draws(8, 0, 0, 6))
        self.assertFalse(guide(turn).choices)

    def test_actual_ipoker_delayed_bet_and_single_spade_blocker(self):
        flop = ip(41, "BigNeonMax", "Action 6")
        turn = ip(41, "BigNeonMax", "Action 8")
        self.assertEqual(guide(flop).choices, ("check", "bet"))
        self.assertIn("Overpair", guide(flop).why)
        self.assertIn("delayed c-bet", guide(turn).title)
        self.assertEqual(next_draws(turn.hero_cards, turn.board), Draws(4, 0, 0, 0))
        self.assertIn("two hole cards of that suit", guide(turn).why)
        self.assertIsNone(guide(turn).size)

    def test_check_raise_price_and_raise_rights(self):
        prefix = [move(n, "fold") for n in ("UTG", "HJ", "CO", "SB")]
        prefix.extend(
            (
                move("BTN", "raise", 3, to=3),
                move("BB", "call", 2),
                move("BB", "check", street="Flop"),
                move("BTN", "bet", 2, "Flop"),
            )
        )
        s = view("As Ks 7h 8h", "BB", "Flop", "5s 6s Qd", prefix, owed=2, pot=10)
        self.assertIn("c-bet", guide(s).title)
        self.assertIn("A check-raise", guide(s).why)
        self.assertFalse(guide(s).choices)
        closed = guide(replace(s, can_raise=False))
        self.assertFalse(closed.choices)
        self.assertNotIn("check-raise", closed.why)
        faced = replace(
            s,
            history=(*s.history, move("BB", "raise", 6, "Flop", to=6)),
            hero="BTN",
            seats=tuple(replace(p, is_hero=p.name == "BTN") for p in s.seats),
        )
        self.assertIn("facing a check-raise", guide(faced).title)
        self.assertFalse(guide(faced).choices)
        self.assertIsNone(guide(replace(s, street="River")))

    def test_improvements_of_a_set_are_independent_of_straight_draw(self):
        prefix = [move(n, "fold") for n in ("UTG", "HJ", "CO", "SB")]
        prefix.extend(
            (move("BB", "check", street="Turn"), move("BTN", "bet", 5, "Turn"))
        )
        s = view("Ad Jc Ts Th", "BB", "Turn", "Js Tc 6s Qh", prefix, owed=5, pot=15)
        detail = guide(s)
        self.assertIn("currently hold a set", detail.why)
        self.assertIn("9 cards can make a full house or quads", detail.why)
