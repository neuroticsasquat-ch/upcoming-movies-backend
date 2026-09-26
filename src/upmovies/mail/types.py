"""Provider-neutral mail types: what a rendered message is, and the two surfaces the rest of
the app talks to.

Deliberately free of DB, SQLAlchemy and HTTP-client imports, for the same reason
`llm/types.py` is (design §4): these are the types both the Resend adapter and every future
caller share, so anything vendor-shaped belongs one layer down.

Two Protocols rather than one, because the seam has two sides and they are not the same
question. A **caller** (a route, a digest pass) knows a template name and a context and
nothing about MIME parts or providers — that is `Mailer`. A **transport** knows how to hand
one already-rendered `Envelope` to a provider and get an id back, and knows nothing about
Jinja — that is `Transport`. Collapsing them would put template rendering inside every
adapter, which is how you end up with the Resend adapter and the noop one rendering subjects
slightly differently."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NewType, Protocol

# The provider's own identifier for an accepted message, opaque to us. A `NewType` rather
# than a bare `str` so a caller that stores one cannot quietly store a template name or an
# email address instead — the notification rows in D-31 are where these end up.
MessageId = NewType("MessageId", str)


@dataclass(frozen=True)
class Envelope:
    """One fully rendered message, ready to hand to a provider.

    Both bodies are always present. `text` is the one that must stand alone — it is what a
    plain-text client, a screen reader and most spam filters actually read — and `html` is the
    enhancement, never the only copy. A template that renders an empty `text` is a bug caught
    at render time (`templates.render`) rather than a mail nobody can read.

    `sender` rides on the envelope rather than being read from settings by each transport, so
    that what was sent is fully described by the value the transport was handed. That is what
    lets `NoopTransport` record something a test can assert against without also reaching for
    the configuration that produced it.

    `headers` are extra message headers for the provider to set verbatim — today only the
    digest's `List-Unsubscribe` pair (DC-10). Empty by default: every other mail is
    transactional and owes no header beyond the ones the provider writes itself. They ride on
    the envelope for `sender`'s reason — what the transport was handed is the whole
    description of what was sent."""

    sender: str
    to: str
    subject: str
    text: str
    html: str
    headers: Mapping[str, str] = field(default_factory=dict)


class MailError(RuntimeError):
    """A send could not be completed. Base class for the mail package's own failures.

    Transport-level HTTP failures are *not* wrapped in this: they propagate as `httpx`
    exceptions, exactly as they do out of the LLM adapters, because the retry classifier and
    the caller's own error handling both dispatch on them."""


class UnknownTemplateError(MailError):
    """No template by that name exists under `mail/templates/`.

    A distinct error rather than a Jinja `TemplateNotFound` so a caller can tell "I named a
    template that does not exist" apart from "a template referenced a partial that does not" —
    the first is a typo at the call site, the second is a broken template."""


class TemplateRenderError(MailError):
    """A template could not be rendered against the context it was given.

    Almost always a missing key: the environment uses `StrictUndefined`, so a context that
    spells `verify_url` differently raises instead of sending a mail with a blank link in it.
    Wrapped into this package's own hierarchy rather than left as `jinja2.UndefinedError`,
    because `MailError` is documented as what a caller catches, and the most likely template
    fault escaping that promise would make the promise worthless. The Jinja exception is
    chained, so the offending name is still one `__cause__` away."""


class Mailer(Protocol):
    """What a caller needs in order to send mail: a template name, a recipient, a context — or
    an `Envelope` the caller has already rendered.

    A Protocol for the same reason `StageGateway` is one — routes and services are written
    against the surface, not against `MailGateway`, which reads `Settings` and owns an HTTP
    connection pool. A test substitutes `NoopTransport`-backed gateway, or its own stub,
    without either importing the application's configuration.

    Two ways in, for two kinds of caller. The transactional mails (verify, reset, email
    change) render inside `send`: nothing else ever needs their `Envelope`. The
    digest renders first, through `digest_sender.render_batch`, and hands the result to
    `deliver` — because an admin preview and a test-send need that same `Envelope` without a
    send, and a second render path for them would be a second answer to "what does the mail
    say" (NEU-1460, D-1460.1)."""

    async def send(self, *, to: str, template: str, context: dict[str, Any]) -> MessageId: ...

    async def deliver(self, envelope: Envelope) -> MessageId: ...


class Transport(Protocol):
    """The provider side of the seam: accept one rendered `Envelope`, return the provider's id.

    `aclose` is on the Protocol rather than left to the concrete classes because `MailGateway`
    builds its transport lazily and so cannot enter it as a context manager — it closes it by
    name, the same way `llm.gateway.Gateway` closes its completers."""

    async def send(self, envelope: Envelope) -> MessageId: ...

    async def aclose(self) -> None: ...
