from __future__ import annotations

import collections
import itertools
import re
from typing import Any, Dict, List, Tuple

from .common import RANK_VALUE

ACTION_PATTERNS = [
    ("posts_ante", re.compile(r"^(.+?): posts ante ₮([0-9.]+)")),
    ("posts_sb", re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")),
    ("posts_bb", re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")),
    ("straddle", re.compile(r"^(.+?): STRADDLE ₮([0-9.]+)", re.I)),
    ("bets", re.compile(r"^(.+?): bets ₮([0-9.]+)")),
    ("calls", re.compile(r"^(.+?): calls ₮([0-9.]+)")),
    ("raises", re.compile(r"^(.+?): raises ₮([0-9.]+) to ₮([0-9.]+)")),
    ("allin", re.compile(r"^(.+?): ALLIN ₮([0-9.]+)")),
    ("return", re.compile(r"^(.+?): RETURN ₮([0-9.]+)")),
    ("collected", re.compile(r"^(.+?) collected ₮([0-9.]+) from pot")),
    ("folds", re.compile(r"^(.+?): folds")),
    ("checks", re.compile(r"^(.+?): checks")),
]


def parse_all_cards_from_brackets(text: str) -> List[str]:
    cards: List[str] = []
    for group in re.findall(r"\[([^\]]*)\]", text):
        cards.extend(group.split())
    return cards


def board_bucket(cards: List[str]) -> str:
    if not cards:
        return "no_board"
    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    vals = [RANK_VALUE.get(r, 0) for r in ranks]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    paired = any(v >= 2 for v in rank_counts.values())
    monotone = len(cards) >= 3 and len(set(suits[:3])) == 1
    flush_possible = max(suit_counts.values()) >= 3 if suit_counts else False
    broadways = sum(1 for v in vals if v >= 10)
    uniq = sorted(set(vals))
    best_window = 0
    for low in range(2, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    wet_score = 0
    if monotone or flush_possible:
        wet_score += 2
    elif len(set(suits[:3])) == 2:
        wet_score += 1
    if best_window >= 4:
        wet_score += 2
    elif best_window == 3:
        wet_score += 1
    if paired:
        return "paired"
    if monotone:
        return "monotone"
    if wet_score >= 3:
        return "wet"
    if broadways >= 2:
        return "high"
    if wet_score <= 1:
        return "dry"
    return "medium"


SEAT_RE = re.compile(r"Seat (\d+): (.+?) \(₮([0-9.]+) in chips\)")

BUTTON_RE = re.compile(r"Seat #(\d+) is the button")

KNOWN_CARD_PATTERNS = [
    re.compile(r"^(.+?):\s+shows\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+showed\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+mucks\s+\[([^\]]+)\]", re.I),
    re.compile(r"^(.+?):\s+mucked\s+\[([^\]]+)\]", re.I),
    re.compile(r"^Seat \d+:\s+(.+?)\s+showed\s+\[([^\]]+)\]", re.I),
    re.compile(r"^Seat \d+:\s+(.+?)\s+mucked\s+\[([^\]]+)\]", re.I),
]


def classify_known_hand(cards: List[str]) -> Dict[str, Any]:
    ranks = [c[0].upper() for c in cards]
    suits = [c[1].lower() for c in cards]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    vals = sorted([RANK_VALUE.get(r, 0) for r in ranks], reverse=True)
    uniq = sorted(set(vals))
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    high = sum(1 for v in vals if v >= 11)
    broadway = sum(1 for v in vals if v >= 10)
    best_window = 0
    for low in range(2, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    double_suited = sum(1 for v in suit_counts.values() if v >= 2) >= 2
    if rank_counts.get("A", 0) >= 2:
        hand_class = "AAxx"
    elif rank_counts.get("K", 0) >= 2:
        hand_class = "KKxx"
    elif pairs >= 1:
        hand_class = "pair"
    elif best_window >= 4:
        hand_class = "rundown"
    elif broadway >= 3:
        hand_class = "broadway"
    elif any(c[0].upper() == "A" and suit_counts[c[1].lower()] >= 2 for c in cards):
        hand_class = "suited_ace"
    else:
        hand_class = "other"
    return {
        "cards": " ".join(cards),
        "card_count": len(cards),
        "is_aaxx": rank_counts.get("A", 0) >= 2 and len(cards) == 4,
        "hand_class": hand_class,
        "double_suited": double_suited,
        "pairs": pairs,
        "broadways": broadway,
        "high_cards": high,
        "connected_window": best_window,
    }


def straight_high(vals: List[int]) -> int:
    s = set(vals)
    if 14 in s:
        s.add(1)
    best = 0
    for high in range(14, 4, -1):
        if all(v in s for v in range(high - 4, high + 1)):
            best = high
            break
    return best


def eval_5(cards: List[str]) -> Tuple[int, List[int], str]:
    vals = sorted([RANK_VALUE[c[0]] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    counts = collections.Counter(vals)
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    is_flush = len(set(suits)) == 1
    sh = straight_high(vals)
    if is_flush and sh:
        return (8, [sh], "straight_flush")
    if by_count[0][1] == 4:
        quad = by_count[0][0]
        kicker = max(v for v in vals if v != quad)
        return (7, [quad, kicker], "quads")
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        return (6, [by_count[0][0], by_count[1][0]], "full_house")
    if is_flush:
        return (5, vals, "flush")
    if sh:
        return (4, [sh], "straight")
    if by_count[0][1] == 3:
        trip = by_count[0][0]
        kickers = sorted([v for v in vals if v != trip], reverse=True)
        return (3, [trip] + kickers, "trips_or_set")
    pairs = sorted([v for v, c in counts.items() if c == 2], reverse=True)
    if len(pairs) >= 2:
        kicker = max(v for v in vals if v not in pairs[:2])
        return (2, pairs[:2] + [kicker], "two_pair")
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = sorted([v for v in vals if v != pair], reverse=True)
        return (1, [pair] + kickers, "one_pair")
    return (0, vals, "high_card")


def best_plo_hand(
    hole: List[str], board: List[str]
) -> Tuple[int, List[int], str, List[str]]:
    if len(hole) < 2 or len(board) < 3:
        return (-1, [], "unknown", [])
    best = (-1, [], "unknown", [])
    for hc in itertools.combinations(hole, 2):
        for bc in itertools.combinations(board, 3):
            rank = eval_5(list(hc) + list(bc))
            key = (rank[0], rank[1])
            best_key = (best[0], best[1])
            if key > best_key:
                best = (rank[0], rank[1], rank[2], list(hc) + list(bc))
    return best
