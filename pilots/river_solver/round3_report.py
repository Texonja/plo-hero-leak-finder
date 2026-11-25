"""Read the saved round-three inputs and publish the measured evidence."""

import argparse
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from statistics import mean, median

from .report import RULES
from .round2 import read
from .round3 import CONTROLS, CORE, MATERIAL, RESOLUTIONS
from .run import dump

LABELS = {
    "supports": "za",
    "opposes": "protiv",
    "close": "blizu",
    "unstable": "nestabilno",
    "rare": "retka linija",
}
FOLD_RULES = {"river_call_four_flush_board", "river_call_large_raise"}


def jsons(folder):
    return [read(f) for f in sorted(folder.glob("*.json"))]


def table(head, rows):
    return (
        "<table><thead><tr>"
        + "".join(f"<th>{h}</th>" for h in head)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>" for row in rows
        )
        + "</tbody></table>"
    )


def num(x, n=3):
    return "—" if x is None else f"{x:.{n}f}".replace(".", ",")


def convergence(base, extra):
    spots = defaultdict(dict)
    for s in base + extra:
        i = s["input"]
        a = i["audit"]
        spots[a["id"], a["profile"]][len(i["ranges"]["oop"])] = s
    controls = {}
    for (key, profile), runs in sorted(spots.items()):
        reference = key in CONTROLS and profile == "middle-middle"
        triggered = not runs[500]["comparison"]["stable_across_refinement"]
        if not reference and not triggered:
            continue
        values = {n: r["comparison"]["delta_bb"] for n, r in sorted(runs.items())}
        pot = runs[500]["input"]["pot_bb"]
        full = set(RESOLUTIONS) <= runs.keys()
        target = (
            max(MATERIAL * pot, 0.1 * abs(values[4000]))
            if full and values[4000] is not None
            else None
        )
        difference = (
            abs(values[2250] - values[4000])
            if target is not None and values[2250] is not None
            else None
        )
        credible = bool(
            difference is not None and difference <= target and runs[4000]["reliable"]
        )
        final = runs[max(runs)]
        available = [v for v in values.values() if v is not None]
        controls[key + "/" + profile] = {
            "complete": full,
            "passed": credible,
            "target_bb": target,
            "difference_bb": difference,
            "delta_bb": values[max(runs)] if credible else None,
            "interval_bb": None
            if credible or not available
            else [min(available), max(available)],
            "resolutions": {
                n: {
                    "delta_bb": r["comparison"]["delta_bb"],
                    "status": r["assessment"],
                    "best_action": r["comparison"]["best_legal_action"],
                    "root_exploitability_pct": r["exploitability_pct_pot"],
                    "seconds": r["timing"]["elapsed_seconds"],
                    "hero_equity": r["query"]["hero_equity_given_path"],
                }
                for n, r in sorted(runs.items())
            },
            "verdict": final["assessment"]
            if credible
            else "rare"
            if final["assessment"] == "rare"
            else "unstable",
            "spot": key,
            "profile": profile,
            "source_resolution": max(runs),
        }
    return controls


def aggregate(out, previous):
    cal = read(out / "calibration.json")
    prev = read(previous / "report.json")
    population = (
        read(out / "population.json") if (out / "population.json").is_file() else None
    )
    spots = jsons(out / "series")
    groups = jsons(out / "convergence")
    new_groups = [
        r
        for r in groups
        if r["input"]["range_model"]["oop"]["model"] == "street-likelihood-v3"
    ]
    old_groups = [
        r
        for r in groups
        if r["input"]["range_model"]["oop"]["model"] == "street-likelihood-v2"
    ]
    conv = convergence(spots, new_groups)
    set_demo_path = (
        previous / "spots" / "river_set_small_lead-00-500-middle-middle.json"
    )
    set_demo_old = (
        read(set_demo_path)["query"]["hero_equity_given_path"]
        if set_demo_path.is_file()
        else prev.get("set_demo_equity_at_500")
    )
    set_demo = conv.get("river_set_small_lead-00/middle-middle")
    dump(
        out / "convergence.json",
        {
            "controls": conv,
            "old_vs_new_4202": {
                "round2_middle_middle": prev["resolution"][
                    "river_call_four_flush_board-00"
                ]["runs"],
                "same_calibrated_parameters_old_grouping": {
                    len(r["input"]["ranges"]["oop"]): r["comparison"]["delta_bb"]
                    for r in old_groups
                },
                "same_calibrated_parameters_new_grouping": conv.get(
                    "river_call_four_flush_board-00/middle-middle"
                ),
            },
        },
    )
    samples = read(out / "validation-sample.json")
    validation = {}
    for key, specs in samples.items():
        rows = []
        for spot in specs:
            name = f"{key}-{spot['reference']}.json"
            path = out / "validation" / name
            if not path.is_file():
                continue
            result = read(path)
            q = result.get("query", {})
            rows.append(
                {
                    "file": "validation/" + name,
                    "partition": spot.get("fit_partition", "holdout"),
                    "composition": q.get("called_bettor_composition", {}),
                    "reach": q.get("called_path_reach"),
                    "converged": result.get("reliable", False),
                    "excluded": result.get("excluded"),
                }
            )
        usable = [r for r in rows if r["converged"] and r["composition"]]
        obs = cal["families"][key]
        details = {}
        for k, values in obs["categories"].items():
            p = mean(r["composition"].get(k, 0) for r in usable) if usable else None
            band = values["wilson95"]
            details[k] = {
                **values,
                "predicted": p,
                "within": band[0] <= p <= band[1] if band and p is not None else None,
            }
        valid = bool(usable) and all(r["partition"] == "holdout" for r in usable)
        validation[key] = {
            "selected": len(specs),
            "solved": len(usable),
            "aligns_with_library": all(d["within"] for d in details.values())
            if valid
            else None,
            "within_all_intervals_descriptive": all(
                d["within"] for d in details.values()
            )
            if usable
            else None,
            "rows": rows,
            "categories": details,
        }
    cal["validation"] = validation
    dump(out / "calibration.json", cal)
    base = {}
    for r in spots:
        a = r["input"]["audit"]
        key = a["id"] + "/" + a["profile"]
        if key in conv:
            row = conv[key]
        else:
            row = {
                "delta_bb": r["comparison"]["delta_bb"],
                "verdict": r["assessment"],
                "source_resolution": 500,
            }
        base[key] = (r, row)
    grid = jsons(out / "grid")
    byfamily = defaultdict(Counter)
    switch = defaultdict(Counter)
    for g in grid:
        found = base.get(g["variant"] + "/middle-middle")
        if not found:
            continue
        r, resolution_assessment = found
        initial = r["assessment"]
        family = r["input"]["audit"]["family"]
        byfamily[family][g["verdict"]] += 1
        g["baseline_500"] = initial
        g["baseline_resolved"] = resolution_assessment["verdict"]
        if initial != g["verdict"]:
            change = initial + " → " + g["verdict"]
            switch[family][change] += 1
            g["changed_from"] = initial
        g["clear_reversal"] = (
            initial in CORE and g["verdict"] in CORE and initial != g["verdict"]
        )
    plan = read(out / "sensitivity-design.json")
    expected = sum(len(v) for v in plan["variants"].values()) * 2 * len(plan["grid"])
    active_sources = sum(s["input"]["audit"]["rule_active"] for s in spots)
    full_grid = active_sources * 2 * len(plan["grid"])
    series_timing = out / "series-timing.json"
    projected = (
        full_grid * read(series_timing)["wall_seconds"] / len(spots)
        if spots and series_timing.is_file()
        else None
    )
    dump(
        out / "sensitivity.json",
        {
            "design": plan,
            "expected": expected,
            "completed": len(grid),
            "full_grid_scenarios": full_grid,
            "projected_full_grid_seconds_at_series_precision": projected,
            "outcomes": byfamily,
            "switches": switch,
            "scenarios": grid,
            "comparison": "Middle-middle, 500 representatives. Each coefficient moved alone. Clear reversals require a reliable, material change on a non-rare line.",
        },
    )
    breakdown = {}
    prior = (
        {g["family"]: g for g in prev["rules"].values()}
        if isinstance(prev["rules"], list)
        else prev["rules"]
    )
    for f in RULES:
        rows = []
        for key, (r, v) in base.items():
            a = r["input"]["audit"]
            if a["family"] != f:
                continue
            query = r["query"]
            rows.append(
                {
                    "variant": a["id"],
                    "profile": a["profile"],
                    "active": a["rule_active"],
                    "verdict": v["verdict"],
                    "delta_bb": v["delta_bb"],
                    "resolution": v["source_resolution"],
                    "interval_bb": v.get("interval_bb"),
                    "pot_bb": r["input"]["pot_bb"],
                    "hero_equity_500": query["hero_equity_given_path"],
                    "seconds": r["timing"]["elapsed_seconds"],
                    "game_exploitability_pct": r["exploitability_pct_pot"],
                    "best_action_500": r["comparison"]["best_legal_action"],
                    "old_text": a["app"],
                    "new_text": a["round3_app"],
                    "file": "series/" + key.replace("/", "-500-") + ".json",
                }
            )
        active = [z for z in rows if z["active"]]
        counts = {k: sum(z["verdict"] == k for z in active) for k in LABELS}
        eligible = len(active) - counts["rare"]
        yes, no = counts["supports"], counts["opposes"]
        numeric = eligible > 0 and 5 * yes >= 4 * eligible and not no
        peers = [g for g in grid if g["variant"] in plan["variants"][f]]
        reversals = sum(g.get("clear_reversal", False) for g in peers)
        destabilized = sum(
            "changed_from" in g
            and g["changed_from"] in CORE
            and g["verdict"] not in CORE
            for g in peers
        )
        near = [
            z
            for z in active
            if z["verdict"] in ("supports", "close")
            and z["delta_bb"] is not None
            and z["delta_bb"] >= -MATERIAL * z["pot_bb"]
        ]
        approx = len(near) / eligible if eligible else None
        local = cal["families"][f]["local_closing_calls"] if f in FOLD_RULES else None
        n = local["n"] if local else 0
        conflict = bool(local and n >= 20 and (local["average_bb"] or 0) > 0)
        complete = len(peers) == len(plan["variants"][f]) * 2 * len(plan["grid"])
        status = (
            "confirmed"
            if numeric
            else "revise"
            if eligible and no > yes and 5 * no >= 2 * eligible
            else "conditional"
        )
        breakdown[f] = {
            "counts": counts,
            "check_best_at_500": sum(z["best_action_500"] == "check" for z in active),
            "denominator_without_rare": eligible,
            "total_active": len(active),
            "second_round": prior[f],
            "status": status,
            "numeric_for_directive": numeric,
            "clear_reversals": reversals,
            "anchors_changed_to_uncertain": destabilized,
            "grid_complete": complete,
            "contradicted_by_verified_outcomes": conflict,
            "available_closing_call_observations": n,
            "directive_supported": numeric
            and complete
            and not reversals
            and not conflict,
            "size_within_materiality_count": len(near)
            if f.startswith("value_size")
            else None,
            "size_within_materiality_fraction": approx
            if f.startswith("value_size")
            else None,
            "rows": rows,
        }
    lp = jsons(out / "lp")
    for match in lp:
        if match["representatives"] != [500, 500]:
            continue
        target = (
            out / "series" / f"{match['input']['audit']['id']}-500-middle-middle.json"
        )
        if target.is_file():
            r = read(target)
            assert (
                r["ranges_sha256"] == match["ranges_sha256"]
                and r["input"]["tree"] == match["tree"]
            )
            match["saved_series_difference_pct_pot"] = (
                100 * abs(r["game_ev_bb"][0] - match["lp_oop_bb"]) / match["pot_bb"]
            )
    timings = {
        f: read(p)
        for f in ("series", "convergence", "validation", "grid", "lp")
        if (p := out / (f + "-timing.json")).is_file()
    }
    warn = []
    if len(spots) != 354:
        warn.append(f"Završeno {len(spots)} od 354 rešenja glavne serije.")
    if len(grid) != expected:
        warn.append(f"Završeno {len(grid)} od {expected} provera osetljivosti.")
    if len(lp) < 6 or not all(x["passed"] for x in lp):
        warn.append(
            f"LP provera ispod 0,01% pota potvrđena je za {sum(x['passed'] for x in lp)} od šest traženih ulaza."
        )
    pending = sum(not r["complete"] for r in conv.values())
    failed = sum(r["complete"] and not r["passed"] for r in conv.values())
    if pending:
        warn.append(f"Niz rezolucija još nema sve četiri tačke u {pending} slučajeva.")
    if failed:
        warn.append(
            f"U {failed} slučajeva promena EV nije ispunila kriterijum grupisanja. Prikazan je interval dobijenih vrednosti."
        )
    if population is None:
        warn.append("Nema ponovljenog pregleda verifikovanih isplata.")
    if (out / "tests.json").is_file():
        tests = read(out / "tests.json")
        if tests["returncode"]:
            warn.append("Testovi imaju neuspešan rezultat. Detalji su u testnom logu.")
    else:
        tests = None
    data = {
        "warnings": warn,
        "rule_results": breakdown,
        "convergence": conv,
        "set_demo_example": {
            "hand": 14007,
            "round2_equity_at_500": set_demo_old,
            "round3_at_highest_resolution": set_demo["resolutions"][
                set_demo["source_resolution"]
            ]
            if set_demo
            else None,
        },
        "population": population,
        "validation": validation,
        "calibration_file": "calibration.json",
        "sensitivity_file": "sensitivity.json",
        "convergence_file": "convergence.json",
        "timings": timings,
        "series_solves": len(spots),
        "active_solves": sum(z["total_active"] for z in breakdown.values()),
        "thresholds": {
            "rare_path_probability": 0.01,
            "confirmed_fraction": 0.8,
            "exact_size_regret_fraction_of_pot": MATERIAL,
            "grouping_change_fraction_of_pot": MATERIAL,
            "grouping_change_relative_fraction": 0.1,
            "minimum_verified_closing_calls": 20,
            "lp_difference_ceiling_pct_pot": 0.01,
        },
        "worst_series_exploitability_pct": max(
            (r["exploitability_pct_pot"] for r in spots), default=None
        ),
        "series_medianSeconds": median(r["timing"]["elapsed_seconds"] for r in spots)
        if spots
        else None,
        "lp": [{k: v for k, v in x.items() if k != "input"} for x in lp],
        "tests": tests,
    }
    dump(out / "report.json", data)
    return data


def build(out, previous):
    d = aggregate(out, previous)
    cal = read(out / "calibration.json")
    h = [
        '<!doctype html><html lang="sr-Latn"><meta charset="utf-8"><title>PLO4 river · treća runda</title><style>body{font:16px/1.5 system-ui;color:#172e3b;max-width:1240px;margin:2rem auto;padding:0 18px}table{border-collapse:collapse;width:100%;margin:.6rem 0 1.5rem}td,th{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid #ccd9df}th{background:#e6eef2}a{color:#12577c}.warning{background:#fff0ca;padding:10px}summary{cursor:pointer}</style><h1>PLO4 river · treća runda</h1>'
    ]
    for w in d["warnings"]:
        h.append(f'<p class="warning">{escape(w)}</p>')
    h += [
        "<h2>Nalazi protiv dosadašnjeg teksta</h2>",
        "<ul><li>„Consider folding“ uklonjen je iz dve kritike. Novi tekst glasi „Compare calling with folding“ i čuva cenu i argumente za obe akcije.</li><li>Tačnih 40% i 55% više nema kao preporučene opcije za proverene linije. Aplikacija nudi poređenje checka sa manjim i većim value betom.</li></ul>",
    ]
    extra = [
        escape(RULES[k][0])
        for k, z in d["rule_results"].items()
        if k not in FOLD_RULES
        and not k.startswith("value_size")
        and z["counts"]["opposes"]
    ]
    if extra:
        h.append(
            "<p>Jasan kontra-primer u kalibrisanoj seriji ima i "
            + ", ".join(extra)
            + ".</p>"
        )
    h.append("<h2>Koeficijenti iz biblioteke i pretpostavke</h2>")
    h.append(
        table(
            (
                "Akcija",
                "Koeficijent",
                "Stari",
                "Fit",
                "95% interval",
                "Holdout log-loss stari / novi",
            ),
            [
                (
                    group,
                    key,
                    num(z["old"]),
                    num(z["fit"]),
                    " – ".join(num(x) for x in z["ci95"]),
                    num(v["holdout_log_loss_old"], 4)
                    + " / "
                    + num(v["holdout_log_loss_fit"], 4),
                )
                for group, v in cal["fitted"].items()
                for key, z in v["coefficients"].items()
            ],
        )
    )
    assumptions = cal["remaining_assumptions"]
    h += [
        "<p>Pretpostavke obuhvataju "
        + escape(
            ", ".join(
                assumptions["preflop"]
                + assumptions["postflop"]
                + assumptions["additional"]
            )
        )
        + ".</p>",
        "<p>Fit je uslovljen kartama prikazanih protivnika. Preflop model razlikuje call od raisea među igračima koji nastavljaju, a postflop model bet od checka i raise od calla. Učestalost folda i ulaska u pot obuhvaćena je samo pretpostavkama. Holdout po hešu ruke je odvojen od treninga, uz isti kriterijum za stare i nove koeficijente.</p>",
    ]
    h.append("<h2>Presude po pravilu</h2>")
    h.append(
        "<p>Poređenje dve runde obuhvata različite koeficijente, novu podelu pri grupisanju i zaseban broj retkih linija. Kod #4202 provera sa istim koeficijentima izdvaja efekat samog grupisanja.</p>"
    )
    demo = d["set_demo_example"]
    if (
        demo["round2_equity_at_500"] is not None
        and demo["round3_at_highest_resolution"]
    ):
        possible = demo["round3_at_highest_resolution"]["hero_equity"]
        if possible is not None:
            h.append(
                f"<p>U #14007 najviša proverena rezolucija za srednje profile u trećoj rundi daje {num(100 * possible, 1)}% equityja posle beta. Druga runda sa 500 predstavnika dala je {num(100 * demo['round2_equity_at_500'], 1)}%.</p>"
            )
    h.append(
        table(
            (
                "Pravilo",
                "Druga runda za/protiv/blizu/neodr.",
                "Treća runda za/protiv/blizu/nestabilno/retka",
                "Imenilac",
                "Jasne promene smera",
                "Direktiva prolazi",
                "Sizing blizu najboljeg",
            ),
            [
                (
                    f'<a href="#{k}">{escape(RULES[k][0])}</a>',
                    " / ".join(
                        str(z["second_round"]["counts"].get(s, 0))
                        for s in ("supports", "opposes", "close", "indeterminate")
                    ),
                    " / ".join(str(z["counts"][s]) for s in LABELS),
                    z["denominator_without_rare"],
                    z["clear_reversals"],
                    "da" if z["directive_supported"] else "ne",
                    num(100 * z["size_within_materiality_fraction"], 1) + "%"
                    if z["size_within_materiality_fraction"] is not None
                    else "—",
                )
                for k, z in d["rule_results"].items()
            ],
        )
    )
    h.append(
        "<p>Retka linija ima doseg ispod 1% javno ili ispod 1% među protivničkim rukama kompatibilnim sa Hero kartama. U imenilac ulaze za, protiv, blizu i nestabilno. Direktiva traži najmanje 80% za, nula protiv, mrežu bez jasne promene smera i podršku gde su dostupni dovoljni verifikovani ishodi. Mreža koristi srednje profile oba igrača i po četiri dodatne varijante uz aktivne demo ruke.</p>"
    )
    sensitivity = read(out / "sensitivity.json")
    projection = sensitivity["projected_full_grid_seconds_at_series_precision"]
    if projection is not None:
        h.append(
            f"<p>Projekcija za {sensitivity['full_grid_scenarios']:,} kombinacija svih aktivnih profila pri preciznosti glavne serije iznosi {num(projection / 3600, 2)} sata na ovom CPU-u. Mreža sadrži {sensitivity['completed']:,} pojedinačnih promena parametra i koristi jedan numerički prolaz po rešenju.</p>"
        )
    h.append("<h2>Validacija sastava river rangea</h2>")
    h.append(
        "<p>Sastav modela je prosek uslovnih raspodela za izabrane ruke. Svaka raspodela uslovljena je celom linijom do calla, uključujući call. Za svaki sastav udeo i interval u biblioteci dele se brojem ruku sa prikazanim agresorovim kartama. Kante beta su do 40%, preko 40% do 80% i preko 80% pota. Raise ima svoju kantu.</p>"
    )
    for k, v in d["validation"].items():
        e = cal["families"][k]
        a = e["line"]
        criterion = (
            "u skladu"
            if v["aligns_with_library"]
            else "odstupa"
            if v["aligns_with_library"] is False
            else "nema nezavisne ocene"
        )
        h.append(
            f"<h3>{escape(RULES[k][0])}</h3><p>Flop {escape(' → '.join(a[0]))}. Turn {escape(' → '.join(a[1]))}. River {escape(a[2])}, kanta {escape(a[3])}. P označava agresora, C callera. Heads-up od početka rivera. Najviše karata jedne boje {escape(', '.join(map(str, a[4])))}. Bord {'može biti uparen ili neuparen' if a[5] is None else 'je uparen' if a[5] else 'je neuparen'}. Prikazano {e['shown']} od {e['called']}, nepoznato {e['unknown_cards']}. Model {v['solved']} od {v['selected']} izabranih. {criterion.capitalize()}.</p>"
        )
        h.append(
            table(
                ("Kategorija", "Broj", "Biblioteka %", "Wilson 95%", "Model %"),
                [
                    (
                        k,
                        z["n"],
                        num(z["rate"] * 100, 1) if z["rate"] is not None else "—",
                        " – ".join(num(x * 100, 1) for x in z["wilson95"])
                        if z["wilson95"]
                        else "—",
                        num(z["predicted"] * 100, 1)
                        if z["predicted"] is not None
                        else "—",
                    )
                    for k, z in v["categories"].items()
                ],
            )
        )
        pre = e.get("preflop_lines", {})
        if pre:
            h.append(
                "<details><summary>Preflop redosled aktivnih P/C akcija</summary>"
                + table(
                    ("Redosled", "Ruke"),
                    [
                        (escape(z or "—"), n)
                        for z, n in sorted(pre.items(), key=lambda y: -y[1])
                    ],
                )
                + "</details>"
            )
        if v["rows"]:
            h.append(
                "<details><summary>Izabrani ulazi sa veličinama i rešenjima</summary><ul>"
                + "".join(
                    f'<li><a href="{escape(r["file"])}">{escape(r["file"].split("/")[-1])}</a> · '
                    f"{escape(r['partition'])} · "
                    + ("rešeno" if r["converged"] else "izostavljeno")
                    + "</li>"
                    for r in v["rows"]
                )
                + "</ul></details>"
            )
    p = d["population"]
    if p:
        h.append(
            f"<h2>Ishodi od trenutka odluke</h2><p>Pregledano {p['hands_replayed']:,} iPoker ruku. Glavna tabela meri isplatu iz verifikovanih završnih heads-up callova umanjenu za iznos calla. Plaćene ruke čine izbor koji ne uključuje foldovane ruke.</p>"
        )
        result = []
        for k, g in p["groups"].items():
            for name in ("all_calls", "critique_matched"):
                a = g[name]
                result.append(
                    (
                        escape(RULES[k][0]),
                        "svi" if name == "all_calls" else "tačno pravilo",
                        a["closing_hu_settled"],
                        num(a["realized_closing_call_vs_fold_bb"]),
                        num(a["realized_closing_call_vs_fold_mean_bb"]),
                        str(a["realized_closing_call_positive"])
                        + " / "
                        + str(a["realized_closing_call_negative"]),
                        num(a["trimmed_mean_omit_best_and_worst_bb"]),
                        a["unverified_closing_hu_count"],
                    )
                )
        h.append(
            table(
                (
                    "Linija",
                    "Obuhvat",
                    "n",
                    "Call − fold BB",
                    "Prosek BB",
                    "Plus / minus",
                    "Prosek bez dva ekstrema",
                    "Neporavnati završni HU",
                ),
                result,
            )
        )
        raw = []
        for k, g in p["groups"].items():
            for name in ("all_calls", "critique_matched"):
                a = g[name]
                source = a["source_reported"]
                raw.append(
                    (
                        escape(RULES[k][0]),
                        "svi" if name == "all_calls" else "tačno pravilo",
                        num(source["source_reported_whole_hand_net_bb"]),
                        a["unverified_whole_hand_count"],
                        num(source["source_reported_closing_call_vs_fold_bb"]),
                        a["unverified_closing_hu_count"],
                    )
                )
        h.append(
            "<details><summary>Izvorni iznosi i rezultat cele ruke</summary><p>Rezultat cele ruke uključuje uplate svih prethodnih ulica. JSON navodi i obuhvat pojedinih polja.</p>"
            + table(
                (
                    "Linija",
                    "Obuhvat",
                    "XML net BB",
                    "Bez pune provere neta",
                    "XML call − fold BB",
                    "Bez pune provere calla",
                ),
                raw,
            )
            + '<a href="population.json">Detalji o isplatama</a></details>'
        )
    h.append("<h2>Rezolucija grupisanja</h2>")
    h.append(
        table(
            (
                "Spot i profil",
                "500 / 1.500 / 2.250 / 4.000 BB",
                "Promena BB",
                "Dozvoljeno BB",
                "Nalaz",
            ),
            [
                (
                    escape(key),
                    " / ".join(
                        num(r["resolutions"][str(n)]["delta_bb"])
                        if str(n) in r["resolutions"]
                        else num(r["resolutions"].get(n, {}).get("delta_bb"))
                        for n in RESOLUTIONS
                    )
                    if r["passed"]
                    else (
                        "Interval " + " do ".join(num(x) for x in r["interval_bb"])
                        if r["interval_bb"]
                        else "—"
                    ),
                    num(r["difference_bb"]),
                    num(r["target_bb"]),
                    "iskonvergiralo" if r["passed"] else "nije iskonvergiralo",
                )
                for key, r in d["convergence"].items()
            ],
        )
    )
    h.append(
        '<p><a href="convergence.json">Staro i novo grupisanje na #4202 i numerički detalji</a></p>'
    )
    h.append(
        "<p>Interval grupisanja prikazuje najmanju i najveću izmerenu EV razliku preko rezolucija. Wilsonovi intervali u prethodnoj sekciji odnose se na učestalost kategorija u biblioteci.</p>"
    )
    h.append("<h2>Nezavisni LP i vreme</h2>")
    h.append(
        table(
            (
                "Bord",
                "Predstavnika",
                "LP BB",
                "CFR BB",
                "Odstupanje % pota",
                "Odstupanje od glavne serije % pota",
            ),
            [
                (
                    " ".join(x["board"]),
                    x["representatives"][0],
                    num(x["lp_oop_bb"], 6),
                    num(x["cfr_oop_bb"], 6),
                    num(x["absolute_difference_pct_pot"], 6),
                    num(x.get("saved_series_difference_pct_pot"), 6),
                )
                for x in d["lp"]
            ],
        )
    )
    h.append(
        table(
            ("Faza", "Broj poslova", "Zidno vreme s", "CPU", "Radnici"),
            [
                (
                    k,
                    x["jobs"],
                    num(x.get("series_end_to_last_resolution_seconds",x["wall_seconds"]), 1),
                    escape(x["cpu"]),
                    x["workers"],
                )
                for k, x in d["timings"].items()
            ],
        )
    )
    if d["tests"]:
        h.append('<p><a href="tests.json">Rezultati testova</a></p>')
    h.append("<p>Faze su se delimično izvršavale paralelno. Vreme provere rezolucije meri period od kraja glavne serije do poslednjeg snimljenog rešenja.</p>")
    for k, r in d["rule_results"].items():
        h.append(f'<h2 id="{k}">{escape(RULES[k][0])}</h2>')
        h.append(
            table(
                (
                    "Varijanta",
                    "Protivnik / Hero",
                    "Presuda",
                    "EV prednost BB",
                    "Stvarni tekst aplikacije",
                ),
                [
                    (
                        f'<a href="{x["file"]}">{x["variant"]}</a>',
                        x["profile"],
                        LABELS[x["verdict"]],
                        " do ".join(num(v) for v in x["interval_bb"])
                        if x.get("interval_bb")
                        else num(x["delta_bb"]),
                        escape(
                            (x["new_text"].get("critique") or {}).get("alternative")
                            or x["new_text"]["guide"]
                        )
                        if x["active"]
                        else "Kontrola",
                    )
                    for x in r["rows"]
                ],
            )
        )
    h.append(
        '<p><a href="report.json">Kompletni podaci</a> · <a href="calibration.json">Kalibracija i holdout</a> · <a href="sensitivity.json">Mreža osetljivosti</a></p></html>'
    )
    (out / "Izvestaj.html").write_text("\n".join(h), encoding="utf8")
    return d


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("previous", type=Path)
    args = ap.parse_args()
    build(args.out, args.previous)
