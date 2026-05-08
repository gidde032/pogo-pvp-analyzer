"""Roster module — import/add/list/delete for the user's Pokemon roster.

Exposes a small API used by the CLI. Business rules (IV ranges, level bounds,
species required) are enforced by :class:`RosterEntryModel`, a Pydantic model,
so the same validation applies whether the entry came from a CSV file, a JSON
file, or the interactive prompt.

None of the functions here emit to stdout — callers own display. This makes
them easy to reuse from the AI layer and the team builder.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import data


# ---------------------------------------------------------------------------
# Validation model
# ---------------------------------------------------------------------------


class RosterEntryModel(BaseModel):
    """Validated shape for a single roster record coming from user input."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    species: str = Field(min_length=1)
    pokedex_number: int | None = None

    attack_iv: int = Field(ge=0, le=15)
    defense_iv: int = Field(ge=0, le=15)
    stamina_iv: int = Field(ge=0, le=15)
    level: float = Field(ge=1.0, le=51.0)

    fast_move: str | None = None
    charge_move_1: str | None = None
    charge_move_2: str | None = None

    nickname: str | None = None
    notes: str | None = None

    @field_validator("level")
    @classmethod
    def _level_half_step(cls, v: float) -> float:
        """Levels must be whole or half values."""
        doubled = v * 2
        if abs(doubled - round(doubled)) > 1e-6:
            raise ValueError("level must be in 0.5 increments")
        return v

    def to_entry(self) -> data.RosterEntry:
        return data.RosterEntry(
            species=self.species,
            pokedex_number=self.pokedex_number,
            attack_iv=self.attack_iv,
            defense_iv=self.defense_iv,
            stamina_iv=self.stamina_iv,
            level=self.level,
            fast_move=self.fast_move,
            charge_move_1=self.charge_move_1,
            charge_move_2=self.charge_move_2,
            nickname=self.nickname,
            notes=self.notes,
        )


# ---------------------------------------------------------------------------
# Import result / reporting
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    """Summary returned from :func:`import_from_file`."""

    imported: list[data.RosterEntry]
    errors: list[tuple[int, str]]  # (row_number, message)

    @property
    def total_rows(self) -> int:
        return len(self.imported) + len(self.errors)


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


REQUIRED_CSV_COLUMNS = {"species", "attack_iv", "defense_iv", "stamina_iv", "level"}


def _coerce(value: Any) -> Any:
    """Normalize CSV-originated strings to the types Pydantic expects."""
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed == "":
            return None
        return trimmed
    return value


def _parse_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV {path} has no header row.")
        missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"CSV {path} is missing required columns: {sorted(missing)}. "
                f"Need at least: {sorted(REQUIRED_CSV_COLUMNS)}."
            )
        rows: list[dict[str, Any]] = []
        for row in reader:
            rows.append({k: _coerce(v) for k, v in row.items()})
        return rows


def _parse_json(path: Path) -> list[Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        for key in ("roster", "entries"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError(
            f"JSON {path} must be a list (or an object with a 'roster' list). "
            f"Got {type(payload).__name__}."
        )
    return list(payload)


def _parse_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _parse_csv(path)
    if suffix == ".json":
        return _parse_json(path)
    raise ValueError(f"Unsupported file type {suffix!r}. Expected .csv or .json.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_from_file(path: str | Path) -> ImportReport:
    """Validate a roster file and insert the valid rows into the database.

    The loader is deliberately lenient about the file as a whole and strict
    about each row: if one row in a hundred fails validation, the other 99
    still import and the failing row appears in :attr:`ImportReport.errors`
    with a human-readable message. This matches the spec's NFR-3 ("the
    roster file format must be human-readable so users can edit it
    directly") — a single typo should not abort the whole import.
    """
    path = Path(path)
    rows = _parse_file(path)
    validated: list[data.RosterEntry] = []
    errors: list[tuple[int, str]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append((index, f"expected a JSON object, got {type(row).__name__}"))
            continue
        try:
            model = RosterEntryModel.model_validate(row)
        except Exception as exc:  # pydantic.ValidationError or ValueError
            errors.append((index, format_error(exc)))
            continue
        validated.append(model.to_entry())

    inserted: list[data.RosterEntry] = (
        data.add_entries(validated) if validated else []
    )
    return ImportReport(imported=inserted, errors=errors)


def format_error(exc: Exception) -> str:
    """Pydantic errors are verbose — condense to a single readable line."""
    try:
        from pydantic import ValidationError  # local import to avoid cost elsewhere
    except Exception:  # pragma: no cover
        return str(exc)
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "invalid")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts) or str(exc)
    return str(exc)


def add_entry_from_dict(payload: dict[str, Any]) -> data.RosterEntry:
    """Validate a single dict (e.g. from the interactive CLI) and insert it."""
    model = RosterEntryModel.model_validate(payload)
    return data.add_entry(model.to_entry())


def list_all() -> list[data.RosterEntry]:
    return data.get_all_entries()


def delete_by_id(entry_id: int) -> bool:
    return data.delete_entry(entry_id)


def resolve_by_identifier(
    identifier: str,
) -> tuple[list[data.RosterEntry], data.RosterEntry | None]:
    """Return (species-matches, id-match).

    Callers can decide whether to prompt the user to disambiguate when both
    an id match and a species match are present.
    """
    species_matches = data.get_entries_by_species(identifier)
    id_match: data.RosterEntry | None = None
    if identifier.isdigit():
        id_match = data.get_entry_by_id(int(identifier))
    return species_matches, id_match


def delete_by_identifier(identifier: str) -> list[data.RosterEntry]:
    """Delete every entry matching ``identifier`` (either id or species name).

    Returns the list of deleted entries (empty if nothing matched). Species
    matches are deleted as a group; an id match supersedes any species match
    with the same string (digits).
    """
    species_matches, id_match = resolve_by_identifier(identifier)
    to_delete: list[data.RosterEntry] = []
    if id_match is not None:
        to_delete = [id_match]
    elif species_matches:
        to_delete = species_matches
    for entry in to_delete:
        data.delete_entry(entry.id)
    return to_delete


def iter_roster_dicts() -> Iterable[dict[str, Any]]:
    """Lightweight iteration helper used by the AI layer."""
    for entry in list_all():
        yield entry.to_dict()
