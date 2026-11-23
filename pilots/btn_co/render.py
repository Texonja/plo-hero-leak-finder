"""Render the BTN versus CO pilot to one self-contained, offline HTML file."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from html import escape
from pathlib import Path
from urllib.parse import quote

POSITIONS = ("CO", "BTN", "SB", "BB")
ACTIONS = {"fold": "Fold", "call": "Call", "raise": "3-bet"}
SUITS = {
    "s": ("♠", "spade"),
    "h": ("♥", "heart"),
    "d": ("♦", "diamond"),
    "c": ("♣", "club"),
}
SUIT_CODES = {symbol: code for code, (symbol, _) in SUITS.items()}
CARD = re.compile(
    r"(10|[2-9TJQKA])([shdc♠♥♦♣])|([shdc♠♥♦♣])(10|[2-9TJQKA])", re.IGNORECASE
)


def _read(value):
    if isinstance(value, Mapping):
        return value
    return json.loads(Path(value).read_text(encoding="utf-8-sig"))


def _text(value):
    return escape(str(value), quote=True)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value, decimals=2, signed=False):
    number = _number(value)
    if number is None:
        return "—"
    number = round(number, decimals) or 0.0
    digits = (
        f"{abs(number):.{decimals}f}".rstrip("0").rstrip(".")
        if decimals
        else f"{abs(number):.0f}"
    )
    sign = "−" if number < 0 else ("+" if signed and number > 0 else "")
    return sign + digits


def _chips(value):
    return f"{_fmt(value, 3)} bb" if _number(value) is not None else "—"


def _count(value):
    n = _number(value)
    return f"{int(n):,}".replace(",", "\u202f") if n is not None else "—"


def _cards(value):
    if not value:
        return '<span class="unknown">Cards unavailable</span>'
    raw = " ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
    parts = []
    for match in CARD.finditer(raw):
        rank = (match[1] or match[4]).upper()
        raw_suit = (match[2] or match[3]).lower()
        suit = SUITS[SUIT_CODES.get(raw_suit, raw_suit)]
        parts.append(
            f'<span class="card {suit[1]}">{rank if rank != "T" else "10"}{suit[0]}</span>'
        )
    if len(parts) != 4:
        return _text(raw)
    return '<span class="cards">' + "".join(parts) + "</span>"


def _seat_values(value):
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (list, tuple)):
        return dict(zip(POSITIONS, value))
    return {}


def _key(record):
    return str(record["room"]), str(record["id"])


def _anchor(key):
    return "hand-" + "-".join(key)


def _source(value, locator, action_ref):
    if not value:
        return '<p class="missing">This report has no source record for the hand.</p>'
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False)
    raw = _text(value).replace("\r", "&#13;")
    point = " · ".join(_text(v) for v in (locator, action_ref) if v)
    return (
        '<details class="source"><summary>Open the complete source hand</summary>'
        + (f"<p><b>Decision reference</b> &nbsp;{point}</p>" if point else "")
        + "<p>The source can include actions after this decision and the outcome.</p>"
        f"<pre>{raw}</pre></details>"
    )


def _estimate(value, se):
    point = _number(value)
    if point is None:
        return '<span class="unknown">—</span>'
    error = _number(se)
    if error is None or error < 0:
        interval = "interval unavailable"
    else:
        radius = 1.96 * error
        interval = f"[{_fmt(point - radius, 3)}, {_fmt(point + radius, 3)}]"
    return f"<strong>{_fmt(point, 3, True)}</strong><small>{interval}</small>"


def _scenarios(items):
    if not items:
        return (
            '<p class="missing">Scenario estimates are unavailable for this hand.</p>'
        )
    rows = []
    for item in items:
        call, raise_ev = _number(item.get("call")), _number(item.get("raise"))
        diff = raise_ev - call if call is not None and raise_ev is not None else None
        rows.append(
            f'<tr><th scope="row">{_text(item.get("name", "—"))}</th>'
            f"<td>{_estimate(call, item.get('call_se'))}</td>"
            f"<td>{_estimate(raise_ev, item.get('raise_se'))}</td>"
            f"<td>{_estimate(diff, item.get('diff_se'))}</td></tr>"
        )
    return (
        '<div class="scroll"><table class="ev"><caption>Modeled decision EV in BB</caption>'
        '<thead><tr><th scope="col">Scenario</th><th scope="col">Call</th>'
        '<th scope="col">3-bet</th><th scope="col">3-bet − call</th></tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table></div><p class="hint">Fold is worth 0 BB from the decision point. '
        "Brackets show an approximate 95% Monte Carlo interval "
        "(estimate ± 1.96 standard errors). Differences use paired standard errors. "
        "The interval covers sampling noise within the scenario. Different opponent "
        "or postflop assumptions can shift EV beyond it.</p>"
    )


def _table(hand):
    stacks = _seat_values(hand.get("stacks"))
    seats = []
    for pos in ("BB", "UTG", "HJ", "SB", "BTN", "CO"):
        hero = pos == "BTN"
        folded = pos in ("UTG", "HJ")
        style = " hero" if hero else (" folded" if folded else "")
        title = f"{pos} · Hero" if hero else pos
        if folded:
            body = "<small>Fold</small>"
        else:
            body = f"<span>{_chips(stacks.get(pos))}</span>"
        if hero:
            body += _cards(hand.get("cards"))
        seats.append(f'<div class="seat pos-{pos}{style}"><b>{title}</b>{body}</div>')
    return (
        '<figure class="felt" aria-label="Table before Hero acts">'
        + "".join(seats)
        + '<div class="pot"><span>Preflop · pot</span><b>'
        + _chips(hand.get("pot"))
        + '</b></div></figure><p class="hint">Stacks are chips behind immediately before BTN acts. '
        "The flop has yet to be dealt.</p>"
    )


def _detail(label, value):
    return f"<div><dt>{label}</dt><dd>{value}</dd></div>"


def _hand(hand, result, anchor, index):
    room, number = _key(hand)
    action = str(hand.get("action") or "")
    detail = ACTIONS.get(action, action or "—")
    if action in ("call", "raise") and _number(hand.get("actual_to")) is not None:
        detail += f" to {_chips(hand['actual_to'])}"
    commitments = _seat_values(hand.get("committed"))
    committed = " · ".join(
        f"{p} {_chips(v)}" for p, v in commitments.items() if p in POSITIONS
    )
    if not committed and _number(hand.get("committed")) is not None:
        committed = _chips(hand["committed"])
    facts = [
        _detail("CO opens to", _chips(hand.get("open_to"))),
        _detail("Modeled 3-bet to", _chips(hand.get("threebet_to"))),
        _detail("Hero action", _text(detail)),
    ]
    if committed:
        facts.append(_detail("Already committed, excluding antes", committed))
    if _number(hand.get("ante_per_seat")) is not None:
        facts.append(_detail("Ante per player", _chips(hand["ante_per_seat"])))
    label = _text(result.get("category") or "No estimate")
    reason = _text(
        result.get("reason") or "Pilot estimates are unavailable for this hand."
    )
    url = _text("#" + quote(anchor))
    return (
        f'<article class="hand" id="{_text(anchor)}" aria-labelledby="heading-{index}">'
        f'<header class="handhead"><div><p>{_text(room)} · PLO4</p>'
        f'<h2 id="heading-{index}">Hand #{_text(number)}</h2></div>'
        f'<a href="{url}" class="permalink" aria-label="Permanent link to this hand"># Link to hand</a></header>'
        f'<div class="handbody">{_table(hand)}<dl class="facts">{"".join(facts)}</dl>'
        f'<div class="finding"><b>{label}</b><p>{reason}</p></div>'
        + _scenarios(result.get("scenarios") or [])
        + _source(
            hand.get("source"), hand.get("source_locator"), hand.get("action_ref")
        )
        + "</div></article>"
    )


def _filter(key, label, options):
    options_html = ['<option value="">All</option>']
    for value, text in options:
        options_html.append(f'<option value="{_text(value)}">{_text(text)}</option>')
    return f'<label>{label}<select data-filter="{key}">{"".join(options_html)}</select></label>'


def _timing(timing):
    stats = []
    for key, title, formatter in (
        ("samples", "Deals per hand and scenario", _count),
        ("wall_seconds", "Runtime", lambda v: f"{_fmt(v)} s"),
        ("threads", "Workers", _count),
        ("cpu", "Processor", _text),
    ):
        if timing.get(key) is not None:
            stats.append(_detail(title, formatter(timing[key])))
    if timing.get("seeds"):
        seeds = timing["seeds"]
        if isinstance(seeds, (list, tuple)):
            seeds = ", ".join(map(str, seeds))
        stats.append(_detail("Simulation seeds", _text(seeds)))
    return '<dl class="run">' + "".join(stats) + "</dl>" if stats else ""


def _selection(strata):
    if not strata:
        return ""
    rows = []
    for s in strata:
        rows.append(
            f'<tr><th scope="row">{_text(s["room"])}</th><td>{_chips(s.get("ante_per_seat"))}</td>'
            f"<td>{_count(s.get('eligible'))}</td><td>{_count(s.get('selected'))}</td></tr>"
        )
    return (
        '<div class="selection scroll"><table class="ev"><caption>Source hand selection</caption>'
        '<thead><tr><th scope="col">Room</th><th scope="col">Ante per player</th>'
        '<th scope="col">Eligible</th><th scope="col">Selected</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table><p class="hint">The sample is stratified by room and ante. '
        "The played action and result did not affect selection. Estimating error frequency requires a different sample.</p></div>"
    )


def render(dataset, results, path):
    """Join sources to results by (room, id), then write a standalone UTF-8 report."""
    dataset, results = _read(dataset), _read(results)
    hands = {}
    for hand in dataset.get("hands", []):
        key = _key(hand)
        if key in hands:
            raise ValueError(f"Duplicate source hand {key}")
        hands[key] = hand
    scored = {}
    for result in results.get("hands", []):
        key = _key(result)
        if key not in hands:
            raise ValueError(f"Result has no source hand {key}")
        if key in scored:
            raise ValueError(f"Duplicate result {key}")
        scored[key] = result

    def order(key):
        priority = _number(scored.get(key, {}).get("priority"))
        return (priority is None, -priority if priority is not None else 0, key)

    nav, articles = [], []
    rooms, categories, actions = set(), set(), set()
    for index, key in enumerate(sorted(hands, key=order)):
        hand, result = hands[key], scored.get(key, {})
        room, number = key
        category = str(result.get("category") or "No estimate")
        action = str(hand.get("action") or "")
        rooms.add(room)
        categories.add(category)
        if action:
            actions.add(action)
        anchor = _anchor(key)
        action_label = ACTIONS.get(action, action or "—")
        nav.append(
            f'<li><a class="handlink" data-room="{_text(room)}" data-action="{_text(action)}" '
            f'data-category="{_text(category)}" href="{_text("#" + quote(anchor))}">'
            f'<span class="navline"><b>{_text(room)} #{_text(number)}</b><span>{_text(action_label)}</span></span>'
            f'<span class="navcards">{_cards(hand.get("cards"))}</span><small>{_text(category)}</small></a></li>'
        )
        articles.append(_hand(hand, result, anchor, index))

    metrics = [
        _detail("Hands in report", _count(len(hands))),
        _detail(
            "Consistent reason to review",
            _count(sum(r.get("signal") == "consistent" for r in scored.values())),
        ),
        _detail(
            "Scenario-dependent choice",
            _count(sum(r.get("signal") == "sensitive" for r in scored.values())),
        ),
    ]
    validation = ""
    if results.get("validation_note"):
        validation = (
            '<details class="validation" open><summary>How well does the model match the observed hands?</summary><p>'
            + _text(results["validation_note"])
            + "</p></details>"
        )
    notes = results.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    note_list = (
        "<ul>" + "".join(f"<li>{_text(n)}</li>" for n in notes) + "</ul>"
        if notes
        else ""
    )
    filters = "".join(
        (
            _filter("room", "Poker room", [(v, v) for v in sorted(rooms)]),
            _filter(
                "action",
                "Hero action",
                [(v, ACTIONS.get(v, v)) for v in sorted(actions)],
            ),
            _filter(
                "category", "Review category", [(v, v) for v in sorted(categories)]
            ),
        )
    )
    empty = (
        "No hands match the filters."
        if hands
        else "This sample contains no hands for the spot."
    )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>BTN vs CO · PLO4 Hero Leak Finder</title>
<style>{CSS}</style></head><body><main>
<header class="mast"><div class="brand">♠ <span>PLO4 Hero Leak Finder</span></div>
<div class="intro"><div><h1>BTN vs CO</h1><p>After CO opens, BTN can fold, call or 3-bet.
Both blinds can still act.</p><a class="methodlink" href="#method">Model and sample selection</a></div><dl class="metrics">{"".join(metrics)}</dl></div></header>{validation}
<div class="workspace"><aside><h2>Hands to review</h2><div class="controls">{filters}</div>
<p class="quantity" role="status" aria-live="polite"></p><ol class="handlist">{"".join(nav)}</ol></aside>
<section class="content" aria-label="Hand details"><p id="empty" class="empty"{" hidden" if hands else ""}>{empty}</p>
{"".join(articles)}</section></div>
<section class="method" id="method"><h2>How to read the results</h2>
<p>Hands are ordered by review priority. The pilot compares actions under its modeling assumptions.
A larger difference can warrant a closer look. Assessing a strategic mistake requires additional evidence.</p>
{note_list}{_selection(dataset.get("strata"))}{_timing(results.get("timing") or {})}</section></main>
<script>{JS}</script></body></html>"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8", newline="\n")
    return target


CSS = """
*{box-sizing:border-box}html{background:#c1d0df;color:#162d42;font:15px/1.45 "Segoe UI",Arial,sans-serif}body{margin:0}
main{max-width:1420px;margin:auto;padding:20px}h1,h2,p,dl,dd,figure{margin:0}a{color:#194e75}
.mast,aside,.hand,.method,.empty{background:#fff;border:1px solid #9dB0c2;border-radius:7px;overflow:hidden}
.brand{padding:8px 20px;background:#edaa66;color:#182c40;font-size:22px;font-weight:750;border-bottom:1px solid #bb8e61}
.brand span{font-size:15px;vertical-align:2px;margin-left:6px}.intro{background:#f0f5fa;padding:19px 21px;display:flex;align-items:center;justify-content:space-between;gap:24px}.intro h1{font-size:clamp(26px,3vw,38px);line-height:1.15;margin-bottom:5px}
.intro p,.hint,.quantity,.method p,.source p,.unknown{color:#40576a}.metrics,.run{display:flex;flex-wrap:wrap;gap:10px 25px}
.methodlink{font-size:13px}.selection{margin:14px 16px;max-width:750px}
.validation{margin:10px 0;background:#ffe5cc;color:#202d38;border:1px solid #dfad7d;border-radius:6px;padding:9px 15px}.validation summary{cursor:pointer;font-weight:700}.validation p{margin:4px 0 0}
.metrics dt,.run dt,.facts dt{font-size:12px;font-weight:600;color:#435b70}.metrics dd{font-size:19px;font-weight:700}.metrics{flex-shrink:0}
.workspace{display:grid;grid-template-columns:minmax(235px,29%) minmax(0,1fr);gap:14px;margin:14px 0}
aside{align-self:start;background:#f3f7fb}.content{min-width:0}.controls{display:none;padding:11px 13px 0;gap:8px;grid-template-columns:1fr 1fr}.interactive .controls{display:grid}
aside h2,.method h2{font-size:19px;background:#dbe6f0;padding:11px 15px;border-bottom:1px solid #a4b6c5}
label{font-size:12px;font-weight:650;min-width:0}label:last-child{grid-column:1/-1}select{display:block;width:100%;font:inherit;font-weight:400;font-size:14px;background:#fff;color:#162d42;border:1px solid #8ca1b5;border-radius:4px;padding:5px 4px}
.quantity{padding:7px 14px;font-size:12px}.quantity:empty{display:none}.handlist{padding:0;list-style:none;margin:0}.interactive .handlist{max-height:max(420px,77vh);overflow:auto}
.handlist li+li{border-top:1px solid #c5d2dc}.handlink{display:block;text-decoration:none;color:#172f45;border-left:5px solid transparent;padding:8px 10px 9px;overflow-wrap:anywhere}
.handlink:hover{background:#e4edf6}.handlink[aria-current="true"]{background:#f9d5ab;border-left-color:#a65013}.navline{display:flex;gap:6px;justify-content:space-between;font-size:13px}.navline>span{white-space:nowrap}.handlink small{display:block;color:#354a5c;font-size:12px}
.cards{display:inline-flex;gap:4px;white-space:nowrap}.card{display:inline-block;background:#fff;border:1px solid #aab9c4;border-radius:3px;padding:0 4px;font-weight:750;line-height:1.35;font-size:16px;color:#192c41}
.navcards{display:block;margin:4px 0}.navcards .card{font-size:13px}.card.heart{color:#962c3c}.card.diamond{color:#245388}.card.club{color:#186456}
.hand{scroll-margin:10px}.hand+.hand{margin-top:15px}.interactive .hand+.hand{margin-top:0}.handhead{background:#e9f0f6;border-bottom:1px solid #b6c6d2;padding:11px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.handhead p{font-size:12px;color:#3b5368}.handhead h2{font-size:22px}.permalink{font-size:13px;white-space:nowrap}.handbody{padding:13px 16px}
.felt{background:#243e53;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:auto 42px auto;gap:4px 10px;padding:11px 15px;border-radius:10px;position:relative;isolation:isolate}
.felt:before{content:"";position:absolute;z-index:-1;inset:12% 5%;border-radius:90px;background:#8ab9af;border:4px solid #5c8c86}
.seat{display:flex;align-items:center;flex-wrap:wrap;justify-content:center;align-content:center;gap:1px 6px;justify-self:center;width:100%;max-width:175px;background:#e0eaf1;color:#193447;padding:4px 7px;border:1px solid #869dab;border-radius:6px;font-size:13px;text-align:center}
.seat b{font-size:14px}.seat small{font-size:12px}.seat.hero{background:#f4ac69;border:2px solid #bd6b23;padding:3px}.seat.hero .cards{flex-basis:100%;justify-content:center}.seat.folded{background:#d4dce1;color:#4d6070}
.pos-BB{grid-area:1/1}.pos-UTG{grid-area:1/2}.pos-HJ{grid-area:1/3}.pos-SB{grid-area:3/1}.pos-BTN{grid-area:3/2}.pos-CO{grid-area:3/3}
.pot{grid-area:2/1/3/4;align-self:center;justify-self:center;display:flex;align-items:center;gap:9px;color:#142e3d;background:#d9eae3;padding:3px 11px;border-radius:4px}.pot span{font-size:12px}.pot b{font-size:17px}
.hint{font-size:12px;line-height:1.4;margin-top:5px}.facts{display:flex;flex-wrap:wrap;gap:8px 22px;padding:10px 0}.facts dd,.run dd{font-size:14px;font-weight:650}
.finding{background:#fff1df;border:1px solid #eac59b;border-radius:5px;padding:8px 11px;margin-bottom:9px}.finding p{font-size:14px;margin-top:2px}
.scroll{overflow-x:auto}.ev{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}caption{text-align:left;font-weight:700;font-size:16px;padding:2px 0 5px}
.ev th,.ev td{padding:5px 8px;text-align:right;border-bottom:1px solid #d2dde5}.ev thead{background:#e4edf4;font-size:13px}.ev th:first-child{text-align:left}.ev tbody th{font-size:14px}.ev td small{display:block;font-size:11px;color:#4c6273;white-space:nowrap}
.source{margin-top:12px;border:1px solid #b6c7d4;border-radius:5px}.source summary{padding:8px 11px;background:#eaf0f5;cursor:pointer;font-weight:650}.source p,.source pre{margin:8px 11px}.source p{font-size:12px}.source pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f8fa;padding:8px;font:12px/1.5 Consolas,monospace;color:#162d42}
.method>p,.method>ul,.method>.run{margin:12px 16px}.method ul{padding-left:22px}.method li+li{margin-top:5px}.run{padding-top:10px;border-top:1px solid #cedae4}
.missing,.empty{padding:12px;background:#f3f7fa;color:#40566a}.handlink:focus-visible,summary:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid #245e92;outline-offset:-3px}
[hidden]{display:none!important}
@media(max-width:900px){.intro{align-items:start;flex-direction:column;gap:10px}.metrics{gap:12px 22px}.metrics dd{font-size:16px}main{padding:10px}.workspace{gap:8px}}
@media(max-width:680px){main{padding:7px}.workspace{grid-template-columns:1fr}.interactive .handlist{max-height:185px}.handbody{padding:10px}.felt{padding:8px;gap:3px}.seat{padding:3px 2px;gap:0 3px}.seat .card{font-size:13px;padding:0 2px}.seat.hero .cards{gap:2px}.ev td,.ev th{padding:4px}.intro{padding:12px}.handhead{padding:8px 10px}}
@media print{html{background:white}main{padding:0}.workspace{display:block}aside,.permalink{display:none}.hand[hidden]{display:block!important}article.hand{margin-bottom:12px;break-inside:avoid}.mast,.hand,.method{border-radius:0}.method{break-before:page}}
"""


JS = """
(() => {
  const links = Array.from(document.querySelectorAll('.handlink'));
  if (!links.length) return;
  const filters = Array.from(document.querySelectorAll('[data-filter]'));
  const empty = document.getElementById('empty');
  const quantity = document.querySelector('.quantity');
  const rows = links.map(link => ({link, body: document.getElementById(decodeURIComponent(link.hash.slice(1)))}));
  let current = null;
  document.documentElement.classList.add('interactive');
  function show(requested, updateAddress) {
    const eligible = [];
    for (const row of rows) {
      const visible = filters.every(el => !el.value || row.link.dataset[el.dataset.filter] === el.value);
      row.link.parentElement.hidden = !visible;
      if (visible) eligible.push(row);
    }
    current = eligible.find(row => row.link.hash === requested) || eligible[0] || null;
    for (const row of rows) {
      row.body.hidden = row !== current;
      if (row === current) row.link.setAttribute('aria-current', 'true');
      else row.link.removeAttribute('aria-current');
    }
    empty.hidden = !!current;
    quantity.textContent = 'Showing ' + eligible.length + ' / ' + rows.length;
    if (updateAddress && current && current.link.hash !== location.hash) {
      history.replaceState(null, '', current.link.hash);
    }
  }
  function followHash() {
    if (location.hash === '#method' && current) return;
    if (rows.some(row => row.link.hash === location.hash && row.link.parentElement.hidden)) {
      filters.forEach(el => { el.value = ''; });
    }
    show(location.hash, false);
  }
  links.forEach(link => link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, '', link.hash);
    show(link.hash, false);
  }));
  filters.forEach(el => el.addEventListener('change', () => show(current ? current.link.hash : '', true)));
  window.addEventListener('hashchange', followHash);
  followHash();
})();
"""
