"""Phase 2 tests — data layer (SQLAlchemy roster and JSON meta loaders)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pvp import data


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------


def test_home_dir_respects_environment(hermetic_storage: Path) -> None:
    assert data.get_home_dir() == hermetic_storage
    assert data.get_cache_dir() == hermetic_storage / "cache"
    assert data.get_db_path() == hermetic_storage / "roster.db"


# ---------------------------------------------------------------------------
# Roster CRUD
# ---------------------------------------------------------------------------


def _medicham(**overrides) -> data.RosterEntry:
    defaults = dict(
        species="Medicham",
        pokedex_number=308,
        attack_iv=1,
        defense_iv=15,
        stamina_iv=14,
        level=40.5,
        fast_move="COUNTER_FAST",
        charge_move_1="ICE_PUNCH",
        charge_move_2="DYNAMIC_PUNCH",
        nickname="Wall-breaker",
        notes="best under 1500 CP",
    )
    defaults.update(overrides)
    return data.RosterEntry(**defaults)


def test_create_db_and_tables_is_idempotent() -> None:
    data.create_db_and_tables()
    data.create_db_and_tables()  # second call should not raise


def test_add_and_retrieve_entry() -> None:
    entry = data.add_entry(_medicham())
    assert entry.id is not None
    fetched = data.get_entry_by_id(entry.id)
    assert fetched is not None
    assert fetched.species == "Medicham"
    assert fetched.iv_tuple == (1, 15, 14)
    assert fetched.nickname == "Wall-breaker"


def test_add_entries_bulk() -> None:
    items = data.add_entries(
        [
            _medicham(),
            _medicham(species="Azumarill", attack_iv=0, defense_iv=14, stamina_iv=15),
        ]
    )
    assert all(item.id is not None for item in items)
    assert {item.species for item in items} == {"Medicham", "Azumarill"}


def test_get_all_entries_empty() -> None:
    assert data.get_all_entries() == []


def test_get_all_entries_ordering() -> None:
    data.add_entry(_medicham())
    data.add_entry(_medicham(species="Azumarill"))
    ordered = data.get_all_entries()
    assert [e.species for e in ordered] == ["Medicham", "Azumarill"]


def test_delete_entry() -> None:
    entry = data.add_entry(_medicham())
    assert data.delete_entry(entry.id) is True
    assert data.get_entry_by_id(entry.id) is None


def test_delete_entry_missing_id() -> None:
    assert data.delete_entry(9999) is False


def test_get_entries_by_species_is_case_insensitive() -> None:
    data.add_entry(_medicham())
    data.add_entry(_medicham(nickname="backup"))
    matches = data.get_entries_by_species("medicham")
    assert len(matches) == 2
    assert {e.nickname for e in matches} == {"Wall-breaker", "backup"}


def test_roster_entry_to_dict_contains_all_fields() -> None:
    entry = data.add_entry(_medicham())
    as_dict = entry.to_dict()
    for field in (
        "id",
        "species",
        "pokedex_number",
        "attack_iv",
        "defense_iv",
        "stamina_iv",
        "level",
        "fast_move",
        "charge_move_1",
        "charge_move_2",
        "nickname",
        "notes",
    ):
        assert field in as_dict


# ---------------------------------------------------------------------------
# JSON loaders
# ---------------------------------------------------------------------------


def _copy_fixtures(cache_dir: Path, fixtures_dir: Path) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")


def test_load_rankings_missing_raises_cache_miss(cache_dir: Path) -> None:
    with pytest.raises(data.CacheMissError) as excinfo:
        data.load_pvpoke_rankings("GL")
    # The error message must mention how to fix the problem.
    assert "pvp sync" in str(excinfo.value)


def test_load_rankings_happy_path(cache_dir: Path, fixtures_dir: Path) -> None:
    _copy_fixtures(cache_dir, fixtures_dir)
    rankings = data.load_pvpoke_rankings("gl")
    assert isinstance(rankings, list)
    assert any(entry["speciesId"] == "medicham" for entry in rankings)


def test_load_rankings_league_aliases(cache_dir: Path, fixtures_dir: Path) -> None:
    _copy_fixtures(cache_dir, fixtures_dir)
    a = data.load_pvpoke_rankings("GL")
    b = data.load_pvpoke_rankings("great")
    c = data.load_pvpoke_rankings("Great-League")
    assert a == b == c


def test_load_rankings_rejects_unknown_league() -> None:
    with pytest.raises(ValueError):
        data.load_pvpoke_rankings("elite-league")


def test_load_rankings_malformed_payload(cache_dir: Path) -> None:
    (cache_dir / "rankings-gl.json").write_text('{"oops": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        data.load_pvpoke_rankings("GL")


def test_load_rankings_corrupted_json_raises_valueerror(cache_dir: Path) -> None:
    (cache_dir / "rankings-gl.json").write_text('{"truncated":', encoding="utf-8")
    with pytest.raises(ValueError, match="pvp sync"):
        data.load_pvpoke_rankings("GL")


def test_load_game_master_corrupted_json_raises_valueerror(cache_dir: Path) -> None:
    (cache_dir / "game-master.json").write_text('not json at all', encoding="utf-8")
    with pytest.raises(ValueError, match="pvp sync"):
        data.load_game_master()


def test_load_game_master_happy_path(cache_dir: Path, fixtures_dir: Path) -> None:
    _copy_fixtures(cache_dir, fixtures_dir)
    gm = data.load_game_master()
    assert isinstance(gm, dict)
    assert "itemTemplates" in gm


def test_load_game_master_missing(cache_dir: Path) -> None:
    with pytest.raises(data.CacheMissError):
        data.load_game_master()


def test_iter_game_master_entries_handles_list_shape() -> None:
    items = list(data.iter_game_master_entries([{"a": 1}, "nope", {"b": 2}]))
    assert items == [{"a": 1}, {"b": 2}]


def test_iter_game_master_entries_handles_snake_case() -> None:
    items = list(
        data.iter_game_master_entries({"item_templates": [{"x": 1}, {"y": 2}]})
    )
    assert items == [{"x": 1}, {"y": 2}]


def test_iter_game_master_entries_handles_camel_case(fixtures_dir: Path) -> None:
    gm = json.loads((fixtures_dir / "game-master.json").read_text())
    items = list(data.iter_game_master_entries(gm))
    assert len(items) >= 5
    assert all("templateId" in it for it in items)


# ---------------------------------------------------------------------------
# Cache metadata
# ---------------------------------------------------------------------------


def test_write_cache_metadata_is_atomic(cache_dir: Path) -> None:
    # Verify write_cache_metadata uses an atomic tmp→rename pattern: no .tmp
    # file should be left on disk after a successful write.
    ts = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    data.write_cache_metadata(rankings={"GL": ts})
    leftover_tmp = list(data.get_cache_dir().glob("*.tmp"))
    assert leftover_tmp == [], f"Stale .tmp files after write: {leftover_tmp}"
    # The real metadata file must be valid JSON.
    import json as _json
    raw = (data.metadata_path()).read_text()
    parsed = _json.loads(raw)
    assert "rankings" in parsed


def test_cache_metadata_empty_before_any_sync(cache_dir: Path) -> None:
    meta = data.get_cache_metadata()
    assert meta.rankings == {}
    assert meta.game_master is None


def test_write_and_read_cache_metadata(cache_dir: Path) -> None:
    ts = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    data.write_cache_metadata(rankings={"GL": ts}, game_master=ts)
    meta = data.get_cache_metadata()
    assert meta.rankings["gl"] == ts
    assert meta.game_master == ts


def test_write_cache_metadata_merges(cache_dir: Path) -> None:
    t1 = datetime(2026, 4, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 2, tzinfo=timezone.utc)
    data.write_cache_metadata(rankings={"GL": t1})
    data.write_cache_metadata(rankings={"UL": t2})
    meta = data.get_cache_metadata()
    assert meta.rankings["gl"] == t1
    assert meta.rankings["ul"] == t2


def test_cache_metadata_pretty() -> None:
    meta = data.CacheMetadata(
        rankings={"gl": datetime(2026, 4, 1, tzinfo=timezone.utc)},
        game_master=None,
    )
    pretty = meta.pretty()
    assert pretty["game_master"] == "never"
    assert pretty["rankings-gl"].startswith("2026-04-01")
