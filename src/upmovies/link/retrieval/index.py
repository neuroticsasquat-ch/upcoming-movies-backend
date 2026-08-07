"""The in-memory token → film candidate index. Built once per run, then pure lookups.

Mirrors what `link/roster.py` does today — one read of the active catalog — minus the
rendering, and minus sending any of it to the model. `build_candidate_index` is the only
thing here that touches the database; everything below it (`build_index`, `indexed_film`,
and every lookup on `CandidateIndex`) is pure, so the scorer in `select.py` and the recall
oracle test can work against a hand-built index with no session at all.

**Search scope is unchanged.** The same `active_film_clause` the roster uses gates the
index: released and canceled films stay out. That is a product decision about what the
site tracks, not a prefix-size workaround, so shrinking the prompt does not get to widen it.

**Only titles are indexed** — `film.title`, `film.original_title`, and
`catalog.film_alternative_title`. Collection name and talent are deliberately absent from
the *score*: the ablation in ADR-0008 measured each one adding candidate-set size for zero
recall gain. They are still carried as display fields, because narrowing the candidate set
costs precision and the rendering pays that back by telling the model more about each of
the few films it does see.

**Two structures, because token overlap alone has a known hole.** A film whose title the
headline spells differently — tracked "Nagabandham" against "Naga Bandham Movie Trailer
Launch" — shares no token with the story, so the inverted index cannot reach it at any
threshold. `rescue_folds` is the second structure: every title's squash-fold, pre-filtered
to those long enough to match safely, for the substring rescue that turns that miss into a
hit (ADR-0008).

**Scale.** At a 20x catalog (~24k films) this is a larger read, but it is still once per
run, not per story, and scoring stays proportional to the tokens in a headline rather than
to catalog size. The build logs its duration and row count on purpose: that measurement is
the trigger for reconsidering the Postgres route, which is closed today only because
pgvector is unavailable and `pg_trgm` is not installed on the shared instance.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Row, ScalarSelect, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmCredit,
    FilmGenre,
    Genre,
    Person,
)
from upmovies.catalog.queries import active_film_clause
from upmovies.config import get_settings
from upmovies.link.retrieval.normalize import (
    SQUASH_FOLD_MIN_CHARS,
    significant_tokens,
    squash_fold,
)

log = logging.getLogger(__name__)

# Billed cast carried for rendering. Three is what the candidate rendering was costed at:
# ~320 tokens per story at a p90 of four candidates, against a ~50k-token roster prefix.
# The whole cast would be unaffordable; three names is enough to tell sibling films apart.
_CAST_LIMIT = 3


@dataclass(frozen=True)
class SearchableTitle:
    """One title a film can be found by, with both normalizations precomputed.

    Computed at build time rather than per story: a title is normalized once per run,
    a headline once per story."""

    text: str
    tokens: tuple[str, ...]
    folded: str


@dataclass(frozen=True)
class FoldedTitle:
    """A squash-folded title long enough to be substring-matched, and its film."""

    folded: str
    film_id: UUID


@dataclass(frozen=True)
class IndexedFilm:
    """A film as retrieval sees it: what it can be found by, and what renders it.

    The display fields are carried here so that rendering a candidate needs no second
    round-trip — the whole point of reading the catalog once per run. `overview` is
    carried in full; trimming it is a prompt-size decision that belongs to rendering."""

    film_id: UUID
    title: str
    original_title: str | None
    titles: tuple[SearchableTitle, ...]
    year: int | None = None
    overview: str | None = None
    genres: tuple[str, ...] = ()
    director: str | None = None
    cast: tuple[str, ...] = ()
    collection: str | None = None


@dataclass(frozen=True)
class CandidateIndex:
    """Token → film ids, plus the folded titles for substring rescue. Pure lookups."""

    films: tuple[IndexedFilm, ...]
    rescue_folds: tuple[FoldedTitle, ...]
    token_films: Mapping[str, frozenset[UUID]] = field(repr=False)
    films_by_id: Mapping[UUID, IndexedFilm] = field(repr=False)

    @property
    def size(self) -> int:
        """The number of indexed films — the row count worth logging and alerting on."""
        return len(self.films)

    @property
    def token_count(self) -> int:
        """The number of distinct tokens indexed — the other half of the build's shape."""
        return len(self.token_films)

    def film(self, film_id: UUID) -> IndexedFilm | None:
        """The indexed film for `film_id`, or None if it is not in the active set."""
        return self.films_by_id.get(film_id)

    def films_for_token(self, token: str) -> frozenset[UUID]:
        """Every film carrying `token` in one of its searchable titles."""
        return self.token_films.get(token, frozenset())

    def films_for_tokens(self, tokens: Iterable[str]) -> set[UUID]:
        """The union over `tokens` — the films worth scoring for a story.

        A union, not an intersection: scoring is the fraction of a *title's* tokens
        present in the story, so a film matching one token of three is a legitimate
        low-scoring candidate for the threshold to judge, not a non-candidate."""
        matched: set[UUID] = set()
        for token in tokens:
            matched |= self.token_films.get(token, frozenset())
        return matched


def _searchable_titles(
    title: str, original_title: str | None, alternative_titles: Sequence[str]
) -> tuple[SearchableTitle, ...]:
    """The distinct titles a film can be found by, in title → original → alternatives order.

    Deduplicated on the fold *and* the tokens together, not on the fold alone: TMDB
    routinely repeats the title as its own alternative title, but two spellings can share
    a fold while tokenizing differently — tracked `"Nagabandham"` beside the alternative
    `"Naga Bandham"` both fold to `nagabandham`, yet only the second carries the tokens
    `naga` and `bandham`. Deduplicating on the fold alone would drop that second form, and
    with it the only way the token scorer can reach a headline that spells the title with
    the space. The fold rescues that particular pair anyway, but only because it clears
    `SQUASH_FOLD_MIN_CHARS`; a shorter pair (`"Sunup"` / `"Sun Up"`, folding to five
    characters) would have no rescue to fall back on and would go unreachable."""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    titles: list[SearchableTitle] = []
    for text in (title, original_title, *alternative_titles):
        if not text:
            continue
        folded = squash_fold(text)
        tokens = tuple(significant_tokens(text))
        if (folded, tokens) in seen:
            continue
        seen.add((folded, tokens))
        titles.append(SearchableTitle(text=text, tokens=tokens, folded=folded))
    return tuple(titles)


def indexed_film(
    *,
    film_id: UUID,
    title: str,
    original_title: str | None = None,
    alternative_titles: Sequence[str] = (),
    year: int | None = None,
    overview: str | None = None,
    genres: Sequence[str] = (),
    director: str | None = None,
    cast: Sequence[str] = (),
    collection: str | None = None,
) -> IndexedFilm:
    """Build an `IndexedFilm`, normalizing its titles. The only way one is constructed."""
    return IndexedFilm(
        film_id=film_id,
        title=title,
        original_title=original_title,
        titles=_searchable_titles(title, original_title, alternative_titles),
        year=year,
        overview=overview,
        genres=tuple(genres),
        director=director,
        cast=tuple(cast),
        collection=collection,
    )


def build_index(films: Iterable[IndexedFilm]) -> CandidateIndex:
    """Invert `films` into the token map and the rescue folds. Pure — no I/O."""
    by_token: defaultdict[str, set[UUID]] = defaultdict(set)
    rescue_folds: list[FoldedTitle] = []
    seen_folds: set[tuple[str, UUID]] = set()
    ordered: list[IndexedFilm] = []
    for film in films:
        ordered.append(film)
        for title in film.titles:
            for token in title.tokens:
                by_token[token].add(film.film_id)
            # Folds shorter than the guard would match inside unrelated words, so they
            # are withheld here rather than re-checked against every story.
            if len(title.folded) < SQUASH_FOLD_MIN_CHARS:
                continue
            # Two of a film's titles can tokenize differently yet share a fold, and both
            # are kept as searchable forms — but the fold only needs offering once.
            if (title.folded, film.film_id) in seen_folds:
                continue
            seen_folds.add((title.folded, film.film_id))
            rescue_folds.append(FoldedTitle(folded=title.folded, film_id=film.film_id))
    return CandidateIndex(
        films=tuple(ordered),
        rescue_folds=tuple(rescue_folds),
        token_films={token: frozenset(ids) for token, ids in by_token.items()},
        films_by_id={f.film_id: f for f in ordered},
    )


async def build_candidate_index(session: AsyncSession) -> CandidateIndex:
    """Read the active catalog once and build the index over it.

    Child tables are read as separate keyed queries rather than one wide join: joining
    alternative titles, genres and credits onto the same statement multiplies rows by the
    product of their cardinalities, which is a worse trade than four extra round-trips
    made once per run."""
    started = time.perf_counter()
    excluded = get_settings().tmdb_excluded_statuses
    today = datetime.now(UTC).date()
    # Built once and shared by every query below, so the child reads can never disagree
    # with the film read about what "active" means.
    is_active = active_film_clause(today=today, excluded_statuses=excluded)
    active_ids = select(Film.id).where(is_active).scalar_subquery()

    film_rows = (
        await session.execute(
            select(Film, Collection.name)
            .outerjoin(Collection, Collection.id == Film.collection_id)
            .where(is_active)
            .order_by(Film.title)
        )
    ).all()

    alt_titles = _group(
        (
            await session.execute(
                select(FilmAlternativeTitle.film_id, FilmAlternativeTitle.title)
                .where(FilmAlternativeTitle.film_id.in_(active_ids))
                .order_by(FilmAlternativeTitle.film_id, FilmAlternativeTitle.id)
            )
        ).all()
    )
    genres = _group(
        (
            await session.execute(
                select(FilmGenre.film_id, Genre.name)
                .join(Genre, Genre.id == FilmGenre.genre_id)
                .where(FilmGenre.film_id.in_(active_ids))
                .order_by(FilmGenre.film_id, Genre.name.asc(), Genre.id.asc())
            )
        ).all()
    )
    directors = _group(
        (
            await session.execute(
                select(FilmCredit.film_id, Person.name)
                .join(Person, Person.id == FilmCredit.person_id)
                .where(
                    FilmCredit.film_id.in_(active_ids),
                    FilmCredit.credit_type == "crew",
                    FilmCredit.job == "Director",
                )
                .order_by(
                    FilmCredit.film_id,
                    nulls_last(FilmCredit.credit_order.asc()),
                    Person.name.asc(),
                )
            )
        ).all()
    )
    cast = _group(await _top_billed_cast(session, active_ids))

    films = [
        indexed_film(
            film_id=film.id,
            title=film.title,
            original_title=film.original_title,
            alternative_titles=alt_titles.get(film.id, []),
            year=film.release_date.year if film.release_date else None,
            overview=film.overview,
            genres=genres.get(film.id, []),
            director=", ".join(directors[film.id]) if film.id in directors else None,
            cast=cast.get(film.id, []),
            collection=collection_name,
        )
        for film, collection_name in film_rows
    ]
    index = build_index(films)
    log.info(
        "retrieval index built: films=%d searchable_titles=%d tokens=%d rescue_folds=%d "
        "duration_ms=%d",
        index.size,
        sum(len(f.titles) for f in index.films),
        index.token_count,
        len(index.rescue_folds),
        round((time.perf_counter() - started) * 1000),
    )
    return index


async def _top_billed_cast(
    session: AsyncSession, active_ids: ScalarSelect[UUID]
) -> Sequence[Row[tuple[UUID, str]]]:
    """The first `_CAST_LIMIT` billed cast names per active film, in billing order."""
    rank = (
        func.row_number()
        .over(
            partition_by=FilmCredit.film_id,
            order_by=(nulls_last(FilmCredit.credit_order.asc()), Person.name.asc()),
        )
        .label("rank")
    )
    ranked = (
        select(FilmCredit.film_id.label("film_id"), Person.name.label("name"), rank)
        .join(Person, Person.id == FilmCredit.person_id)
        .where(FilmCredit.film_id.in_(active_ids), FilmCredit.credit_type == "cast")
        .subquery()
    )
    return (
        await session.execute(
            select(ranked.c.film_id, ranked.c.name)
            .where(ranked.c.rank <= _CAST_LIMIT)
            .order_by(ranked.c.film_id, ranked.c.rank)
        )
    ).all()


def _group(rows: Iterable[Row[tuple[UUID, str]]]) -> dict[UUID, list[str]]:
    """Collapse `(film_id, value)` rows into a per-film list, preserving row order."""
    grouped: dict[UUID, list[str]] = {}
    for film_id, value in rows:
        grouped.setdefault(film_id, []).append(value)
    return grouped
