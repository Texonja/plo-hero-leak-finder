"""Independent release gate for the standalone river pilot (NumPy + SciPy)."""

import itertools
import json
import math
import os
import unittest
from pathlib import Path
from typing import ClassVar

try:
    import numpy as np
    from scipy.optimize import linprog
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        "Install the standalone river pilot requirements to run its validation gate"
    ) from exc
from pilots.river_solver.model import Game, rank
from pilots.river_solver.solver import solve


def rows(entries):
    return [{"cards": c.split(), "weight": w} for c, w in entries]


def fixture(frac=1):
    return {
        "board": ["As", "Js", "7s", "3d", "2c"],
        "pot_bb": 10,
        "effective_stack_bb": 38,
        "ranges": {
            "oop": rows([("Ks Qs 8d 9c", 1), ("4h 6h Tc 8c", 2)]),
            "ip": rows([("Ah Kd Th 9d", 1)]),
        },
        "tree": {
            "bet_fractions": [frac],
            "ip_bet_fractions": [],
            "max_raises": 0,
            "all_in": False,
        },
        "query": {"path": [], "hero": ["Ks", "Qs", "8d", "9c"]},
    }


def small():
    s = fixture(0.5)
    s["tree"] = {
        "bet_fractions": [0.5],
        "raise_fractions": [1],
        "max_raises": 1,
        "all_in": True,
    }
    s["ranges"]["oop"] += rows(
        [("Ac Jd 4s 6c", 0.7), ("Ad 8s 9d 7c", 1.2), ("Qh Kc Td 2h", 0.8)]
    )
    s["ranges"]["ip"] += rows(
        [("Kd Qs 8d 2h", 0.7), ("5c 4d Ts 8c", 0.65), ("9s 6s Qc 4c", 1.3)]
    )
    return s


def exact_lp(spec):
    # Separate extensive-form construction for this one-size/one-raise game.
    # Realization plans: distinct sequence per hand, seat, information set and action.
    pot, cap = spec["pot_bb"], spec["effective_stack_bb"]
    hands = [[tuple(r["cards"]) for r in spec["ranges"][s]] for s in ("oop", "ip")]
    weights = [[r["weight"] for r in spec["ranges"][s]] for s in ("oop", "ip")]
    board = tuple(spec["board"])
    flows = [{}, {}]
    seq = [{}, {}]
    pay = {}

    def seqid(s, h, path):
        return seq[s].setdefault((h, path), len(seq[s]))

    for s in (0, 1):
        for h in range(len(hands[s])):
            flows[s][h, "root"] = ({seqid(s, h, ()): 1}, 1)
    mass = 0
    for i, j in itertools.product(range(len(hands[0])), range(len(hands[1]))):
        if set(hands[0][i]) & set(hands[1][j]):
            continue
        chance = weights[0][i] * weights[1][j]
        mass += chance
        a, b = rank(hands[0][i], board), rank(hands[1][j], board)
        sign = (a > b) - (a < b)

        def visit(path, parents, sign=sign, chance=chance, hand_indices=(i, j)):
            actor = len(path) % 2
            bet = pot * 0.5
            raised = min(cap, bet + pot + 2 * bet)
            if path and path[-1] in ("fold", "call") or path == ("check", "check"):
                if path[-1] == "fold":
                    prior = bet if "raise" in path else 0
                    v = (pot / 2 + prior) * (1 if actor == 0 else -1)
                else:
                    v = sign * (
                        pot / 2
                        + (raised if "raise" in path else bet if "bet" in path else 0)
                    )
                pay[parents] = pay.get(parents, 0) + chance * v
                return
            moves = (
                ("check", "bet")
                if not path or path[-1] == "check"
                else ("fold", "call", "raise")
                if path[-1] == "bet"
                else ("fold", "call")
            )
            h = hand_indices[actor]
            constraint = {parents[actor]: -1}
            for mv in moves:
                ids = list(parents)
                ids[actor] = seqid(actor, h, path + (mv,))
                constraint[ids[actor]] = 1
                visit(path + (mv,), tuple(ids))
            flows[actor][h, path] = constraint, 0

        visit((), (seqid(0, i, ()), seqid(1, j, ())))
    A = np.zeros(tuple(map(len, seq)))
    for (i, j), p in pay.items():
        A[i, j] = p / mass

    def constraints(s):
        E = np.zeros((len(flows[s]), len(seq[s])))
        e = []
        for n, (row, rhs) in enumerate(flows[s].values()):
            for k, v in row.items():
                E[n, k] = v
            e.append(rhs)
        return E, np.asarray(e)

    def minimax(A, E, e, F, f):
        nx = A.shape[0]
        ny = len(f)
        lp = linprog(
            np.r_[np.zeros(nx), -f],
            A_ub=np.c_[-A.T, F.T],
            b_ub=np.zeros(A.shape[1]),
            A_eq=np.c_[E, np.zeros((len(e), ny))],
            b_eq=e,
            bounds=[(0, None)] * nx + [(None, None)] * ny,
            method="highs",
        )
        assert lp.success, lp.message
        return -lp.fun

    E, e = constraints(0)
    F, f = constraints(1)
    lo = minimax(A, E, e, F, f)
    hi = -minimax(-A.T, F, f, E, e)
    assert abs(lo - hi) < 1e-8
    return lo + pot / 2


class RiverSolverTests(unittest.TestCase):
    metrics: ClassVar[dict] = {}

    @classmethod
    def tearDownClass(cls):
        if "RIVER_GATE_METRICS" in os.environ:
            Path(os.environ["RIVER_GATE_METRICS"]).write_text(
                json.dumps(cls.metrics, indent=2), encoding="utf8"
            )

    def test_analytic(self):
        for f in (0.33, 0.5, 1):
            with self.subTest(f=f):
                s = fixture(f)
                r, _ = solve(s, iterations=12000, minimum_iterations=12000)
                groups = r["query"]["range_strategy_by_made_hand"]
                action = next(a for a in r["query"]["hero_strategy"] if a != "check")
                nuts = groups["flush"][action]
                air = groups["high card"][action]
                self.assertAlmostEqual(
                    2 * air / (nuts + 2 * air), f / (1 + 2 * f), delta=0.005
                )
                s["query"] = {"path": [action], "hero": s["ranges"]["ip"][0]["cards"]}
                r, _ = solve(s, iterations=12000, minimum_iterations=12000)
                self.assertAlmostEqual(
                    r["query"]["hero_strategy"]["call"], 1 / (1 + f), delta=0.005
                )
                self.assertTrue(r["reliable"])
                self.metrics.setdefault("polar", []).append(
                    {
                        "fraction": f,
                        "expected_bluff_share": f / (1 + 2 * f),
                        "actual_bluff_share": 2 * air / (nuts + 2 * air),
                        "expected_call": 1 / (1 + f),
                        "actual_call": r["query"]["hero_strategy"]["call"],
                        "iterations": r["iterations"],
                    }
                )

    def test_sequence_form(self):
        s = small()
        v = exact_lp(s)
        r, _ = solve(s, target_pct=0.05)
        self.assertTrue(r["reliable"])
        self.assertAlmostEqual(r["game_ev_bb"][0], v, delta=0.001 * s["pot_bb"])
        self.assertLessEqual(s["pot_bb"] - r["best_response_ev_bb"][1] - 1e-8, v)
        self.assertGreaterEqual(r["best_response_ev_bb"][0] + 1e-8, v)
        self.metrics["lp"] = {
            "lp_oop_bb": v,
            "cfr_oop_bb": r["game_ev_bb"][0],
            "absolute_difference_pct_pot": 100
            * abs(v - r["game_ev_bb"][0])
            / s["pot_bb"],
            "combos": [len(s["ranges"][k]) for k in ("oop", "ip")],
            "pot_bb": s["pot_bb"],
        }

    def test_blockers(self):
        s = small()
        g = Game(s)
        brute = np.array(
            [
                [
                    math.prod((wa, wb)) if len(set(a + b)) == 8 else 0
                    for b, wb in zip(g.hands[1], g.weights[1])
                ]
                for a, wa in zip(g.hands[0], g.weights[0])
            ]
        )
        np.testing.assert_allclose(
            brute, g.weights[0][:, None] * g.legal[0] * g.weights[1]
        )
        self.assertGreater((brute == 0).sum(), 0)
        for i, row in enumerate(brute):
            weights = g.weights[1] * g.legal[0][i]
            np.testing.assert_allclose(row / row.sum(), weights / weights.sum())

    def test_money(self):
        s = small()
        r, _ = solve(s)
        self.assertAlmostEqual(sum(r["game_ev_bb"]), s["pot_bb"], places=10)

    def test_best_response_without_observing_hidden_cards(self):
        s = fixture(0.5)
        r, _ = solve(s, iterations=13)  # deliberately away from equilibrium
        b = s["pot_bb"] * 0.5
        pot = s["pot_bb"]
        polar = r["query"]["range_strategy_by_made_hand"]
        pn, pa = polar["flush"]["bet 5"], polar["high card"]["bet 5"]
        s["query"] = {"path": ["bet 5"], "hero": s["ranges"]["ip"][0]["cards"]}
        answer, _ = solve(s, iterations=13)
        call = answer["query"]["hero_strategy"]["call"]
        # OOP can condition on its own type. IP must choose one action across both polar holdings.
        oop = (max(pot, pot + b * call) + 2 * max(0, pot * (1 - call) - b * call)) / 3
        ip = (2 * (1 - pa) * pot + max(0, 2 * pa * (pot + b) - pn * b)) / 3
        np.testing.assert_allclose(
            r["best_response_ev_bb"], [oop, ip], rtol=0, atol=1e-10
        )

    def test_deterministic(self):
        s = small()
        self.assertEqual(solve(s)[0], solve(s)[0])
        unfinished, _ = solve(s, iterations=1, target_pct=100)
        self.assertGreater(unfinished["exploitability_pct_pot"], 0.1)
        self.assertFalse(unfinished["reliable"])

    def test_pot_limit_and_short_allin(self):
        s = small()
        s["effective_stack_bb"] = 8
        s["tree"]["max_raises"] = 3
        root = Game(s).root
        self.assertIn("bet 8", root.children)
        self.assertEqual(
            set(root.children["bet 5"].children), {"call", "fold", "raise 8"}
        )
        self.assertEqual(
            set(root.children["bet 5"].children["raise 8"].children), {"call", "fold"}
        )
        s["effective_stack_bb"] = 100
        root = Game(s).root
        self.assertNotIn("bet 100", root.children)
        self.assertIn("raise 25", root.children["bet 5"].children)
        self.assertIn("raise 85", root.children["bet 5"].children["raise 25"].children)
