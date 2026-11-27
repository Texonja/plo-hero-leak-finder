"""Shared betting ledger and observed choices, independent of room and Hero."""

import json
from collections import defaultdict
from dataclasses import dataclass

from .bet_sizing import bet_size_key
from .importers import STREETS, Excluded
from .vendor.nodes import postflop
from .vendor.primitives import board_bucket


@dataclass(frozen=True)
class Metric:
    label: str
    street: str
    success: str
    confirmatory: bool = False


METRICS = {
    "OPEN": Metric("Raise first in", "Preflop", "raise"),
    "THREEBET": Metric("Three-bet facing a single open", "Preflop", "raise", True),
    "BET_IP_VS_MISSED_FLOP_CB": Metric(
        "Bet IP after the raiser checks", "Flop", "bet", True
    ),
    "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB": Metric(
        "Fold to an IP flop stab", "Flop", "fold"
    ),
    "BET_IP_VS_MISSED_TURN_CB": Metric(
        "Bet IP after a missed turn barrel", "Turn", "bet"
    ),
    "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB": Metric(
        "Fold to an IP turn stab", "Turn", "fold"
    ),
    "TURN_PROBE_AFTER_FLOP_CHECKBACK": Metric(
        "Probe turn after a flop check-back", "Turn", "bet"
    ),
    "FOLD_TO_TURN_PROBE": Metric("Fold to a turn probe", "Turn", "fold"),
    "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED": Metric(
        "Barrel turn after a called flop bet", "Turn", "bet"
    ),
    "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL": Metric(
        "Fold to a second barrel", "Turn", "fold"
    ),
    "FOLD_TO_RIVER_PROBE": Metric("Fold to a river probe", "River", "fold"),
    "RIVER_BARREL": Metric("Triple barrel after two calls", "River", "bet"),
    "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL": Metric(
        "Fold to a third barrel", "River", "fold"
    ),
    "RIVER_PROBE": Metric("Probe river after a turn check-back", "River", "bet"),
}


@dataclass
class Event:
    idx: int
    street: str
    player: str
    action: str
    amount: int
    pot: int
    owed: int
    remaining: dict
    live: tuple
    ref: str
    facing: str
    can_raise: bool
    allin: bool
    players_behind: int


@dataclass
class Decision:
    node: str
    event: Event
    context: str


def band(value, breaks):
    return sum(value >= boundary for boundary in breaks)


def replay(h, *, metrics=True):
    order = [h.seats[s] for s in sorted(h.seats)]
    button = order.index(h.seats[h.button])
    order = order[button + 1 :] + order[: button + 1]
    live = set(order)
    stacks = dict(h.stacks)
    events = defaultdict(list)
    start_order = {}
    pending = []
    commits = defaultdict(int)
    decisions = []
    pot = antes = price = 0
    raises = limps = 0
    opener = pfa = ""
    clean_open = False
    last_size = h.bb
    faced = ""
    street = ""
    posted = {}
    last_acted_at = {}

    def after(name):
        j = order.index(name) + 1
        return order[j:] + order[:j]

    def unfinished():
        return len(live) > 1 and any(
            stacks[p] > 0
            and (commits[p] < price or sum(stacks[n] > 0 for n in live) > 1)
            for p in pending
        )

    def emit(node, e, opponent):
        # These comparison nodes model bet/check or a response to a live price.
        # Keep an explicit fold at a free option in the ledger for review.
        if e.action == "fold" and e.owed == 0:
            return
        pool = [n for n in e.live if n != e.player]
        relevant = e.remaining.get(
            opponent, max((e.remaining[n] for n in pool), default=0)
        )
        depth = min(e.remaining[e.player], relevant) / h.bb
        board = h.boards.get(e.street, [])
        size = e.facing
        ctx = [
            h.room,
            h.currency,
            h.bb,
            antes,
            len(order),
            h.pos[e.player],
            h.pos.get(opponent, ""),
            band(depth, (40, 80, 150)),
            h.pot_type if e.street != "Preflop" else "pre",
            board_bucket(board) if board else "",
            size,
            e.can_raise if e.owed else True,
            e.players_behind if e.street == "Preflop" else 0,
        ]
        decisions.append(Decision(node, e, json.dumps(ctx, separators=(",", ":"))))

    for tok in h.tokens:
        name, k, x = tok.name, tok.kind, tok.amount
        if name not in stacks:
            raise Excluded("Action by unknown seat")
        if tok.street != street:
            if street and unfinished():
                raise Excluded("Street missing a decision")
            if street and STREETS.index(tok.street) <= STREETS.index(street):
                raise Excluded("Out-of-order street")
            street = tok.street
            if street != "Preflop" and street not in h.boards:
                raise Excluded("Missing board at decision")
            start_order[street] = [n for n in order if n in live]
            pending = [n for n in order if n in live and stacks[n] > 0]
            price = 0
            commits.clear()
            last_acted_at.clear()
            last_size = h.bb
            faced = ""
        if k in {"posts_ante", "posts_sb", "posts_bb"}:
            if street != "Preflop" or events[street] or x > stacks[name]:
                raise Excluded("Unsupported forced post")
            if k == "posts_ante":
                antes += x
            else:
                if k in posted or x != (h.sb if k == "posts_sb" else h.bb):
                    raise Excluded("Extra or short blind")
                posted[k] = name
                commits[name] += x
                price = max(price, commits[name])
                if k == "posts_bb":
                    pending = [n for n in after(name) if n in live and stacks[n] > 0]
            pot += x
            stacks[name] -= x
            continue
        if k == "return":
            if x > commits[name]:
                raise Excluded("Invalid uncalled return")
            pot -= x
            stacks[name] += x
            commits[name] -= x
            price = max(commits.values())
            continue
        if set(posted) != {"posts_sb", "posts_bb"}:
            raise Excluded("Missing forced blinds")
        expected_blinds = (
            (order[-1], order[0]) if len(order) == 2 else (order[0], order[1])
        )
        if (posted["posts_sb"], posted["posts_bb"]) != expected_blinds:
            raise Excluded("Irregular button or blinds")
        pending = [n for n in pending if n in live and stacks[n] > 0]
        if not pending or name != pending[0] or len(live) < 2:
            raise Excluded("Action order or inactive player")
        owed = max(0, price - commits[name])
        before = stacks[name]
        delta = x - commits[name] if k == "raises" else x
        if k == "ip_allin":
            # A type-7 sum can be an incremental amount or a street target.
            # The starting-stack ledger distinguishes these when it matters.
            if x == before:
                delta = x
            elif x - commits[name] == before:
                delta = before
            else:
                raise Excluded("Ambiguous iPoker all-in amount")
        if delta < 0 or delta > before:
            raise Excluded("Amount exceeds stack or invalid raise target")
        new = commits[name] + delta
        if k in {"allin", "ip_allin"}:
            if delta != before:
                raise Excluded("All-in and stack disagree")
            action = ("raise" if price else "bet") if new > price else "call"
        else:
            action = {
                "calls": "call",
                "raises": "raise",
                "bets": "bet",
                "folds": "fold",
                "checks": "check",
            }[k]
        actionable = [
            n
            for n in live
            if n != name and stacks[n] + commits[n] > price and stacks[n] > 0
        ]
        reopened = name not in last_acted_at or price - last_acted_at[name] >= last_size
        can_raise = bool(actionable and before > owed and reopened)
        if (
            action == "check"
            and owed
            or action == "call"
            and (not owed or delta != min(owed, before))
        ):
            raise Excluded("Call or check does not match live price")
        if action in {"bet", "raise"}:
            if (
                (action == "bet") != (price == 0)
                or new <= price
                or not can_raise
                or (new - price < last_size and delta != before)
            ):
                raise Excluded("Invalid aggression or raise rights")
            if delta > pot + 2 * owed:
                raise Excluded("Above pot-limit size")
        if action in {"check", "fold"} and delta:
            raise Excluded("Unexpected chip payment")
        if (
            action == "fold"
            and not owed
            and not any(stacks[n] > 0 for n in live if n != name)
        ):
            raise Excluded("No betting option against all-in opponents")
        e = Event(
            len(events[street]) + 1,
            street,
            name,
            action,
            delta,
            pot,
            owed,
            dict(stacks),
            tuple(n for n in order if n in live),
            tok.ref,
            faced,
            can_raise,
            delta == before,
            len(pending) - 1,
        )
        pending.pop(0)
        if street == "Preflop":
            if (
                metrics
                and not raises
                and not limps
                and owed
                and can_raise
                and action in {"raise", "fold", "call"}
            ):
                emit("OPEN", e, "")
            if (
                metrics
                and raises == 1
                and clean_open
                and not limps
                and can_raise
                and owed
                and name != opener
            ):
                emit("THREEBET", e, opener)
            if action == "raise":
                raises += 1
                if raises == 1:
                    opener = name
                    clean_open = not limps and new - price >= last_size
                pfa = name
            elif action == "call":
                limps += 1
        events[street].append(e)
        stacks[name] -= delta
        commits[name] = new
        pot += delta
        if action == "fold":
            live.remove(name)
        if action in {"bet", "raise"}:
            last_size = max(last_size, new - price)
            price = new
            if street == "Preflop":
                faced = f"open-{band(new / h.bb, (2.25, 3, 4))}"
            else:
                faced = bet_size_key(delta / e.pot)
            pending = [
                n for n in after(name) if n != name and n in live and stacks[n] > 0
            ]
        last_acted_at[name] = price
        if sum(stacks[n] > 0 for n in live) <= 1:
            pending = [n for n in pending if commits[n] < price]
    if not events["Preflop"] or unfinished():
        raise Excluded("Incomplete action sequence")
    if len(live) > 1 and not h.boards.get("River"):
        raise Excluded("Incomplete runout")
    h.spent = {n: h.stacks[n] - stacks[n] for n in stacks}
    h.final_commits = dict(commits)
    h.survivors = set(live)
    h.pot_type = ("limped", "srp", "3bet", "4bet+")[min(raises, 3)]
    h.last_preflop_raiser = pfa
    h.events = {s.upper(): ev for s, ev in events.items()}
    h.active_players = lambda s: start_order.get(s.title(), [])
    if not metrics:
        return decisions, events
    rows = postflop(h)

    def called(st):
        seq = events[st]
        return [e.action for e in seq] in (
            ["bet", "call"],
            ["check", "bet", "call"],
        ) and any(e.player == pfa and e.action == "bet" for e in seq)

    flop_called = called("Flop")
    turn_called = called("Turn")
    for node, e, opp in rows:
        if node not in METRICS or len(e.live) != 2:
            continue
        if (
            node
            in {
                "BET_IP_VS_MISSED_TURN_CB",
                "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB",
                "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED",
                "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL",
                "PFA_RESPONSE_AFTER_TURN_GIVEUP",
                "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL",
            }
            and not flop_called
        ):
            continue
        if node == "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL" and not turn_called:
            continue
        emit(node, e, opp)
    river = events["River"]
    headsup = all(len(start_order.get(st, [])) == 2 for st in ("Flop", "Turn", "River"))
    if headsup and river and pfa in start_order["River"]:
        oop, ip = start_order["River"]
        if flop_called and turn_called:
            ev = next((e for e in river if e.player == pfa), None)
            if ev and ev.action in {"check", "bet"} and not ev.owed:
                emit("RIVER_BARREL", ev, ip if pfa == oop else oop)
        if (
            pfa == ip
            and [e.action for e in events["Turn"]] == ["check", "check"]
            and river[0].player == oop
        ):
            emit("RIVER_PROBE", river[0], ip)
            if river[0].action == "bet" and len(river) > 1 and river[1].player == ip:
                emit("FOLD_TO_RIVER_PROBE", river[1], oop)
    return decisions, events
