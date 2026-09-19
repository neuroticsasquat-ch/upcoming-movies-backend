"""What a push send needs, what it may raise, and the Protocol a sender depends on.

Mirrors `mail/types.py`: the send pass depends on `Pusher`, not on `WebPushGateway`, so a test
can hand it a recorder without `Settings` and a VAPID keypair coming along for the ride."""

from dataclasses import dataclass
from typing import Protocol


class PushError(Exception):
    """The push service refused this message, or could not be reached.

    Transient by assumption: the row is marked `failed` with this message, and the sender's
    abort guard counts it, because a push service refusing one send is evidence about the next
    one."""


class PushSubscriptionGone(PushError):
    """The push service answered 404 or 410: this endpoint will never accept another message.

    A subclass rather than a flag, because it is the one refusal with a *side effect* — the
    subscription row is deleted (`push_sender`). 404 and 410 are the two RFC 8030 spellings of
    "this endpoint is not a thing any more" (the browser cleared its site data, the service
    expired the registration), and no amount of retrying changes either."""


class PushConfigurationError(Exception):
    """`VAPID_*` cannot sign a send. Raised at boot, not at send time — see `gateway`."""


@dataclass(frozen=True)
class PushSubscriptionInfo:
    """One browser's subscription, in the shape the Push API defines and the browser hands to
    the client verbatim (`PushSubscription.toJSON()`).

    Kept as three fields rather than the nested dict because that is how the row is stored, and
    rebuilt into the nested form at the one place that needs it — the library call."""

    endpoint: str
    p256dh: str
    auth: str

    def as_subscription_info(self) -> dict[str, object]:
        """The `subscription_info` mapping `pywebpush` expects."""
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class Pusher(Protocol):
    """Sends one encrypted payload to one subscription.

    No batching in the interface, unlike `Mailer`: Web Push has no notion of several
    recipients on one message — each subscription is a separate encrypted body, because each
    is encrypted to its own key."""

    async def send(self, *, subscription: PushSubscriptionInfo, payload: str) -> None: ...
