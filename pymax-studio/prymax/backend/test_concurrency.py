"""
PryMax Studio — concurrency stress test
==========================================
Closes a gap this project's own README used to flag as untested:
"nothing here proves the app behaves correctly under concurrent writers."
Now something does. This fires many real concurrent writes at store.py
from multiple threads — the exact scenario introduced when app.py's
Flask server was switched to threaded=True (see the Frontend Browser
Tests section of the README for why that change was made) — and checks
for the two failure modes that would actually matter: SQLite "database
is locked" errors, and data loss/corruption (missing or duplicate rows).

This test is slower and more resource-intensive than the rest of the
suite (spinning up real thread pools), so it's kept in its own file
rather than folded into test_app.py — run it deliberately, not as part
of a tight inner-loop test cycle:

    python3 -m unittest test_concurrency.py -v
"""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import time
import unittest


class ConcurrencyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        os.environ["PRYMAX_DB_PATH"] = self.tmp_db.name

        import importlib
        import store

        importlib.reload(store)
        self.store = store

        # Consistent with the rest of the suite: keep real structured-
        # logging output (store.init_db logs applied migrations) out of
        # normal test-runner output.
        import logging

        self._prymax_logger = logging.getLogger("prymax")
        self._original_log_level = self._prymax_logger.level
        self._prymax_logger.setLevel(logging.CRITICAL)

        self.store.init_db()

    def tearDown(self):
        self._prymax_logger.setLevel(self._original_log_level)
        # See testutil.py's docstring for exactly why this can't be a
        # plain os.unlink(self.tmp_db.name) here — that was the original,
        # subtly wrong version of this fix.
        import testutil

        testutil.track_for_cleanup(self.tmp_db.name)

    def test_500_concurrent_chat_writes_no_loss_no_corruption_no_locking_errors(self):
        room_id = "stress-room"
        self.store.load_room(room_id, time.time())
        n = 500
        errors = []

        def write(i):
            try:
                self.store.add_chat(
                    room_id, {"id": i + 1, "name": f"user{i}", "message": f"msg{i}", "ts": time.time()}
                )
                return None
            except Exception as e:  # noqa: BLE001 — we want to see any error type here
                return str(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(write, range(n)))

        errors = [r for r in results if r is not None]
        self.assertEqual(errors, [], f"concurrent writes raised errors: {errors[:3]}")

        room = self.store.load_room(room_id, time.time())
        chat_ids = [m["id"] for m in room["chat"]]
        self.assertEqual(len(chat_ids), n, "some concurrent writes were lost")
        self.assertEqual(len(set(chat_ids)), n, "duplicate ids found — corruption")

    def test_concurrent_writes_across_multiple_distinct_rooms(self):
        """Different rooms shouldn't interfere with each other's data
        even when written to at the same time from different threads."""
        room_ids = [f"room-{i}" for i in range(10)]
        for r in room_ids:
            self.store.load_room(r, time.time())

        def write_to_room(args):
            room_id, i = args
            self.store.add_chat(
                room_id, {"id": i, "name": "user", "message": f"msg-{room_id}-{i}", "ts": time.time()}
            )

        tasks = [(room_ids[i % len(room_ids)], i) for i in range(300)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
            list(executor.map(write_to_room, tasks))

        for room_id in room_ids:
            room = self.store.load_room(room_id, time.time())
            for entry in room["chat"]:
                self.assertIn(
                    room_id,
                    entry["message"],
                    "a message ended up in the wrong room — cross-room data corruption",
                )


if __name__ == "__main__":
    unittest.main()
