# pvp-analyzer

A command-line Pokemon GO PVP roster analyzer with a Claude-powered natural
language interface.

## Install

```bash
pip install pvp-analyzer
```

For development (from a clone of this repo):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.10 or newer is required.

## Quick start

```bash
# Download the current PvPoke rankings and PokeMiners game master data.
pvp sync

# Import a roster exported from PoGoHub or CalcyIV (CSV or JSON both work).
pvp import-roster my-roster.csv

# Or add a single Pokemon interactively.
pvp add

# List what's in the roster.
pvp list

# See where a single Pokemon ranks in Great League and what its optimal
# IVs would be.
pvp rank Medicham --league GL

# Compare two roster entries side-by-side.
pvp compare Medicham Azumarill --league GL

# Build the best 3-Pokemon team your roster can field. Optionally require
# coverage of specific types.
pvp team --league GL
pvp team --league GL --cover fairy,ghost

# Ask Claude about your roster. Requires ANTHROPIC_API_KEY.
pvp ask "What are my best options against steel types in Great League?" --league GL
```

### Example output — `pvp rank`

```
╭──────────────────── Medicham  (Fighting/Psychic) ────────────────────╮
│ CP 1496   Rank #1 of 6 (top 16.7%)   Score 100.0                     │
│ IVs 1/15/14 @ L40.5      Stat product 2,094,891                      │
│ Optimal moveset: COUNTER / ICE PUNCH / DYNAMIC PUNCH                 │
╰──────────────────────────────────────────────────────────────────────╯
```

### Example output — `pvp team`

```
                  Great League team (3 members)
┏━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Pos ┃ Species         ┃ Moves           ┃ Covers       ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ 1   │ Medicham        │ Counter / …     │ dark, ice, … │
│ 2   │ Azumarill       │ Bubble / …      │ dragon, …    │
│ 3   │ Registeel       │ Lock-On / …     │ fairy, ice, …│
└─────┴─────────────────┴─────────────────┴──────────────┘
```

## Roster CSV format

Header row required; one row per Pokemon. Unknown columns are ignored,
so files exported from PoGoHub / CalcyIV usually work as-is.

```csv
species,attack_iv,defense_iv,stamina_iv,level,fast_move,charge_move_1,charge_move_2,nickname
Medicham,1,15,14,40.5,COUNTER,ICE_PUNCH,DYNAMIC_PUNCH,Zen
Azumarill,0,15,15,40,,,,
Registeel,3,15,15,27.5,LOCK_ON,FOCUS_BLAST,FLASH_CANNON,
```

Required columns: `species`, `attack_iv`, `defense_iv`, `stamina_iv`,
`level`. Everything else is optional. IVs are 0–15, level is a 0.5 step
between 1 and 51. Invalid rows are reported and skipped; the rest still
import. JSON is also accepted — same field names, either a top-level
list or `{"entries": [...]}`.

## Claude API key

`pvp ask` uses Anthropic's API:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

You can also pass `--plain` to get a streaming, non-Rich transcript suitable
for piping to a file.

## Architecture

Each module has one concern. This is enforced by a test
(`test_display_is_sole_rich_importer`) that fails CI if the boundary leaks.

| Module | Responsibility |
| --- | --- |
| `pvp.data` | SQLAlchemy models + JSON cache loaders. No business logic. |
| `pvp.sync` | Downloads PvPoke rankings + PokeMiners game master, writes the cache. |
| `pvp.engine` | Pure-function CP math, stat product, rank lookup, team builder. |
| `pvp.roster` | CSV/JSON import + add/list/delete over `pvp.data`. |
| `pvp.ai` | Anthropic streaming client; prompt assembly from data + engine output. |
| `pvp.display` | All Rich usage: tables, messaging helpers, streaming renderer. |
| `pvp.cli` | Typer commands that glue the above together. |

## Development

```bash
pytest                           # 155 tests, in-memory SQLite, offline fixtures
pytest --cov=pvp                 # coverage report — aim for 90%+
```

The test suite is hermetic: `tests/conftest.py` redirects
`PVP_ANALYZER_HOME` into a tmp dir and `PVP_ANALYZER_DB_URL` to
`sqlite:///:memory:`, so no test ever touches the real cache or DB file.

## License

MIT.
