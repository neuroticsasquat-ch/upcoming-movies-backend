"""`Event.subject_key`: the entities an event is *about* — normalized person names, and
`company:<tmdb_id>` / `collection:<tmdb_id>` tokens for the studio and franchise halves (EF-5).

Both paths that card a person write it, and both read it before carding one — a casting
group the cluster stage forms from trade stories, and a credit attachment the sweep reads
out of `catalog.film_credit_change` (ADR-0014). That is the whole reason it lives here
rather than in either: two normalizations would be two different answers to "have we
already carded this person", and the duplicate card would only show up on the feed.
"""

import unicodedata
from collections.abc import Collection
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import Event


def normalize_name(name: str) -> str:
    """Deterministic casting-identity key: NFKC-fold, casefold, collapse whitespace.
    String-based (not TMDB-person-id) — imperfect on aliases/typos, but stable and
    dependency-free, which fits breaking-cast news where TMDB credits lag."""
    folded = unicodedata.normalize("NFKC", name).casefold().strip()
    return " ".join(folded.split())


_COMPANY_SUBJECT_PREFIX = "company:"
_COLLECTION_SUBJECT_PREFIX = "collection:"


def _prefixed_ids_in(subject_key: list[str] | None, prefix: str) -> list[int]:
    """The TMDB ids a card's `subject_key` carries under one prefix, in the order it holds
    them.

    Tolerant of every other token, because one column carries them all: a mixed key is read
    for the prefix asked about and nothing else. A token whose remainder is not an integer is
    skipped rather than raised on — this is read on the delivery path, where one malformed row
    must not cost a whole query.
    """
    ids: list[int] = []
    for token in subject_key or []:
        if not token.startswith(prefix):
            continue
        try:
            ids.append(int(token[len(prefix) :]))
        except ValueError:
            continue
    return ids


def company_subject_token(company_id: int) -> str:
    """The `Event.subject_key` token one production company carries: `company:<tmdb_id>`.

    An id rather than a normalized name, which is the one place the studio half deliberately
    departs from the person half. `normalize_name` exists because trade stories name people
    days before TMDB holds a credit for them, so the only identity both paths share is the
    string. A company change is read out of `catalog.film_company_change`, which carries
    TMDB's own id — and studio names are exactly the strings that would fold together wrongly
    ("Warner Bros. Pictures" and "Warner Bros. Animation" are one company under no
    normalization worth having, and two under every correct one).

    Prefixed for the reason `youtube:<key>` is: `subject_key` is one column shared by every
    event type, and a bare `20` beside a casting card's names would be ambiguous on its face.
    """
    return f"{_COMPANY_SUBJECT_PREFIX}{company_id}"


def company_ids_in(subject_key: list[str] | None) -> list[int]:
    """The company ids a card's `subject_key` names, in the order it holds them."""
    return _prefixed_ids_in(subject_key, _COMPANY_SUBJECT_PREFIX)


def collection_subject_token(collection_id: int) -> str:
    """The `Event.subject_key` token one TMDB collection carries: `collection:<tmdb_id>`.

    An id rather than a normalized name, for `company_subject_token`'s reason and one more of
    its own: a collection is read out of `catalog.film_field_change`, which records the
    `collection_id` column itself, so the id is the only identity the change ever had. It is
    also the identity a *move* needs — `id -> id'` cards a departure and an arrival that must
    name two different franchises, and two collections can share a display name where they
    cannot share an id.
    """
    return f"{_COLLECTION_SUBJECT_PREFIX}{collection_id}"


def collection_ids_in(subject_key: list[str] | None) -> list[int]:
    """The collection ids a card's `subject_key` names, in the order it holds them."""
    return _prefixed_ids_in(subject_key, _COLLECTION_SUBJECT_PREFIX)


async def recorded_subject_names(
    session: AsyncSession, *, film_id: UUID, event_types: Collection[str]
) -> set[str]:
    """Every normalized name this film's events of `event_types` already represent.

    A dedicated query rather than a read off the attach-window candidate set: "have we
    carded this person" is a question about the film's whole history, and an event that
    aged out of the window is still a card the person is on.
    """
    rows = (
        (
            await session.execute(
                select(Event.subject_key).where(
                    Event.film_id == film_id,
                    Event.event_type.in_(event_types),
                    Event.subject_key.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    names: set[str] = set()
    for key in rows:
        names.update(key or [])
    return names
