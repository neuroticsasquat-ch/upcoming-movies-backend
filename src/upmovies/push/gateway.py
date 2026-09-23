"""Signing and sending one Web Push message, and refusing a boot that could not sign any.

**The library is synchronous, so the send is not.** `pywebpush` encrypts the payload to the
subscription's key (RFC 8291) and signs the request with VAPID (RFC 8292), then puts it on the
wire with `requests`. The encryption is the part worth having and the transport is the part
that does not fit — so the call goes to a worker thread rather than onto the event loop, and
the alternative, re-implementing the encryption against `httpx`, would be a cryptographic
rewrite bought for an I/O preference.

**Configuration is checked where a send can actually be made, and only once a subscription
exists** (`validate_push_configuration`). `VAPID_*` is empty by default like the mail
credential, because a deployment that is not doing push must still boot — but the moment a
browser has registered, an unconfigured process is one that will queue notifications and send
nothing, and silence is the failure nobody reports. So the existence of a row is what turns the
setting from optional into required.

The caller that asks is `pipeline_run.run_notify_stage`, before the push half of the notify
slot, and **not** the API's lifespan: the API never sends a push, so refusing its boot over the
keypair would trade the website for a setting it does not read — the same call
`validate_sweep_configuration` makes. What the API does instead is refuse to *take* a
subscription it could not push to (`routers.push.require_push_configured`), so the two ends
agree without either of them killing a process that is serving pages.
"""

import asyncio
import logging

from pywebpush import WebPushException, webpush

from upmovies.config import Settings
from upmovies.push.types import (
    PushConfigurationError,
    PushError,
    PushSubscriptionGone,
    PushSubscriptionInfo,
)

log = logging.getLogger(__name__)

PUSH_TTL_SECONDS = 24 * 60 * 60
"""How long a push service may hold a message for a device that is offline.

A day, rather than the library's default of 0 ("deliver now or drop"). Every beat on D-32's
whitelist is news that is still news tomorrow morning — a date moved, a film landed on a
service, a trailer went up — so a phone that was off overnight should hear about it when it
wakes, not never. Not longer than a day because past that the alert is competing with the
digest that also carries it."""

GONE_STATUSES = frozenset({404, 410})
"""The two statuses that mean the endpoint is permanently gone (RFC 8030 §7.3). Anything else
— 400, 429, 500, a timeout — is this send failing, not this subscription ending."""


def push_configuration_problems(settings: Settings) -> list[str]:
    """Everything missing from the VAPID configuration, as a list of sentences.

    Collected rather than raised one at a time, like `mail.gateway.validate_mail_configuration`:
    a deploy turning push on for the first time gets all three missing variables from one failed
    boot instead of from three."""
    problems: list[str] = []
    if not settings.vapid_public_key:
        problems.append("VAPID_PUBLIC_KEY is unset")
    if not settings.vapid_private_key:
        problems.append("VAPID_PRIVATE_KEY is unset")
    # The claim's `sub`, which every push service reads and some reject the request without.
    # Checked for its scheme rather than parsed: the job is to catch `VAPID_SUBJECT=ops@x.com`,
    # a value that looks right and is refused on the wire.
    if not settings.vapid_subject:
        problems.append("VAPID_SUBJECT is unset")
    elif not settings.vapid_subject.startswith(("mailto:", "https:")):
        problems.append(
            f"VAPID_SUBJECT is {settings.vapid_subject!r}, which is not a mailto: or https: URL"
        )
    return problems


def validate_push_configuration(settings: Settings) -> None:
    """Assert this process can sign a push. Raises `PushConfigurationError` listing every fault.

    The caller decides *when* to ask — `main.lifespan` and `pipeline_run` both ask only when a
    `push_subscription` row exists, which is the condition that makes the keys load-bearing."""
    problems = push_configuration_problems(settings)
    if problems:
        raise PushConfigurationError(
            "push subscriptions exist but the VAPID configuration is unusable:\n  "
            + "\n  ".join(problems)
        )


class WebPushGateway:
    """Sends one encrypted, VAPID-signed message per subscription.

    Implements `types.Pusher`; callers depend on that Protocol. Stateless — unlike
    `MailGateway` there is no pooled transport to own, because `pywebpush` builds its own
    `requests` session per call — so it is constructed where it is used rather than hung off
    `app.state`."""

    def __init__(self, settings: Settings):
        self._private_key = settings.vapid_private_key
        self._claims = {"sub": settings.vapid_subject}

    async def send(self, *, subscription: PushSubscriptionInfo, payload: str) -> None:
        """Encrypt `payload` to this subscription and hand it to its push service.

        `vapid_claims` is copied per send because `pywebpush` *mutates* what it is given — it
        fills in `aud` from the endpoint and `exp` from the clock — and a claims dict shared
        across sends would carry the first endpoint's `aud` to the second push service, which
        rejects it. One dict per call is the fix, and it is the kind of thing that fails only
        once a second push service is involved."""
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription.as_subscription_info(),
                data=payload,
                vapid_private_key=self._private_key,
                vapid_claims=dict(self._claims),
                ttl=PUSH_TTL_SECONDS,
            )
        except WebPushException as exc:
            if exc.status_code in GONE_STATUSES:
                raise PushSubscriptionGone(f"{exc.status_code}: the endpoint is gone") from exc
            raise PushError(str(exc)) from exc
        except Exception as exc:
            # Everything `requests` can raise from inside the thread, plus the `ValueError`s
            # `py_vapid` raises on a malformed key. Narrowed to `PushError` here rather than
            # left to propagate, because the send pass's contract is "a refusal is a failed
            # row" — a `requests.ConnectionError` reaching it as itself would abort a batch
            # that a push service having a bad minute should only have cost one row.
            raise PushError(f"{type(exc).__name__}: {exc}") from exc
