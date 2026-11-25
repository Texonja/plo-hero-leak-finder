"""A post-session table snapshot immediately before an observed decision.

All chip values are integral millionths of the hand's table currency. Only the
recorded point-of-view player's dealt cards can enter a snapshot.
"""

import zlib
from collections import defaultdict
from dataclasses import dataclass, replace
from xml.etree import ElementTree as ET

from .engine import replay
from .importers import SCALE, STREETS, Record, decode
from .statistics import settled_chips


@dataclass(frozen=True)
class ActionOption:
    ref: str
    street: str
    name: str


@dataclass(frozen=True)
class ReplaySeat:
    seat: int
    name: str
    position: str
    stack: int
    live: bool
    all_in: bool
    is_hero: bool
    is_button: bool
    committed: int  # Live contribution on this street, excluding antes.


@dataclass(frozen=True)
class ReplayAction:
    street: str
    name: str
    action: str
    amount: int  # Chips moved by this action. A return moves chips to the seat.
    ref: str
    raised_to: int | None
    all_in: bool


@dataclass(frozen=True)
class ReplaySnapshot:
    room: str
    number: str
    ref: str
    street: str
    hero: str
    hero_cards: tuple[str, ...] | None
    board: tuple[str, ...]
    seats: tuple[ReplaySeat, ...]
    history: tuple[ReplayAction, ...]
    pot: int
    to_call: int  # What this seat can pay, capped by its remaining stack.
    price_to_match: int  # Additional chips owed if the seat could cover in full.
    bb: int
    currency: str
    pocket_source: str | None
    can_raise: bool
    stamp: str = ""
    sb: int = 0


@dataclass(frozen=True)
class HandResult:
    showdown: bool
    board: tuple[str, ...]
    awards: tuple
    verified: bool
    net_chips: int | None
    opponents: tuple
    subsequent: tuple


def hand_result(hand, streets, actor, ref):
    """Describe the completed record separately from the pre-decision view."""
    events = tuple(event for street in STREETS for event in streets[street])
    current = next(i for i, event in enumerate(events) if event.ref == ref)
    showdown = len(hand.survivors) > 1
    relevant = hand.survivors if showdown else set(events[current].live)
    board = next(
        (
            tuple(hand.boards[street])
            for street in reversed(STREETS)
            if street in hand.boards
        ),
        (),
    )
    net = settled_chips(hand)
    return HandResult(
        showdown,
        board,
        tuple((name, amount) for name, amount in hand.awards.items() if amount > 0),
        bool(net),
        net.get(actor),
        tuple(
            (name, tuple(hand.cards.get(name, ())))
            for name in hand.seats.values()
            if name != actor and name in relevant
        ),
        events[current + 1 :],
    )


def chip_text(value):
    """Format integer chip units exactly, including very small or large values."""
    if value is None:
        return "Unknown"
    whole, fraction = divmod(abs(value), SCALE)
    tail = f".{fraction:06d}".rstrip("0") if fraction else ""
    return f"{'-' if value < 0 else ''}{whole}{tail}"


COIN_OFFSETS = {"UTC": "+00:00", "GMT": "+00:00", "CET": "+01:00", "CEST": "+02:00"}


def record_stamp(record, date):
    if record.room != "CoinPoker":
        return date
    zone = record.raw.split("\n", 1)[0].strip().rsplit(" ", 1)[-1]
    offset = COIN_OFFSETS.get(zone)
    return date + offset if offset else ""


def _source_records(db, hand_id):
    rows = db.execute(
        """SELECT h.room,h.number,o.locator,o.raw FROM hands h JOIN origins o ON o.hand=h.id
           WHERE h.id=? AND h.conflict=0 ORDER BY o.rowid""",
        (hand_id,),
    )
    for room, number, locator, raw in rows:
        yield Record(room, zlib.decompress(raw).decode(), locator, number)


def _canonical(records):
    record = next(records, None)
    if record is None:
        raise ValueError("No accepted source for this hand.")
    return record


def _ledger(record):
    hand = decode(record)
    _, streets = replay(hand)
    events = {e.ref: e for street in STREETS for e in streets[street]}
    return hand, events


def available_actions(db, hand_id, hero=None):
    """Ordered navigable decisions. Passing a name restricts the list to that seat."""
    records = _source_records(db, hand_id)
    _, events = _ledger(_canonical(records))
    return tuple(
        ActionOption(e.ref, e.street, e.player)
        for e in events.values()
        if hero is None or e.player == hero
    )


def _visible_cards(record, hand, event):
    """Use dealt-card provenance; the normal decoder also collects showdown cards."""
    name = event.player
    cards = hand.cards.get(name) if name == hand.hint else None
    if not cards:
        return None
    if record.room == "CoinPoker":
        preceding = record.raw.splitlines()[: int(event.ref.split()[1]) - 1]
        if any(line.startswith(f"Dealt to {name} [") for line in preceding):
            return tuple(cards)
        return None

    xml = record.raw.strip()
    if record.locator.startswith("HM2") and xml.endswith("</game>"):
        xml += "</session>"
    # XML card elements belong to a betting round. Some exports serialize
    # them after the actions in that round, including the recorded player's deal.
    dealt = ET.fromstring(xml).findall('game/round[@no="1"]/cards[@type="Pocket"]')
    if any(
        node.get("player") == name and "X" not in (node.text or "").split()
        for node in dealt
    ):
        return tuple(cards)
    return None


def _apply_token(token, event, commits):
    """Use the audited engine's incremental payments and track the live street bet."""
    action = event.action if event else token.kind
    amount = event.amount if event else token.amount
    if action != "posts_ante":
        commits[token.name] += -amount if action == "return" else amount
    return ReplayAction(
        token.street,
        token.name,
        action,
        amount,
        token.ref,
        commits[token.name] if action == "raise" else None,
        event.allin if event else False,
    )


@dataclass(frozen=True)
class PlaybackFrame:
    phase: str  # start, pockets, deal, action or result
    street: str
    board: tuple[str, ...]
    seats: tuple[ReplaySeat, ...]
    pot: int  # Includes live bets. On a verified result this is the gross awarded pot.
    step: ReplayAction | None = None


def playback_frames(hand, streets, actor):
    """Build an ordered, seekable display from one validated engine replay."""
    events = {e.ref: e for street in STREETS for e in streets[street]}
    stacks = hand.stacks.copy()
    live = set(stacks)
    commits = defaultdict(int)
    pot = 0
    frames = []

    def append(phase, street, step=None):
        seats = tuple(
            ReplaySeat(
                seat,
                name,
                hand.pos[name],
                stacks[name],
                name in live,
                name in live and stacks[name] == 0,
                name == actor,
                seat == hand.button,
                commits[name],
            )
            for seat, name in sorted(hand.seats.items())
        )
        frames.append(
            PlaybackFrame(
                phase, street, tuple(hand.boards.get(street, ())), seats, pot, step
            )
        )

    def apply(step):
        nonlocal pot
        delta = -step.amount if step.action == "return" else step.amount
        stacks[step.name] -= delta
        pot += delta
        if step.action == "fold":
            live.remove(step.name)
        append("action", step.street, step)

    append("start", "Preflop")
    verified = bool(settled_chips(hand))
    # Some iPoker exports count the last unmatched bet as paid without a
    # return action. Only animate a refund that the settlement reconciled.
    refund = (
        sum(hand.spent.values()) - sum(hand.awards.values()) - hand.fees
        if verified
        else 0
    )
    by_street = defaultdict(list)
    for token in hand.tokens:
        by_street[token.street].append(token)
    for street in STREETS:
        if street != "Preflop":
            if street not in hand.boards:
                continue
            commits.clear()
            append("deal", street)
        for token in by_street[street]:
            if street == "Preflop" and token.ref == streets["Preflop"][0].ref:
                append("pockets", street)
            apply(_apply_token(token, events.get(token.ref), commits))
        if refund and street == hand.tokens[-1].street:
            name = max(hand.final_commits, key=hand.final_commits.get)
            commits[name] -= refund
            apply(ReplayAction(street, name, "return", refund, "", None, False))
    commits.clear()
    if verified:
        for name, award in hand.awards.items():
            stacks[name] += award
    append("result", frames[-1].street)
    return tuple(frames)


def _build(record, hand, events, hero, ref, cards):
    event = events.get(ref)
    if not event or event.player != hero:
        raise ValueError("Choose an observed decision by the selected seat.")
    history = []
    commits = defaultdict(int)
    street = None
    for token in hand.tokens:
        if token.street != street:
            street = token.street
            commits.clear()
        if token.ref == ref:
            break
        history.append(_apply_token(token, events.get(token.ref), commits))
    seats = tuple(
        ReplaySeat(
            seat,
            name,
            hand.pos[name],
            event.remaining[name],
            name in event.live,
            name in event.live and event.remaining[name] == 0,
            name == hero,
            seat == hand.button,
            commits[name],
        )
        for seat, name in sorted(hand.seats.items())
    )
    return ReplaySnapshot(
        hand.room,
        hand.number,
        event.ref,
        event.street,
        hero,
        cards,
        tuple(hand.boards.get(event.street, ())),
        seats,
        tuple(history),
        event.pot,
        min(event.owed, event.remaining[hero]),
        event.owed,
        hand.bb,
        hand.currency,
        record.locator if cards else None,
        event.can_raise,
        record_stamp(record, hand.date),
        hand.sb,
    )


def snapshot_from_record(record, hero, ref):
    """Build directly from a CoinPoker text, iPoker XML, or HM2 import record."""
    hand, events = _ledger(record)
    return _snapshot(record, hand, events, hero, ref)


def _snapshot(record, hand, events, hero, ref):
    event = events.get(ref)
    cards = (
        _visible_cards(record, hand, event) if event and event.player == hero else None
    )
    return _build(record, hand, events, hero, ref, cards)


def hero_decisions(db, hand_id, hero, street):
    """Yield each decision and snapshot from one replay of the canonical source."""
    records = _source_records(db, hand_id)
    record = _canonical(records)
    hand, events = _ledger(record)
    choices = [
        e
        for e in events.values()
        if e.player == hero and street in ("All streets", e.street)
    ]
    if not choices:
        return
    e = choices[0]
    view = _snapshot(record, hand, events, hero, e.ref)
    if view.hero_cards is None:
        view = _alternate_cards(view, records, e, hero)
    if view.hero_cards is None:
        return
    yield e, view
    for e in choices[1:]:
        yield (
            e,
            replace(
                _build(record, hand, events, hero, e.ref, view.hero_cards),
                pocket_source=view.pocket_source,
            ),
        )


def load_snapshot(db, hand_id, hero, ref):
    """Resolve the original reference; use compatible dealt evidence in other copies."""
    records = _source_records(db, hand_id)
    original = _canonical(records)
    hand, events = _ledger(original)
    view = _snapshot(original, hand, events, hero, ref)
    return (
        view if view.hero_cards else _alternate_cards(view, records, events[ref], hero)
    )


def _alternate_cards(view, records, event, hero):
    # Equivalent copies preserve the street and replay index of each action.
    for alternate in records:
        other = decode(alternate)
        if hero == other.hint and hero in other.cards:
            _, streets = replay(other)
            same = streets[event.street][event.idx - 1]
            if same.player == hero:
                cards = _visible_cards(alternate, other, same)
                if cards:
                    return replace(
                        view, hero_cards=cards, pocket_source=alternate.locator
                    )
    return view
