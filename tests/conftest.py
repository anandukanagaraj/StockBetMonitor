"""Shared fixtures: a fresh, migrated database per test and a logged-in client.

Network access is blocked for web tests so pages that fetch prices fail fast
and deterministically instead of calling Yahoo/NSE.
"""
import importlib
import os
import socket

import pytest

# Keep test runs out of the real logs\ files (app.py reads this at import).
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import db
import options_db


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def app_db(tmp_path, monkeypatch, no_network):
    """Import the app against an empty temp database and migrate it."""
    path = tmp_path / "web.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(options_db, "DB_PATH", path)
    app_module = importlib.import_module("app")
    from services.schema import ensure_schema

    ensure_schema()
    app_module.app.config["TESTING"] = True
    app_module._login_failures.clear()
    return app_module


def make_client(app_module, user_id=1, username="admin", is_admin=True):
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-token"
        if user_id is not None:
            sess.update(user_id=user_id, username=username, is_admin=is_admin)
    return client
