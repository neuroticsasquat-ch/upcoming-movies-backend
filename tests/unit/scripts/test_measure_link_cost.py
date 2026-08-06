from uuid import uuid4

from scripts.measure_link_cost import (
    format_report,
    measure_sequential,
)
from upmovies.link.roster import Roster, RosterEntry
from upmovies.llm.client import Usage
from upmovies.llm.pricing import Rates, price
from upmovies.news.models import Story

RATES = Rates(input_per_mtok=1.00, output_per_mtok=5.00)


def test_price_full_rates_sequential():
    # 1,000,000 uncached input @ $1 + 1,000,000 output @ $5 = $6.00
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price(u, RATES, batch=False) == 6.0


def test_price_cache_multipliers():
    # cache write = 1.25x input, cache read = 0.10x input
    u = Usage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    # 1.25 + 0.10 = $1.35
    assert price(u, RATES, batch=False) == 1.35


def test_price_batch_halves_total():
    # The batch call path is gone (ADR-0005), but `batch=True` must keep pricing historical
    # `run_llm_usage` rows that were recorded under it.
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price(u, RATES, batch=True) == 3.0


def test_format_report_includes_totals_and_decision_fields():
    seq = [
        Usage(
            input_tokens=100,
            output_tokens=10,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=100,
        )
    ]
    report = format_report(seq, RATES)
    assert "cache_read_input_tokens" in report
    assert "cache_creation_input_tokens" in report
    assert "sequential" in report.lower()
    assert "$" in report


def _roster(film_id):
    entry = RosterEntry(
        film_id=film_id, title="Runner", original_title=None, year=2026, overview=None, genres=[]
    )
    return Roster(entries=[entry], text='#1 "Runner" (2026)')


def _story(title="A headline"):
    return Story(
        id=uuid4(), source="X", url=f"https://e/{uuid4()}", title=title, raw={"summary": ""}
    )


class _FakeUsageClient:
    """Returns a fixed `Usage` per call, recording how many calls were made."""

    def __init__(self, usage: Usage):
        self._usage = usage
        self.complete_calls = 0

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        self.complete_calls += 1
        return "[]", self._usage


async def test_measure_sequential_sums_usage_across_chunks():
    u = Usage(
        input_tokens=10, output_tokens=2, cache_read_input_tokens=5, cache_creation_input_tokens=1
    )
    client = _FakeUsageClient(u)
    chunks = [[_story()], [_story()], [_story()]]  # 3 chunks
    total = await measure_sequential(client, _roster(uuid4()), chunks, model="link-m")
    assert client.complete_calls == 3
    assert total == Usage(
        input_tokens=30, output_tokens=6, cache_read_input_tokens=15, cache_creation_input_tokens=3
    )
