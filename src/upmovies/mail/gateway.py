"""Which provider sends the mail, template rendering in front of it, and one lifecycle over
the client that does it.

The `llm/gateway.py` shape, at the size this problem actually is (D-30, ADR-0007). Two things
carry over because they are the reasons that module exists rather than a settings lookup at
each call site:

* **The transport is built on first use and pooled.** A process that never sends mail — the
  daily ingest chain, today every process — opens no connection pool and needs no credential
  at the moment of construction.
* **`validate_mail_configuration` runs at startup, not at the first send.** A missing
  `RESEND_API_KEY` discovered by sending is discovered halfway through a signup, after the
  user row is committed and before the verification mail exists, which is the one state the
  account flow cannot recover from by retrying. Discovered at boot it is a container that
  does not start (spec §7).

What does *not* carry over is per-stage routing: there is one provider for all mail, because
unlike an LLM stage there is no axis along which one mail would want a different one. The
template name is the axis that varies, and it varies inside one provider."""

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from upmovies.config import DEFAULT_API_BASE_URL, Settings
from upmovies.llm.retry import RetryPolicy
from upmovies.mail import templates
from upmovies.mail.noop import NoopTransport
from upmovies.mail.registry import MAIL_PROVIDERS, NOOP, RESEND, TRANSMITTING_PROVIDERS
from upmovies.mail.resend import DEFAULT_MAIL_RETRY_POLICY, ResendClient
from upmovies.mail.types import Envelope, MessageId, Transport


class MissingCredentialError(RuntimeError):
    """The configured mail provider's API key is unset.

    Named and raised like its `llm.gateway` counterpart, and for the same reason it is not
    resolved to something else: there is no fallback provider. Falling back to `noop` would
    turn a credential typo into mail that is never sent and never reported — the failure mode
    this package's boot check exists to make impossible."""


class MailConfigurationError(RuntimeError):
    """The mail configuration cannot serve a send: no credential, no sender address, or a
    template tree that is not there.

    Raised at startup so a misconfiguration fails the container rather than the signup (spec
    §7) — the same class of guard as `TMDB_API_KEY` being required for the app to boot, and
    the same one `StageConfigurationError` is for LLM routing."""


def credential_for(settings: Settings, provider: str) -> str | None:
    """The configured API key for `provider`, or None when it is unset **or empty**, or when
    the provider needs none.

    Empty counts as unset for the same reason it does in `llm.gateway.credential_for`: the
    compose file passes `RESEND_API_KEY: "${RESEND_API_KEY:-}"`, so an unconfigured credential
    arrives as `""` rather than as an absent variable.

    Raises `KeyError` for a provider with no entry — a provider nobody has written a line for
    is a mistake worth crashing on, not one worth guessing a credential for."""
    keys: dict[str, str | None] = {
        RESEND: settings.resend_api_key,
        # Not "unset": `noop` transmits nothing, so it has no credential to be missing. The
        # distinction matters to `Gateway._build`, which refuses to build a transport whose
        # credential is None — `noop` is never asked.
        NOOP: None,
    }
    return keys[provider] or None


def validate_mail_configuration(settings: Settings) -> None:
    """Assert the configured provider can actually send: a known provider, a credential, a
    deliverable sender and a public API origin where the provider needs them, and a complete
    template tree.

    Every fault is collected and reported together rather than raised at the first one — a
    deploy that turns mail on for the first time gets its missing key *and* its missing
    `MAIL_FROM` from one failed boot instead of from two.

    The template check is here rather than left to the first render because it catches a
    different class of fault from the other two: the templates are data files inside a Python
    package, referenced by nothing in the import graph, so a build that drops them imports and
    starts perfectly (`templates.validate_templates`)."""
    provider = settings.mail_provider
    problems: list[str] = []
    if provider not in MAIL_PROVIDERS:
        # Unreachable through `Settings`, whose `MailProvider` Literal rejects it first. Kept
        # for the settings object assembled outside an entrypoint — a script, or a test — the
        # same backstop `llm.gateway._build`'s credential guard is.
        problems.append(
            f"MAIL_PROVIDER is {provider!r}: expected one of {', '.join(MAIL_PROVIDERS)}"
        )
    elif provider in TRANSMITTING_PROVIDERS:
        if credential_for(settings, provider) is None:
            problems.append(
                f"MAIL_PROVIDER is {provider!r} but {provider.upper()}_API_KEY is unset"
            )
        # "@" rather than a full address parse: the job is to catch `MAIL_FROM=Backlotter`,
        # which Resend rejects at send time with a 422 nobody sees until a user signs up.
        # Anything stricter would start rejecting the `Name <addr>` form the setting is
        # documented to accept.
        if "@" not in settings.mail_from:
            problems.append(
                f"MAIL_PROVIDER is {provider!r} but MAIL_FROM is {settings.mail_from!r}, "
                f"which is not an email address"
            )
        # The digest's `List-Unsubscribe` link is built on it (DC-10). The default is the dev
        # API, so a deploy that forgot the variable would mail every reader an unsubscribe
        # link to localhost — which a mailbox provider's one-click POST cannot reach.
        if settings.api_base_url == DEFAULT_API_BASE_URL:
            problems.append(
                f"MAIL_PROVIDER is {provider!r} but API_BASE_URL is the default "
                f"{DEFAULT_API_BASE_URL!r}: set it to the API's public origin"
            )
    problems.extend(templates.validate_templates())
    if problems:
        raise MailConfigurationError("mail configuration is unusable:\n  " + "\n  ".join(problems))


class MailGateway:
    """Renders a template and sends it through the configured provider.

        async with MailGateway(settings) as mail:
            await mail.send(to=user.email, template="verify", context={"verify_url": url})

    Implements `types.Mailer`; callers depend on that Protocol rather than on this class, so a
    route under test can be handed a stub without `Settings` coming with it."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        policy: RetryPolicy = DEFAULT_MAIL_RETRY_POLICY,
    ):
        self._settings = settings
        self._policy = policy
        self._provider = settings.mail_provider
        # An explicit transport wins over the configured provider, and is the seam the
        # integration tests and a local `NoopTransport` both come in through. It is *not*
        # entered on the stack: whoever constructed it owns closing it, because the common
        # case is a transport that outlives this gateway (a test asserting on `sent` after
        # the `async with` block has exited).
        self._transport = transport
        self._stack = AsyncExitStack()
        self._closed = False

    async def __aenter__(self) -> "MailGateway":
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Flagged, not just unwound: without it a `send` after exit would build a fresh
        # transport onto an already-unwound stack — one nothing would ever close.
        self._closed = True
        await self._stack.aclose()

    @property
    def provider(self) -> str:
        """Which provider this gateway sends through. What a caller records alongside a
        `MessageId`, which is only interpretable against the provider that issued it."""
        return self._provider

    async def send(self, *, to: str, template: str, context: Mapping[str, Any]) -> MessageId:
        """Render `template` against `context` and send it to `to`, returning the provider's id.

        Rendering happens before the transport is touched, so a template fault costs no
        connection and no provider call — and, more to the point, cannot half-send."""
        self._check_open()
        envelope = templates.render(template, dict(context), sender=self._settings.mail_from, to=to)
        return await self._resolve().send(envelope)

    async def deliver(self, envelope: Envelope) -> MessageId:
        """Send an already-rendered `envelope` as it stands, returning the provider's id.

        No render: the caller built the `Envelope`, and it is exactly what the transport gets."""
        self._check_open()
        return await self._resolve().send(envelope)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("this MailGateway is closed: its transport has been released")

    def _resolve(self) -> Transport:
        if self._transport is None:
            self._transport = self._build()
        return self._transport

    def _build(self) -> Transport:
        if self._provider == NOOP:
            return NoopTransport()
        if self._provider != RESEND:
            # The same discipline `credential_for` applies one line down, stated rather than
            # relied on: today an unknown provider raises `KeyError` there, but only because
            # its map happens to have no entry for it. A third provider added to the registry
            # and to that map would otherwise fall through to a Resend client — silently
            # sending someone else's mail through Resend is a worse failure than not booting.
            raise MailConfigurationError(
                f"no transport is implemented for MAIL_PROVIDER {self._provider!r}"
            )
        api_key = credential_for(self._settings, self._provider)
        if api_key is None:
            raise MissingCredentialError(
                f"MAIL_PROVIDER is {self._provider!r} but {self._provider.upper()}_API_KEY is unset"
            )
        client = ResendClient(api_key=api_key, policy=self._policy)
        # Registered as a callback rather than entered as a context manager because the
        # transport is built lazily, from a synchronous call. The stack still closes it on
        # exit. Only transports this gateway built ever reach here — an injected one short-
        # circuits in `_resolve` — so nothing registered belongs to somebody else.
        self._stack.push_async_callback(client.aclose)
        return client
