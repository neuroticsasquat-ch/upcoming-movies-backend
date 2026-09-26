"""Franchise history: the one row the `film_field_change` trigger cannot write (EF-4).

The franchise half needs no history table. `film.collection_id` is a plain `catalog.film`
column outside `FILM_FIELD_CHANGE_DENYLIST`, so `film_field_change_trg` has recorded every
change to it since the trigger shipped, and `ingest.sweep.collection_events` reads those rows
straight. This module exists for the one transition the trigger structurally cannot see: the
trigger is `BEFORE UPDATE`, so a film **inserted** already belonging to a collection writes no
history at all.

That is ordinarily right — a first observation is a baseline (ADR-0014) — and it is wrong for
exactly the case EF-4 names. Somebody follows a franchise to hear when a film joins it, and
the most common way a film joins is by entering the catalog already in it: the sweep admits it
through the `followed` tranche, or a seed tranche, or a story, with `belongs_to_collection`
already set. Left alone, the beat the follow was made for is the one beat that never cards.

So the admission path writes the row the trigger would have written on an update —
`field='collection_id'`, `NULL -> id` — and writes it **only when both hold**: the upsert
inserted the film row, and somebody follows that collection. A film *updated* into a
collection is the trigger's business already, and a film inserted into a collection nobody
follows is a baseline like every other.

The row is indistinguishable from a trigger-written one, on purpose (D-1436.4). NEU-1434's
reader maps `NULL -> id` to `collection_attached`, quarantines it, and checks at publication
that the film still holds the collection, so a followed franchise's admission cards on the
same clock as every other franchise attachment — one carding rule, not two. The cost of that
reuse is a coupling worth naming: if `film_field_change_trg` is ever rewritten to fire on
insert, this row becomes a duplicate and has to be removed with it.

Rejected alternatives: carding directly from the admission path (two carding rules for one
beat), and a third history table beside `film_credit_change` and `film_company_change` (forks
the path NEU-1434 deliberately did not take).
"""

from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_franchises
from upmovies.catalog.models import COLLECTION_FIELD, FilmFieldChange


async def load_followed_franchise_ids(session: AsyncSession) -> set[int]:
    """The collections somebody follows — read only to answer the admission question below.

    `ingest.tmdb.company_history.load_followed_company_ids` for franchises, and read even more
    rarely: only when an upsert actually inserted a film that arrived with a collection.

    The follow's `entity_type` is `franchise` and the catalog's column is `collection_id` —
    the glossary keeps both words, so this returns collection ids under the franchise's name.
    """
    return set((await session.execute(followed_franchises())).scalars().all())


async def record_collection_admission(
    session: AsyncSession, film_id: UUID, collection_id: int | None, *, film_inserted: bool
) -> None:
    """Write the synthetic `NULL -> collection_id` history row for a newly admitted film in a
    followed franchise, if it is one. Pure DB I/O — the caller commits.

    The three guards are in cost order, so the follow query runs only for a film that is
    actually a new admission into some collection:

    - `film_inserted` — an *update* into a collection is the trigger's row to write, and
      writing a second one here would card the move twice. It comes from the upsert's own
      pre-select rather than from `credits_observed_at` or `companies_observed_at`: those are
      per-payload-section markers with their own backfill histories, and the collection has no
      marker of its own.
    - `collection_id is not None` — a film admitted outside every franchise has nothing to
      record.
    - the follow — everything else on a new film stays the baseline it has always been.

    `changed_at` is left to the column's `now()` default, as the trigger leaves it.

    One accepted race: `film_inserted` is the pre-select's answer while the write is
    `ON CONFLICT DO UPDATE`, so two ingests admitting the same film at once can both read
    "not there" and one of them will then take the update branch — writing this row *and*
    firing the trigger, and carding the arrival twice. It needs two concurrent first ingests
    of one film, which the sweep's per-film sessions do not produce, and the alternative
    (reading `xmax` off the `RETURNING`) buys a rarer failure with a more obscure statement.
    """
    if not film_inserted or collection_id is None:
        return
    if collection_id not in await load_followed_franchise_ids(session):
        return
    await session.execute(
        insert(FilmFieldChange).values(
            film_id=film_id,
            field=COLLECTION_FIELD,
            old_value=None,
            new_value=collection_id,
        )
    )
