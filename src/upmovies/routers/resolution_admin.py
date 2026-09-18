"""Session + is_admin protected, read-only person-resolution endpoint for the admin UI (D-25).

Human-facing, so `require_current_admin` (session cookie + `is_admin`) rather than the
ADMIN_TOKEN `require_admin` that gates the machine endpoints next door — reading why a trade
story's "Chris Evans" went unlinked is a thing a person does, not a cron.

No CSRF dependency and no write routes: D-25 is a queue you *read*. Corrections are not owed
and are deliberately absent — the daily run re-derives every path from the scorer's inputs,
so a `person_id` typed in here would not survive the next pass.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.deps import get_session, require_current_admin
from upmovies.link.resolve import queue
from upmovies.link.resolve.scoring import Path
from upmovies.news.models import Story, StoryPerson

router = APIRouter(
    prefix="/admin/resolution",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


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
    """One person the scorer considered, with the feature breakdown behind their score.

    Every field is optional because this is read straight out of `story_person.candidates`
    JSONB, which carries no schema of its own. `_candidate_out` below turns anything that
    still will not validate into an empty row, so between them the page whose job is showing
    anomalies renders a malformed shortlist entry rather than 500ing on it.
    """

    person_id: int | None = None
    name: str | None = None
    score: float | None = None
    features: dict[str, Any] = {}


class ResolutionDecisionOut(BaseModel):
    """One decided mention, as the review page renders it."""

    id: UUID
    story: ResolutionStoryOut
    film: ResolutionFilmOut | None
    name_as_written: str
    role: str | None
    department: str | None
    evidence_span: str | None
    path: str
    person_id: int | None
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


def _to_out(mention: StoryPerson, story: Story, film: Film | None) -> ResolutionDecisionOut:
    return ResolutionDecisionOut(
        id=mention.id,
        story=ResolutionStoryOut(
            id=story.id, title=story.title, url=story.url, outlet=story.outlet
        ),
        film=(
            ResolutionFilmOut(id=film.id, tmdb_id=film.tmdb_id, title=film.title)
            if film is not None
            else None
        ),
        name_as_written=mention.name_as_written,
        role=mention.role,
        department=mention.department,
        evidence_span=mention.evidence_span,
        # Narrowed by `queue._decided()`, which filters `path IS NULL` out of every page.
        path=mention.path or "",
        person_id=mention.person_id,
        confidence=mention.confidence,
        features=mention.features or {},
        candidates=[_candidate_out(c) for c in (mention.candidates or [])],
        resolved_at=mention.resolved_at,
    )


@router.get("", response_model=ResolutionPage)
async def list_decisions(
    path: Path | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> ResolutionPage:
    """Resolver decisions, newest first, optionally narrowed to one `path`.

    `path` is the `story_person` vocabulary (D-24) — `accepted`, `tiebreak`, `unlinked`,
    `not_in_tmdb` — and anything else is a 422 from the enum rather than an empty page, so a
    misspelled filter reads as the mistake it is instead of as a clean queue.

    Paging is by `cursor`, not `offset`: see `queue.list_decisions`. Pass back the
    `next_cursor` the previous page returned; a null one means there is nothing after it.
    """
    try:
        page = await queue.list_decisions(db, path=path, limit=limit, cursor=cursor)
    except queue.InvalidCursor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_cursor"
        ) from None
    return ResolutionPage(
        items=[_to_out(r.mention, r.story, r.film) for r in page.rows],
        next_cursor=page.next_cursor,
    )
