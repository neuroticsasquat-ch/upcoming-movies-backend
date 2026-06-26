from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
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
