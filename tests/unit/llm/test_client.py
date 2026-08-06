import json
import types

import httpx
import respx

from upmovies.llm.client import (
    AnthropicClient,
    Usage,
    cached_system_block,
)

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
async def test_complete_returns_text_and_sends_cache_control():
    route = respx.post(MESSAGES_URL).mock(
        return_value=_message_response([{"type": "text", "text": "hello"}])
    )
    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete(
            model="claude-haiku-4-5",
            system=[cached_system_block("ROSTER")],
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
        )
    assert out == "hello"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "claude-haiku-4-5"
    assert body["system"][0]["text"] == "ROSTER"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


@respx.mock
async def test_complete_concatenates_text_blocks():
    respx.post(MESSAGES_URL).mock(
        return_value=_message_response(
            [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
        )
    )
    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete(
            model="claude-haiku-4-5",
            system=[cached_system_block("X")],
            messages=[{"role": "user", "content": "hi"}],
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
            system=[cached_system_block("ROSTER")],
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
        )
    assert text == "hi there"
    assert usage == Usage(
        input_tokens=12,
        output_tokens=4,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=100,
    )


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
