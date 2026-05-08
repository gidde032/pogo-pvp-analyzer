"""Command-line interface for the PVP Analyzer.

This module defines the Typer application and all command entry points.
In Phase 1, commands are placeholders that print "not implemented yet".
Subsequent phases wire each command to its implementation module.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from . import ai, data, display, engine, roster as roster_module, sync as sync_module
from .display import (
    console,
    print_error,
    print_info,
    print_success,
    print_warning,
)

app = typer.Typer(
    name="pvp",
    help="Pokemon GO PVP Roster Analyzer",
    no_args_is_help=True,
    add_completion=False,
)


class League(str, Enum):
    """Supported PVP leagues."""

    GL = "GL"
    UL = "UL"
    ML = "ML"


@app.command("import-roster")
def import_roster(
    path: Path = typer.Argument(..., help="Path to a CSV or JSON roster file"),
) -> None:
    """Import a roster from a CSV or JSON file.

    Valid rows are inserted; invalid rows are skipped and reported.
    """
    data.create_db_and_tables()
    try:
        report = roster_module.import_from_file(path)
    except FileNotFoundError as exc:
        print_error(
            console,
            str(exc),
            hint="Double-check the path, or use a CSV exported from PoGoHub / CalcyIV.",
        )
        raise typer.Exit(code=1)
    except ValueError as exc:
        print_error(console, str(exc))
        raise typer.Exit(code=1)

    print_success(
        console,
        f"Imported {len(report.imported)} / {report.total_rows} entries.",
    )
    if report.errors:
        print_warning(console, f"Skipped {len(report.errors)} invalid row(s):")
        for row_number, message in report.errors:
            console.print(f"  row {row_number}: {message}")


@app.command("add")
def add() -> None:
    """Add a single Pokemon to the roster interactively."""
    data.create_db_and_tables()
    species = typer.prompt("Species")
    attack_iv = typer.prompt("Attack IV", type=int)
    defense_iv = typer.prompt("Defense IV", type=int)
    stamina_iv = typer.prompt("Stamina IV", type=int)
    level = typer.prompt("Level", type=float)
    fast_move = typer.prompt("Fast move (optional)", default="", show_default=False)
    charge_move_1 = typer.prompt("Charge move 1 (optional)", default="", show_default=False)
    charge_move_2 = typer.prompt("Charge move 2 (optional)", default="", show_default=False)
    nickname = typer.prompt("Nickname (optional)", default="", show_default=False)

    payload = {
        "species": species,
        "attack_iv": attack_iv,
        "defense_iv": defense_iv,
        "stamina_iv": stamina_iv,
        "level": level,
        "fast_move": fast_move or None,
        "charge_move_1": charge_move_1 or None,
        "charge_move_2": charge_move_2 or None,
        "nickname": nickname or None,
    }
    try:
        entry = roster_module.add_entry_from_dict(payload)
    except Exception as exc:
        print_error(
            console,
            f"Invalid entry — {roster_module.format_error(exc)}",
            hint="Check IVs are 0–15 and level is a 0.5 step between 1 and 51.",
        )
        raise typer.Exit(code=1)
    print_success(console, f"Added id={entry.id} {entry.species}")


@app.command("list")
def list_roster() -> None:
    """List every Pokemon in the roster."""
    data.create_db_and_tables()
    entries = roster_module.list_all()
    if not entries:
        print_info(
            console,
            "Roster is empty. Run `pvp import-roster` or `pvp add` first.",
        )
        return
    console.print(display.roster_table(entries))


@app.command("delete")
def delete(
    identifier: str = typer.Argument(..., help="Entry ID or species name to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a roster entry by ID or species name."""
    data.create_db_and_tables()
    species_matches, id_match = roster_module.resolve_by_identifier(identifier)
    candidates = [id_match] if id_match is not None else species_matches
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        print_warning(console, f"No roster entry matched {identifier!r}.")
        raise typer.Exit(code=1)
    summary = ", ".join(
        f"#{c.id} {c.species} ({c.attack_iv}/{c.defense_iv}/{c.stamina_iv})"
        for c in candidates
    )
    if not yes:
        confirmed = typer.confirm(f"Delete {len(candidates)} entry(ies): {summary}?")
        if not confirmed:
            console.print("Cancelled.")
            return
    # Delete by the IDs already resolved above — avoids a second species-name
    # lookup, though a concurrent modification between resolve and delete is
    # still possible (single-user CLI makes this extremely unlikely).
    deleted = [c for c in candidates if data.delete_entry(c.id)]
    print_success(console, f"Deleted {len(deleted)} entry(ies).")


def _load_context_or_exit(league: str) -> tuple[list[dict], dict, list[float]]:
    """Load rankings + base stats + CPM table, or exit with a useful error."""
    try:
        return engine.load_analysis_context(league)
    except data.CacheMissError as exc:
        print_error(console, str(exc))
        raise typer.Exit(code=1)


def _select_roster_entry(species: str, league: League = League.GL) -> data.RosterEntry:
    matches = data.get_entries_by_species(species)
    if not matches:
        print_error(
            console,
            f"{species!r} is not in your roster.",
            hint="Run `pvp list` to see what is, or `pvp add` to add it.",
        )
        raise typer.Exit(code=1)
    if len(matches) > 1:
        # ML has no CP cap so high attack IV is desirable; capped leagues favour
        # low attack IV to maximise def/sta at a given CP.
        if league == League.ML:
            hint = "highest attack IV, highest def+sta"
            matches.sort(key=lambda e: (-e.attack_iv, -(e.defense_iv + e.stamina_iv)))
        else:
            hint = "lowest attack IV, highest def+sta"
            matches.sort(key=lambda e: (e.attack_iv, -(e.defense_iv + e.stamina_iv)))
        print_warning(
            console,
            f"Multiple {species} entries — using the one with the best IVs ({hint}).",
        )
    return matches[0]


def _build_rank_panel_data(
    entry: data.RosterEntry,
    league: str,
    rankings,
    bases,
    cpm,
) -> display.RankPanelData:
    try:
        base = engine.find_base_stats(entry.species, bases)
    except engine.SpeciesNotFound:
        print_error(
            console,
            f"no base stats for {entry.species!r} in the game master.",
            hint="It may have an unusual form — run `pvp sync` to refresh.",
        )
        raise typer.Exit(code=1)
    cp = engine.compute_cp(
        base.attack, base.defense, base.stamina,
        entry.attack_iv, entry.defense_iv, entry.stamina_iv,
        entry.level, cpm,
    )
    rank_result = None
    try:
        rank_result = engine.lookup_rank(entry.species, rankings)
    except engine.SpeciesNotFound:
        pass
    try:
        fast_move, charge_moves = engine.get_optimal_moveset(entry.species, rankings)
    except engine.SpeciesNotFound:
        fast_move, charge_moves = (entry.fast_move, [entry.charge_move_1 or "—", entry.charge_move_2 or "—"])
    stat_product = engine.compute_stat_product(
        base.attack, base.defense, base.stamina,
        entry.attack_iv, entry.defense_iv, entry.stamina_iv,
        entry.level, cpm,
    )
    return display.RankPanelData(
        entry=entry,
        league=league,
        cp=cp,
        rank=rank_result.rank if rank_result else None,
        total=rank_result.total if rank_result else None,
        percentile=rank_result.percentile if rank_result else None,
        stat_product=stat_product,
        score=rank_result.score if rank_result else None,
        fast_move=fast_move or (entry.fast_move or None),
        charge_moves=charge_moves or [m for m in (entry.charge_move_1, entry.charge_move_2) if m],
        notes=entry.notes,
    )


@app.command("rank")
def rank(
    species: str = typer.Argument(..., help="Species name to analyze"),
    league: League = typer.Option(League.GL, "--league", "-l", help="League to analyze"),
) -> None:
    """Show PVP rank, stat product, CP, and optimal moveset for a roster Pokemon."""
    data.create_db_and_tables()
    rankings, bases, cpm = _load_context_or_exit(league.value)
    entry = _select_roster_entry(species, league)
    panel_data = _build_rank_panel_data(entry, league.value, rankings, bases, cpm)
    console.print(display.rank_panel(panel_data))


@app.command("compare")
def compare(
    species_a: str = typer.Argument(..., help="First species"),
    species_b: str = typer.Argument(..., help="Second species"),
    league: League = typer.Option(League.GL, "--league", "-l", help="League to analyze"),
) -> None:
    """Compare two roster Pokemon head-to-head for a given league."""
    data.create_db_and_tables()
    rankings, bases, cpm = _load_context_or_exit(league.value)
    entry_a = _select_roster_entry(species_a, league)
    entry_b = _select_roster_entry(species_b, league)
    left = _build_rank_panel_data(entry_a, league.value, rankings, bases, cpm)
    right = _build_rank_panel_data(entry_b, league.value, rankings, bases, cpm)

    def _maybe(text: str | None) -> str:
        return text or "—"

    rows = [
        display.CompareRow(
            "IVs",
            f"{entry_a.attack_iv}/{entry_a.defense_iv}/{entry_a.stamina_iv}",
            f"{entry_b.attack_iv}/{entry_b.defense_iv}/{entry_b.stamina_iv}",
        ),
        display.CompareRow("Level", f"{entry_a.level:g}", f"{entry_b.level:g}"),
        display.CompareRow(
            "CP",
            str(left.cp),
            str(right.cp),
            higher_is_better=True,
            left_numeric=left.cp,
            right_numeric=right.cp,
        ),
        display.CompareRow(
            "Rank",
            f"#{left.rank}" if left.rank else "—",
            f"#{right.rank}" if right.rank else "—",
            higher_is_better=False,
            left_numeric=float(left.rank) if left.rank else None,
            right_numeric=float(right.rank) if right.rank else None,
        ),
        display.CompareRow(
            "Stat Product",
            f"{int(left.stat_product):,}" if left.stat_product is not None else "—",
            f"{int(right.stat_product):,}" if right.stat_product is not None else "—",
            higher_is_better=True,
            left_numeric=left.stat_product,
            right_numeric=right.stat_product,
        ),
        display.CompareRow("Fast Move", _maybe(left.fast_move), _maybe(right.fast_move)),
        display.CompareRow(
            "Charge 1",
            _maybe(left.charge_moves[0] if left.charge_moves else None),
            _maybe(right.charge_moves[0] if right.charge_moves else None),
        ),
        display.CompareRow(
            "Charge 2",
            _maybe(left.charge_moves[1] if len(left.charge_moves) > 1 else None),
            _maybe(right.charge_moves[1] if len(right.charge_moves) > 1 else None),
        ),
    ]
    table = display.compare_table(
        title=f"Compare — {league.value} League",
        left_name=entry_a.species,
        right_name=entry_b.species,
        rows=rows,
    )
    console.print(table)


@app.command("team")
def team(
    league: League = typer.Option(League.GL, "--league", "-l", help="League to analyze"),
    cover: Optional[str] = typer.Option(
        None,
        "--cover",
        "-c",
        help="Comma-separated types the team must cover (e.g. dragon,steel,water)",
    ),
) -> None:
    """Suggest the best team from the roster for the given league."""
    data.create_db_and_tables()
    try:
        rankings = data.load_pvpoke_rankings(league.value)
        gm = data.load_game_master()
    except data.CacheMissError as exc:
        print_error(console, str(exc))
        raise typer.Exit(code=1)

    cover_list = [c for c in (cover or "").split(",") if c.strip()]
    roster_entries = roster_module.list_all()
    if not roster_entries:
        print_warning(
            console,
            "Your roster is empty. Run `pvp import-roster` or `pvp add` first.",
        )
        raise typer.Exit(code=1)

    bases = engine.extract_base_stats(gm)
    type_chart = engine.extract_type_effectiveness(gm)
    move_types = engine.extract_move_types(gm)

    result = engine.build_team(
        league.value,
        cover=cover_list,
        rankings=rankings,
        bases=bases,
        type_chart=type_chart,
        move_types=move_types,
        roster_entries=roster_entries,
    )

    rows = [
        display.TeamRow(
            species=member.entry.species,
            rank=member.rank,
            types=member.types,
            covers=member.covers,
        )
        for member in result.members
    ]
    title = f"Team — {result.league} League"
    if cover_list:
        title += f" (covering {', '.join(t.lower() for t in cover_list)})"
    console.print(display.team_table(title, rows))
    if result.note:
        print_warning(console, result.note)


@app.command("sync")
def sync(
    skip_game_master: bool = typer.Option(
        False,
        "--skip-game-master",
        help="Skip the game master download (rankings only).",
    ),
) -> None:
    """Download the latest PvPoke rankings and game master."""
    console.print("[bold]Syncing PvP meta data[/bold]")
    result = sync_module.sync(
        include_game_master=not skip_game_master,
        progress=display.make_sync_progress(console),
    )
    if result.ok:
        print_success(
            console,
            f"Done. Updated: {', '.join(result.updated) or '(nothing)'}",
        )
    else:
        for resource, reason in result.failed.items():
            print_error(console, f"{resource} — {reason}")
        raise typer.Exit(code=1)

    meta = data.get_cache_metadata()
    pretty = meta.pretty()
    console.print()
    print_info(console, "Cache timestamps:")
    for key, value in pretty.items():
        console.print(f"  [dim]{key}:[/dim] {value}")


@app.command("ask")
def ask(
    query: str = typer.Argument(..., help="Natural-language question about your roster"),
    league: League = typer.Option(League.GL, "--league", "-l", help="League context"),
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Print streamed output as plain text instead of Rich markdown.",
    ),
) -> None:
    """Ask Claude a free-text question about your roster."""
    data.create_db_and_tables()
    entries = roster_module.list_all()
    if not entries:
        print_warning(
            console,
            "Your roster is empty. Run `pvp import-roster` or `pvp add` first.",
        )
        raise typer.Exit(code=1)

    if plain:
        def on_chunk(chunk: str) -> None:
            console.print(chunk, end="", soft_wrap=True, highlight=False)

        def finish() -> str:  # pragma: no cover — trivial
            console.print()
            return ""
    else:
        on_chunk, finish = display.make_live_renderer(console)

    try:
        ai.ask(query, league.value, on_chunk=on_chunk)
    except ai.AIConfigError as exc:
        finish()
        print_error(console, str(exc))
        raise typer.Exit(code=1)
    except Exception:
        finish()
        raise
    finish()


if __name__ == "__main__":
    app()
