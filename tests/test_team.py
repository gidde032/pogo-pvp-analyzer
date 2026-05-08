"""Phase 7 tests — team builder engine + `pvp team` command."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pvp import data, engine, roster
from pvp.cli import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------


def _sample_roster() -> list[data.RosterEntry]:
    """A roster that mirrors the fixture species without touching the DB."""
    return [
        data.RosterEntry(
            id=1, species="Medicham",
            attack_iv=1, defense_iv=15, stamina_iv=14, level=50.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
        data.RosterEntry(
            id=2, species="Galarian Stunfisk",
            attack_iv=0, defense_iv=14, stamina_iv=14, level=25.5,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
        data.RosterEntry(
            id=3, species="Swampert",
            attack_iv=2, defense_iv=14, stamina_iv=15, level=21.5,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
        data.RosterEntry(
            id=4, species="Azumarill",
            attack_iv=0, defense_iv=15, stamina_iv=15, level=40.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
        data.RosterEntry(
            id=5, species="Registeel",
            attack_iv=3, defense_iv=15, stamina_iv=15, level=22.5,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
        data.RosterEntry(
            id=6, species="Talonflame",
            attack_iv=2, defense_iv=13, stamina_iv=14, level=41.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
    ]


@pytest.fixture
def loaded(fixtures_dir: Path) -> dict:
    """Load rankings + game master derivatives from the JSON fixtures."""
    import json
    rankings = json.loads((fixtures_dir / "rankings-gl.json").read_text())
    gm = json.loads((fixtures_dir / "game-master.json").read_text())
    return {
        "rankings": rankings,
        "bases": engine.extract_base_stats(gm),
        "type_chart": engine.extract_type_effectiveness(gm),
        "move_types": engine.extract_move_types(gm),
    }


# ---------------------------------------------------------------------------
# extract_move_types
# ---------------------------------------------------------------------------


def test_extract_move_types_indexes_canonical_and_bare_ids(loaded: dict) -> None:
    """Fast moves should be reachable under both ``FOO_FAST`` and ``FOO``."""
    mt = loaded["move_types"]
    assert mt["COUNTER_FAST"] == "fighting"
    assert mt["COUNTER"] == "fighting"
    assert mt["MUD_SHOT"] == "ground"
    assert mt["ICE_PUNCH"] == "ice"
    assert mt["FLASH_CANNON"] == "steel"


# ---------------------------------------------------------------------------
# super_effective_targets
# ---------------------------------------------------------------------------


def test_super_effective_targets_ice(loaded: dict) -> None:
    targets = engine.super_effective_targets(["ice"], loaded["type_chart"])
    # From the fixture: ice is super effective against dragon, flying, grass, ground.
    assert {"dragon", "flying", "grass", "ground"}.issubset(targets)
    # And not super-effective against water or fire (those are resisted).
    assert "water" not in targets
    assert "fire" not in targets


def test_super_effective_targets_combines_inputs(loaded: dict) -> None:
    targets = engine.super_effective_targets(
        ["fighting", "fairy"], loaded["type_chart"]
    )
    assert "dragon" in targets  # from fairy
    assert "dark" in targets  # fighting and fairy both hit dark
    assert "steel" in targets  # fighting hits steel


def test_super_effective_targets_ignores_unknown_attacker(loaded: dict) -> None:
    # "water" type chart exists; "mystery" does not.
    assert engine.super_effective_targets(["mystery"], loaded["type_chart"]) == set()


# ---------------------------------------------------------------------------
# build_team — without cover
# ---------------------------------------------------------------------------


def test_build_team_picks_top_three_by_rank(loaded: dict) -> None:
    result = engine.build_team(
        "gl",
        roster_entries=_sample_roster(),
        **loaded,
    )
    assert len(result.members) == 3
    species = [m.entry.species for m in result.members]
    # Top three ranks in the fixture are Medicham, Stunfisk, Swampert.
    assert species[0] == "Medicham"
    assert "Galarian Stunfisk" in species
    assert "Swampert" in species
    assert result.missing_coverage == ()
    assert result.note is None


def test_build_team_avoids_primary_type_collision(loaded: dict) -> None:
    """With two fighting-primary Pokemon in the roster, the builder should
    diversify — but only when an alternative exists.
    """
    roster_entries = _sample_roster()
    # Add a second fighting-primary entry (Medicham clone).
    roster_entries.append(
        data.RosterEntry(
            id=99, species="Medicham",
            attack_iv=15, defense_iv=1, stamina_iv=1, level=20.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        )
    )
    result = engine.build_team(
        "gl", roster_entries=roster_entries, **loaded,
    )
    primary_types = [m.types[0] if m.types else "" for m in result.members]
    # At most one fighting-primary member should make it.
    assert primary_types.count("fighting") <= 1


# ---------------------------------------------------------------------------
# build_team — with cover
# ---------------------------------------------------------------------------


def test_build_team_cover_fairy_selects_steel_counter(loaded: dict) -> None:
    result = engine.build_team(
        "gl",
        cover=["fairy"],
        roster_entries=_sample_roster(),
        **loaded,
    )
    species = [m.entry.species for m in result.members]
    # Stunfisk carries steel moves and is the highest-ranked fairy counter.
    assert "Galarian Stunfisk" in species
    assert result.missing_coverage == ()


def test_build_team_cover_dragon_is_covered_by_ice_or_fairy_move(
    loaded: dict,
) -> None:
    result = engine.build_team(
        "gl",
        cover=["dragon"],
        roster_entries=_sample_roster(),
        **loaded,
    )
    # Medicham has Ice Punch, Azumarill has Ice Beam + Play Rough — either
    # satisfies dragon coverage.
    assert result.missing_coverage == ()
    covering = [m for m in result.members if "dragon" in m.covers]
    assert covering, "expected at least one member to cover dragon"


def test_build_team_cover_impossible_type_reports_missing(loaded: dict) -> None:
    # No entry in the sample roster covers "ghost" — none of their moves hits
    # ghost super-effectively, and no one is ghost-typed.
    result = engine.build_team(
        "gl",
        cover=["ghost"],
        roster_entries=_sample_roster(),
        **loaded,
    )
    assert result.missing_coverage == ("ghost",)
    assert result.note and "ghost" in result.note
    # Team is still built with the best available.
    assert len(result.members) == 3


def test_build_team_cover_multiple_types(loaded: dict) -> None:
    result = engine.build_team(
        "gl",
        cover=["dragon", "fairy"],
        roster_entries=_sample_roster(),
        **loaded,
    )
    assert result.missing_coverage == ()
    covered = {c for m in result.members for c in m.covers}
    assert {"dragon", "fairy"}.issubset(covered)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_build_team_with_empty_roster(loaded: dict) -> None:
    result = engine.build_team("gl", roster_entries=[], **loaded)
    assert result.members == ()
    assert "0 roster member" in (result.note or "")


def test_build_team_with_only_two_entries(loaded: dict) -> None:
    entries = _sample_roster()[:2]
    result = engine.build_team("gl", roster_entries=entries, **loaded)
    assert len(result.members) == 2
    assert result.note is not None
    assert "Only 2" in result.note


def test_build_team_skips_species_without_base_stats(loaded: dict) -> None:
    entries = _sample_roster() + [
        data.RosterEntry(
            id=100, species="Gyarados",  # not in fixture game master
            attack_iv=5, defense_iv=5, stamina_iv=5, level=30.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
    ]
    result = engine.build_team("gl", roster_entries=entries, **loaded)
    # Gyarados should be silently skipped — there's nothing we know about it.
    assert all(m.entry.species != "Gyarados" for m in result.members)


def test_build_team_records_member_covers_when_no_cover_requested(
    loaded: dict,
) -> None:
    """Without a cover list, each member's ``covers`` is its own type(s).

    This fuels the rendering layer — `TeamRow` wants something to show in the
    Covers column even when the user didn't ask for specific coverage.
    """
    result = engine.build_team("gl", roster_entries=_sample_roster(), **loaded)
    for member in result.members:
        assert member.covers == member.types


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(cache_dir: Path, fixtures_dir: Path) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    roster.import_from_file(fixtures_dir / "sample-roster.csv")


def test_cli_team_happy_path(seeded) -> None:
    result = runner.invoke(app, ["team", "--league", "GL"])
    assert result.exit_code == 0, result.output
    assert "Team" in result.output
    # Three members picked — at minimum the top-ranked Medicham.
    assert "Medicham" in result.output


def test_cli_team_with_cover_option(seeded) -> None:
    result = runner.invoke(app, ["team", "--cover", "fairy"])
    assert result.exit_code == 0, result.output
    assert "covering fairy" in result.output.lower()


def test_cli_team_reports_missing_coverage(seeded) -> None:
    result = runner.invoke(app, ["team", "--cover", "ghost"])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output.lower()


def test_cli_team_empty_roster(cache_dir: Path, fixtures_dir: Path) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    # No roster imported.
    result = runner.invoke(app, ["team"])
    assert result.exit_code != 0
    assert "empty" in result.output.lower()


def test_cli_team_missing_cache(fixtures_dir: Path) -> None:
    # Roster is present but no cache files.
    roster.import_from_file(fixtures_dir / "sample-roster.csv")
    result = runner.invoke(app, ["team"])
    assert result.exit_code != 0
    assert "pvp sync" in result.output
