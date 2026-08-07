"""The retrieval recall oracle: is the labeled film in the candidate set?

Recall is a pure, deterministic question — no LLM call, no tokens — so it is a test rather
than a script, and it runs on every commit instead of when someone remembers to spend
money on it. What it guards is silent degradation: normalization and scoring keep moving
through M3's tuning, and a change that quietly stops reaching a film would otherwise
surface only in shadow telemetry weeks later.

**The catalog is a fixture, not the dev database.** `retrieval_catalog.json` carries the
titles of exactly the films `validation_set.json` labels, exported once by
`scripts/export_retrieval_catalog.py`. Scoring against whatever happens to be ingested
locally would make the floor unreproducible — and the fixture is a point-in-time snapshot
whose films go inactive as they reach release (see the fixture README), so nine of the
thirty-three are already unretrievable against a live catalog. Seeding every fixture film
with a future release date removes the as-of date from the measurement entirely.

**T and K are read from the module defaults, deliberately.** M3 retunes them against
shadow data, and this gate is meant to re-measure at whatever they become: a retune that
costs recall is exactly the silent degradation it exists to catch. The floor is therefore
pinned to the constants of the day rather than to a frozen pair, and the failure message
names the ones in force.

**What this does not measure.** A catalog holding only the labeled films has no
distractors, so it cannot see a precision regression, and `DEFAULT_CANDIDATE_LIMIT` never
binds at thirty-three films. Precision is the offline F1 gate's job (§5), and cap
saturation is a shadow-telemetry signal (§4.5). This gate answers recall alone, which is
the one thing that is free to answer.
"""

from datetime import date
from uuid import UUID

from tests.fixtures.link_retrieval import about_items, labeled_tmdb_id, retrieval_catalog
from upmovies.catalog.models import Film, FilmAlternativeTitle
from upmovies.link.retrieval.index import IndexedFilm, build_candidate_index
from upmovies.link.retrieval.select import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    CandidateSet,
    select_candidates,
)
from upmovies.link.validation import ValidationItem

# The floor is the repaired fixture's measured result: every labeled film is retrieved.
# No slack — a regression to 92/93 is exactly what this test exists to catch, and the
# corpus demonstrably sustains 100% at the current constants.
RECALL_FLOOR = 1.0

# The corpus the floor was measured over, 2026-08-07. A ratio alone cannot tell a fixed
# retriever from a shrunken denominator: dropping the rows that miss would also read as
# 100%. The fixture may legitimately grow, so this is a floor rather than an equality.
MEASURED_ABOUT_ITEMS = 93


async def _seed_catalog(session) -> dict[int, UUID]:
    """Insert the fixture catalog and return its tmdb id → film id map.

    Every film is dated a year out on purpose. `active_film_clause` gates the index, and
    the fixture's real release dates have been passing steadily since it was labeled — so
    seeding the real dates would decay this test's floor as a side effect of the calendar
    rather than of any change to retrieval. Scope filtering is covered by `test_index.py`;
    here it would only be noise."""
    catalog = retrieval_catalog()
    active_on = date(date.today().year + 1, 7, 15)
    films = [
        Film(
            tmdb_id=entry.tmdb_id,
            title=entry.title,
            original_title=entry.original_title,
            release_date=active_on,
        )
        for entry in catalog
    ]
    session.add_all(films)
    await session.flush()
    by_tmdb_id = {film.tmdb_id: film.id for film in films}
    session.add_all(
        [
            FilmAlternativeTitle(film_id=by_tmdb_id[entry.tmdb_id], title=title, iso_3166_1="US")
            for entry in catalog
            for title in entry.alternative_titles
        ]
    )
    await session.commit()
    return by_tmdb_id


def _describe_miss(item: ValidationItem, film: IndexedFilm | None, candidates: CandidateSet) -> str:
    """One miss, named, and said which of the three ways it failed.

    A film that scored but was not offered was cut by the cap — a ranking problem, fixed
    by K. A film that never scored is a lexical miss — a normalization or tokenization
    problem, and K will not touch it (§4.5). A film absent from the index is neither: the
    fixtures have drifted. Conflating them sends the next reader after the wrong
    constant."""
    where = f"  {item.title!r}\n    expected "
    if film is None:
        return (
            f"{where}tmdb {labeled_tmdb_id(item)} — not in the index at all; "
            "the fixture catalog and the validation set have drifted"
        )
    score = candidates.score_of(film.film_id)
    if score is None:
        why = f"never scored ≥ T — a lexical miss ({candidates.over_threshold} film(s) cleared T)"
    else:
        why = (
            f"scored {score:.3f} but ranked outside the cap "
            f"({candidates.over_threshold} film(s) cleared T)"
        )
    return f"{where}{film.title!r} (tmdb {labeled_tmdb_id(item)}) — {why}"


async def test_retrieval_recall_over_labeled_about_items_clears_the_floor(session):
    by_tmdb_id = await _seed_catalog(session)
    index = await build_candidate_index(session)
    items = about_items()

    assert len(items) >= MEASURED_ABOUT_ITEMS, (
        f"the validation set has shrunk to {len(items)} 'about' rows from the "
        f"{MEASURED_ABOUT_ITEMS} the floor was measured over — a smaller corpus makes "
        "the recall figure weaker, not better"
    )

    misses = []
    for item in items:
        candidates = select_candidates(index, headline=item.title, dek=item.summary)
        expected = by_tmdb_id.get(labeled_tmdb_id(item))
        if expected is None or candidates.rank_of(expected) is None:
            misses.append(
                _describe_miss(item, index.film(expected) if expected else None, candidates)
            )

    recall = (len(items) - len(misses)) / len(items)
    # Naming the misses is the point. A bare ratio going red says nothing about whether
    # normalization broke, the cap bit, or a label moved.
    assert recall >= RECALL_FLOOR, (
        f"retrieval recall {len(items) - len(misses)}/{len(items)} = {recall:.3f} "
        f"is below the floor {RECALL_FLOOR:.3f} "
        f"(T={DEFAULT_SCORE_THRESHOLD}, K={DEFAULT_CANDIDATE_LIMIT}, "
        f"{index.size} films)\nmisses:\n" + "\n".join(misses)
    )
