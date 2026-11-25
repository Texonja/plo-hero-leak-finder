"""Independent river sequence-form LP for checking CFR game values on identical inputs."""

import argparse
import json
import os
import time
import warnings
from pathlib import Path

for key in (
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "OMP_NUM_THREADS",
):
    os.environ[key] = "1"


def public_sequences(spec):
    """Generate each player's realization constraints and all terminal pot payoffs."""
    pot, cap, tree = spec["pot_bb"], spec["effective_stack_bb"], spec["tree"]
    minimum = tree.get("min_bet_bb", 1)
    seq_counts = [1, 1]  # empty sequence for every private hand
    flows = [[{0: 1}], [{0: 1}]]
    leaves = []

    def visit(actor, paid, raises, last, sequences):
        other = 1 - actor
        owed = paid[other] - paid[actor]
        if owed:
            moves = [("fold", 0), ("call", paid[other])]
            # All amounts denote total commitment since the start of the street.
            # A raise is capped at pot after calling or the effective all-in.
            after_call = pot + 2 * paid[other]
            limit = min(cap, paid[other] + after_call)
            targets = {
                round(min(limit, paid[other] + r * after_call), 8)
                for r in tree.get("raise_fractions", [1])
            }
            if tree.get("all_in", True) and cap <= limit + 1e-8:
                targets.add(round(cap, 8))
            if raises < tree.get("max_raises", 1):
                for target in sorted(targets):
                    if target > paid[other] and (
                        target - paid[other] + 1e-8 >= owed or abs(target - cap) < 1e-8
                    ):
                        moves.append(("raise", target))
        else:
            moves = [("check", 0)]
            fractions = (
                tree.get("ip_bet_fractions", tree["bet_fractions"])
                if actor
                else tree["bet_fractions"]
            )
            bets = {round(min(cap, max(minimum, pot * f)), 8) for f in fractions}
            if tree.get("all_in", True) and cap <= pot and fractions:
                bets.add(round(cap, 8))
            moves.extend(("bet", bet) for bet in sorted(bets) if 0 < bet <= pot + 1e-8)
        row = {sequences[actor]: -1}
        for action, amount in moves:
            seq = list(sequences)
            seq[actor] = seq_counts[actor]
            seq_counts[actor] += 1
            row[seq[actor]] = 1
            if action == "fold":
                # Signed OOP payoff in units of pot, centered at the starting half pot.
                leaves.append(
                    (
                        tuple(seq),
                        False,
                        (0.5 + paid[actor] / pot) * (1 if other == 0 else -1),
                    )
                )
            elif action == "call" or action == "check" and last == "check":
                leaves.append((tuple(seq), True, 0.5 + amount / pot))
            else:
                next_paid = list(paid)
                next_paid[actor] = amount
                visit(other, next_paid, raises + (action == "raise"), action, seq)
        flows[actor].append(row)

    visit(0, [0, 0], 0, None, [0, 0])
    return seq_counts, flows, leaves


def exact_value(spec, *, threads=None):
    import numpy as np
    from scipy import sparse
    from scipy.optimize import OptimizeWarning, linprog

    from leakfinder.vendor.primitives import best_plo_hand

    started = time.perf_counter()
    sizes, flows, leaves = public_sequences(spec)
    hands = [
        [set(row["cards"]) for row in spec["ranges"][seat]] for seat in ("oop", "ip")
    ]
    counts = [len(h) for h in hands]
    weights = [
        np.array([row["weight"] for row in spec["ranges"][seat]], dtype=float)
        for seat in ("oop", "ip")
    ]
    weights = [w / w.sum() for w in weights]
    # Chance is carried in the private-hand root realization constraints, separately
    # for each player. Remove impossible joint deals and normalize by their legal mass.
    legal = np.array(
        [[left.isdisjoint(right) for right in hands[1]] for left in hands[0]]
    )
    mass = weights[0] @ legal @ weights[1]
    board = list(spec["board"])

    def rank(h):
        made, kickers, _, _ = best_plo_hand(sorted(h), board)
        return made, tuple(kickers)

    ranks = [[rank(h) for h in hs] for hs in hands]
    order = {r: i for i, r in enumerate(sorted(set(ranks[0] + ranks[1])))}
    score = [np.array([order[r] for r in rs]) for rs in ranks]
    showdown = sparse.csr_matrix(np.sign(score[0][:, None] - score[1]) * legal / mass)
    legal = sparse.csr_matrix(legal.astype(float) / mass)
    block_rows = []
    block_cols = []
    block_values = {"fold": [], "show": []}
    for (x, y), show, multiplier in leaves:
        block_rows.append(x)
        block_cols.append(y)
        block_values["fold"].append(0 if show else multiplier)
        block_values["show"].append(multiplier if show else 0)

    def blocks(key, private):
        pattern = sparse.coo_matrix(
            (block_values[key], (block_rows, block_cols)), shape=tuple(sizes)
        ).tocsr()
        pattern.eliminate_zeros()
        return sparse.kron(pattern, private, format="csr")

    A = blocks("fold", legal) + blocks("show", showdown)

    def flow(seat):
        rows, cols, vals = [], [], []
        for n, row in enumerate(flows[seat]):
            for j, v in row.items():
                rows.append(n)
                cols.append(j)
                vals.append(v)
        pattern = sparse.coo_matrix(
            (vals, (rows, cols)), shape=(len(flows[seat]), sizes[seat])
        )
        E = sparse.kron(pattern, sparse.eye(counts[seat]), format="csr")
        rhs = np.zeros(E.shape[0])
        rhs[: counts[seat]] = weights[seat]
        return E, rhs

    E, e = flow(0)
    F, f = flow(1)
    nx = A.shape[0]
    ny = len(f)
    constructed = time.perf_counter()
    with warnings.catch_warnings():
        # HiGHS accepts threads although scipy does not expose a named option.
        warnings.filterwarnings(
            "ignore", category=OptimizeWarning, message="Unrecognized options.*"
        )
        problem = linprog(
            np.r_[np.zeros(nx), -f],
            A_ub=sparse.hstack([-A.T, F.T], format="csr"),
            b_ub=np.zeros(A.shape[1]),
            A_eq=sparse.hstack([E, sparse.csr_matrix((len(e), ny))], format="csr"),
            b_eq=e,
            bounds=[(0, None)] * nx + [(None, None)] * ny,
            method="highs-ipm",
            options={
                # Embedded callers may already have initialized the process-wide
                # HiGHS scheduler. A fresh command-line process pins one worker.
                **({"threads": threads} if threads is not None else {}),
                "primal_feasibility_tolerance": 1e-8,
                "dual_feasibility_tolerance": 1e-8,
            },
        )
    if not problem.success:
        raise RuntimeError(f"HiGHS {problem.status}: {problem.message}")
    x = problem.x[:nx]
    v = problem.x[nx:]
    y = -problem.ineqlin.marginals
    w = -problem.eqlin.marginals
    residuals = {
        "oop_flow": float(abs(E @ x - e).max()),
        "ip_flow": float(abs(F @ y - f).max()),
        "oop_nonneg": float(max(0, -x.min())),
        "ip_nonneg": float(max(0, -y.min())),
        "lower_value": float(max(0, (F.T @ v - A.T @ x).max())),
        "upper_value": float(max(0, (A @ y - E.T @ w).max())),
    }
    if max(residuals.values()) > 5e-7:
        raise ArithmeticError(f"LP strategies or dual bounds infeasible: {residuals}")
    pot = spec["pot_bb"]
    lower = (0.5 + f @ v) * pot
    upper = (0.5 + e @ w) * pot
    if abs(lower - upper) > pot * 1e-7:
        raise ArithmeticError("LP primal/dual value mismatch")
    return {
        "lp_oop_bb": float(lower),
        "dual_oop_bb": float(upper),
        "representatives": counts,
        "terminal_histories": len(leaves),
        "realization_sequences": list(A.shape),
        "payoff_nonzeros": A.nnz,
        "solver": "HiGHS interior point with crossover",
        "cpu_threads": threads,
        "iterations": problem.nit,
        "max_residuals": residuals,
        "setup_seconds": constructed - started,
        "lp_seconds": time.perf_counter() - constructed,
    }


def check(spec, *, threads=None, target_pct=0.04):
    from .model import digest
    from .solver import solve

    result = exact_value(spec, threads=threads)
    cfr, timing = solve(spec, target_pct=target_pct, iterations=20000)
    pot = spec["pot_bb"]
    delta = 100 * abs(result["lp_oop_bb"] - cfr["game_ev_bb"][0]) / pot
    lower = pot - cfr["best_response_ev_bb"][1]
    upper = cfr["best_response_ev_bb"][0]
    result.update(
        input_sha256=digest(spec),
        ranges_sha256=digest(spec["ranges"]),
        board=spec["board"],
        pot_bb=pot,
        effective_stack_bb=spec["effective_stack_bb"],
        tree=spec["tree"],
        cfr_oop_bb=cfr["game_ev_bb"][0],
        cfr_iterations=cfr["iterations"],
        cfr_exploitability_pct=cfr["exploitability_pct_pot"],
        cfr_seconds=timing["elapsed_seconds"],
        cfr_best_response_bounds_bb=[lower, upper],
        absolute_difference_pct_pot=delta,
        passed=bool(
            delta <= 0.1
            and cfr["reliable"]
            and lower - 1e-7 <= result["lp_oop_bb"] <= upper + 1e-7
        ),
    )
    return result


def compare_production(result, saved):
    """Attach the actual batch equilibrium, when the LP used that exact batch input."""
    if saved["input_sha256"] != result["input_sha256"]:
        raise ValueError("LP and production input hashes differ")
    pot = result["pot_bb"]
    value = saved["game_ev_bb"][0]
    delta = 100 * abs(value - result["lp_oop_bb"]) / pot
    br_lower = pot - saved["best_response_ev_bb"][1]
    br_upper = saved["best_response_ev_bb"][0]
    passed = (
        saved["reliable"]
        and delta <= 0.1
        and br_lower - 1e-7 <= result["lp_oop_bb"] <= br_upper + 1e-7
    )
    result["production"] = {
        "cfr_oop_bb": value,
        "cfr_iterations": saved["iterations"],
        "cfr_exploitability_pct": saved["exploitability_pct_pot"],
        "cfr_best_response_bounds_bb": [br_lower, br_upper],
        "solution_sha256": saved["solution_sha256"],
        "absolute_difference_pct_pot": delta,
        "passed": bool(passed),
    }
    result["passed"] = bool(result["passed"] and passed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--production", type=Path, help="Directory with saved batch results"
    )
    args = parser.parse_args()
    results = []
    for p in args.inputs:
        spec = json.loads(p.read_text(encoding="utf8"))
        result = check(spec, threads=1)
        result["input_file"] = p.name
        if args.production and (saved := args.production / p.name).is_file():
            compare_production(result, json.loads(saved.read_text(encoding="utf8")))
        results.append(result)
        print(
            p.name, result["absolute_difference_pct_pot"], result["passed"], flush=True
        )
        args.output.write_text(
            json.dumps(
                {
                    "cases": results,
                    "all_passed": all(r["passed"] for r in results),
                    "max_absolute_difference_pct_pot": max(
                        r["absolute_difference_pct_pot"] for r in results
                    ),
                },
                indent=2,
                allow_nan=False,
            ),
            encoding="utf8",
        )


if __name__ == "__main__":
    main()
