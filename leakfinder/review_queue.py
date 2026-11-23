"""Select actual Hero decisions for a short, source-linked post-session review."""

import json
import sqlite3
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, replace

from .analysis import analyze
from .engine import METRICS, replay
from .importers import STREETS, Record, decode
from .opponent_history import (
    OpponentHistory,
    apply_history,
    observations,
    river_opponent,
    size_band,
)
from .replay_view import hero_decisions
from .statistics import BITS, DEFS
from .storage import cohort
from .strategy_guide import LABELS, advice_at

REVIEW_PAGE_SIZE = 12
GUIDE_CACHE_VERSION = 1  # Bump when the indexed guide or pattern definitions change.


@dataclass(frozen=True)
class ReviewItem:
    hand: int
    room: str
    number: str
    hero: str
    node: str
    street: str
    ref: str
    action: str
    title: str
    reason: str
    evidence: str
    status: str
    cards: tuple = ()
    position: str = ""
    played: str = ""
    suggested: str = ""
    explanation: str = ""
    difference: str = ""
    priority: int = 0
    close: bool = False
    pattern: str = ""
    repeat_count: int = 0
    review_reason: str = ""


@dataclass(frozen=True)
class ReviewQueue:
    scope: str
    hand_count: int
    library_hands: int
    items: tuple
    source_count: int
    guide_items: tuple = ()
    guide_choices: int = 0


@dataclass(frozen=True)
class GuideIndex:
    counts: dict
    items: tuple


def guide_revision(comparison_key):
    return json.dumps((GUIDE_CACHE_VERSION, comparison_key), separators=(",", ":"))


def cached_guides(db, room, revision):
    row = db.execute(
        "SELECT payload FROM review_guide_cache WHERE room=? AND revision=?",
        (room, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        counts, entries = json.loads(zlib.decompress(row[0]))
        return GuideIndex(
            {
                street: tuple(tuple(row) for row in rows)
                for street, rows in counts.items()
            },
            tuple(
                ReviewItem(**(item | {"cards": tuple(item["cards"])}))
                for item in entries
            ),
        )
    except (TypeError, ValueError, IndexError, KeyError, zlib.error):
        return None


def save_guides(db, room, revision, index):
    payload = zlib.compress(
        json.dumps((index.counts, [asdict(item) for item in index.items])).encode(), 1
    )
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        db.execute("PRAGMA busy_timeout=100")
        with db:
            db.execute(
                """INSERT INTO review_guide_cache VALUES(?,?,?)
                   ON CONFLICT(room) DO UPDATE SET revision=excluded.revision,
                                                   payload=excluded.payload""",
                (room, revision, payload),
            )
    except sqlite3.Error:
        # The guide remains available in memory if another window is writing.
        return
    finally:
        db.execute(f"PRAGMA busy_timeout={timeout}")


def _pattern(snap, event, advice, position):
    active = [seat for seat in snap.seats if seat.live]
    effective = (
        min(
            next(seat.stack + seat.committed for seat in active if seat.is_hero),
            max((s.stack + s.committed for s in active if not s.is_hero), default=0),
        )
        / snap.bb
    )
    texture = (
        len({c[0] for c in snap.board}) < len(snap.board),
        max((sum(c[1] == s for c in snap.board) for s in "cdhs"), default=0),
    )
    return json.dumps(
        (
            snap.room,
            event.street,
            snap.currency,
            snap.bb,
            len(snap.seats),
            len(active),
            position,
            advice.title,
            event.action,
            advice.choices,
            texture,
            sum(effective >= bound for bound in (40, 80, 150)),
            size_band(snap.to_call, snap.pot) if snap.to_call else -1,
        )
    )


def index_guides(db, room, progress=None):
    """Replay each card-known Hero hand once across all streets."""
    items = []
    covered = {street: [] for street in STREETS}
    history = None
    rows = db.execute("""SELECT q.hand,q.hero FROM cohort q
        JOIN hands h ON h.id=q.hand
        WHERE EXISTS (SELECT 1 FROM json_each(h.cards) c WHERE c.key=q.hero)
        ORDER BY q.hand DESC""")
    for index, (hid, hero) in enumerate(rows, 1):
        counts = Counter()
        for event, snap in hero_decisions(db, hid, hero, "All streets"):
            advice = advice_at(snap)
            if advice and river_opponent(snap):
                if history is None:
                    # prepare_hands already selected this room's cohort. Reusing
                    # it avoids SQLite schema writes while the hand cursor reads.
                    history = OpponentHistory(observations(db, room, prepared=True))
                advice = apply_history(snap, advice, history)
            if advice is None or not advice.choices:
                continue
            counts[event.street] += 1
            difference = advice.difference(event.action, event.amount)
            if not difference:
                continue
            position = next(s.position for s in snap.seats if s.is_hero)
            played = LABELS[event.action]
            if event.action == "raise":
                played = advice.raise_label
            if event.amount and event.action in {"bet", "call", "raise"}:
                pot_share = (
                    f" · {event.amount / snap.pot:.0%} pot"
                    if event.action == "bet"
                    else ""
                )
                played += f" ({event.amount / snap.bb:g} BB{pot_share})"
            title = advice.title
            if difference == "size":
                title += " · bet size"
            close = len(advice.choices) > 1
            items.append(
                ReviewItem(
                    hid,
                    snap.room,
                    snap.number,
                    hero,
                    "GUIDE",
                    event.street,
                    event.ref,
                    event.action,
                    title,
                    f"Played {played} · Suggested {advice.play}. {advice.why}",
                    "Study recommendation",
                    "Guide",
                    snap.hero_cards,
                    position,
                    played,
                    advice.play,
                    advice.why,
                    difference,
                    advice.priority,
                    close,
                    _pattern(snap, event, advice, position)
                    if difference == "action" and not close
                    else "",
                )
            )
        for street, count in counts.items():
            covered[street].append((hid, count))
        if progress and index % 500 == 0:
            progress(index)
    return GuideIndex(
        {street: tuple(rows) for street, rows in covered.items()}, tuple(items)
    )


def rank_guides(index, street, hands=None):
    """Keep one representative per repeated pattern ahead of follow-up examples."""
    wanted = STREETS if street == "All streets" else (street,)
    covered = sum(
        count
        for label in wanted
        for hand, count in index.counts[label]
        if hands is None or hand in hands
    )
    items = [
        i
        for i in index.items
        if i.street in wanted and (hands is None or i.hand in hands)
    ]
    patterns = {}
    for i in items:
        if i.pattern:
            patterns.setdefault(i.pattern, set()).add(i.hand)
    representatives, followups, seen = [], [], set()
    for i in sorted(items, key=lambda entry: -entry.priority):
        repeats = len(patterns.get(i.pattern, ()))
        if i.close:
            tier = 3
            why = "Close decision · The guide offers more than one action."
        elif i.difference == "size":
            tier = 2
            why = "Bet size difference · At least 25 percentage points of the pot."
        elif repeats > 1:
            tier = 0
            why = f"The same action difference appears in {repeats:,} comparable hands."
        else:
            tier = 1
            why = "Action difference · The guide favors one action here."
        item = replace(i, repeat_count=repeats, review_reason=why)
        entry = (tier, -repeats, -item.priority, item)
        if tier == 0 and item.pattern in seen:
            followups.append(entry)
        else:
            representatives.append(entry)
            if tier == 0:
                seen.add(item.pattern)
    return tuple(
        entry[3]
        for group in (representatives, followups)
        for entry in sorted(group, key=lambda item: item[:3])
    ), covered


def has_saved_batch(db):
    return bool(db.execute("SELECT 1 FROM review_batch LIMIT 1").fetchone())


def remember_batch(db, source_ids):
    """Remember successful files from one chooser operation, including repeat imports."""
    ids = sorted(set(source_ids))
    with db:
        sources = db.execute(
            "SELECT id FROM imports WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
            (json.dumps(ids),),
        ).fetchall()
        if sources:
            db.execute("DELETE FROM review_batch")
            db.executemany("INSERT INTO review_batch(source) VALUES(?)", sources)
    return len(sources)


# First listed descriptive situation wins if several metrics describe one action.
# Hand results, showdown and the unresolved fold-to-c-bet denominator are ineligible.
SPOTS = {
    "FOLD3": (
        "Facing a three-bet after opening",
        "Which hands would you continue with against this three-bet?",
    ),
    "SQUEEZE": (
        "Facing an open and a caller",
        "What changes when there is already a caller behind the opener?",
    ),
    "RIVERC": (
        "Facing a river bet",
        "Which plausible bluffs and value hands reach this bet size?",
    ),
    "FLOP_XR": (
        "Facing a bet after checking the flop",
        "Which hands would you check-raise against this sizing?",
    ),
    "TURN_XR": (
        "Facing a bet after checking the turn",
        "Which hands would you check-raise on this turn?",
    ),
    "DELAY": (
        "Delayed turn continuation bet",
        "What changed on the turn after the flop checked through?",
    ),
    "TURNF": ("Facing a turn bet", "Which hands can continue against this turn size?"),
    "FCB": (
        "Flop continuation-bet decision",
        "Which worse hands could call and which better hands could fold?",
    ),
    "DEFEND": (
        "Blind defense against an open",
        "How do the opening position and price affect your defense?",
    ),
    "COLD": (
        "Cold-call opportunity against an open",
        "How do position and the players behind affect a call?",
    ),
    "FLOP_AGG": (
        "Flop decision",
        "Which hands could continue against aggression on this flop?",
    ),
    "TURN_AGG": ("Turn decision", "How did the turn card change your available lines?"),
    "RIVER_AGG": (
        "River decision",
        "Which value hands and bluffs can reach this river?",
    ),
}

MATCHED_SPOTS = {
    "THREEBET": (
        "Facing a single open",
        "What would a three-bet accomplish against this opening position?",
    ),
    "BET_IP_VS_MISSED_FLOP_CB": (
        "The raiser checked the flop to you",
        "Which hands could a bet target after the raiser's check?",
    ),
    "OOP_RESPONSE_VS_IP_FLOP_STAB_AFTER_MISSED_CB": (
        "Facing a flop stab after checking",
        "What can the bettor represent after your missed continuation bet?",
    ),
    "BET_IP_VS_MISSED_TURN_CB": (
        "The raiser checked the turn to you",
        "What changed after the raiser bet the flop and checked the turn?",
    ),
    "OOP_RESPONSE_VS_IP_TURN_STAB_AFTER_MISSED_CB": (
        "Facing a turn stab after checking",
        "Which hands can continue after your turn check gets bet?",
    ),
    "TURN_PROBE_AFTER_FLOP_CHECKBACK": (
        "First to act after a flop check-back",
        "What hands could you target on this turn?",
    ),
    "FOLD_TO_TURN_PROBE": (
        "Facing a turn probe",
        "What can the bettor represent after you checked back the flop?",
    ),
    "PFA_TURN_BARREL_AFTER_FLOP_CBET_CALLED": (
        "Turn decision after your flop bet was called",
        "Which hands would call another bet on this turn?",
    ),
    "FOLD_TO_TURN_BARREL_AFTER_FLOP_CALL": (
        "Facing a second barrel",
        "Which hands still belong in your continuing range on this turn?",
    ),
    "FOLD_TO_RIVER_PROBE": (
        "Facing a river probe",
        "Which value hands and bluffs reach this river after your turn check-back?",
    ),
    "RIVER_BARREL": (
        "River decision after two called barrels",
        "What can call or fold to a third barrel?",
    ),
    "FOLD_TO_RIVER_BARREL_AFTER_FLOP_TURN_CALL": (
        "Facing a third barrel",
        "Which value hands and bluffs plausibly bet all three streets?",
    ),
    "RIVER_PROBE": (
        "River decision after a turn check-back",
        "Which hands might call a river lead after checking back the turn?",
    ),
    "OPEN": (
        "First-in preflop decision",
        "How do your position and the players behind affect your opening range?",
    ),
}


def prepare_hands(db, room, scope):
    cohort(db, room)
    library = db.execute("SELECT count(*) FROM cohort").fetchone()[0]
    source_count = db.execute("SELECT count(*) FROM review_batch").fetchone()[0]
    latest = scope == "Latest import" and source_count > 0
    db.execute("DROP TABLE IF EXISTS temp.review_hands")
    db.execute("CREATE TEMP TABLE review_hands(hand INTEGER PRIMARY KEY, hero TEXT)")
    if latest:
        db.execute("""INSERT OR IGNORE INTO review_hands
            SELECT c.hand,c.hero FROM review_batch b
            CROSS JOIN origins o ON o.source=b.source
            JOIN cohort c ON c.hand=o.hand""")
        selected = db.execute("SELECT count(*) FROM review_hands").fetchone()[0]
    else:
        db.execute("INSERT INTO review_hands SELECT * FROM cohort")
        selected = library
    return ("Latest import" if latest else "All hands", selected, library, source_count)


def candidates(db, findings, street, limit):
    """Keep a bounded number of distinct hands for each node and evidence level."""
    nodes = [k for k, m in METRICS.items() if street in ("All streets", m.street)]
    db.execute("DROP TABLE IF EXISTS temp.review_contexts")
    db.execute("""CREATE TEMP TABLE review_contexts(node,cell,aligned,
                  PRIMARY KEY(node,cell)) WITHOUT ROWID""")
    contexts = []
    for node in nodes:
        f = findings.get(node)
        if f is None:
            continue
        for key in f.selected:
            c = f.cells[key]
            # A supported frequency is an aggregate statement. Only feature
            # actions that also point in that direction in this exact context.
            aligned = -1
            if (
                METRICS[node].confirmatory
                and f.status == "Supported"
                and (c.hx / c.h - c.px / c.p) * f.gap > 0
            ):
                aligned = int(f.gap > 0)
            contexts.append((node, key, aligned))
    db.executemany("INSERT INTO review_contexts VALUES(?,?,?)", contexts)
    matched = """SELECT d.hand,d.node,d.ref,d.cell,
               CASE WHEN x.aligned=d.success THEN 0 WHEN x.node IS NOT NULL THEN 1 ELSE 2 END AS tier
               FROM review_hands q CROSS JOIN decisions d ON d.hand=q.hand AND d.actor=q.hero
               LEFT JOIN review_contexts x ON x.node=d.node AND x.cell=d.cell
               WHERE d.node IN (SELECT value FROM json_each(?))"""

    descriptive = [k for k in SPOTS if street in ("All streets", DEFS[k].street)]
    # Keys and integer bits are internal constants. Each real source action
    # receives its most specific eligible descriptive name.
    cases = " ".join(f"WHEN o.eligible & {BITS[k]} THEN '{k}'" for k in descriptive)
    examples = f"""SELECT o.hand,CASE {cases} END AS node,o.ref,'' AS cell,2 AS tier
               FROM review_hands q CROSS JOIN obs o ON o.hand=q.hand AND o.actor=q.hero
               WHERE o.eligible & ? AND NOT EXISTS (
                   SELECT 1 FROM decisions d WHERE d.hand=o.hand AND d.ref=o.ref AND d.actor=o.actor)"""
    rows = db.execute(
        f"""WITH choices AS ({matched} UNION ALL {examples}),
               per_hand AS (
               SELECT *,row_number() OVER (PARTITION BY tier,node,hand
                   ORDER BY CAST(substr(ref,instr(ref,' ')+1) AS INTEGER),ref) AS hand_rank FROM choices),
               per_node AS (
               SELECT *,row_number() OVER (PARTITION BY tier,node ORDER BY hand DESC) AS node_rank
               FROM per_hand WHERE hand_rank=1)
               SELECT hand,node,ref,cell,tier,node_rank FROM per_node WHERE node_rank<=?""",
        (json.dumps(nodes), sum(BITS[k] for k in descriptive), limit),
    ).fetchall()
    # Select a variety of streets and decisions among the bounded examples.
    # A single supported node may fill the queue if it has enough examples.
    order = list(MATCHED_SPOTS)[:-1] + list(SPOTS) + ["OPEN"]
    used_nodes = set()
    used_streets = set()
    for _ in range(limit):
        if not rows:
            break
        chosen = min(
            rows,
            key=lambda r: (
                r["tier"] != 0,
                r["node"] in used_nodes,
                DEFS[r["node"]].street in used_streets,
                r["tier"],
                order.index(r["node"]),
                r["node_rank"],
                -r["hand"],
            ),
        )
        yield chosen
        used_nodes.add(chosen["node"])
        used_streets.add(DEFS[chosen["node"]].street)
        rows = [r for r in rows if r["hand"] != chosen["hand"]]


def explain(row, finding):
    if not finding:
        return "One observed hand for review. It has no matched opponent comparison."
    cell = finding.cells[row["cell"]]
    verb = VERBS[METRICS[finding.node].success]
    if row["cell"] not in finding.selected:
        note = (
            "No matching opponent decisions are available."
            if not cell.p
            else (
                f"Only {cell.p:,} opponent {'decision matches' if cell.p == 1 else 'decisions match'} this exact spot. "
                "It falls outside the matched comparison."
            )
        )
        return f"In this library spot, you {verb} on {cell.hx:,} of {cell.h:,} {'chance' if cell.h == 1 else 'chances'}. {note}"
    x, n, _, p = finding.counts
    if n < 10 or p < 20:
        total_x = sum(c.hx for c in finding.cells.values())
        return (
            f"In your library, you {verb} on {total_x:,} of {finding.hero:,} {'chance' if finding.hero == 1 else 'chances'}. "
            f"The comparison covers {n:,} of yours and {p:,} opponent decision{'s' if p != 1 else ''}. "
            "The sample is too small for a clear difference."
        )
    lo, hi = finding.adjusted if row["tier"] == 0 else finding.interval
    label = "Adjusted" if row["tier"] == 0 else "95%"
    return (
        f"In your library, you {verb} on {x:,} of {n:,} matched chances. "
        f"Matched opponents averaged {100 * finding.expected:.0f}% across {p:,} chances. "
        f"{label} range for the gap is {100 * lo:+.1f} to {100 * hi:+.1f} points."
    )


VERBS = {
    "raise": "raised",
    "bet": "bet",
    "call": "called",
    "fold": "folded",
    "check": "checked",
}


def build_queue(
    db,
    room="Both",
    *,
    findings=None,
    limit=REVIEW_PAGE_SIZE,
    street="All streets",
    scope="Latest import",
    guide_index=None,
):
    """Use current library evidence; restrict displayed examples to the selected source files."""
    db.execute("SAVEPOINT read_home_queue")
    try:
        return _read_queue(db, room, findings, limit, street, scope, guide_index)
    except BaseException:
        db.execute("ROLLBACK TO read_home_queue")
        raise
    finally:
        db.execute("RELEASE read_home_queue")


def _read_queue(db, room, findings, limit, street, scope, guide_index):
    if street not in ("All streets", *STREETS) or scope not in (
        "All hands",
        "Latest import",
    ):
        raise ValueError("Unknown review filter.")
    if limit < 0:
        raise ValueError("The review limit must be nonnegative.")
    actual_scope, count, library, source_count = prepare_hands(db, room, scope)
    if not count or not limit:
        return ReviewQueue(actual_scope, count, library, (), source_count)
    if findings is None:
        findings = analyze(db, room)[0]
    if not isinstance(findings, dict):
        findings = {f.node: f for f in findings}
    selected = (
        {row[0] for row in db.execute("SELECT hand FROM review_hands")}
        if room != "Both" or actual_scope == "Latest import" and count != library
        else None
    )
    if guide_index is None:
        guide_index = index_guides(db, room)
    guide, covered = rank_guides(guide_index, street, selected)
    items = []
    used = set()
    for item in guide:
        if item.hand not in used:
            items.append(item)
            used.add(item.hand)
        if len(items) == limit:
            break
    fallback = candidates(db, findings, street, limit) if len(items) < limit else ()
    for row in fallback:
        if len(items) == limit:
            break
        hid = row["hand"]
        if hid in used:
            continue
        origin = db.execute(
            """SELECT h.room,h.number,q.hero,o.locator,o.raw FROM hands h
            JOIN review_hands q ON q.hand=h.id JOIN origins o ON o.hand=h.id
            WHERE h.id=? ORDER BY o.rowid LIMIT 1""",
            (hid,),
        ).fetchone()
        h = decode(
            Record(
                origin["room"],
                zlib.decompress(origin["raw"]).decode(),
                origin["locator"],
            )
        )
        _, events = replay(h)
        node = row["node"]
        st = DEFS[node].street
        hero = origin["hero"]
        e = next(e for e in events[st] if e.ref == row["ref"] and e.player == hero)
        verb = VERBS[e.action]
        if e.action == "bet":
            verb += f" {e.amount / h.bb:g} BB ({100 * e.amount / e.pot:.0f}% pot)"
        spot, prompt = (MATCHED_SPOTS if node in MATCHED_SPOTS else SPOTS)[node]
        opponent_pos = json.loads(row["cell"])[6] if row["cell"] else ""
        against = f" vs {opponent_pos}" if opponent_pos else ""
        board = h.boards.get(st, ())
        street_cards = f" on {' '.join(board)}" if board else ""
        price = (
            f" facing {min(e.owed, e.remaining[hero]) / h.bb:g} BB" if e.owed else ""
        )
        reason = f"You {verb} from {h.pos[hero]}{against}{street_cards}{price} with {e.pot / h.bb:g} BB in the pot. {prompt}"
        items.append(
            ReviewItem(
                hid,
                h.room,
                h.number,
                hero,
                node,
                st,
                e.ref,
                e.action,
                spot,
                reason,
                explain(row, findings.get(node)),
                "Supported" if row["tier"] == 0 else "Exploratory",
            )
        )
    return ReviewQueue(
        actual_scope, count, library, tuple(items), source_count, guide, covered
    )
