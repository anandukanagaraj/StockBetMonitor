"""Tests for the P3 review fixes not covered by test_web_layer.py."""
import configparser
from datetime import date, datetime

import scheduler
from conftest import make_client


def _config(holidays):
    config = configparser.ConfigParser()
    config.read_dict({"MARKET": {"HOLIDAYS": holidays}})
    return config


def test_holiday_warning_when_current_year_missing():
    message = scheduler.holiday_coverage_warning(_config("2026-01-26"), date(2027, 3, 1))
    assert message and "2027" in message


def test_holiday_warning_in_december_for_next_year():
    config = _config("2026-01-26,2026-12-25")
    assert scheduler.holiday_coverage_warning(config, date(2026, 11, 30)) is None
    message = scheduler.holiday_coverage_warning(config, date(2026, 12, 1))
    assert message and "2027" in message and "2026" not in message.split("for ")[1].split(".")[0]
    assert scheduler.holiday_coverage_warning(_config("2026-12-25,2027-01-26"), date(2026, 12, 1)) is None


def test_holiday_warning_sent_once_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(scheduler, "send_telegram", lambda config, message: sent.append(message) or True)
    config = _config("2026-01-26")
    scheduler.warn_about_holidays_if_due(config, now=datetime(2027, 1, 2, 9, 0))
    scheduler.warn_about_holidays_if_due(config, now=datetime(2027, 1, 2, 15, 0))
    assert len(sent) == 1
    scheduler.warn_about_holidays_if_due(config, now=datetime(2027, 1, 3, 9, 0))
    assert len(sent) == 2


def test_health_does_not_expose_file_paths(app_db):
    response = app_db.app.test_client().get("/health")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "heartbeat_path" not in body
    assert "\\\\" not in body and "/data/" not in body


def test_requests_logged_at_debug_not_info(app_db, caplog):
    client = make_client(app_db)
    with caplog.at_level("INFO", logger="app"):
        client.get("/options")
    assert not any("Request started" in record.getMessage() for record in caplog.records)
