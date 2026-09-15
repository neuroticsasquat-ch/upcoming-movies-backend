"""Resend adapter: one `Transport` over the provider's `POST /emails` endpoint.

A thin first-party client over the `httpx` already in the dependency list and already OTel-
instrumented, rather than the vendor's SDK — the same call ADR-0007 makes for the LLM
adapters, and for the same reason. The surface actually used is one POST with five fields;
what an SDK would add here is a second retry loop and a second set of exception types for the
caller to learn.

The retry policy is imported from `llm.retry` rather than reimplemented. That module is
already the repo's *single* statement of how an outbound HTTP call retries — deliberately free
of vendor and DB imports, and its own docstring is an argument against adapters whose retry
behaviour is merely similar rather than shared. It lives under `llm/` because that is where
the second adapter needing it appeared, not because it is about language models; the day a
third consumer shows up it is worth hoisting to a package of its own, and until then a move
would be churn across two packages for no behaviour change."""

from typing import Any

import httpx

from upmovies.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Attempts,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_for_status,
)
from upmovies.mail.registry import RESEND_BASE_URL
from upmovies.mail.types import Envelope, MailError, MessageId


def _to_wire(envelope: Envelope) -> dict[str, Any]:
    """The Resend `POST /emails` body for one envelope.

    `to` is a list because the API takes one, not because we batch: a send is to one
    recipient, and keeping it that way is what makes a delivery failure attributable to a
    user. Both bodies are always sent — Resend assembles the `multipart/alternative` itself
    — so a client that cannot render HTML still gets the copy that matters."""
    return {
        "from": envelope.sender,
        "to": [envelope.to],
        "subject": envelope.subject,
        "text": envelope.text,
        "html": envelope.html,
    }


def _classify(exc: BaseException) -> Retry | None:
    """Which httpx failures are worth another attempt.

    The status verdict is deferred to `retry_for_status` so this is literally the same
    judgement the two LLM adapters make — 408/409/429 and 5xx, honouring `Retry-After`, which
    matters here because Resend rate-limits at 2 requests/second and says so in that header.

    A 4xx that is not in that set is the provider telling us the message is wrong (an
    unverified sending domain, a malformed `from`). Asking again buys the same refusal."""
    if isinstance(exc, httpx.HTTPStatusError):
        return retry_for_status(exc.response.status_code, exc.response.headers)
    if isinstance(exc, httpx.TransportError):
        # Connect errors, read errors and timeouts: the request never got an answer, so
        # nothing is known about whether it would fail again.
        return Retry()
    # Anything else came from a response that did arrive — a body that would not decode, most
    # likely. Asking again buys the same broken body.
    return None


class ResendClient:
    """Async client over Resend's `/emails` endpoint.

    Built with its credential rather than reading `Settings`, so it is constructible in a test
    without the application's configuration — `MailGateway` is the piece that knows how to get
    one from the other."""

    def __init__(
        self,
        *,
        api_key: str,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        base_url: str = RESEND_BASE_URL,
    ):
        self._policy = policy
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=policy.timeout,
        )

    async def __aenter__(self) -> "ResendClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying connection pool. Separate from `__aexit__` because
        `MailGateway` builds its transport lazily and so cannot enter it as a context manager
        — it closes it by name instead."""
        await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/emails", json=body)
        response.raise_for_status()
        return response.json()

    async def send(self, envelope: Envelope) -> MessageId:
        """Hand one envelope to Resend and return the id it assigns.

        A 2xx with no `id` is raised on rather than passed through as `""`. An empty id looks
        like a successful send everywhere it is later stored, and the row it would write —
        D-31's notification ledger — is the only record that the mail was ever accepted; a
        falsified one is worse than a failure, because a failure gets retried and a lie does
        not."""
        body = _to_wire(envelope)
        payload = await call_with_retry(
            lambda: self._post(body),
            policy=self._policy,
            classify=_classify,
            attempts=Attempts(),
        )
        message_id = payload.get("id")
        if not message_id:
            raise MailError(
                f"Resend accepted the message for {envelope.to} but returned no id: {payload!r}"
            )
        return MessageId(str(message_id))
