"""Consistent SQLite backups.

Uses SQLite's online backup API, so changes still in the WAL file are included
and the copy is consistent even while the app and scheduler are writing.
"""
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "data" / "stock_monitor.db"
BACKUP_DIR = BASE_DIR / "data" / "backups"
# Only files matching this prefix are pruned, so manual "before_*" milestone
# backups are never deleted.
DAILY_PREFIX = "stock_monitor_daily_"


def _prune(directory, keep):
    backups = sorted(directory.glob(f"{DAILY_PREFIX}*.db"), key=lambda path: path.name, reverse=True)
    for old in backups[keep:]:
        old.unlink()
        logger.info("Old database backup removed: %s", old)


def backup_database(db_path=DB_PATH, backup_dir=BACKUP_DIR, keep=30, copy_dir=None, now=None):
    """Write a verified backup and return its path. Optionally mirror it to copy_dir."""
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    keep = max(1, int(keep))
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{DAILY_PREFIX}{stamp}.db"
    temp = target.with_suffix(".tmp")

    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(temp)
        try:
            source.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            destination.close()
    finally:
        source.close()

    if result != "ok":
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Backup integrity check failed: {result}")
    temp.replace(target)
    logger.info("Database backup created: %s (%s bytes)", target, target.stat().st_size)
    _prune(backup_dir, keep)

    if copy_dir:
        copy_dir = Path(copy_dir)
        copy_dir.mkdir(parents=True, exist_ok=True)
        mirrored = copy_dir / target.name
        shutil.copy2(target, mirrored)
        logger.info("Database backup copied off-site: %s", mirrored)
        _prune(copy_dir, keep)

    return target


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Create a consistent backup of the StockMonitor database.")
    parser.add_argument("--keep", type=int, default=30)
    parser.add_argument("--copy-dir", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(backup_database(keep=args.keep, copy_dir=args.copy_dir))
