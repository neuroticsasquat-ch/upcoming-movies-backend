"""Reading back what the resolver decided — the query behind D-25's `/admin/resolution`.

Read-only by design. The page it feeds exists so a human can see *why* a mention went where
it went; it corrects nothing, because a correction surface would have to write `person_id`
back onto a row the next run re-derives from scratch. Unlinked stays unlinked until the
scorer's inputs change.

The rows this returns are the ones `pipeline.py::_write_decision` wrote: `path` set,
`features` carrying both the extraction-time context and the resolver's own working notes
under `resolution`, and `candidates` holding the whole ranked shortlist rather than only the
winner. A near-miss the scorer rejected is the most useful thing on the page.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.link.resolve.scoring import Path
from upmovies.news.models import Story, StoryPerson


class InvalidCursor(ValueError):
    """The `cursor` query parameter was not one this module minted."""


@dataclass(frozen=True)
class DecisionRow:
    """One decided mention with the story it was found in and the film that story is about.

    Assembled here rather than left as three ORM rows so the router does no joining of its
    own: `story` is always present (a mention cannot exist without one), `film` is not — a
    story whose link was later removed keeps its mentions and loses its `film_id`.
    """

    mention: StoryPerson
    story: Story
    film: Film | None


@dataclass(frozen=True)
class DecisionPage:
    """One page of decisions, newest first, plus the cursor that fetches the next one.

    `next_cursor` is None on the last page — which is how a caller knows it has reached the
    end, since a keyset page cannot report a total without a second count over a queue that
    grows while it is being read.
    """

    rows: list[DecisionRow]
    next_cursor: str | None


def encode_cursor(resolved_at: datetime, mention_id: UUID) -> str:
    """Pack a row's sort key into the opaque token the next request sends back.

    Opaque (base64) rather than the raw timestamp: the ordering key is this module's business,
    and a client that learns to construct one will keep constructing it after the key changes.
    """
    raw = f"{resolved_at.isoformat()}|{mention_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Unpack a cursor, or raise `InvalidCursor`.

    Every malformed shape lands on the same exception, because from the caller's side they
    are one mistake: a token this module did not mint. The caller turns that into a 400.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        timestamp, _, mention_id = raw.partition("|")
        resolved_at = datetime.fromisoformat(timestamp)
        if resolved_at.tzinfo is None:
            # `resolved_at` is `timestamptz`; a naive value would compare at whatever instant
            # the driver assumed and quietly page from the wrong place. `encode_cursor` never
            # mints one, so this is a forgery, which is the same mistake as the rest.
            raise ValueError("naive timestamp")
        return resolved_at, UUID(mention_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor(cursor) from exc


def _decided() -> list[ColumnElement[bool]]:
    """What makes a `story_person` row a decision rather than a mention awaiting one.

    `resolved_at` is checked alongside `path` although `_write_decision` always writes the
    two together: no constraint pairs them, and the keyset below orders on `resolved_at`, so
    a NULL slipping through would not merely omit a row — it would silently truncate a page.
    """
    return [StoryPerson.path.is_not(None), StoryPerson.resolved_at.is_not(None)]


async def list_decisions(
    db: AsyncSession, *, path: Path | None = None, limit: int, cursor: str | None = None
) -> DecisionPage:
    """One page of resolver decisions, newest first, optionally narrowed to a single `path`.

    Keyset-paginated on `(resolved_at, id)` rather than offset-paginated like the other admin
    lists, because every daily run writes into this queue while it is being read: with an
    offset, rows arriving above the boundary push unread ones past it, and those are precisely
    the ones the admin has not seen. The keyset makes that class of skip impossible.

    It does not make paging stable against a *re-decision*, which rewrites `resolved_at` and
    moves a row to the front — no cursor over a mutable sort key can, and an offset would
    fare worse. Note too that `resolved_at` is decision time rather than mention time, so a
    run that re-resolves reshuffles the queue; there is no reviewed-marker to work a backlog
    down with, deliberately, because D-25 gives this page nothing to write.

    Omitting `path` returns all four outcomes. It does not return undecided mentions — those
    have no features, no candidates and no decision to inspect, which is the whole of what
    this page renders.
    """
    filters = _decided()
    if path is not None:
        filters.append(StoryPerson.path == path.value)
    if cursor is not None:
        resolved_at, mention_id = decode_cursor(cursor)
        filters.append(tuple_(StoryPerson.resolved_at, StoryPerson.id) < (resolved_at, mention_id))

    # One row beyond the page, to learn whether a next page exists without counting the queue.
    result = await db.execute(
        select(StoryPerson, Story, Film)
        .join(Story, Story.id == StoryPerson.story_id)
        .outerjoin(Film, Film.id == Story.film_id)
        .where(*filters)
        .order_by(StoryPerson.resolved_at.desc(), StoryPerson.id.desc())
        .limit(limit + 1)
    )
    found = [DecisionRow(mention=m, story=s, film=f) for m, s, f in result.all()]

    rows = found[:limit]
    next_cursor = None
    if len(found) > limit and rows:
        last = rows[-1].mention
        assert last.resolved_at is not None  # `_decided()` filtered the NULLs out
        next_cursor = encode_cursor(last.resolved_at, last.id)
    return DecisionPage(rows=rows, next_cursor=next_cursor)
