# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Shared fixtures. Every test runs against a throwaway database, never financie.db, and
never sees the user's statements folder."""

import os

# The user's own settings point at the real database and statements: drop them before the
# package reads them on import
for _name in [n for n in os.environ if n.startswith("FINANCIE_") or n in ("GEMMA_MODEL", "OLLAMA_URL")]:
    del os.environ[_name]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from financie import db, gemma  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "IMPORTS_DIR", None)
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
