"""POST /admin/debug/raise-test-error — the `upmovies.debug.raise-test-error` trigger (NEU-1590).

Raises on purpose so the runbook can prove the whole error path end to end: SDK -> Bugsink ->
Discord `#alerts` (NEU-1597). Gated by the machine-facing `ADMIN_TOKEN` bearer like
`ingest_admin`, so `curl` can reach it without a session.

The exception is left unhandled: the Sentry FastAPI integration reports what escapes a route, so
catching it here would test a path production errors never take.
"""

from fastapi import APIRouter, Depends

from upmovies.deps import require_admin

router = APIRouter(prefix="/admin/debug", tags=["admin"], dependencies=[Depends(require_admin)])


class TelemetryTestError(RuntimeError):
    """Its own type, so Bugsink groups every trigger into one issue that no real failure can be
    mistaken for."""


@router.post("/raise-test-error")
async def raise_test_error() -> None:
    raise TelemetryTestError("upmovies.debug.raise-test-error: deliberate test error (NEU-1590)")
