import json
from datetime import UTC, date, datetime

import pytest

from upmovies.llm.client import BatchResult
from upmovies.synthesize.summarizer import (
    EventInput,
    StoryInput,
    build_summary_batch_request,
    build_summary_request,
    parse_summary,
    summarize_event,
)


def _event(*, stories=None, event_type="casting", film_title="Runner"):
    return EventInput(
        event_type=event_type,
        film_title=film_title,
        source_updated_at=datetime(2026, 6, 22, tzinfo=UTC),
        stories=stories or [StoryInput(title="Big news", dek="A short dek.", source="Deadline")],
    )


class FakeCompleter:
    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        from upmovies.llm.client import Usage

        self.calls.append(
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        )
        return self._response, Usage()


class FakeBatchCompleter:
    def __init__(self, response: str):
        self._response = response
        self.requests: list | None = None

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        self.requests = list(requests)
        return {
            r.custom_id: BatchResult(custom_id=r.custom_id, ok=True, text=self._response)
            for r in requests
        }


def test_build_summary_request_plain_block_and_payload():
    event = _event(
        stories=[
            StoryInput(title="Casting news", dek="Star joins the film.", source="Variety"),
            StoryInput(title="More", dek="Another report.", source="THR"),
        ]
    )
    system, messages = build_summary_request(event, date(2026, 6, 25))
    # plain block — NO caching (short prompt, sub-minimum cacheable size)
    assert "cache_control" not in system[0]
    assert "paraphrase" in system[0]["text"].lower()
    assert "as_of_date" in system[0]["text"]  # instructions point at the field
    # NEU-453: the beat is dated by its own date, not another date in the sources.
    # (verbatim substring of the new rule — case-sensitive)
    assert "OWN date" in system[0]["text"]
    payload = json.loads(messages[0]["content"])
    assert payload["as_of_date"] == "2026-06-25"
    assert payload["film"] == "Runner"
    assert payload["event_type"] == "casting"
    assert [s["source"] for s in payload["stories"]] == ["Variety", "THR"]
    assert payload["stories"][0]["title"] == "Casting news"
    assert payload["stories"][0]["dek"] == "Star joins the film."


def test_build_summary_batch_request_carries_as_of_date():
    system, messages = build_summary_request(_event(), date(2026, 6, 25))
    assert json.loads(messages[0]["content"])["as_of_date"] == "2026-06-25"
    req = build_summary_batch_request(
        custom_id="e1", model="m", event=_event(), run_date=date(2026, 6, 25)
    )
    assert json.loads(req.messages[0]["content"])["as_of_date"] == "2026-06-25"


def test_build_summary_request_truncates_dek():
    long_dek = "x" * 1000
    _system, messages = build_summary_request(
        _event(stories=[StoryInput(title="t", dek=long_dek, source="Deadline")]),
        date(2026, 6, 25),
    )
    payload = json.loads(messages[0]["content"])
    assert len(payload["stories"][0]["dek"]) == 500


def test_parse_summary_extracts_and_strips():
    assert parse_summary('{"summary": "  A neutral update.  "}') == "A neutral update."


def test_parse_summary_tolerates_prose_and_fences():
    raw = 'Sure:\n```json\n{"summary": "Filming wrapped."}\n```'
    assert parse_summary(raw) == "Filming wrapped."


def test_parse_summary_raises_on_empty():
    with pytest.raises(ValueError):
        parse_summary('{"summary": "   "}')


def test_parse_summary_raises_on_missing_key():
    with pytest.raises(ValueError):
        parse_summary('{"note": "no summary here"}')


def test_parse_summary_raises_on_malformed():
    with pytest.raises(json.JSONDecodeError):
        parse_summary("not json at all")


def test_build_summary_request_seeds_assistant_prefill():
    # The assistant turn is prefilled so the model continues a JSON envelope (no preamble).
    _system, messages = build_summary_request(_event(), date(2026, 6, 25))
    assert messages[-1] == {"role": "assistant", "content": '{"summary": "'}


def test_parse_summary_handles_prefilled_continuation():
    # With the prefill, the model's reply has no leading brace — it continues the envelope.
    assert parse_summary('A neutral update."}') == "A neutral update."


def test_parse_summary_rejects_runaway_reasoning():
    # Chain-of-thought that slips past the JSON contract is far too long to be a summary.
    leaked = "Wait, let me reconsider this beat. " * 30  # > 400 chars
    with pytest.raises(ValueError):
        parse_summary(f'{{"summary": "{leaked}"}}')


def test_parse_summary_rejects_code_fence_leak():
    with pytest.raises(ValueError):
        parse_summary('{"summary": "no release date. ``` json {x}"}')


async def test_summarize_event_returns_result_with_provenance():
    client = FakeCompleter('{"summary": "The studio confirmed a 2027 release."}')
    event = _event()
    result, _usage = await summarize_event(
        client=client,
        model="claude-haiku-4-5",
        prompt_version="1",
        event=event,
        run_date=date(2026, 6, 25),
    )
    assert result.summary == "The studio confirmed a 2027 release."
    assert result.model == "claude-haiku-4-5"
    assert result.prompt_version == "1"
    assert result.source_updated_at == event.source_updated_at
    # the sequential path pins max_tokens for parity with the batched path
    assert client.calls[0]["max_tokens"] == 256


async def test_summarize_event_prompt_includes_every_member_story():
    client = FakeCompleter('{"summary": "ok."}')
    event = _event(
        stories=[
            StoryInput(title="A", dek="da", source="Deadline"),
            StoryInput(title="B", dek="db", source="Variety"),
            StoryInput(title="C", dek="dc", source="THR"),
        ]
    )
    await summarize_event(
        client=client, model="m", prompt_version="1", event=event, run_date=date(2026, 6, 25)
    )
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert [s["title"] for s in payload["stories"]] == ["A", "B", "C"]
    assert [s["source"] for s in payload["stories"]] == ["Deadline", "Variety", "THR"]


async def test_batched_path_round_trips_to_same_summary():
    event = _event()
    envelope = '{"summary": "Principal photography has begun."}'
    client = FakeBatchCompleter(envelope)

    req = build_summary_batch_request(
        custom_id="evt-9", model="m", event=event, run_date=date(2026, 6, 25)
    )
    assert req.custom_id == "evt-9"
    assert req.model == "m"
    assert req.max_tokens == 256
    # parity: same system + messages the sequential builder produces
    system, messages = build_summary_request(event, date(2026, 6, 25))
    assert req.system == system
    assert req.messages == messages
    assert "cache_control" not in req.system[0]

    results = await client.complete_batch([req])
    assert client.requests is not None
    assert client.requests[0].custom_id == "evt-9"
    result = results["evt-9"]
    assert result.ok
    # batched path yields the identical parsed summary as the sequential path
    assert parse_summary(result.text) == "Principal photography has begun."


def test_parse_summary_recovers_unescaped_inner_quote(caplog):
    raw = '{"summary": "Netflix calls it "the one" of the franchise."}'
    with caplog.at_level("WARNING"):
        assert parse_summary(raw) == 'Netflix calls it "the one" of the franchise.'
    assert "recovered summary from malformed" in caplog.text


def test_parse_summary_recovers_control_char_in_value(caplog):
    raw = '{"summary": "Filming\x01wrapped in Atlanta."}'
    with caplog.at_level("WARNING"):
        assert parse_summary(raw) == "Filming wrapped in Atlanta."
    assert "recovered summary from malformed" in caplog.text


def test_parse_summary_recovers_from_fenced_malformed_envelope():
    raw = 'Sure:\n```json\n{"summary": "She said "yes" to the role."}\n```'
    assert parse_summary(raw) == 'She said "yes" to the role.'


def test_parse_summary_happy_path_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        assert parse_summary('{"summary": "A clean update."}') == "A clean update."
    assert "recovered summary from malformed" not in caplog.text


def test_instructions_reject_a_different_films_date():
    from upmovies.synthesize.summarizer import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    # Mode 2: a spin-off/sequel/prequel's date co-mentioned in the sources is
    # context, never this film's release-date beat.
    assert "different film" in lowered
    assert "spin-off" in lowered


def test_instructions_require_stating_actual_release_date():
    """NEU-483 #4: a release_date beat must state the actual date, not describe the
    announcement only in relational terms."""
    from upmovies.synthesize.summarizer import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "state the actual date" in lowered
    assert "without giving the date itself" in lowered


def test_instructions_allow_naming_people_not_descriptors():
    """NEU-483 #6: naming an actor/character is a fact, not a paraphrase violation —
    don't substitute "the lead actor" / "his character" for the actual name."""
    from upmovies.synthesize.summarizer import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "not a paraphrase violation" in lowered
    assert "vague descriptor" in lowered


def test_instructions_forbid_genre_label_and_unrelated_history_filler():
    """NEU-483 #10, #11: no genre-label tack-ons unless the genre itself is the news,
    and no unrelated history (e.g. a prior title change) bleeding into the beat."""
    from upmovies.synthesize.summarizer import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "genre label" in lowered
    assert "prior title change" in lowered
