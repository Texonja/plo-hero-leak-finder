"""Standalone SQLite snapshots kept beside the library before a schema upgrade."""

import filecmp
import sqlite3
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp


def backup_before_upgrade(db, old_version, new_version):
    """Call with the source write lock held and before changing any of its tables."""
    filename = next(
        row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"
    )
    if not filename:  # In-memory libraries have no persistent file to restore.
        return
    source = Path(filename)
    backups = source.parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"{source.name}-v{old_version}-before-v{new_version}-"
    folder = None
    try:
        backups.mkdir(mode=0o700, exist_ok=True)
        previous = max(
            (
                d / source.name
                for d in backups.iterdir()
                if d.name.startswith(prefix) and (d / source.name).is_file()
            ),
            key=lambda p: p.stat().st_mtime_ns,
            default=None,
        )
        folder = Path(mkdtemp(prefix=f"{prefix}{stamp}-", dir=backups))
        partial = folder / (source.name + ".partial")
        # A separate reader sees all committed pages, including the current WAL.
        # Calling backup() on the connection that holds BEGIN IMMEDIATE would wait on itself.
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(partial)) as snapshot,
        ):
            reader.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE").fetchone()
            if snapshot.execute("PRAGMA user_version").fetchone()[0] != old_version:
                raise sqlite3.DatabaseError(
                    "The snapshot has an unexpected library version."
                )
        # A retry after a failed migration can reach the exact same snapshot.
        # Check the bytes; an older version could have imported hands in between.
        if (
            previous is not None
            and not any(
                Path(str(previous) + suffix).exists() for suffix in ("-wal", "-journal")
            )
            and filecmp.cmp(partial, previous, shallow=False)
        ):
            partial.unlink()
            folder.rmdir()
            return
        # Each destination folder is created exclusively. Publish only a closed snapshot.
        partial.rename(folder / source.name)
    except (OSError, sqlite3.Error) as exc:
        if folder is not None:
            with suppress(OSError):
                partial.unlink(missing_ok=True)
                folder.rmdir()
        raise OSError(
            f"Could not back up {source.name} to {backups}. "
            f"The upgrade stopped before changing the library. {exc}"
        ) from exc
