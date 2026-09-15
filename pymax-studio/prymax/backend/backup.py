#!/usr/bin/env python3
"""
PryMax Studio — database backup
==================================
A real, working backup for backend/prymax.db — not a cron-job stub. Uses
SQLite's own online backup API (`sqlite3.Connection.backup()`), which
safely copies a live database even while the Flask app has it open for
writes (it doesn't require stopping the server), unlike a plain file
copy which could grab a half-written page mid-transaction.

Usage:
    python3 backup.py                      # one backup, default settings
    python3 backup.py --keep 14            # keep the 14 most recent backups
    python3 backup.py --dest /some/path    # write backups somewhere else
    python3 backup.py --restore FILE       # restore from a specific backup

Typical deployment: run as a cron job / scheduled task, e.g. nightly:
    0 3 * * *  cd /path/to/prymax/backend && python3 backup.py --keep 14

What this is NOT: off-site/cloud backup, encryption at rest, or a
point-in-time recovery system with continuous WAL archiving. It's a
periodic snapshot to a local `backups/` directory with simple count-based
retention — genuinely useful, not a substitute for a real backup service
once this handles data anyone would be upset to lose.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("PRYMAX_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "prymax.db"
)
DEFAULT_BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")


def backup_database(db_path: str, backup_dir: str, keep: int) -> str:
    """Creates a timestamped backup of db_path in backup_dir using
    SQLite's online backup API, then prunes old backups beyond `keep`.
    Returns the path to the new backup file. Raises FileNotFoundError if
    db_path doesn't exist yet (nothing to back up)."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"no database found at {db_path}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest_path = os.path.join(backup_dir, f"prymax-{timestamp}.db")

    source_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    _prune_old_backups(backup_dir, keep)
    return dest_path


def _prune_old_backups(backup_dir: str, keep: int) -> list[str]:
    """Deletes all but the `keep` most recent backup files (by filename,
    which sorts chronologically thanks to the YYYYMMDD-HHMMSS format).
    keep <= 0 means "keep everything, prune nothing". Returns the list
    of deleted file paths."""
    if keep <= 0:
        return []
    backups = sorted(Path(backup_dir).glob("prymax-*.db"))
    to_delete = backups[:-keep] if len(backups) > keep else []
    deleted = []
    for path in to_delete:
        path.unlink()
        deleted.append(str(path))
    return deleted


def restore_database(backup_path: str, db_path: str) -> None:
    """Restores db_path from a backup file. The live database (if any)
    is moved aside with a .pre-restore suffix rather than deleted
    outright, in case the restore target was wrong."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"backup file not found: {backup_path}")

    if os.path.exists(db_path):
        safety_copy = f"{db_path}.pre-restore-{int(time.time())}"
        shutil.move(db_path, safety_copy)
        print(f"Existing database moved aside to {safety_copy} before restoring.")

    shutil.copy2(backup_path, db_path)


def list_backups(backup_dir: str) -> list[tuple[str, int, float]]:
    """Returns (path, size_bytes, mtime) for every backup, oldest first."""
    backups = sorted(Path(backup_dir).glob("prymax-*.db"))
    return [(str(p), p.stat().st_size, p.stat().st_mtime) for p in backups]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="path to prymax.db")
    parser.add_argument("--dest", default=DEFAULT_BACKUP_DIR, help="backup directory")
    parser.add_argument("--keep", type=int, default=14, help="number of backups to retain (0 = keep all)")
    parser.add_argument("--restore", metavar="BACKUP_FILE", help="restore from this backup file instead of taking a new one")
    parser.add_argument("--list", action="store_true", help="list existing backups and exit")
    args = parser.parse_args()

    if args.list:
        backups = list_backups(args.dest)
        if not backups:
            print(f"No backups found in {args.dest}")
            return 0
        for path, size, mtime in backups:
            when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(mtime))
            print(f"{path}  ({size:,} bytes, {when})")
        return 0

    if args.restore:
        try:
            restore_database(args.restore, args.db)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Restored {args.db} from {args.restore}")
        return 0

    try:
        dest = backup_database(args.db, args.dest, args.keep)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    size = os.path.getsize(dest)
    print(f"Backed up {args.db} -> {dest} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
