"""Run paired CPU simulations on predecision snapshots, then join the observed actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import NormalDist

HERE = Path(__file__).resolve().parent
SEEDS = (48371, 83219, 260003)
# Each row changes one assumption. Cutoffs refer to all PLO4 combos on the
# model's own strength ordering, including hands that do not reach the node.
BASE = {
    "open-pct": 32,
    "co-continue": 18,
    "blind-scale": 1,
    "postflop": "simple",
    "rake": 0.05,
    "cap": 3,
}
SCENARIOS = [
    ("Base model", {}),
    ("Tighter CO open · 24%", {"open-pct": 24}),
    ("Wider CO open · 42%", {"open-pct": 42}),
    ("Tighter CO response to a 3-bet · 12%", {"co-continue": 12}),
    ("Wider CO response to a 3-bet · 25%", {"co-continue": 25}),
    ("Wider blind defense", {"blind-scale": 1.35}),
    ("No rake", {"rake": 0}),
    ("Postflop checkdown", {"postflop": "checkdown"}),
]
ACTIONS = ("fold", "call", "raise")
LABEL = {"fold": "fold", "call": "call", "raise": "pot 3-bet"}


def cpu_name():
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    return platform.processor() or platform.machine()


def simulate(executable, hand, changes, samples, seed):
    # Whitelist at the process boundary. The simulator does not receive the
    # observed action, source, actual opponent cards, board or hand result.
    cfg = BASE | changes
    options = {
        "hero": "".join(hand["cards"]),
        "pot": hand["pot"],
        "open": hand["open_to"],
        "sb": hand["committed"][2],
        "bb": hand["committed"][3],
        "stacks": ",".join(str(x) for x in hand["stacks"]),
        "trials": samples,
        "ante": hand["ante_per_seat"],
        "seed": seed,
        "profile": "base",
        **cfg,
    }
    args = [str(executable)]
    for key, value in options.items():
        args.extend(["--" + key, str(value)])
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode:
        raise RuntimeError(
            f"CPU engine failed for {hand['room']} {hand['id']}: {proc.stderr}"
        )
    data = json.loads(proc.stdout)
    if data["trials"] != samples or not math.isclose(
        data["threebet_to"], hand["threebet_to"], abs_tol=1e-5
    ):
        raise ValueError(
            "Simulation and source replay disagree on trial count or pot-sized raise"
        )
    return data


def pool(runs, mean_key, se_key):
    n = sum(r["trials"] for r in runs)
    mean = sum(r["trials"] * r[mean_key] for r in runs) / n
    # Within plus between independent runs. SE_i^2 * n_i is sample variance.
    ss = sum(
        (r["trials"] - 1) * r["trials"] * r[se_key] ** 2
        + r["trials"] * (r[mean_key] - mean) ** 2
        for r in runs
    )
    return mean, math.sqrt(ss / ((n - 1) * n))


def contrast(scenario, a, b):
    if a == b:
        return 0.0, 0.0
    if b == "fold":
        return scenario[a], scenario[a + "_se"]
    if a == "fold":
        return -scenario[b], scenario[b + "_se"]
    delta = scenario["raise"] - scenario["call"]
    return (delta if a == "raise" else -delta), scenario["diff_se"]


def rank_hand(hand, scenarios):
    played = hand["action"]
    if played not in ACTIONS:
        raise ValueError("Unexpected BTN action")
    item = {"room": hand["room"], "id": hand["id"], "scenarios": scenarios}
    if played == "raise" and not math.isclose(
        hand["actual_to"], hand["threebet_to"], abs_tol=1e-5
    ):
        return item | {
            "priority": 0,
            "signal": "size",
            "category": "Different 3-bet size",
            "reason": f"Hero raised to {hand['actual_to']:g} BB. The model evaluates a pot-sized 3-bet to {hand['threebet_to']:g} BB. The played size falls outside the model.",
        }
    z = NormalDist().inv_cdf(1 - 0.05 / (2 * 3 * len(scenarios)))
    others = [a for a in ACTIONS if a != played]
    intervals = {}
    for a in others:
        intervals[a] = []
        for s in scenarios:
            diff, se = contrast(s, a, played)
            intervals[a].append((diff - z * se, diff + z * se, diff, s["name"]))
    best = max(others, key=lambda a: min(t[0] for t in intervals[a]))
    weakest = min(intervals[best], key=lambda t: t[0])
    if weakest[0] > 0:
        lo = min(t[2] for t in intervals[best])
        hi = max(t[2] for t in intervals[best])
        return item | {
            "signal": "consistent",
            "priority": 300 + min(weakest[0], 99),
            "alternative": best,
            "category": "Consistent reason to review",
            "reason": f"Hero chose {LABEL[played]}. {LABEL[best].capitalize()} leads in all {len(scenarios)} scenarios. The modeled gap ranges from {lo:.2f} to {hi:.2f} BB.",
        }
    # Look for a genuine sign reversal for the same pair, with both directions
    # separated from simulation noise. Changes among two unplayed actions alone
    # do not move an observed decision up the queue.
    for a in others:
        low = min(intervals[a], key=lambda t: t[1])
        high = max(intervals[a], key=lambda t: t[0])
        if low[1] < 0 < high[0]:
            extra = ""
            checkdown = next(
                (t for t in intervals[a] if t[3] == "Postflop checkdown"), None
            )
            betting = [t for t in intervals[a] if t is not checkdown]
            if checkdown and betting:
                if checkdown[0] > 0 and all(t[0] <= 0 for t in betting):
                    extra = " The alternative has a clear edge only in the checkdown scenario."
                elif checkdown[1] < 0 and all(t[1] >= 0 for t in betting):
                    extra = " The played action has a clear edge only in the checkdown scenario."
            return item | {
                "signal": "sensitive",
                "priority": 200 + min(high[0], -low[1], 99),
                "alternative": a,
                "category": "Scenario-dependent choice",
                "reason": f"Hero chose {LABEL[played]}. Relative to that action, {LABEL[a]} leads in the {high[3]} scenario (+{high[2]:.2f} BB) and trails in the {low[3]} scenario ({low[2]:.2f} BB)."
                + extra,
            }
    high, option = max(
        ((max(intervals[a], key=lambda t: t[0]), a) for a in others),
        key=lambda pair: pair[0][0],
    )
    if high[0] > 0:
        n = sum(t[0] > 0 for t in intervals[option])
        return item | {
            "signal": "exploratory",
            "priority": 100 + min(high[0], 99),
            "alternative": option,
            "category": "Conditional reason to review",
            "reason": f"{LABEL[option].capitalize()} leads the played {LABEL[played]} in {n} of {len(scenarios)} scenarios after accounting for simulation noise. The clearest gap is in the {high[3]} scenario (+{high[2]:.2f} BB).",
        }
    return item | {
        "signal": "none",
        "priority": 0,
        "category": "No clear difference",
        "reason": f"The modeled comparisons show no clear advantage over the played {LABEL[played]}.",
    }


def notes(samples, seeds):
    return [
        "The deterministic SHA-256 order selects three public anonymized CoinPoker hands with antes, two without antes and five iPoker hands without antes. The selection uses the state before BTN acts and requires at least 30 BB behind for all four active players. Measuring error frequency would require a different sample.",
        "Each trial deals new opponent cards and a new board. Call and pot-sized 3-bet use the same deal within a trial. Fold has zero EV from the decision point. The inputs include Hero's cards, the pot, commitments and stacks. The source view can show the observed result and showdown cards.",
        "Ranges and policies are modeling assumptions. CO percentages are cutoffs on the model's PLO4 ordering before conditioning on action. The base cutoffs are 32% to open and 18% to continue. Each variant changes one assumption.",
        "The simulator accounts for Hero's four card blockers and the CO open. The observed UTG and HJ folds select the spot. Remaining cards are dealt independently of those folding ranges. The hand ordering uses rank, pairs, suits and connectivity.",
        "Both blinds can call or reraise. Preflop allows pot-sized raises through the 4-bet, followed by call or fold. The simple postflop policy allows up to one half-pot bet per street with calls or folds. Frequencies depend on the hand, board, initiative and remaining players. Every player follows these preset postflop rules.",
        "The base rake assumption is 5% capped at 3 BB when a flop is dealt. A separate scenario omits rake. The actual schedules for these specific hands are unverified.",
        f"Each scenario combines {len(seeds)} independent runs of {samples:,} accepted deals. EV intervals represent approximately 95% simulation uncertainty. The 3-bet versus call difference uses paired trials.",
        "The review order uses Bonferroni-adjusted lower bounds across the three action pairs and all scenarios for each hand. It prioritizes alternatives that lead in every scenario, clear reversals and then advantages limited to some scenarios. Within each group, larger lower bounds come first. All hands remain available. This adjustment covers simulation noise. Strategic accuracy requires separate evidence.",
    ]


def execute(dataset, executable, samples, seeds, jobs):
    if not seeds or len(set(seeds)) != len(seeds) or samples < 2 or jobs < 1:
        raise ValueError(
            "Use distinct seeds, at least two trials per seed and a positive job count"
        )
    t0 = time.perf_counter()
    hands = dataset["hands"]
    tasks = []
    for index, hand in enumerate(hands):
        salt = int.from_bytes(
            hashlib.sha256(f"{hand['room']}|{hand['id']}".encode()).digest()[:4], "big"
        )
        for sidx, (_, changes) in enumerate(SCENARIOS):
            for seed in seeds:
                tasks.append((index, sidx, hand, changes, (salt + seed) % (2**32)))

    def work(task):
        i, j, hand, changes, seed = task
        return i, j, simulate(executable, hand, changes, samples, seed)

    by_run = {}
    with ThreadPoolExecutor(max_workers=jobs) as workers:
        for i, j, run in workers.map(work, tasks):
            by_run.setdefault((i, j), []).append(run)
    wall = time.perf_counter() - t0
    rows = []
    for i, hand in enumerate(hands):
        scenarios = []
        for j, (name, changes) in enumerate(SCENARIOS):
            records = by_run[i, j]
            call, call_se = pool(records, "call_ev", "call_se")
            raise_ev, raise_se = pool(records, "threebet_ev", "threebet_se")
            delta, delta_se = pool(records, "difference_ev", "difference_se")
            if not math.isclose(delta, raise_ev - call, abs_tol=1e-5):
                raise ValueError("Inconsistent paired action payoffs")
            scenarios.append(
                {
                    "name": name,
                    "call": call,
                    "raise": raise_ev,
                    "call_se": call_se,
                    "raise_se": raise_se,
                    "diff_se": delta_se,
                    "parameters": BASE | changes,
                    "replicates": records,
                }
            )
        ranked = rank_hand(hand, scenarios)
        base = by_run[i, 0]
        trials = sum(r["trials"] for r in base)
        multi = 100 * sum(r["call_events"]["multiway_flop"] for r in base) / trials
        take = 100 * sum(r["threebet_events"]["won_preflop"] for r in base) / trials
        ranked["baseline_mechanism"] = {
            "multiway_flop_after_call_pct": multi,
            "won_preflop_after_threebet_pct": take,
        }
        ranked["reason"] += (
            f" U osnovnom modelu posle calla najmanje troje vidi flop u {multi:.0f}% proba, "
            f"a pot 3-bet osvaja pot pre flopa u {take:.0f}%."
        )
        rows.append(ranked)
    return {
        "hands": rows,
        "timing": {
            "wall_seconds": wall,
            "cpu": cpu_name(),
            "threads": jobs,
            "seeds": list(seeds),
            "samples": samples * len(seeds),
            "total_trials": len(tasks) * samples,
            "scenario_count": len(SCENARIOS),
        },
        "notes": notes(samples, seeds),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hands", type=Path, default=HERE / "hands.json")
    p.add_argument(
        "--exe", type=Path, default=HERE / ("sim.exe" if os.name == "nt" else "sim")
    )
    p.add_argument(
        "--samples",
        type=int,
        default=100000,
        help="Accepted deals per hand, scenario and independent seed",
    )
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--output-dir", type=Path, default=HERE)
    args = p.parse_args()
    dataset = json.loads(args.hands.read_text(encoding="utf-8"))
    results = execute(dataset, args.exe.resolve(), args.samples, SEEDS, args.jobs)
    validation_path = HERE / "validation.json"
    if validation_path.is_file():
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.get("pilot_keys") == sorted(
            [h["room"], h["id"]] for h in dataset["hands"]
        ):
            results["validation_note"] = validation["note_en"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if __package__:
        from .render import render
    else:
        from render import render
    render(dataset, results, args.output_dir / "BTN-vs-CO.html")
    (args.output_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(json.dumps(results["timing"], ensure_ascii=True))
    for item in sorted(results["hands"], key=lambda h: -h["priority"]):
        print(
            item["room"],
            item["id"],
            item["signal"],
            item["reason"].encode("ascii", "backslashreplace").decode(),
        )


if __name__ == "__main__":
    main()
