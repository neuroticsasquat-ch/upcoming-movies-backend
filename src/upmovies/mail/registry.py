"""Which mail providers exist, and where the ones that speak HTTP live.

Mirrors `llm/registry.py`, including why the base URL is a constant and not a setting: it is a
property of the provider, not of a deployment, and the tests reach the adapter by mocking the
transport rather than by pointing it at a different host. A configurable value whose only
correct answer is the one written below is an env var to get wrong, not a lever.

The provider strings are load-bearing beyond this module — `config.MailProvider` is a
`Literal` over the same names, and a test pins the two together — so they are named constants
rather than literals scattered per call site."""

RESEND = "resend"
NOOP = "noop"

MAIL_PROVIDERS: tuple[str, ...] = (RESEND, NOOP)

# Providers that actually put a message on the wire, and therefore need a credential and a
# deliverable `MAIL_FROM`. `noop` is deliberately absent: requiring either of it would make
# a local checkout fail to boot for a capability it is explicitly not using.
TRANSMITTING_PROVIDERS: frozenset[str] = frozenset({RESEND})

RESEND_BASE_URL = "https://api.resend.com"
