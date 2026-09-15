"""A mailbox on `app.state` for every test that reaches a route.

Autouse, because the alternative is a per-test override on a route that most tests reach only
incidentally: `POST /auth/signup` sends a verification mail (NEU-1339), so every signup test
in the suite would otherwise get `deps.get_mailer`'s `mail_unavailable` 500 — the lifespan
that normally builds the gateway does not run under `ASGITransport`.

Yields the `NoopTransport`, so a test that cares asserts on `mailbox.sent`; one that does not
gets a mailer that records and transmits nothing."""

from collections.abc import Iterator

import pytest

from upmovies.config import get_settings
from upmovies.mail import MailGateway, NoopTransport
from upmovies.main import app


@pytest.fixture(autouse=True)
def mailbox() -> Iterator[NoopTransport]:
    transport = NoopTransport()
    # The gateway is handed its transport, so the configured provider is never consulted and
    # no credential is needed — the same seam `tests/integration/test_mail.py` comes in
    # through, and why this does not have to care what `MAIL_PROVIDER` is in the environment.
    app.state.mailer = MailGateway(get_settings(), transport=transport)
    yield transport
    app.state.mailer = None
