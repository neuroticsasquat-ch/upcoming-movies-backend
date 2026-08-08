"""`OpenAICompatClient` against a mocked transport — never the live network (`CLAUDE.md`).

The `usage` mapping is asserted against whole recorded bodies in `tests/fixtures/llm/` rather
than hand-written dicts, because the thing most likely to be wrong is a *field name*, and a
hand-written dict can only ever agree with whatever the author assumed. See that directory's
README for what those bodies are and — importantly — what has not yet been verified about them.
"""

import json
import pathlib

import httpx
import pytest
import respx

from upmovies.llm import AnthropicClient, OpenAICompatClient
from upmovies.llm.retry import RetryPolicy
from upmovies.llm.types import CallLog, Completer, Prompt, UnsupportedPromptError, Usage

_FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "llm"

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai/chat/completions"

# Backoff is irrelevant to what these tests assert, and waiting for it would make the suite
# slow for nothing. Attempt *counts* are still the real defaults.
_NO_WAIT = RetryPolicy(initial_backoff=0.0, jitter=0.0)


def _recorded(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


def _completion(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1785283200,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    )


# --- the wire request ---------------------------------------------------------------------


@respx.mock
async def test_the_stable_prefix_becomes_the_leading_system_message():
    """The neutral contract is "stable content first, byte-identical across calls". Here there
    is no `cache_control` to place — DeepInfra and DeepSeek cache automatically on the longest
    byte-identical prefix — so the adapter's whole obligation is *position*: the prefix leads,
    unmodified, and the varying payload follows it (spec §5.1)."""
    route = respx.post(DEEPSEEK_URL).mock(return_value=_completion("hello"))
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        out = await c.complete(
            model="deepseek-v4-flash",
            prompt=Prompt(stable_prefix="INSTRUCTIONS", user="hi", max_tokens=16),
        )

    assert out == "hello"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["max_tokens"] == 16
    assert body["messages"] == [
        {"role": "system", "content": "INSTRUCTIONS"},
        {"role": "user", "content": "hi"},
    ]
    assert "response_format" not in body


@respx.mock
async def test_json_object_becomes_a_response_format_directive():
    route = respx.post(DEEPSEEK_URL).mock(return_value=_completion("{}"))
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        await c.complete(
            model="deepseek-v4-flash",
            prompt=Prompt(stable_prefix="I", user="hi", json_object=True),
        )

    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"}


@respx.mock
async def test_the_api_key_is_sent_as_a_bearer_token():
    respx.post(DEEPINFRA_URL).mock(return_value=_completion("ok"))
    async with OpenAICompatClient(provider="deepinfra", api_key="sekrit") as c:
        await c.complete(
            model="deepseek-ai/DeepSeek-V4-Flash",
            prompt=Prompt(stable_prefix="I", user="hi"),
        )

    assert respx.calls.last.request.headers["authorization"] == "Bearer sekrit"


async def test_a_prefill_is_refused_rather_than_silently_dropped():
    """`summarize` relies on the prefill to force a JSON-envelope continuation, and its parser
    is written to read that continuation. Honouring the rest of the request while quietly
    discarding this part would hand that parser a shape it does not expect — at the one stage
    that recovers from bad JSON instead of failing loudly. So the adapter refuses (`types.Prompt`).
    """
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        with pytest.raises(UnsupportedPromptError):
            await c.complete_call(
                model="deepseek-v4-flash",
                prompt=Prompt(stable_prefix="I", user="hi", prefill='{"summary": "'),
                calls=calls,
            )

    # Nothing reached the provider, so there is nothing to bill or to record.
    assert calls.results == ()


async def test_both_adapters_satisfy_the_completer_protocol():
    """The seam's whole point: a stage takes a `Completer` and cannot tell which provider
    answered it. The annotation is what pyright checks — the assertion is incidental."""
    async with (
        AnthropicClient(api_key="k") as anthropic,
        OpenAICompatClient(provider="deepseek", api_key="k") as openai_compat,
    ):
        completers: list[Completer] = [anthropic, openai_compat]

    assert len(completers) == 2


def test_an_unknown_provider_is_refused_at_construction():
    """Same discipline as `rates_for`: crash on an unrecognised provider rather than guess a
    base URL for it."""
    with pytest.raises(KeyError):
        OpenAICompatClient(provider="anthropic", api_key="k")


# --- usage mapping, against recorded bodies -----------------------------------------------


@respx.mock
async def test_deepseek_cached_tokens_map_onto_usage():
    """DeepSeek reports the hit/miss split directly, so `input_tokens` is its miss count — not
    `prompt_tokens`, which is the inclusive total."""
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=_recorded("deepseek_chat_completion"))
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        text, usage = await c.complete_with_usage(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi")
        )

    assert json.loads(text)[0]["kind"] == "casting"
    assert usage == Usage(
        input_tokens=392,
        output_tokens=120,
        cache_read_input_tokens=4608,
        cache_creation_input_tokens=0,
    )


@respx.mock
async def test_deepinfra_cached_tokens_map_onto_usage():
    """DeepInfra reports the OpenAI shape, where `prompt_tokens` *includes* the cached tokens.
    Mapping that total straight onto `input_tokens` would price every cached token twice —
    once at base and once at the cache-read rate (see `Rates` in `pricing.py`)."""
    respx.post(DEEPINFRA_URL).mock(
        return_value=httpx.Response(200, json=_recorded("deepinfra_chat_completion"))
    )
    async with OpenAICompatClient(provider="deepinfra", api_key="k") as c:
        _text, usage = await c.complete_with_usage(
            model="deepseek-ai/DeepSeek-V4-Flash", prompt=Prompt(stable_prefix="I", user="hi")
        )

    assert usage == Usage(
        input_tokens=648,  # 5000 total - 4352 cached
        output_tokens=64,
        cache_read_input_tokens=4352,
        cache_creation_input_tokens=0,
    )


@respx.mock
async def test_a_zero_cache_hit_is_read_as_zero_and_not_as_a_missing_field():
    """A genuine cache miss reports `prompt_cache_hit_tokens: 0`. Resolving the two shapes on
    truthiness would read that as "field absent" and fall through to the other one — so the
    stage most worth measuring, the one where caching did *not* engage, would be the stage that
    reported cached tokens it never got."""
    respx.post(DEEPSEEK_URL).mock(
        return_value=_completion(
            "ok",
            usage={
                "prompt_tokens": 300,
                "completion_tokens": 7,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 256},
            },
        )
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        _text, usage = await c.complete_with_usage(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi")
        )

    assert usage == Usage(input_tokens=300, output_tokens=7, cache_read_input_tokens=0)


@respx.mock
async def test_a_response_with_no_cache_fields_reports_all_input_as_uncached():
    respx.post(DEEPSEEK_URL).mock(
        return_value=_completion(
            "ok", usage={"prompt_tokens": 300, "completion_tokens": 7, "total_tokens": 307}
        )
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        _text, usage = await c.complete_with_usage(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi")
        )

    assert usage == Usage(input_tokens=300, output_tokens=7)


@respx.mock
async def test_cache_creation_is_always_zero_on_an_automatic_caching_provider():
    """There is no explicit write to charge for when the provider matches prefixes by itself.
    Leaving the field at 0 is what makes `pricing.price` charge no write premium here, which is
    the whole reason `cache_write_mult` stopped being an Anthropic constant (spec §7)."""
    respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(200, json=_recorded("deepseek_chat_completion"))
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        _text, usage = await c.complete_with_usage(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi")
        )

    assert usage.cache_creation_input_tokens == 0


# --- telemetry ----------------------------------------------------------------------------


@respx.mock
async def test_complete_call_records_the_call_and_returns_it():
    respx.post(DEEPSEEK_URL).mock(return_value=_completion("hi there"))
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k") as c:
        result = await c.complete_call(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
        )

    assert result.text == "hi there"
    assert result.ok is True
    assert result.error_type is None
    assert result.parse_ok is None
    assert result.attempts == 1
    assert result.latency_ms >= 0
    assert calls.results == (result,)


# --- retries ------------------------------------------------------------------------------


@respx.mock
async def test_a_429_with_retry_after_is_retried_and_counted_as_one_call():
    respx.post(DEEPSEEK_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow down"}),
            _completion("ok"),
        ]
    )
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        result = await c.complete_call(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
        )

    assert result.attempts == 2
    assert result.ok is True
    assert len(calls.results) == 1  # one *logical* call, retries folded in (spec §5.2)


@respx.mock
async def test_a_5xx_is_retried():
    respx.post(DEEPSEEK_URL).mock(
        side_effect=[httpx.Response(503, json={"error": "unavailable"}), _completion("ok")]
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        result = await c.complete_call(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=CallLog()
        )

    assert result.attempts == 2


@respx.mock
async def test_a_connection_error_is_retried():
    respx.post(DEEPSEEK_URL).mock(
        side_effect=[httpx.ConnectError("no route to host"), _completion("ok")]
    )
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        result = await c.complete_call(
            model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=CallLog()
        )

    assert result.attempts == 2


@respx.mock
async def test_a_4xx_is_not_retried():
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.complete_call(
                model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
            )

    assert route.call_count == 1
    (failed,) = calls.results
    assert failed.attempts == 1


@respx.mock
async def test_exhausted_retries_record_a_failed_call_then_re_raise():
    """Under-retrying does not surface as an error here — `_link_stage_sequential` catches per
    chunk and continues — so it would surface as a silently dropped chunk of 15 stories that an
    eval run misreads as "this provider links fewer stories" (spec §9). The row is what makes
    the difference visible."""
    route = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.complete_call(
                model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
            )

    assert route.call_count == 4  # 1 initial attempt + the default 3 retries
    (failed,) = calls.results
    assert failed.ok is False
    assert failed.attempts == 4
    assert failed.error_type == "HTTPStatusError"
    assert failed.usage == Usage()


@respx.mock
async def test_a_200_whose_body_is_not_json_is_recorded_and_not_retried():
    """A response that arrives but does not parse is still a call that was made, took time and
    cost tokens — and retrying it would just buy the same broken body again."""
    route = respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, text="not json"))
    calls = CallLog()
    async with OpenAICompatClient(provider="deepseek", api_key="k", policy=_NO_WAIT) as c:
        with pytest.raises(Exception):  # noqa: B017 — json module's own decode error type
            await c.complete_call(
                model="deepseek-v4-flash", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
            )

    assert route.call_count == 1
    (failed,) = calls.results
    assert failed.ok is False
    assert failed.error_type is not None
