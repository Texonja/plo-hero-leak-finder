import gzip
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from leakfinder.engine import replay
from leakfinder.importers import Excluded, coin, ipoker, money
from leakfinder.vendor.primitives import best_plo_hand, classify_known_hand

FIX = Path(__file__).parent / "fixtures"


def original(room, num):
    return (FIX / f"{room}-{num}.hh").read_bytes().decode()


# Parallel encodings of the same legal three-handed deal. C is the button.
def paired(rounds, pf=None, number="817261"):
    cp = [
        f"CoinPoker Hand #{number}: PLO 4 (₮0.25/₮0.50) 2026/03/01 12:00:00 CET",
        "Table 'Test' 3-max Seat #3 is the button",
    ]
    xml = ET.Element("session")
    sg = ET.SubElement(xml, "general")
    for tag, value in dict(
        gametype="Omaha PL €0,25/€0,50",
        tablecurrency="EUR",
        bigblind="€0,50",
        smallblind="€0,25",
        nickname="B",
    ).items():
        ET.SubElement(sg, tag).text = value
    game = ET.SubElement(xml, "game", gamecode=number)
    gen = ET.SubElement(game, "general")
    ET.SubElement(gen, "startdate").text = "2026-03-01 12:00:00"
    players = ET.SubElement(gen, "players")
    for i, name in enumerate("ABC", 1):
        cp.append(f"Seat {i}: {name} (₮50 in chips)")
        ET.SubElement(
            players,
            "player",
            seat=str(i),
            name=name,
            chips="€50,00",
            dealer=str(int(i == 3)),
        )
    pf = pf or [("C", "fold", 0), ("A", "raise", 1.5), ("B", "call", 1.0)]
    rds = [[("A", "sb", 0.25), ("B", "bb", 0.5)], pf, *rounds]
    paid = {}
    seq = 0
    boards = ["As", "8c", "2h", "7s", "Kh"]
    for ri, actions in enumerate(rds):
        rd = ET.SubElement(game, "round", no=str(ri))
        if ri == 1:
            cp.extend(["*** HOLE CARDS ***", "Dealt to B [4h 5h 9s Td]"])
            ET.SubElement(rd, "cards", type="Pocket", player="B").text = "H4 H5 S9 D10"
        if ri >= 2:
            paid = {}
            cards = boards[:3] if ri == 2 else [boards[ri]]
            cp.append(
                "*** "
                + ["FLOP", "TURN", "RIVER"][ri - 2]
                + " *** ["
                + " ".join(boards[: ri + 1])
                + "]"
            )
            ET.SubElement(
                rd, "cards", type=["Flop", "Turn", "River"][ri - 2]
            ).text = " ".join(x[1].upper() + x[0] for x in cards)
        for who, a, x in actions:
            seq += 1
            words = {
                "fold": "folds",
                "call": "calls",
                "check": "checks",
                "bet": "bets",
                "sb": "posts small blind",
                "bb": "posts big blind",
                "allin": "ALLIN",
            }
            if a == "raise":
                cp.append(
                    f"{who}: raises ₮{x - max(paid.values(), default=0):.2f} to ₮{x:.2f}"
                )
                paid[who] = x
            else:
                cp.append(f"{who}: {words[a]}" + (f" ₮{x:.2f}" if x else ""))
                paid[who] = paid.get(who, 0) + x
            typ = {
                "sb": 1,
                "bb": 2,
                "fold": 0,
                "call": 3,
                "check": 4,
                "bet": 5,
                "allin": 7,
                "raise": 23,
            }[a]
            ET.SubElement(
                rd,
                "action",
                no=str(seq),
                type=str(typ),
                player=who,
                sum=f"€{x:.2f}".replace(".", ","),
            )
    cp.append("*** SUMMARY ***")
    return "\n".join(cp), ET.tostring(xml, encoding="unicode")


def decoded(pair):
    return [coin(pair[0]), ipoker(pair[1])]


def ids(hand):
    return [d.node for d in replay(hand)[0]]


class RealHandTests(unittest.TestCase):
    def test_actual_coin_allin_changes_aggressor(self):
        h = coin(original("coin", 146))
        _, ev = replay(h)
        self.assertEqual(h.pot_type, "3bet")
        self.assertEqual([e.action for e in ev["Preflop"]].count("raise"), 2)

    def test_real_short_call_and_blind_contribution(self):
        _, ev = replay(ipoker(original("ip", 101), True))
        e = next(e for e in ev["Flop"] if e.ref == "Action 12")
        self.assertEqual(
            (e.action, e.amount, e.owed, e.allin),
            ("call", money("0.14"), money("11.25"), True),
        )
        _, ev = replay(ipoker(original("ip", 4), True))
        self.assertTrue(
            any(
                e.action == "raise" and e.amount == money("2.50") for e in ev["Preflop"]
            )
        )

    def test_actual_stab_and_classification(self):
        for h in [coin(original("coin", 30)), ipoker(original("ip", 601), True)]:
            ds, events = replay(h)
            (row,) = [d for d in ds if d.node == "BET_IP_VS_MISSED_FLOP_CB"]
            self.assertEqual((row.event.player, row.event.action), (h.hint, "check"))
        self.assertEqual(
            best_plo_hand(h.cards[h.hint], h.boards["Flop"])[2], "high_card"
        )
        c = coin(original("coin", 1))
        self.assertEqual(
            best_plo_hand(c.cards[c.hint], c.boards["River"])[2], "two_pair"
        )

    def test_independent_raw_hand_gold_set(self):
        pack = json.loads(gzip.decompress((FIX / "source-audit.json.gz").read_bytes()))
        self.assertEqual(len(pack), 240)
        verbs = {
            "raises": "raise",
            "calls": "call",
            "folds": "fold",
            "checks": "check",
            "bets": "bet",
        }
        for row in pack:
            with self.subTest(room=row["room"], hand=row["number"], node=row["node"]):
                h = (
                    coin(row["raw"])
                    if row["room"] == "CoinPoker"
                    else ipoker(row["raw"], True)
                )
                got = [
                    (q.event.ref, q.event.player, q.event.action)
                    for q in replay(h)[0]
                    if q.node == row["node"]
                ]
                self.assertEqual(
                    got, [(ref, n, verbs[a]) for ref, n, a in row["expected"]]
                )

    def test_currency_and_hidden_cards(self):
        self.assertEqual(money("€1.234,56"), money("$1,234.56"))
        with self.assertRaises(Excluded):
            money("")
        h = ipoker(original("ip", 1), True)
        self.assertEqual(len(h.cards), 1)
        self.assertEqual(h.cards[h.hint], ["3c", "8d", "7s", "5s"])

    def test_exact_omaha_combinations_and_ace_suit(self):
        self.assertEqual(
            best_plo_hand("As 2h Jd Tc".split(), "Ks Qs 7s 6s 5s".split())[2],
            "high_card",
        )
        self.assertEqual(
            best_plo_hand("As 2h Jd Tc".split(), "3s 4s 5c".split())[2], "straight"
        )
        self.assertNotEqual(
            classify_known_hand("As 7h 2h 9c".split())["hand_class"], "suited_ace"
        )


class SameDecisionTests(unittest.TestCase):
    def test_xml_card_element_order_is_round_scoped(self):
        from xml.etree import ElementTree as ET

        _, original_xml = paired(
            [[("A", "check", 0), ("B", "bet", 1), ("A", "fold", 0)]]
        )
        root = ET.fromstring(original_xml)
        flop = root.find('game/round[@no="2"]')
        board = flop.find("cards")
        flop.remove(board)
        flop.append(board)
        for tag in ("smallblind", "bigblind"):
            root.find("general").remove(root.find("general/" + tag))
        migrated = ipoker(ET.tostring(root, encoding="unicode"))
        self.assertEqual(migrated.boards, ipoker(original_xml).boards)
        self.assertEqual(ids(migrated), ids(ipoker(original_xml)))

    def both(self, rounds, pf=None):
        pair = paired(rounds, pf)
        hands = decoded(pair)
        ds = [replay(h)[0] for h in hands]

        def projection(rows):
            return [
                (
                    d.node,
                    d.event.street,
                    d.event.idx,
                    d.event.player,
                    d.event.action,
                    d.event.amount,
                    d.event.pot,
                    d.event.owed,
                    json.loads(d.context)[2:],
                )
                for d in rows
            ]

        self.assertEqual(projection(ds[0]), projection(ds[1]))
        return ds[0]

    def test_four_streets_and_fold_to_river_barrel(self):
        rows = self.both(
            [
                [("A", "bet", 2), ("B", "call", 2)],
                [("A", "bet", 3), ("B", "call", 3)],
                [("A", "bet", 5), ("B", "fold", 0)],
            ]
        )
        keys = {d.node for d in rows}
        self.assertTrue(
            {
                "OPEN",
                "THREEBET",
                "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED",
                "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL",
                "RIVER_BARREL",
                "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL",
            }
            <= keys
        )
        self.assertNotIn("FOLD_TO_FLOP_CBET", keys)

    def test_missing_barrel_and_fold_to_stab(self):
        rows = self.both(
            [
                [("A", "bet", 1), ("B", "call", 1)],
                [("A", "check", 0), ("B", "bet", 2), ("A", "fold", 0)],
            ]
        )
        self.assertTrue(
            {"BET_IP_VS_MISSED_TURN_CB", "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB"}
            <= {d.node for d in rows}
        )

    def test_probe_turn_and_river(self):
        pf = [("C", "raise", 1.5), ("A", "fold", 0), ("B", "call", 1)]
        rows = self.both(
            [
                [("B", "check", 0), ("C", "check", 0)],
                [("B", "check", 0), ("C", "check", 0)],
                [("B", "bet", 2), ("C", "fold", 0)],
            ],
            pf,
        )
        self.assertTrue(
            {"TURN_PROBE_AFTER_FLOP_CHECKBACK", "RIVER_PROBE", "FOLD_TO_RIVER_PROBE"}
            <= {d.node for d in rows}
        )
        rows = self.both(
            [
                [("B", "check", 0), ("C", "check", 0)],
                [("B", "bet", 2), ("C", "fold", 0)],
            ],
            pf,
        )
        self.assertIn("FOLD_TO_TURN_PROBE", [d.node for d in rows])

    def test_stab_responses(self):
        rows = self.both([[("A", "check", 0), ("B", "bet", 1), ("A", "fold", 0)]])
        self.assertTrue(
            {"BET_IP_VS_MISSED_FLOP_CB", "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB"}
            <= {d.node for d in rows}
        )

    def test_multiway_and_squeeze_exclusions(self):
        pf = [("C", "raise", 1.5), ("A", "call", 1.25), ("B", "call", 1)]
        rows = self.both(
            [
                [
                    ("A", "check", 0),
                    ("B", "check", 0),
                    ("C", "bet", 2),
                    ("A", "fold", 0),
                    ("B", "fold", 0),
                ]
            ],
            pf,
        )
        self.assertEqual([d.event.player for d in rows if d.node == "THREEBET"], ["A"])
        self.assertFalse(any(d.event.street == "Flop" for d in rows))

    def test_missing_action_and_unknown_code_excluded(self):
        pair = paired([[("B", "bet", 1), ("A", "fold", 0)]])
        for h in decoded(pair):
            with self.assertRaisesRegex(Excluded, "order"):
                replay(h)
        with self.assertRaisesRegex(Excluded, "Unknown iPoker"):
            ipoker(pair[1].replace('type="3"', 'type="88"'))

    def test_unresolved_or_partial_allin_excluded(self):
        raw = original("ip", 101).replace(
            'sum="€0,14" type="7"', 'sum="€0,13" type="7"'
        )
        with self.assertRaisesRegex(Excluded, "all-in"):
            replay(ipoker(raw, True))

    def test_unsupported_games_and_multiple_boards(self):
        coin_raw = original("coin", 30)
        for s in [
            coin_raw.replace("PLO 4", "PLO 5"),
            coin_raw.replace("*** FLOP", "*** FIRST FLOP"),
            coin_raw.split("*** SUMMARY")[0],
        ]:
            with self.assertRaises(Excluded):
                coin(s)
        with self.assertRaises(Excluded):
            ipoker(original("ip", 1).replace("X X X X", "X X X X X"), True)
