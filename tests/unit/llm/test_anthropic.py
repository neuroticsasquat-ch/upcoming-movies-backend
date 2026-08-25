import json
import types
from dataclasses import replace

import httpx2
import pytest
from anthropic import APIStatusError

from upmovies.llm import AnthropicClient
from upmovies.llm.retry import RetryPolicy
from upmovies.llm.types import CallLog, CallResult, Prompt, Usage

from .conftest import mock_client

# Backoff is not what these tests are about, and waiting for it would make the suite slow for
# nothing. Attempt *counts* are the real thing under test, so they stay explicit per test.
_NO_WAIT = RetryPolicy(initial_backoff=0.0, jitter=0.0)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _message_response(
    blocks: list[dict[str, str]],
    usage: dict | None = None,
    *,
    stop_reason: str = "end_turn",
) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 10, "output_tokens": 3},
        },
    )


def _client_with(route, **kwargs):
    """Return an AnthropicClient wired to a mock httpx2 client."""
    client, _route = mock_client(**kwargs)
    return AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=client)


async def test_the_stable_prefix_becomes_the_leading_cached_system_block():
    """The adapter — not the builder — is what knows Anthropic caches explicitly. It marks
    the stable prefix unconditionally: deciding *when* caching is worth it would mean
    guessing token counts against a per-model floor, which is the vendor leak the DTO
    removes. Below the floor `cache_control` silently no-ops, so an inert contract costs
    nothing (spec §3)."""
    http_client, route = mock_client(
        return_value=_message_response([{"type": "text", "text": "hello"}])
    )
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
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


async def test_a_prefill_becomes_a_trailing_assistant_turn():
    http_client, route = mock_client(
        return_value=_message_response([{"type": "text", "text": 'ok"}'}])
    )
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        await c.complete(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="I", user="hi", prefill='{"summary": "'),
        )
    body = json.loads(route.calls.last.request.content)
    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": '{"summary": "'},
    ]


async def test_json_object_is_ignored_rather_than_approximated():
    """Anthropic has no `response_format`. The field is a request, not a guarantee, and the
    call sites' hand-rolled extractors are what has always got JSON out of this provider."""
    http_client, route = mock_client(
        return_value=_message_response([{"type": "text", "text": "[]"}])
    )
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        await c.complete(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="I", user="hi", json_object=True),
        )
    assert "response_format" not in json.loads(route.calls.last.request.content)


async def test_a_429_with_retry_after_is_retried_and_the_header_is_read():
    """The same four retry cases are asserted on both adapters, deliberately. A policy shared
    in `retry.py` but wired up differently at one of the two call sites would still leave a
    provider comparison measuring the wiring (spec §9), and only an end-to-end assertion here
    catches that — this is the one test that exercises `_classify` reading `Retry-After` off an
    SDK exception rather than an httpx one."""
    http_client, _route = mock_client(
        side_effect=[
            httpx2.Response(
                429, headers={"Retry-After": "0"}, json={"error": {"type": "rate_limit_error"}}
            ),
            _message_response([{"type": "text", "text": "ok"}]),
        ]
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=http_client) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="S", user="hi"), calls=calls
        )

    assert result.attempts == 2
    assert result.ok is True
    assert len(calls.results) == 1


async def test_exhausted_retries_record_a_failed_call_then_re_raise():
    """Exhaustion at the *default* retry count, so the number of attempts a failing provider
    costs is pinned on this path too."""
    http_client, route = mock_client(
        return_value=httpx2.Response(503, json={"error": {"type": "overloaded_error"}})
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=http_client) as c:
        with pytest.raises(APIStatusError):
            await c.complete_call(
                model="claude-haiku-4-5",
                prompt=Prompt(stable_prefix="S", user="hi"),
                calls=calls,
            )

    assert route.call_count == 4  # 1 initial attempt + the default 3 retries
    (failed,) = calls.results
    assert failed.ok is False
    assert failed.attempts == 4
    assert failed.usage == Usage()


async def test_a_connection_error_is_retried_by_the_shared_loop():
    http_client, _route = mock_client(
        side_effect=[
            httpx2.ConnectError("no route to host"),
            _message_response([{"type": "text", "text": "ok"}]),
        ]
    )
    async with AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=http_client) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="S", user="hi"), calls=CallLog()
        )

    assert result.attempts == 2


async def test_a_4xx_is_not_retried():
    """The shared status verdict applies on this path too — retrying a bad credential four
    times just spends the same failure four times (`retry.retry_for_status`)."""
    http_client, route = mock_client(
        return_value=httpx2.Response(401, json={"error": {"type": "authentication_error"}})
    )
    async with AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=http_client) as c:
        with pytest.raises(APIStatusError):
            await c.complete_call(
                model="claude-haiku-4-5",
                prompt=Prompt(stable_prefix="S", user="hi"),
                calls=CallLog(),
            )

    assert route.call_count == 1


async def test_complete_concatenates_text_blocks():
    http_client, _route = mock_client(
        return_value=_message_response(
            [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
        )
    )
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        out = await c.complete(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="X", user="hi")
        )
    assert out == "foobar"


async def test_complete_with_usage_returns_text_and_usage():
    http_client, _route = mock_client(
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
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
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


async def test_complete_call_records_the_call_and_returns_it():
    http_client, _route = mock_client(
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
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
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


async def test_complete_call_counts_a_retried_call_as_one_call_with_two_attempts():
    """Retries here are the shared loop's, not the SDK's — `max_retries=0` is passed to
    `AsyncAnthropic` on purpose so both adapters retry under one policy rather than two
    separately-configured ones that look alike (spec §9)."""
    http_client, _route = mock_client(
        side_effect=[
            httpx2.Response(
                429, headers={"Retry-After": "0"}, json={"error": {"type": "rate_limit_error"}}
            ),
            _message_response([{"type": "text", "text": "ok"}]),
        ]
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=replace(_NO_WAIT, max_retries=1), http_client=http_client) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5",
            prompt=Prompt(stable_prefix="S", user="hi"),
            calls=calls,
        )

    # One *logical* call — retries folded in, per NEU-975/spec §5.2 — but visibly retried.
    assert len(calls.results) == 1
    assert result.attempts == 2
    assert result.ok is True


async def test_complete_call_records_a_failed_call_then_re_raises():
    http_client, _route = mock_client(
        return_value=httpx2.Response(500, json={"error": {"type": "api_error"}})
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=replace(_NO_WAIT, max_retries=1), http_client=http_client) as c:
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


async def test_complete_call_records_a_200_whose_body_does_not_validate():
    """A response that arrives but doesn't deserialize is still a call that was made and paid
    for — it must not vanish from the telemetry just because the SDK raised late."""
    http_client, _route = mock_client(
        return_value=httpx2.Response(200, json={"not": "a message"})
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=replace(_NO_WAIT, max_retries=0), http_client=http_client) as c:
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


async def test_a_reply_cut_off_at_the_ceiling_is_recorded_as_truncated():
    """Anthropic spells it `stop_reason: "max_tokens"` where the OpenAI-compatible providers
    say `finish_reason: "length"`. Both are the same provider-neutral predicate — the reply hit
    the ceiling — which is why `CallResult` carries the predicate and not either spelling
    (NEU-1014)."""
    http_client, _route = mock_client(
        return_value=_message_response(
            [{"type": "text", "text": '[{"id": "a", "fi'}], stop_reason="max_tokens"
        )
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
        )

    assert result.truncated is True
    assert result.ok is True


async def test_a_reply_that_stopped_on_its_own_is_not_truncated():
    http_client, _route = mock_client(
        return_value=_message_response([{"type": "text", "text": "hi"}])
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        result = await c.complete_call(
            model="claude-haiku-4-5", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
        )

    assert result.truncated is False


async def test_a_call_that_never_returned_reports_truncated_as_unknown():
    """None rather than False: no reply arrived, so "was it cut off" has no answer."""
    http_client, _route = mock_client(
        return_value=httpx2.Response(400, json={"error": "bad"})
    )
    calls = CallLog()
    async with AnthropicClient(api_key="test-key", policy=_NO_WAIT, http_client=http_client) as c:
        with pytest.raises(APIStatusError):
            await c.complete_call(
                model="claude-haiku-4-5", prompt=Prompt(stable_prefix="I", user="hi"), calls=calls
            )

    (failed,) = calls.results
    assert failed.ok is False
    assert failed.truncated is None


async def test_an_optional_prefill_is_still_sent_to_a_provider_that_supports_it():
    """`prefill_required=False` says the caller can cope without it — not that it stopped being
    worth having. Anthropic can seed the assistant turn, so it still does: the prefill is what
    stops Haiku narrating its reasoning into a stored summary (`summarizer.py`)."""
    http_client, route = mock_client(
        return_value=_message_response([{"type": "text", "text": 'ok"}'}])
    )
    async with AnthropicClient(api_key="test-key", http_client=http_client) as c:
        await c.complete(
            model="claude-haiku-4-5",
            prompt=Prompt(
                stable_prefix="I", user="hi", prefill='{"summary": "', prefill_required=False
            ),
        )

    messages = json.loads(route.calls.last.request.content)["messages"]
    assert messages[-1] == {"role": "assistant", "content": '{"summary": "'}
