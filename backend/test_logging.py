"""
PryMax Studio — logging behavior tests
=========================================
Separate from test_app.py because these tests need to capture and parse
actual log output, which means redirecting the logger's stream handler —
different setup from the plain API tests. Run together with test_app.py:

    python3 -m unittest discover -p "test_*.py" -v

These tests exist because "the app logs things" is a claim that's easy to
get subtly wrong (wrong level, malformed JSON, missing fields, a handler
that silently swallows output) without ever showing up as a crash. Every
test here redirects the real logger's output to a buffer and parses what
actually came out, the same way test_app.py exercises real HTTP behavior
instead of asserting against internals.

Isolation: each test reloads store.py fresh, pointed at its own temp
database — not just a one-time module-level setup. See test_app.py's
module docstring and testutil.py for why: store's connection is a
process-wide singleton, and without a per-test reload, this suite's
tests would silently inherit whatever database a different test file
(test_migrations.py, test_concurrency.py) left `store` pointed at if it
happened to run first in the same `unittest discover` process.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import tempfile
import unittest
import uuid

import app as app_module  # noqa: E402
import ratelimit  # noqa: E402
import store  # noqa: E402
import testutil  # noqa: E402


# Registered once at import time — Flask forbids adding routes after the
# app has handled its first request, so this can't live inside a test
# method (discovered by actually running this suite, not by inspection).
@app_module.app.get("/__test_only_crash")
def _test_only_crash():
    raise ValueError("boom")


def new_room_id() -> str:
    return "t" + uuid.uuid4().hex[:12]


class LoggingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        os.environ["PRYMAX_DB_PATH"] = self.tmp_db.name

        # Suppress specifically for init_db()'s own "migrations applied"
        # INFO line — at this point the buffer redirect below hasn't
        # happened yet, so without this that line would escape straight
        # to real stdout instead of being captured or suppressed, which
        # is exactly what was observed (and is otherwise harmless, but
        # noisy) when this file's setUp order was first written.
        original_level_for_init = app_module.logger.level
        app_module.logger.setLevel(logging.CRITICAL)
        importlib.reload(store)
        store.init_db()
        app_module.logger.setLevel(original_level_for_init)

        self.client = app_module.app.test_client()
        app_module._rooms.clear()
        with ratelimit._lock:
            ratelimit._hits.clear()
        self.room = new_room_id()

        # Whichever process-wide log level another test module set at
        # import time (env vars only take effect once, at setup_logging()
        # call time — see logging_config.py), force it to INFO for the
        # duration of this test so log lines are actually captured,
        # regardless of import order relative to test_app.py.
        self.original_level = app_module.logger.level
        app_module.logger.setLevel(logging.INFO)

        # Redirect the real logger's stream handler to a buffer we can
        # read back and parse, then restore it in tearDown so other test
        # modules aren't affected.
        self.buf = io.StringIO()
        self.handler = app_module.logger.handlers[0]
        self.original_stream = self.handler.stream
        self.handler.stream = self.buf

    def tearDown(self):
        self.handler.stream = self.original_stream
        app_module.logger.setLevel(self.original_level)
        # Deferred to process exit — see testutil.py's docstring.
        testutil.track_for_cleanup(self.tmp_db.name)

    def _log_lines(self) -> list[dict]:
        raw = self.buf.getvalue().strip()
        if not raw:
            return []
        return [json.loads(line) for line in raw.split("\n")]

    def test_every_log_line_is_valid_json_with_required_fields(self):
        self.client.get("/api/health")
        self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        lines = self._log_lines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertIn("ts", line)
            self.assertIn("level", line)
            self.assertIn("message", line)

    def test_successful_request_logs_at_info_with_status_and_duration(self):
        self.client.get("/api/health")
        lines = self._log_lines()
        request_lines = [l for l in lines if l["message"] == "request"]
        self.assertEqual(len(request_lines), 1)
        entry = request_lines[0]
        self.assertEqual(entry["level"], "INFO")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["path"], "/api/health")
        self.assertIn("duration_ms", entry)
        self.assertIsInstance(entry["duration_ms"], (int, float))

    def test_rejected_request_logs_at_warning(self):
        self.client.post(f"/api/room/{self.room}/chat", json={"name": "", "message": ""})
        lines = self._log_lines()
        rejected = [l for l in lines if l["message"] == "request rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["level"], "WARNING")
        self.assertEqual(rejected[0]["status"], 400)

    def test_host_claim_logs_business_event_without_leaking_the_token(self):
        res = self.client.post(f"/api/room/{self.room}/host/claim")
        token = res.get_json()["host_token"]

        lines = self._log_lines()
        claim_events = [l for l in lines if l["message"] == "host claimed"]
        self.assertEqual(len(claim_events), 1)
        self.assertEqual(claim_events[0]["room_id"], self.room)

        # The raw token must never appear anywhere in the log output —
        # only its hash is ever persisted, and the token itself shouldn't
        # be logged even transiently.
        full_log_text = self.buf.getvalue()
        self.assertNotIn(token, full_log_text)

    def test_raffle_draw_logs_winner_and_entry_count(self):
        self.client.post(f"/api/room/{self.room}/raffle/enter", json={"name": "Alice"})
        self.client.post(f"/api/room/{self.room}/raffle/enter", json={"name": "Bob"})
        claim = self.client.post(f"/api/room/{self.room}/host/claim").get_json()
        self.client.post(
            f"/api/room/{self.room}/raffle/draw",
            json={"host_token": claim["host_token"]},
        )
        lines = self._log_lines()
        draws = [l for l in lines if l["message"] == "raffle drawn"]
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0]["entry_count"], 2)
        self.assertIn(draws[0]["winner"], ["Alice", "Bob"])

    def test_breakout_start_logs_group_config(self):
        for name in ("A", "B", "C"):
            self.client.post(f"/api/room/{self.room}/join", json={"name": name})
        claim = self.client.post(f"/api/room/{self.room}/host/claim").get_json()
        self.client.post(
            f"/api/room/{self.room}/breakout/start",
            json={"host_token": claim["host_token"], "count": 2, "duration_seconds": 60},
        )
        lines = self._log_lines()
        starts = [l for l in lines if l["message"] == "breakout started"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["count"], 2)
        self.assertEqual(starts[0]["peer_count"], 3)

    def test_rate_limit_exceeded_is_logged(self):
        for i in range(6):
            self.client.post(f"/api/room/{self.room}chk{i}/host/claim")
        lines = self._log_lines()
        rate_limit_events = [l for l in lines if l["message"] == "rate limit exceeded"]
        self.assertGreaterEqual(len(rate_limit_events), 1)
        self.assertEqual(rate_limit_events[0]["limiter"], "host_claim")

    def test_uncaught_exception_logs_at_error_with_traceback(self):
        res = self.client.get("/__test_only_crash")
        self.assertEqual(res.status_code, 500)

        lines = self._log_lines()
        errors = [l for l in lines if l["level"] == "ERROR"]
        self.assertGreaterEqual(len(errors), 1)
        self.assertIn("exc_info", errors[0])
        self.assertIn("ValueError", errors[0]["exc_info"])

    def test_normal_404_is_not_treated_as_a_server_error(self):
        res = self.client.get("/this/route/does/not/exist")
        self.assertEqual(res.status_code, 404)
        lines = self._log_lines()
        # Should be logged as a normal rejected request (WARNING, status
        # 404) — NOT as an "unhandled exception" ERROR, which would mean
        # the generic exception handler incorrectly swallowed a routine
        # 404 (a real bug this exact test caught during development).
        error_lines = [l for l in lines if l["level"] == "ERROR"]
        self.assertEqual(error_lines, [])
        rejected = [l for l in lines if l["message"] == "request rejected"]
        self.assertEqual(rejected[0]["status"], 404)

    def test_request_id_present_in_header_and_matches_log_line(self):
        res = self.client.get("/api/health")
        header_id = res.headers.get("X-Request-ID")
        self.assertTrue(header_id, "X-Request-ID header should be present")

        lines = self._log_lines()
        request_lines = [l for l in lines if l["message"] == "request"]
        self.assertEqual(request_lines[0]["request_id"], header_id)

    def test_request_id_correlates_business_event_with_its_request_log(self):
        res = self.client.post(f"/api/room/{self.room}/host/claim")
        header_id = res.headers.get("X-Request-ID")

        lines = self._log_lines()
        claim_line = next(l for l in lines if l["message"] == "host claimed")
        request_line = next(l for l in lines if l["message"] == "request")

        self.assertEqual(claim_line["request_id"], header_id)
        self.assertEqual(request_line["request_id"], header_id)

    def test_different_requests_get_different_request_ids(self):
        res1 = self.client.get("/api/health")
        res2 = self.client.get("/api/health")
        self.assertNotEqual(
            res1.headers.get("X-Request-ID"), res2.headers.get("X-Request-ID")
        )

    def test_spoofed_forwarded_for_is_ignored_by_default(self):
        # PRYMAX_TRUST_PROXY isn't set in this test process, so a client
        # claiming to be some other IP via X-Forwarded-For must be
        # ignored — logging the real connecting address instead.
        self.assertFalse(ratelimit.TRUST_PROXY)
        self.client.get("/api/health", headers={"X-Forwarded-For": "1.2.3.4"})
        lines = self._log_lines()
        request_line = next(l for l in lines if l["message"] == "request")
        self.assertNotEqual(request_line["client_ip"], "1.2.3.4")

    def test_forwarded_for_is_used_when_proxy_trust_is_explicitly_enabled(self):
        # Simulates PRYMAX_TRUST_PROXY=1 by patching the already-imported
        # module's flag directly (the env var itself is only read once,
        # at import time) — confirms the *other* branch of the same
        # code path the test above exercises, so both outcomes of the
        # config flag are actually verified, not just the default one.
        original = ratelimit.TRUST_PROXY
        ratelimit.TRUST_PROXY = True
        try:
            self.client.get("/api/health", headers={"X-Forwarded-For": "1.2.3.4"})
            lines = self._log_lines()
            request_line = next(l for l in lines if l["message"] == "request")
            self.assertEqual(request_line["client_ip"], "1.2.3.4")
        finally:
            ratelimit.TRUST_PROXY = original


if __name__ == "__main__":
    unittest.main()
