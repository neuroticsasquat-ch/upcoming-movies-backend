"""`Event.subject_key`: the normalized person names an event is *about*.

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
