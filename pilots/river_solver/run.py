"""python -m pilots.river_solver.run --out PATH [--workers N]; each solve uses one BLAS thread."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

for name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[name] = "1"


def dump(path, data):
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(
        json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False), encoding="utf8"
    )
    staging.replace(path)


def handle(source):
    from .solver import solve

    spec = json.loads(source.read_text(encoding="utf8"))
    coarse, prior_time = solve(spec, target_pct=0.04, iterations=12000)
    result, timing = solve(
        spec,
        target_pct=0.012,
        iterations=20000,
        minimum_iterations=coarse["iterations"] + 100,
    )
    timing["fine_seconds"] = timing["elapsed_seconds"]
    timing["coarse_seconds"] = prior_time["elapsed_seconds"]
    timing["elapsed_seconds"] += prior_time["elapsed_seconds"]
    timing["cpu_threads"] = 1
    compare(spec, coarse, result)
    result["timing"] = timing
    path = source.parent.parent / "spots" / source.name
    dump(path, result)
    write_spot_text(path, result)
    return source.name, result["reliable"]


def compare(spec, coarse, result):
    a = spec["audit"]
    q = result["query"]
    pot = spec["pot_bb"]
    canonical = q["hero_ev_with_best_continuation_vs_fixed_opponent_bb"]
    base = q["hero_action_ev_bb"]
    family = a["family"]
    if family in ("river_call_large_raise", "river_call_four_flush_board"):
        alternative = ["fold"]
    elif family == "river_set_small_lead":
        alternative = ["call"]
    elif family.startswith("value_size"):
        from .model import label

        alternative = [
            label(
                "bet",
                min(spec["effective_stack_bb"], max(1, pot * int(family[-2:]) / 100)),
            )
        ]
    else:
        alternative = [k for k in base if k.startswith("bet")]
    if family == "river_broadway_checked_to" or family == "river_check_back_top_two":
        from .model import label

        if a["app"]["size_bb"]:
            alternative = [label("bet", a["app"]["size_bb"])]
    alternative = [x for x in alternative if x in base]
    other = (
        [k for k in base if k not in alternative]
        if family.startswith("value_size")
        else ["fold"]
        if family == "river_set_small_lead"
        else ["call"]
        if alternative == ["fold"]
        else ["check"]
    )
    other = [k for k in other if k in base]
    threshold = 0.005 * pot

    def comparison(ev):
        return (
            max(ev[k] for k in alternative) - max(ev[k] for k in other)
            if alternative and other
            else None
        )

    delta = comparison(base)
    delta_best = comparison(canonical)

    def direction(v):
        if v is None:
            return "indeterminate"
        return "supports" if v > threshold else "opposes" if v < -threshold else "close"

    # A small global gap does not certify the EV of an individual low-reach information set.
    status = direction(delta)
    before = coarse["query"]["hero_action_ev_bb"]
    coarse_delta = (
        comparison(before) if all(k in before for k in alternative + other) else None
    )
    stable = direction(coarse_delta) == status
    if (
        not result["reliable"]
        or q["opponent_path_probability_given_hero"] < 0.01
        or q["node_probability"] < 0.001
        or direction(delta_best) != status
        or not stable
    ):
        status = "indeterminate"
    result["comparison"] = {
        "candidate": alternative,
        "other": other,
        "delta_bb": delta,
        "best_continuation_delta_bb": delta_best,
        "coarse_delta_bb": coarse_delta,
        "stable_across_refinement": stable,
        "coarse_iterations": coarse["iterations"],
        "coarse_gap_pct": coarse["nashconv_pct_pot"],
        "best_legal_action": max(base, key=base.get) if base else None,
        "verdict": status,
        "indifference_bb": threshold,
    }
    return result["comparison"]


def write_spot_text(path, result):
    spec = result["input"]
    a, query, comparison = spec.get("audit"), result["query"], result.get("comparison")
    title = (
        f"{a['id']} / {a['profile']} | rule active: {a['rule_active']}"
        if a
        else path.stem
    )
    assessment = (
        f"{comparison['verdict']} | delta {comparison['delta_bb']} BB | "
        if comparison
        else ""
    )
    reliability = "Converged" if result["reliable"] else "UNRELIABLE"
    actions = ", ".join(
        f"{k} {v:.4f} BB" for k, v in query["hero_action_ev_bb"].items()
    )
    groups = [
        name
        + " | "
        + ", ".join(
            f"{action} {probability:.2%}" for action, probability in frequencies.items()
        )
        for name, frequencies in query["range_strategy_by_made_hand"].items()
    ]
    path.with_suffix(".txt").write_text(
        f"{title} | {reliability}\n"
        f"Hero {' '.join(spec['query']['hero'])} | board {' '.join(spec['board'])} | pot {spec['pot_bb']} BB | eff. {spec['effective_stack_bb']} BB | path {spec['query']['path']}\n"
        f"{actions}\n{assessment}exploitability {result['exploitability_pct_pot']:.6f}% pot"
        f" | sum of BR gains {result['nashconv_pct_pot']:.6f}% | {result['iterations']} iterations"
        f" | {result['timing']['elapsed_seconds']:.3f} s, one CPU thread.\n"
        f"Tree {json.dumps(spec['tree'])}\n"
        f"Weighted four-card ranges in {path.name}, input.ranges | SHA-256 {result['ranges_sha256']}\n"
        f"Acting player's conditional strategy by made hand\n"
        + "\n".join(groups)
        + "\n",
        encoding="utf8",
    )


def main():
    """Run the full public benchmark; personal-library evidence is an explicit extra input."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--previous", type=Path, help="First-round folder or frozen round-two manifest"
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--library", type=Path, help="Optional private SQLite library, read-only"
    )
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    run_gate(out)
    for phase in ("prepare", "solve", "refine"):
        cmd = [
            sys.executable,
            "-B",
            "-m",
            "pilots.river_solver.round2",
            phase,
            str(out),
            "--workers",
            str(args.workers),
        ]
        if args.previous:
            cmd += ["--previous", str(args.previous)]
        subprocess.run(cmd, check=True)
    if args.library:
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "pilots.river_solver.population",
                str(args.library),
                str(out / "population.json"),
            ],
            check=True,
        )
    from .round2 import LP_IDS

    samples = [
        str(out / "validation-inputs" / (case + "-200-middle-middle.json"))
        for case in sorted(LP_IDS)
    ]
    samples.extend(
        str(out / "inputs" / (case + "-500-middle-middle.json"))
        for case in sorted(LP_IDS)
    )
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pilots.river_solver.lp_check",
            *samples,
            "--production",
            str(out / "spots"),
            "--output",
            str(out / "lp.json"),
        ],
        check=True,
    )
    from .round2_report import build

    build(out)


def run_gate(out):
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_river_solver*.py",
        "-v",
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "RIVER_GATE_METRICS": str(out / "gate-measurements.json")},
    )
    log = res.stdout + res.stderr
    dump(out / "tests.json", {"returncode": res.returncode, "output": log})
    if res.returncode or "skipped=" in log:
        raise RuntimeError(log)


if __name__ == "__main__":
    main()
