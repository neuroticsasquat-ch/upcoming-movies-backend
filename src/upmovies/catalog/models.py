from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from upmovies.db import Base


class Film(Base):
    """Canonical film record. TMDB is the spine; downstream projects extend the
    `catalog` schema (people, credits) without altering this seam."""

    __tablename__ = "film"
    __table_args__ = (
        Index("ix_catalog_film_slug", "slug", unique=True),
        {"schema": "catalog"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    adult: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    backdrop_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    runtime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tagline: Mapped[str | None] = mapped_column(Text, nullable=True)
    video: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vote_average: Mapped[float | None] = mapped_column(Float, nullable=True)
    vote_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin_country: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    collection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("catalog.collection.id"), nullable=True
    )
    tmdb_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credits_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the catalog first held an observation of this film's credits — NULL until it has.

    The durable form of "first observation is a baseline, never a change" (ADR-0014, spec
    §5.3). It cannot be inferred from `film_credit` being empty: a speculative TMDB entry can
    be admitted with an empty credits payload, and a film that *was* observed holding nothing
    must be told apart from one that was never looked at, or the director who attaches next
    run is silently swallowed as a baseline. Ingest bookkeeping, not a fact about the film —
    hence its place in `FILM_FIELD_CHANGE_DENYLIST`.
    """
    release_dates_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the catalog first held an observation of this film's release slate — NULL until it
    has. `credits_observed_at` for release dates (NEU-1121), and load-bearing for exactly the
    same reason, only more so: an undated film is admitted with *no* release rows at all, so
    inferring "never observed" from "holds nothing" would re-baseline it on every ingest and
    swallow the first date it is ever given — the single most valuable beat this project's
    undated population can produce. Ingest bookkeeping, so it joins the denylist too.
    """
    tmdb_missing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When TMDB was last confirmed to have no entry at this film's id — NULL while it is live.

    A tombstone for the *TMDB entry*, not for the film. Films are never deleted (spec §4.4:
    "if we announced it, we do not un-announce it"), so a missing film keeps its page, its
    events and its linked stories. All this column does is take the id off the sweep's normal
    refresh cadence, which is the one place a permanently dead id costs a request every single
    day (NEU-1124).

    Load-bearing because a 404 is self-perpetuating without it: the refresh set is ordered
    stalest-first on `updated_at`, and a film that cannot be fetched never has its `updated_at`
    bumped, so it holds the head of the queue forever and every pass pays for it again.

    *Last confirmed*, not first observed, because the column also drives the re-check cadence
    below — a first-observation stamp would go stale immediately and make every later pass due.
    The cost is that "how long has this been gone" is no longer readable off the row; the
    alternative was a second column to carry it, for a question nothing asks yet.

    **Tombstoned is not deleted, and not a one-way door.** A missing film returns to the refresh
    set once its tombstone is older than `dormant_refresh_days`, on exactly the reduced cadence
    §4.5 gives dormant films and for the same reason: detecting that TMDB has restored an entry
    requires asking TMDB about it, so a tombstone that suppressed the only reader would be a
    door with no handle on the other side. A confirmed-still-missing film re-stamps and drops
    back out, so the standing cost is one request per dead id per cadence, not per pass.

    Ingest bookkeeping rather than a fact about the film, so it joins
    `FILM_FIELD_CHANGE_DENYLIST` — a catalog-sourced event announcing that TMDB deleted a
    record would be news about our source, not about the production.
    """
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Genre(Base):
    """TMDB genre reference (natural PK = TMDB's stable genre id)."""

    __tablename__ = "genre"
    __table_args__ = {"schema": "catalog"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ProductionCompany(Base):
    __tablename__ = "production_company"
    __table_args__ = {"schema": "catalog"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    logo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_country: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProductionCountry(Base):
    __tablename__ = "production_country"
    __table_args__ = {"schema": "catalog"}

    iso_3166_1: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class SpokenLanguage(Base):
    __tablename__ = "spoken_language"
    __table_args__ = {"schema": "catalog"}

    iso_639_1: Mapped[str] = mapped_column(Text, primary_key=True)
    english_name: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Collection(Base):
    __tablename__ = "collection"
    __table_args__ = {"schema": "catalog"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    poster_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    backdrop_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class FilmGenre(Base):
    __tablename__ = "film_genre"
    __table_args__ = {"schema": "catalog"}

    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        primary_key=True,
    )
    genre_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog.genre.id"), primary_key=True)


class FilmProductionCompany(Base):
    __tablename__ = "film_production_company"
    __table_args__ = {"schema": "catalog"}

    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        primary_key=True,
    )
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog.production_company.id"), primary_key=True
    )


class FilmProductionCountry(Base):
    __tablename__ = "film_production_country"
    __table_args__ = {"schema": "catalog"}

    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        primary_key=True,
    )
    iso_3166_1: Mapped[str] = mapped_column(
        Text, ForeignKey("catalog.production_country.iso_3166_1"), primary_key=True
    )


class FilmSpokenLanguage(Base):
    __tablename__ = "film_spoken_language"
    __table_args__ = {"schema": "catalog"}

    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        primary_key=True,
    )
    iso_639_1: Mapped[str] = mapped_column(
        Text, ForeignKey("catalog.spoken_language.iso_639_1"), primary_key=True
    )


class FilmReleaseDate(Base):
    """Per-country, per-type TMDB release date for a film."""

    __tablename__ = "film_release_date"
    __table_args__ = (
        Index("ix_catalog_film_release_date_film", "film_id"),
        # Seeded for the upcoming GET /calendar endpoint (browse by date/type); the current
        # film-detail query is served by ix_catalog_film_release_date_film.
        Index("ix_catalog_film_release_date_lookup", "release_date", "release_type"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    iso_3166_1: Mapped[str] = mapped_column(Text, nullable=False)
    release_type: Mapped[int] = mapped_column(Integer, nullable=False)
    release_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    certification: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    iso_639_1: Mapped[str | None] = mapped_column(Text, nullable=True)


class FilmAlternativeTitle(Base):
    """Per-country TMDB alternative title for a film."""

    __tablename__ = "film_alternative_title"
    __table_args__ = (
        Index("ix_catalog_film_alt_title_film", "film_id"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    iso_3166_1: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_type: Mapped[str | None] = mapped_column(Text, nullable=True)


class Person(Base):
    """TMDB person reference (natural PK = TMDB's stable person id)."""

    __tablename__ = "person"
    __table_args__ = {"schema": "catalog"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    known_for_department: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[int | None] = mapped_column(Integer, nullable=True)
    popularity: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmdb_missing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When TMDB was last confirmed to have no entry at this person's id — NULL while live.

    `Film.tmdb_missing_at` for seed people (NEU-1124). Around fifty of the ~7,700 filmographies
    the enumerate phase requests each run are for people TMDB has deleted, and without this they
    are re-requested every run forever; the credit rows that make them seeds are ours and
    outlive the person record upstream.

    Revival needs no cadence here, unlike the film case: this is cleared by the person upsert
    that runs on every film ingest, so a restored person reappears in the seed set as soon as
    any film they are credited on is next read. The handle on the other side is a door someone
    else opens.
    """


class FilmFieldChange(Base):
    """Append-only history of changed `catalog.film` column values, written by the
    `film_field_change_trg` trigger (see the trigger SQL below). Enables deterministic
    "how long have we held this value?" checks without per-field timestamp columns."""

    __tablename__ = "film_field_change"
    __table_args__ = (
        Index("ix_film_field_change_lookup", "film_id", "field", "changed_at"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FilmCredit(Base):
    """Per-film credit edge linking a Film to a Person (rebuilt each ingest)."""

    __tablename__ = "film_credit"
    __table_args__ = (
        Index("ix_catalog_film_credit_film", "film_id"),
        Index("ix_catalog_film_credit_person", "person_id"),
        {"schema": "catalog"},
    )

    credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog.person.id"), nullable=False)
    credit_type: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    job: Mapped[str | None] = mapped_column(Text, nullable=True)
    character: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_order: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FilmCreditChange(Base):
    """Append-only history of seed-grade credit attachments and detachments, written by the
    credit rebuild in `ingest.tmdb.credit_history` (ADR-0014, spec §5.2).

    `catalog.film_credit` is delete-and-rebuilt on every ingest, so it holds no memory of a
    director having been attached — only that one is attached now. This table is that memory,
    and it is the whole reason a credit can card as an event (NEU-1083).

    Two properties it does *not* get for free, unlike `film_field_change`, which is a
    `BEFORE UPDATE` trigger and so writes no history on insert:

    - **First observation is a baseline, never a change** (spec §5.3). The rebuild has both
      sides in hand and diffs them explicitly; a film whose credits the catalog has never
      held records them with no rows here. Without it, admitting 3,000 films would write tens
      of thousands of false attachments on day one.
    - **Only seed-grade credits** (director, writer, top-5 billed cast) are recorded. A
      40th-billed extra churning between ingests is noise, and recording it would make this
      table churn with TMDB's cast-ordering edits.
    """

    __tablename__ = "film_credit_change"
    __table_args__ = (
        Index("ix_catalog_film_credit_change_lookup", "film_id", "changed_at"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog.person.id"), nullable=False)
    credit_type: Mapped[str] = mapped_column(Text, nullable=False)
    """`cast` or `crew`, mirroring `film_credit.credit_type`."""
    job: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The crew job (`Director`, `Writer`, `Screenplay`); NULL for a cast credit, which has
    no job — the seed grade it carries is its top-5 billing, not a title."""
    change: Mapped[str] = mapped_column(Text, nullable=False)
    """`added` or `removed`."""
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FilmReleaseDateChange(Base):
    """Append-only history of **displayable** release dates being set or moved, written by the
    release-date rebuild in `ingest.tmdb.release_date_history` (NEU-1121).

    `catalog.film_release_date` is delete-and-rebuilt on every ingest, so it holds no memory of
    a date having moved — only where it stands now. This table is that memory, and it is what
    lets a release date card as an event.

    It exists because the *primary* date could not do the job. `film.release_date` gets change
    history free from the `film_field_change` trigger, and that is what release-date events used
    to card from — but the primary is the earliest release in any country of any type, while the
    film page shows US-or-origin theatrical dates only. The two are different quantities, so the
    card routinely cited a date the page never displayed. Only rows passing
    `catalog.release_grade.is_displayable_release` are recorded here; anything else is noise the
    site would never show.

    Like `film_credit_change`, and unlike the trigger, it has to earn two properties explicitly:

    - **First observation is a baseline, never a change** (spec §5.3), keyed on the durable
      `film.release_dates_observed_at` rather than on the rows being absent.
    - **Withdrawals are not recorded.** A date disappearing leaves the page with one fewer line
      and nothing to render a card about.
    """

    __tablename__ = "film_release_date_change"
    __table_args__ = (
        Index("ix_catalog_film_release_date_change_lookup", "film_id", "changed_at"),
        {"schema": "catalog"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.film.id", ondelete="CASCADE"),
        nullable=False,
    )
    iso_3166_1: Mapped[str] = mapped_column(Text, nullable=False)
    release_type: Mapped[int] = mapped_column(Integer, nullable=False)
    """TMDB release `type`; always 2 (limited) or 3 (wide) — `THEATRICAL_RELEASE_TYPES`.
    Together with `iso_3166_1` this is the *subject*: US limited and US wide are two subjects
    on one film, and a distributor can move one without the other."""
    previous_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """NULL when the change is `set` — there was no prior date for this subject."""
    new_date: Mapped[date] = mapped_column(Date, nullable=False)
    change: Mapped[str] = mapped_column(Text, nullable=False)
    """`set` or `moved`."""
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# --- Film column-change history trigger -------------------------------------
# Volatile columns TMDB churns on nearly every ingest — excluded so the history
# table records only semantic changes (release_date, status, title, runtime, ...).
# `credits_observed_at` is excluded for the other reason: it is ingest bookkeeping
# rather than a property of the film, and a history row for it would make a film
# look active to `dormant_film_clause` on the day it was admitted.
FILM_FIELD_CHANGE_DENYLIST: tuple[str, ...] = (
    "popularity",
    "vote_average",
    "vote_count",
    "revenue",
    "tmdb_raw",
    "updated_at",
    "credits_observed_at",
    "release_dates_observed_at",
    "tmdb_missing_at",
)


def _denylist_sql_array(cols: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{c}'" for c in cols) + "]::text[]"


# asyncpg's extended query protocol (used by both the test engine and Alembic's
# async engine) rejects a single execute() call containing more than one top-level
# SQL command ("cannot insert multiple commands into a prepared statement"). The
# CREATE FUNCTION body's internal semicolons are fine (they're inside a single
# dollar-quoted statement) — but the DROP/CREATE TRIGGER statements that follow it
# must be issued as separate execute() calls. We keep each command as its own
# string and compose the public INSTALL/DROP tuples below from them, so there is
# exactly one place each statement's text is written.
_CREATE_FIELD_CHANGE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION catalog.log_film_field_change() RETURNS trigger AS $$
DECLARE
    o jsonb := to_jsonb(OLD);
    n jsonb := to_jsonb(NEW);
    k text;
BEGIN
    FOR k IN SELECT jsonb_object_keys(n) LOOP
        IF k = ANY({_denylist_sql_array(FILM_FIELD_CHANGE_DENYLIST)}) THEN
            CONTINUE;
        END IF;
        IF o -> k IS DISTINCT FROM n -> k THEN
            INSERT INTO catalog.film_field_change (film_id, field, old_value, new_value)
            VALUES (NEW.id, k, o -> k, n -> k);
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DROP_FIELD_CHANGE_TRIGGER_SQL = "DROP TRIGGER IF EXISTS film_field_change_trg ON catalog.film;"

_CREATE_FIELD_CHANGE_TRIGGER_SQL = """
CREATE TRIGGER film_field_change_trg
    BEFORE UPDATE ON catalog.film
    FOR EACH ROW EXECUTE FUNCTION catalog.log_film_field_change();
"""

_DROP_FIELD_CHANGE_FUNCTION_SQL = "DROP FUNCTION IF EXISTS catalog.log_film_field_change();"

# Tuple of per-statement DDL (not a single joined string) because asyncpg's extended
# query protocol cannot run multiple top-level commands in one execute() call — Task
# 3's Alembic migration must call op.execute() once per element, e.g.
# `for stmt in INSTALL_FILM_FIELD_CHANGE_TRIGGER: op.execute(stmt)`.
INSTALL_FILM_FIELD_CHANGE_TRIGGER: tuple[str, ...] = (
    _CREATE_FIELD_CHANGE_FUNCTION_SQL,
    _DROP_FIELD_CHANGE_TRIGGER_SQL,
    _CREATE_FIELD_CHANGE_TRIGGER_SQL,
)

DROP_FILM_FIELD_CHANGE_TRIGGER: tuple[str, ...] = (
    _DROP_FIELD_CHANGE_TRIGGER_SQL,
    _DROP_FIELD_CHANGE_FUNCTION_SQL,
)


def _register_ddl(event_name: str, statements: tuple[str, ...]) -> None:
    for stmt in statements:
        event.listen(Film.__table__, event_name, DDL(stmt))


# Install under create_all (test DB). Prod installs the same commands via the
# Alembic migration's op.execute (Task 3). before_drop keeps metadata.drop_all
# symmetric. Each command is registered as its own DDL/event so a single
# op.execute()/connection.execute() never receives more than one SQL statement
# (see the asyncpg note above).
_register_ddl("after_create", INSTALL_FILM_FIELD_CHANGE_TRIGGER)
_register_ddl("before_drop", DROP_FILM_FIELD_CHANGE_TRIGGER)
