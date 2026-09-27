"""The per-test reset `tests/fixtures/db.py` gives the `session` fixture (NEU-1506). Driven
directly rather than through a pair of tests that run in order, because only a direct call can
prove the restart semantics without depending on collection order."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fixtures.db import DatabaseInventory, reset_database


async def _counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                    "WHERE schemaname IN ('app', 'catalog', 'news', 'ingest')"
                )
            )
        ).scalars()
        return {
            t: (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one() for t in tables
        }


async def test_reset_empties_every_table_and_restarts_every_sequence(
    session,  # noqa: ARG001 -- its own teardown clears what this test leaves on failure
    test_engine: AsyncEngine,
    db_inventory: DatabaseInventory,
):
    # Rows across two schemas, one hanging off another by FK, committed on a connection of
    # their own rather than through `session`: the reset reads the data, not the session.
    async with test_engine.begin() as conn:
        film_id = (
            await conn.execute(
                text(
                    "INSERT INTO catalog.film (tmdb_id, title, status) "
                    "VALUES (1506, 'Teardown', 'In Production') RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO catalog.film_alternative_title (film_id, title) VALUES (:f, 'Alt')"),
            {"f": film_id},
        )
        await conn.execute(text("INSERT INTO app.login_attempt (email) VALUES ('a@example.com')"))
        await conn.execute(text("INSERT INTO app.login_attempt (email) VALUES ('b@example.com')"))
        # A sequence advanced with no row left behind, as a flush-then-rollback leaves one.
        await conn.execute(text("SELECT nextval('catalog.film_video_id_seq')"))
    assert {t: n for t, n in (await _counts(test_engine)).items() if n} == {
        "catalog.film": 1,
        "catalog.film_alternative_title": 1,
        "app.login_attempt": 2,
    }

    async with test_engine.begin() as conn:
        await reset_database(conn, db_inventory)

    assert not any((await _counts(test_engine)).values())
    async with test_engine.connect() as conn:
        for seq in (
            "app.login_attempt_id_seq",
            "catalog.film_alternative_title_id_seq",
            "catalog.film_video_id_seq",
        ):
            assert (await conn.execute(text(f"SELECT nextval('{seq}')"))).scalar_one() == 1


async def test_the_test_engine_commits_asynchronously(test_engine: AsyncEngine):
    async with test_engine.connect() as conn:
        assert (await conn.execute(text("SHOW synchronous_commit"))).scalar_one() == "off"
