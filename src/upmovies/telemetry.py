"""Release and environment, resolved once for Sentry and OpenTelemetry alike (NEU-1590).

The instrumentation contract (ops `neuroticsasquat-ch-systems-strategy-project-spec.md`,
§Instrumentation contract) requires Sentry's `release` to equal OTel's `service.version` (the
deployed git SHA) and Sentry's `environment` to equal OTel's `deployment.environment`, so an
error in Bugsink and a trace in SigNoz name the same build.

OTel reads `OTEL_RESOURCE_ATTRIBUTES` inside `opentelemetry-instrument`, before this process
imports anything of ours, so the app cannot hand OTel a value — it can only read the one OTel
already took. That is why the resource attributes are the first source here and the older
variables only a fallback for runs that do not set them (local dev, tests).

Same shape as tvbf-backend's `tvbf/telemetry.py` (NEU-1589), the contract's reference
implementation.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote

import sentry_sdk
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

log = logging.getLogger(__name__)

# Coolify passes the literal string "unknown" as SOURCE_COMMIT unless "Include SOURCE_COMMIT in
# build" is enabled, and the compose fallbacks use it too. It is never a SHA, so among the
# fallbacks it must not win over one that holds one.
_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Telemetry:
    release: str
    environment: str
    # What OTel itself was given, kept so the startup log can show both sides.
    otel_service_version: str | None
    otel_deployment_environment: str | None


def parse_resource_attributes(raw: str) -> dict[str, str]:
    """Parse `OTEL_RESOURCE_ATTRIBUTES` (`k1=v1,k2=v2`, values percent-encoded)."""
    attrs: dict[str, str] = {}
    for pair in raw.split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            attrs[key.strip()] = unquote(value.strip())
    return attrs


def _known(*candidates: str | None) -> str | None:
    for value in candidates:
        if value and value != _UNKNOWN:
            return value
    return None


def resolve_telemetry(env: Mapping[str, str]) -> Telemetry:
    attrs = parse_resource_attributes(env.get("OTEL_RESOURCE_ATTRIBUTES", ""))
    service_version = attrs.get("service.version")
    deployment_environment = attrs.get("deployment.environment")
    # Whatever OTel was given wins verbatim, "unknown" included: a better value here than
    # SigNoz holds would break the equality this module exists for.
    release = service_version or _known(env.get("SOURCE_COMMIT"), env.get("GIT_SHA")) or _UNKNOWN
    return Telemetry(
        release=release,
        environment=deployment_environment or env.get("ENVIRONMENT") or "development",
        otel_service_version=service_version,
        otel_deployment_environment=deployment_environment,
    )


def init_sentry(env: Mapping[str, str] = os.environ) -> Telemetry:
    """Initialise the Sentry SDK (pointed at Bugsink) when `SENTRY_DSN` is set, and log the
    resolved release and environment either way."""
    telemetry = resolve_telemetry(env)
    dsn = env.get("SENTRY_DSN")
    log.info(
        "telemetry: release=%s environment=%s otel service.version=%s "
        "deployment.environment=%s sentry=%s",
        telemetry.release,
        telemetry.environment,
        telemetry.otel_service_version,
        telemetry.otel_deployment_environment,
        "on" if dsn else "off",
    )
    if env.get("OTEL_EXPORTER_OTLP_ENDPOINT") and (
        telemetry.otel_service_version is None or telemetry.otel_deployment_environment is None
    ):
        # Exporting without them means SigNoz and Bugsink disagree on the build.
        log.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but OTEL_RESOURCE_ATTRIBUTES lacks "
            "service.version or deployment.environment"
        )
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            traces_sample_rate=0.1,
            environment=telemetry.environment,
            release=telemetry.release,
        )
    return telemetry


def record_handled_exception(exc: BaseException) -> None:
    """Record an exception we caught and handled on the current OTel span, and mark the span
    failed — the contract's `span.record_exception()` for handled errors.

    A handled error never reaches the instrumentation's own exception hooks, so without this the
    trace of a crashed stage reads as a success. A no-op when nothing is recording: no
    `opentelemetry-instrument` wrapper, or a span that has already ended (an `/admin/ingest`
    trigger's request span, which closes before the stage task it spawned finishes).
    """
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
