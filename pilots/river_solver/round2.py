"""Reconstruct the prior spot manifest with the current app; vary both arrival ranges."""

import argparse
import json
import os
import platform
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

from .run import dump, handle

LP_IDS = {
    f"{rule}-07"
    for rule in (
        "river_set_small_lead",
        "river_call_four_flush_board",
        "river_check_back_top_two",
    )
}
RESOLUTION_IDS = {
    f"{rule}-00"
    for rule in (
        "river_set_small_lead",
        "river_call_four_flush_board",
        "river_call_large_raise",
    )
}
PROFILES = ("tight", "middle", "wide")


def prepare_one(task):
    from .cases import case
    from .ranges import seat_ranges

    start = time.perf_counter()
    reference, record, out = task
    audit = reference["audit"]
    family = audit["family"]
    base_no = int(audit["source_hand"]) % 100000
    s, base, meta = case(family, base_no, audit["variant"], record)
    assert all(
        base[k] == reference[k]
        for k in ("board", "query", "pot_bb", "effective_stack_bb")
    ), audit["id"]
    # Reproduce the historical sizing comparison after the guide stops
    # presenting a fixed percentage. Keep the live guide text separately.
    base["tree"] = reference["tree"]
    meta["current_rule_active"] = meta["rule_active"]
    meta["rule_active"] = audit["rule_active"]
    hero_seat = 1 - meta["opponent_seat"]
    opponent = next(p.name for p in s.seats if p.live and not p.is_hero)
    counts = [500]
    key = audit["id"]
    if key in LP_IDS:
        counts.append(200)
    if key in RESOLUTION_IDS:
        counts.extend([1500, 2250])
    ranges = [
        seat_ranges(
            s.board,
            s.hero_cards if i == hero_seat else (),
            s.history,
            s.hero if i == hero_seat else opponent,
            counts,
        )
        for i in (0, 1)
    ]
    main_files = []
    meta.pop("range_basis", None)
    for count in counts:
        if count == 500 or (key in RESOLUTION_IDS and count > 500):
            pairs = (
                list(product(PROFILES, repeat=2))
                if meta["source"] == "demo"
                else list(zip(PROFILES, PROFILES))
            )
        else:
            pairs = [("middle", "middle")]
        for op, hero in pairs:
            versions = [op, op]
            versions[hero_seat] = hero
            items = {
                seat: ranges[i][count][versions[i]]
                for i, seat in enumerate(("oop", "ip"))
            }
            spec = {
                **base,
                "ranges": {k: v["entries"] for k, v in items.items()},
                "range_model": {k: v["metadata"] for k, v in items.items()},
                "audit": {
                    **meta,
                    "profile": op + "-" + hero,
                    "oop_profile": versions[0],
                    "ip_profile": versions[1],
                    "opponent_profile": op,
                    "hero_profile": hero,
                    "representatives": count,
                },
            }
            name = f"{key}-{count}-{op}-{hero}.json"
            source = out / ("inputs" if count == 500 else "validation-inputs") / name
            dump(source, spec)
            if count == 500:
                main_files.append(name)
    return key, time.perf_counter() - start, main_files


def read(p):
    return json.loads(p.read_text(encoding="utf8"))


def reusable(source, output):
    from .model import digest

    if not output.is_file() or not output.with_suffix(".txt").is_file():
        return False
    try:
        r = read(output)
        return (
            r["input_sha256"] == digest(read(source))
            and r["target_gap_pct"] == 0.012
            and "comparison" in r
        )
    except (ValueError, KeyError, OSError):
        return False


def cpu_name():
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0]
    return platform.processor() or platform.machine()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("prepare", "solve", "refine"))
    ap.add_argument("out", type=Path)
    ap.add_argument(
        "--previous",
        type=Path,
        default=Path(__file__).with_name("variant_manifest.json"),
    )
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    out = args.out.resolve()
    for name in ("inputs", "validation-inputs", "spots"):
        (out / name).mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    if args.phase == "prepare":
        from .cases import read_demo

        records = read_demo()
        if args.previous.is_file():
            cases = read(args.previous)
        else:
            cases = [
                read(p)
                for p in sorted((args.previous / "inputs").glob("*-middle.json"))
            ]
        cases.sort(
            key=lambda s: (
                s["audit"]["id"] not in LP_IDS | RESOLUTION_IDS,
                s["audit"]["id"],
            )
        )
        assert len(cases) == 102
        tasks = []
        for s in cases:
            tasks.append((s, records[int(s["audit"]["source_hand"]) % 100000], out))
        inventory = {}
        with ProcessPoolExecutor(args.workers) as pool:
            futures = [pool.submit(prepare_one, job) for job in tasks]
            for f in as_completed(futures):
                key, elapsed, names = f.result()
                inventory[key] = {"elapsed_seconds": elapsed, "files": names}
                if len(inventory) % 10 == 0:
                    print("Prepared", len(inventory), flush=True)
        s, _, observed = records[8585]
        dump(
            out / "excluded.json",
            {"hand": 8585, "live": sum(p.live for p in s.seats), "app": observed},
        )
        dump(
            out / "inventory.json",
            {
                "variants": inventory,
                "cpu": cpu_name(),
                "seconds": time.perf_counter() - t,
                "workers": args.workers,
            },
        )
        return
    phases = read(out / "inventory.json")
    if args.phase == "solve":
        jobs = [
            out / "inputs" / n for v in phases["variants"].values() for n in v["files"]
        ]
        jobs = [
            *(
                out / "validation-inputs" / f"{case}-1500-middle-middle.json"
                for case in sorted(RESOLUTION_IDS)
            ),
            *jobs,
        ]
    else:
        jobs = []
        for case in sorted(RESOLUTION_IDS):
            c500 = read(out / "spots" / f"{case}-500-middle-middle.json")["comparison"]
            c1500 = read(out / "spots" / f"{case}-1500-middle-middle.json")[
                "comparison"
            ]
            if (
                sign(c500) != sign(c1500)
                or c500["best_legal_action"] != c1500["best_legal_action"]
            ):
                jobs.extend(
                    sorted((out / "validation-inputs").glob(f"{case}-2250-*.json"))
                )
    with ProcessPoolExecutor(args.workers) as pool:
        futures = {
            pool.submit(handle, f): f
            for f in jobs
            if not reusable(f, out / "spots" / f.name)
        }
        for n, f in enumerate(as_completed(futures), 1):
            name, ok = f.result()
            if n % 15 == 0 or not ok:
                print(n, len(futures), name, ok, flush=True)
    dump(
        out / (args.phase + "-timing.json"),
        {
            "seconds": time.perf_counter() - t,
            "jobs": len(jobs),
            "computed": len(futures),
            "workers": args.workers,
            "cpu": cpu_name(),
        },
    )


def sign(comparison):
    delta = comparison["delta_bb"]
    return None if delta is None else (delta > 0) - (delta < 0)


if __name__ == "__main__":
    main()
