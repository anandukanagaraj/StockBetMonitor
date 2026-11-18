from datetime import datetime, timedelta
from pathlib import Path

from services.report_cleanup import cleanup_old_reports, latest_report_path, update_report_manifest


def test_cleanup_old_reports_removes_stale_files(tmp_path):
    old_file = tmp_path / "report_masked_20240101_120000.html"
    new_file = tmp_path / "report_masked_20990101_120000.html"
    old_file.write_text("<html>old</html>", encoding="utf-8")
    new_file.write_text("<html>new</html>", encoding="utf-8")
    old_time = datetime.now() - timedelta(days=30)
    import os

    os.utime(old_file, (old_time.timestamp(), old_time.timestamp()))

    result = cleanup_old_reports(tmp_path, keep_days=14)
    assert result["removed"] == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_latest_report_path_uses_manifest(tmp_path):
    report = tmp_path / "report_masked_20260101_120000.html"
    report.write_text("<html>masked</html>", encoding="utf-8")
    manifest = tmp_path.parent / "data"
    manifest.mkdir(parents=True, exist_ok=True)

    from services import report_cleanup

    original_manifest = report_cleanup.MANIFEST_PATH
    original_reports = report_cleanup.REPORTS_DIR
    report_cleanup.MANIFEST_PATH = manifest / "latest_reports.json"
    report_cleanup.REPORTS_DIR = tmp_path
    try:
        update_report_manifest({"masked": report})
        assert latest_report_path("masked", tmp_path) == report
    finally:
        report_cleanup.MANIFEST_PATH = original_manifest
        report_cleanup.REPORTS_DIR = original_reports
