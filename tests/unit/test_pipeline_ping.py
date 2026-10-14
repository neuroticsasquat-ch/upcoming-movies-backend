"""The Uptime Kuma push the pipeline runs feed their deadman with (NEU-1582).

Kuma reads a push as `GET <url>?status=up|down&msg=…&ping=…`; there is no start ping and no
path suffix. Which run pings what, when, is pinned in `tests/integration/test_pipeline_run.py`
— this file is the wire shape alone.
"""

import time

import httpx
import pytest
import respx

from upmovies import pipeline_run
from upmovies.config import get_settings
from upmovies.pipeline_run import MAX_PING_MESSAGE, StageOutcome, _ping, _ping_up

PUSH = "https://kuma.example.com/api/push/abc123"


def _params(route: respx.Route) -> dict[str, str]:
    return dict(route.calls.last.request.url.params)


@respx.mock
async def test_up_sends_status_up_with_the_duration():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await _ping_up(PUSH, time.monotonic() - 1.5)

    assert route.call_count == 1
    params = _params(route)
    assert params["status"] == "up"
    assert params["msg"] == "ok"
    assert 1500 <= int(params["ping"]) < 2500


@respx.mock
async def test_down_sends_status_down_with_the_message_and_no_duration():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await _ping(PUSH, "down", "link ended failed: provider timed out")

    assert route.call_count == 1
    assert _params(route) == {"status": "down", "msg": "link ended failed: provider timed out"}


@respx.mock
async def test_down_url_encodes_and_truncates_the_message():
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await _ping(PUSH, "down", "a&b=c " + "x" * 500)

    msg = _params(route)["msg"]
    assert len(msg) == MAX_PING_MESSAGE
    assert msg.startswith("a&b=c ")
    assert "a%26b%3Dc" in str(route.calls.last.request.url)


@respx.mock
async def test_the_query_replaces_one_pasted_from_kuma():
    """Kuma's UI shows the push URL with `?status=up&msg=OK&ping=` on the end; pasting it
    whole into Coolify must not leave a stale `status=up` on a down."""
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))

    await _ping(f"{PUSH}?status=up&msg=OK&ping=", "down", "boom")

    assert _params(route) == {"status": "down", "msg": "boom"}


@pytest.mark.parametrize("url", [None, ""])
@respx.mock
async def test_an_unset_url_sends_nothing(url):
    await _ping_up(url, time.monotonic())
    await _ping(url, "down", "boom")

    assert not respx.calls


@respx.mock
async def test_a_ping_that_cannot_get_out_is_swallowed(caplog):
    respx.get(PUSH).mock(side_effect=httpx.ConnectError("no route"))

    await _ping_up(PUSH, time.monotonic())
    await _ping(PUSH, "down", "boom")

    assert "deadman ping" in caplog.text


def test_failure_message_carries_the_first_line_of_the_recorded_error():
    outcome = StageOutcome("link", "failed", "ReadTimeout: provider\nTraceback ...")

    assert outcome.failure_message == "link ended failed: ReadTimeout: provider"


def test_failure_message_without_an_error_is_the_stage_and_status():
    assert StageOutcome("feeds", "cancelled", None).failure_message == "feeds ended cancelled"


@pytest.fixture
def no_cleanup(monkeypatch):
    """Keep the runs off the database: the stale-run sweep is not what is under test."""

    async def _noop(settings):
        return None

    monkeypatch.setattr(pipeline_run, "_clear_stale_runs", _noop)


@respx.mock
async def test_a_failed_stage_pings_down_with_its_recorded_error(monkeypatch, no_cleanup):
    async def failed(kind, runner, settings):
        return StageOutcome(kind, "failed", "provider timed out")

    monkeypatch.setattr(pipeline_run, "_run_tracked_stage", failed)
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))
    settings = get_settings().model_copy(update={"healthcheck_hourly_url": PUSH})

    assert await pipeline_run.run_hourly(settings) is False
    assert route.call_count == 1
    assert _params(route) == {"status": "down", "msg": "feeds ended failed: provider timed out"}


@respx.mock
async def test_a_crash_outside_the_stage_pings_down_and_still_raises(monkeypatch, no_cleanup):
    """The run insert or the status read-back raising leaves no row to report through, so
    the deadman hears it directly — and the task still fails, so Coolify hears it too."""

    async def crash(kind, runner, settings):
        raise RuntimeError("could not insert run\nmore detail")

    monkeypatch.setattr(pipeline_run, "_run_tracked_stage", crash)
    route = respx.get(PUSH).mock(return_value=httpx.Response(200))
    settings = get_settings().model_copy(update={"healthcheck_daily_url": PUSH})

    with pytest.raises(RuntimeError):
        await pipeline_run.run_daily(settings)
    assert route.call_count == 1
    assert _params(route) == {"status": "down", "msg": "RuntimeError: could not insert run"}
