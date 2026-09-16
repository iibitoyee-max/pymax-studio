"""
PryMax Studio — SQLite schema migrations
==========================================
A minimal, dependency-free migration runner — not Alembic or Flyway (no
down-migrations, no branching, no autogeneration from ORM models, since
there's no ORM here). It's the smallest thing that actually solves "how
do I change the schema without hand-editing every already-deployed
prymax.db file."

Each migration is a (id, description, sql) tuple. Applied migration ids
are recorded in a schema_migrations table, so:
  - A brand-new database starts empty and applies every migration in order.
  - An existing database (including ones created by earlier versions of
    this project, before this migrations system existed) applies only
    the migrations it's missing.
  - Running it twice is always safe — already-applied migrations are
    skipped, never re-run.

Migration 1 is deliberately written with CREATE TABLE IF NOT EXISTS,
because it has to work both for a fresh database AND for one that already
has these tables from before migrations existed (this project's own
prymax.db files from earlier development). See test_migrations.py's
test_upgrading_a_pre_migrations_database for the specific scenario this
guards against.

Rule for adding a new migration: append a new tuple to MIGRATIONS below.
Never edit an already-numbered one — databases that already recorded it
as applied won't re-run it, so an edit would silently diverge from what's
actually on disk for anyone who already upgraded.
"""
from __future__ import annotations

import sqlite3
import time

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema (rooms, chat_messages, polls, questions, whiteboard_strokes)",
        """
        CREATE TABLE IF NOT EXISTS rooms (
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

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_room ON chat_messages(room_id);

        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            votes_json TEXT NOT NULL,
            voters_json TEXT NOT NULL,
            is_open INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_polls_room ON polls(room_id);

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            name TEXT NOT NULL,
            question TEXT NOT NULL,
            upvotes INTEGER NOT NULL,
            upvoted_by_json TEXT NOT NULL,
            answered INTEGER NOT NULL,
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_questions_room ON questions(room_id);

        CREATE TABLE IF NOT EXISTS whiteboard_strokes (
            id INTEGER PRIMARY KEY,
            room_id TEXT NOT NULL,
            points_json TEXT NOT NULL,
            color TEXT NOT NULL,
            width REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strokes_room ON whiteboard_strokes(room_id);
        """,
    ),
    (
        2,
        "add rooms.last_activity_at, for future backup/retention tooling",
        """
        ALTER TABLE rooms ADD COLUMN last_activity_at REAL;
        """,
    ),
    (
        3,
        "add rooms.host_token_expires_at, closing the previously-documented "
        "'no token expiry' gap",
        """
        ALTER TABLE rooms ADD COLUMN host_token_expires_at REAL;
        """,
    ),
]


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Applies any migrations this database hasn't recorded yet. Returns
    the list of migration ids actually applied during this call (empty
    if the database was already fully up to date)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at REAL NOT NULL
        )"""
    )
    already_applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}

    newly_applied = []
    for migration_id, description, sql in MIGRATIONS:
        if migration_id in already_applied:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (id, description, applied_at) VALUES (?, ?, ?)",
            (migration_id, description, time.time()),
        )
        newly_applied.append(migration_id)

    return newly_applied


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration id, or 0 for a database with no
    migrations table at all (shouldn't happen once run_migrations has
    been called at least once, but useful for diagnostics)."""
    try:
        row = conn.execute("SELECT MAX(id) FROM schema_migrations").fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0
