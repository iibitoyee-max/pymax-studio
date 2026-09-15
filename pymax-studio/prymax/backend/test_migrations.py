"""
PryMax Studio — migration system tests
=========================================
Tests migrations.py in isolation (plain sqlite3, no Flask/app.py needed)
plus one integration test proving store.py actually wires it in correctly
against a real file-backed database.

Run:
    python3 -m unittest test_migrations.py -v
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import time
import unittest

import migrations


class MigrationRunnerTestCase(unittest.TestCase):
    def test_fresh_database_applies_all_migrations_in_order(self):
        conn = sqlite3.connect(":memory:")
        applied = migrations.run_migrations(conn)
        self.assertEqual(applied, [m[0] for m in migrations.MIGRATIONS])
        self.assertEqual(
            migrations.current_schema_version(conn), migrations.MIGRATIONS[-1][0]
        )

    def test_running_twice_is_a_true_no_op(self):
        conn = sqlite3.connect(":memory:")
        migrations.run_migrations(conn)
        second_run = migrations.run_migrations(conn)
        self.assertEqual(second_run, [])

    def test_every_migration_id_gets_recorded_with_a_timestamp(self):
        conn = sqlite3.connect(":memory:")
        before = time.time()
        migrations.run_migrations(conn)
        after = time.time()
        rows = conn.execute(
            "SELECT id, applied_at FROM schema_migrations ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [m[0] for m in migrations.MIGRATIONS])
        for _id, applied_at in rows:
            self.assertTrue(before <= applied_at <= after)

    def test_upgrading_a_pre_migrations_database_preserves_existing_data(self):
        """The scenario that actually matters: every prymax.db created by
        earlier stages of this project has the original tables but no
        schema_migrations table and no rooms.last_activity_at column.
        Upgrading must not lose or corrupt what's already there."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE rooms (
                room_id TEXT PRIMARY KEY,
                host_token_hash TEXT,
                profile_title TEXT,
                profile_mode TEXT NOT NULL DEFAULT 'webrtc',
                profile_video_quality TEXT NOT NULL DEFAULT '1080p',
                profile_audio_format TEXT NOT NULL DEFAULT 'aac',
                raffle_entries_json TEXT NOT NULL DEFAULT '[]',
                raffle_winner TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, room_id TEXT, name TEXT, message TEXT, ts REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO rooms (room_id, host_token_hash, created_at) VALUES (?, ?, ?)",
            ("preexisting-room", "some-real-hash", time.time()),
        )
        conn.execute(
            "INSERT INTO chat_messages (id, room_id, name, message, ts) VALUES (1, ?, ?, ?, ?)",
            ("preexisting-room", "Alice", "a message from before migrations existed", time.time()),
        )
        conn.commit()

        applied = migrations.run_migrations(conn)
        self.assertEqual(
            applied,
            [m[0] for m in migrations.MIGRATIONS],
            "should apply every migration despite migration 1's tables already existing",
        )

        # Old data must survive untouched.
        room = conn.execute(
            "SELECT host_token_hash, last_activity_at FROM rooms WHERE room_id = ?",
            ("preexisting-room",),
        ).fetchone()
        self.assertEqual(room[0], "some-real-hash")
        self.assertIsNone(room[1], "new column should exist but be NULL for pre-existing rows")

        chat = conn.execute(
            "SELECT name, message FROM chat_messages WHERE room_id = ?", ("preexisting-room",)
        ).fetchone()
        self.assertEqual(chat, ("Alice", "a message from before migrations existed"))

    def test_current_schema_version_on_untouched_database_is_zero(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(migrations.current_schema_version(conn), 0)


class StoreIntegrationTestCase(unittest.TestCase):
    """Proves store.py actually calls run_migrations correctly against a
    real file on disk — not just that migrations.py works in isolation.

    store.py opens its SQLite connection at module import time, bound to
    whatever PRYMAX_DB_PATH was set to at that first import — a module
    singleton. A second `import store` after changing the env var is a
    no-op (Python caches modules), so without an explicit reload, a
    second test in this class would silently keep operating on the
    first test's database file instead of its own. Caught by directly
    checking `store.DB_PATH` after a simulated second import and finding
    it unchanged — so every test here forces a real reload instead of
    trusting a plain `import store` to pick up the new path.
    """

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        os.environ["PRYMAX_DB_PATH"] = self.tmp_db.name

        import store

        importlib.reload(store)
        self.store = store

        # Consistent with test_app.py/test_logging.py: keep the real
        # structured-logging output (store.init_db logs applied
        # migrations) out of normal test-runner output.
        import logging

        self._prymax_logger = logging.getLogger("prymax")
        self._original_log_level = self._prymax_logger.level
        self._prymax_logger.setLevel(logging.CRITICAL)

    def tearDown(self):
        self._prymax_logger.setLevel(self._original_log_level)
        # See testutil.py's docstring for exactly why this can't be a
        # plain os.unlink(self.tmp_db.name) — that was the original,
        # subtly wrong version of this fix, which just traded a
        # "closed connection" bug for an equally bad "read-only
        # connection after the file it pointed at was unlinked" bug.
        import testutil

        testutil.track_for_cleanup(self.tmp_db.name)

    def test_store_init_db_applies_migrations_to_a_real_file(self):
        self.store.init_db()
        version = migrations.current_schema_version(self.store._conn)
        self.assertEqual(version, migrations.MIGRATIONS[-1][0])
        self.assertEqual(self.store.DB_PATH, self.tmp_db.name)

    def test_last_activity_at_updates_on_a_real_write(self):
        self.store.init_db()
        room_id = "activity-test-room"
        self.store.load_room(room_id, time.time())

        before = self.store._conn.execute(
            "SELECT last_activity_at FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        self.assertIsNone(before)

        self.store.add_chat(
            room_id, {"id": 99999, "name": "X", "message": "hi", "ts": time.time()}
        )

        after = self.store._conn.execute(
            "SELECT last_activity_at FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        self.assertIsNotNone(after)


if __name__ == "__main__":
    unittest.main()
