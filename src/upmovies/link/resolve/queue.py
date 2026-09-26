"""Reading back what the resolver decided — the query behind D-25's `/admin/resolution`.

Read-only by design. The page it feeds exists so a human can see *why* a mention went where
it went; it corrects nothing, because a correction surface would have to write an id back onto
a row the next run re-derives from scratch. Unlinked stays unlinked until the scorer's inputs
change.

The rows this returns are the ones the two resolvers wrote — `path` set, `features` carrying
both the extraction-time context and the resolver's own working notes under `resolution`, and
`candidates` holding the whole ranked shortlist rather than only the winner. A near-miss the
scorer rejected is the most useful thing on the page.

**One page, three kinds** (EF-12). People live in `news.story_person` and organisations in
`news.story_entity`; a caller picks one `kind` per request and the row that comes back is
flattened to the fields both tables share plus the two that are the person table's alone. The
two are *not* unioned into a single page: the tables have different columns and different
keyset orders, and paging across both would have to interleave two cursors to save a filter
the admin page has anyway.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.link.resolve.scoring import Path
from upmovies.news.models import (
    ORGANISATION_KINDS,
    PERSON_KIND,
    Story,
    StoryEntity,
    StoryPerson,
)
from upmovies.pagination import InvalidCursor as InvalidCursor
from upmovies.pagination import decode_cursor as decode_cursor
from upmovies.pagination import encode_cursor as encode_cursor

DECISION_KINDS = (PERSON_KIND, *ORGANISATION_KINDS)
"""The three `kind` values this page lists, person first — which is also the default, so a
caller written against the person-only endpoint keeps the page it had (EF-12)."""


@dataclass(frozen=True)
class DecisionRow:
    """One decided mention with the story it was found in and the film that story is about.

    Flattened off the ORM row rather than handed over as one, so the router does no joining
    and no type-switching of its own: `story_person` and `story_entity` are different tables,
    and a caller that had to ask which one it held would be re-deciding per row what the
    `kind` filter already decided per page.

    `story` is always present (a mention cannot exist without one), `film` is not — a story
    whose link was later removed keeps its mentions and loses its `film_id`. `role` and
    `department` are always None for an organisation: they are person facts, and
    `story_entity` has no column for them.
    """

    id: UUID
    kind: str
    story: Story
    film: Film | None
    name_as_written: str
    role: str | None
    department: str | None
    evidence_span: str | None
    path: str
    entity_id: int | None
    confidence: float | None
    features: dict[str, Any]
    candidates: list[Any]
    resolved_at: datetime


@dataclass(frozen=True)
class DecisionPage:
    """One page of decisions, newest first, plus the cursor that fetches the next one.

    `next_cursor` is None on the last page — which is how a caller knows it has reached the
    end, since a keyset page cannot report a total without a second count over a queue that
    grows while it is being read.
    """

    rows: list[DecisionRow]
    next_cursor: str | None


def _decided(model: type[StoryPerson] | type[StoryEntity]) -> list[ColumnElement[bool]]:
    """What makes a mention row a decision rather than a mention awaiting one.

    `resolved_at` is checked alongside `path` although both resolvers always write the two
    together: no constraint pairs them, and the keyset below orders on `resolved_at`, so a
    NULL slipping through would not merely omit a row — it would silently truncate a page.
    """
    return [model.path.is_not(None), model.resolved_at.is_not(None)]


async def list_decisions(
    db: AsyncSession,
    *,
    kind: str = PERSON_KIND,
    path: Path | None = None,
    limit: int,
    cursor: str | None = None,
) -> DecisionPage:
    """One page of resolver decisions of one `kind`, newest first, optionally narrowed to a
    single `path`.

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
    if kind not in DECISION_KINDS:
        raise ValueError(f"unknown decision kind {kind!r}")
    model: type[StoryPerson] | type[StoryEntity] = (
        StoryPerson if kind == PERSON_KIND else StoryEntity
    )
    filters = _decided(model)
    if kind in ORGANISATION_KINDS:
        filters.append(StoryEntity.kind == kind)
    if path is not None:
        filters.append(model.path == path.value)
    if cursor is not None:
        resolved_at, mention_id = decode_cursor(cursor)
        filters.append(tuple_(model.resolved_at, model.id) < (resolved_at, mention_id))

    # One row beyond the page, to learn whether a next page exists without counting the queue.
    result = await db.execute(
        select(model, Story, Film)
        .join(Story, Story.id == model.story_id)
        .outerjoin(Film, Film.id == Story.film_id)
        .where(*filters)
        .order_by(model.resolved_at.desc(), model.id.desc())
        .limit(limit + 1)
    )
    found = [_row(kind, mention, story, film) for mention, story, film in result.all()]

    rows = found[:limit]
    next_cursor = None
    if len(found) > limit and rows:
        last = rows[-1]
        next_cursor = encode_cursor(last.resolved_at, last.id)
    return DecisionPage(rows=rows, next_cursor=next_cursor)


def _row(
    kind: str, mention: StoryPerson | StoryEntity, story: Story, film: Film | None
) -> DecisionRow:
    """One ORM row flattened. `path` and `resolved_at` are narrowed here rather than asserted
    at the call site: `_decided` filtered the NULLs out of both, so the page's own filter is
    what makes the narrowing sound."""
    assert mention.path is not None and mention.resolved_at is not None  # `_decided()` filters
    if isinstance(mention, StoryPerson):
        role, department, entity_id = mention.role, mention.department, mention.person_id
    else:
        role, department, entity_id = None, None, mention.entity_id
    return DecisionRow(
        id=mention.id,
        kind=kind,
        story=story,
        film=film,
        name_as_written=mention.name_as_written,
        role=role,
        department=department,
        evidence_span=mention.evidence_span,
        path=mention.path,
        entity_id=entity_id,
        confidence=mention.confidence,
        features=mention.features or {},
        candidates=list(mention.candidates or []),
        resolved_at=mention.resolved_at,
    )
