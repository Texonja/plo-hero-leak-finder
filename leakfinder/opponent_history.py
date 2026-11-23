"""Walk-forward postflop evidence for a named opponent in the local library."""

import re
import xml.etree.ElementTree as ET
import zlib
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .analysis import wilson
from .engine import replay
from .importers import Record, decode
from .replay_view import COIN_OFFSETS, _build, record_stamp
from .river_holdings import river_holdings
from .statistics import BITS
from .storage import cohort
from .vendor.primitives import best_plo_hand

MIN_CASES = 30
MIN_DATES = 3
MARGIN = 0.03


def clock(value):
    try:
        return datetime.fromisoformat(value.strip().replace("/", "-"))
    except (AttributeError, ValueError):
        return None


def finished_by(record, started):
    """Require a recorded end or a containing iPoker session's upper bound."""
    if started is None:
        return None
    if record.room == "CoinPoker":
        match = re.search(
            r"^Game ended: (\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) (\w+)\b",
            record.raw,
            re.MULTILINE,
        )
        if not match or match[2] not in COIN_OFFSETS:
            return None
        end = clock(match[1] + COIN_OFFSETS[match[2]])
    else:
        xml = record.raw.strip()
        if record.locator.startswith("HM2") and xml.endswith("</game>"):
            xml += "</session>"
        session = ET.fromstring(xml)
        start = clock(session.findtext("general/startdate"))
        duration = session.findtext("general/duration") or ""
        match = re.fullmatch(r"(\d{1,3}):([0-5]\d):([0-5]\d)", duration.strip())
        if (
            not start
            or (start.tzinfo is None) != (started.tzinfo is None)
            or start > started
            or not match
        ):
            return None
        h, m, s = map(int, match.groups())
        end = start + timedelta(hours=h, minutes=m, seconds=s)
    if end and (end.tzinfo is None) == (started.tzinfo is None) and end >= started:
        return end
    return None


def size_band(amount, pot):
    return 0 if 5 * amount <= 2 * pot else 1 if 4 * amount <= 3 * pot else 2


def context(snap, actor, size, opening_pot):
    """Match a single common decision, including its source policy context."""
    order = sorted(snap.seats, key=lambda seat: seat.seat)
    btn = next(i for i, s in enumerate(order) if s.is_button)
    active = [s.name for s in order[btn + 1 :] + order[: btn + 1] if s.live]
    pre = [
        a.name for a in snap.history if a.street == "Preflop" and a.action == "raise"
    ]
    preceding = {"Flop": "", "Turn": "Flop", "River": "Turn"}[snap.street]
    prior = [
        a
        for a in snap.history
        if a.street == preceding and a.action in {"bet", "raise"}
    ]
    role = "raiser" if pre and pre[-1] == actor else "defender" if pre else "limped"
    turn_role = (
        "active"
        if prior and prior[-1].name == actor
        else "defended"
        if prior
        else "checked"
    )
    antes = sum(a.amount for a in snap.history if a.action == "posts_ante")
    shape = (
        len({c[0] for c in snap.board}) < len(snap.board),
        max(sum(c[1] == s for c in snap.board) for s in "cdhs") >= 3,
    )
    if snap.street in {"Flop", "Turn"}:
        effective = min(s.stack + s.committed for s in order if s.live) / opening_pot
        shape += (
            max(sum(c[1] == s for c in snap.board) for s in "cdhs") == 2,
            sum(effective >= limit for limit in (1, 3, 6)),
        )
    ip = bool(active and active[-1] == actor)
    return (
        snap.currency,
        snap.sb,
        snap.bb,
        antes,
        min(len(pre), 2),
        role,
        ip,
        turn_role,
        shape,
        size_band(size, opening_pot),
    )


def postflop_opponent(snap):
    if {"Flop": 3, "Turn": 4, "River": 5}.get(snap.street) != len(snap.board):
        return None
    live = [s for s in snap.seats if s.live]
    if len(live) != 2:
        return None
    return next((s for s in live if s.name != snap.hero), None)


def river_opponent(snap):
    return postflop_opponent(snap) if snap.street == "River" else None


@dataclass(frozen=True)
class Observation:
    hand: int
    number: str
    room: str
    actor: str
    ref: str
    started: datetime
    end: datetime
    case: tuple
    outcome: str
    category: int | None = None
    air: bool = False
    cards: tuple[str, ...] = ()
    street: str = "River"


def observations(db, room, opponent=None, *, prepared=False):
    if not prepared:
        cohort(db, room)
    query = """SELECT h.id,h.room,h.number,c.hero,o.actor,o.opp,o.ref,s.locator,s.raw
        FROM cohort c JOIN hands h ON h.id=c.hand JOIN obs o ON o.hand=c.hand
        JOIN origins s ON s.rowid=(SELECT min(p.rowid) FROM origins p WHERE p.hand=c.hand)
        WHERE (o.eligible & ?)<>0 AND (c.hero=o.actor OR c.hero=o.opp)
        AND (? IS NULL OR (c.hero=o.actor AND o.opp=?) OR (c.hero=o.opp AND o.actor=?))"""
    for row in db.execute(query, (BITS["RIVERC"], opponent, opponent, opponent)):
        hid, room, number, hero, responder, bettor, ref, locator, raw = row
        record = Record(room, zlib.decompress(raw).decode(), locator, number)
        hand = decode(record)
        start = clock(record_stamp(record, hand.date))
        end = finished_by(record, start)
        if end is None:
            continue
        _, streets = replay(hand)
        events = streets["River"]
        event = next(
            (e for e in events if e.ref == ref and e.player == responder), None
        )
        if not event or event.action not in {"call", "fold", "raise"}:
            continue
        opener = events[event.idx - 2]
        if opener.action != "bet" or opener.player != bettor or len(opener.live) != 2:
            continue
        snap = _build(
            record,
            hand,
            {e.ref: e for street in streets.values() for e in street},
            bettor,
            opener.ref,
            None,
        )
        if responder != hero:
            case = context(snap, responder, opener.amount, opener.pot)
            yield Observation(
                hid, number, room, responder, ref, start, end, case, event.action
            )
            continue
        case = context(snap, bettor, opener.amount, opener.pot)
        cards = tuple(hand.cards.get(bettor, ())) if event.action == "call" else ()
        # A direct called bet followed by actual showdown can expose this bet.
        # Voluntary showing after a fold is excluded from the known subtotal.
        if cards and len(hand.survivors) == 2:
            category, kicks, _, five = best_plo_hand(cards, snap.board)
            # Count only high card or a board-only pair without private pairing.
            used_ranks = [c[0] for c in five if c in cards]
            pair = "23456789TJQKA"[kicks[0] - 2] if category == 1 else ""
            air = category == 0 or (category == 1 and pair not in used_ranks)
            yield Observation(
                hid,
                number,
                room,
                bettor,
                opener.ref,
                start,
                end,
                case,
                "bet",
                category,
                air,
                cards,
            )
        else:
            yield Observation(
                hid, number, room, bettor, opener.ref, start, end, case, "bet"
            )


def early_observations(db, room, opponent, street, *, prepared=False):
    """Read first wagers and direct responses in already heads-up spots."""
    if street not in {"Flop", "Turn"}:
        raise ValueError("Choose Flop or Turn")
    if not prepared:
        cohort(db, room)
    mask = BITS[street.upper() + "_BET"]
    query = """SELECT h.id,h.room,h.number,c.hero,s.locator,s.raw
        FROM cohort c JOIN hands h ON h.id=c.hand
        JOIN origins s ON s.rowid=(SELECT min(p.rowid) FROM origins p WHERE p.hand=c.hand)
        WHERE h.room=? AND (? IS NULL OR EXISTS (
            SELECT 1 FROM seats v WHERE v.hand=c.hand AND v.name=? AND v.name<>c.hero))
        AND EXISTS (SELECT 1 FROM obs o WHERE o.hand=c.hand AND (o.hits & ?)<>0)"""
    for hid, network, number, hero, locator, raw in db.execute(
        query, (room, opponent, opponent, mask)
    ):
        record = Record(network, zlib.decompress(raw).decode(), locator, number)
        hand = decode(record)
        start = clock(record_stamp(record, hand.date))
        end = finished_by(record, start)
        if end is None:
            continue
        _, streets = replay(hand)
        actions = streets[street]
        if not actions or len(actions[0].live) != 2 or hero not in actions[0].live:
            continue
        rival = next(name for name in actions[0].live if name != hero)
        if opponent is not None and opponent != rival:
            continue
        wager_idx = next((j for j, e in enumerate(actions) if e.action == "bet"), None)
        if wager_idx is None:
            continue
        bet = actions[wager_idx]
        snap = _build(
            record,
            hand,
            {e.ref: e for seq in streets.values() for e in seq},
            bet.player,
            bet.ref,
            None,
        )
        if bet.pot <= 0:
            continue
        case = context(snap, rival, bet.amount, bet.pot)
        if bet.player == rival:
            yield Observation(
                hid,
                number,
                network,
                rival,
                bet.ref,
                start,
                end,
                case,
                "bet",
                street=street,
            )
        elif (
            wager_idx == 1
            and actions[0].player == rival
            and actions[0].action == "check"
            and bet.amount < min(seat.stack for seat in snap.seats if seat.live)
            and wager_idx + 1 < len(actions)
        ):
            response = actions[wager_idx + 1]
            if response.player == rival and response.action in {
                "fold",
                "call",
                "raise",
            }:
                yield Observation(
                    hid,
                    number,
                    network,
                    rival,
                    response.ref,
                    start,
                    end,
                    case,
                    response.action,
                    street=street,
                )


@dataclass(frozen=True)
class Sample:
    comparable: tuple[Observation, ...]
    broader: int
    previous: tuple[Observation, ...] = ()

    @property
    def days(self):
        return len({r.started.date() for r in self.comparable})

    @property
    def ready(self):
        return len(self.comparable) >= MIN_CASES and self.days >= MIN_DATES


class OpponentHistory:
    def __init__(self, rows=()):
        self.rows = defaultdict(list)
        for row in rows:
            self.rows[row.room, row.actor, row.street, row.outcome == "bet"].append(row)

    def sample(self, snap, size=None):
        opponent = postflop_opponent(snap)
        start = clock(snap.stamp)
        if (
            opponent is None
            or start is None
            or snap.pot <= 0
            or any(a.street == snap.street and a.action == "fold" for a in snap.history)
        ):
            return Sample((), 0)
        wagers = [
            a
            for a in snap.history
            if a.street == snap.street and a.action in {"bet", "raise"}
        ]
        facing = snap.to_call > 0
        rows = tuple(
            r
            for r in self.rows[snap.room, opponent.name, snap.street, facing]
            if r.number != snap.number
            and (r.end.tzinfo is None) == (start.tzinfo is None)
            and r.end < start
        )
        if facing:
            if (
                len(wagers) != 1
                or wagers[0].action != "bet"
                or wagers[0].name != opponent.name
            ):
                return Sample((), len(rows), rows)
            wager = wagers[0].amount
            pot = snap.pot - wager
        else:
            if wagers or not size:
                return Sample((), 0)
            wager, pot = size, snap.pot
        if pot <= 0:
            return Sample((), 0)
        case = context(snap, opponent.name, wager, pot)
        return Sample(tuple(r for r in rows if r.case == case), len(rows), rows)


def heads_up_price(snap):
    if not snap.to_call:
        return 0.0
    after = snap.pot + 2 * snap.to_call - snap.price_to_match
    return snap.to_call / after if after > 0 else 1.0


def betting_limits(sample, category, allowed=None):
    n = len(sample.comparable)
    if not n:
        return (0.0, 1.0), (0.0, 1.0), 0, 0
    shown = [r for r in sample.comparable if r.category is not None]
    air = sum(r.air for r in shown)
    unknown = n - len(shown)
    transferable = (
        shown if allowed is None else [r for r in shown if r.category in allowed]
    )
    weak = sum(r.category < category for r in transferable)
    stronger = sum(r.category > category for r in transferable)
    return (
        (air / n, (air + unknown) / n),
        (wilson(weak / n, n)[0], wilson((n - stronger) / n, n)[1]),
        len(shown),
        air,
    )


def responses(sample):
    rows = sample.comparable
    return tuple(
        sum(r.outcome == action for r in rows) for action in ("fold", "call", "raise")
    )


def current_categories(snap):
    holdings = river_holdings(snap.hero_cards, snap.board)
    return {g.category for g in (*holdings.stronger, *holdings.ties, *holdings.weaker)}


def apply_history(snap, base, history):
    """Change a suggestion only when conservative matched historical bounds clear its price."""
    if base is None or river_opponent(snap) is None or not snap.hero_cards:
        return base
    if snap.to_call:
        if "raise" in base.choices:
            return base
        sample = history.sample(snap)
        if not sample.ready:
            return base
        category = best_plo_hand(snap.hero_cards, snap.board)[0]
        # A weak hand with rank zero can still beat weaker kickers. The broad
        # same-class uncertainty remains in the upper limit.
        _, (lo, hi), shown, _ = betting_limits(
            sample, category, current_categories(snap)
        )
        price = heads_up_price(snap)
        action = (
            "call" if lo > price + MARGIN else "fold" if hi < price - MARGIN else None
        )
        if action and base.choices != (action,):
            note = f"In this spot, {shown}/{len(sample.comparable)} past bets showed cards. The conservative range based on prior hand categories is {lo:.1%}–{hi:.1%}. The current call threshold is {price:.1%}. Consider a {action} if the opponent's past behavior carries over to this spot. Base guide · {base.play or 'manual review'}."
            return replace(
                base,
                title="River · opponent history",
                choices=(action,),
                why=note,
            )
        return base
    if (
        not base.size
        or "bluff with a blocker" not in base.title
        or best_plo_hand(snap.hero_cards, snap.board)[0] != 0
    ):
        return base
    sample = history.sample(snap, base.size.amount)
    if not sample.ready:
        return base
    f, c, r = responses(sample)
    n = f + c + r
    lo = wilson(f / n, n)[0]
    threshold = base.size.amount / (snap.pot + base.size.amount)
    if lo > threshold + MARGIN:
        note = f"Among {n} comparable responses to this size group there were {f} folds, {c} calls and {r} raises. The lower Wilson bound is {lo:.1%}. A pure bluff needs {threshold:.1%}folds. This supports betting if checking has negligible showdown value. Base guide · {base.play}."
        return replace(
            base,
            title="River · bluff supported by fold history",
            choices=("bet",),
            why=note,
        )
    return base
