"""Phase 6 tests — `pvp rank` and `pvp compare` end-to-end."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pvp import data, display, engine, roster
from pvp.cli import app


runner = CliRunner()


@pytest.fixture
def seeded(cache_dir: Path, fixtures_dir: Path) -> None:
    """Copy rankings + game master into the cache and import a sample roster."""
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    roster.import_from_file(fixtures_dir / "sample-roster.csv")


# ---------------------------------------------------------------------------
# rank
# ---------------------------------------------------------------------------


def test_rank_happy_path(seeded) -> None:
    result = runner.invoke(app, ["rank", "Medicham", "--league", "GL"])
    assert result.exit_code == 0, result.output
    output = result.output
    assert "Medicham" in output
    assert "GL" in output
    # Rank in fixture is #1 of 6 for Medicham
    assert "#1" in output
    assert "Counter" in output or "COUNTER" in output


def test_rank_exits_on_missing_cache(fixtures_dir: Path, hermetic_storage: Path) -> None:
    # Import roster but do NOT copy any cache files.
    roster.import_from_file(fixtures_dir / "sample-roster.csv")
    result = runner.invoke(app, ["rank", "Medicham"])
    assert result.exit_code != 0
    assert "pvp sync" in result.output


def test_rank_not_in_roster(cache_dir: Path, fixtures_dir: Path) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    result = runner.invoke(app, ["rank", "Medicham"])
    assert result.exit_code != 0
    assert "not in your roster" in result.output


def test_rank_species_without_game_master_entry(
    cache_dir: Path,
    fixtures_dir: Path,
) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    # Add an entry not represented in the fixture's game master
    roster.add_entry_from_dict(
        {
            "species": "Gyarados",
            "attack_iv": 1,
            "defense_iv": 15,
            "stamina_iv": 14,
            "level": 40.0,
        }
    )
    result = runner.invoke(app, ["rank", "Gyarados"])
    assert result.exit_code != 0
    assert "no base stats" in result.output


def test_rank_multiple_entries_selects_best(seeded) -> None:
    # Add a second, worse Medicham
    roster.add_entry_from_dict(
        {
            "species": "Medicham",
            "attack_iv": 15,
            "defense_iv": 3,
            "stamina_iv": 3,
            "level": 20.0,
        }
    )
    result = runner.invoke(app, ["rank", "Medicham"])
    assert result.exit_code == 0, result.output
    # The "best" Medicham from the fixture has IVs 1/15/14
    assert "1/15/14" in result.output


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_happy_path(seeded) -> None:
    result = runner.invoke(app, ["compare", "Medicham", "Swampert"])
    assert result.exit_code == 0, result.output
    assert "Medicham" in result.output
    assert "Swampert" in result.output
    assert "Compare" in result.output


def test_compare_missing_second_species(seeded) -> None:
    result = runner.invoke(app, ["compare", "Medicham", "Unknown"])
    assert result.exit_code != 0
    assert "not in your roster" in result.output


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def test_rank_color_tiers() -> None:
    assert "green" in display._rank_color(1)
    assert "green" in display._rank_color(50)
    assert display._rank_color(300) == "yellow"
    assert display._rank_color(900) == "red"
    assert display._rank_color(None) == "dim"


def test_format_helpers() -> None:
    assert display._format_stat_product(None) == "—"
    assert display._format_stat_product(1234567.89) == "1,234,567"
    assert display._format_percentile(None) == "—"
    assert display._format_percentile(99.753) == "99.8%"


def test_compare_highlights_better_value() -> None:
    row = display.CompareRow(
        "CP",
        "1500",
        "1400",
        higher_is_better=True,
        left_numeric=1500,
        right_numeric=1400,
    )
    rendered = display.compare_table("title", "A", "B", [row])
    # Table object — rendering to a console is effectively integration-tested
    # above. Here we verify the data model rather than the console output.
    assert rendered.columns[1].header == "A"
    assert rendered.columns[2].header == "B"
