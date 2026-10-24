# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Shared fixtures. Every test runs against a throwaway database, never financie.db."""

import pytest
from fastapi.testclient import TestClient

from financie import db, gemma


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    # no Ollama in tests: the deterministic paths must work on their own
    monkeypatch.setattr(gemma, "available", lambda: False)
    db.init_db()
    return db.DB_PATH


@pytest.fixture
def client():
    from financie.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def account_id(client):
    return client.get("/api/accounts").json()[0]["id"]
