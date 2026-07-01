import json

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
