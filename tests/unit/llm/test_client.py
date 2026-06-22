import json
import types

import httpx
import pytest
import respx

from upmovies.llm.client import (
    AnthropicClient,
    BatchRequest,
    Usage,
    cached_system_block,
)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"
BATCH_ID = "msgbatch_1"
RETRIEVE_URL = f"{BATCHES_URL}/{BATCH_ID}"
RESULTS_URL = f"{BATCHES_URL}/{BATCH_ID}/results"


def _batch(status: str, results_url: str | None = None) -> dict:
    return {
        "id": BATCH_ID,
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0,
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "ended_at": None,
        "created_at": "2026-06-21T00:00:00Z",
        "expires_at": "2026-06-22T00:00:00Z",
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": results_url,
    }


def _succeeded_line(custom_id: str, text: str, usage: dict | None = None) -> dict:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": usage or {"input_tokens": 1, "output_tokens": 1},
            },
        },
    }


def _errored_line(custom_id: str, etype: str, message: str) -> dict:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "errored",
            "error": {"type": "error", "error": {"type": etype, "message": message}},
        },
    }


def _jsonl(*lines: dict) -> str:
    return "\n".join(json.dumps(line) for line in lines)


def _req(custom_id: str) -> BatchRequest:
    return BatchRequest(
        custom_id=custom_id,
        model="claude-haiku-4-5",
        system=[cached_system_block("ROSTER")],
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=16,
    )


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


@respx.mock
async def test_complete_batch_empty_requests_returns_empty_and_makes_no_calls():
    create_route = respx.post("https://api.anthropic.com/v1/messages/batches")
    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete_batch([])
    assert out == {}
    assert not create_route.called


@respx.mock
async def test_complete_batch_polls_to_ended_and_collects_succeeded():
    respx.post(BATCHES_URL).mock(return_value=httpx.Response(200, json=_batch("in_progress")))
    respx.get(RETRIEVE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_batch("in_progress")),
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
            # Anthropic SDK implementation detail: .results() calls .retrieve() once
            # internally to refresh results_url before fetching the JSONL stream.
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
        ]
    )
    respx.get(RESULTS_URL).mock(
        return_value=httpx.Response(
            200,
            text=_jsonl(
                _succeeded_line("req-0", "alpha"),
                _succeeded_line("req-1", "beta"),
            ),
            headers={"content-type": "application/x-jsonl"},
        )
    )

    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete_batch([_req("req-0"), _req("req-1")], poll_interval=0)

    assert set(out) == {"req-0", "req-1"}
    assert out["req-0"].custom_id == "req-0" and out["req-0"].ok and out["req-0"].text == "alpha"
    assert out["req-1"].custom_id == "req-1" and out["req-1"].ok and out["req-1"].text == "beta"


@respx.mock
async def test_complete_batch_surfaces_errored_and_missing_results():
    respx.post(BATCHES_URL).mock(return_value=httpx.Response(200, json=_batch("in_progress")))
    respx.get(RETRIEVE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
            # Anthropic SDK implementation detail: .results() calls .retrieve() once
            # internally to refresh results_url before fetching the JSONL stream.
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
        ]
    )
    # Stream returns a success for req-0 and an error for req-1; req-2 is omitted entirely.
    respx.get(RESULTS_URL).mock(
        return_value=httpx.Response(
            200,
            text=_jsonl(
                _succeeded_line("req-0", "ok-text"),
                _errored_line("req-1", "invalid_request_error", "boom"),
            ),
            headers={"content-type": "application/x-jsonl"},
        )
    )

    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete_batch([_req("req-0"), _req("req-1"), _req("req-2")], poll_interval=0)

    assert out["req-0"].ok is True
    assert out["req-0"].text == "ok-text"

    assert out["req-1"].ok is False
    assert out["req-1"].error_type == "invalid_request_error"
    assert out["req-1"].error_message == "boom"

    # Omitted from the stream → surfaced as a "missing" error, not dropped.
    assert out["req-2"].ok is False
    assert out["req-2"].error_type == "missing"


@respx.mock
async def test_complete_batch_preserves_cache_control_in_request_params():
    create_route = respx.post(BATCHES_URL).mock(
        return_value=httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL))
    )
    # Anthropic SDK implementation detail: .results() calls .retrieve() once
    # internally to refresh results_url before fetching the JSONL stream.
    respx.get(RETRIEVE_URL).mock(
        return_value=httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL))
    )
    respx.get(RESULTS_URL).mock(
        return_value=httpx.Response(
            200,
            text=_jsonl(_succeeded_line("req-0", "x")),
            headers={"content-type": "application/x-jsonl"},
        )
    )

    async with AnthropicClient(api_key="test-key") as c:
        await c.complete_batch([_req("req-0")], poll_interval=0)

    body = json.loads(create_route.calls.last.request.content)
    params = body["requests"][0]["params"]
    assert params["model"] == "claude-haiku-4-5"
    assert params["max_tokens"] == 16
    assert params["system"][0]["text"] == "ROSTER"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"] == [{"role": "user", "content": "hi"}]


@respx.mock
async def test_complete_batch_raises_timeout_when_never_ends():
    respx.post(BATCHES_URL).mock(return_value=httpx.Response(200, json=_batch("in_progress")))
    # Retrieve would keep returning in_progress, but timeout=0 trips the deadline first.
    respx.get(RETRIEVE_URL).mock(return_value=httpx.Response(200, json=_batch("in_progress")))

    async with AnthropicClient(api_key="test-key") as c:
        with pytest.raises(TimeoutError):
            await c.complete_batch([_req("req-0")], poll_interval=0, timeout=0)


@respx.mock
async def test_complete_batch_populates_usage_on_success():
    respx.post(BATCHES_URL).mock(return_value=httpx.Response(200, json=_batch("in_progress")))
    respx.get(RETRIEVE_URL).mock(
        side_effect=[
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
            httpx.Response(200, json=_batch("ended", results_url=RESULTS_URL)),
        ]
    )
    respx.get(RESULTS_URL).mock(
        return_value=httpx.Response(
            200,
            text=_jsonl(
                _succeeded_line(
                    "req-0",
                    "alpha",
                    usage={
                        "input_tokens": 7,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 50,
                    },
                ),
                _errored_line("req-1", "invalid_request_error", "boom"),
            ),
            headers={"content-type": "application/x-jsonl"},
        )
    )

    async with AnthropicClient(api_key="test-key") as c:
        out = await c.complete_batch([_req("req-0"), _req("req-1")], poll_interval=0)

    assert out["req-0"].usage == Usage(
        input_tokens=7, output_tokens=2, cache_read_input_tokens=500, cache_creation_input_tokens=50
    )
    # Errored entry has no usage.
    assert out["req-1"].usage is None


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
