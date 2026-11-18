from services import scheduler_health as health


def test_write_heartbeat_preserves_last_market_check_at(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "scheduler_heartbeat.json"
    monkeypatch.setattr(health, "HEARTBEAT_PATH", heartbeat_path)

    health.write_heartbeat(status="ok", last_market_check_at="2026-08-07T15:00:00")
    health.write_heartbeat(status="running")

    payload = health.read_heartbeat()
    assert payload["last_market_check_at"] == "2026-08-07T15:00:00"
    assert payload["status"] == "running"


def test_write_heartbeat_updates_last_market_check_at(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "scheduler_heartbeat.json"
    monkeypatch.setattr(health, "HEARTBEAT_PATH", heartbeat_path)

    health.write_heartbeat(status="ok", last_market_check_at="2026-08-07T15:00:00")
    health.write_heartbeat(status="ok", last_market_check_at="2026-08-07T15:30:00")

    payload = health.read_heartbeat()
    assert payload["last_market_check_at"] == "2026-08-07T15:30:00"
