"""Phase 1 smoke tests — verify the CLI skeleton is wired up correctly.

These tests do not exercise any business logic; they simply check that:
    * the Typer app loads without ImportError,
    * every expected command is registered,
    * commands with required arguments / constrained options surface clean errors.

By the end of Phase 8 every command is implemented, so the "still placeholder"
test that used to live here is gone — each command now has its own suite.
"""

from __future__ import annotations

from typer.testing import CliRunner

from pvp.cli import app


runner = CliRunner()


EXPECTED_COMMANDS = {
    "import-roster",
    "add",
    "list",
    "delete",
    "rank",
    "compare",
    "team",
    "sync",
    "ask",
}


def test_help_exits_zero_and_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in EXPECTED_COMMANDS:
        assert command in result.output, f"missing command '{command}' in --help output"


def test_rank_requires_species_argument() -> None:
    """Commands with required arguments should error out cleanly when called without them."""
    result = runner.invoke(app, ["rank"])
    assert result.exit_code != 0


def test_league_flag_rejects_bad_values() -> None:
    result = runner.invoke(app, ["rank", "Medicham", "--league", "XL"])
    assert result.exit_code != 0
