"""
PryMax Studio — backup/restore tests
=======================================
Every test here uses real files in a temp directory and real sqlite3
connections — no mocking of the filesystem or the database. Run:

    python3 -m unittest test_backup.py -v
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

import backup


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.db_path = os.path.join(self.workspace, "prymax.db")
        self.backup_dir = os.path.join(self.workspace, "backups")

        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE rooms (room_id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO rooms VALUES ('room1', 'important data')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_backup_creates_a_valid_copy_with_correct_data(self):
        dest = backup.backup_database(self.db_path, self.backup_dir, keep=14)
        self.assertTrue(os.path.exists(dest))

        conn = sqlite3.connect(dest)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        row = conn.execute("SELECT * FROM rooms WHERE room_id = 'room1'").fetchone()
        self.assertEqual(row, ("room1", "important data"))

    def test_backup_of_missing_database_raises_clearly(self):
        with self.assertRaises(FileNotFoundError):
            backup.backup_database(
                os.path.join(self.workspace, "does-not-exist.db"), self.backup_dir, keep=14
            )

    def test_retention_keeps_only_the_n_most_recent(self):
        for _ in range(5):
            backup.backup_database(self.db_path, self.backup_dir, keep=3)
            time.sleep(1.1)  # filenames are second-resolution timestamps

        remaining = backup.list_backups(self.backup_dir)
        self.assertEqual(len(remaining), 3)

    def test_keep_zero_means_keep_all(self):
        for _ in range(4):
            backup.backup_database(self.db_path, self.backup_dir, keep=0)
            time.sleep(1.1)

        remaining = backup.list_backups(self.backup_dir)
        self.assertEqual(len(remaining), 4)

    def test_backup_survives_concurrent_writes_without_corruption(self):
        """The actual reason to use sqlite3's online backup API instead
        of a plain file copy: a live database being written to
        concurrently must still back up cleanly, not half-written."""
        stop = threading.Event()

        def writer():
            conn = sqlite3.connect(self.db_path)
            i = 0
            while not stop.is_set():
                conn.execute(
                    "INSERT OR REPLACE INTO rooms VALUES (?, ?)", (f"room{i}", f"data-{i}")
                )
                conn.commit()
                i += 1
                time.sleep(0.005)
            conn.close()

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.1)
        try:
            dest = backup.backup_database(self.db_path, self.backup_dir, keep=14)
        finally:
            stop.set()
            t.join()

        conn = sqlite3.connect(dest)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_restore_replaces_database_and_keeps_a_safety_copy_of_the_old_one(self):
        backup_path = backup.backup_database(self.db_path, self.backup_dir, keep=14)

        # Simulate a corrupted live database.
        with open(self.db_path, "w") as f:
            f.write("not a real database")

        backup.restore_database(backup_path, self.db_path)

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        row = conn.execute("SELECT * FROM rooms WHERE room_id = 'room1'").fetchone()
        self.assertEqual(row, ("room1", "important data"))

        safety_copies = [
            f for f in os.listdir(self.workspace) if f.startswith("prymax.db.pre-restore-")
        ]
        self.assertEqual(len(safety_copies), 1)
        with open(os.path.join(self.workspace, safety_copies[0])) as f:
            self.assertEqual(f.read(), "not a real database")

    def test_restore_from_missing_file_raises_clearly(self):
        with self.assertRaises(FileNotFoundError):
            backup.restore_database(
                os.path.join(self.workspace, "no-such-backup.db"), self.db_path
            )

    def test_list_backups_on_empty_directory_returns_empty(self):
        empty_dir = os.path.join(self.workspace, "no-backups-yet")
        self.assertEqual(backup.list_backups(empty_dir), [])


if __name__ == "__main__":
    unittest.main()
