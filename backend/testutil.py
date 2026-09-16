"""
PryMax Studio — shared test support
======================================
Not a test file itself (deliberately not named test_*.py, so `unittest
discover -p "test_*.py"` never tries to collect it as one).

Exists because of a real bug found while building this project's test
suite: store.py's SQLite connection is a module-level singleton, shared
by every test file that runs in the same process (e.g. under `unittest
discover`). A test that does `importlib.reload(store)` to point that
connection at its own temp database, then deletes that file in its
tearDown, was assumed safe on POSIX ("the open file descriptor keeps
working after unlink"). It isn't, for SQLite specifically: once the
file's path no longer resolves, SQLite can't create the `-journal`
sidecar file a write transaction needs, and permanently marks the
connection read-only — confirmed with a minimal, direct reproduction
(connect, unlink, write — the write raises "attempt to write a readonly
database" immediately). That poisoned connection then broke every
subsequent test file's writes in the same run, with no clue from the
failure itself that a completely unrelated file's cleanup was the cause.

Fix: never delete a temp file that store._conn might still be pointing
at while other tests could still run afterward. Defer all cleanup to
process exit via atexit, by which point nothing will touch store again.
"""
from __future__ import annotations

import atexit
import os

_pending_cleanup: list[str] = []
_registered = False


def track_for_cleanup(path: str) -> None:
    """Call this from a test's tearDown instead of os.unlink(path)
    whenever that path might still be store.py's active connection
    target. The file is removed at process exit instead of immediately."""
    global _registered
    _pending_cleanup.append(path)
    if not _registered:
        atexit.register(_cleanup_all)
        _registered = True


def _cleanup_all() -> None:
    for path in _pending_cleanup:
        try:
            os.unlink(path)
        except OSError:
            pass
