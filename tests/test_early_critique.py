import re
import unittest
from dataclasses import replace
from types import SimpleNamespace

from leakfinder.demo import SAMPLES
from leakfinder.early_critique import review_early
from leakfinder.early_equity import showdown_example
from leakfinder.importers import sources
from leakfinder.replay_view import _ledger, _snapshot
from tests.test_early_audit import equity, spot


def review(snap, action, amount=0):
    return review_early(snap, SimpleNamespace(action=action, amount=amount), None)


def all_in_opponent(snap):
    return replace(snap, seats=tuple(
        replace(s, all_in=True, stack=0) if s.live and not s.is_hero else s
        for s in snap.seats
    ))


class EarlyCritiqueTests(unittest.TestCase):
    def test_both_networks_match_the_actual_wager_and_bet_alternative(self):
        expected = {
            ("CoinPoker", "9000009975", "Line 32"): (
                "large-weak-hand-bet", "bet", "50.0%", "0 unseen next cards"),
            ("iPoker", "9000005995", "Action 9"): (
                "large-weak-hand-bet", "bet", "44.4%", "4 unseen next cards"),
            ("CoinPoker", "9000000016", "Line 36"): (
                "turn-set-raise-straight-possible", "raise", "78.48 BB", "9 make Hero"),
            ("iPoker", "9000008012", "Action 14"): (
                "turn-set-raise-straight-possible", "raise", "22.5 BB", "10 make Hero"),
        }
        found = set()
        line_evidence = {
            "9000009975": ("BB checked", "set on 4 next cards"),
            "9000005995": ("led into the preflop raiser (BTN)",),
            "9000000016": ("61.64 BB of that can be matched", "44.64 BB beyond calling", "Holding Ad reduces"),
            "9000008012": ("Holding 5c reduces",),
        }
        for file in (SAMPLES / "CoinPoker-HeroDemo.txt", SAMPLES / "iPoker-HeroDemo.sql"):
            for record in sources(file):
                hand, events = _ledger(record)
                for key, (rule, action, text, cards) in expected.items():
                    if (hand.room, str(hand.number)) != key[:2]:
                        continue
                    ref = key[2]
                    event = events[ref]
                    snap = _snapshot(record, hand, events, hand.hint, ref)
                    with self.subTest(key=key):
                        self.assertEqual(event.action, action)
                        item = review_early(snap, event, None)
                        self.assertEqual(item.rule, rule)
                        self.assertIn(text, item.reason)
                        self.assertIn(cards, item.reason)
                        for detail in line_evidence[key[1]]:
                            self.assertIn(detail, item.reason)
                        self.assertTrue(item.counterargument)
                        self.assertIsNone(review(snap, "call" if snap.to_call else "check", snap.to_call))
                    found.add(key)
        self.assertEqual(found, set(expected))

    def test_stronger_draw_small_bet_and_uncontestable_excess(self):
        weak = spot("7h 7d 5s 5c", "Ad Jd 9h")
        self.assertIsNotNone(review(weak, "bet", 80))
        self.assertIsNone(review(weak, "bet", 33))
        self.assertIsNone(review(weak, "bet", 100))  # Only 90 chips left to contest.
        wrap = spot("Ts 9s 8h 7h", "8d 6s Qc")
        self.assertIsNone(review(wrap, "bet", 80))

    def test_bluff_defense_uses_the_actual_blockers(self):
        kings = spot("Kd Ks 8d 3h", "Ah Qd Jc Ts")
        item = review(kings, "bet", 80)
        self.assertIn("24 of the 48 two-card combinations", item.reason)
        self.assertEqual(-1, item.review_priority)
        nut_blocker = spot("Ad 8s 4c 3h", "Jd 7d 2d")
        self.assertIn("Ad blocks the strongest ordinary flush", review(nut_blocker, "bet", 80).reason)
        backdoors = spot("Ac 2c Ks 7s", "Jc 2h Ts")
        backdoor_bluff = review(backdoors, "bet", 80)
        self.assertIn("Ac gives Hero a backdoor draw", backdoor_bluff.reason)
        self.assertEqual(0, backdoor_bluff.review_priority)

    def test_set_with_a_flush_redraw_counts_distinct_rivers(self):
        snap = spot("Ah 6c 6d 2h", "6h 5h Ac 9s", owed=12, pot=40)
        item = review(snap, "raise", 35)
        self.assertIn("9 make Hero a full house or quads", item.reason)
        self.assertIn("Another 8 make a straight, flush or straight flush", item.reason)
        self.assertIn("4 guarantee at least a tie", item.reason)

    def test_free_card_needs_current_nuts_an_exposed_suit_and_last_action(self):
        top_set = spot("As Ad 7d 3s", "Ah 9c 4c")
        check = review(top_set, "check")
        self.assertEqual(check.rule, "checked-back-vulnerable-nuts")
        self.assertIn("10 keep the board unpaired", check.reason)
        self.assertIsNone(review(replace(top_set, history=()), "check"))
        self.assertIsNone(review(spot("As Ad 7c 3c", "Ah 9c 4c"), "check"))
        self.assertIsNone(review(spot("9s 9d 7d 3s", "Ah 9c 4c"), "check"))
        self.assertIsNone(review(spot("As Ad 7d 3s", "Ah Qc Jc"), "check"))
        nut_straight = spot("8s 7d 3h 2d", "9h 6c 5c")
        self.assertIn("nut straight", review(nut_straight, "check").reason)

    def test_closing_call_bound_is_optimistic_even_with_blockers(self):
        h, board = "As Kh 3d 2c", "Ac 9d 6s Jh"
        snap = spot(h, board, owed=50, pot=160)
        closed = all_in_opponent(snap)
        result = review(closed, "call", 50)
        self.assertEqual(result.rule, "turn-closing-call-value-bound")
        self.assertIn("at most 2 of the 40", result.reason)
        self.assertIn("5.0%", result.reason)
        self.assertIn("19.8%", result.reason)
        self.assertEqual(result.alternative, "Compare call and fold by range")
        match = re.search(r"Against a possible (\S+ \S+ \S+ \S+) \(.*?\), Hero has ([\d.]+)%", result.reason)
        self.assertIsNotNone(match)
        self.assertEqual(match[2], f"{100 * equity(h, match[1], board):.1f}")
        self.assertIn("40 runouts", result.reason)
        for villain in ("9s 9c 7d 8d", "Ad Ah 7d 8d", "Js Jd 5h 4h"):
            self.assertLessEqual(equity(h, villain, board), 2 / 40)
        self.assertIsNone(review(snap, "call", 50))  # River betting still possible.
        self.assertIsNone(review(replace(closed, to_call=2, price_to_match=2), "call", 2))
        multi = replace(closed, seats=tuple(replace(s, live=True) if s.name == "SB" else s for s in closed.seats))
        self.assertIsNone(review(multi, "call", 50))

    def test_fold_requires_a_conservative_guaranteed_share_after_card_removal(self):
        h, board = "As Ks 5c 4c", "2s 7s Jh Qd"
        snap = spot(h, board, owed=2, pot=100)
        result = review(snap, "fold")
        self.assertEqual(result.rule, "turn-fold-guaranteed-share")
        self.assertIn("10 unseen rivers", result.reason)
        self.assertIn("7.5%", result.reason)
        for villain in ("Qs Qc 8s 9s", "Js Ts 8s 9s", "Ac Kc 8d 9d"):
            self.assertGreaterEqual(equity(h, villain, board), 0.075)
        self.assertIsNone(review(spot(h, board, owed=20, pot=100), "fold"))
        overhang = replace(snap, seats=tuple(replace(s, committed=60) if s.name == "SB" else s for s in snap.seats))
        self.assertIsNone(review(overhang, "fold"))

    def test_trips_board_has_a_four_card_counterexample_without_a_pocket_pair(self):
        hole = ("Ac", "Kd", "6c", "4h")
        board = ("Js", "Jh", "Jd", "3s")
        self.assertIn("three of a kind now", showdown_example(hole, board, 6))

    def test_break_even_mixture_counts_showdown_shares_and_recognizes_impossible_price(self):
        from leakfinder.early_equity import _scenarios
        from leakfinder.river_holdings import DECK
        from tests.test_early_audit import equity
        hole, board = tuple("Ad Ac 6s 2h".split()), tuple("Qh 5d Tc 8d".split())
        rival, _, low, count, strong, high = _scenarios(hole, board, 3)
        self.assertEqual(count, 40)
        for holding, share in ((rival, low), (strong, high)):
            self.assertEqual(share, equity(" ".join(hole), " ".join(DECK[c] for c in holding), " ".join(board)))
        self.assertGreater(low, high)
        self.assertIn("50.0% of bets", showdown_example(hole, board, 3, (low + high) / 2))
        self.assertIn("No mix", showdown_example(hole, board, 3, low + 0.05))


if __name__ == "__main__":
    unittest.main()
