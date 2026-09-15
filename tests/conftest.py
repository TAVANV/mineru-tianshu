"""Shared temporary database and API fixtures for both integration batches."""

import pytest
from test_phase1 import db as _db, api as _api


@pytest.fixture
def db(tmp_path, monkeypatch):
    return _db.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def api(tmp_path, monkeypatch):
    return _api.__wrapped__(tmp_path, monkeypatch)
