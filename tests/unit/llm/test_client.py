import json

import httpx
import respx

from upmovies.llm.client import AnthropicClient, cached_system_block

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _message_response(blocks: list[dict[str, str]]) -> httpx.Response:
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
            "usage": {"input_tokens": 10, "output_tokens": 3},
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
