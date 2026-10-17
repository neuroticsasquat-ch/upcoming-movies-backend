"""Release and environment resolution: the instrumentation contract (NEU-1590)."""

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import StatusCode

from upmovies import telemetry
from upmovies.telemetry import (
    init_sentry,
    parse_resource_attributes,
    record_handled_exception,
    resolve_telemetry,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
PROD_ATTRS = (
    f"service.name=upcoming-movies-backend,deployment.environment=production,service.version={SHA}"
)


def test_parse_resource_attributes_splits_pairs_and_percent_decodes():
    assert parse_resource_attributes("a=1, b = two%2Cparts ,,junk,c=") == {
        "a": "1",
        "b": "two,parts",
        "c": "",
    }


def test_release_and_environment_are_otels_own_resource_attributes():
    t = resolve_telemetry(
        {
            "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS,
            # Both disagree on purpose: the resource attributes are what OTel reported, so they
            # are what Sentry must report too.
            "SOURCE_COMMIT": "someothersha",
            "ENVIRONMENT": "staging",
        }
    )
    assert t.release == t.otel_service_version == SHA
    assert t.environment == t.otel_deployment_environment == "production"


def test_prod_compose_shape_resolves_one_sha_on_both_sides():
    # What docker-compose.prod.yml produces once Coolify interpolates SOURCE_COMMIT.
    t = resolve_telemetry(
        {"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS, "SOURCE_COMMIT": SHA, "GIT_SHA": SHA}
    )
    assert t.release == t.otel_service_version == SHA


def test_an_unknown_service_version_wins_over_a_better_fallback():
    # SigNoz holds "unknown", so Bugsink must too, or the two name different builds.
    t = resolve_telemetry({"OTEL_RESOURCE_ATTRIBUTES": "service.version=unknown", "GIT_SHA": SHA})
    assert t.release == "unknown"


@pytest.mark.parametrize(
    ("env", "release"),
    [
        ({"SOURCE_COMMIT": SHA, "GIT_SHA": "older"}, SHA),
        ({"GIT_SHA": SHA}, SHA),
        # Coolify's placeholder when "Include SOURCE_COMMIT in build" is off.
        ({"SOURCE_COMMIT": "unknown", "GIT_SHA": SHA}, SHA),
        ({}, "unknown"),
    ],
)
def test_release_falls_back_past_missing_and_unknown_values(env, release):
    assert resolve_telemetry(env).release == release


def test_environment_falls_back_to_environment_then_development():
    assert resolve_telemetry({"ENVIRONMENT": "production"}).environment == "production"
    assert resolve_telemetry({}).environment == "development"


def test_init_sentry_passes_the_resolved_pair_to_the_sdk(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(telemetry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    init_sentry(
        {"SENTRY_DSN": "https://k@bugsink.example/1", "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS}
    )
    assert len(calls) == 1
    assert calls[0]["release"] == SHA
    assert calls[0]["environment"] == "production"


def test_init_sentry_skips_the_sdk_without_a_dsn(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(telemetry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    init_sentry({"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS})
    assert calls == []


def test_init_sentry_logs_both_sides_of_the_contract(caplog):
    with caplog.at_level(logging.INFO, logger="upmovies.telemetry"):
        init_sentry({"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS})
    assert (
        f"release={SHA} environment=production otel service.version={SHA} "
        "deployment.environment=production sentry=off"
    ) in caplog.text


def test_init_sentry_warns_when_exporting_without_the_contract_attributes(caplog):
    with caplog.at_level(logging.WARNING, logger="upmovies.telemetry"):
        init_sentry(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://signoz:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=upcoming-movies-backend",
            }
        )
    assert "lacks service.version or deployment.environment" in caplog.text


def test_init_sentry_does_not_warn_when_the_contract_attributes_are_set(caplog):
    with caplog.at_level(logging.WARNING, logger="upmovies.telemetry"):
        init_sentry(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://signoz:4318",
                "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS,
            }
        )
    assert "lacks" not in caplog.text


# --- handled errors on the current span ------------------------------------------------------


def test_record_handled_exception_records_on_the_current_span_and_fails_it():
    # A private provider rather than the global one, so the test leaves no tracer behind.
    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("pipeline_run daily") as span:
        record_handled_exception(ValueError("boom"))
    [event] = span.events  # type: ignore[attr-defined]
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "ValueError"
    assert span.status.status_code is StatusCode.ERROR  # type: ignore[attr-defined]


def test_record_handled_exception_is_a_no_op_without_a_recording_span():
    record_handled_exception(ValueError("boom"))  # no current span: must not raise


def test_record_handled_exception_leaves_an_ended_span_alone():
    tracer = TracerProvider().get_tracer(__name__)
    span = tracer.start_span("request")
    span.end()
    with trace.use_span(span, end_on_exit=False):
        record_handled_exception(ValueError("boom"))
    assert span.events == ()  # type: ignore[attr-defined]
