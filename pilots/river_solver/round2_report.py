"""Build the round-two report entirely from saved inputs, solved games and local aggregates."""

from collections import defaultdict
from html import escape
from pathlib import Path
from statistics import median

from .model import GROUPS, digest
from .report import LABEL, RULES, SETS, classify
from .round2 import RESOLUTION_IDS, read, sign
from .run import dump


def brief(r, path):
    spec = r["input"]
    a, q, c = spec["audit"], r["query"], r["comparison"]
    seat = ("oop", "ip")[a["opponent_seat"]]
    model = spec["range_model"]
    return {
        "file": path.name,
        "family": a["family"],
        "variant": a["id"],
        "source": a["source"],
        "hand": int(a["source_hand"]) % 100000,
        "active": a["rule_active"],
        "opponent": a["opponent_profile"],
        "hero": a["hero_profile"],
        "count": a["representatives"],
        "board": spec["board"],
        "hole": spec["query"]["hero"],
        "path": spec["query"]["path"],
        "pot_bb": spec["pot_bb"],
        "played": a["app"]["played"],
        "guide": a["app"]["guide"],
        "critique": a["app"]["critique"],
        "actions": q["hero_action_ev_bb"],
        "best": c["best_legal_action"],
        "delta": c["delta_bb"],
        "verdict": c["verdict"],
        "threshold_bb": c["indifference_bb"],
        "public_reach": q["node_probability"],
        "unstable_refinement": not c["stable_across_refinement"],
        "coarse_delta": c["coarse_delta_bb"],
        "best_continuation_delta": c["best_continuation_delta_bb"],
        "opponent_reach": q["opponent_path_probability_given_hero"],
        "equity": q["hero_equity_given_path"],
        "opponent_after_line": q["opponent_categories_given_hero_and_path"],
        "hero_side": q["actor"],
        "opponent_side": seat,
        "time": r["timing"]["elapsed_seconds"],
        "exploitability_pct": r["exploitability_pct_pot"],
        "gap_pct": r["nashconv_pct_pot"],
        "converged": r["reliable"],
        "range_hash": r["ranges_sha256"],
        "model": {
            role: {
                k: model[role][k]
                for k in (
                    "support_count",
                    "universe_count",
                    "hero_probe",
                    "composition_full",
                    "composition_represented",
                    "discarded_mass_fraction",
                )
            }
            for role in ("oop", "ip")
        },
    }


def assemble(out):
    inv = read(out / "inventory.json")
    count = len(inv["variants"])
    assert count == 102
    sens = {}
    for key in sorted(RESOLUTION_IDS):
        rows = []
        for size in (500, 1500, 2250):
            f = out / "spots" / f"{key}-{size}-middle-middle.json"
            if not f.exists():
                if size < 2250:
                    raise FileNotFoundError(f)
                continue
            r = read(f)
            rows.append(
                {
                    "size": size,
                    "file": f.name,
                    "delta": r["comparison"]["delta_bb"],
                    "sign": sign(r["comparison"]),
                    "best": r["comparison"]["best_legal_action"],
                    "ev": r["query"]["hero_action_ev_bb"],
                    "verdict": r["comparison"]["verdict"],
                    "gap": r["exploitability_pct_pot"],
                    "seconds": r["timing"]["elapsed_seconds"],
                }
            )
        flip = rows[0]["sign"] != rows[1]["sign"]
        best_changed = rows[0]["best"] != rows[1]["best"]
        if (flip or best_changed) and len(rows) != 3:
            raise ValueError(f"Missing expanded comparison for {key}")
        sens[key] = {
            "runs": rows,
            "difference_bb": rows[1]["delta"] - rows[0]["delta"],
            "sign_changed": flip,
            "best_changed": best_changed,
            "best_at_final_two_agrees": rows[-1]["best"] == rows[-2]["best"],
            "stable_final_direction": rows[-1]["sign"] == rows[-2]["sign"],
            "series_size": 2250 if flip or best_changed else 500,
        }
    items = []
    seen_hands = set()
    for key, v in sorted(inv["variants"].items()):
        for name in v["files"]:
            size = sens.get(key, {}).get("series_size", 500)
            if size != 500:
                name = name.replace("-500-", f"-{size}-")
            f = out / "spots" / name
            r = read(f)
            spec = r["input"]
            assert (
                digest(spec) == r["input_sha256"]
                and digest(spec["ranges"]) == r["ranges_sha256"]
            )
            assert (
                read(out / ("inputs" if size == 500 else "validation-inputs") / name)
                == spec
            )
            assert abs(sum(r["game_ev_bb"]) - spec["pot_bb"]) < 1e-6
            row = brief(r, f)
            if key in sens and not sens[key]["stable_final_direction"]:
                row["verdict"] = "indeterminate"
            if row["source"] == "demo":
                seen_hands.add(row["hand"])
            items.append(row)
    assert len(items) == 354 and len(seen_hands) == 8
    grouped = defaultdict(list)
    for x in items:
        grouped[x["family"]].append(x)
    rules = {}
    for key, rows in grouped.items():
        active = [r for r in rows if r["active"]]
        counts = {k: sum(r["verdict"] == k for r in active) for k in LABEL}
        supported = [r for r in active if not r["model"][r["hero_side"]]["hero_probe"]]
        supported_counts = {k: sum(r["verdict"] == k for r in supported) for k in LABEL}
        rules[key] = {
            "counts": counts,
            "hero_tail_probes": len(active) - len(supported),
            "excluding_hero_tail_probes": {
                "counts": supported_counts,
                "classification": classify(supported_counts),
            },
            "classification": classify(counts),
            "active_solves": len(active),
            "active_variants": len({x["variant"] for x in active}),
            "controls": len(rows) - len(active),
            "negative_raw_delta": sum(
                r["delta"] is not None and r["delta"] < 0 for r in active
            ),
            "rare_opponent_path": sum(r["opponent_reach"] < 0.01 for r in active),
            "rare_public_path": sum(r["public_reach"] < 0.001 for r in active),
            "crosses_materiality_on_refinement": sum(
                r["unstable_refinement"] for r in active
            ),
            "fold_worse_majority": counts["opposes"] * 2 > len(active)
            if key in ("river_call_large_raise", "river_call_four_flush_board")
            else None,
        }
    lp = read(out / "lp.json")
    ex = read(out / "excluded.json")
    pop_files = [
        k
        for k in ("population.json", "population-confirmed-aliases.json")
        if (out / k).is_file()
    ]
    verification = read(out / "tests.json")
    warnings = []
    if not lp["all_passed"] or len(lp["cases"]) < 3:
        warnings.append("LP provera nije prošla na tri potrebite igre.")
    if verification["returncode"] or "skipped=" in verification["output"]:
        warnings.append("Testovi pilota nisu u celosti prošli.")
    failed = [r["file"] for r in items if not r["converged"]]
    if failed:
        warnings.append(
            f"{len(failed)} rešenja nije zadovoljilo numerički cilj. Navedena su u JSON-u."
        )
    if not pop_files:
        warnings.append("Lična biblioteka nije proverena u ovom pokretanju.")
    unresolved = [k for k, x in sens.items() if not x["stable_final_direction"]]
    if unresolved:
        warnings.append(
            "Smer se menja i pri najvećoj rezoluciji za " + ", ".join(unresolved) + "."
        )
    best_unstable = [k for k, x in sens.items() if not x["best_at_final_two_agrees"]]
    if best_unstable:
        warnings.append(
            "Akcija sa najvišim EV i dalje zavisi od rezolucije za "
            + ", ".join(best_unstable)
            + "."
        )
    times = [r["time"] for r in items]
    solve_time = read(out / "solve-timing.json")
    refinement = read(out / "refine-timing.json")
    elapsed = {
        "preparation_seconds": inv["seconds"],
        "batch_seconds": solve_time["seconds"],
        "refinement_seconds": refinement["seconds"],
        "primary_median_seconds": median(times),
        "primary_min_seconds": min(times),
        "primary_max_seconds": max(times),
        "primary_sum_seconds": sum(times),
        "series_wall_with_prep_seconds": inv["seconds"]
        + solve_time["seconds"]
        + refinement["seconds"],
        "workers": solve_time["workers"],
        "cpu": inv["cpu"],
        "threads_per_solve": 1,
    }
    return {
        "warnings": warnings,
        "variants": count,
        "primary_solves": len(items),
        "demo_hands": sorted(seen_hands),
        "demo_solves": sum(r["source"] == "demo" for r in items),
        "non_demo_solves": sum(r["source"] != "demo" for r in items),
        "profile_order": ["opponent", "hero"],
        "active_solves": sum(r["active_solves"] for r in rules.values()),
        "universe": items[0]["model"]["oop"]["universe_count"],
        "thresholds": {
            "confirmed_fraction": 0.8,
            "revision_fraction": 0.4,
            "fold_worse_majority_strictly_above": 0.5,
            "equivalent_action_fraction": 0.005,
            "exploitability_ceiling_pct": 0.1,
        },
        "rules": rules,
        "resolution": sens,
        "lp": lp,
        "excluded": ex,
        "timing": elapsed,
        "tests": verification,
        "failed_solves": failed,
        "worst_exploitability_pct": max(x["exploitability_pct"] for x in items),
        "population": {k: read(out / k) for k in pop_files},
        "spots": items,
    }


def build(out):
    out = Path(out)
    data = assemble(out)
    if (out / "first-round-counts.json").is_file():
        data["historical_reclassification"] = read(out / "first-round-counts.json")
    if (out / "critique-decision.json").is_file():
        data["critique_decision"] = read(out / "critique-decision.json")
    if (out / "population-settlement.json").is_file():
        data["settlement"] = read(out / "population-settlement.json")
    dump(out / "report.json", data)

    def fmt(v, n=2):
        return "—" if v is None else f"{v:.{n}f}".replace(".", ",")

    def esc(s):
        return escape(str(s))

    def link(url, text):
        return f'<a href="{esc(url)}">{esc(text)}</a>'

    def table(cols, rows):
        return (
            "<table><thead><tr>"
            + "".join("<th>" + esc(x) + "</th>" for x in cols)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>" + "".join("<td>" + str(x) + "</td>" for x in row) + "</tr>"
                for row in rows
            )
            + "</tbody></table>"
        )

    h = [
        '<!doctype html><html lang="sr-Latn"><meta charset="utf-8"><title>River solver · druga runda</title><style>body{font:16px/1.5 system-ui;color:#172e3b;max-width:1240px;margin:2rem auto;padding:0 18px}table{border-collapse:collapse;width:100%;margin:.6rem 0 1.5rem}td,th{text-align:left;vertical-align:top;padding:6px 9px;border-bottom:1px solid #ccd9df}th{background:#e6eef2}summary{cursor:pointer}a{color:#12577c}small{color:#52636c}.warn{background:#ffe1b6;padding:10px}</style><h1>River solver · druga runda</h1>'
    ]
    for msg in data["warnings"]:
        h.append('<p class="warn">' + esc(msg) + "</p>")
    for bucket, title in zip(
        SETS, ("Potvrđeno", "Za ispravku", "Zavisi od opsega ili je neodređeno")
    ):
        rows = [
            [link("#" + k, RULES[k][0]), *(v["counts"][t] for t in LABEL)]
            for k, v in data["rules"].items()
            if v["classification"] == bucket
        ]
        h += [
            "<h2>" + title + "</h2>",
            table(["Pravilo", "Za", "Protiv", "Blizu", "Neodređeno"], rows)
            if rows
            else "<p>Nijedno.</p>",
        ]
    h += [
        "<p>Imenilac je svako aktivno rešenje, uključujući neodređena. Potvrda zahteva najmanje 80% za i nijedno protiv. Ispravka zahteva više protiv nego za i najmanje 40% protiv. Za promenu dve kritike dodatno se traži jasna većina svih aktivnih rešenja i razmatraju se dostupne ruke iz biblioteke. Devet kombinacija jedne demo ruke predstavljaju analizu osetljivosti i imaju zajednički bord.</p>"
    ]
    if "critique_decision" in data:
        h += ["<h2>Odluka o dve kritike</h2>"]
        for k, v in data["critique_decision"].items():
            h.append(
                "<p><b>" + esc(RULES[k][0]) + "</b> · " + esc(v["explanation"]) + "</p>"
            )
    if "historical_reclassification" in data:
        h.append(
            "<details><summary>Isto razvrstavanje na podacima iz prve runde</summary>"
            + table(
                [
                    "Pravilo",
                    "Za / protiv / blizu / neodređeno",
                    "Prvobitno",
                    "Novi kriterijum",
                ],
                [
                    [
                        esc(RULES[k][0]),
                        " / ".join(str(x["counts"][key]) for key in LABEL),
                        esc(x["original_heading"]),
                        esc(x["new_heading"]),
                    ]
                    for k, x in data["historical_reclassification"].items()
                ],
            )
            + "</details>"
        )
    h += [
        "<h2>Obuhvat i provera</h2>",
        f"<p>{data['variants']} varijante, {data['primary_solves']} rešenja. Osam demo ruku dalo je {data['demo_solves']} rešenja, ostale varijante {data['non_demo_solves']}. #8585 ima četiri igrača na riveru i ostaje izvan heads-up zaključaka. Provereni su stvarni saveti iz pokrenute aplikacije. Sve odluke odnose se na river bez rakea, uz pot-limit stablo iz ulaza.</p>",
        f"<p>Oba igrača kreću od {data['universe']:,} legalnih ruku pre međusobnog uklanjanja karata. Model ponderiše svaku po odigranim prethodnim akcijama. Parametri i odstranjeni repovi su pretpostavke. Prikaz daje težinski sastav pune podrške i izbor stvarnih predstavnika. Njihovi međusobni blokerski efekti mogu odstupati od pune igre. Tri provere rezolucije mere tamošnje promene u EV, a ne najveću moguću grešku drugih varijanti.</p>",
        f"<p>Najviša exploitability reprezentativne igre bila je {fmt(data['worst_exploitability_pct'], 5)}% pota. Rešenja su proverena na dva numerička cilja. Prednost do 0,5% početnog pota računa se kao bliska. Mali doseg čvora ili promena ocene pri preciznijem rešavanju daju neodređen nalaz. Ako bi profil odstranio stvarnu Hero ruku kao malo verovatnu, zadržana je zasebno sa svojom prvobitnom težinom. JSON po pravilu sadrži i rezultate bez tih slučajeva.</p>",
    ]
    h += [
        table(
            [
                "Kontrolni spot",
                "Prednost predloga pri 500 / 1500 / povećanom broju (BB)",
                "Promena 1500 − 500 (BB)",
                "Najviši EV po rezoluciji",
                "Predstavnici u glavnoj seriji",
            ],
            [
                [
                    esc(k),
                    " / ".join(fmt(x["delta"], 3) for x in r["runs"]),
                    fmt(r["difference_bb"], 3),
                    " → ".join(esc(x["best"]) for x in r["runs"]),
                    r["series_size"],
                ]
                for k, r in data["resolution"].items()
            ],
        )
    ]
    h += [
        table(
            [
                "Nezavisni LP",
                "Predstavnici po igraču",
                "LP OOP (BB)",
                "Kontrolni CFR OOP (BB)",
                "Kontrolno odstupanje (% pota)",
                "Odstupanje od završne serije (% pota)",
            ],
            [
                [
                    esc(r["input_file"]),
                    r["representatives"][0],
                    fmt(r["lp_oop_bb"], 5),
                    fmt(r["cfr_oop_bb"], 5),
                    fmt(r["absolute_difference_pct_pot"], 6),
                    fmt(
                        (r.get("production") or {}).get("absolute_difference_pct_pot"),
                        6,
                    ),
                ]
                for r in data["lp"]["cases"]
            ],
        )
    ]
    t = data["timing"]
    h.append(
        f"<p>{esc(t['cpu'])}, {t['workers']} paralelnih radnika i jedna nit po rešenju. Medijana {fmt(t['primary_median_seconds'])} s, raspon {fmt(t['primary_min_seconds'])}–{fmt(t['primary_max_seconds'])} s. Priprema {fmt(t['preparation_seconds'])} s, računanje serije uključujući kontrole grupisanja {fmt(t['batch_seconds'] + t['refinement_seconds'])} s. Ukupno {fmt(t['series_wall_with_prep_seconds'])} s. Nezavisne LP i provere biblioteke merene su odvojeno.</p>"
    )
    h.append("<h2>Stvarni callovi iz biblioteke</h2>")
    for file, pop in data["population"].items():
        title = (
            "Aktivni profil"
            if file == "population.json"
            else "Svi potvrđeni lokalni aliasi"
        )
        h.append(
            f"<h3>{title}</h3><p>Pregledano {pop['hands_replayed']:,} Hero ruku. {link(file, 'Agregatni podaci i definicije')}.</p>"
        )
        rows = []
        for k, g in pop["groups"].items():
            for which in ("all_calls", "critique_matched"):
                x = g[which]
                s = x["by_last_matching_call"]
                raw = x["source_reported"]
                rows.append(
                    [
                        "Call protiv river raisea"
                        if k == "river_call_large_raise"
                        else "Call protiv beta na bordu sa četiri iste boje",
                        "svi callovi"
                        if which == "all_calls"
                        else "tačni uslovi kritike",
                        x["hands"],
                        " / ".join(
                            str(s[a]) for a in ("stronger", "weaker", "tie", "unknown")
                        ),
                        x["weak_air_proxy_subset_of_weaker"],
                        fmt(raw["source_reported_whole_hand_net_bb"], 3)
                        + " ("
                        + str(raw["hands_with_source_net"])
                        + ")",
                        fmt(raw["source_reported_closing_call_vs_fold_bb"], 3)
                        + " ("
                        + str(raw["closing_hu_hands_with_source_award"])
                        + ")",
                        fmt(x["whole_hand_net_bb"])
                        + " ("
                        + str(x["whole_hand_settled"])
                        + ")",
                        fmt(x["realized_closing_call_vs_fold_bb"])
                        + " ("
                        + str(x["closing_hu_settled"])
                        + ")",
                    ]
                )
        h.append(
            table(
                [
                    "Grupa",
                    "Uslovi",
                    "Ruke",
                    "Protivnik jači / slabiji / isto / nepoznato",
                    "Vrlo slab pokazani sastav¹",
                    "XML net cele ruke BB (n)",
                    "XML završni HU call prema foldu BB (n)",
                    "Verifikovan net cele ruke BB (n)",
                    "Verifikovan završni HU call prema foldu BB (n)",
                ],
                rows,
            )
        )
        h.append(
            "<p>XML net je iznos evidentirane isplate umanjen za ulaganje. Usklađenost s ukupnim potom zasebno se proverava kada izvor dozvoljava poravnanje svih uplata, isplata i naknada. Nedostajuća polja za rake smanjuju obuhvat pune provere.</p>"
        )
    h.append(
        "<p>¹ Visoka karta ili samo zajednički par na bordu među slabijim rukama. Pokazane karte opisuju jačinu. Blef je namera i ne može se pouzdano prebrojati iz ishoda. Uzorak sadrži samo plaćene betove i raiseove. Dobitak završnog calla prema foldu meri realizovani rezultat u toj podeli. Ruke s više igrača ulaze u široki opis, a poslednja kolona obuhvata samo proverene završne heads-up callove.</p>"
    )
    if "settlement" in data:
        h.append(
            "<p>"
            + link(
                "population-settlement.json",
                "Dodatna provera isplata u izvornim istorijama",
            )
            + "</p>"
        )
    by_family = defaultdict(list)
    for row in data["spots"]:
        by_family[row["family"]].append(row)
    for key, rows in by_family.items():
        h += [
            '<h2 id="' + key + '">' + esc(RULES[key][0]) + "</h2>",
            "<p>" + esc(RULES[key][1]) + " " + esc(RULES[key][2]) + "</p>",
        ]
        variants = defaultdict(list)
        for x in rows:
            variants[x["variant"]].append(x)
        for name, v in variants.items():
            x = v[0]
            a = x["critique"] or {}
            heading = (
                ("Demo #" + str(x["hand"]) if x["source"] == "demo" else name)
                + " · "
                + " ".join(x["board"])
                + " · Hero "
                + " ".join(x["hole"])
            )
            h += [
                "<details><summary>"
                + esc(heading)
                + (" · kontrola" if not x["active"] else "")
                + "</summary>",
                "<p>Odigrano <b>"
                + esc(x["played"])
                + "</b> · "
                + esc(x["guide"])
                + " · "
                + esc(a.get("alternative", ""))
                + "<br>"
                + esc(a.get("reason", ""))
                + "</p>",
            ]
            table_rows = []
            for r in v:
                src = "inputs/" if r["count"] == 500 else "validation-inputs/"
                comp = table(
                    ["River sastav pre akcije", "OOP %", "IP %"],
                    [
                        [
                            cat,
                            fmt(100 * r["model"]["oop"]["composition_full"][cat]),
                            fmt(100 * r["model"]["ip"]["composition_full"][cat]),
                        ]
                        for cat in GROUPS
                    ],
                )
                support = " / ".join(
                    str(r["model"][seat]["support_count"]) for seat in ("oop", "ip")
                )
                table_rows.append(
                    [
                        esc(r["opponent"] + " / " + r["hero"]),
                        esc(LABEL[r["verdict"]]),
                        fmt(r["delta"], 3),
                        fmt(r["equity"] * 100, 2) if r["equity"] is not None else "—",
                        "<details><summary>"
                        + esc(support)
                        + "</summary>"
                        + comp
                        + "</details>",
                        link(src + r["file"], "Ulaz")
                        + " · "
                        + link("spots/" + r["file"], "EV i strategija")
                        + " · "
                        + link(
                            "spots/" + Path(r["file"]).with_suffix(".txt").name, "Tekst"
                        ),
                    ]
                )
            h += [
                table(
                    [
                        "Protivnik / Hero",
                        "Nalaz",
                        "Prednost predloga BB",
                        "Hero equity posle linije %",
                        "Puna podrška OOP / IP i sastav",
                        "Detalji",
                    ],
                    table_rows,
                ),
                "</details>",
            ]
    h += ["<p>" + link("report.json", "Kompletan numerički izveštaj") + "</p></html>"]
    (out / "Izvestaj.html").write_text("\n".join(h), encoding="utf8")
    return data
