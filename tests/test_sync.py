"""Phase 3 tests — sync module.

All tests drive :func:`pvp.sync.sync` through an in-memory fetcher so nothing
talks to GitHub during the test run. The fetcher also lets us simulate HTTP
failures, bad JSON, and shape mismatches deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pvp import data, sync as sync_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(fixtures_dir: Path, name: str) -> Any:
    return json.loads((fixtures_dir / name).read_text())


def _make_fetcher(responses: dict[str, Any | Exception]):
    """Return a fetcher that looks up URLs in ``responses``.

    Values that are exceptions are raised instead of returned. Non-bytes
    values are JSON-encoded for convenience.
    """

    def fetch(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"Unexpected fetch: {url}")
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return json.dumps(value).encode("utf-8")

    return fetch


@pytest.fixture
def rankings_payload(fixtures_dir: Path) -> Any:
    return _load_fixture(fixtures_dir, "rankings-gl.json")


@pytest.fixture
def gm_payload(fixtures_dir: Path) -> Any:
    return _load_fixture(fixtures_dir, "game-master.json")


@pytest.fixture
def all_urls(rankings_payload, gm_payload) -> dict[str, Any]:
    return {
        sync_module.LEAGUE_ENDPOINTS["gl"]: rankings_payload,
        sync_module.LEAGUE_ENDPOINTS["ul"]: rankings_payload,
        sync_module.LEAGUE_ENDPOINTS["ml"]: rankings_payload,
        sync_module.GAME_MASTER_URL: gm_payload,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sync_writes_all_caches(all_urls, cache_dir: Path) -> None:
    result = sync_module.sync(fetcher=_make_fetcher(all_urls))
    assert result.ok
    assert set(result.updated) == {
        "rankings-gl",
        "rankings-ul",
        "rankings-ml",
        "game_master",
    }
    assert (cache_dir / "rankings-gl.json").exists()
    assert (cache_dir / "rankings-ul.json").exists()
    assert (cache_dir / "rankings-ml.json").exists()
    assert (cache_dir / "game-master.json").exists()

    # Metadata timestamps recorded
    meta = data.get_cache_metadata()
    assert set(meta.rankings.keys()) == {"gl", "ul", "ml"}
    assert meta.game_master is not None


def test_sync_skip_game_master(all_urls, cache_dir: Path) -> None:
    del all_urls[sync_module.GAME_MASTER_URL]  # ensures we don't accidentally fetch
    result = sync_module.sync(
        include_game_master=False,
        fetcher=_make_fetcher(all_urls),
    )
    assert result.ok
    assert "game_master" not in result.updated
    assert not (cache_dir / "game-master.json").exists()


def test_sync_single_league(rankings_payload, cache_dir: Path) -> None:
    urls = {sync_module.LEAGUE_ENDPOINTS["gl"]: rankings_payload}
    result = sync_module.sync(
        leagues=["gl"],
        include_game_master=False,
        fetcher=_make_fetcher(urls),
    )
    assert result.ok
    assert result.updated == ("rankings-gl",)
    assert (cache_dir / "rankings-gl.json").exists()
    assert not (cache_dir / "rankings-ul.json").exists()


def test_sync_progress_callback(all_urls) -> None:
    events: list[tuple[str, str]] = []
    sync_module.sync(
        fetcher=_make_fetcher(all_urls),
        progress=lambda stage, msg: events.append((stage, msg)),
    )
    # Each of the four resources produces a start + ok event.
    starts = [e for e in events if e[0] == "start"]
    oks = [e for e in events if e[0] == "ok"]
    assert len(starts) == 4
    assert len(oks) == 4


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_sync_http_error_is_collected(all_urls, cache_dir: Path) -> None:
    all_urls[sync_module.GAME_MASTER_URL] = httpx.HTTPError("boom")
    result = sync_module.sync(fetcher=_make_fetcher(all_urls))
    assert not result.ok
    assert "game_master" in result.failed
    assert "rankings-gl" in result.updated  # other resources still succeeded


def test_sync_invalid_json_is_sync_validation_error(cache_dir: Path) -> None:
    def bad_fetch(url: str) -> bytes:
        return b"<html>this is not json</html>"

    result = sync_module.sync(
        leagues=["gl"],
        include_game_master=False,
        fetcher=bad_fetch,
    )
    assert not result.ok
    assert "rankings-gl" in result.failed


def test_sync_wrong_shape_rankings(cache_dir: Path) -> None:
    # Server returns a dict instead of a list
    urls = {sync_module.LEAGUE_ENDPOINTS["gl"]: {"not": "a list"}}
    result = sync_module.sync(
        leagues=["gl"],
        include_game_master=False,
        fetcher=_make_fetcher(urls),
    )
    assert not result.ok
    assert "rankings-gl" in result.failed
    assert not (cache_dir / "rankings-gl.json").exists(), (
        "A bad payload must not overwrite the cache file."
    )


def test_default_fetcher_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # _default_fetcher must raise SyncValidationError when the server returns
    # more bytes than _MAX_RESPONSE_BYTES, before attempting JSON parsing.
    import httpx as _httpx

    big_content = b"x" * (sync_module._MAX_RESPONSE_BYTES + 1)

    class _FakeResponse:
        content = big_content
        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(_httpx, "get", lambda *_a, **_kw: _FakeResponse())

    with pytest.raises(sync_module.SyncValidationError, match="too large"):
        sync_module._default_fetcher("http://example.com")


def test_sync_unknown_league_is_reported(all_urls, cache_dir: Path) -> None:
    result = sync_module.sync(
        leagues=["gl", "bogus"],
        include_game_master=False,
        fetcher=_make_fetcher(all_urls),
    )
    assert "bogus" in result.failed
    assert "rankings-gl" in result.updated


# ---------------------------------------------------------------------------
# Idempotence / second run
# ---------------------------------------------------------------------------


def test_second_sync_overwrites_existing_cache(all_urls, cache_dir: Path) -> None:
    # First sync
    sync_module.sync(fetcher=_make_fetcher(all_urls))
    first_mtime = (cache_dir / "rankings-gl.json").stat().st_mtime_ns
    # Second sync with a different payload
    all_urls[sync_module.LEAGUE_ENDPOINTS["gl"]] = [
        {"speciesId": "different", "speciesName": "Different", "stats": {}, "moveset": []}
    ]
    sync_module.sync(
        leagues=["gl"],
        include_game_master=False,
        fetcher=_make_fetcher(
            {sync_module.LEAGUE_ENDPOINTS["gl"]: all_urls[sync_module.LEAGUE_ENDPOINTS["gl"]]}
        ),
    )
    on_disk = json.loads((cache_dir / "rankings-gl.json").read_text())
    assert on_disk[0]["speciesId"] == "different"
    second_mtime = (cache_dir / "rankings-gl.json").stat().st_mtime_ns
    assert second_mtime >= first_mtime


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_sync_exits_zero_on_success(all_urls, monkeypatch) -> None:
    from typer.testing import CliRunner

    from pvp.cli import app

    monkeypatch.setattr(sync_module, "_default_fetcher", _make_fetcher(all_urls))
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "Done" in result.output


def test_cli_sync_exits_nonzero_on_failure(monkeypatch) -> None:
    from typer.testing import CliRunner

    from pvp.cli import app

    def bad(url: str) -> bytes:
        raise httpx.HTTPError("network down")

    monkeypatch.setattr(sync_module, "_default_fetcher", bad)
    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    # Every failed resource should surface via the standard error helper.
    assert "Error:" in result.output
    assert "network down" in result.output
