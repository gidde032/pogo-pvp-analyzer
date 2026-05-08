"""Phase 8 tests — AI layer (`pvp ask`).

Every test avoids the real Anthropic API by injecting a fake client whose
``messages.stream(...)`` yields prewritten text chunks. This lets us assert
on prompt content, streaming behaviour, error handling, and CLI wiring
without spending a cent.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pvp import ai, data, engine, roster
from pvp.cli import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


@dataclass
class _FakeStream:
    """Stand-in for the context manager the SDK returns from ``.stream()``."""

    chunks: list[str]
    call_record: dict[str, Any]

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    @property
    def text_stream(self):
        yield from self.chunks


class FakeMessages:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.last_call: dict[str, Any] = {}

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.last_call = kwargs
        return _FakeStream(self.chunks, self.last_call)


class FakeClient:
    def __init__(self, chunks: list[str] | None = None) -> None:
        self.messages = FakeMessages(chunks or ["Hello", " ", "trainer."])


class ExplodingMessages:
    def stream(self, **kwargs: Any) -> Any:
        raise RuntimeError("simulated network outage")


class ExplodingClient:
    def __init__(self) -> None:
        self.messages = ExplodingMessages()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _sample_rankings(fixtures_dir: Path) -> list[dict[str, Any]]:
    return json.loads((fixtures_dir / "rankings-gl.json").read_text())


def _sample_bases(fixtures_dir: Path) -> dict[str, engine.BaseStats]:
    gm = json.loads((fixtures_dir / "game-master.json").read_text())
    return engine.extract_base_stats(gm)


@pytest.fixture
def seeded(cache_dir: Path, fixtures_dir: Path) -> None:
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    roster.import_from_file(fixtures_dir / "sample-roster.csv")


# ---------------------------------------------------------------------------
# format_roster
# ---------------------------------------------------------------------------


def test_format_roster_empty() -> None:
    assert ai.format_roster([], [], {}, "gl") == "(roster is empty)"


def test_format_roster_includes_species_types_ivs_rank_moves(
    fixtures_dir: Path,
) -> None:
    rankings = _sample_rankings(fixtures_dir)
    bases = _sample_bases(fixtures_dir)
    entry = data.RosterEntry(
        id=1, species="Medicham",
        attack_iv=1, defense_iv=15, stamina_iv=14, level=50.0,
        fast_move=None, charge_move_1=None, charge_move_2=None,
    )
    rendered = ai.format_roster([entry], rankings, bases, "gl")
    assert "Medicham" in rendered
    assert "Fighting/Psychic" in rendered
    assert "1/15/14" in rendered
    assert "#1 in GL" in rendered
    # Falls back to PvPoke's optimal moveset when user hasn't set their own.
    assert "COUNTER" in rendered
    assert "DYNAMIC_PUNCH" in rendered


def test_format_roster_uses_nickname_if_present(fixtures_dir: Path) -> None:
    rankings = _sample_rankings(fixtures_dir)
    bases = _sample_bases(fixtures_dir)
    entry = data.RosterEntry(
        id=1, species="Medicham", nickname="Zen",
        attack_iv=1, defense_iv=15, stamina_iv=14, level=50.0,
        fast_move=None, charge_move_1=None, charge_move_2=None,
    )
    rendered = ai.format_roster([entry], rankings, bases, "gl")
    assert "Medicham [Zen]" in rendered


def test_format_roster_marks_unknown_species(fixtures_dir: Path) -> None:
    rankings = _sample_rankings(fixtures_dir)
    bases = _sample_bases(fixtures_dir)
    entry = data.RosterEntry(
        id=1, species="Fakemon",  # not in fixture game master or rankings
        attack_iv=0, defense_iv=0, stamina_iv=0, level=1.0,
        fast_move=None, charge_move_1=None, charge_move_2=None,
    )
    rendered = ai.format_roster([entry], rankings, bases, "gl")
    assert "Unknown" in rendered
    assert "not ranked in GL" in rendered


# ---------------------------------------------------------------------------
# top_meta_context
# ---------------------------------------------------------------------------


def test_top_meta_context_truncates(fixtures_dir: Path) -> None:
    rankings = _sample_rankings(fixtures_dir)
    rendered = ai.top_meta_context(rankings, "gl", n=3)
    assert "Top 3 in GL" in rendered
    # First three species appear; fourth does not.
    assert "Medicham" in rendered
    assert "Galarian Stunfisk" in rendered
    assert "Swampert" in rendered
    assert "Azumarill" not in rendered


def test_top_meta_context_empty() -> None:
    rendered = ai.top_meta_context([], "gl")
    assert "no cached rankings" in rendered.lower()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_build_system_prompt_mentions_league() -> None:
    assert "UL" in ai.build_system_prompt("ul")
    # and steers Claude away from fabrication
    assert "do not invent" in ai.build_system_prompt("gl").lower()


def test_build_user_message_weaves_roster_meta_and_query() -> None:
    msg = ai.build_user_message(
        roster_text="R1\nR2", meta_text="M1", query="Who should I build?"
    )
    assert "R1" in msg and "R2" in msg
    assert "M1" in msg
    assert "Who should I build?" in msg
    # Order — roster before meta, query at the end
    assert msg.index("R1") < msg.index("M1") < msg.index("Who should I build?")


# ---------------------------------------------------------------------------
# stream_response
# ---------------------------------------------------------------------------


def test_stream_response_yields_chunks_in_order() -> None:
    client = FakeClient(["A", "B", "C"])
    config = ai.StreamConfig(model="mock", max_tokens=10)
    out = list(ai.stream_response(
        client, system="sys", user_message="hi", config=config,
    ))
    assert out == ["A", "B", "C"]
    # The fake captured the kwargs — verify they match the Anthropic shape.
    call = client.messages.last_call
    assert call["model"] == "mock"
    assert call["max_tokens"] == 10
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# ask — integration with the fake client
# ---------------------------------------------------------------------------


def test_ask_streams_and_returns_full_response(seeded) -> None:
    chunks = ["Medicham ", "is your ", "top-ranked ", "option."]
    client = FakeClient(chunks)
    seen: list[str] = []
    result = ai.ask(
        "Which is my best Pokemon?",
        "gl",
        client=client,
        on_chunk=seen.append,
    )
    assert result == "Medicham is your top-ranked option."
    assert seen == chunks
    # Verify the user message included roster + meta + the actual question.
    user_msg = client.messages.last_call["messages"][0]["content"]
    assert "Medicham" in user_msg  # from roster
    assert "Top" in user_msg  # from meta header
    assert "Which is my best Pokemon?" in user_msg


def test_ask_without_api_key_raises_configerror(
    seeded, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # client=None forces the key lookup
    with pytest.raises(ai.AIConfigError) as excinfo:
        ai.ask("what?", "gl")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_ask_whitespace_only_api_key_raises_configerror(
    seeded, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(ai.AIConfigError) as excinfo:
        ai.ask("what?", "gl")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_ask_with_cache_miss_raises_configerror(
    fixtures_dir: Path,
) -> None:
    # Import a roster but do NOT stage the cache.
    roster.import_from_file(fixtures_dir / "sample-roster.csv")
    with pytest.raises(ai.AIConfigError) as excinfo:
        ai.ask("what?", "gl", client=FakeClient())
    assert "pvp sync" in str(excinfo.value)


def test_ask_wraps_sdk_errors_into_configerror(seeded) -> None:
    with pytest.raises(ai.AIConfigError) as excinfo:
        ai.ask("q", "gl", client=ExplodingClient())
    assert "simulated network outage" in str(excinfo.value)


def test_ask_honors_custom_client_factory(
    seeded, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    invoked: dict[str, Any] = {}

    def factory(api_key: str) -> FakeClient:
        invoked["key"] = api_key
        return FakeClient(["OK"])

    result = ai.ask("q", "gl", client=None, client_factory=factory)
    assert result == "OK"
    assert invoked["key"] == "sk-ant-test"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_ask_empty_roster(cache_dir: Path, fixtures_dir: Path) -> None:
    # Cache exists but roster is empty
    shutil.copy(fixtures_dir / "rankings-gl.json", cache_dir / "rankings-gl.json")
    shutil.copy(fixtures_dir / "game-master.json", cache_dir / "game-master.json")
    result = runner.invoke(app, ["ask", "anything"])
    assert result.exit_code != 0
    assert "empty" in result.output.lower()


def test_cli_ask_missing_api_key(
    seeded, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["ask", "anything", "--plain"])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output


def test_cli_ask_plain_mode_prints_stream(
    seeded, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Replace the factory so no real SDK is needed.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    chunks = ["Hi ", "there!"]

    def fake_factory(api_key: str) -> FakeClient:
        return FakeClient(chunks)

    monkeypatch.setattr(ai, "_default_client_factory", fake_factory)
    result = runner.invoke(app, ["ask", "who wins?", "--plain"])
    assert result.exit_code == 0, result.output
    assert "Hi there!" in result.output
