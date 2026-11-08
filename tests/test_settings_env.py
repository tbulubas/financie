# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Environment variables: FINANCIE_* names first, the old unprefixed ones as a fallback."""

from financie import gemma


def test_prefixed_name_wins(monkeypatch):
    monkeypatch.setenv("FINANCIE_GEMMA_MODEL", "new-model")
    monkeypatch.setenv("GEMMA_MODEL", "old-model")
    assert gemma._env("FINANCIE_GEMMA_MODEL", "GEMMA_MODEL", "default") == "new-model"


def test_old_name_still_works(monkeypatch):
    monkeypatch.delenv("FINANCIE_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://old:1")
    assert gemma._env("FINANCIE_OLLAMA_URL", "OLLAMA_URL", "default") == "http://old:1"


def test_default_and_empty_values(monkeypatch):
    monkeypatch.setenv("FINANCIE_GEMMA_MODEL", "")
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    assert gemma._env("FINANCIE_GEMMA_MODEL", "GEMMA_MODEL", "default") == "default"
