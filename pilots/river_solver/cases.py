"""Read the public demo via the application; run unchanged guide and critique on every test input."""

from dataclasses import asdict, replace
from itertools import combinations

from leakfinder import demo
from leakfinder.critique import critique_for
from leakfinder.replay_view import ReplayAction
from leakfinder.review_queue import index_guides
from leakfinder.storage import cohort, connect, initialize
from leakfinder.strategy_guide import advice_at

from .model import label

FAMILIES = {
    "river_set_small_lead": [14007],
    "river_broadway_checked_to": [9999],
    "river_check_back_value": [9999],
    "river_check_back_top_two": [2872],
    "river_call_four_flush_board": [4202],
    "river_call_large_raise": [10026],
    "value_size_40": [5983],
    "value_size_55": [1429, 95],
}


def output(s, e, a):
    c = critique_for(s, e, a)
    return {
        "street": s.street,
        "ref": s.ref,
        "played": e.action,
        "guide": a.play if a else "",
        "explanation": a.why if a else "",
        "choices": list(a.choices) if a else [],
        "size_bb": a.size.amount / s.bb if a and a.size else None,
        "critique": asdict(c) if c else None,
    }


def read_demo():
    db = connect(":memory:")
    initialize(db)
    demo.load_demo(db)
    cohort(db, "Both")
    ids = {n for ns in FAMILIES.values() for n in ns} | {8585}
    result = {}

    def inspect(e, s, a):
        n = int(s.number) % 100000
        if n in ids and s.street == "River" and (n != 10026 or e.action == "call"):
            result[n] = s, e, output(s, e, a)

    try:
        index_guides(db, "Both", audit=inspect)
    finally:
        db.close()
    assert set(result) == ids
    return result


def texture(family, base, v):
    b, h = base.board, base.hero_cards
    if family == "river_check_back_value":
        # An IP checked-to node, with a nut straight/boat replacing the demo holding.
        layouts = [
            ("8c 5s Jh 3d Tc", "Qc 9h Kh 9s"),
            ("9s 6d Qc 4h Jd", "Ks Th Ah 5h"),
            ("6s 2d 8c 3s 7h", "Ts 9h Ad 4s"),
            ("As Ac Kc 6h 2d", "Ah Kd Ks 4s"),
            ("Ks Kd Jc 4h 3s", "Kc Jd 9h 5h"),
            ("Ts 9s 2c 3d 5s", "As Ks 8c 4d"),
        ]
        b, h = [x.split() for x in layouts[v % len(layouts)]]
    if not v and family != "river_check_back_value":
        return tuple(b), tuple(h)
    ranks = "23456789TJQKA"
    if family == "river_check_back_top_two":
        windows = ["A2345"] + [ranks[i : i + 5] for i in range(9)]
        dry = [
            r
            for r in combinations(ranks, 5)
            if all(len(set(w) & set(r)) < 3 for w in windows)
        ]
        a, c, d, j, k = dry[(7 * v + v // 3) % len(dry)]
        b = (j + "c", c + "c", a + "h", k + "s", d + ("c" if v % 6 == 5 else "s"))
        side = next(r for r in reversed(ranks) if r not in (a, c, d, j, k))
        h = (k + "h", side + "s", j + "d", d + "h")
        remap = {}
    elif (
        family in ("river_call_large_raise", "value_size_40")
        or family == "river_check_back_value"
    ):
        # Translate straight windows rather than assigning a fictitious made hand.
        delta = (0, -1, 1, -2, 2)[v % 5]
        remap = {r: ranks[(i + delta) % 13] for i, r in enumerate(ranks)}
    else:
        fixed = {
            "river_set_small_lead": "7",
            "river_broadway_checked_to": "TJQKA",
            "river_check_back_top_two": "AJ",
            "river_call_four_flush_board": "Q",
            "value_size_55": "J58",
        }[family]
        movable = [x for x in ranks if x not in fixed]
        shift = (v * 3 + v // 4) % len(movable)
        remap = {x: movable[(i + shift) % len(movable)] for i, x in enumerate(movable)}
    suits = "cdhs"

    def transform(c):
        return remap.get(c[0], c[0]) + suits[(suits.index(c[1]) + v % 4) % 4]

    b = tuple(map(transform, b))
    h = list(map(transform, h))
    # Vary side-card suit/blocker while preserving rank. Rule matcher decides eligibility.
    idx = (v // 2) % 4
    focus = max(suits, key=lambda s: sum(c[1] == s for c in b))
    targets = (
        [focus] + list(suits)
        if v % 3 == 0
        else [s for s in suits if s != focus] + [focus]
    )
    for s in targets:
        cand = h[idx][0] + s
        if cand not in (*b, *h[:idx], *h[idx + 1 :]):
            h[idx] = cand
            break
    return b, tuple(h)


def case(family, n, v, record):
    orig, e, actual = record
    bb = orig.bb
    live = [s for s in orig.seats if s.live]
    hero = next(s for s in live if s.is_hero)
    opp = next(s for s in live if not s.is_hero)
    is_demo = (
        v == 0
        and family != "river_check_back_value"
        or family == "value_size_55"
        and v == 1
        and n == 95
    )
    b, h = (orig.board, orig.hero_cards) if is_demo else texture(family, orig, v)
    boundary = v >= 200
    if boundary:
        b, h = orig.board, orig.hero_cards
        if v == 203:
            h = list(h)
            if n == 14007:
                h[0] = "8d"
            if n == 4202:
                h[2] = "Jh"
            if n == 10026:
                h[2] = "Ah"
            h = tuple(h)
    old_r = [a for a in orig.history if a.street == "River"]
    P = orig.pot - sum(s.committed for s in live)
    cap = min(s.stack + s.committed for s in live)
    earlier = tuple(a for a in orig.history if a.street != "River")
    typ = (
        "facing"
        if family in ("river_set_small_lead", "river_call_four_flush_board")
        else "raise"
        if family == "river_call_large_raise"
        else "checked"
        if family.startswith("river_")
        else "lead"
    )
    if not is_demo and not boundary:
        cap = round(P * (0.85, 1.15, 1.8, 2.35, 3.3, 4.5, 6.75)[v % 7])
        if typ == "raise":
            cap = round(P * (2.5 + v % 4 * 0.6))
    if is_demo:
        street = old_r
    else:

        def move(who, action, amount=0, total=None):
            return ReplayAction("River", who, action, amount, "variant", total, False)

        if typ == "checked":
            street = [move(opp.name, "check")]
        elif typ == "lead":
            street = []
        elif typ == "facing":
            sizes = (
                (0.18, 0.22, 0.25, 0.28, 0.30, 0.32, 1 / 3, 0.34, 0.36)
                if family == "river_set_small_lead"
                else (0.68, 0.73, 0.749, 0.75, 0.751, 0.78, 0.82, 0.86, 0.95, 1)
            )
            amount = round(P * sizes[v % len(sizes)])
            if boundary:
                cutoff = 1 / 3 if n == 14007 else 0.75
                amount = round(P * cutoff) + ({200: -1, 201: 0, 202: 1, 203: 0}[v])
            street = [move(opp.name, "bet", min(cap, amount))]
        else:
            bet = round(P * (0.35, 0.45, 0.5, 0.65, 0.75, 0.85)[v % 6])
            price = (0.22, 0.245, 0.25, 0.251, 0.265, 0.28, 0.30, 0.31)[v % 8]
            to = min(cap, round((bet + price * P) / (1 - 2 * price)))
            if boundary:
                bet = old_r[0].amount
                to = round(P / 2 + 2 * bet) + ({200: -1, 201: 0, 202: 1, 203: 0}[v])
            street = [move(hero.name, "bet", bet), move(opp.name, "raise", to, to)]
    path = []
    paid = {hero.name: 0, opp.name: 0}
    for a in street:
        paid[a.name] += a.amount
        path.append(
            "check" if a.action == "check" else label(a.action, paid[a.name] / bb)
        )
    diff = paid[opp.name] - paid[hero.name]
    if is_demo:
        s = orig
    else:
        seats = tuple(
            replace(
                o,
                stack=cap - paid[o.name],
                committed=paid[o.name],
                all_in=cap == paid[o.name],
            )
            if o.live
            else o
            for o in orig.seats
        )
        s = replace(
            orig,
            board=b,
            hero_cards=h,
            seats=seats,
            history=earlier + tuple(street),
            pot=P + sum(paid.values()),
            to_call=diff,
            price_to_match=diff,
            can_raise=cap > max(paid.values()),
        )
    play = (
        e.action
        if is_demo
        else {
            "checked": "check",
            "lead": "bet",
            "facing": ("fold" if n == 14007 else "call"),
            "raise": "call",
        }[typ]
    )
    event = (
        replace(
            e,
            action=play,
            amount=(
                diff
                if play == "call"
                else 0
                if play in ("fold", "check")
                else min(s.pot, round(s.pot * 0.6))
            ),
        )
        if not is_demo
        else e
    )
    outcome = actual if is_demo else output(s, event, advice_at(s))
    displayed = outcome["size_bb"]
    rule = (outcome["critique"] or {}).get("rule")
    fired = (
        rule == family
        if not family.startswith("value_size")
        else displayed is not None
        and abs(displayed / (P / bb) - int(family[-2:]) / 100) < 0.001
    )
    base_bet = street[0].amount / P if street and street[0].action == "bet" else None
    sizes = [base_bet, 1] if base_bet else [0.33, 0.66, 1]
    if displayed is not None:
        sizes.append(min(1, displayed / (P / bb)))
    if family.startswith("value_size"):
        sizes.extend([0.4, 0.55])
        center = int(family[-2:]) / 100
        sizes.append(center + (-0.05, -0.025, 0.025, 0.05)[v % 4])
        if is_demo:
            sizes.append(min(1, e.amount / P))
    raises = [1.0]
    if typ == "raise":
        bet = street[0].amount
        raised = street[1].amount
        raises.append((raised - bet) / (P + 2 * bet))
    if typ == "checked" and family == "river_check_back_value":
        sizes.append(0.33)
    opp_seat = 0 if typ in ("checked", "facing") else 1
    metadata = {
        "family": family,
        "variant": v,
        "id": f"{family}-{v:02d}",
        "source": "demo" if is_demo else "constructed",
        "source_hand": orig.number,
        "room": orig.room,
        "app": outcome,
        "rule_active": fired,
        "matched_boundary_control": boundary,
        "line": [
            {
                "street": a.street,
                "actor": a.name,
                "move": a.action,
                "amount_bb": a.amount / bb,
            }
            for a in s.history
        ],
        "opponent_seat": opp_seat,
    }
    return (
        s,
        {
            "board": list(b),
            "pot_bb": P / bb,
            "effective_stack_bb": cap / bb,
            "tree": {
                "bet_fractions": sorted(set(sizes)),
                "raise_fractions": sorted(set(raises)),
                "all_in": True,
                "max_raises": 2,
                "min_bet_bb": 1,
            },
            "query": {"hero": list(h), "path": path},
        },
        metadata,
    )
