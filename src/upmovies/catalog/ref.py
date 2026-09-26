"""Public URL refs: `<tmdb_id>-<slug-of-current-name>`, for films and for people.

A ref resolves on its **leading id only**; everything after the first hyphen is decorative and
derived from the film's current title at read time, so it can never go stale the way a stored
slug does. `/film/1061474-anything-at-all` and `/film/1061474` both reach the same film — the
caller is expected to redirect to the canonical form.

This is deliberately *not* `catalog.slug`. That module still owns `film.slug`, which stays
immutable and now exists only to resolve URLs minted before this scheme (NEU-1143).

The decorative half carries **no release year**, unlike `base_slug`. Release years move
constantly for upcoming films — that is the domain — and every move would churn the canonical
URL and mint another redirect. A title changes far less often than its date.

The **person**, **company** and **collection** trio (NEU-1418, NEU-1428) is the same scheme
over `catalog.person`, `catalog.production_company` and `catalog.collection`. They share one
private implementation and keep three named pairs at the call sites, because the name is what
makes a ref readable where it is minted — but the rule itself is one rule and must not drift
three ways.

The **film** pair stays its own: only the film side has a legacy `slug` column to fall back on
(`parse_film_ref`'s whole subtlety), and folding that asymmetry into the shared helper as a
parameter would be a worse way of saying it than the four lines below.
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


def _entity_ref(entity_id: int, name: str) -> str:
    """The canonical URL ref for an entity addressed by its TMDB id. Falls back to the bare id
    when the name has no slugifiable stem, exactly as `film_ref` does — TMDB carries names in
    every script, and a name that transliterates to nothing is still an entity with a page."""
    stem = slugify(name)
    return f"{entity_id}-{stem}" if stem else str(entity_id)


def _parse_entity_ref(ref: str) -> int | None:
    """The entity id a ref addresses, or None when it does not lead with a number.

    Unlike `parse_film_ref` this is an **answer, not a candidate**: none of these tables has a
    legacy slug column, so there is no second resolution path and nothing for a numeric name to
    collide with. A person called "1917" slugs to `<id>-1917`, which still leads with the id.
    """
    match = _LEADING_ID.match(ref)
    return int(match.group(1)) if match else None


def person_ref(person_id: int, name: str) -> str:
    """The canonical URL ref for a person (`catalog.person`)."""
    return _entity_ref(person_id, name)


def parse_person_ref(ref: str) -> int | None:
    """The `catalog.person` id a ref addresses, or None when it does not lead with a number."""
    return _parse_entity_ref(ref)


def company_ref(company_id: int, name: str) -> str:
    """The canonical URL ref for a studio (`catalog.production_company`)."""
    return _entity_ref(company_id, name)


def parse_company_ref(ref: str) -> int | None:
    """The `catalog.production_company` id a ref addresses, or None when it does not lead with
    a number."""
    return _parse_entity_ref(ref)


def collection_ref(collection_id: int, name: str) -> str:
    """The canonical URL ref for a franchise (`catalog.collection`)."""
    return _entity_ref(collection_id, name)


def parse_collection_ref(ref: str) -> int | None:
    """The `catalog.collection` id a ref addresses, or None when it does not lead with a
    number."""
    return _parse_entity_ref(ref)
