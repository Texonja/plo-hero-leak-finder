"""Metric dictionary and descriptive facts from the shared, validated action prefix."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise

from .bet_sizing import MATCH_LABELS, bet_size_key
from .engine import METRICS
from .importers import STREETS


@dataclass(frozen=True)
class Definition:
    label: str
    street: str
    numerator: str
    denominator: str
    fields: str
    exclusions: str


DEFS = {}


def metric(
    key,
    label,
    street,
    numerator,
    denominator,
    fields,
    exclusions="Excluded globally: conflicting hand or Hero identity, incomplete or irregular betting, unsupported game/variant, straddle and multiple runouts.",
):
    DEFS[key] = Definition(label, street, numerator, denominator, fields, exclusions)


pf = "Preflop"
pfields = (
    "Button, seats, blinds, stacks, ordered preflop commitments and legal raise rights."
)
for key, label, hit, den in [
    (
        "VPIP",
        "VPIP",
        "Hands with a voluntary preflop call or raise",
        "All dealt-in actor hands, including walks",
    ),
    (
        "PFR",
        "PFR",
        "Hands with at least one preflop raise",
        "All dealt-in actor hands, including walks",
    ),
    (
        "FOURBET",
        "Four-bet after opening",
        "Opener makes the next raise",
        "Original opener faces exactly a full three-bet, with legal raising rights and no cold caller of the three-bet",
    ),
    (
        "FOLD3",
        "Fold versus a three-bet",
        "Opener folds",
        "Original opener faces exactly one full three-bet, with no intervening cold caller",
    ),
    (
        "CALL3",
        "Call versus a three-bet",
        "Opener calls",
        "Same opportunity as fold versus three-bet, including short calls",
    ),
    (
        "SQUEEZE",
        "Squeeze after open and callers",
        "Actor re-raises",
        "First voluntary decision facing one full open and at least one intervening caller, with legal raising rights; no prior limp",
    ),
    (
        "COLD",
        "Cold-call a single open",
        "Actor calls",
        "First decision outside either blind facing a full opening raise, with no intervening caller or prior limp",
    ),
    (
        "DEFEND",
        "Continue from a blind versus an open",
        "Blind calls or raises",
        "SB or BB facing one full open with no prior limp or intervening caller",
    ),
]:
    extra = (
        ""
        if key in {"VPIP", "PFR"}
        else " Exclude limped opens and all-in opening or re-raising actions preceding this choice."
    )
    metric(key, label, pf, hit, den + extra, pfields)
conditions = {
    "OPEN": "Action facing only the blinds, no voluntary calls, raising legally available; excludes a walked BB.",
    "THREEBET": "First choice after one full open, no previous limp or intervening caller, with a legal raise available.",
    "BET_IP_VS_MISSED_FLOP_CB": "Heads-up, final preflop raiser checks first OOP; next IP action is bet or check.",
    "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB": "OOP preflop raiser checks, IP bets, OOP responds.",
    "BET_IP_VS_MISSED_TURN_CB": "Heads-up OOP raiser bets flop, IP calls; OOP checks turn and IP acts.",
    "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB": "Same flop line; OOP checks turn, IP bets and OOP responds.",
    "TURN_PROBE_AFTER_FLOP_CHECKBACK": "OOP checks flop, IP preflop raiser checks back, OOP acts first on turn.",
    "FOLD_TO_TURN_PROBE": "OOP probes after IP preflop raiser's flop check-back; IP responds.",
    "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED": "Flop c-bet and call without a raise; surviving raiser has an unobstructed turn bet/check.",
    "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL": "Defender called a flop c-bet, now faces the raiser's turn barrel.",
    "RIVER_BARREL": "Raiser's flop and turn bets both called without raises; unobstructed river bet/check.",
    "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL": "Same flop/turn line; defender faces raiser's river barrel.",
    "RIVER_PROBE": "Heads-up from flop, IP preflop raiser checks back turn, OOP acts first on river.",
    "FOLD_TO_RIVER_PROBE": "OOP bets river after IP preflop raiser's turn check-back; IP responds.",
}
for k, m in METRICS.items():
    metric(
        k,
        m.label,
        m.street,
        f"Recorded action is {m.success}",
        conditions[k],
        pfields
        if m.street == pf
        else "Preflop aggressor, active positions, ordered street actions, current price and board.",
    )

postfields = "Ordered HU actions, live seats, preflop aggressor, current street pot and commitments."
for k, label, street, num, den in [
    (
        "FCB",
        "Flop continuation bet",
        "Flop",
        "Raiser bets",
        "Heads-up final preflop raiser reaches first bet/check option, first OOP or checked to IP",
    ),
    (
        "FFC",
        "Fold facing a flop c-bet · audit required",
        "Flop",
        "Defender folds",
        "Heads-up defender actually faces first flop bet by final preflop raiser after a legal check if OOP",
    ),
    (
        "DELAY",
        "Delayed turn c-bet",
        "Turn",
        "Raiser bets turn",
        "HU flop checks through; preflop raiser has first unobstructed turn bet/check",
    ),
    (
        "TURNF",
        "Fold facing a turn bet",
        "Turn",
        "Responder folds",
        "First response to a HU opening turn bet; excludes raises faced",
    ),
    (
        "RIVERC",
        "Call facing a river bet",
        "River",
        "Responder calls",
        "First response to a HU opening river bet",
    ),
    (
        "RIVERF",
        "Fold facing a river bet",
        "River",
        "Responder folds",
        "First response to a HU opening river bet",
    ),
]:
    if k == "FFC":
        den += ". The external HUD denominator is unresolved. This observed line is for descriptive review only."
    metric(k, label, street, num, den, postfields)
for street in STREETS[1:]:
    name = street.upper()
    metric(
        name + "_BET",
        street + " bet frequency",
        street,
        "Actor bets",
        "HU actionable checks or opening bets facing zero price",
        postfields,
    )
    metric(
        name + "_AGG",
        street + " aggression frequency",
        street,
        "Actor bets or raises",
        "All observed HU voluntary decisions including checks, calls and folds",
        postfields,
    )
    metric(
        name + "_SIZE",
        street + " bet sizing",
        street,
        "Opening bets in each chosen-size band",
        "All HU opening bets with positive pot before bet",
        postfields,
    )
    if street != "River":
        metric(
            name + "_XR",
            street + " check-raise",
            street,
            "Checking actor raises",
            "HU OOP checks, faces IP's first bet and can legally raise",
            postfields,
        )
metric(
    "SHOW",
    "Reached showdown",
    "Showdown",
    "Actor remains live on completed river with at least one live opponent",
    "Dealt-in actors who saw the flop, including all-in runouts",
    "Board, folds, completed betting.",
)
metric(
    "WONSHOW",
    "Won a share at showdown",
    "Showdown",
    "Actor has a positive recorded award, including split pots",
    "Actors reaching showdown in hands with reconciled awards",
    "River board, active seats, collections, contributions, fees.",
)
metric(
    "NET",
    "Realized result",
    "Results",
    "Sum of verified net big blinds",
    "Distinct selected actor hands with reconciled settlements; display BB/100 and coverage",
    "Actual payouts, uncalled returns, pot, rake/fees, stakes and investments.",
)
BITS = {k: 1 << i for i, k in enumerate(DEFS)}
assert len(BITS) < 63


def tags(cards):
    if len(cards) != 4:
        return ["Unknown"]
    rank = "23456789TJQKA"
    vals = [rank.index(c[0]) + 2 for c in cards]
    counts = Counter(vals)
    suit = Counter(c[1] for c in cards)
    shape = sorted(suit.values(), reverse=True)
    labels = [
        {
            (2, 2): "Double suited",
            (2, 1, 1): "Single suited",
            (1, 1, 1, 1): "Rainbow",
            (3, 1): "Three of a suit",
            (4,): "Four of a suit",
        }[tuple(shape)]
    ]
    if any(c[0] == "A" and suit[c[1]] >= 2 for c in cards):
        labels.append("Suited ace")
    if counts[14] >= 2:
        labels.append("AAxx")
    if counts[14] == 2 and shape == [2, 2] and all(v >= 10 for v in vals):
        labels.append("Premium AAxx")
    if max(counts.values()) >= 2:
        labels.append("Paired")
    if sorted(counts.values()) == [2, 2]:
        labels.append("Double paired")
    if max(counts.values()) >= 3:
        labels.append("Trips or quads")
    if len(counts) == 4:
        sets = [sorted(vals)]
        if 14 in vals:
            sets.append(sorted(1 if v == 14 else v for v in vals))
        span = min(s[-1] - s[0] for s in sets)
        if span == 3:
            labels.append("Four-card rundown")
        elif span == 4:
            labels.append("One total gap")
        elif span == 5:
            labels.append("Two total gaps")
        elif span >= 8 and all(
            sum(b - a == 1 for a, b in pairwise(s)) <= 1 for s in sets
        ):
            labels.append("Disconnected")
    return labels


def sizing(fraction):
    return "No bet" if fraction is None else MATCH_LABELS[bet_size_key(fraction)]


def settlements(h):
    """Require conservation after attributable returns and declared fees."""
    spent = h.spent.copy()
    awards = h.awards
    if not awards or not set(awards) <= set(spent):
        return {}
    if any(value > 0 and name not in h.survivors for name, value in awards.items()):
        return {}
    if h.room == "CoinPoker" and h.reported_pot is None:
        return {}
    if h.room == "iPoker":
        if h.reported_bets != spent:
            return {}
        ordered = sorted(h.final_commits.items(), key=lambda x: -x[1])
        if len(ordered) >= 1:
            n, v = ordered[0]
            second = ordered[1][1] if len(ordered) > 1 else 0
            if v > second:
                if n not in h.survivors:
                    return {}
                spent[n] -= v - second
    gross = sum(spent.values())
    if h.reported_pot is not None and gross != h.reported_pot:
        return {}
    if sum(awards.values()) + h.fees != gross:
        return {}
    return {n: (awards.get(n, 0) - v) / h.bb for n, v in spent.items()}


def observations(h, decisions, events):
    """One mask per observed action or dealt seat; outcomes never enter metric selection."""
    from .engine import band

    by_event = defaultdict(list)
    for d in decisions:
        by_event[(d.event.street, d.event.idx)].append(d.node)
    net = settlements(h)
    pre = events["Preflop"]
    for seat, name in h.seats.items():
        actions = [e.action for e in pre if e.player == name]
        saw = "Flop" in h.boards and "fold" not in actions
        show = (
            saw
            and len(h.survivors) >= 2
            and name in h.survivors
            and "River" in h.boards
        )
        flags = {
            "VPIP": any(a in {"raise", "call"} for a in actions),
            "PFR": "raise" in actions,
            "NET": name in net,
        }
        if saw:
            flags["SHOW"] = show
        if show and name in net:
            flags["WONSHOW"] = h.awards.get(name, 0) > 0
        yield (
            name,
            f"Seat {seat}",
            "",
            band(h.stacks[name] / h.bb, (40, 80, 150)),
            None,
            None,
            sum(BITS[k] for k in flags),
            sum(BITS[k] for k, v in flags.items() if v),
        )
    history = []
    for street in STREETS:
        seq = events[street]
        bet_by = None
        aggression = []
        acted = set()
        for j, e in enumerate(seq):
            flags = {k: e.action == METRICS[k].success for k in by_event[street, e.idx]}
            opponents = [n for n in e.live if n != e.player]
            opp = opponents[0] if len(opponents) == 1 else ""
            chosen = e.amount / e.pot if e.action == "bet" and e.pot else None
            faced = (
                bet_by.amount / bet_by.pot if e.owed and bet_by and bet_by.pot else None
            )
            if street == "Preflop":
                if aggression:
                    last = aggression[-1]
                    opp = last.player
                    after = seq[last.idx : j]
                    intervening = any(v.action == "call" for v in after)
                    clean = not any(
                        v.action == "call" for v in seq[: aggression[0].idx - 1]
                    )
                    full = all(not a.allin for a in aggression)
                    if (
                        clean
                        and full
                        and len(aggression) == 1
                        and e.player not in acted
                        and e.owed
                    ):
                        if intervening and e.can_raise:
                            flags["SQUEEZE"] = e.action == "raise"
                        if not intervening:
                            blind = h.pos[e.player] in {"SB", "BB", "BTN/SB"}
                            if blind:
                                flags["DEFEND"] = e.action in {"call", "raise"}
                            else:
                                flags["COLD"] = e.action == "call"
                    if (
                        clean
                        and full
                        and len(aggression) == 2
                        and e.player == aggression[0].player
                        and not intervening
                        and e.owed
                    ):
                        if e.can_raise:
                            flags["FOURBET"] = e.action == "raise"
                        flags.update(FOLD3=e.action == "fold", CALL3=e.action == "call")
                if e.action == "raise":
                    aggression.append(e)
                acted.add(e.player)
            elif len(e.live) == 2 and len(seq[0].live) == 2:
                k = street.upper()
                flags[k + "_AGG"] = e.action in {"bet", "raise"}
                if not e.owed and e.action in {"check", "bet"}:
                    flags[k + "_BET"] = e.action == "bet"
                if chosen is not None:
                    flags[k + "_SIZE"] = True
                if (
                    j == 2
                    and [a.action for a in seq[:2]] == ["check", "bet"]
                    and e.can_raise
                    and street != "River"
                ):
                    flags[k + "_XR"] = e.action == "raise"
                firstbet = bet_by is not None and bet_by.action == "bet" and e.owed > 0
                if street == "Flop" and h.last_preflop_raiser in e.live:
                    if (
                        e.player == h.last_preflop_raiser
                        and e.action in {"check", "bet"}
                        and all(a.action == "check" for a in seq[:j])
                    ):
                        flags["FCB"] = e.action == "bet"
                    if (
                        firstbet
                        and bet_by.player == h.last_preflop_raiser
                        and all(a.action == "check" for a in seq[: bet_by.idx - 1])
                    ):
                        flags["FFC"] = e.action == "fold"
                if street == "Turn":
                    if firstbet:
                        flags["TURNF"] = e.action == "fold"
                    if (
                        history == ["check", "check"]
                        and e.player == h.last_preflop_raiser
                        and e.action in {"check", "bet"}
                        and all(a.action == "check" for a in seq[:j])
                    ):
                        flags["DELAY"] = e.action == "bet"
                if street == "River" and firstbet:
                    flags.update(RIVERC=e.action == "call", RIVERF=e.action == "fold")
            if flags:
                relevant = (
                    e.remaining[opp]
                    if opp
                    else max((e.remaining[n] for n in opponents), default=0)
                )
                depth = band(min(e.remaining[e.player], relevant) / h.bb, (40, 80, 150))
                yield (
                    e.player,
                    e.ref,
                    opp,
                    depth,
                    chosen,
                    faced,
                    sum(BITS[k] for k in flags),
                    sum(BITS[k] for k, v in flags.items() if v),
                )
            if e.action in {"raise", "bet"}:
                bet_by = e
        history = [e.action for e in seq]
