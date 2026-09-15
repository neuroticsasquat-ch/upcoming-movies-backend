"""`deps.get_mailer` — the seam that makes `MailGateway` injectable.

The gateway itself is covered in `tests/unit/mail/`; what is asserted here is the two answers
this dependency can give, and that the failing one says what is wrong."""

import pytest
from fastapi import FastAPI, HTTPException, Request

from upmovies.config import get_settings
from upmovies.deps import get_mailer
from upmovies.mail import MailGateway


def _request_against(app: FastAPI) -> Request:
    """The minimum ASGI scope `get_mailer` reads. A TestClient would work too and would bring
    a lifespan with it, which is the thing these two cases need to control."""
    return Request({"type": "http", "app": app, "headers": []})


def test_it_returns_the_mailer_the_lifespan_put_on_app_state():
    mailer = MailGateway(get_settings().model_copy(update={"mail_provider": "noop"}))
    app = FastAPI()
    app.state.mailer = mailer

    assert get_mailer(_request_against(app)) is mailer


def test_it_names_the_reason_when_the_lifespan_never_ran():
    """A TestClient that bypasses the lifespan gets a 500 that says `mail_unavailable`; the
    fix — run the lifespan, or override this dependency — is not guessable from an
    `AttributeError` on `app.state`."""
    with pytest.raises(HTTPException) as exc:
        get_mailer(_request_against(FastAPI()))

    assert exc.value.status_code == 500
    assert exc.value.detail == "mail_unavailable"
