"""Round-three calibration validation, coefficient grid and reproducible solver batch."""

import argparse
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from itertools import product
from pathlib import Path
from types import SimpleNamespace

from .model import GROUPS, label
from .ranges import PROFILES, seat_ranges
from .round2 import cpu_name, read
from .run import compare, dump

CONTROLS = {
    "river_call_four_flush_board-00",
    "river_call_large_raise-00",
    "river_set_small_lead-00",
}
RESOLUTIONS = (500, 1500, 2250, 4000)
CORE = ("supports", "opposes")
MATERIAL = 0.005


def arrival(
    spec, history, names, parameters, counts, stratified=True, profiles=PROFILES
):
    hero = spec["query"]["hero"]
    return [
        seat_ranges(
            spec["board"],
            hero if seat == spec["hero_seat"] else (),
            history,
            name,
            counts,
            parameters=parameters,
            reference_cards=hero,
            stratify=stratified,
            profiles=profiles,
        )
        for seat, name in enumerate(names)
    ]


def put_ranges(spec, ranges, count, profiles):
    rows = {k: ranges[i][count][profiles[i]] for i, k in enumerate(("oop", "ip"))}
    return {
        **spec,
        "ranges": {k: v["entries"] for k, v in rows.items()},
        "range_model": {k: v["metadata"] for k, v in rows.items()},
    }


def solved(spec, *, validation_path=None, quick=False):
    from unittest.mock import patch

    from . import solver

    if validation_path is not None:
        original = solver.describe

        def public(game, spec):
            value = original(game, spec)
            node = game.root
            reach = [w.copy() for w in game.weights]
            for action in validation_path:
                order = list(node.children).index(action)
                reach[node.seat] *= node.policy[order]
                node = node.children[action]
            seat = spec["bettor_seat"]
            mass = (game.legal[seat] @ reach[1 - seat]) * reach[seat]
            total = float(mass.sum())
            value["called_path_reach"] = total / game.mass
            categories = Counter()
            for rank, w in zip(game.ranks[seat], mass, strict=True):
                categories[GROUPS[rank[0]] if rank[0] < 6 else "full house+"] += float(
                    w
                )
            value["called_bettor_composition"] = (
                {k: w / total for k, w in categories.items()} if total > 0 else {}
            )
            return value

        with patch.object(solver, "describe", public):
            r, t = solver.solve(spec, iterations=5500, target_pct=0.085)
        r["timing"] = t
        return r
    if quick:
        result, timing = solver.solve(spec, iterations=3500, target_pct=0.095)
        compare(spec, result, result)
    else:
        coarse, first = solver.solve(spec, iterations=3500, target_pct=0.095)
        result, timing = solver.solve(
            spec,
            iterations=9000,
            target_pct=0.04,
            minimum_iterations=coarse["iterations"] + 100,
        )
        timing["elapsed_seconds"] += first["elapsed_seconds"]
        compare(spec, coarse, result)
    result["timing"] = timing
    q, c = result["query"], result["comparison"]
    rare = (
        q["opponent_path_probability_given_hero"] < 0.01 or q["node_probability"] < 0.01
    )
    result["assessment"] = (
        "rare"
        if rare
        else c["verdict"]
        if c["verdict"] != "indeterminate"
        else "unstable"
    )
    return result


def solution_summary(result):
    c = result["comparison"]
    return {
        "verdict": result["assessment"],
        "delta_bb": c["delta_bb"],
        "coarse_delta_bb": c["coarse_delta_bb"],
        "coarse_stable": c["stable_across_refinement"],
        "best_continuation_delta_bb": c["best_continuation_delta_bb"],
        "best_action": c["best_legal_action"],
        "exploitability_pct_pot": result["exploitability_pct_pot"],
        "converged_game": result["reliable"],
        "root_bb": result["game_ev_bb"],
        "iterations": result["iterations"],
        "seconds": result["timing"]["elapsed_seconds"],
        "opponent_reach": result["query"]["opponent_path_probability_given_hero"],
        "public_reach": result["query"]["node_probability"],
        "hero_equity": result["query"]["hero_equity_given_path"],
    }


def baseline_job(job):
    from .cases import case

    reference, prior, record, parameters, out, phase = job
    key = reference["audit"]["id"]
    _, _, live = record
    assert live is not None
    snap, fresh, meta = case(
        reference["audit"]["family"],
        int(reference["audit"]["source_hand"]) % 100000,
        reference["audit"]["variant"],
        record,
    )
    assert fresh["board"] == reference["board"] and fresh["query"] == reference["query"]
    default = {
        k: reference[k]
        for k in ("board", "tree", "pot_bb", "effective_stack_bb", "query")
    }
    hero = 1 - meta["opponent_seat"]
    default.update(
        {"hero_seat": hero, "audit": {**prior["audit"], "round3_app": meta["app"]}}
    )
    seats = [next(s.name for s in snap.seats if s.live and not s.is_hero)] * 2
    seats[hero] = snap.hero
    pairs = (
        list(product(PROFILES, repeat=2))
        if meta["source"] == "demo"
        else list(zip(PROFILES, PROFILES))
    )
    tag = "series" if phase == "series" else "convergence"
    for op, h in pairs:
        sizes = (
            (500,)
            if phase == "series"
            else followups(out, key, op, h, reference["pot_bb"])
        )
        if not sizes:
            continue
        profiles = [op, op]
        profiles[hero] = h
        rows = arrival(
            default,
            snap.history,
            seats,
            parameters,
            sizes,
            profiles=tuple(dict.fromkeys(profiles)),
        )
        for n in sizes:
            file = out / tag / f"{key}-{n}-{op}-{h}.json"
            if file.exists():
                continue
            spec = put_ranges(default, rows, n, profiles)
            spec["audit"] = {
                **default["audit"],
                "representatives": n,
                "profile": op + "-" + h,
                "opponent_profile": op,
                "hero_profile": h,
            }
            result = solved(spec)
            dump(file, result)
    if phase == "convergence" and key == "river_call_four_flush_board-00":
        profiles = ("middle", "middle")
        for n in RESOLUTIONS:
            file = out / tag / f"{key}-{n}-old-grouping.json"
            if file.exists():
                continue
            rows = arrival(
                default,
                snap.history,
                seats,
                parameters,
                (n,),
                stratified=False,
                profiles=("middle",),
            )
            spec = put_ranges(default, rows, n, profiles)
            dump(file, solved(spec))
    if phase == "convergence" and key in CONTROLS:
        for n in RESOLUTIONS:
            folder = "series" if n == 500 else tag
            file = out / folder / f"{key}-{n}-middle-middle.json"
            if not file.exists():
                raise RuntimeError(file.name)
    return key


def followups(out, key, op, h, pot):
    series = read(out / "series" / f"{key}-500-{op}-{h}.json")
    comparison = series["comparison"]
    # A change of material direction at the tighter numerical target
    # triggers the same representation ladder as the three pilot hands.
    return (
        RESOLUTIONS[1:]
        if (key in CONTROLS and op == h == "middle")
        or not comparison["stable_across_refinement"]
        else ()
    )


def sensitivity_grid(calibration):
    grid = {}
    for key in (
        "continue_intercept",
        "continue_strength_slope",
        "continue_draw_slope",
        "enter_intercept",
    ):
        section = "preflop" if key == "enter_intercept" else "postflop"
        base = calibration["calibrated_parameters"][section][key]
        grid[section + "." + key] = (base - 0.5, base + 0.5)
    for item in calibration["fitted"].values():
        for name, c in item["coefficients"].items():
            grid[item["section"] + "." + name] = tuple(c["ci95"])
    return grid


def sensitivity_tasks(manifest, old, records, calibration, out, per_family):
    selected = {}
    for family in sorted({x["audit"]["family"] for x in manifest}):
        eligible = [
            x
            for x in manifest
            if x["audit"]["family"] == family and x["audit"]["rule_active"]
        ]
        demo = [
            s for s in eligible if old[s["audit"]["id"]]["audit"]["source"] == "demo"
        ]
        rest = [s for s in eligible if s not in demo]
        import hashlib

        rest.sort(key=lambda x: hashlib.sha256(x["audit"]["id"].encode()).digest())
        selected[family] = demo + rest[:per_family]
    grid = sensitivity_grid(calibration)
    dump(
        out / "sensitivity-design.json",
        {
            "variants": {
                f: [s["audit"]["id"] for s in ss] for f, ss in selected.items()
            },
            "grid": grid,
            "profiles": ["middle", "middle"],
            "representatives": 500,
            "selection": "All active demo variants and four non-demo variants per rule ordered by SHA-256 of variant ID. One coefficient at a time.",
        },
    )
    return [
        (
            ref,
            records[int(ref["audit"]["source_hand"]) % 100000],
            old[ref["audit"]["id"]],
            out,
            calibration["calibrated_parameters"],
            k,
            d,
            v,
        )
        for refs in selected.values()
        for ref in refs
        for k, bounds in grid.items()
        for d, v in zip(("lower", "upper"), bounds, strict=True)
    ]


def sensitivity_job(job):
    from .cases import case

    ref, record, prior, out, base, key, direction, value = job
    audit = ref["audit"]
    file = out / "grid" / f"{audit['id']}-{key}-{direction}.json"
    if file.exists():
        return file.name
    params = deepcopy(base)
    section, name = key.split(".")
    params[section][name] = value
    snap, _, meta = case(
        audit["family"], int(audit["source_hand"]) % 100000, audit["variant"], record
    )
    hero = 1 - meta["opponent_seat"]
    names = [next(s.name for s in snap.seats if s.live and not s.is_hero)] * 2
    names[hero] = snap.hero
    spec = {
        **{
            k: ref[k]
            for k in ("board", "query", "pot_bb", "tree", "effective_stack_bb")
        },
        "hero_seat": hero,
        "audit": prior["audit"],
    }
    rows = arrival(spec, snap.history, names, params, (500,), profiles=("middle",))
    spec = put_ranges(spec, rows, 500, ("middle", "middle"))
    r = solved(spec, quick=True)
    s = solution_summary(r)
    # Recreate any input from the frozen case, coefficient and deterministic grouping.
    s.update(
        {
            "variant": audit["id"],
            "coefficient": key,
            "direction": direction,
            "value": value,
            "input_sha256": r["input_sha256"],
            "range_sha256": r["ranges_sha256"],
        }
    )
    dump(file, s)
    return file.name


def validation_job(task):
    family, sample, parameters, out = task
    token = sample["reference"]
    file = out / "validation" / f"{family}-{token}.json"
    if file.exists():
        return file.name
    p, cap = sample["pot_bb"], sample["effective_stack_bb"]
    plan = sample["river"]
    bets = {0.33, 0.66, 1.0}
    raises = {1.0}
    total = {"OOP": 0.0, "IP": 0.0}
    path = []
    for event in plan:
        move, seat = event["action"], event["seat"]
        if move == "check":
            path.append("check")
        if move == "bet":
            total[seat] = min(cap, event["amount_bb"])
            bets.add(min(1, total[seat] / p))
            path.append(label("bet", total[seat]))
        if move == "raise":
            previous = max(total.values())
            total[seat] = min(cap, total[seat] + event["amount_bb"])
            frac = (total[seat] - previous) / (p + 2 * previous)
            if frac > 0:
                raises.add(min(1, frac))
            path.append(label("raise", total[seat]))
        if move == "call":
            path.append("call")
    spec = {
        "board": sample["board"],
        "pot_bb": p,
        "effective_stack_bb": cap,
        "tree": {
            "bet_fractions": sorted(bets),
            "raise_fractions": sorted(raises),
            "max_raises": max(2, sum(a.startswith("raise") for a in path)),
            "all_in": True,
            "min_bet_bb": 1,
        },
        "river_path_to_call": path,
        "bettor_seat": int(sample["bettor"] == "IP"),
        "query": {"path": []},
        "audit": {"family": family, "selection_reference": token},
    }
    history = [SimpleNamespace(**e) for e in sample["history"]]
    rows = [
        seat_ranges(
            spec["board"],
            (),
            history,
            seat,
            (500,),
            parameters=parameters,
            stratify=True,
            profiles=("middle",),
        )
        for seat in ("OOP", "IP")
    ]
    spec = put_ranges(spec, rows, 500, ("middle", "middle"))
    spec["query"]["hero"] = spec["ranges"]["oop"][0]["cards"]
    from .model import Game, Leaf

    node = Game(spec).root
    for action in path:
        if isinstance(node, Leaf) or action not in node.children:
            dump(
                file,
                {
                    "excluded": "Recorded path is outside the equal-effective-stack tree",
                    "path": path,
                    "board": spec["board"],
                    "family": family,
                    "input": spec,
                },
            )
            return file.name
        node = node.children[action]
    dump(file, solved(spec, validation_path=path))
    return file.name


def lp_job(task):
    from .cases import case
    from .lp_check import check

    reference, prior, record, parameters, out, n = task
    audit = reference["audit"]
    file = out / "lp" / f"{audit['id']}-{n}.json"
    if file.exists():
        return file.name
    s, _, meta = case(
        audit["family"], int(audit["source_hand"]) % 100000, audit["variant"], record
    )
    hero = 1 - meta["opponent_seat"]
    names = [next(p.name for p in s.seats if p.live and not p.is_hero)] * 2
    names[hero] = s.hero
    base = {
        **{
            k: reference[k]
            for k in ("board", "query", "pot_bb", "tree", "effective_stack_bb")
        },
        "hero_seat": hero,
        "audit": prior["audit"],
    }
    spec = put_ranges(
        base,
        arrival(base, s.history, names, parameters, (n,), profiles=("middle",)),
        n,
        ("middle", "middle"),
    )
    result = check(spec, threads=1, target_pct=0.008)
    result["passed"] &= result["absolute_difference_pct_pot"] < 0.01
    dump(file, {**result, "input": spec})
    return file.name


def run_pool(fn, jobs, workers, outputs):
    start = time.perf_counter()
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(fn, x) for x in jobs]
        for n, f in enumerate(as_completed(futures), 1):
            name = f.result()
            if n % 20 == 0 or n == len(futures):
                print(fn.__name__, n, "/", len(futures), name, flush=True)
    timing={"jobs":len(jobs),"wall_seconds":time.perf_counter()-start,"workers":workers,"cpu":cpu_name()}
    if outputs.name=="convergence-timing.json":
        series_stamp=(outputs.parent/"series-timing.json").stat().st_mtime
        last=max(p.stat().st_mtime for p in (outputs.parent/"convergence").glob("*.json"))
        timing["series_end_to_last_resolution_seconds"]=last-series_stamp
    dump(outputs,timing)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("previous", type=Path)
    parser.add_argument(
        "phase", choices=("series", "grid", "convergence", "validation", "lp")
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    out = args.out.resolve()
    calibration = read(out / "calibration.json")
    (out / args.phase).mkdir(exist_ok=True)
    params = calibration["calibrated_parameters"]
    if args.phase == "validation":
        jobs = [
            (k, item, params, out)
            for k, items in read(out / "validation-sample.json").items()
            for item in items
        ]
        run_pool(validation_job, jobs, args.workers, out / "validation-timing.json")
        return
    from .cases import read_demo

    demo = read_demo()
    old = {
        x["audit"]["id"]: x
        for path in args.previous.joinpath("inputs").glob("*-500-middle-middle.json")
        for x in (read(path),)
    }
    manifest = read(Path(__file__).with_name("variant_manifest.json"))
    if args.phase == "lp":
        from .round2 import LP_IDS

        jobs = [
            (
                s,
                old[s["audit"]["id"]],
                demo[int(s["audit"]["source_hand"]) % 100000],
                params,
                out,
                n,
            )
            for s in manifest
            if s["audit"]["id"] in LP_IDS
            for n in (200, 500)
        ]
        run_pool(lp_job, jobs, args.workers, out / "lp-timing.json")
        return
    if args.phase == "grid":
        jobs = sensitivity_tasks(manifest, old, demo, calibration, out, 4)
        run_pool(sensitivity_job, jobs, args.workers, out / "grid-timing.json")
        return
    manifest.sort(key=lambda s: (s["audit"]["id"] not in CONTROLS, s["audit"]["id"]))
    jobs = [
        (
            s,
            old[s["audit"]["id"]],
            demo[int(s["audit"]["source_hand"]) % 100000],
            params,
            out,
            args.phase,
        )
        for s in manifest
    ]
    run_pool(baseline_job, jobs, args.workers, out / f"{args.phase}-timing.json")


if __name__ == "__main__":
    main()
