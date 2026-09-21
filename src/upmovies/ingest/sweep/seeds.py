"""The seed set: who the sweep enumerates, and what their credits are worth.

Seed grade itself — director, writer (`Writer`/`Screenplay`), top-5 billed cast — is defined
in `upmovies.catalog.seed_grade`, because the credit history (`ingest.tmdb.credit_history`)
applies the same cut and the two must not drift. This module is about how the sweep *uses*
it: the grade is checked twice on purpose (spec §3.2, §4.1),
once on the *person*, to decide whose filmography is worth a request, and once on their role
on the *candidate film*, so a "Special Thanks" credit cannot drag in someone's short on the
strength of the directing credit that made them a seed.

Since M9 the enumeration set is wider than the seed set, and the module name is now the
narrower of the two things it holds: `load_seed_person_ids` returns the seed people **plus**
everyone somebody follows (D-50, ADR-0013 as amended, EF-2). Seed grade itself is untouched
by that — the followed half is a second admission rule beside it, not a widening of it —
which is why the `followed` role is spelled separately everywhere it appears.

These rules were written for the read-only probe (`scripts/probe_undated_candidates.py`,
NEU-1073) and moved here when the sweep landed. The probe imports them rather than keeping
a copy: a probe measuring a different definition of seed grade than the sweep applies is a
measurement of nothing — and it passes no `followed`, so it still measures seed grade alone.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_people
from upmovies.catalog.models import Film, FilmCredit, Person
from upmovies.catalog.queries import active_film_clause, seed_grade_credit_clause
from upmovies.catalog.seed_grade import crew_role, is_top_billed
from upmovies.ingest.tmdb.schemas import TMDBPersonMovieCredits

SessionFactory = Callable[[], AsyncSession]


FOLLOWED_ROLE = "followed"
"""The role a *followed* person's non-seed credit reaches a candidate under (D-50).

Not a seed grade, and deliberately not spelled as one: it is what `SWEEP_ADMIT_FOLLOWED` gates,
and it sits last in `catalog.seed_grade.ROLE_ORDER` for the same reason. A followed person's
seed-grade credits keep their own roles — the same person can reach one candidate as
`director` and another as `followed`, and the tranches judge those two independently."""


@dataclass(frozen=True)
class SeedAttachment:
    """One enumerated person reaching one undated film through one credit, at the role that
    credit carries — a seed grade, or `followed` (D-50)."""

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
    """Distinct people the sweep enumerates: seed people, plus everyone followed.

    **A follow is its own admission rule** (D-50, ADR-0013 as amended, EF-2). Seed grade asks
    whether a person's filmography is worth a request on the catalog's evidence; a follow is a
    user saying so directly, which is the same relevance prior ADR-0013 chose people for in the
    first place. The two sets are unioned rather than seed grade being widened — the NEU-1090
    measurement stands, producers stay out — and the followed half is bounded by the follow
    graph rather than by dormancy, because a follow does not go quiet.

    The seed half is unchanged (spec §3.2): distinct people holding a seed-grade credit on an
    active film. Dormant films contribute no seed people (ADR-0015). This is what stops the seed set
    compounding without bound as admitted films contribute their own credits back: a film
    that goes nowhere goes quiet, its credits drop out of this query, and the sweep
    contracts (§3.3).

    People TMDB has deleted are excluded from **both** halves (`person.tmdb_missing_at`). Their
    credit rows are ours and outlive the person record upstream, so without this the sweep
    re-requests a dead filmography every run forever — about fifty of them today; and a
    tombstoned person is not a follow target either (`public.service._LIVE_PERSON`), so a follow
    row left pointing at one must not resurrect the request.

    Both halves join `catalog.person` **outer**, for one reason: the exclusion may only ever
    remove a person it can positively say is gone. The seed half is defined by `film_credit`,
    where a credit whose person row we never wrote is still a seed (NEU-1124); the followed
    half is defined by the follow, and a follow can name a person the catalog holds no row for
    — the follow routes validate the id's *shape*, not its existence, and the D-15/D-16
    importers write `entity_id` straight from their caller. An inner join would drop exactly
    those, which is the one case where the follow is the only evidence there is.
    """
    seeds = (
        select(FilmCredit.person_id)
        .join(Film, Film.id == FilmCredit.film_id)
        .outerjoin(Person, Person.id == FilmCredit.person_id)
        .where(
            seed_grade_credit_clause(),
            Person.tmdb_missing_at.is_(None),
            active_film_clause(
                today=today,
                excluded_statuses=excluded_statuses,
                dormancy_days=dormancy_days,
            ),
        )
    )
    followed_ids = followed_people().subquery()
    followed = (
        select(followed_ids.c[0])
        .outerjoin(Person, Person.id == followed_ids.c[0])
        .where(Person.tmdb_missing_at.is_(None))
    )
    stmt = union(seeds, followed).subquery()
    return list((await session.execute(select(stmt.c[0]).order_by(stmt.c[0]))).scalars().all())


async def load_known_film_tmdb_ids(session: AsyncSession) -> set[int]:
    """Every `catalog.film` TMDB id, active or not — a candidate we already hold is not a
    candidate, whatever state it is in."""
    return set((await session.execute(select(Film.tmdb_id))).scalars().all())


def seed_attachments(
    person_id: int, credits: TMDBPersonMovieCredits, *, followed: bool = False
) -> list[SeedAttachment]:
    """The undated films this person reaches — at seed grade, and at `followed` when they are
    somebody's `any` follow (D-50).

    The role is the one held on the candidate film, not the one that made this person a
    seed (§4.1 rule 2). That rule is exactly what `followed` extends: a followed person's
    seed-grade credits still yield their own roles, and only their *non*-seed credits yield
    `followed` — so one person can reach one candidate as its director and another as a second
    assistant editor, and the tranches judge the two films separately.

    `followed=False` is the whole of the pre-M9 behaviour, which is what the probe
    (`scripts/probe_undated_candidates.py`) and every seed-only caller get by not passing it.
    """
    attachments: list[SeedAttachment] = []
    for entry in credits.cast:
        if entry.release_date is not None:
            continue
        if is_top_billed(entry.order):
            attachments.append(SeedAttachment(entry.id, entry.title, person_id, "cast"))
        elif followed:
            attachments.append(SeedAttachment(entry.id, entry.title, person_id, FOLLOWED_ROLE))
    for crew_entry in credits.crew:
        if crew_entry.release_date is not None:
            continue
        role = crew_role(crew_entry.job)
        if role is not None:
            attachments.append(SeedAttachment(crew_entry.id, crew_entry.title, person_id, role))
        elif followed:
            attachments.append(
                SeedAttachment(crew_entry.id, crew_entry.title, person_id, FOLLOWED_ROLE)
            )
    return attachments


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
