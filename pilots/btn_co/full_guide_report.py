"""Generate the offline guide from public HeroDemo and the production advice engine."""

from contextlib import closing
from html import escape
from pathlib import Path

from leakfinder.demo import load_demo
from leakfinder.replay_view import _source_records, hero_decisions
from leakfinder.review_queue import build_queue
from leakfinder.storage import connect, initialize
from leakfinder.strategy_guide import LABELS, advice_at

from .render import _cards, _source

MATRIX = (
    (
        "UTG and other early positions",
        "Open AA and strong hands with useful suits and connectivity.",
        "When opening first in, consider who can still 3-bet.",
    ),
    (
        "HJ",
        "Open strong groups. Save weaker connected hands for later positions.",
        "Calling an UTG open requires a different range from defending against a late open.",
    ),
    (
        "CO and BTN",
        "Open supported KK, suited connected hands, and other playable groups.",
        "Position gives you more room to call. Some strong groups can 3-bet against a late open.",
    ),
    (
        "SB and heads-up BTN/SB",
        "Open strong or well-connected hands. Consider completing with marginal hands.",
        "When facing an open, account for playing out of position postflop at tables with three or more seats.",
    ),
    (
        "BB",
        "After limps, the BB can see a free flop. Strong groups can raise for value.",
        "Consider the call price and opener position. A small open gives you more room to call.",
    ),
)

SITUATIONS = (
    (
        "Limpers or an open with a caller",
        "The guide identifies clear value raises and folds. In close spots, weigh the number of players, the price, and who can still raise.",
    ),
    (
        "Facing a 3-bet or another raise",
        "With AA, depth and side cards guide the choice between calling and reraising. Weakly connected hands tend to fold against a high price. Consider cold 3-bets, squeezes, and 4-bets separately.",
    ),
    (
        "Flop",
        "Compare value betting and checking, including c-bet opportunities and action after the raiser checks. Consider made hands and draws. Top set can call or raise a bet on boards that allow neither a flush nor a straight.",
    ),
    (
        "Turn",
        "Reassess the made hand, possible flushes and straights, board pairing, and call price after the turn. For close draws, check which outs are clean and how much value a hit can earn.",
    ),
    (
        "River",
        "Look for value with the nuts. With a weak made hand facing pressure, consider the size and the players remaining. For a close bluff catch, look at the equity threshold and plausible bluffs from earlier action.",
    ),
)

# Outcome notes are appended after advice_at has processed the prefix.
AUDITED_RIVER = (
    (
        "CoinPoker",
        "9000002844",
        "Line 32",
        "Both players checked the flop and turn. BB has a flush with Q♦ and 3♦ and needs 25% equity to call. A weaker J♦T♦ flush can bet small. A♦T♦ and K♦J♦ beat Hero. A bare A♦ can bluff when the other three cards are not diamonds. Calling is reasonable at this price.",
    ),
    (
        "CoinPoker",
        "9000001429",
        "Line 40",
        "SB acts first on the river with JJJ22 after CO called the turn. Smaller full houses such as 33 and strong flushes such as A♣Q♣ are value targets. KK and 22 beat Hero. A larger bet makes sense if flushes or smaller full houses call often enough. Hero bet around 75% pot.",
    ),
    (
        "CoinPoker",
        "9000004214",
        "Line 39",
        "CO called the flop and checked the turn and river. Hero has a seven-high straight. A4 makes a weaker straight that can call. 84 and 98 make stronger straights. A medium or small value bet depends on how many weaker straights reach the river.",
    ),
    (
        "CoinPoker",
        "9000002860",
        "Line 23",
        "BB called the turn and checked the river. J♠8♠ makes a flush and a weaker 9♠7♠ can call a small bet. QT can make a full house, and A♠K♠ makes a higher flush. Checking is a good starting point when weaker hands rarely call.",
    ),
    (
        "CoinPoker",
        "9000000024",
        "Line 43",
        "Three players checked both streets. CO bets half pot, with HJ still to act behind Hero. Hero has aces and fives and only one club. K♣ blocks the king-high flush on an A♣ board. A full house such as A5 and stronger made hands can value bet.",
    ),
    (
        "iPoker",
        "9000005984",
        "Action 16",
        "SB checked and can act after BTN. BB bets half pot after two checked streets. Hero has one pair. KQ can make a king-high straight, and A♠K♠ can represent a missed draw. The additional player favors folding.",
    ),
    (
        "iPoker",
        "9000010026",
        "Action 17",
        "SB bet 66% pot and BB raised all-in. Hero's nine-high straight faces a 15 BB call into 35.88 BB, a price of 29.5%. QT and T7 can make stronger straights. A♣K♣ can represent a missed turn draw. The call needs enough shoves from weaker hands or ties.",
    ),
    (
        "iPoker",
        "9000008038",
        "Action 16",
        "Three players checked the flop and turn. Hero has tens and sevens on a paired river. J9 makes a straight and T7 a full house. Hero beats missed hearts such as K♥Q♥. A weaker call and folds from stronger hands are both difficult to find, so check.",
    ),
    (
        "iPoker",
        "9000009995",
        "Action 13",
        "SB bet the turn and checked the river. Hero already beats missed hearts such as A♥T♥, and Hero's two hearts block some missed draws. A bluff would need to target better hands such as a weaker 7x. The line favors checking.",
    ),
    (
        "iPoker",
        "9000014007",
        "Action 16",
        "SB called a flop raise, checked the turn, and bet one-third pot on the river. Hero's three pocket sevens make a set here. A K♦J♦ flush or a KJ Broadway straight can bet for value. A missed draw is possible. Without a flush blocker, there is a case for folding even at this price.",
    ),
)

AUDITED = (
    (
        "CoinPoker",
        "9000000030",
        "Flop",
        "SB raised preflop and checked to BB. Hero has a pair of twos and no next card immediately makes a straight or flush. Checking preserves showdown value. A missed c-bet alone offers little reason to stab.",
    ),
    (
        "CoinPoker",
        "9000000030",
        "Turn",
        "SB checked again. The J♣ gives Hero eight of 44 unseen cards that make a river straight. Six make the verified current nuts or a tie. A bet also needs to account for which hands SB actually folds.",
    ),
    (
        "CoinPoker",
        "9000000021",
        "Turn",
        "The flop checked through. SB holds one spade on a three-spade turn and needs two to make a flush. Probing depends on whether the opponent can fold enough better hands.",
    ),
    (
        "iPoker",
        "9000003988",
        "Turn",
        "The flop checked through and a third club arrived on the turn. Hero has an overpair without a club. Checking avoids building a pot against made flushes and strong hands.",
    ),
    (
        "iPoker",
        "9000012001",
        "Turn",
        "Hero has trip eights and faces a reraise at about a 33% price. Another card can make a full house or quads, and a full house can still lose. Check which stronger hands already raise and how much an improvement can earn.",
    ),
    (
        "iPoker",
        "9000000068",
        "Flop",
        "Fifteen cards immediately make a straight or flush. Two make both, and seven make the verified current nuts or a tie. Around half pot is a possible semibluff. Checking is reasonable if the line gets too many calls or raises.",
    ),
    (
        "CoinPoker",
        "9000005623",
        "Flop",
        "Both blinds called a BTN open to 3.4 BB and checked. QQ can call a bet, and rare Qx and high pairs may continue against a small size. Checking is a reasonable starting point. Hero checked, then a small turn bet got one call.",
    ),
    (
        "CoinPoker",
        "9000005623",
        "Turn",
        "The 5♦ makes 55 a possible full house and gives some weaker pairs a new diamond draw. After three flop checks and two turn checks, the played small bet is reasonable. SB called and BB folded.",
    ),
    (
        "CoinPoker",
        "9000005623",
        "River",
        "Around 35% pot is a reasonable size after a call on the small turn bet. A larger bet needs enough full-house calls. SB folded to the played 75% bet, with cards hidden.",
    ),
    (
        "CoinPoker",
        "9000002855",
        "Flop",
        "SB 3-bet preflop and BTN called. A small bet on the dry K♣ 2♠ 4♦ can keep weaker Kx in the hand. BTN actually raised the 33% pot bet with K♥ J♥ T♦ 9♣. This shows one response to the small size.",
    ),
    (
        "CoinPoker",
        "9000005632",
        "Turn",
        "SB called a pot-sized bet on Q♠ 9♦ T♦. The J♠ allows more straights. Checking keeps the nut diamond draw in play. The pot-sized flop bet warrants separate review. Both players checked the turn.",
    ),
    (
        "iPoker",
        "9000005973",
        "Turn",
        "BB called 66% pot on the flop. The 7♥ leaves Hero with a pair of tens and the nut flush draw. Check behind by default, or use around one-third pot as a semibluff. BB folded to the played pot-sized bet.",
    ),
    (
        "iPoker",
        "9000005983",
        "River",
        "UTG opened preflop and checked back the flop and turn. Hero has the nut straight with Q9. A small bet gives a weaker straight such as 97 a price to call. UTG folded to a pot-sized bet with cards hidden.",
    ),
    (
        "iPoker",
        "9000011994",
        "Flop",
        "SB raised over a limper. Three players checked to BTN. With QQ and a queen-high diamond draw, checking is reasonable. Sets, two pair and higher diamond draws can continue. SB and BB called the played bet.",
    ),
)


def decision_text(event, snap, advice):
    played = advice.raise_label if event.action == "raise" else LABELS[event.action]
    if event.action == "bet":
        played += f" {event.amount / snap.pot:.0%} pot ({event.amount / snap.bb:g} BB)"
    recommendation = advice.play or "manual review"
    return f"Played {played} · Suggested {recommendation}. {advice.why}"


def render_example(snap, title, reason, record, note=None):
    board = f"<p>Board <b>{escape(' '.join(snap.board))}</b></p>" if snap.board else ""
    audit = f"<p>{escape(note)}</p>" if note else ""
    return (
        f'<article id="hand-{escape(snap.number)}-{snap.street.lower()}"><h3>{escape(snap.room)} #{escape(snap.number)} · {escape(title)}</h3>'
        f"{_cards(snap.hero_cards)}{board}<p>{escape(reason)}</p>{audit}"
        f"{_source(record.raw, record.locator, snap.ref)}</article>"
    )


def render():
    with closing(connect(":memory:")) as db:
        initialize(db)
        load_demo(db)
        queue = build_queue(db, scope="All hands", findings={})
        rivers = []
        for room, number, ref, note in AUDITED_RIVER:
            hid, hero = db.execute(
                "SELECT h.id,q.hero FROM review_hands q JOIN hands h ON h.id=q.hand WHERE h.room=? AND h.number=?",
                (room, number),
            ).fetchone()
            event, snap = next(
                (e, s)
                for e, s in hero_decisions(db, hid, hero, "River")
                if e.ref == ref
            )
            advice = advice_at(snap)
            rivers.append(
                render_example(
                    snap,
                    advice.title,
                    decision_text(event, snap, advice),
                    next(_source_records(db, hid)),
                    note,
                )
            )
        audits = []
        for room, number, street, note in AUDITED:
            hid, hero = db.execute(
                """SELECT h.id,q.hero FROM review_hands q JOIN hands h ON h.id=q.hand WHERE h.room=? AND h.number=?""",
                (room, number),
            ).fetchone()
            preferred_ref = "Action 23" if number == "9000012001" else None
            event, snap = next(
                (e, s)
                for e, s in hero_decisions(db, hid, hero, street)
                if preferred_ref is None or e.ref == preferred_ref
            )
            advice = advice_at(snap)
            record = next(_source_records(db, hid))
            audits.append(
                render_example(
                    snap, advice.title, decision_text(event, snap, advice), record, note
                )
            )
        samples = []
        for room in ("CoinPoker", "iPoker"):
            item = next(
                (
                    i
                    for i in queue.guide_items
                    if i.room == room and i.street == "Preflop"
                ),
                None,
            )
            if item:
                record = next(_source_records(db, item.hand))
                _, snap = next(
                    (e, s)
                    for e, s in hero_decisions(db, item.hand, item.hero, "Preflop")
                    if e.ref == item.ref
                )
                samples.append(render_example(snap, item.title, item.reason, record))
    matrix = "".join(
        f"<tr><th>{escape(pos)}</th><td>{escape(rfi)}</td><td>{escape(vs_open)}</td></tr>"
        for pos, rfi, vs_open in MATRIX
    )
    situations = "".join(
        f"<dt>{escape(title)}</dt><dd>{escape(line)}</dd>" for title, line in SITUATIONS
    )
    examples = "".join(samples)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PLO4 hand guide</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#edf2f6;color:#192f43;font:16px/1.5 'Segoe UI',Arial,sans-serif}}
main{{max-width:1060px;margin:auto;padding:22px 16px}}header{{background:#ffc88e;border-radius:8px;padding:18px 22px}}
h1,h2,h3,p{{margin:4px 0 10px}}h2{{margin-top:22px}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;background:white;text-align:left}}
th,td{{padding:9px 12px;vertical-align:top;border-bottom:1px solid #dae4eb}}thead{{background:#dbe5ed}}th{{min-width:130px}}
dl,article{{background:white;padding:14px 18px;border:1px solid #cbd7e0;border-radius:6px}}dt{{font-weight:700}}dd{{margin:2px 0 12px}}
article{{margin:10px 0}}.cards{{display:inline-flex;gap:4px;margin:3px 0}}.card{{background:#f0f4f6;border:1px solid #b7c7d3;border-radius:3px;padding:1px 5px;font-weight:700}}
.heart,.diamond{{color:#843545}}summary{{cursor:pointer;font-weight:600}}pre{{max-height:300px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}
</style><main><header><h1>PLO4 hand guide</h1><p>Study recommendations for completed CoinPoker and iPoker hands.</p></header>
<p>Start with <b>Import</b>. <b>Hands to review</b> prioritizes decisions where the guide suggests a different action.
<b>Guide differences only</b> lists those spots. Open a hand to replay the exact decision and inspect the source.</p>
<p>The HeroDemo guide makes a recommendation for {queue.guide_choices} decisions and flags {len(queue.guide_items)} differences. A hand can contain multiple decisions.
Two suggested actions indicate a close spot. More complex decisions get a question for manual review.</p>
<p>A sizing difference can qualify for review at 25 percentage points of the pot. The comparison uses amounts remaining opponents can actually match. Suggested sizing depends on plausible calls, prior action, and stacks.</p>
<p><a href="#river">River without the nuts</a> · <a href="#postflop">Other postflop decisions</a></p>
<h2>Preflop by position</h2><div class="scroll"><table>
<thead><tr><th>Position</th><th>First in</th><th>Facing an open and later action</th></tr></thead><tbody>{matrix}</tbody></table></div>
<h2>From limps to the river</h2><dl>{situations}</dl>
<p>The guide requires four known Hero cards. Postflop hand strength uses exactly two hole cards and three board cards.
Stack depth, rake, and opponent ranges remain part of the manual review.</p>
<h2 id="river">Ten river decisions without the nuts</h2>
<p>Five hands come from each network. The app checks blockers, made hands, and missed draws against the river board, then considers the line and sizing. The examples show two cards from a possible PLO4 holding. The other two can change the best made hand and how it fits the line. Betting or bluffing frequencies require separate evidence.</p>
<p>In a heads-up pot, the call price is the amount to call divided by the pot after calling, before rake. If Hero only beats bluffs and ties are impossible, that percentage is the minimum bluff share. Weaker value bets also count toward winning outcomes when Hero beats them. Multiway decisions account for players still to act and possible side pots.</p>
{"".join(rivers)}
<h2 id="postflop">More postflop checks on real hands</h2>
<p>Each study note starts with information available before the decision. The follow-up describes the actual line and known later action. The observed response applies to the played size.</p>
{"".join(audits)}<h2>Preflop examples from HeroDemo</h2>{examples}</main></html>"""


if __name__ == "__main__":
    Path(__file__).with_name("PLO4-guide.html").write_text(render(), encoding="utf-8")
