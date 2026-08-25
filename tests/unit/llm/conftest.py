"""Shared fixtures for LLM adapter tests.
Replaces `@respx.mock` with `httpx2.MockTransport` since anthropic 1.0.0
migrated from httpx to httpx2 (2026-08-25), and respx only intercepts httpx.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from httpx2 import AsyncClient, MockTransport, Request, Response


@dataclass
class MockRoute:
    """Tracks calls to a mocked httpx2 endpoint, replacing respx's route API."""

    calls: list[Request] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> Request | None:
        return self.calls[-1] if self.calls else None


def _make_handler(
    route: MockRoute,
    responses: list[Response] | None = None,
    side_effect: list[Response | Exception] | None = None,
) -> Callable[[Request], Response]:
    """Build a MockTransport handler that records calls and returns canned responses."""

    def handler(request: Request) -> Response:
        route.calls.append(request)
        if side_effect is not None:
            item = side_effect.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if responses is not None:
            return responses[0]
        return Response(200)

    return handler


def mock_client(
    *,
    return_value: Response | None = None,
    side_effect: list[Response | Exception] | None = None,
    base_url: str = "https://api.anthropic.com",
) -> tuple[AsyncClient, MockRoute]:
    """Create an httpx2.AsyncClient pre-configured with a MockTransport.
    Returns (client, route) so tests can inspect call counts and request bodies.
    """
    route = MockRoute()
    responses = [return_value] if return_value is not None else None
    transport = MockTransport(handler=_make_handler(route, responses=responses, side_effect=side_effect))
    client = AsyncClient(transport=transport, base_url=base_url)
    return client, route
