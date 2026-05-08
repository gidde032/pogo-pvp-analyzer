"""Phase 10 tests — close coverage gaps left by earlier phases.

Prior phases covered their own surface area well, but a few branches slipped
through:

* the interactive ``pvp add`` command was never driven by a test,
* ``pvp delete`` with an identifier that matches nothing had no test,
* ``engine.max_level_under_cap`` had no case exercising the "even L1 is
  over the cap" fallback,
* ``engine.build_team`` had no case exercising the partial-dependency-
  injection path (caller passes some of bases/type_chart/move_types but
  not all).

Each test below closes exactly one of those branches so we get a clear
signal if someone regresses the behaviour later.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pvp import data, engine, roster


# ---------------------------------------------------------------------------
# Fixture helpers — re-used from Phase 4's pattern so this file is self-
# sufficient for grep / coverage inspection.
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded(cache_dir: Path, fixtures_dir: Path):
    """Copy pinned fixtures into the hermetic cache and expose parsed handles."""
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    gm = data.load_game_master()
    return {
        "rankings": data.load_pvpoke_rankings("GL"),
        "gm": gm,
        "cpm": engine.extract_cpm_table(gm),
        "bases": engine.extract_base_stats(gm),
        "type_chart": engine.extract_type_effectiveness(gm),
        "move_types": engine.extract_move_types(gm),
    }


# ---------------------------------------------------------------------------
# ``pvp add`` — interactive command, driven by stdin
# ---------------------------------------------------------------------------


def _prompt_answers(
    *,
    species: str = "Medicham",
    attack_iv: int = 1,
    defense_iv: int = 15,
    stamina_iv: int = 14,
    level: float = 40.5,
    fast_move: str = "",
    charge_move_1: str = "",
    charge_move_2: str = "",
    nickname: str = "",
) -> str:
    """Produce the newline-joined input the ``add`` command expects."""
    return (
        f"{species}\n{attack_iv}\n{defense_iv}\n{stamina_iv}\n{level}\n"
        f"{fast_move}\n{charge_move_1}\n{charge_move_2}\n{nickname}\n"
    )


def test_cli_add_happy_path() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["add"], input=_prompt_answers())
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    assert "Medicham" in result.output
    assert len(data.get_entries_by_species("Medicham")) == 1


def test_cli_add_with_moves_and_nickname() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["add"],
        input=_prompt_answers(
            fast_move="COUNTER",
            charge_move_1="ICE_PUNCH",
            charge_move_2="DYNAMIC_PUNCH",
            nickname="Zen",
        ),
    )
    assert result.exit_code == 0, result.output
    entry = data.get_entries_by_species("Medicham")[0]
    assert entry.nickname == "Zen"
    assert entry.fast_move == "COUNTER"
    assert entry.charge_move_1 == "ICE_PUNCH"
    assert entry.charge_move_2 == "DYNAMIC_PUNCH"


def test_cli_add_rejects_invalid_iv() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["add"], input=_prompt_answers(attack_iv=20))
    assert result.exit_code != 0
    assert "Error:" in result.output
    # Hint is part of the unified error helper and should surface here.
    assert "IVs" in result.output
    # Nothing should have landed in the DB.
    assert data.get_entries_by_species("Medicham") == []


# ---------------------------------------------------------------------------
# ``pvp delete`` — no matching identifier
# ---------------------------------------------------------------------------


def test_cli_delete_no_match_exits_nonzero() -> None:
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["delete", "NoSuchSpecies", "--yes"])
    assert result.exit_code != 0
    assert "No roster entry matched" in result.output
    assert "NoSuchSpecies" in result.output


def test_cli_delete_no_match_by_numeric_id() -> None:
    """Delete by an integer id that doesn't exist also hits the no-match path."""
    from pvp.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["delete", "9999", "--yes"])
    assert result.exit_code != 0
    assert "No roster entry matched" in result.output


# ---------------------------------------------------------------------------
# Engine — ``max_level_under_cap`` with an impossibly low cap
# ---------------------------------------------------------------------------


def test_max_level_under_cap_returns_l1_when_cap_impossible(loaded) -> None:
    """If even a level-1 0/0/0 build is over the cap, we still return L1 data.

    The caller shouldn't get ``None`` — that would force every display layer
    to handle the edge case separately. This documents and locks in the
    "always return something" contract.
    """
    base = engine.find_base_stats("Medicham", loaded["bases"])
    # Medicham at L1 with 15/15/15 produces a CP well above 10; pick a cap
    # below that so every (level, ivs) combination is disqualified.
    build = engine.max_level_under_cap(
        base, 15, 15, 15,
        cpm_table=loaded["cpm"],
        cp_cap=1,
    )
    assert build.level == 1.0
    assert build.cp >= 10  # the CP-10 floor still applies
    assert build.stat_product > 0


# ---------------------------------------------------------------------------
# Engine — ``build_team`` with partial dependency injection
# ---------------------------------------------------------------------------


def test_build_team_loads_missing_dependencies_from_disk(loaded) -> None:
    """If the caller hands in ``rankings`` but omits bases/type_chart/
    move_types, ``build_team`` loads the game master from disk to fill the
    gaps. This branch exists because the CLI passes a single combined blob,
    whereas unit tests usually pass everything explicitly.
    """
    roster.add_entry_from_dict({
        "species": "Medicham",
        "attack_iv": 1, "defense_iv": 15, "stamina_iv": 14, "level": 40.5,
    })
    roster.add_entry_from_dict({
        "species": "Azumarill",
        "attack_iv": 0, "defense_iv": 15, "stamina_iv": 15, "level": 40.0,
    })
    roster.add_entry_from_dict({
        "species": "Registeel",
        "attack_iv": 3, "defense_iv": 15, "stamina_iv": 15, "level": 27.5,
    })
    # Pass rankings explicitly but let bases/type_chart/move_types come from
    # the cache (which ``loaded`` populated).
    result = engine.build_team(
        "GL",
        cover=None,
        rankings=loaded["rankings"],
        target_size=3,
    )
    assert len(result.members) == 3
    assert {m.entry.species for m in result.members} == {
        "Medicham", "Azumarill", "Registeel",
    }
