"""
PryMax Studio — automated API test suite
==========================================
Uses Python's built-in `unittest` + Flask's test client — no pytest,
because this sandbox has no network access to install it. Everything here
is a real, runnable, repeatable test; nothing is simulated or asserted
without actually exercising the Flask app's routing/validation/locking
code, exactly as the manual curl checks did throughout development. This
suite formalizes those checks so they don't have to be re-run by hand
after every change.

Run:
    cd backend
    python3 -m unittest test_app.py -v

Isolation:
    - Each test reloads store.py fresh, pointed at its own temp SQLite
      file, so tests never touch the real prymax.db or leave rows behind
      — and, critically, never inherit database state left over by a
      DIFFERENT test file's tests that happened to run first in the same
      process. That per-test reload replaced an earlier, weaker version
      of this suite that only set PRYMAX_DB_PATH once at module import
      time: that assumption broke the moment another test file
      (test_migrations.py, test_concurrency.py) also reloaded store.py
      mid-run, silently repointing this suite's tests at a foreign
      database and causing failures with no obvious connection to their
      real cause. See testutil.py's docstring for the full story.
    - setUp() also clears app._rooms (the in-memory cache) and
      ratelimit._hits (the rate-limit counters) before every test.
    - Each test uses a UUID-based room id as a further layer of isolation.
"""
from __future__ import annotations

import importlib
import logging
import os
import tempfile
import time
import unittest
import uuid

import app as app_module  # noqa: E402
import ratelimit  # noqa: E402
import store  # noqa: E402
import testutil  # noqa: E402


def new_room_id() -> str:
    return "t" + uuid.uuid4().hex[:12]


class PryMaxApiTestCase(unittest.TestCase):
    def setUp(self):
        # Fresh, isolated database for this test alone — see the module
        # docstring for why this can't just be a one-time module-level
        # setup. store's connection is a process-wide singleton; app.py
        # looks up `store.whatever(...)` fresh on every call rather than
        # caching a reference, so reloading the module here is
        # immediately visible to app.py without needing to reload app
        # itself.
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        os.environ["PRYMAX_DB_PATH"] = self.tmp_db.name

        # Quiet the logger before init_db() runs, not after — init_db()
        # itself logs an INFO line listing applied migrations, and doing
        # this in the wrong order means that line leaks into every
        # single test's output instead of being suppressed like the rest.
        self._original_log_level = app_module.logger.level
        app_module.logger.setLevel(logging.CRITICAL)

        importlib.reload(store)
        store.init_db()

        self.client = app_module.app.test_client()
        app_module._rooms.clear()
        with ratelimit._lock:
            ratelimit._hits.clear()
        self.room = new_room_id()

    def tearDown(self):
        app_module.logger.setLevel(self._original_log_level)
        # Deferred to process exit, not deleted now — see testutil.py's
        # docstring for exactly why an immediate os.unlink here would
        # poison store's connection for whichever test runs next.
        testutil.track_for_cleanup(self.tmp_db.name)

    # -----------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------

    def test_health(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")

    def test_frontend_files_served(self):
        for path in ("/room", "/room.js", "/room.css"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, f"{path} did not serve")

    # -----------------------------------------------------------------
    # Presence / join / roles
    # -----------------------------------------------------------------

    def test_join_as_attendee_by_default(self):
        res = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["role"], "attendee")
        self.assertEqual(data["existing_peers"], [])

    def test_join_requires_name(self):
        res = self.client.post(f"/api/room/{self.room}/join", json={"name": ""})
        self.assertEqual(res.status_code, 400)

    def test_join_rejects_oversized_name(self):
        res = self.client.post(
            f"/api/room/{self.room}/join", json={"name": "x" * 150}
        )
        self.assertEqual(res.status_code, 400)

    def test_second_peer_sees_first_in_existing_peers(self):
        self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        res = self.client.post(f"/api/room/{self.room}/join", json={"name": "Bob"})
        existing_names = [p["name"] for p in res.get_json()["existing_peers"]]
        self.assertIn("Alice", existing_names)

    def test_peers_list_reflects_roles(self):
        token = self._claim_host()
        self.client.post(
            f"/api/room/{self.room}/join", json={"name": "Alice", "host_token": token}
        )
        self.client.post(f"/api/room/{self.room}/join", json={"name": "Bob"})
        res = self.client.get(f"/api/room/{self.room}/peers")
        roles = {p["name"]: p["role"] for p in res.get_json()}
        self.assertEqual(roles["Alice"], "host")
        self.assertEqual(roles["Bob"], "attendee")

    def test_join_returns_a_peer_secret(self):
        res = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        data = res.get_json()
        self.assertIn("peer_secret", data)
        self.assertGreater(len(data["peer_secret"]), 10)

    def test_peers_listing_never_leaks_the_secret(self):
        self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        res = self.client.get(f"/api/room/{self.room}/peers")
        body_text = res.get_data(as_text=True)
        self.assertNotIn("secret", body_text.lower())

    def test_leave_requires_own_peer_secret(self):
        """Regression test for a real vulnerability found via live testing:
        /peers exposes every peer_id to every room member, and /leave
        originally trusted a bare peer_id with no proof of ownership,
        letting any attendee forcibly disconnect any other peer."""
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()

        no_secret = self.client.post(
            f"/api/room/{self.room}/leave", json={"peer_id": alice["peer_id"]}
        )
        self.assertEqual(no_secret.status_code, 403)

        wrong_secret = self.client.post(
            f"/api/room/{self.room}/leave",
            json={"peer_id": alice["peer_id"], "peer_secret": "guessed-wrong"},
        )
        self.assertEqual(wrong_secret.status_code, 403)

        still_present = self.client.get(f"/api/room/{self.room}/peers").get_json()
        self.assertTrue(any(p["peer_id"] == alice["peer_id"] for p in still_present))

        real_leave = self.client.post(
            f"/api/room/{self.room}/leave",
            json={"peer_id": alice["peer_id"], "peer_secret": alice["peer_secret"]},
        )
        self.assertEqual(real_leave.status_code, 200)
        after = self.client.get(f"/api/room/{self.room}/peers").get_json()
        self.assertFalse(any(p["peer_id"] == alice["peer_id"] for p in after))

    # -----------------------------------------------------------------
    # Signaling relay
    # -----------------------------------------------------------------

    def test_signaling_relay_pass_through(self):
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()
        bob = self.client.post(f"/api/room/{self.room}/join", json={"name": "Bob"}).get_json()
        pa, pb = alice["peer_id"], bob["peer_id"]

        res = self.client.post(
            f"/api/room/{self.room}/signal",
            json={
                "to": pa,
                "from": pb,
                "from_secret": bob["peer_secret"],
                "type": "offer",
                "payload": {"sdp": "fake"},
            },
        )
        self.assertEqual(res.status_code, 200)

        inbox = self.client.get(
            f"/api/room/{self.room}/signal/{pa}?secret={alice['peer_secret']}"
        ).get_json()
        types = [m["type"] for m in inbox]
        self.assertIn("offer", types)
        self.assertIn("peer-joined", types)  # from Bob joining after Alice

    def test_signal_requires_sender_proof(self):
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()
        bob = self.client.post(f"/api/room/{self.room}/join", json={"name": "Bob"}).get_json()

        no_secret = self.client.post(
            f"/api/room/{self.room}/signal",
            json={"to": alice["peer_id"], "from": bob["peer_id"], "type": "offer", "payload": {}},
        )
        self.assertEqual(no_secret.status_code, 403)

        wrong_secret = self.client.post(
            f"/api/room/{self.room}/signal",
            json={
                "to": alice["peer_id"],
                "from": bob["peer_id"],
                "from_secret": "not-the-real-secret",
                "type": "offer",
                "payload": {},
            },
        )
        self.assertEqual(wrong_secret.status_code, 403)

    def test_signal_inbox_requires_own_secret(self):
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()

        no_secret = self.client.get(f"/api/room/{self.room}/signal/{alice['peer_id']}")
        self.assertEqual(no_secret.status_code, 403)

        wrong_secret = self.client.get(
            f"/api/room/{self.room}/signal/{alice['peer_id']}?secret=wrong"
        )
        self.assertEqual(wrong_secret.status_code, 403)

        right_secret = self.client.get(
            f"/api/room/{self.room}/signal/{alice['peer_id']}?secret={alice['peer_secret']}"
        )
        self.assertEqual(right_secret.status_code, 200)

    def test_signal_to_unknown_peer_is_404(self):
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()
        res = self.client.post(
            f"/api/room/{self.room}/signal",
            json={
                "to": "does-not-exist",
                "from": alice["peer_id"],
                "from_secret": alice["peer_secret"],
                "type": "offer",
                "payload": {},
            },
        )
        self.assertEqual(res.status_code, 404)

    # -----------------------------------------------------------------
    # Chat
    # -----------------------------------------------------------------

    def test_chat_post_and_since_filter(self):
        r1 = self.client.post(
            f"/api/room/{self.room}/chat", json={"name": "Alice", "message": "hi"}
        ).get_json()
        self.client.post(
            f"/api/room/{self.room}/chat", json={"name": "Bob", "message": "hey"}
        )
        res = self.client.get(f"/api/room/{self.room}/chat?since={r1['id']}")
        messages = res.get_json()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["name"], "Bob")

    def test_chat_rejects_empty_message(self):
        res = self.client.post(
            f"/api/room/{self.room}/chat", json={"name": "Alice", "message": "  "}
        )
        self.assertEqual(res.status_code, 400)

    # -----------------------------------------------------------------
    # Polls
    # -----------------------------------------------------------------

    def test_poll_create_vote_and_double_vote_rejected(self):
        poll = self.client.post(
            f"/api/room/{self.room}/poll",
            json={"question": "Best pillar?", "options": ["AI", "Broadcast"]},
        ).get_json()

        res = self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/vote",
            json={"name": "Alice", "option_index": 1},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["votes"], [0, 1])

        dupe = self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/vote",
            json={"name": "Alice", "option_index": 0},
        )
        self.assertEqual(dupe.status_code, 400)

    def test_poll_requires_two_to_ten_options(self):
        too_few = self.client.post(
            f"/api/room/{self.room}/poll", json={"question": "Q", "options": ["only"]}
        )
        self.assertEqual(too_few.status_code, 400)

        too_many = self.client.post(
            f"/api/room/{self.room}/poll",
            json={"question": "Q", "options": [f"opt{i}" for i in range(11)]},
        )
        self.assertEqual(too_many.status_code, 400)

    def test_poll_close_requires_host_token(self):
        poll = self.client.post(
            f"/api/room/{self.room}/poll",
            json={"question": "Q", "options": ["A", "B"]},
        ).get_json()

        unauthorized = self.client.post(f"/api/room/{self.room}/poll/{poll['id']}/close")
        self.assertEqual(unauthorized.status_code, 403)

        token = self._claim_host()
        authorized = self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/close",
            json={"host_token": token},
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertFalse(authorized.get_json()["open"])

    def test_vote_rejected_after_close(self):
        poll = self.client.post(
            f"/api/room/{self.room}/poll",
            json={"question": "Q", "options": ["A", "B"]},
        ).get_json()
        token = self._claim_host()
        self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/close",
            json={"host_token": token},
        )
        res = self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/vote",
            json={"name": "Alice", "option_index": 0},
        )
        self.assertEqual(res.status_code, 400)

    # -----------------------------------------------------------------
    # Q&A
    # -----------------------------------------------------------------

    def test_qa_post_upvote_and_answered_requires_host(self):
        q = self.client.post(
            f"/api/room/{self.room}/qa", json={"name": "Bob", "question": "Does it scale?"}
        ).get_json()

        up = self.client.post(
            f"/api/room/{self.room}/qa/{q['id']}/upvote", json={"name": "Alice"}
        )
        self.assertEqual(up.get_json()["upvotes"], 1)

        dupe = self.client.post(
            f"/api/room/{self.room}/qa/{q['id']}/upvote", json={"name": "Alice"}
        )
        self.assertEqual(dupe.status_code, 400)

        unauthorized = self.client.post(f"/api/room/{self.room}/qa/{q['id']}/answered")
        self.assertEqual(unauthorized.status_code, 403)

        token = self._claim_host()
        authorized = self.client.post(
            f"/api/room/{self.room}/qa/{q['id']}/answered", json={"host_token": token}
        )
        self.assertTrue(authorized.get_json()["answered"])

    def test_qa_sorted_by_upvotes_desc(self):
        q1 = self.client.post(
            f"/api/room/{self.room}/qa", json={"name": "A", "question": "one"}
        ).get_json()
        q2 = self.client.post(
            f"/api/room/{self.room}/qa", json={"name": "B", "question": "two"}
        ).get_json()
        self.client.post(f"/api/room/{self.room}/qa/{q2['id']}/upvote", json={"name": "X"})

        ordered = self.client.get(f"/api/room/{self.room}/qa").get_json()
        self.assertEqual(ordered[0]["id"], q2["id"])
        self.assertEqual(ordered[1]["id"], q1["id"])

    # -----------------------------------------------------------------
    # Reactions
    # -----------------------------------------------------------------

    def test_reaction_since_filter(self):
        r1 = self.client.post(
            f"/api/room/{self.room}/reaction", json={"name": "A", "emoji": "👏"}
        ).get_json()
        self.client.post(f"/api/room/{self.room}/reaction", json={"name": "B", "emoji": "🎉"})
        res = self.client.get(f"/api/room/{self.room}/reaction?since={r1['id']}")
        self.assertEqual(len(res.get_json()), 1)

    # -----------------------------------------------------------------
    # Raffle
    # -----------------------------------------------------------------

    def test_raffle_dedupes_entries_and_draw_requires_host(self):
        self.client.post(f"/api/room/{self.room}/raffle/enter", json={"name": "Alice"})
        self.client.post(f"/api/room/{self.room}/raffle/enter", json={"name": "Alice"})
        self.client.post(f"/api/room/{self.room}/raffle/enter", json={"name": "Bob"})
        entries = self.client.get(f"/api/room/{self.room}/raffle").get_json()["entries"]
        self.assertEqual(sorted(entries), ["Alice", "Bob"])

        unauthorized = self.client.post(f"/api/room/{self.room}/raffle/draw")
        self.assertEqual(unauthorized.status_code, 403)

        token = self._claim_host()
        drawn = self.client.post(
            f"/api/room/{self.room}/raffle/draw", json={"host_token": token}
        )
        self.assertIn(drawn.get_json()["winner"], ["Alice", "Bob"])

    def test_raffle_draw_with_no_entries_fails(self):
        token = self._claim_host()
        res = self.client.post(
            f"/api/room/{self.room}/raffle/draw", json={"host_token": token}
        )
        self.assertEqual(res.status_code, 400)

    # -----------------------------------------------------------------
    # Whiteboard
    # -----------------------------------------------------------------

    def test_whiteboard_stroke_and_clear_requires_host(self):
        res = self.client.post(
            f"/api/room/{self.room}/whiteboard/stroke",
            json={"points": [[0, 0], [1, 1]], "color": "#fff", "width": 3},
        )
        self.assertEqual(res.status_code, 200)

        unauthorized = self.client.post(f"/api/room/{self.room}/whiteboard/clear")
        self.assertEqual(unauthorized.status_code, 403)

        token = self._claim_host()
        authorized = self.client.post(
            f"/api/room/{self.room}/whiteboard/clear", json={"host_token": token}
        )
        self.assertEqual(authorized.status_code, 200)
        strokes = self.client.get(f"/api/room/{self.room}/whiteboard?since=0").get_json()
        self.assertEqual(strokes, [])

    def test_whiteboard_rejects_bad_points(self):
        bad_shape = self.client.post(
            f"/api/room/{self.room}/whiteboard/stroke",
            json={"points": [["a", "b"]], "color": "#fff", "width": 3},
        )
        self.assertEqual(bad_shape.status_code, 400)

        too_wide = self.client.post(
            f"/api/room/{self.room}/whiteboard/stroke",
            json={"points": [[0, 0], [1, 1]], "color": "#fff", "width": 9999},
        )
        self.assertEqual(too_wide.status_code, 400)

        too_many_points = self.client.post(
            f"/api/room/{self.room}/whiteboard/stroke",
            json={"points": [[i, i] for i in range(2500)], "color": "#fff", "width": 3},
        )
        self.assertEqual(too_many_points.status_code, 400)

    # -----------------------------------------------------------------
    # Breakout rooms
    # -----------------------------------------------------------------

    def test_breakout_round_robin_assignment(self):
        peers = [
            self.client.post(f"/api/room/{self.room}/join", json={"name": n}).get_json()
            for n in ("A", "B", "C", "D")
        ]
        token = self._claim_host()
        start = self.client.post(
            f"/api/room/{self.room}/breakout/start",
            json={"host_token": token, "count": 2, "duration_seconds": 300},
        )
        self.assertEqual(start.status_code, 200)

        assignments = {
            p["peer_id"]: self.client.get(
                f"/api/room/{self.room}/breakout/{p['peer_id']}?secret={p['peer_secret']}"
            ).get_json()["group_index"]
            for p in peers
        }
        # Round-robin over 4 peers into 2 groups: alternating 0,1,0,1
        self.assertEqual([assignments[p["peer_id"]] for p in peers], [0, 1, 0, 1])

    def test_breakout_assignment_requires_own_secret(self):
        alice = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"}).get_json()
        token = self._claim_host()
        self.client.post(
            f"/api/room/{self.room}/breakout/start",
            json={"host_token": token, "count": 1, "duration_seconds": 300},
        )
        no_secret = self.client.get(f"/api/room/{self.room}/breakout/{alice['peer_id']}")
        self.assertEqual(no_secret.status_code, 403)

    def test_breakout_start_requires_host(self):
        res = self.client.post(
            f"/api/room/{self.room}/breakout/start", json={"count": 2}
        )
        self.assertEqual(res.status_code, 403)

    def test_breakout_auto_expires(self):
        token = self._claim_host()
        peer = self.client.post(f"/api/room/{self.room}/join", json={"name": "A"}).get_json()
        self.client.post(
            f"/api/room/{self.room}/breakout/start",
            json={"host_token": token, "count": 1, "duration_seconds": 300},
        )
        # Force expiry without sleeping 300s: back-date started_at directly
        # on the in-memory room, exactly as real elapsed time would.
        room = app_module._get_room(self.room)
        room.breakout["started_at"] = time.time() - 301

        status = self.client.get(
            f"/api/room/{self.room}/breakout/{peer['peer_id']}?secret={peer['peer_secret']}"
        ).get_json()
        self.assertFalse(status["active"])

    # -----------------------------------------------------------------
    # Session profile
    # -----------------------------------------------------------------

    def test_profile_set_requires_host_and_validates_enum(self):
        unauthorized = self.client.put(
            f"/api/room/{self.room}/profile", json={"mode": "srt"}
        )
        self.assertEqual(unauthorized.status_code, 403)

        token = self._claim_host()
        bad_mode = self.client.put(
            f"/api/room/{self.room}/profile",
            json={"host_token": token, "mode": "betamax"},
        )
        self.assertEqual(bad_mode.status_code, 400)

        good = self.client.put(
            f"/api/room/{self.room}/profile",
            json={"host_token": token, "mode": "srt", "title": "Q3 Briefing"},
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.get_json()["mode"], "srt")

    # -----------------------------------------------------------------
    # Host auth
    # -----------------------------------------------------------------

    def test_double_claim_rejected(self):
        self._claim_host()
        res = self.client.post(f"/api/room/{self.room}/host/claim")
        self.assertEqual(res.status_code, 409)

    def test_verify_rejects_wrong_token(self):
        self._claim_host()
        res = self.client.post(
            f"/api/room/{self.room}/host/verify", json={"host_token": "not-the-token"}
        )
        self.assertFalse(res.get_json()["is_host"])

    def test_claim_response_includes_expires_at(self):
        res = self.client.post(f"/api/room/{self.room}/host/claim")
        data = res.get_json()
        self.assertIn("expires_at", data)
        # Default TTL is positive (7 days), so a real deadline is expected
        # unless the test process itself set PRYMAX_HOST_TOKEN_TTL_SECONDS=0.
        if app_module.HOST_TOKEN_TTL_SECONDS > 0:
            self.assertIsNotNone(data["expires_at"])
            self.assertGreater(data["expires_at"], time.time())

    def test_token_expires_after_ttl(self):
        original_ttl = app_module.HOST_TOKEN_TTL_SECONDS
        app_module.HOST_TOKEN_TTL_SECONDS = 0.5
        try:
            token = self._claim_host()

            still_valid = self.client.post(
                f"/api/room/{self.room}/host/verify", json={"host_token": token}
            ).get_json()
            self.assertTrue(still_valid["is_host"])

            time.sleep(0.7)

            expired = self.client.post(
                f"/api/room/{self.room}/host/verify", json={"host_token": token}
            ).get_json()
            self.assertFalse(expired["is_host"])

            # A privileged action must also be rejected once expired, not
            # just the /verify endpoint's own report.
            privileged = self.client.put(
                f"/api/room/{self.room}/profile", json={"host_token": token, "title": "x"}
            )
            self.assertEqual(privileged.status_code, 403)
        finally:
            app_module.HOST_TOKEN_TTL_SECONDS = original_ttl

    def test_ttl_zero_disables_expiry(self):
        original_ttl = app_module.HOST_TOKEN_TTL_SECONDS
        app_module.HOST_TOKEN_TTL_SECONDS = 0
        try:
            res = self.client.post(f"/api/room/{self.room}/host/claim")
            self.assertIsNone(res.get_json()["expires_at"])
        finally:
            app_module.HOST_TOKEN_TTL_SECONDS = original_ttl

    def test_revoke_requires_valid_token_and_invalidates_it(self):
        token = self._claim_host()

        unauthorized = self.client.post(f"/api/room/{self.room}/host/revoke", json={})
        self.assertEqual(unauthorized.status_code, 403)

        authorized = self.client.post(
            f"/api/room/{self.room}/host/revoke", json={"host_token": token}
        )
        self.assertEqual(authorized.status_code, 200)

        verify = self.client.post(
            f"/api/room/{self.room}/host/verify", json={"host_token": token}
        ).get_json()
        self.assertFalse(verify["is_host"], "revoked token must no longer verify")

    def test_room_reclaimable_after_revoke(self):
        token = self._claim_host()
        self.client.post(f"/api/room/{self.room}/host/revoke", json={"host_token": token})

        reclaim = self.client.post(f"/api/room/{self.room}/host/claim")
        self.assertEqual(reclaim.status_code, 200)
        self.assertNotEqual(reclaim.get_json()["host_token"], token)

    # -----------------------------------------------------------------
    # Room ID validation
    # -----------------------------------------------------------------

    def test_room_id_with_spaces_rejected(self):
        res = self.client.post("/api/room/bad room/join", json={"name": "A"})
        self.assertEqual(res.status_code, 400)

    def test_breakout_room_id_format_accepted(self):
        res = self.client.get(f"/api/room/{self.room}::bo0/peers")
        self.assertEqual(res.status_code, 200)

    # -----------------------------------------------------------------
    # Rate limiting
    # -----------------------------------------------------------------

    def test_chat_rate_limit_triggers_and_recovers(self):
        codes = []
        for i in range(25):
            res = self.client.post(
                f"/api/room/{self.room}/chat",
                json={"name": "A", "message": f"msg{i}"},
            )
            codes.append(res.status_code)
        self.assertEqual(codes[:20], [200] * 20)
        self.assertTrue(all(c == 429 for c in codes[20:]))

        # Simulate the window passing without a real sleep in the test.
        with ratelimit._lock:
            ratelimit._hits[("chat", "127.0.0.1")].clear()
        recovered = self.client.post(
            f"/api/room/{self.room}/chat", json={"name": "A", "message": "after window"}
        )
        self.assertEqual(recovered.status_code, 200)

    # -----------------------------------------------------------------
    # Persistence — exercises the exact rehydration path a real process
    # restart would run, without needing to actually kill this process.
    # -----------------------------------------------------------------

    def test_data_survives_cache_eviction_and_rehydration(self):
        token = self._claim_host()
        self.client.put(
            f"/api/room/{self.room}/profile",
            json={"host_token": token, "title": "Persisted Title", "mode": "ndi"},
        )
        self.client.post(
            f"/api/room/{self.room}/chat", json={"name": "Alice", "message": "before"}
        )
        poll = self.client.post(
            f"/api/room/{self.room}/poll",
            json={"question": "Persist?", "options": ["Yes", "No"]},
        ).get_json()
        self.client.post(
            f"/api/room/{self.room}/poll/{poll['id']}/vote",
            json={"name": "Alice", "option_index": 0},
        )

        # Simulate a restart: drop the in-memory cache entirely — the next
        # access must rehydrate from store.py, same code path a real
        # process restart would take.
        del app_module._rooms[self.room]

        profile = self.client.get(f"/api/room/{self.room}/profile").get_json()
        self.assertEqual(profile["title"], "Persisted Title")
        self.assertEqual(profile["mode"], "ndi")

        chat = self.client.get(f"/api/room/{self.room}/chat?since=0").get_json()
        self.assertEqual(chat[0]["message"], "before")

        polls = self.client.get(f"/api/room/{self.room}/poll").get_json()
        self.assertEqual(polls[0]["votes"], [1, 0])

        verify = self.client.post(
            f"/api/room/{self.room}/host/verify", json={"host_token": token}
        )
        self.assertTrue(verify.get_json()["is_host"])

    # -----------------------------------------------------------------
    # Admin login
    # -----------------------------------------------------------------

    def test_admin_login_with_correct_credentials_returns_host_token(self):
        res = self.client.post(
            "/api/admin/login", json={"username": "Ibitoye", "room": "Superroom"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("host_token", data)
        self.assertEqual(data["room_id"], "Superroom")
        self.assertEqual(data["role"], "admin")

    def test_admin_login_with_wrong_credentials_rejected(self):
        wrong_user = self.client.post(
            "/api/admin/login", json={"username": "NotIbitoye", "room": "Superroom"}
        )
        self.assertEqual(wrong_user.status_code, 401)

        wrong_room = self.client.post(
            "/api/admin/login", json={"username": "Ibitoye", "room": "WrongRoom"}
        )
        self.assertEqual(wrong_room.status_code, 401)

        empty = self.client.post("/api/admin/login", json={})
        self.assertEqual(empty.status_code, 401)

    def test_admin_login_grants_working_host_privileges(self):
        login = self.client.post(
            "/api/admin/login", json={"username": "Ibitoye", "room": "Superroom"}
        ).get_json()
        token = login["host_token"]

        join = self.client.post(
            "/api/room/Superroom/join", json={"name": "Ibitoye", "host_token": token}
        ).get_json()
        self.assertEqual(join["role"], "host")

        profile = self.client.put(
            "/api/room/Superroom/profile", json={"host_token": token, "title": "Test"}
        )
        self.assertEqual(profile.status_code, 200)

    def test_admin_login_is_repeatable_not_claim_once(self):
        """Unlike the normal /host/claim flow, admin login must succeed
        every time with the right credentials — it's a repeatable login,
        not a one-time claim."""
        first = self.client.post(
            "/api/admin/login", json={"username": "Ibitoye", "room": "Superroom"}
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/admin/login", json={"username": "Ibitoye", "room": "Superroom"}
        )
        self.assertEqual(second.status_code, 200)
        # Each login issues a fresh token; the old one should no longer work.
        old_token = first.get_json()["host_token"]
        still_valid = self.client.post(
            "/api/room/Superroom/host/verify", json={"host_token": old_token}
        ).get_json()
        self.assertFalse(still_valid["is_host"])

    # -----------------------------------------------------------------
    # Scheduled meetings & live streaming
    # -----------------------------------------------------------------

    def test_schedule_meeting_returns_working_credentials(self):
        res = self.client.post(
            "/api/meetings/schedule", json={"name": "Host", "title": "Weekly Sync"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertRegex(data["meeting_id"], r"^\d{11}$")
        self.assertRegex(data["passcode"], r"^[A-Z0-9]{6}$")
        self.assertIn("host_token", data)
        self.assertIn(data["meeting_id"], data["join_link"])
        self.assertIn(data["passcode"], data["join_link"])
        self.assertEqual(data["type"], "meeting")

    def test_schedule_meeting_ids_are_unique(self):
        ids = set()
        for _ in range(10):
            res = self.client.post("/api/meetings/schedule", json={"name": "Host"})
            ids.add(res.get_json()["meeting_id"])
        self.assertEqual(len(ids), 10)

    def test_live_stream_sets_broadcast_profile_mode(self):
        res = self.client.post(
            "/api/meetings/schedule", json={"name": "Streamer", "type": "stream"}
        )
        data = res.get_json()
        self.assertEqual(data["type"], "stream")
        profile = self.client.get(f"/api/room/{data['meeting_id']}/profile").get_json()
        self.assertEqual(profile["mode"], "rtmp")

    def test_join_scheduled_meeting_requires_correct_passcode(self):
        meeting = self.client.post(
            "/api/meetings/schedule", json={"name": "Host"}
        ).get_json()
        mid = meeting["meeting_id"]

        no_pass = self.client.post(f"/api/room/{mid}/join", json={"name": "Attendee"})
        self.assertEqual(no_pass.status_code, 403)

        wrong_pass = self.client.post(
            f"/api/room/{mid}/join", json={"name": "Attendee", "passcode": "WRONG1"}
        )
        self.assertEqual(wrong_pass.status_code, 403)

        right_pass = self.client.post(
            f"/api/room/{mid}/join", json={"name": "Attendee", "passcode": meeting["passcode"]}
        )
        self.assertEqual(right_pass.status_code, 200)
        self.assertEqual(right_pass.get_json()["role"], "attendee")

    def test_host_can_rejoin_scheduled_meeting_without_passcode(self):
        meeting = self.client.post(
            "/api/meetings/schedule", json={"name": "Host"}
        ).get_json()
        rejoin = self.client.post(
            f"/api/room/{meeting['meeting_id']}/join",
            json={"name": "Host", "host_token": meeting["host_token"]},
        )
        self.assertEqual(rejoin.status_code, 200)
        self.assertEqual(rejoin.get_json()["role"], "host")

    def test_verify_meeting_endpoint(self):
        meeting = self.client.post(
            "/api/meetings/schedule", json={"name": "Host", "title": "Verify Test"}
        ).get_json()
        mid = meeting["meeting_id"]

        wrong = self.client.post(
            "/api/meetings/verify", json={"meeting_id": mid, "passcode": "WRONG1"}
        )
        self.assertEqual(wrong.status_code, 403)

        right = self.client.post(
            "/api/meetings/verify", json={"meeting_id": mid, "passcode": meeting["passcode"]}
        )
        self.assertEqual(right.status_code, 200)
        self.assertEqual(right.get_json()["title"], "Verify Test")

        # Formatted (spaced) meeting id, as displayed to the user, must
        # also work since the frontend/user may paste it with spaces.
        spaced = self.client.post(
            "/api/meetings/verify",
            json={"meeting_id": meeting["meeting_id_display"], "passcode": meeting["passcode"]},
        )
        self.assertEqual(spaced.status_code, 200)

        nonexistent = self.client.post(
            "/api/meetings/verify", json={"meeting_id": "99999999999", "passcode": "X"}
        )
        self.assertEqual(nonexistent.status_code, 404)

    def test_ad_hoc_rooms_are_unaffected_by_the_passcode_feature(self):
        """A plain /join on an ordinary (non-scheduled) room must keep
        working exactly as before — no passcode required."""
        res = self.client.post(f"/api/room/{self.room}/join", json={"name": "Alice"})
        self.assertEqual(res.status_code, 200)

    # -----------------------------------------------------------------
    # In-meeting sharing (meeting info, for inviting more people mid-call)
    # -----------------------------------------------------------------

    def test_meeting_info_requires_peer_authentication(self):
        meeting = self.client.post(
            "/api/meetings/schedule", json={"name": "Host"}
        ).get_json()
        peer = self.client.post(
            f"/api/room/{meeting['meeting_id']}/join",
            json={"name": "Attendee", "passcode": meeting["passcode"]},
        ).get_json()

        no_secret = self.client.get(
            f"/api/room/{meeting['meeting_id']}/meeting-info/{peer['peer_id']}"
        )
        self.assertEqual(no_secret.status_code, 403)

        wrong_secret = self.client.get(
            f"/api/room/{meeting['meeting_id']}/meeting-info/{peer['peer_id']}?secret=wrong"
        )
        self.assertEqual(wrong_secret.status_code, 403)

    def test_meeting_info_returns_shareable_details_for_an_authenticated_peer(self):
        meeting = self.client.post(
            "/api/meetings/schedule", json={"name": "Host", "title": "Standup"}
        ).get_json()
        peer = self.client.post(
            f"/api/room/{meeting['meeting_id']}/join",
            json={"name": "Attendee", "passcode": meeting["passcode"]},
        ).get_json()

        res = self.client.get(
            f"/api/room/{meeting['meeting_id']}/meeting-info/{peer['peer_id']}"
            f"?secret={peer['peer_secret']}"
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["meeting_id"], meeting["meeting_id"])
        self.assertEqual(data["passcode"], meeting["passcode"])
        self.assertEqual(data["title"], "Standup")
        self.assertIn(meeting["meeting_id"], data["join_link"])
        self.assertIn(meeting["passcode"], data["join_link"])

    def test_meeting_info_for_ad_hoc_room_has_no_passcode(self):
        peer = self.client.post(
            f"/api/room/{self.room}/join", json={"name": "Alice"}
        ).get_json()
        res = self.client.get(
            f"/api/room/{self.room}/meeting-info/{peer['peer_id']}?secret={peer['peer_secret']}"
        )
        data = res.get_json()
        self.assertIsNone(data["passcode"])
        self.assertNotIn("passcode=", data["join_link"])

    # -----------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------

    def _claim_host(self) -> str:
        res = self.client.post(f"/api/room/{self.room}/host/claim")
        return res.get_json()["host_token"]


if __name__ == "__main__":
    unittest.main()
