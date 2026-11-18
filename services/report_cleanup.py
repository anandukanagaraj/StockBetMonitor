import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = BASE_DIR / "reports"
MANIFEST_PATH = BASE_DIR / "data" / "latest_reports.json"
REPORT_PREFIXES = ("report_masked_", "report_targets_", "report_app_targets_")
REPORT_PATTERN = re.compile(r"^report_(masked|targets|app_targets)_(\d{8}_\d{6})\.html$")


def update_report_manifest(paths):
    payload = {
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "paths": {key: str(value) for key, value in paths.items()},
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def latest_report_path(report_kind, reports_dir=None):
    reports_dir = Path(reports_dir or REPORTS_DIR)
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            candidate = (manifest.get("paths") or {}).get(report_kind)
            if candidate:
                path = Path(candidate)
                if path.exists():
                    return path
        except Exception:
            logger.exception("Could not read report manifest: %s", MANIFEST_PATH)

    candidates = sorted(
        reports_dir.glob(f"report_{report_kind}_*.html"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def cleanup_old_reports(reports_dir=None, keep_days=14):
    reports_dir = Path(reports_dir or REPORTS_DIR)
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1.")
    if not reports_dir.exists():
        return {"removed": 0, "kept": 0}

    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    kept = 0
    for path in reports_dir.glob("report_*.html"):
        if not REPORT_PATTERN.match(path.name):
            kept += 1
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
        else:
            kept += 1

    logger.info("Report cleanup complete: removed=%s kept=%s keep_days=%s", removed, kept, keep_days)
    return {"removed": removed, "kept": kept, "keep_days": keep_days}
