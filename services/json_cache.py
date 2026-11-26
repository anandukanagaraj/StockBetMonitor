"""Helpers for the JSON price-history caches.

Cache keys look like "<version>|<TICKER>|<start>|<end>". For open bets the end
date is today, so a new entry appears every day; prune_superseded keeps only the
newest end date per (version, ticker, start).
"""
import json
import os
import tempfile
from pathlib import Path


def prune_superseded(cache, version):
    """Return a copy of cache without old-version keys and superseded end dates."""
    latest = {}
    for key in cache:
        parts = key.split("|")
        if len(parts) != 4 or parts[0] != version:
            continue
        group = tuple(parts[:3])
        if group not in latest or parts[3] > latest[group].split("|")[3]:
            latest[group] = key
    keep = set(latest.values())
    return {key: value for key, value in cache.items() if key in keep}


def atomic_write_json(path, data):
    """Write JSON to a temp file in the same folder, then swap it in, so a
    crash or a concurrent reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
