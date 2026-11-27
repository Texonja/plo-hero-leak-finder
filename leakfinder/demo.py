"""A small local library populated exclusively with bundled, sanitized records."""

from pathlib import Path

from .storage import (
    file_digest,
    import_file,
    import_file_with_source,
    profile,
    set_profile,
)

SAMPLES = Path(__file__).resolve().parents[1] / "demo"


def demo_path(personal):
    personal = Path(personal)
    return personal.parent / (personal.stem + "-demo") / "HeroDemo.sqlite3"


def load_demo(db):
    sources = db.execute(
        "SELECT id,path,digest,EXISTS(SELECT 1 FROM rejected WHERE source=imports.id "
        "AND reason='Fold with no price') OR EXISTS(SELECT 1 FROM amount_retry "
        "WHERE source=imports.id) AS needs_retry FROM imports"
    ).fetchall()
    for name in ("CoinPoker-HeroDemo.txt", "iPoker-HeroDemo.sql"):
        # A release can be extracted at a new location. Read the same logical
        # fixture only once even when the path that originally supplied it moves.
        known = [row for row in sources if Path(row["path"]).name == name]
        path = SAMPLES / name
        if not known:
            import_file(db, path)
        else:
            for row in known:
                if row["needs_retry"] and row["digest"] == file_digest(path):
                    import_file_with_source(db, path, retry_source=row["id"])
    demo_aliases = {"CoinPoker": ["HeroDemo"], "iPoker": ["HeroDemo"]}
    if profile(db) != demo_aliases:
        set_profile(db, demo_aliases)
