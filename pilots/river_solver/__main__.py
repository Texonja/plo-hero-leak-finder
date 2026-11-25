"""Solve one explicit pilot input and write an auditable result."""

import argparse
import json
import os
from pathlib import Path

for key in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
):
    os.environ[key] = "1"


def main():
    from .run import write_spot_text
    from .solver import solve

    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--iterations", type=int, default=12000)
    parser.add_argument("--min-iterations", type=int, default=100)
    parser.add_argument(
        "--target",
        type=float,
        default=0.04,
        help="maximum sum of best-response gains in percent of pot",
    )
    args = parser.parse_args()
    if (
        args.output.suffix.lower() != ".json"
        or args.input.resolve() == args.output.resolve()
    ):
        parser.error("Choose a separate .json output file.")
    spec = json.loads(args.input.read_text(encoding="utf8"))
    result, timing = solve(
        spec,
        iterations=args.iterations,
        target_pct=args.target,
        minimum_iterations=args.min_iterations,
    )
    result["timing"] = {**timing, "cpu_threads": 1}
    args.output.write_text(
        json.dumps(result, indent=1, ensure_ascii=False, allow_nan=False),
        encoding="utf8",
    )
    write_spot_text(args.output, result)
    print(
        "Reliable" if result["reliable"] else "Unreliable",
        result["query"]["hero_action_ev_bb"],
        "exploitability % pot",
        result["exploitability_pct_pot"],
        "seconds",
        timing["elapsed_seconds"],
    )


if __name__ == "__main__":
    main()
