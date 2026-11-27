"""Lossless source carriers and decoding for regular four-card cash games."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path, PurePosixPath

from .vendor.common import assign_positions, parse_header, split_hand_history
from .vendor.hm2_backup import MissingHandhistories, iter_hm2_handhistories
from .vendor.primitives import (
    ACTION_PATTERNS,
    BUTTON_RE,
    KNOWN_CARD_PATTERNS,
    SEAT_RE,
    parse_all_cards_from_brackets,
)

STREETS = ("Preflop", "Flop", "Turn", "River")
SCALE = 1_000_000


class Excluded(ValueError):
    """A hand outside coverage or with unverified semantics."""


MONEY_PREFIX = re.compile(r"^(?:€|\$|£|₮|EUR|USD|GBP)\s*")
GROUPED = re.compile(r"[1-9]\d{0,2}([.,])\d{3}(?:\1\d{3})*")


@lru_cache(maxsize=32768)
def money(raw):
    token = MONEY_PREFIX.sub("", (raw or "").strip())
    if "," in token and "." in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        token = token.replace("." if decimal_sep == "," else ",", "")
    token = token.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,6})?", token):
        raise Excluded("Invalid or missing chip amount")
    return int(Decimal(token) * SCALE)


def ipoker_amount_fields(meta, game, *, include_totals=False):
    tags = ("smallblind", "bigblind")
    if include_totals:
        tags += ("bets", "wins", "chipsin", "chipsout")
    for tag in tags:
        value = meta.findtext(tag)
        if value:
            yield value
    for player in game.iterfind("general/players/player"):
        for key in ("chips", "bet", "win", "rakeamount"):
            if key in player.attrib:
                yield player.attrib[key]
    for action in game.iterfind("round/action"):
        if "sum" in action.attrib:
            yield action.attrib["sum"]


@lru_cache(maxsize=32768)
def _number_parts(raw):
    token = MONEY_PREFIX.sub("", raw.strip())
    grouped = GROUPED.fullmatch(token)
    return token, grouped[1] if grouped else ""


def ipoker_money_reader(meta, game):
    """Resolve 1,234 / 1.234 only with evidence from this original record."""
    decimal = None

    def read(raw):
        nonlocal decimal
        token, mark = _number_parts(raw or "")
        if not mark:
            return money(raw)
        if token.count(mark) > 1:
            return money(token.replace(mark, ""))
        if decimal is None:
            evidence = set()
            for value in ipoker_amount_fields(meta, game, include_totals=True):
                other, sep = _number_parts(value)
                if sep:
                    if other.count(sep) > 1:
                        evidence.add("." if sep == "," else ",")
                elif re.fullmatch(r"\d+(?:[.,]\d+)+", other):
                    evidence.add(other[max(other.rfind("."), other.rfind(","))])
            if len(evidence) != 1:
                raise Excluded("Ambiguous iPoker chip separators")
            decimal = evidence.pop()
        return money(token if mark == decimal else token.replace(mark, ""))

    return read


@dataclass
class Token:
    street: str
    name: str
    kind: str
    amount: int
    ref: str


@dataclass
class Hand:
    room: str
    number: str
    date: str
    currency: str
    sb: int
    bb: int
    button: int
    hint: str
    seats: dict = field(default_factory=dict)
    stacks: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)
    boards: dict = field(default_factory=dict)
    tokens: list = field(default_factory=list)
    awards: dict = field(default_factory=dict)
    reported_bets: dict = field(default_factory=dict)
    fees: int = 0
    reported_pot: int | None = None
    ipoker_session: tuple[str, str] = ("", "")
    ipoker_hero_dealt: bool = False

    def validate(self):
        if not self.number or self.bb <= 0 or not 0 < self.sb <= self.bb:
            raise Excluded("Missing hand ID or invalid blinds")
        if (
            self.button not in self.seats
            or not 2 <= len(self.seats) <= 9
            or len(set(self.seats.values())) != len(self.seats)
        ):
            raise Excluded("Unresolved button or seats")
        self.pos = assign_positions(self.seats, self.button)
        board = []
        for n, street in enumerate(STREETS[1:], 3):
            if street in self.boards:
                cards = self.boards[street]
                if (
                    (n > 3 and len(board) != n - 1)
                    or len(cards) != n
                    or cards[: len(board)] != board
                ):
                    raise Excluded("Inconsistent board streets")
                board = cards
        seen = list(board)
        for name, cards in self.cards.items():
            if name not in self.stacks or len(cards) != 4:
                raise Excluded("Unsupported pocket count or owner")
            seen.extend(cards)
        if len(seen) != len(set(seen)) or any(
            not re.fullmatch(r"[2-9TJQKA][cdhs]", c) for c in seen
        ):
            raise Excluded("Invalid or duplicate card")
        return self

    def fingerprint(self):
        public = [
            self.room,
            self.number,
            self.date,
            self.currency,
            self.sb,
            self.bb,
            self.button,
            sorted(self.seats.items()),
            sorted(self.stacks.items()),
            self.boards,
            [(a.street, a.name, a.kind, a.amount) for a in self.tokens],
        ]
        return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


@dataclass
class Record:
    room: str
    raw: str
    locator: str
    expected: str = ""
    skip: str = ""


def sources(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            yield from _zip_sources(path, archive)
    elif path.suffix.lower() in {".pghembk", ".sql"}:
        yield from _hm2_sources(path)
    else:
        yield from _native_sources(path.read_bytes())


def _hm2_sources(path):
    for row in iter_hm2_handhistories(path):
        reason = "Tournament" if row.tourneynumber else ""
        if not row.handhistory.lstrip().startswith("<"):
            reason = "Unsupported HM2 hand-history dialect"
        yield Record(
            "iPoker",
            row.handhistory,
            f"HM2 row {row.handhistory_id}",
            str(row.gamenumber),
            reason,
        )


def _zip_sources(path, archive):
    native = []
    has_sql = False
    for index, member in enumerate(archive.infolist(), 1):
        name = PurePosixPath(member.filename.replace("\\", "/"))
        if member.is_dir() or "__MACOSX" in name.parts or name.name.startswith("._"):
            continue
        suffix = name.suffix.lower()
        if suffix in {".txt", ".xml"}:
            native.append((index, member))
        elif suffix == ".sql":
            has_sql = True

    # Keep the backup reader and its existing locators for HM2 archives.
    if has_sql or (not native and path.suffix.lower() == ".pghembk"):
        try:
            yield from _hm2_sources(path)
        except MissingHandhistories:
            if not native:
                raise
        else:
            return

    for index, member in native:
        locator = f"ZIP entry {index}: {member.filename}"
        data = archive.read(member)
        try:
            yield from _native_sources(data, locator=locator)
        except (ET.ParseError, ValueError, LookupError) as exc:
            # Isolate malformed histories from other files in the same archive.
            yield Record("", "", locator, skip=str(exc))


def _native_sources(data, *, locator=""):
    enc = (
        "utf-16"
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        else "utf-8-sig"
    )
    if data[:1] == b"<":
        decl = re.match(rb"""<\?xml[^>]*encoding=["']([^"']+)""", data[:200], re.I)
        if decl:
            enc = decl[1].decode("ascii")
    text = data.decode(enc)
    suffix = f" · {locator}" if locator else ""
    if text.lstrip().startswith("<"):
        root = ET.fromstring(text)
        if root.tag != "session":
            raise Excluded("Expected an iPoker session element")
        games = list(re.finditer(r"<game(?=\s)[^>]*>.*?</game\s*>", text, re.S))
        if len(games) != len(root.findall("game")) or not games:
            raise Excluded("Unsupported XML game boundaries")
        header = text[: games[0].start()]
        for i, (match, game) in enumerate(zip(games, root.findall("game")), 1):
            yield Record(
                "iPoker",
                header + match.group() + "\n</session>",
                f"XML game {i}{suffix}",
                game.get("gamecode", ""),
            )
    elif not locator or "CoinPoker Hand #" in text:
        for i, block in enumerate(split_hand_history(text), 1):
            yield Record("CoinPoker", block, f"Text hand {i}{suffix}")


def remember(cards, name, pocket):
    if name in cards and cards[name] != pocket:
        raise Excluded("Conflicting pocket cards")
    cards[name] = pocket


def coin(raw):
    lines = raw.splitlines()
    meta = parse_header(lines[0]) if lines else None
    if (
        not meta
        or meta["plo_cards"] != 4
        or meta["is_bombpot"]
        or not re.fullmatch(r"PLO\s+4", meta["desc"], re.I)
    ):
        raise Excluded("Outside regular CoinPoker PLO4 cash")
    if re.search(
        r": STRADDLE\b|\*\*\* (?:FIRST|SECOND|THIRD) |Hand was run (?:twice|three)",
        raw,
        re.I,
    ):
        raise Excluded("Straddle or multiple runouts")
    if "*** SUMMARY ***" not in raw:
        raise Excluded("Incomplete CoinPoker hand")
    stakes = re.findall(r"₮([\d.]+)", meta["stakes_str"])
    button = BUTTON_RE.search(raw)
    if len(stakes) < 2 or not button:
        raise Excluded("Unsupported CoinPoker stakes or button")
    h = Hand(
        "CoinPoker",
        meta["hand_id"],
        meta["dt"].replace("/", "-"),
        "USDT",
        money(stakes[0]),
        money(stakes[1]),
        int(button[1]),
        "",
    )
    street = "Preflop"
    finished = False
    for idx, line in enumerate(lines, 1):
        award = re.fullmatch(
            r"(.+?) collected ₮([\d.]+) from (?:pot|(?:main|side) pot(?:[- #]\d+)?)",
            line,
            re.I,
        )
        if award:
            h.awards[award[1]] = h.awards.get(award[1], 0) + money(award[2])
            continue
        if line.startswith("Total pot "):
            charges = re.findall(r"₮([\d.]+)", line)
            if charges:
                h.reported_pot = money(charges[0])
                h.fees = sum(
                    money(x)
                    for x in re.findall(
                        r"(?:Rake|Splash Fee|Jackpot Fee) ₮([\d.]+)", line, re.I
                    )
                )
        seat = SEAT_RE.fullmatch(line)
        if seat:
            if int(seat[1]) in h.seats or seat[2] in h.stacks:
                raise Excluded("Duplicate CoinPoker seat")
            h.seats[int(seat[1])] = seat[2]
            h.stacks[seat[2]] = money(seat[3])
            continue
        if line.startswith("Dealt to ") and " [" in line:
            name = line[9 : line.index(" [")]
            if h.hint and h.hint != name:
                raise Excluded("Multiple dealt-to identities")
            h.hint = name
            remember(h.cards, name, parse_all_cards_from_brackets(line))
            continue
        for pat in KNOWN_CARD_PATTERNS:
            match = pat.match(line)
            if match and match[1] in h.stacks:
                remember(h.cards, match[1], match[2].split())
                break
        if line.startswith(("*** SUMMARY", "*** SHOWDOWN", "*** SHOW DOWN")):
            finished = True
        if finished:
            continue
        marker = re.match(r"\*\*\* (FLOP|TURN|RIVER) \*\*\*", line)
        if marker:
            street = marker[1].title()
            h.boards[street] = parse_all_cards_from_brackets(line)
            continue
        for kind, pat in ACTION_PATTERNS:
            m = pat.fullmatch(line)
            if m:
                if kind not in {"straddle", "collected"}:
                    amt = (
                        money(m[3] if kind == "raises" else m[2])
                        if kind not in {"checks", "folds"}
                        else 0
                    )
                    h.tokens.append(Token(street, m[1], kind, amt, f"Line {idx}"))
                break
        else:
            if (
                ": " in line
                and line.split(": ", 1)[0] in h.stacks
                and not re.search(r": (?:shows|mucks|showed|mucked)\b", line)
            ):
                raise Excluded("Unknown CoinPoker action")
    return h.validate()


def ip_card(s):
    if not re.fullmatch(r"[CDHS](?:10|[2-9TJQKA])", s.upper()):
        raise Excluded("Unknown iPoker card format")
    return s[1:].upper().replace("10", "T") + s[0].lower()


IP_ACTION = {
    "0": "folds",
    "1": "posts_sb",
    "2": "posts_bb",
    "3": "calls",
    "4": "checks",
    "5": "bets",
    "7": "ip_allin",
    "23": "raises",
}


def ipoker_session(raw, fragment=False):
    sample = raw.strip()
    if fragment and sample.endswith("</game>") and "</session>" not in sample:
        sample += "</session>"
    return ET.fromstring(sample)


def ipoker(raw, fragment=False):
    root = ipoker_session(raw, fragment)
    games = root.findall("game")
    if root.tag != "session" or len(games) != 1:
        raise Excluded("Expected one iPoker game")
    g = games[0]
    meta = root.find("general")
    if meta is None:
        raise Excluded("Missing iPoker session metadata")
    desc = meta.findtext("gametype", "")
    if (
        not re.match(r"^(?:Omaha(?:\s+4)?\s+(?:PL|Pot Limit)|PL Omaha)\b", desc, re.I)
        or re.search(
            r"(?:5|6)[ -]?card|hi.?lo|bomb|straddle|tournament|omaha\s+[56]\b",
            desc,
            re.I,
        )
        or any(
            meta.findtext(k, "").strip() for k in ("tournamentname", "tournamentcode")
        )
    ):
        raise Excluded("Outside regular iPoker PLO4 cash")
    cur = meta.findtext("tablecurrency") or meta.findtext("currency")
    if not cur:
        raise Excluded("Missing table currency")
    amount = ipoker_money_reader(meta, g)

    def blind(tag, code):
        value = meta.findtext(tag)
        if value:
            return amount(value)
        posts = g.findall(f'round[@no="0"]/action[@type="{code}"]')
        if len(posts) != 1:
            raise Excluded("Ambiguous blind amount")
        return amount(posts[0].get("sum"))

    h = Hand(
        "iPoker",
        g.get("gamecode", ""),
        g.findtext("general/startdate", ""),
        cur,
        blind("smallblind", "1"),
        blind("bigblind", "2"),
        -1,
        meta.findtext("nickname", "").strip(),
    )
    h.ipoker_session = (meta.findtext("startdate") or "", meta.findtext("duration") or "")
    for p in g.findall("general/players/player"):
        seat = int(p.get("seat", "0"))
        name = p.get("name", "").strip()
        if seat in h.seats or not name:
            raise Excluded("Invalid player seat")
        h.seats[seat] = name
        h.stacks[name] = amount(p.get("chips"))
        if p.get("bet") is not None:
            h.reported_bets[name] = amount(p.get("bet"))
        if p.get("win") is not None:
            h.awards[name] = amount(p.get("win"))
        if p.get("rakeamount") is not None:
            h.fees += amount(p.get("rakeamount"))
        if p.get("dealer") == "1":
            if h.button != -1:
                raise Excluded("Multiple dealer buttons")
            h.button = seat
    pocket_evidence = False
    board = []
    last_no = -1
    prior_round = -1
    for rnd in g.findall("round"):
        rn = int(rnd.get("no", "-1"))
        if rn not in range(5) or rn <= prior_round:
            raise Excluded("Unknown or repeated iPoker round")
        prior_round = rn
        street = STREETS[max(0, rn - 1)]
        for el in rnd:
            if el.tag == "action":
                typ = el.get("type")
                no = int(el.get("no", "-1"))
                if typ not in IP_ACTION or no <= last_no:
                    raise Excluded("Unknown iPoker action or sequence")
                last_no = no
                h.tokens.append(
                    Token(
                        street,
                        el.get("player", ""),
                        IP_ACTION[typ],
                        amount(
                            el.get("sum", "0") if typ in {"0", "4"} else el.get("sum")
                        ),
                        f"Action {no}",
                    )
                )
            elif el.tag == "cards":
                typ = el.get("type")
                slots = (el.text or "").split()
                if typ == "Pocket":
                    if len(slots) != 4:
                        raise Excluded("Unsupported iPoker pocket count")
                    pocket_evidence = True
                    if "X" not in slots:
                        name = el.get("player", "")
                        remember(
                            h.cards, name, [ip_card(x) for x in slots]
                        )
                        if rnd.get("no") == "1" and name == h.hint:
                            h.ipoker_hero_dealt = True
                elif typ in STREETS[1:] and typ == street:
                    if street in h.boards:
                        raise Excluded("Multiple runouts")
                    board.extend(ip_card(x) for x in slots)
                    h.boards[street] = list(board)
                else:
                    raise Excluded("Unknown iPoker board")
            else:
                raise Excluded("Unknown iPoker round event")
    if not pocket_evidence or prior_round < 1:
        raise Excluded("Missing four-card evidence")
    return h.validate()


def decode(record):
    if record.skip:
        raise Excluded(record.skip)
    h = (
        coin(record.raw)
        if record.room == "CoinPoker"
        else ipoker(record.raw, record.locator.startswith("HM2"))
    )
    if record.expected and h.number != record.expected:
        raise Excluded("Container and hand ID conflict")
    return h
