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


# --- Film column-change history trigger -------------------------------------
# Volatile columns TMDB churns on nearly every ingest — excluded so the history
# table records only semantic changes (release_date, status, title, runtime, ...).
FILM_FIELD_CHANGE_DENYLIST: tuple[str, ...] = (
    "popularity",
    "vote_average",
    "vote_count",
    "revenue",
    "tmdb_raw",
    "updated_at",
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
