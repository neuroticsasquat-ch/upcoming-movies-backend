"""Shared setup for the sweep's integration tests: a TMDB client pointed at respx and an
open `sweep` run for the phase to report progress against."""

import pytest

from upmovies.ingest import runs
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = "https://api.themoviedb.org/3"


@pytest.fixture
async def tmdb_client():
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        yield client


@pytest.fixture
async def run_id(session):
    run = await runs.create_run(session, kind="sweep")
    await session.commit()
    return run
