"""Sync module — fetch PvPoke rankings and the Pokemon GO game master.

Design notes:

* All HTTP access goes through ``httpx`` behind an injectable ``fetch`` function
  so tests can drive the sync flow without hitting GitHub.
* After each download the payload is validated for shape (list / dict / expected
  keys) before it is written to the cache. An unexpected shape is treated as a
  hard sync failure so stale-but-valid cached data keeps working (NFR-2).
* Cache writes are atomic — we write to ``<name>.tmp`` and rename — so a crash
  mid-write can never leave a half-JSON file that later loaders would trip
  over.
* Timestamps are recorded via :func:`pvp.data.write_cache_metadata` so the
  ``sync`` subcommand can show "when did I last sync" without reparsing the
  payloads.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from . import data


# ---------------------------------------------------------------------------
# Source URLs
# ---------------------------------------------------------------------------

# PvPoke publishes ranked lists under `src/data/rankings/<league>/overall/` on
# the main branch. The three league subdirectories map to CP caps 1500 / 2500 /
# 10000 — these magic numbers are PvPoke's convention, documented in their
# rankings CLI README.
PVPOKE_BASE = os.environ.get(
    "PVP_PVPOKE_BASE",
    "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/rankings",
)

# PokeMiners publishes Niantic's game master JSON at the root of their
# pokemon-go-protobuf repo.
GAME_MASTER_URL = os.environ.get(
    "PVP_GAME_MASTER_URL",
    "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/latest.json",
)


LEAGUE_ENDPOINTS: dict[str, str] = {
    "gl": f"{PVPOKE_BASE}/all/overall/rankings-1500.json",
    "ul": f"{PVPOKE_BASE}/all/overall/rankings-2500.json",
    "ml": f"{PVPOKE_BASE}/all/overall/rankings-10000.json",
}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Fetcher = Callable[[str], bytes]
ProgressCallback = Callable[[str, str], None]
"""Called as ``progress(stage, message)``. ``stage`` is ``"start" | "ok" | "fail"``."""


@dataclass(frozen=True)
class SyncResult:
    """Summary of one sync run — which files we refreshed, which we skipped."""

    updated: tuple[str, ...]
    failed: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB — well above any real game master


def _default_fetcher(url: str) -> bytes:
    """Default HTTP fetcher — synchronous httpx with a reasonable timeout."""
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise SyncValidationError(
            f"Response from {url} is too large "
            f"({len(response.content):,} bytes > {_MAX_RESPONSE_BYTES:,} limit)."
        )
    return response.content


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write bytes to ``path`` atomically via a sibling ``.tmp`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


# -- Shape validators ------------------------------------------------------


class SyncValidationError(ValueError):
    """Raised when a downloaded payload does not have the expected shape."""


def _validate_rankings(payload: Any, *, league: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise SyncValidationError(
            f"PvPoke rankings for {league.upper()} should be a JSON array — got {type(payload).__name__}."
        )
    if not payload:
        raise SyncValidationError(
            f"PvPoke rankings for {league.upper()} are empty — source may be broken."
        )
    first = payload[0]
    if not isinstance(first, dict):
        raise SyncValidationError(
            f"PvPoke rankings entries should be objects — got {type(first).__name__}."
        )
    # We only require one identifier key — different snapshots use different
    # spellings. The loader in data.py accepts either.
    if not any(k in first for k in ("speciesId", "speciesName", "species_id")):
        raise SyncValidationError(
            "PvPoke rankings entries lack a speciesId/speciesName field — unexpected shape."
        )
    return payload


def _validate_game_master(payload: Any) -> dict[str, Any] | list[Any]:
    if not isinstance(payload, (dict, list)):
        raise SyncValidationError(
            f"Game master must be a JSON object or list — got {type(payload).__name__}."
        )
    # Confirm we can find at least one template. This uses the same iterator
    # the rest of the codebase will use for reads so if the iterator breaks
    # the validator breaks with it.
    wrapped: dict[str, Any]
    if isinstance(payload, list):
        wrapped = {"itemTemplates": payload}
    else:
        wrapped = payload
    templates = list(data.iter_game_master_entries(wrapped))
    if not templates:
        raise SyncValidationError(
            "Game master payload contains no templates — unexpected shape."
        )
    return payload


# -- Individual download steps --------------------------------------------


def _fetch_json(url: str, fetcher: Fetcher) -> Any:
    raw = fetcher(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncValidationError(f"Response from {url} is not valid JSON: {exc}") from exc


def _emit(progress: ProgressCallback | None, stage: str, message: str) -> None:
    if progress is not None:
        progress(stage, message)


def _sync_league(
    league: str,
    *,
    fetcher: Fetcher,
    progress: ProgressCallback | None,
) -> None:
    url = LEAGUE_ENDPOINTS[league]
    _emit(progress, "start", f"PvPoke rankings ({league.upper()})")
    payload = _fetch_json(url, fetcher)
    _validate_rankings(payload, league=league)
    target = data.rankings_cache_path(league)
    _write_atomic(target, json.dumps(payload).encode("utf-8"))
    data.write_cache_metadata(rankings={league: data.now_utc()})
    _emit(progress, "ok", f"PvPoke rankings ({league.upper()})")


def _sync_game_master(
    *,
    fetcher: Fetcher,
    progress: ProgressCallback | None,
) -> None:
    _emit(progress, "start", "Game master")
    payload = _fetch_json(GAME_MASTER_URL, fetcher)
    _validate_game_master(payload)
    if isinstance(payload, list):
        payload = {"itemTemplates": payload}
    _write_atomic(data.game_master_cache_path(), json.dumps(payload).encode("utf-8"))
    data.write_cache_metadata(game_master=data.now_utc())
    _emit(progress, "ok", "Game master")


def sync(
    *,
    leagues: Iterable[str] = data.VALID_LEAGUES,
    include_game_master: bool = True,
    fetcher: Fetcher | None = None,
    progress: ProgressCallback | None = None,
) -> SyncResult:
    """Run a full or partial sync.

    Each resource is fetched, validated, and cached independently. A failure
    on one resource does not abort the rest — we collect failures in the
    :class:`SyncResult` so the caller can report them all at once.

    Parameters
    ----------
    leagues:
        Which league rankings to refresh. Defaults to all three.
    include_game_master:
        Whether to refresh the game master. Defaults to true.
    fetcher:
        Overridable HTTP fetcher for tests. Receives a URL, returns bytes.
    progress:
        Optional callback for per-step progress updates.
    """
    fetch = fetcher or _default_fetcher

    # Ensure the cache dir exists before any write so the metadata file can
    # always be placed regardless of what succeeds.
    data.get_cache_dir().mkdir(parents=True, exist_ok=True)

    updated: list[str] = []
    failed: dict[str, str] = {}

    for league in leagues:
        normalized = league.strip().lower()
        if normalized not in LEAGUE_ENDPOINTS:
            failed[league] = f"Unknown league {league!r}"
            _emit(progress, "fail", f"PvPoke rankings ({league})")
            continue
        try:
            _sync_league(normalized, fetcher=fetch, progress=progress)
        except (SyncValidationError, httpx.HTTPError, OSError) as exc:
            failed[f"rankings-{normalized}"] = str(exc)
            _emit(progress, "fail", f"PvPoke rankings ({normalized.upper()})")
        else:
            updated.append(f"rankings-{normalized}")

    if include_game_master:
        try:
            _sync_game_master(fetcher=fetch, progress=progress)
        except (SyncValidationError, httpx.HTTPError, OSError) as exc:
            failed["game_master"] = str(exc)
            _emit(progress, "fail", "Game master")
        else:
            updated.append("game_master")

    return SyncResult(updated=tuple(updated), failed=failed)
