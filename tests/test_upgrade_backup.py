"""Verify pre-migration snapshots and retries on real on-disk SQLite files."""

import shutil
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from leakfinder.storage import connect, import_file, initialize
from tests.test_library import original


class UpgradeBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "private #1.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        initialize(self.db)
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def snapshots(self):
        return sorted((self.root / "backups").glob("*/" + self.path.name))

    def test_wal_commit_is_in_standalone_restorable_old_snapshot(self):
        self.assertFalse((self.root / "backups").exists())
        with self.db:
            self.db.execute("PRAGMA user_version=3")
            self.db.execute("INSERT INTO aliases VALUES('CoinPoker','SnapshotHero')")
        # The main file still shows v4. Only the WAL contains this v3 state.
        with self.path.open("rb") as f:
            f.seek(60)
            self.assertEqual(int.from_bytes(f.read(4), "big"), 4)
        initialize(self.db)
        (backup,) = self.snapshots()
        self.assertEqual(list(backup.parent.iterdir()), [backup])
        restored = self.root / "restored.sqlite3"
        shutil.copyfile(backup, restored)
        with closing(sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(old.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                old.execute("SELECT name FROM aliases").fetchone()[0], "SnapshotHero"
            )
        initialize(self.db)
        self.assertEqual(self.snapshots(), [backup])

    def test_backup_failure_precedes_even_schema_creation(self):
        with self.db:
            self.db.execute("PRAGMA user_version=2")
            self.db.execute("DROP TABLE import_recovery")
        blocked = self.root / "backups"
        blocked.write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(OSError, "Could not back up"):
            initialize(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(
            self.db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='import_recovery'"
            ).fetchone()
        )
        blocked.unlink()
        initialize(self.db)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_retries_preserve_changed_states_without_accumulating_identical_copies(
        self,
    ):
        with self.db:
            self.db.execute("PRAGMA user_version=3")
            self.db.execute(
                "INSERT INTO review_comparison_cache VALUES('Both','old',X'01')"
            )
        self.db.execute("""CREATE TEMP TRIGGER block_upgrade BEFORE DELETE ON review_comparison_cache
                            BEGIN SELECT RAISE(ABORT,'retry upgrade'); END""")
        for _ in range(2):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "retry upgrade"):
                initialize(self.db)
            self.assertEqual(len(self.snapshots()), 1)
            self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        (first,) = self.snapshots()
        with self.db:
            self.db.execute("INSERT INTO aliases VALUES('iPoker','LaterAlias')")
        self.db.execute("DROP TRIGGER block_upgrade")
        initialize(self.db)
        (later,) = set(self.snapshots()) - {first}
        for path, expected in ((first, 0), (later, 1)):
            with closing(sqlite3.connect(path)) as saved:
                self.assertEqual(
                    saved.execute("SELECT count(*) FROM aliases").fetchone()[0],
                    expected,
                )
                self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_concurrent_openers_produce_one_complete_backup(self):
        with self.db:
            self.db.execute("PRAGMA user_version=3")
        start = Barrier(2)

        def open_library(_):
            with closing(connect(self.path)) as db:
                start.wait(timeout=10)
                initialize(db)
                return db.execute("PRAGMA user_version").fetchone()[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(open_library, range(2))), [4, 4])
        self.assertEqual(len(self.snapshots()), 1)

    def test_restart_after_process_stops_mid_upgrade(self):
        with self.db:
            self.db.execute("PRAGMA user_version=3")
            self.db.execute(
                "INSERT INTO review_comparison_cache VALUES('Both','old',X'01')"
            )
        script = """
import os, sys
from leakfinder.storage import connect, initialize
db = connect(sys.argv[1])
db.create_function("interrupt_upgrade", 0, lambda: os._exit(73))
db.execute('''CREATE TEMP TRIGGER interrupt AFTER DELETE ON review_comparison_cache
              BEGIN SELECT interrupt_upgrade(); END''')
initialize(db)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.path)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(
            self.db.execute("SELECT revision FROM review_comparison_cache").fetchone()[
                0
            ],
            "old",
        )
        (backup,) = self.snapshots()
        initialize(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.snapshots(), [backup])

    def test_v1_snapshot_contains_original_schema_and_replay_rebuilds_all_facts(self):
        src = self.root / "original.txt"
        src.write_text(original("coin-30.hh"), encoding="utf-8")
        self.assertEqual(import_file(self.db, src)["new hands"], 1)
        tables = ("facts", "players", "obs", "decisions")

        def contents():
            return [
                list(self.db.execute(f"SELECT * FROM {t} ORDER BY rowid"))
                for t in tables
            ]

        expected = contents()
        with self.db:
            self.db.execute("PRAGMA user_version=1")
            for table in ("facts", "players", "obs", "review_comparison_cache"):
                self.db.execute(f"DROP TABLE {table}")
        initialize(self.db)
        self.assertEqual(contents(), expected)
        (backup,) = self.snapshots()
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(
                saved.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='facts'"
                ).fetchone()
            )
            self.assertEqual(
                saved.execute("SELECT count(*) FROM origins").fetchone()[0], 1
            )
