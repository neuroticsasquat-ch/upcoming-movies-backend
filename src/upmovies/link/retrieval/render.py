"""Candidate → prompt payload. Pure — no I/O, no session.

A retrieved film is shown to the classifier as a small JSON object carrying **more** than
the roster line it replaces: the roster's title, year, original title, genres and overview,
**plus director, top-3 billed cast, and collection name**. At ~1,200 films those extra
fields were unaffordable; at a measured mean of 4.2 candidates per story a rendered candidate
costs ~102 tokens and a story's whole candidate list ~360, against the ~46k-token prefix they
replace (spec §4.3, §5.1, §5.2).

The richer rendering is not a bonus — it is the counterweight to the risk the whole project
runs. Narrowing the candidate set **hurts precision**: shown one film and a headline about
that film's untracked sibling, the model has nothing correct to point at. Director, cast and
collection are what let it tell a tracked entry from its franchise siblings, which is why
they are carried through `IndexedFilm` even though ADR-0008 measured them as worthless to
the *score*.

**Numbering is per story and starts at 1.** Index `n` in one story's list is a different
film from index `n` in another's, which is the whole difference from the global roster
index — a reply naming an index outside a story's own list is rejectable rather than
resolvable (NEU-999). Only the candidates under the cap are numbered: a film the cap
discarded is not shown, so it must not be nameable.

**Scores are not rendered.** They are retrieval-health telemetry; putting them in front of
the classifier would invite it to defer to the lexical ranking it exists to check.
"""

from typing import Any

from upmovies.link.retrieval.index import IndexedFilm
from upmovies.link.retrieval.select import CandidateSet

# Matches the roster's trim, so the cutover does not change what the model reads about a
# film at the same time as it changes which films it reads about. NEU-1001 weighed widening
# it and **kept 120**: 120 characters is already ~30 of the ~102 tokens a candidate costs,
# so tripling it would add ~29% to every request to buy something no free measurement can
# price — whether a longer overview helps the classifier is an F1 question, and answering it
# means paying for a `validate_linking` run. Worse, moving it now would move the roster
# path's baseline at the same time, which is the one comparison the cutover gate rests on.
# Revisit after the cutover, when the roster is gone and only one path moves.
OVERVIEW_MAX = 120


def render_candidate(n: int, film: IndexedFilm) -> dict[str, Any]:
    """One numbered candidate as the classifier sees it.

    Empty fields are dropped rather than sent as nulls: a null tells the model nothing and
    the key still costs tokens on every candidate of every story in the batch."""
    rendered: dict[str, Any] = {"n": n, "title": film.title}
    if film.original_title and film.original_title != film.title:
        rendered["original_title"] = film.original_title
    if film.year is not None:
        rendered["year"] = film.year
    if film.director:
        rendered["director"] = film.director
    if film.cast:
        rendered["cast"] = list(film.cast)
    if film.collection:
        rendered["collection"] = film.collection
    if film.genres:
        rendered["genres"] = list(film.genres)
    if film.overview:
        rendered["overview"] = film.overview[:OVERVIEW_MAX]
    return rendered


def render_candidates(candidate_set: CandidateSet) -> list[dict[str, Any]]:
    """A story's offered candidates, numbered from 1 in the order they were offered."""
    return [
        render_candidate(n, candidate.film)
        for n, candidate in enumerate(candidate_set.candidates, start=1)
    ]
