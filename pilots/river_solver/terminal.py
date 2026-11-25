"""Optional exact terminal-product accelerator. The CFR updates and tree are unchanged."""

import ctypes
from bisect import bisect_left, bisect_right
from collections import defaultdict
from functools import lru_cache
from itertools import combinations

import numpy as np


@lru_cache
def kernel(path):
    fn = ctypes.CDLL(path).terminal_product
    ints = np.ctypeslib.ndpointer(np.int32, flags="C_CONTIGUOUS")
    reals = np.ctypeslib.ndpointer(np.float64, flags="C_CONTIGUOUS")
    fn.argtypes = [
        ints,
        ctypes.c_int,
        ints,
        ints,
        ctypes.c_int,
        reals,
        ctypes.c_int,
        reals,
        reals,
    ]
    fn.restype = None
    return fn


def subsets(hand):
    for k in range(5):
        yield from combinations(hand, k)


class Product:
    def __init__(self, game, seat, path):
        self.fn = kernel(path)
        groups = defaultdict(list)
        other = 1 - seat
        for j in sorted(
            range(len(game.hands[other])), key=game.ranks[other].__getitem__
        ):
            for key in subsets(game.hands[other][j]):
                groups[key].append(j)
        order = []
        locations = {}
        for key, members in groups.items():
            locations[key] = len(order), [game.ranks[other][j] for j in members]
            order.extend([-1, *members])
        offsets, terms = [0], []
        for hand, rank in zip(game.hands[seat], game.ranks[seat]):
            for key in subsets(hand):
                if key in locations:
                    start, ranks = locations[key]
                    terms.append(
                        (
                            start + bisect_left(ranks, rank),
                            start + bisect_right(ranks, rank),
                            start + len(ranks),
                            -1 if len(key) % 2 else 1,
                        )
                    )
            offsets.append(len(terms))
        self.order = np.asarray(order, dtype=np.int32)
        self.offsets = np.asarray(offsets, dtype=np.int32)
        self.terms = np.asarray(terms, dtype=np.int32)
        self.prefix = np.empty(len(order))
        self.width = len(offsets) - 1

    def __call__(self, reach, showdown):
        values = np.empty(self.width)
        self.fn(
            self.order,
            len(self.order),
            self.offsets,
            self.terms,
            self.width,
            reach,
            showdown,
            self.prefix,
            values,
        )
        return values
