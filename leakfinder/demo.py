"""A small local library populated exclusively with bundled, sanitized records."""

from pathlib import Path

from .storage import import_file, profile, set_profile

SAMPLES = Path(__file__).resolve().parents[1] / "demo"


def demo_path(personal):
    personal = Path(personal)
    return personal.parent / (personal.stem + "-demo") / "HeroDemo.sqlite3"


def load_demo(db):
    for name in ("CoinPoker-HeroDemo.txt", "iPoker-HeroDemo.sql"):
        # A release can be extracted at a new location. Read the same logical
        # fixture only once even when the path that originally supplied it moves.
        known = [Path(row[0]).name for row in db.execute("SELECT path FROM imports")]
        if name not in known:
            import_file(db, SAMPLES / name)
    demo_aliases = {"CoinPoker": ["HeroDemo"], "iPoker": ["HeroDemo"]}
    if profile(db) != demo_aliases:
        set_profile(db, demo_aliases)
