"""Session + is_admin protected, read-only resolution endpoint for the admin UI (D-25, EF-12).

Human-facing, so `require_current_admin` (session cookie + `is_admin`) rather than the
ADMIN_TOKEN `require_admin` that gates the machine endpoints next door — reading why a trade
story's "Chris Evans" went unlinked is a thing a person does, not a cron.

No CSRF dependency and no write routes: D-25 is a queue you *read*. Corrections are not owed
and are deliberately absent — the daily run re-derives every path from the scorer's inputs,
so a `person_id` typed in here would not survive the next pass.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.deps import get_session, require_current_admin
from upmovies.link.resolve import queue
from upmovies.link.resolve.scoring import Path
from upmovies.news.models import PERSON_KIND

router = APIRouter(
    prefix="/admin/resolution",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


class DecisionKind(StrEnum):
    """The three queues this page lists (EF-12), as a query enum so a misspelled `kind` is a
    422 from FastAPI rather than an empty page that reads like a clean queue — the same
    treatment `path` already gets.

    Spelled out rather than generated from `queue.DECISION_KINDS`, because FastAPI documents
    an enum's members in the OpenAPI schema and a dynamically built one has no readable name
    for them. `tests/integration/routers/test_resolution_admin.py` pins the two lists together,
    which is what keeps the duplication honest."""

    PERSON = "person"
    COMPANY = "company"
    COLLECTION = "collection"


class ResolutionStoryOut(BaseModel):
    """The story the mention was found in — enough to go and read the sentence yourself."""

    id: UUID
    title: str
    url: str
    outlet: str | None


class ResolutionFilmOut(BaseModel):
    """The film the story is about. Null when the story's link was removed after the mention
    was extracted, which leaves the decision standing and its film gone."""

    id: UUID
    tmdb_id: int
    title: str


class ResolutionCandidateOut(BaseModel):
    """One candidate the scorer considered, with the feature breakdown behind their score.

    Every field is optional because this is read straight out of the `candidates` JSONB, which
    carries no schema of its own. `_candidate_out` below turns anything that still will not
    validate into an empty row, so between them the page whose job is showing anomalies
    renders a malformed shortlist entry rather than 500ing on it.

    `person_id` is what the person resolver logs and `entity_id` is what the organisation one
    logs (EF-12); a row carries whichever its kind wrote, and the page reads them the same way
    the decision rows below do.
    """

    person_id: int | None = None
    entity_id: int | None = None
    kind: str | None = None
    name: str | None = None
    score: float | None = None
    features: dict[str, Any] = {}


class ResolutionDecisionOut(BaseModel):
    """One decided mention, as the review page renders it.

    `role` and `department` are always null for an organisation — they are person facts, and
    `news.story_entity` has no column for them.
    """

    id: UUID
    kind: str
    story: ResolutionStoryOut
    film: ResolutionFilmOut | None
    name_as_written: str
    role: str | None
    department: str | None
    evidence_span: str | None
    path: str
    entity_id: int | None
    """The TMDB id this mention resolved to, in the catalogue `kind` names — a person, a
    production company or a collection. Null on the three routes that name nobody."""
    person_id: int | None
    """`entity_id` again when `kind` is `person`, and null otherwise. Kept for the admin page
    as it stands, which reads this field; NEU-1447 rebuilds that page around `entity_id` and
    `kind`, and drops this one with it."""
    confidence: float | None
    features: dict[str, Any]
    candidates: list[ResolutionCandidateOut]
    resolved_at: datetime | None


class ResolutionPage(BaseModel):
    """A page of decisions plus the token for the next one. `next_cursor` is null on the last
    page; there is no total, because the queue grows while it is being paged."""

    items: list[ResolutionDecisionOut]
    next_cursor: str | None


def _candidate_out(raw: object) -> ResolutionCandidateOut:
    """One entry of the `candidates` JSONB, never raising.

    A missing key the model already tolerates; an element that is not a dict at all, or that
    carries an uncoercible value, it does not. Both are anomalies in the resolver's own log,
    and an admin looking at this page is exactly the person who should get to see the rest of
    the shortlist rather than a 500 — so a hopeless entry degrades to an empty row.
    """
    try:
        return ResolutionCandidateOut.model_validate(raw)
    except ValidationError:
        return ResolutionCandidateOut()


def _to_out(row: queue.DecisionRow) -> ResolutionDecisionOut:
    return ResolutionDecisionOut(
        id=row.id,
        kind=row.kind,
        story=ResolutionStoryOut(
            id=row.story.id, title=row.story.title, url=row.story.url, outlet=row.story.outlet
        ),
        film=(
            ResolutionFilmOut(id=row.film.id, tmdb_id=row.film.tmdb_id, title=row.film.title)
            if row.film is not None
            else None
        ),
        name_as_written=row.name_as_written,
        role=row.role,
        department=row.department,
        evidence_span=row.evidence_span,
        path=row.path,
        entity_id=row.entity_id,
        person_id=row.entity_id if row.kind == PERSON_KIND else None,
        confidence=row.confidence,
        features=row.features,
        candidates=[_candidate_out(c) for c in row.candidates],
        resolved_at=row.resolved_at,
    )


@router.get("", response_model=ResolutionPage)
async def list_decisions(
    kind: DecisionKind = Query(default=DecisionKind.PERSON),
    path: Path | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> ResolutionPage:
    """Resolver decisions of one `kind`, newest first, optionally narrowed to one `path`.

    `kind` defaults to `person`, which is what this endpoint listed before organisations
    resolved (EF-12): the admin page as it stands asks for no kind and keeps the queue it had
    until NEU-1447 gives it the filter. One kind per request rather than all three at once —
    see `queue.list_decisions` on why the two tables are not unioned.

    `path` is the resolution vocabulary (D-24) — `accepted`, `tiebreak`, `unlinked`,
    `not_in_tmdb` — and anything else is a 422 from the enum rather than an empty page, so a
    misspelled filter reads as the mistake it is instead of as a clean queue. `kind` is an
    enum for the same reason.

    Paging is by `cursor`, not `offset`: see `queue.list_decisions`. Pass back the
    `next_cursor` the previous page returned; a null one means there is nothing after it. A
    cursor is only meaningful within one `kind`, since the two tables number their rows
    independently.
    """
    try:
        page = await queue.list_decisions(
            db, kind=kind.value, path=path, limit=limit, cursor=cursor
        )
    except queue.InvalidCursor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor"
        ) from None
    return ResolutionPage(
        items=[_to_out(row) for row in page.rows],
        next_cursor=page.next_cursor,
    )
