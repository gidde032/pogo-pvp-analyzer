"""Data layer — SQLAlchemy models plus cached JSON loaders.

This module is the single point of contact between the rest of the package and
persistent storage. Other modules must not read the cache directory or issue
raw SQL; they must go through one of the helpers exported here.

Two kinds of data live behind this layer:

1. The user's **roster** — an editable SQLite database at
   ``$PVP_ANALYZER_HOME/roster.db`` (default ``~/.pvp-analyzer/roster.db``).
2. **Cached meta files** — JSON snapshots of PvPoke rankings and the Pokemon
   GO game master, written by :mod:`pvp.sync` into
   ``$PVP_ANALYZER_HOME/cache/``.

The location can be overridden with the ``PVP_ANALYZER_HOME`` environment
variable, which is what the test suite does to keep tests hermetic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from sqlalchemy import Engine, String, create_engine, delete, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def get_home_dir() -> Path:
    """Return the root directory used for user-scoped storage.

    Respects the ``PVP_ANALYZER_HOME`` environment variable so tests and
    integrators can redirect storage without monkey-patching.
    """
    override = os.environ.get("PVP_ANALYZER_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pvp-analyzer"


def get_db_path() -> Path:
    """Path to the roster SQLite database."""
    return get_home_dir() / "roster.db"


def get_cache_dir() -> Path:
    """Path to the JSON cache directory (used by sync and loaders)."""
    return get_home_dir() / "cache"


# ---------------------------------------------------------------------------
# ORM
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base for all roster tables."""


class RosterEntry(Base):
    """A single Pokemon in the user's PVP roster.

    Stored fields mirror the data the community documentation expects for CP
    and stat-product math. Moves are optional: when omitted the engine falls
    back to the meta-optimal moveset reported by PvPoke.
    """

    __tablename__ = "roster"

    id: Mapped[int] = mapped_column(primary_key=True)
    species: Mapped[str] = mapped_column(String(64))
    pokedex_number: Mapped[int | None] = mapped_column(default=None)

    attack_iv: Mapped[int]
    defense_iv: Mapped[int]
    stamina_iv: Mapped[int]
    level: Mapped[float]

    fast_move: Mapped[str | None] = mapped_column(String(64), default=None)
    charge_move_1: Mapped[str | None] = mapped_column(String(64), default=None)
    charge_move_2: Mapped[str | None] = mapped_column(String(64), default=None)

    nickname: Mapped[str | None] = mapped_column(String(64), default=None)
    notes: Mapped[str | None] = mapped_column(String(512), default=None)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def iv_tuple(self) -> tuple[int, int, int]:
        """Return (attack, defense, stamina) for ergonomic lookups."""
        return self.attack_iv, self.defense_iv, self.stamina_iv

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "species": self.species,
            "pokedex_number": self.pokedex_number,
            "attack_iv": self.attack_iv,
            "defense_iv": self.defense_iv,
            "stamina_iv": self.stamina_iv,
            "level": self.level,
            "fast_move": self.fast_move,
            "charge_move_1": self.charge_move_1,
            "charge_move_2": self.charge_move_2,
            "nickname": self.nickname,
            "notes": self.notes,
        }

    def __repr__(self) -> str:  # pragma: no cover — repr
        return (
            f"RosterEntry(id={self.id!r}, species={self.species!r}, "
            f"ivs={self.attack_iv}/{self.defense_iv}/{self.stamina_iv}, "
            f"level={self.level})"
        )


# ---------------------------------------------------------------------------
# Engine / session management
# ---------------------------------------------------------------------------


# Module-level cache of the engine/sessionmaker. The test suite resets this
# via :func:`reset_engine` to switch to an in-memory database per-test.
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _database_url() -> str:
    """Build a SQLite URL based on the currently configured home dir."""
    # Allow tests to force in-memory DB via env var.
    url_override = os.environ.get("PVP_ANALYZER_DB_URL")
    if url_override:
        return url_override
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def get_engine() -> Engine:
    """Return (and lazily build) the SQLAlchemy engine."""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(_database_url(), future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None  # for type-checkers
    return _SessionLocal


def reset_engine() -> None:
    """Drop the cached engine/session factory. Used between tests."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def create_db_and_tables() -> None:
    """Create the roster database file and all tables if they do not exist."""
    engine = get_engine()
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Roster CRUD
# ---------------------------------------------------------------------------


def add_entry(entry: RosterEntry, *, session: Session | None = None) -> RosterEntry:
    """Insert a RosterEntry, returning the refreshed (id-populated) instance."""
    if session is not None:
        session.add(entry)
        session.flush()
        session.refresh(entry)
        return entry
    with get_sessionmaker()() as s:
        s.add(entry)
        s.commit()
        s.refresh(entry)
        return entry


def add_entries(entries: Iterable[RosterEntry]) -> list[RosterEntry]:
    """Bulk-insert multiple entries in a single transaction."""
    items = list(entries)
    with get_sessionmaker()() as s:
        s.add_all(items)
        s.commit()
        # expire_on_commit=False keeps items valid after the session closes;
        # no explicit refresh needed.
    return items


def get_all_entries() -> list[RosterEntry]:
    """Return every roster entry, ordered by id."""
    with get_sessionmaker()() as s:
        return list(s.scalars(select(RosterEntry).order_by(RosterEntry.id)))


def get_entry_by_id(entry_id: int) -> RosterEntry | None:
    with get_sessionmaker()() as s:
        return s.get(RosterEntry, entry_id)


def get_entries_by_species(species: str) -> list[RosterEntry]:
    """Case-insensitive species lookup. Returns empty list if none match."""
    needle = species.strip().lower()
    with get_sessionmaker()() as s:
        return list(
            s.scalars(
                select(RosterEntry).where(
                    RosterEntry.species.ilike(needle)
                )
            ).all()
        )


def delete_entry(entry_id: int) -> bool:
    """Delete an entry by id. Returns True if a row was deleted."""
    with get_sessionmaker()() as s:
        result = s.execute(delete(RosterEntry).where(RosterEntry.id == entry_id))
        s.commit()
        return (result.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# JSON meta cache
# ---------------------------------------------------------------------------


VALID_LEAGUES: tuple[str, ...] = ("gl", "ul", "ml")

# League abbreviation aliases -> canonical slug used on disk
_LEAGUE_ALIASES: dict[str, str] = {
    "gl": "gl",
    "great": "gl",
    "great-league": "gl",
    "ul": "ul",
    "ultra": "ul",
    "ultra-league": "ul",
    "ml": "ml",
    "master": "ml",
    "master-league": "ml",
}


def _normalize_league(league: str) -> str:
    key = league.strip().lower()
    if key not in _LEAGUE_ALIASES:
        raise ValueError(
            f"Unknown league {league!r}. Expected one of: GL, UL, ML (or their full names)."
        )
    return _LEAGUE_ALIASES[key]


def rankings_cache_path(league: str) -> Path:
    return get_cache_dir() / f"rankings-{_normalize_league(league)}.json"


def game_master_cache_path() -> Path:
    return get_cache_dir() / "game-master.json"


def metadata_path() -> Path:
    return get_cache_dir() / "metadata.json"


class CacheMissError(FileNotFoundError):
    """Raised when a cache file the user expected is not on disk yet."""


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Serialize ``payload`` to JSON and write to ``path`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, *, description: str) -> Any:
    if not path.exists():
        raise CacheMissError(
            f"{description} is not cached yet at {path}. "
            f"Run `pvp sync` to download the latest data."
        )
    with path.open("r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{description} cache at {path} is corrupted (invalid JSON). "
                f"Run `pvp sync` to re-download. Details: {exc}"
            ) from exc


def load_pvpoke_rankings(league: str) -> list[dict[str, Any]]:
    """Return the parsed PvPoke rankings list for the given league."""
    normalized = _normalize_league(league)
    path = rankings_cache_path(normalized)
    payload = _read_json(path, description=f"PvPoke {normalized.upper()} rankings")
    if not isinstance(payload, list):
        raise ValueError(
            f"PvPoke rankings cache at {path} is not a list — got {type(payload).__name__}."
        )
    return payload


def load_game_master() -> dict[str, Any]:
    """Return the parsed game master JSON."""
    path = game_master_cache_path()
    data = _read_json(path, description="Pokemon GO game master")
    if not isinstance(data, dict):
        raise ValueError(
            f"Game master cache at {path} is not a JSON object — got {type(data).__name__}."
        )
    return data


@dataclass(frozen=True)
class CacheMetadata:
    """Timestamps describing when each cache file was last synced."""

    rankings: dict[str, datetime]  # keyed by league slug
    game_master: datetime | None

    def pretty(self) -> dict[str, str]:
        """Human-readable ISO strings for display."""
        out: dict[str, str] = {
            f"rankings-{league}": ts.isoformat()
            for league, ts in sorted(self.rankings.items())
        }
        out["game_master"] = self.game_master.isoformat() if self.game_master else "never"
        return out


def write_cache_metadata(
    *,
    rankings: dict[str, datetime] | None = None,
    game_master: datetime | None = None,
) -> None:
    """Update the on-disk metadata file with new sync timestamps.

    Merges with the existing file rather than overwriting, so syncing one
    league does not erase timestamps for another.
    """
    existing = _safe_load_metadata()
    merged_rankings: dict[str, str] = {
        league: ts.isoformat() for league, ts in existing.rankings.items()
    }
    if rankings:
        for league, ts in rankings.items():
            merged_rankings[_normalize_league(league)] = ts.isoformat()
    gm_iso = (
        game_master.isoformat()
        if game_master is not None
        else (existing.game_master.isoformat() if existing.game_master else None)
    )
    payload = {"rankings": merged_rankings, "game_master": gm_iso}
    _write_json_atomic(metadata_path(), payload)


def _safe_load_metadata() -> CacheMetadata:
    path = metadata_path()
    if not path.exists():
        return CacheMetadata(rankings={}, game_master=None)
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    rankings: dict[str, datetime] = {}
    for league, iso in (raw.get("rankings") or {}).items():
        try:
            rankings[league] = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
    gm_iso = raw.get("game_master")
    gm = None
    if isinstance(gm_iso, str):
        try:
            gm = datetime.fromisoformat(gm_iso)
        except ValueError:
            gm = None
    return CacheMetadata(rankings=rankings, game_master=gm)


def get_cache_metadata() -> CacheMetadata:
    """Return when each cache file was last synced.

    The result is always a :class:`CacheMetadata` — even if nothing has been
    synced yet, in which case the fields are empty / ``None``. This avoids
    forcing callers to handle a missing file case separately.
    """
    return _safe_load_metadata()


def now_utc() -> datetime:
    """Convenience helper — all timestamps are stored in UTC."""
    return datetime.now(timezone.utc)


def iter_game_master_entries(game_master: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield item templates from a PokeMiners-style game master.

    The game master has historically used one of two top-level shapes — a list
    under ``"itemTemplates"`` (camelCase) or ``"item_templates"`` (snake case).
    Some snapshots put the list at the top level instead. This helper accepts
    all three and shields callers from the inconsistency.
    """
    if isinstance(game_master, list):
        for item in game_master:
            if isinstance(item, dict):
                yield item
        return
    for key in ("itemTemplates", "item_templates", "templates"):
        items = game_master.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield item
            return
    # Fallback: treat the entire dict as a single template list under "data".
    items = game_master.get("data")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                yield item
