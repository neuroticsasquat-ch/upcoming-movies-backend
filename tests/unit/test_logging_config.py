"""The redacting log filter (NEU-1124).

The exception messages `TMDBClient` raises are scrubbed at the raise site, but that only
covers the errors *we* format. The filter is what covers everything else — above all
`httpx`, which logs the full request URL at INFO on every single call, and so wrote the TMDB
key to stdout and Sentry roughly nine thousand times per sweep.
"""

import logging

from upmovies.logging_config import RedactingFilter, configure_logging, redact_api_key

URL = "https://api.themoviedb.org/3/movie/1?api_key=s3cret&page=2"


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
