"""AI layer — Claude-powered natural-language queries about the roster.

The flow, factored into pure-ish pieces so tests can stub the network call:

    ask(query, league)
      → load roster, rankings, base stats, type chart
      → format_roster() + top_meta_context()
      → build_system_prompt() + build_user_message()
      → stream_response() drives Anthropic's streaming API
      → Rich Live display renders the accumulating text as markdown

Every function below is callable on its own. Only ``ask`` touches disk and
the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from . import data, engine


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TOP_N = 20
ENV_API_KEY = "ANTHROPIC_API_KEY"


class AIConfigError(RuntimeError):
    """Raised when the AI layer cannot run — missing key, unreachable API."""


# ---------------------------------------------------------------------------
# Formatting — pure functions
# ---------------------------------------------------------------------------


def _format_ivs(entry: data.RosterEntry) -> str:
    return f"{entry.attack_iv}/{entry.defense_iv}/{entry.stamina_iv}"


def _types_string(species: str, bases: dict[str, engine.BaseStats]) -> str:
    try:
        base = engine.find_base_stats(species, bases)
    except engine.SpeciesNotFound:
        return "Unknown"
    return "/".join(t.capitalize() for t in base.types)


def _rank_string(
    species: str,
    rankings: list[dict[str, Any]],
    league: str,
) -> str:
    try:
        r = engine.lookup_rank(species, rankings)
    except engine.SpeciesNotFound:
        return f"not ranked in {league.upper()}"
    return f"#{r.rank} in {league.upper()}"


def _moves_string(
    entry: data.RosterEntry,
    rankings: list[dict[str, Any]],
) -> str:
    fast = entry.fast_move
    charge_1 = entry.charge_move_1
    charge_2 = entry.charge_move_2
    # Prefer PvPoke's optimal moveset when present and the user hasn't chosen.
    if not (fast and charge_1 and charge_2):
        try:
            f, c = engine.get_optimal_moveset(entry.species, rankings)
            fast = fast or f
            if not charge_1 and len(c) >= 1:
                charge_1 = c[0]
            if not charge_2 and len(c) >= 2:
                charge_2 = c[1]
        except engine.SpeciesNotFound:
            pass
    parts = [p for p in (fast, charge_1, charge_2) if p]
    return " / ".join(parts) if parts else "moves unknown"


def format_roster(
    entries: Sequence[data.RosterEntry],
    rankings: list[dict[str, Any]],
    bases: dict[str, engine.BaseStats],
    league: str,
) -> str:
    """Render the roster as one line per entry.

    Format per the spec: ``Species (Type/Type): IVs X/X/X, Rank #N in GL,
    Moves: Fast / Charge1 / Charge2``. Unknown fields get dashes so Claude
    sees every slot.
    """
    if not entries:
        return "(roster is empty)"
    lines: list[str] = []
    for entry in entries:
        types = _types_string(entry.species, bases)
        ivs = _format_ivs(entry)
        rank = _rank_string(entry.species, rankings, league)
        moves = _moves_string(entry, rankings)
        display_name = (
            f"{entry.species}" if not entry.nickname else f"{entry.species} [{entry.nickname}]"
        )
        lines.append(
            f"- {display_name} ({types}): IVs {ivs}, Lv {entry.level:g}, "
            f"Rank {rank}, Moves: {moves}"
        )
    return "\n".join(lines)


def top_meta_context(
    rankings: list[dict[str, Any]],
    league: str,
    n: int = DEFAULT_TOP_N,
) -> str:
    """Render the top-N PvPoke entries compactly, one per line."""
    if not rankings:
        return f"(no cached rankings for {league.upper()})"
    lines: list[str] = [f"Top {min(n, len(rankings))} in {league.upper()}:"]
    for index, entry in enumerate(rankings[:n], start=1):
        name = entry.get("speciesName") or entry.get("speciesId") or "?"
        types = entry.get("types") or []
        type_str = "/".join(t.capitalize() for t in types if t and t != "none")
        moveset = entry.get("moveset") or []
        moveset_str = " / ".join(str(m) for m in moveset[:3]) if moveset else "moves unknown"
        score = entry.get("score") or entry.get("rating")
        score_str = f", score {float(score):.1f}" if isinstance(score, (int, float)) else ""
        lines.append(
            f"  {index}. {name} ({type_str}){score_str} — {moveset_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_system_prompt(league: str) -> str:
    """The system prompt positions Claude as a PvP coach with bounded data."""
    return (
        "You are a Pokemon GO PvP coach helping a trainer analyze their roster. "
        f"The current context is the {league.upper()} league. "
        "You have access to the trainer's roster and the current top-ranked "
        "meta Pokemon. Answer concisely, ground every recommendation in the "
        "data provided, and flag any claim you are not certain about. "
        "Do not invent ranks, stat products, or matchups you were not given — "
        "if the data is insufficient, say so."
    )


def build_user_message(
    *,
    roster_text: str,
    meta_text: str,
    query: str,
) -> str:
    """Combine roster, meta context, and the user's question into one message."""
    return (
        "Here is the trainer's roster:\n\n"
        f"{roster_text}\n\n"
        "Meta context (current top-ranked Pokemon in this league):\n\n"
        f"{meta_text}\n\n"
        f"Question: {query}\n"
    )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@dataclass
class StreamConfig:
    """Parameters passed to the streaming call — in one place for clarity."""

    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS


def stream_response(
    client: Any,
    *,
    system: str,
    user_message: str,
    config: StreamConfig,
) -> Iterator[str]:
    """Yield text chunks from Anthropic's streaming API.

    ``client`` only needs a ``messages.stream(...)`` attribute whose return
    value is a context manager exposing ``.text_stream`` (iterable of strs).
    This shape is what the real SDK uses, and what tests mock.
    """
    with client.messages.stream(
        model=config.model,
        max_tokens=config.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            yield chunk


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def _default_client_factory(api_key: str) -> Any:
    """Import the real Anthropic client lazily so tests can avoid the import."""
    import anthropic  # local import — not every test path needs it
    return anthropic.Anthropic(api_key=api_key)


def _resolve_api_key() -> str:
    key = os.environ.get(ENV_API_KEY)
    if not key or not key.strip():
        raise AIConfigError(
            f"{ENV_API_KEY} is not set. Export it in your shell, e.g. "
            f'`export {ENV_API_KEY}="sk-ant-..."`, then re-run.'
        )
    return key


# ---------------------------------------------------------------------------
# Top-level — ``ask``
# ---------------------------------------------------------------------------


def ask(
    query: str,
    league: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    client: Any | None = None,
    client_factory: Callable[[str], Any] | None = None,
    stream_config: StreamConfig | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Send ``query`` to Claude with full roster + meta context.

    Yields text to ``on_chunk`` as it arrives (if given) and returns the full
    concatenated response once the stream is done. If ``client`` is ``None``,
    one is created via ``client_factory`` using the ``ANTHROPIC_API_KEY``
    environment variable.
    """
    try:
        rankings = data.load_pvpoke_rankings(league)
    except data.CacheMissError as exc:
        raise AIConfigError(str(exc)) from exc
    gm = data.load_game_master()
    bases = engine.extract_base_stats(gm)
    entries = data.get_all_entries()

    roster_text = format_roster(entries, rankings, bases, league)
    meta_text = top_meta_context(rankings, league, n=top_n)
    system = build_system_prompt(league)
    user_message = build_user_message(
        roster_text=roster_text, meta_text=meta_text, query=query
    )

    if client is None:
        key = _resolve_api_key()
        factory = client_factory or _default_client_factory
        client = factory(key)

    config = stream_config or StreamConfig()
    try:
        chunks: list[str] = []
        for chunk in stream_response(
            client, system=system, user_message=user_message, config=config
        ):
            chunks.append(chunk)
            if on_chunk:
                on_chunk(chunk)
        return "".join(chunks)
    except AIConfigError:
        raise
    except Exception as exc:  # pragma: no cover — surface-area for SDK errors
        import anthropic as _anthropic
        if isinstance(exc, _anthropic.AuthenticationError):
            raise AIConfigError(
                "Authentication failed — check your ANTHROPIC_API_KEY."
            ) from exc
        if isinstance(exc, _anthropic.RateLimitError):
            raise AIConfigError(
                "Rate limit reached — wait a moment and try again."
            ) from exc
        if isinstance(exc, _anthropic.APIConnectionError):
            raise AIConfigError(
                "Could not reach the Claude API — check your network connection."
            ) from exc
        name = type(exc).__name__
        raise AIConfigError(f"Claude API error ({name}): {exc}") from exc


# Phase 9: ``make_live_renderer`` moved to ``pvp.display`` so this module
# has no Rich dependency. The CLI wires the two together.
