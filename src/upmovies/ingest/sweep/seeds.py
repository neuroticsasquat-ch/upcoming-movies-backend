"""The seed set: who the sweep enumerates, and what their credits are worth.

Seed grade — director, writer (`Writer`/`Screenplay`), top-5 billed cast — is checked
twice on purpose (spec §3.2, §4.1): once on the *person*, to decide whose filmography is
worth a request, and once on their role on the *candidate film*, so a "Special Thanks"
credit cannot drag in someone's short on the strength of the directing credit that made
them a seed.

Producers are deliberately not seed grade. An EP credit travels far and says little about
whether a project is real, and the 2,191 extra people it would add are the weakest of the
four attachments.

These rules were written for the read-only probe (`scripts/probe_undated_candidates.py`,
NEU-1073) and moved here when the sweep landed. The probe imports them rather than keeping
a copy: a probe measuring a different definition of seed grade than the sweep applies is a
measurement of nothing.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit
from upmovies.catalog.queries import active_film_clause
from upmovies.ingest.tmdb.schemas import TMDBPersonMovieCredits

SessionFactory = Callable[[], AsyncSession]

DIRECTOR_JOB = "Director"
WRITER_JOBS = frozenset({"Writer", "Screenplay"})
# "Top-5 billed" is TMDB's `order`, which is 0-indexed.
TOP_BILLED_ORDER = 5
# Strongest attachment first, so a rendered role list reads the way §3.2 lists the seed
# grades and groups stably.
ROLE_ORDER = ("director", "writer", "cast")


@dataclass(frozen=True)
class SeedAttachment:
    """One seed person reaching one undated film through one seed-grade credit."""

    tmdb_id: int
    title: str
    person_id: int
    role: str


@dataclass
class CandidateTally:
    """Every seed person reaching one candidate film, folded together."""

    tmdb_id: int
    title: str = ""
    seed_person_ids: set[int] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)

    @property
    def seed_attachment_count(self) -> int:
        """Distinct *people*, not credits — one person who both wrote and directed a film
        is a single attachment, and the corroboration threshold counts corroborators."""
        return len(self.seed_person_ids)


async def load_seed_person_ids(
    session: AsyncSession, *, today: date, excluded_statuses: frozenset[str], dormancy_days: int
) -> list[int]:
    """Distinct people holding a seed-grade credit on an active film (spec §3.2).

    Dormant films contribute no seed people (ADR-0015). This is what stops the seed set
    compounding without bound as admitted films contribute their own credits back: a film
    that goes nowhere goes quiet, its credits drop out of this query, and the sweep
    contracts (§3.3).
    """
    seed_grade = or_(
        and_(FilmCredit.credit_type == "crew", FilmCredit.job == DIRECTOR_JOB),
        and_(FilmCredit.credit_type == "crew", FilmCredit.job.in_(WRITER_JOBS)),
        and_(
            FilmCredit.credit_type == "cast",
            FilmCredit.credit_order.is_not(None),
            FilmCredit.credit_order < TOP_BILLED_ORDER,
        ),
    )
    stmt = (
        select(FilmCredit.person_id)
        .join(Film, Film.id == FilmCredit.film_id)
        .where(
            seed_grade,
            active_film_clause(
                today=today,
                excluded_statuses=excluded_statuses,
                dormancy_days=dormancy_days,
            ),
        )
        .distinct()
        .order_by(FilmCredit.person_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def load_known_film_tmdb_ids(session: AsyncSession) -> set[int]:
    """Every `catalog.film` TMDB id, active or not — a candidate we already hold is not a
    candidate, whatever state it is in."""
    return set((await session.execute(select(Film.tmdb_id))).scalars().all())


def seed_attachments(person_id: int, credits: TMDBPersonMovieCredits) -> list[SeedAttachment]:
    """The undated films this person reaches *at seed grade*.

    The role is the one held on the candidate film, not the one that made this person a
    seed (§4.1 rule 2).
    """
    attachments: list[SeedAttachment] = []
    for entry in credits.cast:
        if entry.release_date is not None:
            continue
        if entry.order is not None and entry.order < TOP_BILLED_ORDER:
            attachments.append(SeedAttachment(entry.id, entry.title, person_id, "cast"))
    for crew_entry in credits.crew:
        if crew_entry.release_date is not None:
            continue
        role = crew_role(crew_entry.job)
        if role is not None:
            attachments.append(SeedAttachment(crew_entry.id, crew_entry.title, person_id, role))
    return attachments


def crew_role(job: str | None) -> str | None:
    """The seed grade a crew job carries, or None when it carries none."""
    if job == DIRECTOR_JOB:
        return "director"
    if job in WRITER_JOBS:
        return "writer"
    return None


def tally_attachments(attachments: Iterable[SeedAttachment]) -> dict[int, CandidateTally]:
    """Fold attachments into one tally per candidate film."""
    tallies: dict[int, CandidateTally] = {}
    for attachment in attachments:
        tally = tallies.setdefault(
            attachment.tmdb_id, CandidateTally(tmdb_id=attachment.tmdb_id, title=attachment.title)
        )
        tally.seed_person_ids.add(attachment.person_id)
        tally.roles.add(attachment.role)
    return tallies
