"""Build the offline guide and apply the ordinary replayer entry point to its ten examples."""

import json
from html import escape
from pathlib import Path

from leakfinder.importers import Record, decode
from leakfinder.preflop_guide import GROUPS, SCOPE, at_decision
from leakfinder.replay_view import snapshot_from_record

from .render import _cards, _source


def build(dataset):
    groups = []
    for key, rule in GROUPS.items():
        groups.append(
            f'<tr id="g-{key}"><th>{escape(rule.title)}</th><td>{_cards(rule.example)}</td>'
            f"<td><b>{escape(rule.play)}</b></td><td>{escape(rule.why)}</td></tr>"
        )
    examples = []
    for hand in dataset["hands"]:
        rec = Record(hand["room"], hand["source"], hand["source_locator"], hand["id"])
        snap = snapshot_from_record(rec, decode(rec).hint, hand["action_ref"])
        rule = at_decision(snap)
        group = next((k for k, v in GROUPS.items() if rule == v), None)
        suggested = (
            f'<a href="#g-{group}">{escape(rule.play)}</a>' if rule else "Outside scope"
        )
        action = {"raise": "3-bet", "call": "Call", "fold": "Fold"}[hand["action"]]
        detail = _source(hand["source"], hand["source_locator"], hand["action_ref"])
        examples.append(
            f"<tr><th>{escape(hand['room'])} #{escape(hand['id'])}{detail}</th>"
            f"<td>{_cards(snap.hero_cards)}</td><td>{hand['open_to']:g} BB</td><td>{action}</td>"
            f"<td>{suggested}</td></tr>"
        )
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hand guide · BTN vs CO</title><style>
*{box-sizing:border-box}body{background:#ebf0f4;color:#192e40;margin:0;font:16px/1.5 'Segoe UI',Arial,sans-serif}
main{max-width:1160px;margin:auto;padding:24px 16px}header{background:#ffca96;border-radius:9px;padding:18px 24px}
h1,h2{line-height:1.15;margin:0 0 10px}h2{margin-top:24px}p{margin:6px 0}section{background:white;padding:12px 16px;border-radius:8px;margin:14px 0}
table{border-collapse:collapse;width:100%;text-align:left}th,td{padding:10px 8px;border-bottom:1px solid #d8e1e8;vertical-align:top}
thead{background:#dce6ee}tbody th{font-weight:600}tr:target{background:#ffe1c4}small{font-size:14px}
.scroll{overflow-x:auto}.cards{display:inline-flex;gap:3px;white-space:nowrap}.card{background:#f4f6f7;border:1px solid #bbc9d4;border-radius:3px;padding:0 3px;font-weight:600}
.heart,.diamond{color:#853545}summary,a{color:#24475e;cursor:pointer}summary{font-size:13px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:300px;overflow:auto}details[open]{width:min(470px,65vw)}
@media(max-width:650px){main{padding:10px}section,header{padding:9px}table{font-size:14px}}
</style><main><header><h1>Hand guide · BTN vs CO</h1>
<p>Open a source hand or select a recommendation to see the PLO4 preflop rule behind it.</p></header>
<section><p>"""
        + escape(SCOPE)
        + """</p><p>Two actions indicate a close decision. A difference from the guide is a reason to review the hand. Strategy and frequencies need independent validation.</p></section>
<h2>Ten real decisions</h2><div class="scroll"><table><thead><tr><th>Hand and source</th><th>BTN cards</th><th>CO opens to</th><th>Played</th><th>Guide</th></tr></thead>
<tbody>"""
        + "".join(examples)
        + """</tbody></table></div><h2>Hand groups and recommendations</h2>
<p>Each example shows which cards share a suit.</p><div class="scroll"><table>
<thead><tr><th>Group</th><th>Example</th><th>Recommendation</th><th>Reason</th></tr></thead><tbody>"""
        + "".join(groups)
        + "</tbody></table></div></main></html>"
    )


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent
    data = json.loads((folder / "hands.json").read_text(encoding="utf-8"))
    (folder / "BTN-CO-guide.html").write_text(build(data), encoding="utf-8")
