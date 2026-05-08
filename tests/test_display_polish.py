"""Phase 9 tests — display-layer helpers (roster_table, messaging, streaming).

The centralised display module is the only place Rich is imported at the
top level; these tests exercise the helpers directly so the rest of the
package can be confident about the visual contract.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from pvp import data, display


# ---------------------------------------------------------------------------
# roster_table
# ---------------------------------------------------------------------------


def test_roster_table_renders_all_columns() -> None:
    entries = [
        data.RosterEntry(
            id=1, species="Medicham", nickname="Zen",
            attack_iv=1, defense_iv=15, stamina_iv=14, level=50.0,
            fast_move="COUNTER", charge_move_1="ICE_PUNCH", charge_move_2="DYNAMIC_PUNCH",
        ),
        data.RosterEntry(
            id=2, species="Azumarill",
            attack_iv=0, defense_iv=15, stamina_iv=15, level=40.0,
            fast_move=None, charge_move_1=None, charge_move_2=None,
        ),
    ]
    table = display.roster_table(entries)
    buffer = io.StringIO()
    console = Console(file=buffer, width=120, force_terminal=False)
    console.print(table)
    output = buffer.getvalue()
    assert "Medicham" in output
    assert "Zen" in output
    assert "Azumarill" in output
    assert "1/15/14" in output
    assert "COUNTER" in output
    # Unknown moves render as em-dash.
    assert "—" in output


def test_roster_table_title_uses_count() -> None:
    table = display.roster_table([])
    # The title uses the entry count; empty → "(0 entries)".
    assert "0 entries" in (table.title or "")


# ---------------------------------------------------------------------------
# Messaging helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def captured() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=120, force_terminal=False), buffer


def test_print_error_prefix(captured) -> None:
    console, buffer = captured
    display.print_error(console, "cache missing")
    assert "Error:" in buffer.getvalue()
    assert "cache missing" in buffer.getvalue()


def test_print_error_with_hint(captured) -> None:
    console, buffer = captured
    display.print_error(console, "cache missing", hint="run `pvp sync`")
    output = buffer.getvalue()
    assert "cache missing" in output
    assert "run `pvp sync`" in output
    # The hint is on its own line prefixed with an arrow-ish glyph.
    assert "→" in output or "->" in output


def test_print_warning_and_success_and_info(captured) -> None:
    console, buffer = captured
    display.print_warning(console, "skipped 1 row")
    display.print_success(console, "imported 5 / 6 entries")
    display.print_info(console, "cache timestamps:")
    output = buffer.getvalue()
    assert "skipped 1 row" in output
    assert "imported 5 / 6 entries" in output
    assert "cache timestamps" in output


# ---------------------------------------------------------------------------
# make_sync_progress
# ---------------------------------------------------------------------------


def test_sync_progress_callback_renders_all_stages(captured) -> None:
    console, buffer = captured
    progress = display.make_sync_progress(console)
    progress("start", "rankings (GL)")
    progress("ok", "rankings (GL)")
    progress("fail", "game master")
    # Unknown stage should be a silent no-op.
    progress("unknown", "x")
    output = buffer.getvalue()
    assert "fetching rankings (GL)" in output
    assert "rankings (GL)" in output
    assert "game master" in output
    # We keep the glyphs visible to the eye (and tests — if someone breaks
    # them, we catch it).
    assert "•" in output
    assert "✓" in output
    assert "✗" in output


# ---------------------------------------------------------------------------
# make_live_renderer
# ---------------------------------------------------------------------------


def test_live_renderer_accumulates_and_finishes(captured) -> None:
    console, _ = captured
    on_chunk, finish = display.make_live_renderer(console)
    on_chunk("Hello ")
    on_chunk("world")
    total = finish()
    assert total == "Hello world"


def test_live_renderer_finish_is_idempotent(captured) -> None:
    """``finish`` is called twice on the error path (once in the except
    branch, once unconditionally). It must not raise on the second call.
    """
    console, _ = captured
    on_chunk, finish = display.make_live_renderer(console)
    on_chunk("partial")
    assert finish() == "partial"
    # Second call should be a no-op, not a crash.
    assert finish() == "partial"


# ---------------------------------------------------------------------------
# No stray Rich imports outside display.py
# ---------------------------------------------------------------------------


def test_display_is_sole_rich_importer() -> None:
    """If a future change reintroduces `from rich.* import …` in cli/ai/etc,
    this test fails loudly so we don't lose the single-source-of-truth
    property.
    """
    import pathlib
    pvp_dir = pathlib.Path(__file__).resolve().parent.parent / "pvp"
    offenders: list[str] = []
    for path in pvp_dir.glob("*.py"):
        if path.name == "display.py":
            continue
        text = path.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("from rich") or stripped.startswith("import rich"):
                # Allow nested (function-local) imports — they are lazy and
                # only the CLI layer can reach them.
                if path.name != "display.py" and not line.startswith(" "):
                    offenders.append(f"{path.name}: {stripped}")
    assert not offenders, "Unexpected Rich imports: " + ", ".join(offenders)
