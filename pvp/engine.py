"""Analysis engine — CP math, stat-product math, rank lookup, moveset lookup.

All public functions take explicit data (CPM table, base stats, ranking list)
so the engine is easy to unit-test without booting the whole CLI. The thin
convenience helpers at the bottom of the module load the relevant data via
:mod:`pvp.data` for production use.

## Formulas used

CP is Niantic's canonical formula:

    CP = max(10, floor(A * sqrt(D) * sqrt(S) * CPM^2 / 10))

where A/D/S are ``(base + IV)`` and CPM is the multiplier for the level.

Stat product (PvP convention, matches PvPoke) is:

    A_eff = (base_atk + atk_iv) * CPM
    D_eff = (base_def + def_iv) * CPM
    HP    = floor((base_sta + sta_iv) * CPM)
    product = A_eff * D_eff * HP

HP is floored because the in-game HP bar is integer-valued — reflecting this
matches PvPoke's rankings exactly and is why the two sources agree.

Half-level CPMs are computed as the geometric mean of the two surrounding
whole-level CPMs::

    CPM(L + 0.5) = sqrt((CPM(L)^2 + CPM(L+1)^2) / 2)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import data


# ---------------------------------------------------------------------------
# League caps
# ---------------------------------------------------------------------------

LEAGUE_CP_CAPS: dict[str, int] = {
    "gl": 1500,
    "ul": 2500,
    "ml": 10_000,  # effectively uncapped; matches PvPoke's convention
}


_LEAGUE_CAP_ALIASES: dict[str, int] = {
    "gl": 1500, "great": 1500, "great-league": 1500,
    "ul": 2500, "ultra": 2500, "ultra-league": 2500,
    "ml": 10_000, "master": 10_000, "master-league": 10_000,
}


def league_cap(league: str) -> int:
    key = league.strip().lower()
    cap = _LEAGUE_CAP_ALIASES.get(key)
    if cap is not None:
        return cap
    raise ValueError(f"Unknown league {league!r}")


# ---------------------------------------------------------------------------
# CP multiplier table
# ---------------------------------------------------------------------------


def cp_multiplier(level: float, cpm_table: Sequence[float]) -> float:
    """Return the CPM for ``level`` (1–51 in 0.5 steps; 51 = Best Buddy cap).

    The table is expected to hold one entry per whole level. Half-levels are
    interpolated with the geometric-mean formula used by Pokemon GO.
    """
    if level < 1 or level > len(cpm_table):
        raise ValueError(
            f"Level {level} is outside the supported range 1..{len(cpm_table)}"
        )
    # Whole levels land exactly.
    whole = int(level)
    if abs(level - whole) < 1e-9:
        return cpm_table[whole - 1]
    # Half level between whole-level entries.
    if abs(level - (whole + 0.5)) < 1e-9:
        if whole >= len(cpm_table):
            raise ValueError(
                f"Cannot compute CPM for level {level}: no next whole-level value."
            )
        cpm_low = cpm_table[whole - 1]
        cpm_high = cpm_table[whole]
        return math.sqrt((cpm_low ** 2 + cpm_high ** 2) / 2.0)
    raise ValueError(
        f"Level {level} is not a supported 0.5 increment"
    )


def extract_cpm_table(game_master: dict[str, Any]) -> list[float]:
    """Find the ``cpMultiplier`` array inside a game-master snapshot."""
    for template in data.iter_game_master_entries(game_master):
        body = template.get("playerLevel") or (template.get("data") or {}).get("playerLevel")
        if body and isinstance(body.get("cpMultiplier"), list):
            cpms = [float(x) for x in body["cpMultiplier"]]
            if cpms:
                return cpms
    raise KeyError("Game master contains no PLAYER_LEVEL_SETTINGS.cpMultiplier array")


# ---------------------------------------------------------------------------
# Base stats and species lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseStats:
    species_id: str
    name: str
    types: tuple[str, ...]
    attack: int
    defense: int
    stamina: int


def _norm_type(raw: str) -> str:
    """Reduce 'POKEMON_TYPE_FIGHTING' to 'fighting'."""
    return raw.replace("POKEMON_TYPE_", "").lower()


def _slug(name: str) -> str:
    """Normalize a species name for flexible matching.

    Converts 'Galarian Stunfisk' → 'galarian stunfisk' (spaces preserved) so
    callers can further fold on it. Use :func:`_species_keys` for lookup.
    """
    return re.sub(r"[\s_\-]+", " ", name.strip().lower())


def _species_keys(name: str) -> set[str]:
    """Possible ways the name might appear in PvPoke or the game master."""
    base = _slug(name)
    underscore = base.replace(" ", "_")
    compact = base.replace(" ", "")
    parts = base.split()
    keys = {base, underscore, compact}
    if len(parts) >= 2:
        # "galarian stunfisk" ↔ "stunfisk galarian" (PvPoke uses form-suffix).
        # Only one rotation is generated; three-word names (e.g. "shadow alolan X")
        # would need additional rotations if they appear in future meta data.
        swapped = " ".join(parts[-1:] + parts[:-1])
        keys.add(swapped)
        keys.add(swapped.replace(" ", "_"))
    return keys


def extract_base_stats(game_master: dict[str, Any]) -> dict[str, BaseStats]:
    """Build a {species_slug: BaseStats} map from a game-master snapshot."""
    out: dict[str, BaseStats] = {}
    for template in data.iter_game_master_entries(game_master):
        settings = template.get("pokemonSettings") or (template.get("data") or {}).get(
            "pokemonSettings"
        )
        if not settings:
            continue
        stats = settings.get("stats") or {}
        try:
            atk = int(stats["baseAttack"])
            df = int(stats["baseDefense"])
            sta = int(stats["baseStamina"])
        except (KeyError, TypeError, ValueError):
            continue
        pid = str(settings.get("pokemonId", "")).strip()
        if not pid:
            continue
        types = [_norm_type(str(settings["type"]))] if settings.get("type") else []
        if settings.get("type2"):
            types.append(_norm_type(str(settings["type2"])))
        bs = BaseStats(
            species_id=pid,
            name=pid,
            types=tuple(types),
            attack=atk,
            defense=df,
            stamina=sta,
        )
        # Index by every key we might look up with.
        for key in _species_keys(pid):
            out[key] = bs
    return out


class SpeciesNotFound(KeyError):
    """Raised when a species is not present in the game master or rankings."""


def find_base_stats(species: str, base_stats_map: dict[str, BaseStats]) -> BaseStats:
    for key in _species_keys(species):
        if key in base_stats_map:
            return base_stats_map[key]
    raise SpeciesNotFound(species)


# ---------------------------------------------------------------------------
# CP and stat product
# ---------------------------------------------------------------------------


def compute_cp(
    base_attack: int,
    base_defense: int,
    base_stamina: int,
    attack_iv: int,
    defense_iv: int,
    stamina_iv: int,
    level: float,
    cpm_table: Sequence[float],
) -> int:
    _validate_ivs(attack_iv, defense_iv, stamina_iv)
    cpm = cp_multiplier(level, cpm_table)
    atk = base_attack + attack_iv
    df = base_defense + defense_iv
    sta = base_stamina + stamina_iv
    raw = atk * math.sqrt(df) * math.sqrt(sta) * (cpm ** 2) / 10.0
    return max(10, math.floor(raw))


def compute_stat_product(
    base_attack: int,
    base_defense: int,
    base_stamina: int,
    attack_iv: int,
    defense_iv: int,
    stamina_iv: int,
    level: float,
    cpm_table: Sequence[float],
) -> float:
    _validate_ivs(attack_iv, defense_iv, stamina_iv)
    cpm = cp_multiplier(level, cpm_table)
    atk_eff = (base_attack + attack_iv) * cpm
    def_eff = (base_defense + defense_iv) * cpm
    hp = math.floor((base_stamina + stamina_iv) * cpm)
    return atk_eff * def_eff * hp


def _validate_ivs(atk: int, df: int, sta: int) -> None:
    for label, value in (("attack", atk), ("defense", df), ("stamina", sta)):
        if not 0 <= value <= 15:
            raise ValueError(f"{label} IV {value} outside the 0–15 range")


# ---------------------------------------------------------------------------
# Optimal level and IVs under a league cap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptimalBuild:
    level: float
    cp: int
    stat_product: float


def max_level_under_cap(
    base: BaseStats,
    attack_iv: int,
    defense_iv: int,
    stamina_iv: int,
    *,
    cpm_table: Sequence[float],
    cp_cap: int,
) -> OptimalBuild:
    """Find the highest level (in 0.5 steps) whose CP stays at or below the cap.

    If no level in range 1..51 keeps the Pokemon under the cap (i.e. CP at
    level 1 already exceeds it), returns the level-1 build.
    """
    best: OptimalBuild | None = None
    max_whole = len(cpm_table)
    # Generate levels 1, 1.5, 2, ..., max_whole (whole levels) and all half-levels
    # between them. With a 51-entry table this covers 1.0 through 51.0, including
    # the level-50.5 buddy-bonus build.
    for step in range(2 * max_whole - 1):
        level = 1 + step * 0.5
        try:
            cp = compute_cp(
                base.attack, base.defense, base.stamina,
                attack_iv, defense_iv, stamina_iv,
                level, cpm_table,
            )
        except ValueError:
            continue
        if cp > cp_cap:
            # CP is monotonically increasing with level for all real PoGo CPM tables,
            # so the first overage means no higher level can be under the cap.
            break
        sp = compute_stat_product(
            base.attack, base.defense, base.stamina,
            attack_iv, defense_iv, stamina_iv,
            level, cpm_table,
        )
        build = OptimalBuild(level=level, cp=cp, stat_product=sp)
        if best is None or build.stat_product > best.stat_product:
            best = build
    if best is None:
        # Even L1 is over the cap — return that anyway so the caller has data.
        cp = compute_cp(
            base.attack, base.defense, base.stamina,
            attack_iv, defense_iv, stamina_iv,
            1.0, cpm_table,
        )
        sp = compute_stat_product(
            base.attack, base.defense, base.stamina,
            attack_iv, defense_iv, stamina_iv,
            1.0, cpm_table,
        )
        return OptimalBuild(level=1.0, cp=cp, stat_product=sp)
    return best


@dataclass(frozen=True)
class RankedBuild:
    """Best build for a species in a given league — the '#1 IV combo'."""

    attack_iv: int
    defense_iv: int
    stamina_iv: int
    level: float
    cp: int
    stat_product: float


def find_optimal_ivs(
    base: BaseStats,
    *,
    cpm_table: Sequence[float],
    cp_cap: int,
) -> RankedBuild:
    """Search all 4096 IV combinations for the highest stat-product build."""
    best: RankedBuild | None = None
    for atk in range(16):
        for df in range(16):
            for sta in range(16):
                b = max_level_under_cap(
                    base, atk, df, sta,
                    cpm_table=cpm_table, cp_cap=cp_cap,
                )
                if best is None or b.stat_product > best.stat_product:
                    best = RankedBuild(
                        attack_iv=atk,
                        defense_iv=df,
                        stamina_iv=sta,
                        level=b.level,
                        cp=b.cp,
                        stat_product=b.stat_product,
                    )
    assert best is not None
    return best


# ---------------------------------------------------------------------------
# PvPoke rank lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankResult:
    species_id: str
    species_name: str
    rank: int              # 1-based index in the ranking list
    total: int             # total entries in the list
    percentile: float      # approaches 100.0 for top ranks; rank 1 of N = 100*(1-1/N)
    score: float | None    # PvPoke "score" / "rating", if present
    stat_product: float | None  # PvPoke reported stat product, if present


def _entry_matches(entry: dict[str, Any], keys: set[str]) -> bool:
    for field in ("speciesId", "speciesName", "species_id", "species_name"):
        value = entry.get(field)
        if not isinstance(value, str):
            continue
        if _slug(value) in keys or value.lower() in keys or value.lower().replace("_", " ") in keys:
            return True
    return False


def lookup_rank(
    species: str,
    rankings: list[dict[str, Any]],
) -> RankResult:
    """Find ``species`` in a PvPoke rankings list."""
    keys = _species_keys(species)
    total = len(rankings)
    for index, entry in enumerate(rankings):
        if _entry_matches(entry, keys):
            rank = index + 1
            percentile = 100.0 * (1 - ((index + 1) / total)) if total else 0.0
            score = entry["score"] if "score" in entry else entry.get("rating")
            product = (entry.get("stats") or {}).get("product")
            return RankResult(
                species_id=str(entry.get("speciesId", "")),
                species_name=str(entry.get("speciesName", species)),
                rank=rank,
                total=total,
                percentile=percentile,
                score=float(score) if isinstance(score, (int, float)) else None,
                stat_product=float(product) if isinstance(product, (int, float)) else None,
            )
    raise SpeciesNotFound(species)


def get_optimal_moveset(
    species: str,
    rankings: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Return ``(fast_move, charge_moves)`` for the species.

    ``charge_moves`` contains 0–2 entries depending on the data available;
    callers must not assume the list always has exactly two elements.
    Falls back to the ``moveset`` array if the structured ``moves`` object is
    not present in the rankings entry.
    """
    keys = _species_keys(species)
    for entry in rankings:
        if _entry_matches(entry, keys):
            moves = entry.get("moves") or {}
            fast = (moves.get("fastMoves") or [])
            charged = (moves.get("chargedMoves") or [])
            fast_move: str | None = None
            if fast and isinstance(fast[0], dict):
                raw_id = fast[0].get("moveId")
                if raw_id is not None:
                    fast_move = str(raw_id)
            elif entry.get("moveset"):
                raw_id = entry["moveset"][0]
                if raw_id is not None:
                    fast_move = str(raw_id)
            charge_moves: list[str] = []
            if charged:
                for item in charged[:2]:
                    if isinstance(item, dict) and item.get("moveId"):
                        charge_moves.append(str(item["moveId"]))
                    elif isinstance(item, str):
                        charge_moves.append(item)
            elif entry.get("moveset") and len(entry["moveset"]) >= 3:
                charge_moves = [str(entry["moveset"][1]), str(entry["moveset"][2])]
            if fast_move is None:
                raise SpeciesNotFound(f"No moveset data for {species}")
            return fast_move, charge_moves
    raise SpeciesNotFound(species)


# ---------------------------------------------------------------------------
# Type chart
# ---------------------------------------------------------------------------


# PokeMiners game master encodes attackScalar as a list indexed by this order.
_POGO_TYPE_ORDER: tuple[str, ...] = (
    "normal", "fighting", "flying", "poison", "ground", "rock",
    "bug", "ghost", "steel", "fire", "water", "grass",
    "electric", "psychic", "ice", "dragon", "dark", "fairy",
)


def extract_type_effectiveness(game_master: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Return ``{attacker_type: {defender_type: multiplier}}`` where known."""
    chart: dict[str, dict[str, float]] = {}
    for template in data.iter_game_master_entries(game_master):
        body = template.get("typeEffective") or (template.get("data") or {}).get("typeEffective")
        if not body:
            continue
        attacker = _norm_type(str(body.get("attackType", "")))
        if not attacker:
            continue
        scalar = body.get("attackScalar") or {}
        if isinstance(scalar, dict):
            chart[attacker] = {
                _norm_type(str(k)): float(v)
                for k, v in scalar.items()
                if isinstance(v, (int, float))
            }
        elif isinstance(scalar, list) and len(scalar) == len(_POGO_TYPE_ORDER):
            chart[attacker] = {
                _POGO_TYPE_ORDER[i]: float(v)
                for i, v in enumerate(scalar)
                if isinstance(v, (int, float))
            }
    return chart


# ---------------------------------------------------------------------------
# Move index
# ---------------------------------------------------------------------------


def extract_move_types(game_master: dict[str, Any]) -> dict[str, str]:
    """Return ``{move_id: type}`` for every combat move in the game master.

    The same move is indexed under both its canonical id (``COUNTER_FAST``) and
    its PvPoke-style bare id (``COUNTER``) so lookups from either source hit.
    """
    out: dict[str, str] = {}
    for template in data.iter_game_master_entries(game_master):
        combat = template.get("combatMove") or (template.get("data") or {}).get("combatMove")
        if not combat:
            continue
        mid = combat.get("uniqueId")
        mtype = combat.get("type")
        if not mid or not mtype:
            continue
        canonical = str(mid).upper()
        norm = _norm_type(str(mtype))
        out[canonical] = norm
        if canonical.endswith("_FAST"):
            out[canonical[:-5]] = norm
    return out


def super_effective_targets(
    move_types: Iterable[str],
    type_chart: dict[str, dict[str, float]],
) -> set[str]:
    """Return the set of defender types any of ``move_types`` is > 1.0× against."""
    targets: set[str] = set()
    for attacker in move_types:
        scalar = type_chart.get(attacker.lower(), {})
        for defender, mult in scalar.items():
            if mult > 1.0:
                targets.add(defender.lower())
    return targets


# ---------------------------------------------------------------------------
# Team builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamMember:
    """One candidate or selected entry in a team."""

    entry: Any  # data.RosterEntry — Any avoids circular typing
    rank: int | None
    types: tuple[str, ...]
    fast_move: str | None
    charge_moves: tuple[str, ...]
    covers: tuple[str, ...]  # types this member contributes toward the cover list


@dataclass(frozen=True)
class TeamResult:
    """Output of :func:`build_team`."""

    members: tuple[TeamMember, ...]
    missing_coverage: tuple[str, ...]
    note: str | None = None
    league: str = ""


def _candidate_from_entry(
    entry: Any,
    *,
    rankings: list[dict[str, Any]],
    bases: dict[str, BaseStats],
    type_chart: dict[str, dict[str, float]],
    move_types: dict[str, str],
    cover_types: Sequence[str],
) -> TeamMember | None:
    """Build a team-member candidate for one roster entry, or ``None`` if the
    species has no base-stats entry at all (we don't know its types).
    """
    try:
        base = find_base_stats(entry.species, bases)
    except SpeciesNotFound:
        return None

    rank: int | None
    fast_move: str | None
    charge_moves: tuple[str, ...]
    try:
        rank_result = lookup_rank(entry.species, rankings)
        rank = rank_result.rank
    except SpeciesNotFound:
        rank = None
    try:
        f, c = get_optimal_moveset(entry.species, rankings)
        fast_move, charge_moves = f, tuple(c)
    except SpeciesNotFound:
        fast_move = entry.fast_move
        charge_moves = tuple(
            m for m in (entry.charge_move_1, entry.charge_move_2) if m
        )

    # Which types does this member cover?
    move_ids: list[str] = []
    if fast_move:
        move_ids.append(fast_move)
    move_ids.extend(charge_moves)
    attack_types = {
        move_types.get(m.upper()) for m in move_ids if move_types.get(m.upper())
    }
    # The member's own types also count as "coverage" per the spec.
    attack_types.update(base.types)
    # What does that pool actually threaten?
    threatened = super_effective_targets(
        (t for t in attack_types if t),
        type_chart,
    )
    # "Own typing as coverage": having fairy type counts as fairy coverage.
    threatened.update(base.types)

    if cover_types:
        covers = tuple(t for t in cover_types if t in threatened)
    else:
        # No cover list — record own types as the member's public "strength".
        covers = tuple(base.types)

    return TeamMember(
        entry=entry,
        rank=rank,
        types=base.types,
        fast_move=fast_move,
        charge_moves=charge_moves,
        covers=covers,
    )


def _select_team(
    candidates: list[TeamMember],
    cover_types: Sequence[str],
    target_size: int,
) -> list[TeamMember]:
    """Greedy selection: at each step pick the candidate that adds the most
    still-missing coverage, breaking ties by rank and then by primary-type
    diversity.

    Complexity: O(k·n) where k = ``target_size`` and n = candidate count. For
    a typical roster (<100 entries) this is a no-op; an exhaustive 3-of-n
    search would be ≤ 160k combinations on a 100-entry roster, also fast, but
    greedy's output is stable and explains itself well — the team builder is
    meant to be read, not just executed.
    """
    selected: list[TeamMember] = []
    remaining = list(candidates)
    covered: set[str] = set()
    required = {t for t in cover_types}

    def _sort_key(m: TeamMember) -> tuple:
        new_coverage = len(set(m.covers) & (required - covered)) if required else 0
        primary = m.types[0] if m.types else ""
        collision = 1 if any(s.types and s.types[0] == primary for s in selected) else 0
        rank_key = m.rank if m.rank is not None else 10**9
        # Lower tuple sorts first: more new coverage, no collision, then better rank.
        return (-new_coverage, collision, rank_key)

    while remaining and len(selected) < target_size:
        # Re-sort every iteration: _sort_key closes over `covered` and `selected`,
        # both of which change after each pick, so the order is not stable across
        # iterations. pop(0) is O(N) on a list but roster sizes are small in practice.
        remaining.sort(key=_sort_key)
        chosen = remaining.pop(0)
        selected.append(chosen)
        covered.update(chosen.covers)

    return selected


def build_team(
    league: str,
    cover: Sequence[str] | None = None,
    *,
    rankings: list[dict[str, Any]] | None = None,
    bases: dict[str, BaseStats] | None = None,
    type_chart: dict[str, dict[str, float]] | None = None,
    move_types: dict[str, str] | None = None,
    roster_entries: Sequence[Any] | None = None,
    target_size: int = 3,
) -> TeamResult:
    """Suggest the best ``target_size``-member team from the user's roster.

    Every data argument is optional — when omitted, the function loads from
    the cache and the database. Passing them in explicitly keeps the function
    testable without I/O.

    Edge cases, by design:
        * roster has fewer than ``target_size`` entries → return what we have
          with a note explaining the shortfall;
        * the roster is empty → return an empty team;
        * ``cover`` includes types nobody's moves can hit → the team is still
          built with the best available and the missed types are listed in
          ``missing_coverage``.
    """
    # --- data loading ------------------------------------------------------
    if rankings is None:
        rankings = data.load_pvpoke_rankings(league)
    if bases is None or type_chart is None or move_types is None:
        gm = data.load_game_master()
        if bases is None:
            bases = extract_base_stats(gm)
        if type_chart is None:
            type_chart = extract_type_effectiveness(gm)
        if move_types is None:
            move_types = extract_move_types(gm)
    if roster_entries is None:
        roster_entries = data.get_all_entries()

    cover_types = [c.strip().lower() for c in (cover or []) if c and c.strip()]

    # --- candidate construction -------------------------------------------
    candidates: list[TeamMember] = []
    for entry in roster_entries:
        member = _candidate_from_entry(
            entry,
            rankings=rankings,
            bases=bases,
            type_chart=type_chart,
            move_types=move_types,
            cover_types=cover_types,
        )
        if member is not None:
            candidates.append(member)

    # Stable order: by rank ascending, None last.
    candidates.sort(key=lambda m: (m.rank if m.rank is not None else 10**9))

    selected = _select_team(candidates, cover_types, target_size)

    # --- coverage summary -------------------------------------------------
    covered_total: set[str] = set()
    for m in selected:
        covered_total.update(m.covers)
    missing = tuple(t for t in cover_types if t not in covered_total)

    notes: list[str] = []
    if len(selected) < target_size:
        notes.append(
            f"Only {len(selected)} roster member(s) had enough data to be selected "
            f"(wanted {target_size})."
        )
    if missing:
        notes.append(
            f"No member covers: {', '.join(missing)}. "
            "Consider adding a Pokemon with moves super-effective against those types."
        )
    note = " ".join(notes) or None

    return TeamResult(
        members=tuple(selected),
        missing_coverage=missing,
        note=note,
        league=league.upper(),
    )


# ---------------------------------------------------------------------------
# Convenience — load + delegate
# ---------------------------------------------------------------------------


def load_analysis_context(league: str) -> tuple[
    list[dict[str, Any]],
    dict[str, BaseStats],
    list[float],
]:
    """Load rankings, base stats, and CPM table for an analysis command."""
    rankings = data.load_pvpoke_rankings(league)
    gm = data.load_game_master()
    base_stats = extract_base_stats(gm)
    cpm = extract_cpm_table(gm)
    return rankings, base_stats, cpm
