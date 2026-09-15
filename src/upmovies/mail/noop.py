"""The provider that sends nothing and remembers everything.

Two audiences, one implementation. **Tests** want to assert that a route sent the right
template to the right address without a mocked transport at every call site, so every
`Envelope` is kept on `sent`. **Local dev** wants a checkout that boots and signs up users
with no Resend account, so a send is a log line rather than a failure.

It is the default `MAIL_PROVIDER` (see `config`), which is a deliberate trade: the safe
failure for a deploy that forgets to set the variable is "mail is not sent and says so
loudly in the logs", not "the container will not start". Every send is logged at INFO with
its recipient and template precisely so that a production container running on the default
is visible in the log stream rather than silently swallowing verification mails."""

import logging
import uuid

from upmovies.mail.types import Envelope, MessageId

logger = logging.getLogger(__name__)


class NoopTransport:
    """Records each `Envelope` and returns a synthetic `MessageId`.

    The id is a fresh UUID with a `noop-` prefix rather than a constant, because callers
    store it (D-31's notification rows) and a constant would make every stored row collide —
    which would make a test that asserts "two distinct sends happened" pass on one send."""

    def __init__(self) -> None:
        self.sent: list[Envelope] = []

    async def send(self, envelope: Envelope) -> MessageId:
        self.sent.append(envelope)
        logger.info(
            "mail.noop.send to=%s subject=%s (MAIL_PROVIDER=noop: nothing was transmitted)",
            envelope.to,
            envelope.subject,
        )
        return MessageId(f"noop-{uuid.uuid4()}")

    async def aclose(self) -> None:
        """Nothing to release. Present because `Transport` requires it, so `MailGateway` can
        close whatever it built without asking which provider it is holding."""
