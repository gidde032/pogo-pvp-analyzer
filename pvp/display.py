"""Display layer — Rich formatting helpers shared by CLI commands.

After Phase 9 this is the only module in the package that imports from
``rich``. Every other module talks to Rich via the helpers defined here
(``roster_table``, ``rank_panel``, ``compare_table``, ``team_table``,
``make_live_renderer``) or through ``print_error`` / ``print_warning`` /
``print_success`` for consistent inline messaging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import data


# ---------------------------------------------------------------------------
# Data classes used by the render helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankPanelData:
    """Everything the ``pvp rank`` command needs to display one Pokemon."""

    entry: data.RosterEntry
    league: str
    cp: int
    rank: int | None
    total: int | None
    percentile: float | None
    stat_product: float | None
    score: float | None
    fast_move: str | None
    charge_moves: Sequence[str]
    notes: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rank_color(rank: int | None) -> str:
    """Consistent colour coding for rank values.

    The spec (Phase 9 review) calls for rank colouring; Phase 6 seeds the
    scheme so `compare` highlights it correctly out of the gate.
    """
    if rank is None:
        return "dim"
    if rank <= 20:
        return "bold green"
    if rank <= 100:
        return "green"
    if rank <= 500:
        return "yellow"
    return "red"


def _format_ivs(entry: data.RosterEntry) -> str:
    return f"{entry.attack_iv}/{entry.defense_iv}/{entry.stamina_iv}"


def _format_stat_product(sp: float | None) -> str:
    if sp is None:
        return "—"
    return f"{int(sp):,}"


def _format_percentile(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p:.1f}%"


# ---------------------------------------------------------------------------
# Rank panel
# ---------------------------------------------------------------------------


def rank_panel(data_obj: RankPanelData) -> Panel:
    """Build the two-column panel shown by the ``pvp rank`` command."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    rank_text = Text("not in meta", style="dim")
    if data_obj.rank is not None and data_obj.total is not None:
        rank_text = Text(
            f"#{data_obj.rank} / {data_obj.total}",
            style=_rank_color(data_obj.rank),
        )

    table.add_row("Your IVs", _format_ivs(data_obj.entry))
    table.add_row("Your Level", f"{data_obj.entry.level:g}")
    table.add_row("Your CP", str(data_obj.cp))
    table.add_row("Rank", rank_text)
    table.add_row("Stat Product", _format_stat_product(data_obj.stat_product))
    table.add_row("Percentile", _format_percentile(data_obj.percentile))
    if data_obj.score is not None:
        table.add_row("Meta Score", f"{data_obj.score:.1f}")
    table.add_row("Fast Move", data_obj.fast_move or "—")
    charge_1 = data_obj.charge_moves[0] if len(data_obj.charge_moves) >= 1 else "—"
    charge_2 = data_obj.charge_moves[1] if len(data_obj.charge_moves) >= 2 else "—"
    table.add_row("Charge Move 1", charge_1)
    table.add_row("Charge Move 2", charge_2)
    if data_obj.notes:
        table.add_row("Notes", data_obj.notes)

    title = f"{data_obj.entry.species} — {data_obj.league.upper()} Analysis"
    return Panel(table, title=title, title_align="left")


# ---------------------------------------------------------------------------
# Compare table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareRow:
    label: str
    left: str | Text
    right: str | Text
    higher_is_better: bool = True
    left_numeric: float | None = None
    right_numeric: float | None = None


def _render_compare_value(
    value: str | Text,
    numeric: float | None,
    other_numeric: float | None,
    higher_is_better: bool,
) -> Text:
    if isinstance(value, Text):
        text = value
    else:
        text = Text(value)
    if numeric is None or other_numeric is None or numeric == other_numeric:
        return text
    # Mark the "better" side in green.
    better = (numeric > other_numeric) if higher_is_better else (numeric < other_numeric)
    if better:
        text.stylize("bold green")
    return text


def compare_table(
    title: str,
    left_name: str,
    right_name: str,
    rows: Iterable[CompareRow],
) -> Table:
    table = Table(title=title)
    table.add_column("", style="dim")
    table.add_column(left_name, justify="center")
    table.add_column(right_name, justify="center")
    for row in rows:
        left = _render_compare_value(
            row.left, row.left_numeric, row.right_numeric, row.higher_is_better
        )
        right = _render_compare_value(
            row.right, row.right_numeric, row.left_numeric, row.higher_is_better
        )
        table.add_row(row.label, left, right)
    return table


# ---------------------------------------------------------------------------
# Team table (seeded for Phase 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamRow:
    species: str
    rank: int | None
    types: tuple[str, ...]
    covers: tuple[str, ...]


def team_table(title: str, rows: Iterable[TeamRow]) -> Table:
    table = Table(title=title)
    table.add_column("Pokemon")
    table.add_column("Rank", justify="right")
    table.add_column("Type")
    table.add_column("Covers")
    for row in rows:
        rank = Text(f"#{row.rank}" if row.rank else "—", style=_rank_color(row.rank))
        table.add_row(
            row.species,
            rank,
            "/".join(t.upper()[:3] for t in row.types),
            ", ".join(c.capitalize() for c in row.covers) or "—",
        )
    return table


# ---------------------------------------------------------------------------
# Roster table (Phase 9 move — used by `pvp list`)
# ---------------------------------------------------------------------------


def roster_table(entries: Sequence[data.RosterEntry]) -> Table:
    """Render the full roster — one row per entry. Columns match the CSV schema."""
    table = Table(title=f"Roster ({len(entries)} entries)")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Species")
    table.add_column("IVs", justify="center")
    table.add_column("Level", justify="right")
    table.add_column("Fast")
    table.add_column("Charge 1")
    table.add_column("Charge 2")
    table.add_column("Nickname")
    for entry in entries:
        table.add_row(
            str(entry.id),
            entry.species,
            f"{entry.attack_iv}/{entry.defense_iv}/{entry.stamina_iv}",
            f"{entry.level:g}",
            entry.fast_move or "—",
            entry.charge_move_1 or "—",
            entry.charge_move_2 or "—",
            entry.nickname or "",
        )
    return table


# ---------------------------------------------------------------------------
# Messaging helpers (Phase 9)
# ---------------------------------------------------------------------------


def print_error(console: Console, message: str, *, hint: str | None = None) -> None:
    """Standard error formatting: bold red prefix, optional how-to-fix hint.

    Every command that fails should go through here so users see the same
    visual shape regardless of which module raised.
    """
    console.print(f"[bold red]Error:[/bold red] {message}")
    if hint:
        console.print(f"  [dim]→[/dim] {hint}")


def print_warning(console: Console, message: str) -> None:
    console.print(f"[yellow]{message}[/yellow]")


def print_success(console: Console, message: str) -> None:
    console.print(f"[green]{message}[/green]")


def print_info(console: Console, message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


# ---------------------------------------------------------------------------
# Sync progress (Phase 9 move — used by `pvp sync`)
# ---------------------------------------------------------------------------


def make_sync_progress(console: Console) -> Callable[[str, str], None]:
    """Return a ``(stage, message)`` callback for :func:`pvp.sync.sync`."""

    def _progress(stage: str, message: str) -> None:
        if stage == "start":
            console.print(f"  [cyan]•[/cyan] fetching {message}…")
        elif stage == "ok":
            console.print(f"  [green]✓[/green] {message}")
        elif stage == "fail":
            console.print(f"  [red]✗[/red] {message}")

    return _progress


# ---------------------------------------------------------------------------
# Streaming renderer (Phase 9 move from ai.py)
# ---------------------------------------------------------------------------


def make_live_renderer(
    console: Console,
) -> tuple[Callable[[str], None], Callable[[], str]]:
    """Return ``(on_chunk, finish)`` that render accumulating text via Rich Live.

    Kept in ``display`` so the AI layer has no Rich dependency. The CLI is
    the only caller.
    """
    from rich.live import Live
    from rich.markdown import Markdown

    buffer: list[str] = []
    live = Live("", console=console, refresh_per_second=10, transient=False)
    live.start()
    _stopped = False

    def on_chunk(chunk: str) -> None:
        buffer.append(chunk)
        live.update(Markdown("".join(buffer)))

    def finish() -> str:
        nonlocal _stopped
        if not _stopped:
            _stopped = True
            live.stop()
        return "".join(buffer)

    return on_chunk, finish


# ---------------------------------------------------------------------------
# Module-level console — the single source of truth for terminal I/O
# ---------------------------------------------------------------------------

console = Console()
