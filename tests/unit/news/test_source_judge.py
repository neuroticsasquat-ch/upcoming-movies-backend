import json
import logging

from upmovies.llm.client import Usage
from upmovies.news.source_quality import (
    build_judge_request,
    judge_domains,
    parse_judge_verdicts,
)


def test_build_judge_request_shape():
    system, messages = build_judge_request(
        [{"domain": "mshale.com", "sample_headline": "New trailer!"}]
    )
    assert isinstance(system, list) and system[0]["type"] == "text"
    assert len(messages) == 1 and messages[0]["role"] == "user"
    payload = json.loads(messages[0]["content"])
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

    async def complete_with_usage(
        self, *, model: str, system: list, messages: list, max_tokens: int = 4096
    ):
        return self._text, Usage(input_tokens=5, output_tokens=3)


async def test_judge_domains_calls_client_and_parses():
    client = _FakeClient('[{"domain": "mshale.com", "tier": "low", "reason": "aggregator"}]')
    verdicts, usage = await judge_domains(
        client=client,
        model="claude-haiku-4-5",
        items=[{"domain": "mshale.com", "sample_headline": "h"}],
    )
    assert verdicts == {"mshale.com": ("low", "aggregator")}
    assert usage.output_tokens == 3


async def test_judge_domains_empty_is_noop():
    client = _FakeClient("[]")
    verdicts, usage = await judge_domains(client=client, model="m", items=[])
    assert verdicts == {}
    assert usage == Usage()


class _RecordingClient:
    """Fake Completer that records each call and answers with a valid verdict array
    covering exactly the domains it was asked about (tier 'acceptable')."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    async def complete_with_usage(
        self, *, model: str, system: list, messages: list, max_tokens: int = 4096
    ):
        items = json.loads(messages[0]["content"])
        self.calls.append(items)
        arr = [{"domain": it["domain"], "tier": "acceptable", "reason": "ok"} for it in items]
        return json.dumps(arr), Usage(input_tokens=10, output_tokens=7)


async def test_judge_domains_chunks_large_batches():
    client = _RecordingClient()
    items = [{"domain": f"d{i}.com", "sample_headline": "h"} for i in range(60)]
    verdicts, usage = await judge_domains(client=client, model="m", items=items)
    # 60 domains / batch size 25 -> 3 calls, each bounded.
    assert len(client.calls) == 3
    assert all(len(call) <= 25 for call in client.calls)
    # All 60 domains judged, verdicts merged across chunks.
    assert len(verdicts) == 60
    assert verdicts["d59.com"] == ("acceptable", "ok")
    # Usage summed across the 3 chunk calls.
    assert usage.output_tokens == 21


async def test_judge_domains_logs_when_a_chunk_yields_no_verdicts(caplog):
    class _OneBadChunk:
        def __init__(self):
            self.n = 0

        async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
            self.n += 1
            items = json.loads(messages[0]["content"])
            if self.n == 1:
                return "truncated junk with no closing bracket [", Usage(output_tokens=5)
            arr = [{"domain": it["domain"], "tier": "low", "reason": "x"} for it in items]
            return json.dumps(arr), Usage(output_tokens=5)

    client = _OneBadChunk()
    items = [{"domain": f"d{i}.com", "sample_headline": "h"} for i in range(30)]
    with caplog.at_level(logging.WARNING):
        verdicts, _ = await judge_domains(client=client, model="m", items=items)
    # Second chunk still merged despite the first failing.
    assert len(verdicts) == 5
    assert any("no verdicts" in r.message for r in caplog.records)
