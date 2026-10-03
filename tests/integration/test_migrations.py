"""Migration ↔ model schema parity (NEU-1394).

The suite builds its database with ``Base.metadata.create_all`` (``tests/conftest.py``), so no
ordinary test ever runs a migration. Alembic autogenerate never emits ``CheckConstraint``, so
every ``ck_*`` is hand-written, and a forgotten one would otherwise ship green. These tests
build a second, scratch database with ``alembic upgrade head`` from a bare database -- the way
prod is built -- and diff its columns, constraints and indexes against the ``create_all``
schema. A missing constraint reads as one line of the assertion message, not a 390-row diff.

The head revision is also round-tripped (``downgrade -1`` → ``upgrade head``) so its
``downgrade()`` is proven to undo exactly what ``upgrade()`` did. Older downgrades are frozen
history and not covered (``26f140c4f334`` cannot downgrade at all).

Alembic runs in-process against each scratch database (``_alembic``), the same ``env.py`` the
CLI runs, pointed at the scratch URL through ``Config.attributes``.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = "'app', 'catalog', 'news', 'ingest'"

Snapshot = set[tuple[object, ...]]


async def _alembic(url: str, cmd: str, revision: str) -> None:
    # In-process, not a `python -m alembic` subprocess: each of those paid ~2.4 s of interpreter
    # startup and model import that pytest has already paid (NEU-1507). Two things make that safe:
    # - No config file name, so `env.py` skips `fileConfig(...)`, which would replace pytest's
    #   log handlers and disable the app's loggers for the rest of the run.
    # - `env.py` calls `asyncio.run()`, which fails under pytest-asyncio's running loop, so the
    #   command runs in a worker thread, which has no running loop of its own.
    # `env.py` prefers `attributes["url"]` to the lru-cached settings, which point at app_test.
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.attributes["url"] = url
    run = {"upgrade": command.upgrade, "downgrade": command.downgrade}[cmd]
    await asyncio.to_thread(run, config, revision)


@pytest.fixture(scope="session")
async def migrated_db_url() -> AsyncIterator[str]:
    """A scratch database built by ``alembic upgrade head`` from nothing -- no schemas, no
    extensions -- so ``env.py``'s bootstrap is under test too. Dropped after the session."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_migrations")
    scratch_db = scratch_url.database
    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", "head")
            yield url
        finally:
            # Also on a failed upgrade -- the case this fixture exists to catch -- so a broken
            # migration never leaves the scratch database behind.
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def _snapshot(engine: AsyncEngine) -> Snapshot:
    """Every column, constraint and index in the four app schemas, keyed by name and
    definition. Column order is deliberately excluded: migrations append columns, the model
    declares them in source order, and that is not a schema difference."""
    columns = text(
        "SELECT 'column', table_schema || '.' || table_name, column_name, data_type, udt_name, "
        "is_nullable, column_default, character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        f"WHERE table_schema IN ({_SCHEMAS}) AND table_name <> 'alembic_version'"
    )
    # pg_constraint is where check constraints, FKs (with ON DELETE), unique and primary keys
    # all live, and pg_get_constraintdef normalises the expression so it compares exactly.
    constraints = text(
        "SELECT 'constraint', n.nspname || '.' || c.relname, con.conname, con.contype::text, "
        "pg_get_constraintdef(con.oid) "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname IN ({_SCHEMAS}) AND c.relname <> 'alembic_version'"
    )
    indexes = text(
        "SELECT 'index', schemaname || '.' || tablename, indexname, indexdef "
        f"FROM pg_indexes WHERE schemaname IN ({_SCHEMAS}) AND tablename <> 'alembic_version'"
    )
    rows: Snapshot = set()
    async with engine.connect() as conn:
        for query in (columns, constraints, indexes):
            rows.update(tuple(row) for row in await conn.execute(query))
    return rows


async def _migrated_snapshot(url: str) -> Snapshot:
    # A throwaway engine, disposed here: a pooled connection left open would make the
    # fixture's DROP DATABASE fail.
    engine = create_async_engine(url)
    try:
        return await _snapshot(engine)
    finally:
        await engine.dispose()


def _assert_parity(model: Snapshot, migrated: Snapshot) -> None:
    missing = sorted(map(repr, model - migrated))
    extra = sorted(map(repr, migrated - model))
    assert not missing and not extra, (
        "\nDeclared by the model but missing from the migrations:\n  "
        + "\n  ".join(missing or ["(none)"])
        + "\nCreated by the migrations but not declared by the model:\n  "
        + "\n  ".join(extra or ["(none)"])
    )


async def test_migrations_build_the_model_schema(test_engine: AsyncEngine, migrated_db_url: str):
    model = await _snapshot(test_engine)
    migrated = await _migrated_snapshot(migrated_db_url)
    _assert_parity(model, migrated)


async def test_head_migration_round_trips(test_engine: AsyncEngine, migrated_db_url: str):
    # The round-trip NEU-1346 had to do by hand: the head revision's downgrade must run, and
    # re-upgrading must land back on the model's schema. Ends at head, so test order is moot.
    await _alembic(migrated_db_url, "downgrade", "-1")
    await _alembic(migrated_db_url, "upgrade", "head")
    model = await _snapshot(test_engine)
    migrated = await _migrated_snapshot(migrated_db_url)
    _assert_parity(model, migrated)


# --- the M8 data migration (NEU-1414) -------------------------------------------------------

_BEFORE_M8 = "f798756f89c7"
"""The revision immediately before M8 (`0a2426602d7c`), which is the one that drops
`app.watchlist_item` and copies what a user chose into follows."""


@pytest.fixture
async def m8_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision *short* of M8, so a test can seed the rows the
    migration is supposed to carry and then run it.

    Its own database rather than the session-scoped one above: that one is at head by
    definition, and the thing under test is the transition. Function-scoped because seeding and
    upgrading are what the test does, and a second test must not inherit the first's rows."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_m8")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_M8)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_m8_migration_turns_chosen_watchlist_rows_into_title_follows(m8_db_url: str):
    """The one-shot copy, against every kind of row the old model could hold (D-1414.10).

    What a user or their import *chose* becomes a title follow carrying its own `source` and
    `created_at` — the date they first showed interest, which is what the computed list sorts
    on. What the follow graph derived is not copied: its follows are still there and recompute
    it on read. A film they already followed keeps the follow it had, `created_at` included,
    because that row is at least as old and at least as truthful."""
    engine = create_async_engine(m8_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO app."user" (id, email, password_hash, display_name)
                VALUES ('11111111-1111-1111-1111-111111111111', 'm8@example.com', 'x', 'M8')
                """)
            )
            for n, film_id in enumerate(
                (
                    "22222222-2222-2222-2222-222222222222",
                    "33333333-3333-3333-3333-333333333333",
                    "44444444-4444-4444-4444-444444444444",
                    "55555555-5555-5555-5555-555555555555",
                ),
                start=1,
            ):
                await conn.execute(
                    text(
                        "INSERT INTO catalog.film (id, tmdb_id, title) "
                        "VALUES (:id, :tmdb_id, :title)"
                    ),
                    {"id": film_id, "tmdb_id": 9000 + n, "title": f"Film {n}"},
                )
            await conn.execute(
                text("""
                INSERT INTO app.watchlist_item (user_id, film_id, source, created_at) VALUES
                  ('11111111-1111-1111-1111-111111111111',
                   '22222222-2222-2222-2222-222222222222', 'manual', '2024-01-02T00:00:00Z'),
                  ('11111111-1111-1111-1111-111111111111',
                   '33333333-3333-3333-3333-333333333333',
                   'letterboxd_import', '2024-02-03T00:00:00Z'),
                  ('11111111-1111-1111-1111-111111111111',
                   '44444444-4444-4444-4444-444444444444',
                   'derived_from_follow', '2024-03-04T00:00:00Z'),
                  ('11111111-1111-1111-1111-111111111111',
                   '55555555-5555-5555-5555-555555555555', 'manual', '2024-04-05T00:00:00Z')
                """)
            )
            # Already followed by title, with an older row than the item above it.
            await conn.execute(
                text("""
                INSERT INTO app.follow (user_id, entity_type, entity_id, source, created_at)
                VALUES ('11111111-1111-1111-1111-111111111111', 'title',
                        '55555555-5555-5555-5555-555555555555', 'manual',
                        '2023-12-01T00:00:00Z')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.watchlist_dismissal (user_id, film_id)
                VALUES ('11111111-1111-1111-1111-111111111111',
                        '44444444-4444-4444-4444-444444444444')
                """)
            )

        await _alembic(m8_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            follows = (
                await conn.execute(
                    text(
                        "SELECT entity_id, source, created_at FROM app.follow "
                        "WHERE entity_type = 'title' ORDER BY entity_id"
                    )
                )
            ).all()
            # `coverage` is not read back: NEU-1432 drops the column further up the chain, and
            # this test runs to *head*. A title follow carried `lead` for nothing anyway.
            assert [(row[0], row[1]) for row in follows] == [
                ("22222222-2222-2222-2222-222222222222", "manual"),
                ("33333333-3333-3333-3333-333333333333", "letterboxd_import"),
                ("55555555-5555-5555-5555-555555555555", "manual"),
            ]
            # The copied rows keep the date the user first showed interest; the row that was
            # already a follow keeps its own, older one.
            assert [row[2].date().isoformat() for row in follows] == [
                "2024-01-02",
                "2024-02-03",
                "2023-12-01",
            ]
            # The mute seeded above does *not* survive to head: `b4c8e2f17a93` drops the
            # table with the watchlist it corrected (EF-14), and the rows go with it because
            # there is nothing to migrate them to — turning one into an unfollow would delete a
            # follow the user made deliberately, which is what D-40 forbids.
            dismissals = await conn.scalar(
                text("SELECT to_regclass('app.watchlist_dismissal') IS NOT NULL")
            )
            assert dismissals is False
            # And `watchlist_item` is gone, as it was at M8.
            exists = await conn.scalar(text("SELECT to_regclass('app.watchlist_item') IS NOT NULL"))
            assert exists is False
            # `alert_stores`, which M8 added, does not survive to head either: ADR-0021 drops
            # it with the alert it narrowed (NEU-1470).
            stores = await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'app' AND table_name = 'user_settings' "
                    "AND column_name = 'alert_stores'"
                )
            )
            assert stores == 0
    finally:
        await engine.dispose()


# --- the binary-follow migration (NEU-1432) -------------------------------------------------

_BEFORE_BINARY_FOLLOWS = "c7a1e4d9b2f3"
"""The revision immediately before `a5e1c93b7d40`, which drops `app.follow.coverage` and
deletes the person follows the ratings and favorites path inferred (EF-1, EF-20)."""


@pytest.fixture
async def binary_follows_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the binary-follow migration, on the
    `m8_db_url` pattern and for its reason: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_binary_follows")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_BINARY_FOLLOWS)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_binary_follow_migration_deletes_exactly_the_imported_person_follows(
    binary_follows_db_url: str,
):
    """EF-1's delete, against every combination of `(entity_type, source)` the table can hold.

    **Only person follows from an import go.** They were an inference — the user rated a film,
    so the importer followed its lead actors — tolerable only because the default coverage kept
    them narrow, and under EF-2 each would push on every credit change of everyone the user
    ever gave four stars to. A *title* follow from the same import is a film the user put on a
    list, which is a choice and is kept; so is a manually followed person, and so are company
    and franchise follows, which the imports never wrote and which have no tier to widen.
    """
    engine = create_async_engine(binary_follows_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO app."user" (id, email, password_hash, display_name)
                VALUES ('11111111-1111-1111-1111-111111111111', 'ef1@example.com', 'x', 'EF1')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO catalog.film (id, tmdb_id, title)
                VALUES ('22222222-2222-2222-2222-222222222222', 9001, 'A Film')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.follow (user_id, entity_type, entity_id, source, coverage) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'person', '100',
                   'letterboxd_import', 'lead'),
                  ('11111111-1111-1111-1111-111111111111', 'person', '101',
                   'tmdb_import', 'lead'),
                  ('11111111-1111-1111-1111-111111111111', 'person', '102', 'manual', 'any'),
                  ('11111111-1111-1111-1111-111111111111', 'person', '103', 'derived', 'major'),
                  ('11111111-1111-1111-1111-111111111111', 'title',
                   '22222222-2222-2222-2222-222222222222', 'letterboxd_import', 'lead'),
                  ('11111111-1111-1111-1111-111111111111', 'company', '200',
                   'tmdb_import', 'lead'),
                  ('11111111-1111-1111-1111-111111111111', 'franchise', '300', 'manual', 'lead')
                """)
            )

        await _alembic(binary_follows_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT entity_type, entity_id, source FROM app.follow "
                        "ORDER BY entity_type, entity_id"
                    )
                )
            ).all()
            assert [tuple(row) for row in rows] == [
                ("company", "200", "tmdb_import"),
                ("franchise", "300", "manual"),
                ("person", "102", "manual"),
                ("person", "103", "derived"),
                ("title", "22222222-2222-2222-2222-222222222222", "letterboxd_import"),
            ]
            # And the column is gone, CHECK included — the parity test compares names, so a
            # constraint outliving its column would fail there instead of here.
            column = await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'app' AND table_name = 'follow' "
                    "AND column_name = 'coverage'"
                )
            )
            assert column == 0
    finally:
        await engine.dispose()


# --- the watchlist_dismissal drop (NEU-1439) ------------------------------------------------

_BEFORE_DISMISSAL_DROP = "e2b7d41c9f08"
"""The revision immediately before `b4c8e2f17a93`, which drops `app.watchlist_dismissal`
(EF-14)."""


@pytest.fixture
async def dismissal_drop_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the drop, on the `m8_db_url` pattern and
    for its reason: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_dismissal_drop")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_DISMISSAL_DROP)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_dismissal_drop_takes_the_mutes_and_leaves_every_follow(
    dismissal_drop_db_url: str,
):
    """EF-14: the table goes, and the follow graph is untouched.

    The follow is the half worth asserting. A mute and a title follow on the same film were two
    rows saying opposite things, and the tempting migration — "a mute means they did not want
    it, so drop the follow too" — would delete a row the user created deliberately, which is
    exactly what D-40 keeps out of this. The user sees that film again; that is the state EF-14
    defines, not data loss the migration should have prevented."""
    engine = create_async_engine(dismissal_drop_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO app."user" (id, email, password_hash, display_name)
                VALUES ('11111111-1111-1111-1111-111111111111', 'ef14@example.com', 'x', 'EF14')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO catalog.film (id, tmdb_id, title)
                VALUES ('22222222-2222-2222-2222-222222222222', 9101, 'Silenced')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.follow (user_id, entity_type, entity_id, source) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'title',
                   '22222222-2222-2222-2222-222222222222', 'manual'),
                  ('11111111-1111-1111-1111-111111111111', 'person', '525', 'manual')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.watchlist_dismissal (user_id, film_id)
                VALUES ('11111111-1111-1111-1111-111111111111',
                        '22222222-2222-2222-2222-222222222222')
                """)
            )

        await _alembic(dismissal_drop_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT to_regclass('app.watchlist_dismissal') IS NOT NULL")
            )
            assert exists is False
            follows = (
                await conn.execute(
                    text(
                        "SELECT entity_type, entity_id FROM app.follow "
                        "ORDER BY entity_type, entity_id"
                    )
                )
            ).all()
            assert [tuple(row) for row in follows] == [
                ("person", "525"),
                ("title", "22222222-2222-2222-2222-222222222222"),
            ]
    finally:
        await engine.dispose()


# --- the credits_observed_at backfill (NEU-1436) --------------------------------------------

_BEFORE_CREDITS_BACKFILL = "d3f5a81c6b47"
"""The revision immediately before `e2b7d41c9f08`, which stamps `film.credits_observed_at` on
every film already holding credits (EF-4, D-1436.6)."""


@pytest.fixture
async def credits_backfill_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the backfill, on the `m8_db_url`
    pattern and for its reason: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_credits_backfill")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_CREDITS_BACKFILL)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_backfill_stamps_only_films_that_hold_credits(credits_backfill_db_url: str):
    """`957421e2651e` added `credits_observed_at` without stamping the catalog, which was
    harmless while a first observation was silent. From NEU-1436 on it is not: an unstamped
    film would card every credit of every followed person on its next read as a fresh
    attachment.

    A film holding *no* credit rows is left NULL, deliberately — it was admitted with an empty
    payload and has genuinely never been observed, so its first credits are still a baseline.
    That is the distinction the marker exists to preserve, and a blanket `UPDATE` would lose
    it. A film already stamped keeps its own timestamp rather than being moved forward.
    """
    engine = create_async_engine(credits_backfill_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO catalog.film (id, tmdb_id, title, credits_observed_at) VALUES
                  ('11111111-1111-1111-1111-111111111111', 9001, 'Has Credits', NULL),
                  ('22222222-2222-2222-2222-222222222222', 9002, 'No Credits', NULL),
                  ('33333333-3333-3333-3333-333333333333', 9003, 'Already Observed',
                   '2026-01-01T00:00:00+00:00')
                """)
            )
            await conn.execute(
                text("INSERT INTO catalog.person (id, name) VALUES (100, 'Someone')")
            )
            await conn.execute(
                text("""
                INSERT INTO catalog.film_credit
                    (credit_id, film_id, person_id, credit_type, job)
                VALUES ('c-1', '11111111-1111-1111-1111-111111111111', 100, 'crew', 'Director'),
                       ('c-2', '33333333-3333-3333-3333-333333333333', 100, 'crew', 'Director')
                """)
            )

        await _alembic(credits_backfill_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT tmdb_id, credits_observed_at FROM catalog.film ORDER BY tmdb_id")
                )
            ).all()
            stamped = {tmdb_id: observed_at for tmdb_id, observed_at in rows}
            assert stamped[9001] is not None
            assert stamped[9002] is None
            assert stamped[9003].year == 2026 and stamped[9003].month == 1

            # The column is on `FILM_FIELD_CHANGE_DENYLIST`, so the UPDATE writes no history:
            # a row here would card as a public event about our own bookkeeping and make the
            # whole catalog look active to `dormant_film_clause` on the day of the deploy.
            history = await conn.scalar(text("SELECT count(*) FROM catalog.film_field_change"))
            assert history == 0
    finally:
        await engine.dispose()


# --- the unsubscribe_token backfill (NEU-1463) ----------------------------------------------

_BEFORE_UNSUBSCRIBE_TOKEN = "61b8dca53f8b"
"""The revision immediately before `32380c924b81`, which adds `user_settings.unsubscribe_token`
NOT NULL and fills it for every existing row (DC-10)."""


@pytest.fixture
async def unsubscribe_token_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the token column, on the
    `credits_backfill_db_url` pattern: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_unsubscribe_token")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_UNSUBSCRIBE_TOKEN)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_migration_gives_every_existing_settings_row_its_own_unsubscribe_token(
    unsubscribe_token_db_url: str,
):
    """The column is NOT NULL, so a row the backfill missed would fail the migration outright;
    what this pins is that each row gets a *distinct* token of `new_unsubscribe_token`'s width —
    one shared value would unsubscribe every reader from one link."""
    engine = create_async_engine(unsubscribe_token_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO app."user" (id, email, password_hash, display_name) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'ada@example.com', 'x', 'Ada'),
                  ('22222222-2222-2222-2222-222222222222', 'bob@example.com', 'x', 'Bob')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.user_settings (user_id, ical_token) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'ical-ada'),
                  ('22222222-2222-2222-2222-222222222222', 'ical-bob')
                """)
            )

        await _alembic(unsubscribe_token_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            tokens = (
                (await conn.execute(text("SELECT unsubscribe_token FROM app.user_settings")))
                .scalars()
                .all()
            )
        assert len(tokens) == 2
        assert len(set(tokens)) == 2
        # `secrets.token_urlsafe(32)`: 32 bytes, base64url without padding.
        assert all(len(token) == 43 for token in tokens)
    finally:
        await engine.dispose()


# --- the digest-only migration (NEU-1470) ---------------------------------------------------

_BEFORE_DIGEST_ONLY = "de5bd1b49fe2"
"""The revision immediately before `a92a8e808b07`, which deletes the `alert` and `push`
notification rows, tightens the two vocabularies and drops `push_subscription` and
`user_settings.alert_stores` (ADR-0021)."""


@pytest.fixture
async def digest_only_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the digest-only migration, on the
    `credits_backfill_db_url` pattern: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_digest_only")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_DIGEST_ONLY)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def test_the_digest_only_migration_keeps_exactly_the_digest_email_rows(
    digest_only_db_url: str,
):
    """Every `alert` row and every `push` row goes, the `digest`/`email` row for the same event
    stays, and the tightened constraints then refuse the old vocabulary outright — so the
    delete is not a one-off cleanup a stray writer could undo."""
    engine = create_async_engine(digest_only_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                INSERT INTO app."user" (id, email, password_hash, display_name) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'ada@example.com', 'x', 'Ada')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO catalog.film (id, tmdb_id, title) VALUES
                  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 1, 'Clayface')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO news.event (id, film_id, event_type, confidence, occurred_at) VALUES
                  ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
                   'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'trailer', 'confirmed', now())
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.notification (user_id, event_id, kind, channel, status) VALUES
                  ('11111111-1111-1111-1111-111111111111',
                   'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'alert', 'email', 'sent'),
                  ('11111111-1111-1111-1111-111111111111',
                   'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'alert', 'push', 'queued'),
                  ('11111111-1111-1111-1111-111111111111',
                   'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'digest', 'email', 'queued')
                """)
            )
            await conn.execute(
                text("""
                INSERT INTO app.push_subscription (user_id, endpoint, p256dh, auth) VALUES
                  ('11111111-1111-1111-1111-111111111111', 'https://push.example/1', 'k', 'a')
                """)
            )

        await _alembic(digest_only_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT kind, channel FROM app.notification"))).all()
            assert [tuple(row) for row in rows] == [("digest", "email")]
            push = await conn.scalar(text("SELECT to_regclass('app.push_subscription')"))
            assert push is None
        for kind, channel in (("alert", "email"), ("digest", "push")):
            with pytest.raises(Exception, match="ck_notification_"):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO app.notification "
                            "(user_id, event_id, kind, channel, status) VALUES "
                            "('11111111-1111-1111-1111-111111111111', "
                            "'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', :kind, :channel, 'queued')"
                        ),
                        {"kind": kind, "channel": channel},
                    )
    finally:
        await engine.dispose()


# --- the credit_removed split (NEU-1518) ----------------------------------------------------

_BEFORE_REMOVAL_SPLIT = "b3e1c5a7d905"
"""The revision immediately before `feb127488dae`, which splits every `credit_removed` card by
role class into `cast_removed` / `crew_removed` and retires the type (NR-12)."""

_FILM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MIXED = "11111111-1111-1111-1111-111111111111"
_CAST_ONLY = "22222222-2222-2222-2222-222222222222"
_CREW_ONLY = "33333333-3333-3333-3333-333333333333"
_CASTING = "44444444-4444-4444-4444-444444444444"
_CREW_ATTACHED = "55555555-5555-5555-5555-555555555555"
_USER = "99999999-9999-9999-9999-999999999999"


@pytest.fixture
async def removal_split_db_url() -> AsyncIterator[str]:
    """A scratch database stopped one revision short of the split, on the
    `credits_backfill_db_url` pattern: the thing under test is the transition."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_removal_split")
    scratch_db = scratch_url.database
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            await _alembic(url, "upgrade", _BEFORE_REMOVAL_SPLIT)
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def _seed_removal_cards(conn) -> None:
    """Three `credit_removed` cards, carded the way the sweep carded them before the split:
    one mixed (a writer and two actors at one observation, with a third actor the
    prior-attachment gate dropped), one cast-only and one crew-only."""
    await conn.execute(
        text(f"INSERT INTO catalog.film (id, tmdb_id, title) VALUES ('{_FILM}', 1, 'Mixed')")
    )
    await conn.execute(
        text(
            "INSERT INTO catalog.person (id, name) VALUES (1, 'D.C. Shen'), (2, 'Alan Bell'), "
            "(3, 'Jack Black'), (4, 'Gated Out'), (5, 'Solo Director')"
        )
    )
    await conn.execute(
        text(f"""
        INSERT INTO catalog.film_credit_change
            (film_id, person_id, credit_type, job, change, changed_at)
        VALUES ('{_FILM}', 1, 'crew', 'Screenplay', 'removed', '2026-08-01T00:00:00+00:00'),
               ('{_FILM}', 2, 'cast', NULL, 'removed', '2026-08-01T00:00:00+00:00'),
               ('{_FILM}', 3, 'cast', NULL, 'removed', '2026-08-01T00:00:00+00:00'),
               ('{_FILM}', 4, 'crew', 'Director', 'removed', '2026-08-01T00:00:00+00:00'),
               ('{_FILM}', 2, 'cast', NULL, 'removed', '2026-08-05T00:00:00+00:00'),
               ('{_FILM}', 5, 'crew', 'Director', 'removed', '2026-08-09T00:00:00+00:00')
        """)
    )
    await conn.execute(
        text(f"""
        INSERT INTO news.event (id, film_id, event_type, confidence, provenance, subject_key,
                                occurred_at, created_at, updated_at)
        VALUES ('{_MIXED}', '{_FILM}', 'credit_removed', 'rumored', 'catalog',
                ARRAY['d.c. shen', 'alan bell', 'jack black'], '2026-08-01T00:00:00+00:00',
                '2026-08-02T00:00:00+00:00', '2026-08-02T00:00:00+00:00'),
               ('{_CAST_ONLY}', '{_FILM}', 'credit_removed', 'rumored', 'catalog',
                ARRAY['alan bell'], '2026-08-05T00:00:00+00:00',
                '2026-08-06T00:00:00+00:00', '2026-08-06T00:00:00+00:00'),
               ('{_CREW_ONLY}', '{_FILM}', 'credit_removed', 'rumored', 'catalog',
                ARRAY['solo director'], '2026-08-09T00:00:00+00:00',
                '2026-08-10T00:00:00+00:00', '2026-08-10T00:00:00+00:00')
        """)
    )
    await conn.execute(
        text(f"""
        INSERT INTO news.event (id, film_id, event_type, confidence, provenance, subject_key,
                                occurred_at, status, superseded_by)
        VALUES ('{_CASTING}', '{_FILM}', 'casting', 'rumored', 'catalog',
                ARRAY['alan bell', 'jack black'], '2026-07-01T00:00:00+00:00',
                'superseded', '{_MIXED}'),
               ('{_CREW_ATTACHED}', '{_FILM}', 'crew_attached', 'rumored', 'catalog',
                ARRAY['d.c. shen'], '2026-07-01T00:00:00+00:00', 'superseded', '{_MIXED}')
        """)
    )
    await conn.execute(
        text(f"""
        INSERT INTO news.event_summary (event_id, summary, model, prompt_version,
                                        source_updated_at)
        VALUES ('{_MIXED}', :mixed_summary, 'deterministic', 'deterministic-8',
                '2026-08-02T00:00:00+00:00'),
               ('{_CAST_ONLY}', 'Alan Bell departs the cast.', 'deterministic',
                'deterministic-8', '2026-08-06T00:00:00+00:00'),
               ('{_CREW_ONLY}', 'Solo Director is no longer attached to direct.',
                'deterministic', 'deterministic-8', '2026-08-10T00:00:00+00:00')
        """),
        {
            "mixed_summary": (
                "D.C. Shen is no longer attached to write. "
                "Alan Bell and Jack Black depart the cast."
            )
        },
    )
    await conn.execute(
        text(
            f'INSERT INTO app."user" (id, email, password_hash, display_name) '
            f"VALUES ('{_USER}', 'ada@example.com', 'x', 'Ada')"
        )
    )
    await conn.execute(
        text(f"""
        INSERT INTO app.notification (user_id, event_id, kind, channel, status, sent_at)
        VALUES ('{_USER}', '{_MIXED}', 'digest', 'email', 'sent', '2026-08-03T00:00:00+00:00')
        """)
    )


async def test_the_split_retypes_single_class_cards_and_splits_a_mixed_one(
    removal_split_db_url: str,
):
    """NR-12. The single-class cards keep everything but their type. The mixed card keeps its
    id as the cast half; the crew half is new, with the original's timestamps, each half's body
    re-rendered from its own class — names read from the credit rows, so "D.C. Shen" survives
    the period a summary parse would have split on — and the person the gate dropped (on the
    same observation's rows but not on the card) is on neither. The crew attachment the
    original corrected now points at the crew half, and the sent digest row is sent for both."""
    from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

    engine = create_async_engine(removal_split_db_url)
    try:
        async with engine.begin() as conn:
            await _seed_removal_cards(conn)

        await _alembic(removal_split_db_url, "upgrade", "head")

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT e.id::text, e.event_type, e.subject_key, e.occurred_at, "
                        "e.created_at, s.summary, s.model, s.prompt_version "
                        "FROM news.event e JOIN news.event_summary s ON s.event_id = e.id "
                        "WHERE e.event_type IN ('cast_removed', 'crew_removed') "
                        "ORDER BY e.occurred_at, e.event_type"
                    )
                )
            ).all()
            assert [(r.id, r.event_type, r.subject_key, r.summary) for r in rows] == [
                (
                    _MIXED,
                    "cast_removed",
                    ["alan bell", "jack black"],
                    "Alan Bell and Jack Black depart the cast.",
                ),
                (
                    rows[1].id,
                    "crew_removed",
                    ["d.c. shen"],
                    "D.C. Shen is no longer attached to write.",
                ),
                (_CAST_ONLY, "cast_removed", ["alan bell"], "Alan Bell departs the cast."),
                (
                    _CREW_ONLY,
                    "crew_removed",
                    ["solo director"],
                    "Solo Director is no longer attached to direct.",
                ),
            ]
            crew_half = rows[1]
            assert crew_half.id not in {_MIXED, _CAST_ONLY, _CREW_ONLY}
            assert (crew_half.occurred_at, crew_half.created_at) == (
                rows[0].occurred_at,
                rows[0].created_at,
            )
            for half in rows[:2]:
                assert (half.model, half.prompt_version) == (DETERMINISTIC_MODEL, TEMPLATE_VERSION)
            # A single-class card's summary is untouched, down to its template version.
            assert rows[2].prompt_version == "deterministic-8"

            attachments = (
                await conn.execute(
                    text(
                        "SELECT id::text, superseded_by::text FROM news.event "
                        "WHERE event_type IN ('casting', 'crew_attached')"
                    )
                )
            ).all()
            links = {row.id: row.superseded_by for row in attachments}
            assert links == {_CASTING: _MIXED, _CREW_ATTACHED: crew_half.id}

            notifications = (
                await conn.execute(
                    text("SELECT event_id::text, status, sent_at FROM app.notification")
                )
            ).all()
            assert sorted((n.event_id, n.status) for n in notifications) == sorted(
                [(_MIXED, "sent"), (crew_half.id, "sent")]
            )
            assert len({n.sent_at for n in notifications}) == 1

        with pytest.raises(Exception, match="ck_event_type"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO news.event (film_id, event_type, confidence, occurred_at) "
                        f"VALUES ('{_FILM}', 'credit_removed', 'rumored', now())"
                    )
                )

        # The downgrade is lossy for the split pair: the crew half folds back into the cast
        # half's id, names and references, and its summary is gone.
        await _alembic(removal_split_db_url, "downgrade", _BEFORE_REMOVAL_SPLIT)

        async with engine.connect() as conn:
            removals = (
                await conn.execute(
                    text(
                        "SELECT id::text, event_type, subject_key FROM news.event "
                        "WHERE event_type NOT IN ('casting', 'crew_attached') ORDER BY occurred_at"
                    )
                )
            ).all()
            assert [tuple(r) for r in removals] == [
                (_MIXED, "credit_removed", ["alan bell", "jack black", "d.c. shen"]),
                (_CAST_ONLY, "credit_removed", ["alan bell"]),
                (_CREW_ONLY, "credit_removed", ["solo director"]),
            ]
            crew_link = await conn.scalar(
                text(f"SELECT superseded_by::text FROM news.event WHERE id = '{_CREW_ATTACHED}'")
            )
            assert crew_link == _MIXED
            notified = (
                await conn.execute(text("SELECT event_id::text FROM app.notification"))
            ).all()
            assert [n.event_id for n in notified] == [_MIXED]
    finally:
        await engine.dispose()


async def test_the_split_fails_naming_a_card_whose_name_has_no_credit_row(
    removal_split_db_url: str,
):
    """The history table is the only record of who was which class, so a name it cannot place
    stops the migration rather than being guessed at from the summary."""
    ghost = "66666666-6666-6666-6666-666666666666"
    engine = create_async_engine(removal_split_db_url)
    try:
        async with engine.begin() as conn:
            await _seed_removal_cards(conn)
            await conn.execute(
                text(f"""
                INSERT INTO news.event (id, film_id, event_type, confidence, provenance,
                                        subject_key, occurred_at)
                VALUES ('{ghost}', '{_FILM}', 'credit_removed', 'rumored', 'catalog',
                        ARRAY['nobody recorded'], '2026-08-20T00:00:00+00:00')
                """)
            )

        with pytest.raises(RuntimeError, match=ghost):
            await _alembic(removal_split_db_url, "upgrade", "head")

        # Transactional DDL: nothing the failed run did is left behind.
        async with engine.connect() as conn:
            remaining = await conn.scalar(
                text("SELECT count(*) FROM news.event WHERE event_type = 'credit_removed'")
            )
            assert remaining == 4
    finally:
        await engine.dispose()


async def test_the_split_fails_on_a_mixed_reading_its_summary_never_said(
    removal_split_db_url: str,
):
    """The old flap gate dropped credits per (person, role), which the name filter cannot see:
    an actor-director whose director credit flapped back while their cast exit stuck is on the
    card, so both of their rows match. The card the sweep wrote only ever said they left the
    cast, so splitting it would invent a crew departure; the migration stops instead."""
    flapped = "77777777-7777-7777-7777-777777777777"
    engine = create_async_engine(removal_split_db_url)
    try:
        async with engine.begin() as conn:
            await _seed_removal_cards(conn)
            await conn.execute(
                text("INSERT INTO catalog.person (id, name) VALUES (6, 'Greta Gerwig')")
            )
            await conn.execute(
                text(f"""
                INSERT INTO catalog.film_credit_change
                    (film_id, person_id, credit_type, job, change, changed_at)
                VALUES ('{_FILM}', 6, 'cast', NULL, 'removed', '2026-08-20T00:00:00+00:00'),
                       ('{_FILM}', 6, 'crew', 'Director', 'removed', '2026-08-20T00:00:00+00:00')
                """)
            )
            await conn.execute(
                text(f"""
                INSERT INTO news.event (id, film_id, event_type, confidence, provenance,
                                        subject_key, occurred_at)
                VALUES ('{flapped}', '{_FILM}', 'credit_removed', 'rumored', 'catalog',
                        ARRAY['greta gerwig'], '2026-08-20T00:00:00+00:00')
                """)
            )
            await conn.execute(
                text(f"""
                INSERT INTO news.event_summary (event_id, summary, model, prompt_version,
                                                source_updated_at)
                VALUES ('{flapped}', 'Greta Gerwig departs the cast.', 'deterministic',
                        'deterministic-8', '2026-08-20T00:00:00+00:00')
                """)
            )

        with pytest.raises(RuntimeError, match=flapped):
            await _alembic(removal_split_db_url, "upgrade", "head")
    finally:
        await engine.dispose()
