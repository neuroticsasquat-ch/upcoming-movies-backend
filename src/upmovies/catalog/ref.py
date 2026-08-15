"""Public film URL refs: `<tmdb_id>-<slug-of-current-title>`.

A ref resolves on its **leading id only**; everything after the first hyphen is decorative and
derived from the film's current title at read time, so it can never go stale the way a stored
slug does. `/film/1061474-anything-at-all` and `/film/1061474` both reach the same film — the
caller is expected to redirect to the canonical form.

This is deliberately *not* `catalog.slug`. That module still owns `film.slug`, which stays
immutable and now exists only to resolve URLs minted before this scheme (NEU-1143).

The decorative half carries **no release year**, unlike `base_slug`. Release years move
constantly for upcoming films — that is the domain — and every move would churn the canonical
URL and mint another redirect. A title changes far less often than its date.
"""

import re

from slugify import slugify

_LEADING_ID = re.compile(r"^(\d+)(?:-|$)")


def film_ref(tmdb_id: int, title: str) -> str:
    """The canonical URL ref for a film. Falls back to the bare id when the title has no
    slugifiable stem (untransliterable or all-punctuation), which is still a valid ref."""
    stem = slugify(title)
    return f"{tmdb_id}-{stem}" if stem else str(tmdb_id)


def parse_film_ref(ref: str) -> int | None:
    """The `tmdb_id` a ref addresses, or None when it does not lead with a number.

    This is a *candidate*, not an answer. Legacy slugs are `<title>-<year>`, and a numeric title
    produces one that reads exactly like a ref: the film "1917" is slugged `1917-2019`, which
    parses here as id 1917 — a real, different film. So the resolver must try the legacy slug
    too and let an exact slug match win; see `get_film_detail`. Returning the candidate and
    resolving the ambiguity at the query is the only honest split, because nothing about the
    string itself distinguishes the two cases.
    """
    match = _LEADING_ID.match(ref)
    return int(match.group(1)) if match else None
