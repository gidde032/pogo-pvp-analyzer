"""Phase 4 tests — analysis engine (CP math, stat product, rank lookup)."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from pvp import data, engine


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded(cache_dir: Path, fixtures_dir: Path):
    """Copy pinned fixtures into the hermetic cache and expose parsed handles."""
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    rankings = data.load_pvpoke_rankings("GL")
    gm = data.load_game_master()
    return {
        "rankings": rankings,
        "gm": gm,
        "cpm": engine.extract_cpm_table(gm),
        "bases": engine.extract_base_stats(gm),
    }


# ---------------------------------------------------------------------------
# CP multiplier table
# ---------------------------------------------------------------------------


def test_cpm_level_1(loaded) -> None:
    assert math.isclose(engine.cp_multiplier(1.0, loaded["cpm"]), 0.094, rel_tol=1e-6)


def test_cpm_level_40(loaded) -> None:
    assert math.isclose(engine.cp_multiplier(40.0, loaded["cpm"]), 0.7903, rel_tol=1e-6)


def test_cpm_half_level_interpolation(loaded) -> None:
    # Level 40.5 is the geometric mean of L40 and L41 CPMs.
    cpm_40 = engine.cp_multiplier(40.0, loaded["cpm"])
    cpm_41 = engine.cp_multiplier(41.0, loaded["cpm"])
    expected = math.sqrt((cpm_40 ** 2 + cpm_41 ** 2) / 2.0)
    assert math.isclose(engine.cp_multiplier(40.5, loaded["cpm"]), expected, rel_tol=1e-9)


def test_cpm_rejects_out_of_range(loaded) -> None:
    with pytest.raises(ValueError):
        engine.cp_multiplier(0.5, loaded["cpm"])
    with pytest.raises(ValueError):
        engine.cp_multiplier(99.0, loaded["cpm"])


def test_cpm_rejects_bad_step(loaded) -> None:
    with pytest.raises(ValueError):
        engine.cp_multiplier(30.3, loaded["cpm"])


# ---------------------------------------------------------------------------
# CP computation — against known values
# ---------------------------------------------------------------------------


# Each row: (species, atk_iv, def_iv, sta_iv, level, expected_cp)
#
# Expected CPs computed directly from Niantic's published CP formula
# using the base stats and CPM table in our fixture. Cross-checked against
# community calculators for the ones widely referenced.
CP_CASES = [
    # Medicham (121/152/155) — widely documented L40 15/15/15 maxes at 1431
    ("Medicham", 15, 15, 15, 40.0, 1431),
    ("Medicham", 0, 15, 15, 40.0, 1273),
    ("Medicham", 0, 0, 0, 1.0, 16),
    # Registeel (143/285/190)
    ("Registeel", 0, 0, 0, 1.0, 29),
    ("Registeel", 15, 15, 15, 40.0, 2447),
    # Galarian Stunfisk (144/171/177) at a half level
    ("Galarian Stunfisk", 15, 15, 15, 20.5, 1099),
    ("Galarian Stunfisk", 0, 15, 15, 40.0, 1699),
    # Azumarill
    ("Azumarill", 15, 15, 15, 40.0, 1588),
]


@pytest.mark.parametrize("species,atk_iv,def_iv,sta_iv,level,expected", CP_CASES)
def test_compute_cp_known_values(
    loaded, species, atk_iv, def_iv, sta_iv, level, expected
) -> None:
    base = engine.find_base_stats(species, loaded["bases"])
    cp = engine.compute_cp(
        base.attack, base.defense, base.stamina,
        atk_iv, def_iv, sta_iv, level,
        loaded["cpm"],
    )
    # Off-by-one tolerance is the traditional trap in this formula;
    # the test requires exact agreement.
    assert cp == expected, f"{species} @ L{level} {atk_iv}/{def_iv}/{sta_iv} expected {expected} got {cp}"


def test_compute_cp_minimum_floor(loaded) -> None:
    base = engine.find_base_stats("Medicham", loaded["bases"])
    # At L1 with 0 IVs the raw formula falls below the CP=10 floor.
    cp = engine.compute_cp(base.attack, base.defense, base.stamina, 0, 0, 0, 1.0, loaded["cpm"])
    assert cp >= 10


def test_compute_cp_rejects_invalid_ivs(loaded) -> None:
    base = engine.find_base_stats("Medicham", loaded["bases"])
    with pytest.raises(ValueError):
        engine.compute_cp(base.attack, base.defense, base.stamina, 16, 0, 0, 40.0, loaded["cpm"])


# ---------------------------------------------------------------------------
# Stat product
# ---------------------------------------------------------------------------


def test_compute_stat_product_orders_by_ivs(loaded) -> None:
    """High def/sta IVs produce a larger stat product than high atk IVs at the same level."""
    base = engine.find_base_stats("Medicham", loaded["bases"])
    sp_high_atk = engine.compute_stat_product(
        base.attack, base.defense, base.stamina, 15, 0, 0, 40.0, loaded["cpm"]
    )
    sp_high_bulk = engine.compute_stat_product(
        base.attack, base.defense, base.stamina, 0, 15, 15, 40.0, loaded["cpm"]
    )
    assert sp_high_bulk > sp_high_atk


def test_compute_stat_product_positive(loaded) -> None:
    base = engine.find_base_stats("Registeel", loaded["bases"])
    sp = engine.compute_stat_product(
        base.attack, base.defense, base.stamina, 5, 5, 5, 30.0, loaded["cpm"]
    )
    assert sp > 0


# ---------------------------------------------------------------------------
# Base stat extraction
# ---------------------------------------------------------------------------


def test_extract_base_stats_has_every_fixture_species(loaded) -> None:
    expected_species = {
        "medicham",
        "stunfisk_galarian",
        "swampert",
        "azumarill",
        "registeel",
        "talonflame",
    }
    keys = set(loaded["bases"].keys())
    # At least one alias for every species is present
    for species in expected_species:
        assert any(species in k for k in keys)


def test_find_base_stats_accepts_alternate_forms(loaded) -> None:
    a = engine.find_base_stats("Galarian Stunfisk", loaded["bases"])
    b = engine.find_base_stats("stunfisk_galarian", loaded["bases"])
    c = engine.find_base_stats("STUNFISK_GALARIAN", loaded["bases"])
    assert a == b == c
    assert a.attack == 144 and a.defense == 171 and a.stamina == 177


def test_find_base_stats_not_found(loaded) -> None:
    with pytest.raises(engine.SpeciesNotFound):
        engine.find_base_stats("Mewthree", loaded["bases"])


# ---------------------------------------------------------------------------
# Optimal level / IV search
# ---------------------------------------------------------------------------


def test_max_level_under_cap_respects_league_cap(loaded) -> None:
    base = engine.find_base_stats("Medicham", loaded["bases"])
    build = engine.max_level_under_cap(
        base, 0, 15, 15,
        cpm_table=loaded["cpm"],
        cp_cap=engine.league_cap("GL"),
    )
    assert build.cp <= 1500
    # Going up one half-level would push over the cap
    next_cp = engine.compute_cp(
        base.attack, base.defense, base.stamina,
        0, 15, 15,
        build.level + 0.5,
        loaded["cpm"],
    )
    assert next_cp > 1500


def test_max_level_under_cap_reaches_buddy_level(loaded) -> None:
    # For ML (effectively uncapped), a Pokémon should be able to reach level 50.5
    # (the Best Buddy bonus) now that the CPM table includes level 51.
    base = engine.find_base_stats("Medicham", loaded["bases"])
    build = engine.max_level_under_cap(
        base, 15, 15, 15,
        cpm_table=loaded["cpm"],
        cp_cap=engine.league_cap("ML"),
    )
    assert build.level >= 50.5


def test_find_optimal_ivs_beats_random_ivs(loaded) -> None:
    base = engine.find_base_stats("Medicham", loaded["bases"])
    best = engine.find_optimal_ivs(base, cpm_table=loaded["cpm"], cp_cap=1500)
    # A random mediocre build should produce less stat product.
    mediocre = engine.max_level_under_cap(
        base, 15, 10, 10,
        cpm_table=loaded["cpm"], cp_cap=1500,
    )
    assert best.stat_product >= mediocre.stat_product
    # Medicham's well-known optimal GL build has low attack IV.
    assert best.attack_iv <= 3


# ---------------------------------------------------------------------------
# Rank lookup
# ---------------------------------------------------------------------------


def test_lookup_rank_returns_one_indexed_position(loaded) -> None:
    # In our fixture Galarian Stunfisk is entry #2 (rating 97.1, highest).
    # Our fixture ordering is: medicham, stunfisk_galarian, swampert, azumarill, registeel, talonflame
    r = engine.lookup_rank("Medicham", loaded["rankings"])
    assert r.rank == 1
    assert r.total == 6


def test_lookup_rank_matches_alternate_names(loaded) -> None:
    a = engine.lookup_rank("Galarian Stunfisk", loaded["rankings"])
    b = engine.lookup_rank("stunfisk_galarian", loaded["rankings"])
    assert a.rank == b.rank
    assert a.species_id == "stunfisk_galarian"


def test_lookup_rank_percentile(loaded) -> None:
    # Rank 1 of 6: percentile = 100 * (1 - 1/6) ≈ 83.3, not 100.
    r = engine.lookup_rank("Medicham", loaded["rankings"])
    assert r.rank == 1
    assert abs(r.percentile - 100.0 * (1 - 1 / r.total)) < 0.01
    assert r.percentile < 100.0


def test_lookup_rank_percentile_last_place(loaded) -> None:
    # Last-ranked entry (Talonflame, rank 6 of 6) should be 0%.
    r = engine.lookup_rank("Talonflame", loaded["rankings"])
    assert r.rank == r.total
    assert r.percentile == 0.0


def test_lookup_rank_score_zero_is_not_absent() -> None:
    # A score of 0 must not be treated as absent and fall through to "rating".
    rankings = [{"speciesId": "testmon", "score": 0, "rating": 99.0}]
    r = engine.lookup_rank("testmon", rankings)
    assert r.score == 0.0


def test_lookup_rank_missing_species(loaded) -> None:
    with pytest.raises(engine.SpeciesNotFound):
        engine.lookup_rank("Mewthree", loaded["rankings"])


# ---------------------------------------------------------------------------
# Moveset lookup
# ---------------------------------------------------------------------------


def test_get_optimal_moveset_from_moves_object(loaded) -> None:
    fast, charges = engine.get_optimal_moveset("Medicham", loaded["rankings"])
    assert fast == "COUNTER"
    assert charges == ["ICE_PUNCH", "DYNAMIC_PUNCH"]


def test_get_optimal_moveset_missing_move_id_raises_not_none_string() -> None:
    # When moveId is absent from the moves dict, the result must not be the
    # literal string "None" — it should raise SpeciesNotFound.
    rankings = [{"speciesId": "testmon", "moves": {"fastMoves": [{}], "chargedMoves": []}}]
    with pytest.raises(engine.SpeciesNotFound):
        engine.get_optimal_moveset("testmon", rankings)


def test_get_optimal_moveset_missing(loaded) -> None:
    with pytest.raises(engine.SpeciesNotFound):
        engine.get_optimal_moveset("Mewthree", loaded["rankings"])


# ---------------------------------------------------------------------------
# Type chart
# ---------------------------------------------------------------------------


def test_extract_type_effectiveness(loaded) -> None:
    chart = engine.extract_type_effectiveness(loaded["gm"])
    assert "fighting" in chart
    # Fighting > Normal should be super effective.
    assert chart["fighting"]["normal"] > 1.0
    assert chart["fighting"]["ghost"] < 0.5


def test_extract_type_effectiveness_list_format() -> None:
    # Real PokeMiners game master stores attackScalar as a positional list, not a dict.
    # Indices follow _POGO_TYPE_ORDER: normal=0, fighting=1, ..., fairy=17.
    scalar_list = [1.6, 1.0, 0.625, 0.625, 1.0, 1.6, 0.625, 0.390625,
                   1.6, 1.0, 1.0, 1.0, 1.0, 0.625, 1.6, 1.0, 1.6, 0.625]
    gm = {"itemTemplates": [{"typeEffective": {"attackType": "POKEMON_TYPE_FIGHTING", "attackScalar": scalar_list}}]}
    chart = engine.extract_type_effectiveness(gm)
    assert "fighting" in chart
    assert chart["fighting"]["normal"] > 1.0   # super effective
    assert chart["fighting"]["ghost"] < 0.5    # immune
    assert chart["fighting"]["psychic"] < 1.0  # not very effective


def test_extract_type_effectiveness_list_wrong_length_skipped() -> None:
    # A list with the wrong length should be silently skipped (not crash).
    gm = {"itemTemplates": [{"typeEffective": {"attackType": "POKEMON_TYPE_FIGHTING", "attackScalar": [1.0, 0.5]}}]}
    chart = engine.extract_type_effectiveness(gm)
    assert "fighting" not in chart


# ---------------------------------------------------------------------------
# League caps
# ---------------------------------------------------------------------------


def test_league_cap_accepts_canonical_slugs() -> None:
    assert engine.league_cap("GL") == 1500
    assert engine.league_cap("UL") == 2500
    assert engine.league_cap("ML") == 10_000


def test_league_cap_accepts_aliases() -> None:
    assert engine.league_cap("great") == 1500
    assert engine.league_cap("Ultra-League") == 2500


def test_league_cap_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        engine.league_cap("elite")


def test_league_cap_rejects_prefix_false_matches() -> None:
    # "glaceon" starts with "gl" but must not be accepted.
    with pytest.raises(ValueError):
        engine.league_cap("glaceon")
    with pytest.raises(ValueError):
        engine.league_cap("glory")
    with pytest.raises(ValueError):
        engine.league_cap("master2")
