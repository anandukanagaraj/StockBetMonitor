"""Single, versioned entry point for database schema setup.

All table creation and migrations live in db.init_db, options_db.init_options_db
and fund_management.init_fund_db. Instead of each module running them on every
import, ensure_schema() runs them once, in order, under a cross-process lock,
and records SCHEMA_VERSION in the database. Later starts see the matching
version and skip the work.

Bump SCHEMA_VERSION whenever any of those init functions change.
"""
import logging
import os
import time
from datetime import datetime

import db

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2026.11.26-1"

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_ensured = set()


class _FileLock:
    """Blocking, cross-process lock (Windows and Linux)."""

    def __init__(self, path, timeout=120):
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.handle.close()
                    raise TimeoutError(f"Timed out waiting for schema lock: {self.path}")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _stored_version():
    with db.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_schema_version'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute("SELECT version FROM app_schema_version WHERE id = 1").fetchone()
        return row["version"] if row else None


def _store_version():
    with db.connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            """INSERT INTO app_schema_version (id, version, applied_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET version = excluded.version, applied_at = excluded.applied_at""",
            (SCHEMA_VERSION, datetime.now().replace(microsecond=0).isoformat()),
        )


def _run_all_migrations():
    import fund_management
    import options_db

    db.init_db()
    options_db.init_options_db()
    fund_management.init_fund_db()


def ensure_schema(force=False):
    """Bring the database schema up to SCHEMA_VERSION. Safe to call often."""
    db_path = db.DB_PATH
    if not force and db_path in _ensured:
        return False
    if not force and db_path.exists() and _stored_version() == SCHEMA_VERSION:
        _ensured.add(db_path)
        return False
    with _FileLock(db_path.with_name(db_path.name + ".schema.lock")):
        # Another process may have finished while we waited for the lock.
        if not force and db_path.exists() and _stored_version() == SCHEMA_VERSION:
            _ensured.add(db_path)
            return False
        logger.info("Applying database schema version %s: %s", SCHEMA_VERSION, db_path)
        _run_all_migrations()
        _store_version()
    _ensured.add(db_path)
    return True
