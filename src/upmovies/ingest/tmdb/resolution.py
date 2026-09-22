"""Placing a title-and-year on a TMDB film, for the imports that have nothing better to go on.

A Letterboxd export carries no TMDB id (D-15), so every row it holds has to be matched against
`/search/movie` hits by name. Pure over the hit list — no client, no session — because the
interesting half is the *rule*, and a rule that can only be exercised through a mocked HTTP
call and a database is a rule nobody adjusts with confidence.

**Never guesses.** An unmatched row is reported to the user, who can add the film by hand in
seconds; a wrong match is a follow for a film they have never heard of,
and they have no way to tell it came from a bad match rather than a bug. So both rules below
require the folded title to be *equal*, not similar and not a substring — this is deliberately
not `link.retrieval`'s job, where a story mentioning a film is a substring question scored
against a threshold. The two use the same fold and ask different things of it."""

from typing import NamedTuple

from upmovies.ingest.tmdb.schemas import TMDBMovieSummary
from upmovies.link.retrieval import squash_fold

FESTIVAL_YEAR_SLACK = 1
"""How far a hit's year may sit from the CSV's before the rule stops accepting it.

Letterboxd dates a film by its first public screening, so a festival premiere in one year and
a TMDB primary release in the next is the ordinary case, not the exotic one — *Anatomy of a
Fall* is 2023 to Letterboxd and 2023 to TMDB, but a film premiering in a November festival and
opening in February is off by one on every export.

Note what this rule can and cannot reach. `search_movie` sends `primary_release_year`, so TMDB
has already filtered on the year before these hits arrive, and a hit a whole year out is one
TMDB itself considered a match for that year — its primary release in some region falls in the
requested year while the `release_date` it returns does not. That makes this a rescue for
TMDB's own regional disagreements rather than a general widening, which is the right size: a
±1 search would double the request count for every row, and this costs nothing."""


class ResolvedTitle(NamedTuple):
    """The film a row was placed on. `title` is TMDB's, not the CSV's — it is what a later
    reader of the logs needs to see, because the two differing is exactly what a bad match
    would look like."""

    tmdb_id: int
    title: str


def resolve(hits: list[TMDBMovieSummary], *, name: str, year: int | None) -> ResolvedTitle | None:
    """The film `name` (`year`) names among `hits`, or None when nothing matches it.

    Two passes, exact year before slack year, each over the whole hit list: a hit whose year is
    right must win over one that is a year out, whatever order TMDB returned them in.

    A row with no year cannot match at all. Both rules are year-equality rules, and dropping
    the year requirement for the rows that lack one would make those exact rows — the ones with
    the least information — the only ones matched on title alone."""
    if year is None:
        return None
    folded = squash_fold(name)
    if not folded:
        return None
    for slack in (0, FESTIVAL_YEAR_SLACK):
        matches = [h for h in hits if _matches(h, folded=folded, year=year, slack=slack)]
        if matches:
            best = max(matches, key=lambda h: (h.popularity or 0.0, h.id))
            return ResolvedTitle(tmdb_id=best.id, title=best.title)
    return None


def _matches(hit: TMDBMovieSummary, *, folded: str, year: int, slack: int) -> bool:
    """Whether this hit is the film, under a given year slack.

    `original_title` is tried alongside `title` because TMDB's `title` is the localized one:
    a French film is `Anatomy of a Fall` there and `Anatomie d'une chute` in the export of a
    user whose Letterboxd is set to the original titles."""
    if hit.release_date is None or abs(hit.release_date.year - year) > slack:
        return False
    return folded in (squash_fold(hit.title), squash_fold(hit.original_title or ""))
