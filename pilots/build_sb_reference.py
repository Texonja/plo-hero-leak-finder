"""Rebuild one public sample branch. Usage: python -m pilots.build_sb_reference original.zip"""

import hashlib
import re
import sys
from pathlib import Path
from zipfile import ZipFile

SHA256 = "4381f336ca573e5c038c613ed05fdbd0a26032f9bab0a716db1950e708e7e29c"
BRANCH = "PLO-6max-100bb-rake5-1bbcap/ranges/Omaha/6-way/100bb[5p-1bb]/0.0.0.0.0.rng"


def compile_branch(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise ValueError("Expected the documented PokerData 100 BB sample.")
    with ZipFile(path) as archive:
        rows = archive.read(BRANCH).decode("ascii").splitlines()
    result = {}
    for name, values in zip(rows[::2], rows[1::2], strict=True):
        # Parentheses group cards of one suit. Ungrouped ranks have distinct suits.
        groups = re.findall(r"\(([2-9TJQKA]+)\)|([2-9TJQKA])", name)
        key = "/".join(sorted("".join(sorted(group or rank)) for group, rank in groups))
        weight = values.split(";")[0]
        if (
            key in result
            or len(key.replace("/", "")) != 4
            or not 0 <= float(weight) <= 1
        ):
            raise ValueError(f"Invalid hand class {name}")
        result[key] = weight
    if len(result) != 16_432:
        raise ValueError("Incomplete PLO4 table")
    return "".join(f"{k}\t{v}\n" for k, v in sorted(result.items()))


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "leakfinder" / "sb_fold_100bb.tsv"
    target.write_bytes(compile_branch(Path(sys.argv[1])).encode("ascii"))
