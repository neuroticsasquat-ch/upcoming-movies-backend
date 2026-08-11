"""Logging setup shared by the app and the scheduled-task entrypoint, and the credential
redaction that goes with it.

Both entrypoints configure the root logger identically, so the configuration lives here once
rather than being restated in two `basicConfig` calls that can drift — and, more to the point,
so the redacting filter cannot be installed on one of them and forgotten on the other.

Depends on nothing else in the package, which is what lets `ingest.tmdb.client` reuse
`redact_api_key` for its own exception messages without an import cycle.
"""

import logging
import re

_API_KEY_PARAM = re.compile(r"(api_key=)[^&\s'\"]+")


def redact_api_key(text: str) -> str:
    """Replace the value of any `api_key` query param in `text` with `REDACTED`.

    TMDB v3 auth puts the key in the query string, so every URL built against it carries a
    live credential (NEU-1124).
    """
    return _API_KEY_PARAM.sub(r"\1REDACTED", text)


class RedactingFilter(logging.Filter):
    """Scrubs credentials out of every log record, whoever emitted it.

    A filter rather than a rule about how *we* log, because the loudest leak was never our
    code: `httpx` logs `HTTP Request: GET <full url>` at INFO on every request, and
    `LOG_LEVEL` defaults to INFO — so a single sweep wrote the TMDB key to stdout, and to
    Sentry, some nine thousand times. Redacting at the handler covers that, the library that
    does the same thing next year, and the log lines nobody has written yet.

    Rewrites `record.msg` and drops `record.args` because the credential can be in either;
    collapsing them into the final message is the only way to scrub both with one pass.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_api_key(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str) -> None:
    """Configure the root logger and install `RedactingFilter` on its handlers."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    # On the handlers, not on the root logger: a filter attached to a logger is not consulted
    # for records propagating up from its children, so `httpx`'s own logger would sail past it.
    redactor = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
