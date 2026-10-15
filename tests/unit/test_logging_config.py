"""The redacting log filter (NEU-1124, NEU-1616).

The exception messages `TMDBClient` raises are scrubbed at the raise site, but that only
covers the errors *we* format. The filter is what covers everything else — above all
`httpx`, which logs the full request URL at INFO on every single call, and so wrote the TMDB
key to stdout and Sentry roughly nine thousand times per sweep.
"""

import logging

from upmovies.logging_config import (
    RedactingFilter,
    configure_logging,
    redact_api_key,
    redact_push_token,
)

URL = "https://api.themoviedb.org/3/movie/1?api_key=s3cret&page=2"
PUSH_URL = "http://100.119.16.113:3001/api/push/abc123?status=up&msg=OK&ping="


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def test_redact_api_key_replaces_only_the_value():
    assert redact_api_key(URL) == "https://api.themoviedb.org/3/movie/1?api_key=REDACTED&page=2"


def test_redact_api_key_leaves_text_without_a_key_alone():
    assert redact_api_key("no credentials here") == "no credentials here"


def test_filter_scrubs_the_message():
    record = _record(f"HTTP Request: GET {URL}")
    RedactingFilter().filter(record)
    assert "s3cret" not in record.getMessage()
    assert "api_key=REDACTED" in record.getMessage()


def test_filter_scrubs_a_key_that_arrives_through_args():
    """httpx interpolates the URL as an argument, so scrubbing `record.msg` alone would miss
    the case that actually leaks."""
    record = _record("HTTP Request: GET %s", URL)
    RedactingFilter().filter(record)
    assert "s3cret" not in record.getMessage()


def test_redact_push_token_replaces_only_the_token():
    assert redact_push_token(PUSH_URL) == (
        "http://100.119.16.113:3001/api/push/REDACTED?status=up&msg=OK&ping="
    )


def test_redact_push_token_leaves_text_without_a_push_path_alone():
    assert redact_push_token("no credentials here") == "no credentials here"


def test_filter_scrubs_a_push_token_in_an_httpx_shaped_record():
    """The Uptime Kuma push token is the credential in the path (NEU-1616): whoever holds it
    can push `status=up` and hide a failed job. The record is shaped exactly as httpx logs it."""
    record = _record('HTTP Request: %s %s "%s %d %s"', "GET", PUSH_URL, "HTTP/1.1", 200, "OK")
    RedactingFilter().filter(record)
    assert "abc123" not in record.getMessage()
    assert "/api/push/REDACTED?status=up" in record.getMessage()


def test_filter_leaves_an_unrelated_record_untouched():
    record = _record("refresh: %d films", 12)
    RedactingFilter().filter(record)
    assert record.getMessage() == "refresh: 12 films"


def test_filter_keeps_the_record(caplog):
    """It redacts; it must never drop a log line."""
    assert RedactingFilter().filter(_record("anything")) is True


def test_configure_logging_installs_the_filter_on_the_root_handlers():
    configure_logging("INFO")
    handlers = logging.getLogger().handlers
    assert handlers
    assert all(any(isinstance(f, RedactingFilter) for f in h.filters) for h in handlers), (
        "a handler without the filter is a handler that leaks"
    )
