"""Produce a self-contained summary and a navigable report with links to every computed spot."""

import json
from collections import Counter, defaultdict
from html import escape
from statistics import median

from .model import digest
from .ranges import PROFILES

RULES = {
    "river_set_small_lead": (
        "Set protiv malog river leada",
        "Uporedi call sa odigranim foldom.",
        "Uz cenu pokaži koliko betova završava sa slabijom rukom, uz blokere i prethodni bet/call. Automatska osuda folda nema osnov.",
    ),
    "river_broadway_checked_to": (
        "Broadway posle tri checka",
        "Uporedi prikazani mali value bet sa checkom iza.",
        "Izdvoj prisustvo karte u boji, slabije straightove koji mogu platiti i moguća check-raise plaćanja. Pokaži check kada mali bet malo ili ništa ne dobija.",
    ),
    "river_check_back_value": (
        "Check iza sa jakom value rukom",
        "Proveri da li bar jedan dozvoljeni bet dobija više od checka.",
        "Zadrži pregled kada su slabiji callovi mogući. Veži jačinu preporuke za njihovu učestalost i snagu protivničkog check-rangea.",
    ),
    "river_check_back_top_two": (
        "Check iza sa dva para posle plaćenog turna",
        "Uporedi predloženi mali bet sa checkom.",
        "Traži slabija dva para koja plaćaju posle turn calla i odvoji promašene drawove koji folduju od setova koji nastavljaju.",
    ),
    "river_call_four_flush_board": (
        "Call bez blokera kada su četiri karte iste boje",
        "Uporedi fold sa odigranim callom kada su četiri karte iste boje.",
        "Prikaži cenu, potencijal pune kuće na uparenom bordu i protivničke betove bez boje. Odsustvo blokera samo po sebi je preslabo za preporuku folda.",
    ),
    "river_call_large_raise": (
        "Call velikog river raisea",
        "Uporedi fold sa odigranim callom.",
        "Prikaži cenu i koliko ruku za value i blef prolazi kroz isti bet–raise. Kada je raise redak ili procena nestabilna, ostavi ručni pregled.",
    ),
    "value_size_40": (
        "Value bet od 40% pota",
        "Uporedi konkretnih 40% sa checkom i svim ostalim dozvoljenim sizingima.",
        "Prikaži više veličina i njihove EV razlike. Potraži gde slabiji callovi nestaju kada veličina poraste.",
    ),
    "value_size_55": (
        "Value bet od 55% pota",
        "Uporedi konkretnih 55% sa checkom i svim ostalim dozvoljenim sizingima.",
        "Uz punu kuću i moguće slabije value ruke prikaži i veći sizing. Tačan procenat zaslužuje prednost samo uz razdvojene EV vrednosti.",
    ),
}
LABEL = {
    "supports": "za",
    "opposes": "protiv",
    "close": "blizu",
    "indeterminate": "neodređeno",
}
SETS = ("potvrđeno", "za reviziju", "zavisno ili neodređeno")


def classify(counts):
    total = sum(counts.values())
    yes, no = counts.get("supports", 0), counts.get("opposes", 0)
    if total and 5 * yes >= 4 * total and no == 0:
        return SETS[0]
    if no > yes and 5 * no >= 2 * total:
        return SETS[1]
    return SETS[2]


def count_text(counts):
    return " / ".join(str(counts.get(key, 0)) for key in LABEL)


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def en(s):
    return escape(str(s))


def display(x, n=2):
    return "—" if x is None else f"{x:.{n}f}".replace(".", ",")


def summary(case):
    s = case["input"]
    a = s["audit"]
    q = case["query"]
    c = case["comparison"]
    response = {}
    for bet, entries in q["opponent_reply_given_hero"].items():
        flat = entries.get("call", {})
        response[bet] = {
            "call": flat.get("frequency"),
            "weaker_share_of_call": flat.get("weaker_fraction"),
            "weaker_call_probability": (flat.get("frequency") or 0)
            * (flat.get("weaker_fraction") or 0),
            "raise": sum(
                v["frequency"] for k, v in entries.items() if k.startswith("raise")
            ),
        }
    return {
        "file": a["id"] + "-" + a["profile"],
        "profile": a["profile"],
        "verdict": c["verdict"],
        "delta_bb": c["delta_bb"],
        "path_probability": q["node_probability"],
        "hero_equity": q["hero_equity_given_path"],
        "flush_share": q["opponent_categories_given_hero_and_path"].get("flush", 0),
        "range_sha256": case["ranges_sha256"],
        "responses": response,
        "best_legal_action": c["best_legal_action"],
        "iterations": case["iterations"],
        "seconds": case["timing"]["elapsed_seconds"],
        "query_ev": q["hero_action_ev_bb"],
        "gap_pct": case["nashconv_pct_pot"],
        "exploitability_pct": case["exploitability_pct_pot"],
        "reliable": case["reliable"],
    }


def build(out):
    if (out / "inventory.json").is_file():
        from .round2_report import build as build_round_two

        return build_round_two(out)
    run = read(out / "run.json")
    measure = read(out / "gate-measurements.json")
    groups = defaultdict(dict)
    times = []
    unreliable = []
    worst = 0
    max_ex = 0
    for file in run["files"]:
        r = read(out / "spots" / file)
        spec = r["input"]
        a = spec["audit"]
        assert read(out / "inputs" / file) == spec and r["ranges_sha256"] == digest(
            spec["ranges"]
        )
        assert abs(sum(r["game_ev_bb"]) - spec["pot_bb"]) < 1e-7
        result = summary(r)
        times.append(result["seconds"])
        if not r["reliable"]:
            unreliable.append(file)
        worst = max(worst, result["gap_pct"])
        max_ex = max(max_ex, result["exploitability_pct"])
        row = groups[a["family"]].setdefault(
            a["id"],
            {
                "id": a["id"],
                "source": a["source"],
                "source_hand": a["source_hand"],
                "room": a["room"],
                "board": spec["board"],
                "hole": spec["query"]["hero"],
                "path": spec["query"]["path"],
                "pot": spec["pot_bb"],
                "variant": a["variant"],
                "rule_active": a["rule_active"],
                "app": a["app"],
                "profiles": {},
            },
        )
        row["profiles"][a["profile"]] = result
    group_list = []
    buckets = {s: [] for s in SETS}
    for family, (title, description, proposal) in RULES.items():
        group = sorted(groups[family].values(), key=lambda x: x["variant"])
        counts = Counter()
        flips = fully = with_support = with_opp = 0
        eligible = sum(v["rule_active"] for v in group)
        for v in group:
            assert set(v["profiles"]) == set(PROFILES)
            verdicts = {r["verdict"] for r in v["profiles"].values()}
            v["flip"] = "supports" in verdicts and "opposes" in verdicts
            if v["rule_active"]:
                counts.update(r["verdict"] for r in v["profiles"].values())
                flips += v["flip"]
                fully += verdicts == {"supports"}
                with_support += "supports" in verdicts
                with_opp += "opposes" in verdicts
        assert eligible >= 10
        bucket = classify(counts)
        buckets[bucket].append(family)
        group_list.append(
            {
                "family": family,
                "title": title,
                "description": description,
                "proposal": proposal,
                "variants": group,
                "eligible": eligible,
                "controls": len(group) - eligible,
                "counts": dict(counts),
                "flips": flips,
                "all_three_support": fully,
                "any_support": with_support,
                "any_oppose": with_opp,
            }
        )
    roll = {
        "app_version": __import__("leakfinder").__version__,
        "cpu": run["cpu"],
        "workers": run["parallel_workers"],
        "threads_per_solve": run["cpu_threads_per_solve"],
        "timed_solves": len(times),
        "variant_count": sum(len(g) for g in groups.values()),
        "active_variants": sum(x["eligible"] for x in group_list),
        "profiles": PROFILES,
        "per_solve_median_seconds": median(times),
        "min_seconds": min(times),
        "max_seconds": max(times),
        "sum_solver_seconds": sum(times),
        "batch_wall_seconds": run["solve_batch_wall_seconds"],
        "end_to_end_seconds": run["end_to_end_seconds"],
        "worst_gap_pct": worst,
        "worst_exploitability_pct": max_ex,
        "unreliable": unreliable,
        "monker": run["monker"],
        "thresholds": {
            "requested_exploitability_pct": 0.1,
            "coarse_sum_of_gains_pct": 0.04,
            "operational_sum_of_gains_pct": 0.012,
            "equivalent_action_pct": 0.5,
            "min_public_reach_pct": 0.1,
            "min_opponent_reach_given_hero_pct": 1,
        },
        "tests": read(out / "tests.json"),
        "test_measurements": measure,
        "lists": buckets,
        "groups": group_list,
        "excluded": read(out / "out-of-scope.json"),
    }
    (out / "report.json").write_text(
        json.dumps(roll, indent=1, ensure_ascii=False), encoding="utf8"
    )
    html = [
        '<!doctype html><html lang="sr-Latn"><meta charset="utf-8"><title>PLO4 river · revizija</title>',
        "<style>body{font:16px/1.48 Segoe UI,Arial;color:#183044;max-width:1250px;margin:36px auto;padding:0 18px}h1,h2{line-height:1.15}table{border-collapse:collapse;width:100%;margin:1em 0 1.5em}th,td{text-align:left;vertical-align:top;padding:7px;border-bottom:1px solid #ccd6dc}th{background:#e8eff3}a{color:#125987}small{color:#415667}summary{cursor:pointer}section{margin-top:2.5rem}.warn{background:#fff1c6;padding:12px}</style><h1>Revizija river pravila za PLO4</h1>",
    ]

    def p(s):
        html.append("<p>" + s + "</p>")

    if unreliable:
        p(
            '<strong class="warn">Numerički cilj nije dostignut za ove spotove: '
            + en(", ".join(unreliable))
            + "</strong>"
        )
    for bucket, h in zip(
        SETS,
        (
            "Potvrđeno u najmanje 80% aktivnih rešenja",
            "Pravila za reviziju u testiranim modelima",
            "Odluke koje zavise od opsega ili ostaju neodređene",
        ),
    ):
        html.append("<h2>" + h + "</h2>")
        html.append(
            "<ul>"
            + (
                "".join(
                    '<li><a href="#'
                    + f
                    + '">'
                    + en(RULES[f][0])
                    + "</a>"
                    + " · za / protiv / blizu / neodređeno "
                    + count_text(
                        next(g["counts"] for g in group_list if g["family"] == f)
                    )
                    + (" · " + en(RULES[f][2]) if bucket == SETS[1] else "")
                    + "</li>"
                    for f in buckets[bucket]
                )
                or "<li>Nijedno.</li>"
            )
            + "</ul>"
        )
    p(
        "Potvrđeno zahteva najmanje 80% za i nula protiv. Za ispravku zahteva više protiv nego za, uz najmanje 40% protiv. Imenilac uključuje sva aktivna rešenja, uključujući bliska i neodređena."
    )
    html.append("<h2>Obuhvat i preciznost</h2>")
    p(
        f"Provereno je {roll['variant_count']} varijanti i {len(times)} rešenja. Prilikom brojanja slaganja uključeno je {roll['active_variants']} varijanti na kojima je aplikacija zaista aktivirala pravilo. Kontrolne varijante dobile su zasebna rešenja. Za svaki model dostupni su ulaz, potpuni ponderisani opsezi, izvorni izlaz aplikacije, EV akcija, odgovor protivnika, mešavine po klasama i trajanje. <a href='report.json'>Podaci iz izveštaja</a>."
    )
    p(
        f"Najviša exploitability iznosi {display(max_ex, 5)}% pota. Ona je polovina zbira jednostranih dobitaka iz najboljeg odgovora oba igrača. Najviši takav zbir je {display(worst, 5)}% pota. Svaki ulaz rešen je prvo do 0,04% i zatim uz cilj 0,012% za ceo zbir. Promena kategorije pri preciznijem računu daje neodređen nalaz. Poređenje koristi prag od 0,5% početnog pota za praktično bliske EV. Taj prag je izbor za pregled. Globalna greška ravnoteže nema značenje intervala poverenja za pojedinu retku ruku."
    )
    p(
        "Na svakom početku rivera igraju dva igrača bez rakea, sa tačnim uklanjanjem karata i svim kombinacijama iz priloženih opsega. Opsezi su namerno ograničene porodice četvorokartnih ruku sa eksplicitnim težinama. Nema uzorkovanja ili grupisanja pri rešavanju. Grupe u prikazu računaju se tek posle rešenja. Hero kroz tri izračunavanja zadržava identičan range. Za protivnika se menja relativna težina value ruku, drawova i slabijih dolazaka posle prethodne akcije. Nijedna frekvencija nije izmerena na konkretnom protivniku."
    )
    p(
        "Model dopušta check, betove definisane u ulazu, najviše dva raisea i all-in kad je legalan. Novac je u BB od početka rivera. Kada su raniji čipovi već uloženi u toj ulici, fold može imati negativan prikazani EV. Razlike akcija u istom čvoru ostaju uporedive. Neodređeno označava nizak doseg čvora, nedovoljan numerički rezultat ili promenu zaključka kada Hero optimizuje nastavak protiv fiksirane strategije. Posebno su dostupni EV prosečne politike i EV najboljeg nastavka."
    )
    p(
        f"CPU {en(run['cpu'])}. Jedna numerička nit po rešenju, {roll['workers']} nezavisnih radnika. Medijana {display(median(times), 3)} s, raspon {display(min(times), 3)}–{display(max(times), 3)} s. Zbir trajanja pojedinačnih rešenja {display(sum(times), 2)} s, zidno vreme paralelnog rešavanja i pisanja rezultata {display(roll['batch_wall_seconds'], 2)} s. Od početka validacije do završetka računanja {display(roll['end_to_end_seconds'], 2)} s."
    )
    p(en(run["monker"]))
    html.append(
        "<h2>Nezavisne provere pre analize</h2><table><tr><th>Bet / pot</th><th>Udeo blefova · teorija / izmereno</th><th>Call · teorija / izmereno</th></tr>"
    )
    for x in measure["polar"]:
        html.append(
            "<tr>"
            + "".join(
                "<td>" + s + "</td>"
                for s in (
                    display(x["fraction"]),
                    display(100 * x["expected_bluff_share"], 3)
                    + " / "
                    + display(100 * x["actual_bluff_share"], 3)
                    + "%",
                    display(100 * x["expected_call"], 3)
                    + " / "
                    + display(100 * x["actual_call"], 3)
                    + "%",
                )
            )
            + "</tr>"
        )
    html.append("</table>")
    lp = measure["lp"]
    p(
        f"Sequence-form LP na nezavisno izgrađenom stablu sa raiseom dao je {display(lp['lp_oop_bb'], 7)} BB za OOP. CFR+ daje {display(lp['cfr_oop_bb'], 7)} BB. Odstupanje je {display(lp['absolute_difference_pct_pot'], 6)}% pota. Prošli su i testovi ponderisanih blokera, očuvanja novca, determinizma i legalnog all-ina. <a href='tests.json'>Pokretanje testova</a>."
    )
    for g in group_list:
        f = g["family"]
        html.append(
            '<section id="'
            + f
            + '"><h2>'
            + en(g["title"])
            + "</h2><small>"
            + f
            + "</small>"
        )
        p(en(g["description"]) + " " + en(g["proposal"]))
        c = g["counts"]
        p(
            f"U {g['eligible']} aktivnih varijanti: jasna prednost u sva tri opsega u {g['all_three_support']}; promena smera između opsega u {g['flips']}. Po rešenjima: za {c.get('supports', 0)}, protiv {c.get('opposes', 0)}, blizu {c.get('close', 0)}, neodređeno {c.get('indeterminate', 0)}. Kontrole van uslova pravila: {g['controls']}."
        )
        p(
            "U ćeliji je prednost predložene akcije u BB. Za value sizing upoređuje se sa najboljom drugom dozvoljenom akcijom. Klik otvara kratak tekst i potpuni JSON. Poslednja kolona pokazuje promenu sastava ili odgovora protivničkog opsega od užeg ka širem, pod istom prethodnom akcijom."
        )
        html.append(
            "<table><tr><th>Varijanta i bord</th><th>Uži</th><th>Srednji</th><th>Širi</th><th>Šta se menja</th></tr>"
        )
        for v in g["variants"]:
            rr = [v["profiles"][k] for k in PROFILES]
            header = (
                "Demo #" + str(int(v["source_hand"]) % 100000)
                if v["source"] == "demo"
                else "Varijanta " + str(v["variant"])
            )
            app = v["app"]
            critique = app["critique"] or {}
            recorded = (
                en(app["guide"] or "Study only") + "<br>" + en(app["explanation"])
            )
            if critique:
                recorded += (
                    "<br><b>"
                    + en(critique["alternative"])
                    + "</b> "
                    + en(critique["reason"])
                    + " "
                    + en(critique["counterargument"])
                )
            html.append(
                "<tr><td><b>"
                + en(header)
                + "</b>"
                + ("" if v["rule_active"] else " · kontrola")
                + "<br>"
                + en(" ".join(v["board"]))
                + "<br><small>"
                + en(" ".join(v["hole"]))
                + "</small><details><summary>Izlaz aplikacije</summary>Played "
                + en(app["played"])
                + " · "
                + en(app["ref"])
                + "<br>"
                + recorded
                + "</details></td>"
            )
            for x in rr:
                html.append(
                    "<td>"
                    + LABEL[x["verdict"]]
                    + " "
                    + display(x["delta_bb"])
                    + "<br><small>Najveći procenjeni EV: "
                    + en(x["best_legal_action"] or "—")
                    + "<br><a href='inputs/"
                    + x["file"]
                    + ".json'>Ulaz</a> · <a href='spots/"
                    + x["file"]
                    + ".txt'>Tekst</a> · <a href='spots/"
                    + x["file"]
                    + ".json'>JSON</a></small></td>"
                )
            first = rr[0]
            action = next((a for a in first["responses"] if a.startswith("bet")), None)
            if action and v["app"]["size_bb"]:
                from .model import label

                target = label("bet", v["app"]["size_bb"])
                if target in first["responses"]:
                    action = target
            if action:
                explanation = (
                    f"Slabiji call na {action} BB (uži / srednji / širi) "
                    + " / ".join(
                        display(
                            100 * x["responses"][action]["weaker_call_probability"], 1
                        )
                        + "%"
                        for x in rr
                    )
                    + "."
                )
            else:
                explanation = (
                    "Equity Heroa protiv ovog beta/raisea (uži / srednji / širi) "
                    + " / ".join(display(100 * x["hero_equity"], 1) + "%" for x in rr)
                    + "."
                    if all(x["hero_equity"] is not None for x in rr)
                    else "Čvor nema dostižan odgovor."
                )
            if v["flip"]:
                explanation += " Smer odluke se okreće."
            elif any(x["verdict"] == "indeterminate" for x in rr):
                explanation += (
                    " Barem jedan opseg ne daje dovoljno pouzdan pojedinačni zaključak."
                )
            else:
                explanation += " Nema jasne promene smera između tri opsega."
            html.append("<td>" + en(explanation) + "</td></tr>")
        html.append("</table></section>")
    item = roll["excluded"]
    p(
        f"Demo #{int(item['id']) % 100000} ima {item['live']} živa igrača na riveru. Ruka je van opsega; nema zaključka o betu ili checku. <a href='out-of-scope.json'>Zabeleženi izlaz</a>."
    )
    (out / "Izvestaj.html").write_text("\n".join(html) + "</html>", encoding="utf8")
