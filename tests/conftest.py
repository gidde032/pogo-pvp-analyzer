"""Shared pytest fixtures.

Two guarantees every test needs:

1. No test ever touches the real ``~/.pvp-analyzer`` directory. We redirect
   ``PVP_ANALYZER_HOME`` to a tmp path and force an in-memory database via
   ``PVP_ANALYZER_DB_URL``.
2. The SQLAlchemy engine is torn down between tests so each test starts with a
   clean, empty database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pvp import data as data_module


@pytest.fixture(autouse=True)
def hermetic_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point all filesystem/DB access at tmp_path and use in-memory SQLite."""
    monkeypatch.setenv("PVP_ANALYZER_HOME", str(tmp_path))
    monkeypatch.setenv("PVP_ANALYZER_DB_URL", "sqlite:///:memory:")
    data_module.reset_engine()
    data_module.create_db_and_tables()
    yield tmp_path
    data_module.reset_engine()


@pytest.fixture
def cache_dir(hermetic_storage: Path) -> Path:
    """Return the cache directory under the hermetic home, creating it."""
    directory = hermetic_storage / "cache"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures directory (pinned JSON snapshots)."""
    return Path(__file__).parent / "fixtures"
