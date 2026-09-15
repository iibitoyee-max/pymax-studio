"""
PryMax Studio — durable storage
================================
SQLite via Python's built-in sqlite3 module — no new dependency. Backs the
data that should survive a server restart: chat, polls, Q&A, whiteboard
strokes, raffle state, the host token, and the session profile.

Deliberately NOT persisted here (stays in the in-memory Room object in
app.py): live peer presence, WebRTC signaling queues, floating reactions,
and breakout-room assignments. Those are legitimately ephemeral — a
browser's WebRTC connection can't survive a server restart regardless of
what's in a database, so persisting "peer X was connected" would just be
stale data on next boot. This mirrors the Redis (ephemeral) / Postgres
(durable) split already described in ../README.md, just with SQLite
standing in for Postgres at this scale.

Concurrency: Flask's dev server can be single- or multi-threaded depending
on how it's run. This module uses one shared connection with
check_same_thread=False, guarded by a module-level lock, so it's safe
either way without requiring a connection pool.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from threading import Lock

import migrations

DB_PATH = os.environ.get("PRYMAX_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "prymax.db"
)

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_db_lock = Lock()


def init_db() -> None:
    with _db_lock, _conn:
        applied = migrations.run_migrations(_conn)
    if applied:
        import logging

        logging.getLogger("prymax").info(
            "database migrations applied", extra={"fields": {"migration_ids": applied}}
        )


def _touch_room_activity(room_id: str) -> None:
    """Called from every write path below — keeps rooms.last_activity_at
    current for future backup/retention tooling to use (e.g. "only back
    up/prune rooms active in the last N days"). Must be called from
    inside an already-held _db_lock/_conn transaction, same as every
    other write in this file."""
    _conn.execute(
        "UPDATE rooms SET last_activity_at = ? WHERE room_id = ?", (time.time(), room_id)
    )


def next_start_id() -> int:
    """Highest id already used across every id-bearing table, so the
    in-process counter in app.py picks up where the last run left off
    instead of colliding with rows already on disk."""
    with _db_lock:
        cur = _conn.execute(
            """
            SELECT MAX(m) FROM (
                SELECT MAX(id) AS m FROM chat_messages
                UNION ALL SELECT MAX(id) FROM polls
                UNION ALL SELECT MAX(id) FROM questions
                UNION ALL SELECT MAX(id) FROM whiteboard_strokes
            )
            """
        )
        row = cur.fetchone()
        highest = row[0] if row and row[0] is not None else 0
        return highest + 1


def _ensure_room_row(room_id: str, created_at: float) -> None:
    _conn.execute(
        "INSERT OR IGNORE INTO rooms (room_id, created_at) VALUES (?, ?)",
        (room_id, created_at),
    )


def load_room(room_id: str, created_at: float) -> dict:
    """Load everything durable for a room. Creates the room's row if this
    is the first time it's been seen (including on a fresh DB)."""
    with _db_lock, _conn:
        _ensure_room_row(room_id, created_at)

        room_row = _conn.execute(
            "SELECT * FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()

        chat = [
            {"id": r["id"], "name": r["name"], "message": r["message"], "ts": r["ts"]}
            for r in _conn.execute(
                "SELECT * FROM chat_messages WHERE room_id = ? ORDER BY id", (room_id,)
            )
        ]

        polls = [
            {
                "id": r["id"],
                "question": r["question"],
                "options": json.loads(r["options_json"]),
                "votes": json.loads(r["votes_json"]),
                "voters": json.loads(r["voters_json"]),
                "open": bool(r["is_open"]),
                "created_at": r["created_at"],
            }
            for r in _conn.execute(
                "SELECT * FROM polls WHERE room_id = ? ORDER BY id", (room_id,)
            )
        ]

        questions = [
            {
                "id": r["id"],
                "name": r["name"],
                "question": r["question"],
                "upvotes": r["upvotes"],
                "upvoted_by": json.loads(r["upvoted_by_json"]),
                "answered": bool(r["answered"]),
                "ts": r["ts"],
            }
            for r in _conn.execute(
                "SELECT * FROM questions WHERE room_id = ? ORDER BY id", (room_id,)
            )
        ]

        whiteboard = [
            {
                "id": r["id"],
                "points": json.loads(r["points_json"]),
                "color": r["color"],
                "width": r["width"],
            }
            for r in _conn.execute(
                "SELECT * FROM whiteboard_strokes WHERE room_id = ? ORDER BY id",
                (room_id,),
            )
        ]

        return {
            "host_token_hash": room_row["host_token_hash"],
            "host_token_expires_at": room_row["host_token_expires_at"],
            "profile": {
                "title": room_row["profile_title"],
                "mode": room_row["profile_mode"],
                "video_quality": room_row["profile_video_quality"],
                "audio_format": room_row["profile_audio_format"],
            },
            "chat": chat,
            "polls": polls,
            "questions": questions,
            "whiteboard": whiteboard,
            "raffle_entries": json.loads(room_row["raffle_entries_json"]),
            "raffle_winner": room_row["raffle_winner"],
        }


def save_host_token_hash(room_id: str, token_hash: str, expires_at: float | None) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "UPDATE rooms SET host_token_hash = ?, host_token_expires_at = ? WHERE room_id = ?",
            (token_hash, expires_at, room_id),
        )
        _touch_room_activity(room_id)


def revoke_host_token(room_id: str) -> None:
    """Clears the host token entirely, returning the room to unclaimed —
    the current host can call this (proving possession of the still-valid
    token) if they suspect it's leaked, immediately invalidating it and
    allowing a fresh /host/claim. This is real revocation, not just
    waiting out an expiry."""
    with _db_lock, _conn:
        _conn.execute(
            "UPDATE rooms SET host_token_hash = NULL, host_token_expires_at = NULL WHERE room_id = ?",
            (room_id,),
        )
        _touch_room_activity(room_id)


def save_profile(room_id: str, profile: dict) -> None:
    with _db_lock, _conn:
        _conn.execute(
            """UPDATE rooms SET profile_title = ?, profile_mode = ?,
               profile_video_quality = ?, profile_audio_format = ?
               WHERE room_id = ?""",
            (
                profile["title"],
                profile["mode"],
                profile["video_quality"],
                profile["audio_format"],
                room_id,
            ),
        )
        _touch_room_activity(room_id)


def add_chat(room_id: str, entry: dict) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "INSERT INTO chat_messages (id, room_id, name, message, ts) VALUES (?, ?, ?, ?, ?)",
            (entry["id"], room_id, entry["name"], entry["message"], entry["ts"]),
        )
        _touch_room_activity(room_id)


def add_poll(room_id: str, poll: dict) -> None:
    with _db_lock, _conn:
        _conn.execute(
            """INSERT INTO polls (id, room_id, question, options_json, votes_json,
               voters_json, is_open, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                poll["id"],
                room_id,
                poll["question"],
                json.dumps(poll["options"]),
                json.dumps(poll["votes"]),
                json.dumps(poll["voters"]),
                int(poll["open"]),
                poll["created_at"],
            ),
        )
        _touch_room_activity(room_id)


def update_poll(poll: dict, room_id: str | None = None) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "UPDATE polls SET votes_json = ?, voters_json = ?, is_open = ? WHERE id = ?",
            (json.dumps(poll["votes"]), json.dumps(poll["voters"]), int(poll["open"]), poll["id"]),
        )
        if room_id:
            _touch_room_activity(room_id)


def add_question(room_id: str, q: dict) -> None:
    with _db_lock, _conn:
        _conn.execute(
            """INSERT INTO questions (id, room_id, name, question, upvotes,
               upvoted_by_json, answered, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                q["id"],
                room_id,
                q["name"],
                q["question"],
                q["upvotes"],
                json.dumps(q["upvoted_by"]),
                int(q["answered"]),
                q["ts"],
            ),
        )
        _touch_room_activity(room_id)


def update_question(q: dict, room_id: str | None = None) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "UPDATE questions SET upvotes = ?, upvoted_by_json = ?, answered = ? WHERE id = ?",
            (q["upvotes"], json.dumps(q["upvoted_by"]), int(q["answered"]), q["id"]),
        )
        if room_id:
            _touch_room_activity(room_id)


def add_stroke(room_id: str, stroke: dict) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "INSERT INTO whiteboard_strokes (id, room_id, points_json, color, width) VALUES (?, ?, ?, ?, ?)",
            (stroke["id"], room_id, json.dumps(stroke["points"]), stroke["color"], stroke["width"]),
        )
        _touch_room_activity(room_id)


def clear_strokes(room_id: str) -> None:
    with _db_lock, _conn:
        _conn.execute("DELETE FROM whiteboard_strokes WHERE room_id = ?", (room_id,))
        _touch_room_activity(room_id)


def save_raffle(room_id: str, entries: list, winner: str | None) -> None:
    with _db_lock, _conn:
        _conn.execute(
            "UPDATE rooms SET raffle_entries_json = ?, raffle_winner = ? WHERE room_id = ?",
            (json.dumps(entries), winner, room_id),
        )
        _touch_room_activity(room_id)
