import json
import logging

from upmovies.llm import CallLog, CallResult, Usage
from upmovies.news.source_quality import (
    build_judge_request,
    judge_domains,
    judge_output_parses,
    parse_judge_verdicts,
)


def test_build_judge_request_shape():
    prompt = build_judge_request([{"domain": "mshale.com", "sample_headline": "New trailer!"}])
    assert "source-quality rater" in prompt.stable_prefix
    payload = json.loads(prompt.user)
    assert payload == [{"domain": "mshale.com", "sample_headline": "New trailer!"}]


def test_parse_judge_verdicts_keeps_known_valid():
    raw = (
        'Here you go:\n[{"domain": "mshale.com", "tier": "low", "reason": "aggregator"},'
        ' {"domain": "variety.com", "tier": "trusted", "reason": "trade"},'
        ' {"domain": "evil.test", "tier": "low", "reason": "x"},'
        ' {"domain": "variety.com", "tier": "bogus", "reason": "y"}]'
    )
    out = parse_judge_verdicts(raw, domains={"mshale.com", "variety.com"})
    assert out == {"mshale.com": ("low", "aggregator"), "variety.com": ("trusted", "trade")}


def test_parse_judge_verdicts_unparseable_returns_empty():
    assert parse_judge_verdicts("no json here", domains={"a.com"}) == {}


class _FakeClient:
    def __init__(self, text: str):
        self._text = text

    async def complete_call(self, *, model: str, prompt, calls):
        return calls.record(
            CallResult(text=self._text, usage=Usage(input_tokens=5, output_tokens=3))
        )


async def test_judge_domains_calls_client_and_parses():
    client = _FakeClient('[{"domain": "mshale.com", "tier": "low", "reason": "aggregator"}]')
    calls = CallLog()
    verdicts = await judge_domains(
        client=client,
        model="claude-haiku-4-5",
        items=[{"domain": "mshale.com", "sample_headline": "h"}],
        calls=calls,
    )
    assert verdicts == {"mshale.com": ("low", "aggregator")}
    assert calls.usage.output_tokens == 3
    assert [r.parse_ok for r in calls.results] == [True]


async def test_judge_domains_empty_is_noop():
    client = _FakeClient("[]")
    calls = CallLog()
    verdicts = await judge_domains(client=client, model="m", items=[], calls=calls)
    assert verdicts == {}
    assert calls.results == ()
    assert calls.usage == Usage()


class _RecordingClient:
    """Fake Completer that records each call and answers with a valid verdict array
    covering exactly the domains it was asked about (tier 'acceptable')."""

    def __init__(self):
        self.requests: list[list[dict]] = []

    async def complete_call(self, *, model: str, prompt, calls):
        items = json.loads(prompt.user)
        self.requests.append(items)
        arr = [{"domain": it["domain"], "tier": "acceptable", "reason": "ok"} for it in items]
        return calls.record(
            CallResult(text=json.dumps(arr), usage=Usage(input_tokens=10, output_tokens=7))
        )


async def test_judge_domains_chunks_large_batches():
    client = _RecordingClient()
    items = [{"domain": f"d{i}.com", "sample_headline": "h"} for i in range(60)]
    calls = CallLog()
    verdicts = await judge_domains(client=client, model="m", items=items, calls=calls)
    # 60 domains / batch size 25 -> 3 calls, each bounded.
    assert len(client.requests) == 3
    assert all(len(call) <= 25 for call in client.requests)
    # All 60 domains judged, verdicts merged across chunks.
    assert len(verdicts) == 60
    assert verdicts["d59.com"] == ("acceptable", "ok")
    # Usage summed across the 3 chunk calls.
    assert calls.usage.output_tokens == 21


async def test_judge_domains_logs_when_a_chunk_yields_no_verdicts(caplog):
    class _OneBadChunk:
        def __init__(self):
            self.n = 0

        async def complete_call(self, *, model, prompt, calls):
            self.n += 1
            items = json.loads(prompt.user)
            if self.n == 1:
                return calls.record(
                    CallResult(
                        text="truncated junk with no closing bracket [",
                        usage=Usage(output_tokens=5),
                    )
                )
            arr = [{"domain": it["domain"], "tier": "low", "reason": "x"} for it in items]
            return calls.record(CallResult(text=json.dumps(arr), usage=Usage(output_tokens=5)))

    client = _OneBadChunk()
    items = [{"domain": f"d{i}.com", "sample_headline": "h"} for i in range(30)]
    with caplog.at_level(logging.WARNING):
        calls = CallLog()
        verdicts = await judge_domains(client=client, model="m", items=items, calls=calls)
    # Second chunk still merged despite the first failing.
    assert len(verdicts) == 5
    assert any("no verdicts" in r.message for r in caplog.records)


def test_judge_output_parses_separates_format_from_usefulness():
    """`parse_ok` records output-format compliance, not quality (design §11) — a reply that is
    valid JSON but yields no usable verdict parsed fine, and must not be recorded as a parse
    failure or `source_judge`'s rate stops being comparable to the other three stages'."""
    unusable_but_valid = '[{"domain": "unasked.com", "tier": "bogus"}]'
    assert parse_judge_verdicts(unusable_but_valid, domains={"asked.com"}) == {}
    assert judge_output_parses(unusable_but_valid) is True
    assert judge_output_parses("truncated junk with no closing bracket [") is False
    assert judge_output_parses('{"verdicts": null}') is False  # an object, not the array
    # Mirrors `parse_judge_verdicts` exactly, fence-salvaging included — the two must agree on
    # what "parsed" means, or `parse_ok` describes a parse the stage never performed.
    assert judge_output_parses('```json\n[{"domain": "a.com"}]\n```') is True


async def test_judge_domains_records_parse_ok_from_format_not_verdict_count():
    client = _FakeClient('[{"domain": "unasked.com", "tier": "trusted", "reason": "x"}]')
    calls = CallLog()
    verdicts = await judge_domains(
        client=client,
        model="claude-haiku-4-5",
        items=[{"domain": "asked.com", "sample_headline": "h"}],
        calls=calls,
    )
    assert verdicts == {}  # nothing usable came back...
    assert [r.parse_ok for r in calls.results] == [True]  # ...but the reply parsed fine
