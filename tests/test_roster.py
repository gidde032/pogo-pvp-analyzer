"""Phase 5 tests — roster import, add, list, delete."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pvp import data, roster


# ---------------------------------------------------------------------------
# Import — CSV
# ---------------------------------------------------------------------------


def test_import_csv_happy_path(fixtures_dir: Path) -> None:
    report = roster.import_from_file(fixtures_dir / "sample-roster.csv")
    assert report.errors == []
    assert len(report.imported) == 6
    species = {e.species for e in report.imported}
    assert "Medicham" in species and "Azumarill" in species


def test_import_csv_preserves_half_levels(fixtures_dir: Path) -> None:
    roster.import_from_file(fixtures_dir / "sample-roster.csv")
    medicham = data.get_entries_by_species("Medicham")[0]
    assert medicham.level == 40.5


def test_import_csv_missing_required_column(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    with path.open("w", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["species", "attack_iv", "defense_iv"])  # no stamina/level
        writer.writerow(["Medicham", 1, 15])
    with pytest.raises(ValueError) as exc:
        roster.import_from_file(path)
    assert "missing required columns" in str(exc.value)


def test_import_csv_reports_invalid_row_without_aborting(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    with path.open("w", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["species", "attack_iv", "defense_iv", "stamina_iv", "level"])
        writer.writerow(["Medicham", 1, 15, 14, 40.5])       # valid
        writer.writerow(["Swampert", 16, 15, 15, 40])         # bad IV
        writer.writerow(["Azumarill", 0, 14, 15, 52])         # bad level
        writer.writerow(["", 0, 0, 0, 20])                     # empty species
        writer.writerow(["Registeel", 3, 15, 15, 27.5])       # valid
    report = roster.import_from_file(path)
    assert len(report.imported) == 2
    assert len(report.errors) == 3
    # Still persisted to the DB
    assert len(data.get_all_entries()) == 2


def test_import_csv_ignores_extra_columns(tmp_path: Path) -> None:
    path = tmp_path / "extra.csv"
    with path.open("w", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["species", "attack_iv", "defense_iv", "stamina_iv", "level", "cp_hint"])
        writer.writerow(["Medicham", 1, 15, 14, 40.5, "1496"])
    report = roster.import_from_file(path)
    assert len(report.imported) == 1


def test_import_csv_accepts_level_51(tmp_path: Path, fixtures_dir: Path) -> None:
    # Level 51 (Best Buddy cap) must be accepted by the validator and must not
    # crash the CP engine.  Previously this silently failed because the fixture
    # CPM table only had 50 entries.
    path = tmp_path / "buddy.csv"
    with path.open("w", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["species", "attack_iv", "defense_iv", "stamina_iv", "level"])
        writer.writerow(["Medicham", 1, 15, 14, 51])
    report = roster.import_from_file(path)
    assert len(report.imported) == 1
    assert report.errors == []
    assert data.get_all_entries()[0].level == 51.0


def test_import_csv_rejects_non_half_level(tmp_path: Path) -> None:
    path = tmp_path / "fraction.csv"
    with path.open("w", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["species", "attack_iv", "defense_iv", "stamina_iv", "level"])
        writer.writerow(["Medicham", 1, 15, 14, 40.3])
    report = roster.import_from_file(path)
    assert len(report.imported) == 0
    assert "level" in report.errors[0][1]


# ---------------------------------------------------------------------------
# Import — JSON
# ---------------------------------------------------------------------------


def test_import_json_happy_path(fixtures_dir: Path) -> None:
    report = roster.import_from_file(fixtures_dir / "sample-roster.json")
    assert len(report.imported) == 2
    assert report.errors == []


def test_import_json_entries_key(tmp_path: Path) -> None:
    path = tmp_path / "entries.json"
    path.write_text(
        '{"entries": [{"species": "Medicham", "attack_iv": 1, "defense_iv": 15, '
        '"stamina_iv": 14, "level": 40.5}]}'
    )
    report = roster.import_from_file(path)
    assert len(report.imported) == 1
    assert report.errors == []


def test_import_json_root_list(tmp_path: Path) -> None:
    path = tmp_path / "flat.json"
    path.write_text(
        '[{"species": "Medicham", "attack_iv": 1, "defense_iv": 15, '
        '"stamina_iv": 14, "level": 40.5}]'
    )
    report = roster.import_from_file(path)
    assert len(report.imported) == 1


def test_import_json_reports_non_dict_items(tmp_path: Path) -> None:
    # Non-dict items inside the list must appear in errors, not be silently dropped.
    path = tmp_path / "mixed.json"
    path.write_text(
        '[{"species": "Medicham", "attack_iv": 1, "defense_iv": 15, '
        '"stamina_iv": 14, "level": 40.5}, "oops", 42]'
    )
    report = roster.import_from_file(path)
    assert len(report.imported) == 1
    assert len(report.errors) == 2
    assert any("str" in msg for _, msg in report.errors)
    assert any("int" in msg for _, msg in report.errors)


def test_import_json_rejects_top_level_scalar(tmp_path: Path) -> None:
    path = tmp_path / "scalar.json"
    path.write_text('"not-a-list"')
    with pytest.raises(ValueError):
        roster.import_from_file(path)


def test_import_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "roster.yaml"
    path.write_text("whatever")
    with pytest.raises(ValueError) as exc:
        roster.import_from_file(path)
    assert "Unsupported file type" in str(exc.value)


def test_import_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        roster.import_from_file("/nonexistent/path.csv")


# ---------------------------------------------------------------------------
# Add / list / delete
# ---------------------------------------------------------------------------


def _sample() -> dict:
    return {
        "species": "Medicham",
        "attack_iv": 1,
        "defense_iv": 15,
        "stamina_iv": 14,
        "level": 40.5,
    }


def test_add_entry_from_dict() -> None:
    entry = roster.add_entry_from_dict(_sample())
    assert entry.id is not None
    assert entry.species == "Medicham"


def test_add_entry_rejects_invalid_iv() -> None:
    with pytest.raises(Exception):
        roster.add_entry_from_dict({**_sample(), "attack_iv": 20})


def test_list_all_returns_inserted_entries() -> None:
    roster.add_entry_from_dict(_sample())
    roster.add_entry_from_dict({**_sample(), "species": "Azumarill"})
    all_entries = roster.list_all()
    assert {e.species for e in all_entries} == {"Medicham", "Azumarill"}


def test_duplicate_species_supported() -> None:
    roster.add_entry_from_dict(_sample())
    roster.add_entry_from_dict({**_sample(), "nickname": "backup"})
    matches = data.get_entries_by_species("Medicham")
    assert len(matches) == 2


def test_delete_by_id() -> None:
    entry = roster.add_entry_from_dict(_sample())
    assert roster.delete_by_id(entry.id) is True
    assert data.get_entry_by_id(entry.id) is None


def test_delete_by_identifier_prefers_id_over_species() -> None:
    roster.add_entry_from_dict(_sample())
    second = roster.add_entry_from_dict({**_sample(), "nickname": "backup"})
    deleted = roster.delete_by_identifier(str(second.id))
    # Only the id-match was deleted even though species matches existed
    assert [e.id for e in deleted] == [second.id]
    remaining = data.get_entries_by_species("Medicham")
    assert len(remaining) == 1


def test_delete_by_identifier_species_match() -> None:
    roster.add_entry_from_dict(_sample())
    roster.add_entry_from_dict({**_sample(), "nickname": "backup"})
    deleted = roster.delete_by_identifier("Medicham")
    assert len(deleted) == 2
    assert data.get_all_entries() == []


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_list_empty() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "empty" in result.output.lower()


def test_cli_import_roster_reports_counts(fixtures_dir: Path) -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["import-roster", str(fixtures_dir / "sample-roster.csv")])
    assert result.exit_code == 0
    assert "Imported" in result.output


def test_cli_import_roster_missing_file() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["import-roster", "/nope.csv"])
    assert result.exit_code != 0
    assert "Error" in result.output


def test_cli_delete_requires_confirmation_by_default(fixtures_dir: Path) -> None:
    from pvp.cli import app

    runner = CliRunner()
    runner.invoke(app, ["import-roster", str(fixtures_dir / "sample-roster.csv")])
    # Answer 'no'
    result = runner.invoke(app, ["delete", "Medicham"], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert len(data.get_entries_by_species("Medicham")) == 1


def test_cli_delete_with_yes_skips_confirmation(fixtures_dir: Path) -> None:
    from pvp.cli import app

    runner = CliRunner()
    runner.invoke(app, ["import-roster", str(fixtures_dir / "sample-roster.csv")])
    result = runner.invoke(app, ["delete", "Medicham", "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    assert data.get_entries_by_species("Medicham") == []
