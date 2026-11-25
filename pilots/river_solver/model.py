"""An explicit two-player river tree. All stakes/commitments are in big blinds."""

import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256

import numpy as np

from leakfinder.vendor.primitives import best_plo_hand

DECK = {r + s for r in "23456789TJQKA" for s in "cdhs"}
GROUPS = (
    "high card",
    "pair",
    "two pair",
    "trips",
    "straight",
    "flush",
    "full house",
    "quads",
    "straight flush",
)


def digest(value):
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def cards(value, count):
    hand = tuple(value.split() if isinstance(value, str) else value)
    if len(set(hand)) != count or len(hand) != count or not set(hand) <= DECK:
        raise ValueError(f"Expected {count} different legal cards: {hand}")
    return hand


@lru_cache(maxsize=100000)
def rank(hand, board):
    category, kickers, _, _ = best_plo_hand(list(hand), list(board))
    return category, tuple(kickers)


def num(value, allow_zero=False):
    val = float(value)
    if not math.isfinite(val) or val < 0 or (not allow_zero and not val):
        raise ValueError("Expected a positive finite amount")
    return val


def label(name, amount):
    return f"{name} {amount:.8f}".rstrip("0").rstrip(".")


@dataclass
class Leaf:
    value: float  # zero-sum units centered on half the starting pot
    winner: int | None


class Node:
    def __init__(self, seat, children, width):
        self.seat, self.children = seat, children
        self.regrets = np.zeros((len(children), width))
        self.sums = np.zeros_like(self.regrets)
        self.policy = np.full_like(self.regrets, 1 / len(children))


class Game:
    def __init__(self, spec):
        self.board = cards(spec["board"], 5)
        self.pot, self.stack = num(spec["pot_bb"]), num(spec["effective_stack_bb"])
        self.hands, self.weights, self.ranks = [], [], []
        if len(spec["ranges"]["oop"]) * len(spec["ranges"]["ip"]) > 16_000_000:
            raise ValueError("Dense pilot limit: sixteen million range pairs.")
        for seat in ("oop", "ip"):
            entries = {}
            for row in spec["ranges"][seat]:
                combo = tuple(sorted(cards(row["cards"], 4)))
                if set(combo).intersection(self.board) or combo in entries:
                    raise ValueError("Duplicate combo or board conflict")
                entries[combo] = num(row["weight"])
            if not entries:
                raise ValueError("Empty player range")
            hands = sorted(entries)
            w = np.array([entries[h] for h in hands])
            self.hands.append(hands)
            self.weights.append(w / w.sum())
            self.ranks.append([rank(h, self.board) for h in hands])
        masks = [
            [sum(1 << i for i, c in enumerate(sorted(DECK)) if c in h) for h in hs]
            for hs in self.hands
        ]
        legal = (
            (np.array(masks[0])[:, None] & np.array(masks[1])[None, :]) == 0
        ).astype(float)
        ranks = sorted(set(self.ranks[0] + self.ranks[1]))
        order = {v: i for i, v in enumerate(ranks)}
        scores = [np.array([order[v] for v in vec]) for vec in self.ranks]
        show = legal * np.sign(scores[0][:, None] - scores[1])
        self.legal = (legal, legal.T.copy())
        self.show = (show, -show.T.copy())
        self.mass = float(self.weights[0] @ (legal @ self.weights[1]))
        if not self.mass:
            raise ValueError("No legal joint deals")
        self.root = self.build(spec["tree"])
        self.products = None
        if os.environ.get("RIVER_TERMINAL_LIBRARY"):
            from .terminal import Product

            self.products = [
                Product(self, s, os.environ["RIVER_TERMINAL_LIBRARY"]) for s in (0, 1)
            ]

    def build(self, tree):
        pot, cap = self.pot, self.stack
        fractions = sorted({num(f) for f in tree["bet_fractions"]})
        raises = sorted({num(f) for f in tree.get("raise_fractions", [1])})
        max_raises = tree.get("max_raises", 1)
        if (
            type(max_raises) != int
            or not 0 <= max_raises <= 12
            or any(f > 1 + 1e-10 for f in fractions + raises)
        ):
            raise ValueError("Illegal pot-limit tree")
        all_in = tree.get("all_in", True)
        minimum = num(tree.get("min_bet_bb", 1))
        half = pot / 2

        def facing(seat, bet, prev, depth):
            kids = {"fold": Leaf(half + prev, 1 - seat), "call": Leaf(half + bet, None)}
            if depth < max_raises and bet < cap - 1e-8:
                pot_after_call = pot + 2 * bet
                maximum = min(cap, bet + pot_after_call)
                totals = {
                    round(min(maximum, bet + f * pot_after_call), 8) for f in raises
                }
                if all_in and cap <= maximum + 1e-8:
                    totals.add(round(cap, 8))
                for to in sorted(totals):
                    if to > bet and (
                        to - bet + 1e-8 >= bet - prev or abs(to - cap) < 1e-8
                    ):
                        kids[label("raise", to)] = facing(1 - seat, to, bet, depth + 1)
            return Node(seat, kids, len(self.hands[seat]))

        def initial(seat, after):
            kids = {"check": after}
            active = fractions if seat == 0 else tree.get("ip_bet_fractions", fractions)
            if any(not 0 < num(f) <= 1 for f in active):
                raise ValueError("Illegal bet fraction")
            bets = {round(min(cap, max(minimum, pot * f)), 8) for f in active}
            if all_in and cap <= pot and active:
                bets.add(round(cap, 8))
            for bet in sorted(bets):
                if 0 < bet <= pot + 1e-8:
                    kids[label("bet", bet)] = facing(1 - seat, bet, 0, 0)
            return Node(seat, kids, len(self.hands[seat]))

        return initial(0, initial(1, Leaf(half, None)))
