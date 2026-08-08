import json
import types

import httpx
import pytest
import respx
from anthropic import APIStatusError

from upmovies.llm import AnthropicClient
from upmovies.llm.types import CallLog, CallResult, Prompt, Usage

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _message_response(blocks: list[dict[str, str]], usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": blocks,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 10, "output_tokens": 3},
        },
    )


@respx.mock
async def test_the_stable_prefix_becomes_the_leading_cached_system_block():
    """The adapter — not the builder — is what knows Anthropic caches explicitly. It marks
    the stable prefix unconditionally: deciding *when* caching is worth it would mean
    guessing token counts against a per-model floor, which is the vendor leak the DTO
    removes. Below the floor `cache_control` silently no-ops, so an inert contract costs
    nothing (spec §3)."""
    route = respx.post(MESSAGES_URL).mock(
        return_value=_message_response([{"type": "text", "text": "hello"}])
    )
    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="INSTRUCTIONS", user="hi", max_tokens=16),
        )
    assert out == "hello"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "claude-haiku-4-5"
    assert body["max_tokens"] == 16
    assert body["system"] == [
        {"type": "text", "text": "INSTRUCTIONS", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@respx.mock
async def test_a_prefill_becomes_a_trailing_assistant_turn():
    route = respx.post(MESSAGES_URL).mock(
        return_value=_message_response([{"type": "text", "text": 'ok"}'}])
    )
    async with AnthropicClient(api_key="test-key") as c:
        await c.complete(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="I", user="hi", prefill='{"summary": "'),
        )
    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": '{"summary": "'},
    ]


@respx.mock
async def test_complete_concatenates_text_blocks():
    respx.post(MESSAGES_URL).mock(
        return_value=_message_response(
            [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
        )
    )
    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="X", user="hi")
        )
    assert out == "foobar"


@respx.mock
async def test_complete_with_usage_returns_text_and_usage():
    respx.post(MESSAGES_URL).mock(
        return_value=_message_response(
            [{"type": "text", "text": "hi there"}],
            usage={
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 100,
            },
        )
    )
    async with AnthropicClient(api_key="test-key") as c:
        text, usage = await c.complete_with_usage(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="INSTRUCTIONS", user="hi", max_tokens=16),
        )
    assert text == "hi there"
    assert usage == Usage(
        input_tokens=12,
        output_tokens=4,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=100,
    )


@respx.mock
async def test_complete_call_records_the_call_and_returns_it():
    respx.post(MESSAGES_URL).mock(
        return_value=_message_response(
            [{"type": "text", "text": "hi there"}],
            usage={
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 100,
            },
        )
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key") as c:
        result = await c.complete_call(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="INSTRUCTIONS", user="hi", max_tokens=16),
            calls=calls,
        )

    assert result.text == "hi there"
    assert result.usage == Usage(
        input_tokens=12,
        output_tokens=4,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=100,
    )
    assert result.ok is True
    assert result.error_type is None
    assert result.parse_ok is None
    assert result.attempts == 1
    assert result.latency_ms >= 0
    assert calls.results == (result,)


@respx.mock
async def test_complete_call_counts_a_retried_call_as_one_call_with_two_attempts():
    respx.post(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"type": "rate_limit_error"}}),
            _message_response([{"type": "text", "text": "ok"}]),
        ]
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", max_retries=1) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="S", user="hi"),
            calls=calls,
        )

    # One *logical* call — retries folded in, per NEU-975/spec §5.2 — but visibly retried.
    assert len(calls.results) == 1
    assert result.attempts == 2
    assert result.ok is True


@respx.mock
async def test_complete_call_records_a_failed_call_then_re_raises():
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(500, json={"error": {"type": "api_error"}})
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", max_retries=1) as c:
        with pytest.raises(APIStatusError):
            await c.complete_call(
                model="claude-haiku-4-5",
                prompt=Prompt(stable_prefix="S", user="hi"),
                calls=calls,
            )

    (failed,) = calls.results
    assert failed.ok is False
    assert failed.error_type == "InternalServerError"
    assert failed.attempts == 2
    assert failed.usage == Usage()


@respx.mock
async def test_complete_call_records_a_200_whose_body_does_not_validate():
    """A response that arrives but doesn't deserialize is still a call that was made and paid
    for — it must not vanish from the telemetry just because the SDK raised late."""
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"not": "a message"}))
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", max_retries=0) as c:
        with pytest.raises(Exception):  # noqa: B017 — SDK's own validation error type
            await c.complete_call(
                model="claude-haiku-4-5",
                prompt=Prompt(stable_prefix="S", user="hi"),
                calls=calls,
            )

    (failed,) = calls.results
    assert failed.ok is False
    assert failed.error_type is not None


def test_call_log_stamps_parse_ok_on_the_most_recent_call():
    calls = CallLog()
    calls.record(CallResult(text="a"))
    calls.record(CallResult(text="b"))
    calls.set_parse_ok(False)

    assert [r.parse_ok for r in calls.results] == [None, False]


def test_call_log_set_parse_ok_without_a_call_is_a_programming_error():
    with pytest.raises(ValueError):
        CallLog().set_parse_ok(True)


def test_call_log_usage_sums_every_recorded_call():
    calls = CallLog()
    calls.record(CallResult(usage=Usage(input_tokens=1, output_tokens=2)))
    calls.record(CallResult(usage=Usage(input_tokens=10, cache_read_input_tokens=5)))

    assert calls.usage == Usage(input_tokens=11, output_tokens=2, cache_read_input_tokens=5)


def test_call_log_usage_of_an_empty_log_is_zero():
    assert CallLog().usage == Usage()


def test_usage_from_sdk_maps_all_four_fields():
    sdk = types.SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=5,
    )
    u = Usage.from_sdk(sdk)
    assert u == Usage(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=5,
    )


def test_usage_from_sdk_defaults_missing_cache_fields_to_zero():
    sdk = types.SimpleNamespace(
        input_tokens=10,
        output_tokens=3,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    u = Usage.from_sdk(sdk)
    assert u.cache_read_input_tokens == 0
    assert u.cache_creation_input_tokens == 0
    assert u.input_tokens == 10
    assert u.output_tokens == 3


def test_usage_add_sums_componentwise():
    a = Usage(
        input_tokens=1, output_tokens=2, cache_read_input_tokens=3, cache_creation_input_tokens=4
    )
    b = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=40,
    )
    assert a + b == Usage(
        input_tokens=11,
        output_tokens=22,
        cache_read_input_tokens=33,
        cache_creation_input_tokens=44,
    )


def test_usage_sum_with_zero_start():
    items = [Usage(input_tokens=1), Usage(input_tokens=2), Usage(input_tokens=3)]
    assert sum(items, Usage()).input_tokens == 6
