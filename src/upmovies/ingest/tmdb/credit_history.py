"""Recorded-grade credit history: the diff `catalog.film_credit`'s rebuild throws away.

`_upsert_credits` deletes a film's credit rows and reinserts the current set on every ingest,
so the table can answer "who is attached now?" and nothing else. A director being *attached*
is invisible — the row simply exists afterwards, with no record that it did not exist before.
This module recovers that signal at the one point in the system where both sides are already
in hand, and writes it to `catalog.film_credit_change` for NEU-1083 to card.

**Recorded grade is seed grade *or* a follow** (D-49). A credit is written down when it is
seed grade — director, Writer/Screenplay, top-5 billed — or when its person is somebody a user
follows. Seed grade is a property of the credit; recorded grade is that, or a property of who
is watching. The catalog already holds every credit of every film it has, so a new follow
reaches existing minor credits through the timeline and the digest the moment it is made;
what this adds is the *future* changes, and only for people somebody asked for.

**Both sides of the diff are judged by the same followed set at the same moment.** The set is
loaded once per `upsert_film` and passed to both `load_recorded_credits` and
`recorded_credits_from_details`. Judging the stored side by yesterday's rule and the incoming
side by today's would turn a credit that was present all along into a phantom `added` row the
first time somebody followed its person — a fabricated beat, carded and delivered, about nothing
that happened.

**First observation is a baseline, never a change** (ADR-0014, spec §5.3). This is the
safety-critical property of the whole credit half, and it is expressed structurally rather
than left to fall out of the rebuild's ordering: `diff_recorded_credits` takes `previous=None`
for a film whose credits the catalog has never observed, and returns nothing for it whatever
the incoming set contains. `previous=set()` is a different statement — the film *was*
observed and held no recorded credit — and a director arriving then is a genuine
attachment.

Which of the two a film is, is read from the durable `film.credits_observed_at` marker and
**not** from `film_credit` being empty. A speculative TMDB entry can be admitted with an
empty credits payload, and inferring "never observed" from "holds no credits" would make
that film baseline again on the very next ingest — swallowing the first director to attach,
which is the single most valuable event the credit half exists to raise.

`film_field_change` gets the equivalent protection by accident, being a `BEFORE UPDATE`
trigger. Accidents do not survive a rewrite, and getting this wrong would emit tens of
thousands of false "attached to direct" rows the first day the expansion ran.

**The one exception: admission is an attachment for a followed person** (EF-4, ADR-0019
decision 4, NEU-1436). Somebody follows a director to hear about the director's *next* film,
and that film enters the catalog with the director already on it — so the rule that protects
the other tens of thousands of credits swallows the single beat the follow was made for. On a
first observation, every recorded credit whose person somebody follows *at that moment* is
written as `added`; everything else on the new film is still a baseline. It lives in
`admission_attachments` rather than in a `previous is None` branch of the diff, so the
baseline rule stays one unconditional statement that a reader can check at a glance and every
test that pins it stays green — the exception is then a choice the caller makes, in one
place, with the marker in hand.
"""

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_people
from upmovies.catalog.models import Film, FilmCredit, FilmCreditChange
from upmovies.catalog.seed_grade import is_seed_grade
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

CREDIT_ADDED = "added"
CREDIT_REMOVED = "removed"


@dataclass(frozen=True)
class RecordedCredit:
    """One recorded-grade credit, identified the way the diff must compare it.

    Not by `credit_id`: TMDB reissues those, and a reissued id on an unchanged attachment
    would read as a detachment followed by a re-attachment. `job` is part of the identity
    because one person can hold two crew credits on a film they both wrote and
    directed, and losing one of those is a change.
    """

    person_id: int
    credit_type: str
    job: str | None


@dataclass(frozen=True)
class CreditChange:
    """One recorded-grade credit crossing into or out of a film's credit set."""

    credit: RecordedCredit
    change: str


def _is_recorded(
    credit_type: str,
    job: str | None,
    credit_order: int | None,
    followed: Collection[int],
    person_id: int,
) -> bool:
    """Recorded grade in one line: seed grade, or a person somebody follows at `any` (D-49)."""
    return is_seed_grade(credit_type, job, credit_order) or person_id in followed


def recorded_credits_from_details(
    details: TMDBMovieDetails, *, followed: Collection[int] = ()
) -> set[RecordedCredit]:
    """The recorded-grade credits in a TMDB details payload — the *incoming* side of the diff.

    `followed` defaults to empty, which is exactly seed grade: every caller that has no reason
    to care about the follow graph gets the pre-M9 behaviour without passing anything.
    """
    credits = details.credits
    if credits is None:
        return set()
    recorded: set[RecordedCredit] = set()
    for member in credits.cast:
        if _is_recorded("cast", None, member.order, followed, member.id):
            recorded.add(RecordedCredit(person_id=member.id, credit_type="cast", job=None))
    for member in credits.crew:
        if _is_recorded("crew", member.job, None, followed, member.id):
            recorded.add(RecordedCredit(person_id=member.id, credit_type="crew", job=member.job))
    return recorded


async def load_recorded_credits(
    session: AsyncSession, film_id: UUID, *, followed: Collection[int] = ()
) -> set[RecordedCredit] | None:
    """The recorded-grade credits the catalog currently holds for a film — the *stored* side of
    the diff — or None if it has never observed this film's credits at all.

    Observedness comes from `film.credits_observed_at`, not from the credit rows: a film
    observed holding nothing returns an empty set, and a director arriving next run is a
    genuine attachment rather than a second baseline.

    `followed` must be the same set the incoming side is judged by, for the reason the module
    docstring gives: a follow made between two observations must not fabricate an attachment.

    Must be called **before** the rebuild's delete, which is the only reason this is a
    function and not a subquery.
    """
    observed_at = (
        await session.execute(select(Film.credits_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    if observed_at is None:
        return None
    stmt = select(
        FilmCredit.person_id, FilmCredit.credit_type, FilmCredit.job, FilmCredit.credit_order
    ).where(FilmCredit.film_id == film_id)
    return {
        RecordedCredit(person_id=person_id, credit_type=credit_type, job=job)
        for person_id, credit_type, job, credit_order in (await session.execute(stmt)).all()
        if _is_recorded(credit_type, job, credit_order, followed, person_id)
    }


def diff_recorded_credits(
    *, previous: Collection[RecordedCredit] | None, current: Collection[RecordedCredit]
) -> list[CreditChange]:
    """The recorded-grade attachments and detachments between two observations of a film.

    `previous is None` means this is the film's first observed credit set, which is a
    **baseline, never a change** — the rule this whole module exists to guarantee.

    The diff is over set membership, not over the writes the rebuild performs. The rebuild
    deletes and reinserts unconditionally, so a diff phrased in terms of what it *did* would
    see every credit as removed-then-added on every single run.

    Additions before removals, each sorted, so a run's rows land in a stable order.
    """
    if previous is None:
        return []
    before, after = set(previous), set(current)
    return [
        *(CreditChange(credit=c, change=CREDIT_ADDED) for c in _ordered(after - before)),
        *(CreditChange(credit=c, change=CREDIT_REMOVED) for c in _ordered(before - after)),
    ]


def admission_attachments(
    current: Collection[RecordedCredit], *, followed: Collection[int]
) -> list[CreditChange]:
    """The `added` rows a **first** observation writes: every recorded credit whose person
    somebody follows at that moment (EF-4, D-1436.1). Everything else in `current` is the
    baseline it has always been.

    The one exception to "first observation is a baseline" (ADR-0014), and deliberately a
    separate function rather than a branch inside `diff_recorded_credits`: the baseline rule
    is the safety-critical property of this module, and it stays structurally intact — the
    diff still returns nothing for `previous is None`, whatever it is handed. What changes is
    which of the two the caller reaches for.

    `followed` is the same set both sides of an ordinary diff are judged by, read once per
    `upsert_film`. That is what makes a follow created *after* admission a no-op: on the next
    ingest `previous` is a set, the person is in `followed` on both sides, and the diff is
    empty.

    Every credit of a followed person is written, seed-grade or not and one row per job, so a
    followed writer-director arrives as two `added` rows. Stable order, as the diff's.
    """
    return [
        CreditChange(credit=c, change=CREDIT_ADDED)
        for c in _ordered({c for c in current if c.person_id in followed})
    ]


def _ordered(credits: set[RecordedCredit]) -> list[RecordedCredit]:
    """A stable order for one side of the diff. `job` is normalized because a cast credit
    carries None there and None does not compare against a crew job."""
    return sorted(credits, key=lambda c: (c.person_id, c.credit_type, c.job or ""))


async def load_followed_person_ids(session: AsyncSession) -> set[int]:
    """The people somebody follows — recorded grade's second half (D-49, EF-2).

    One query per `upsert_film`, read before the rebuild so both sides of the diff are judged
    by the same answer. `ingest` reading `app` has precedent in `ingest.providers`: the follow
    graph is what decides how much of TMDB is worth writing down, so the ingest path has to be
    able to ask.
    """
    return set((await session.execute(followed_people())).scalars().all())


async def record_credit_changes(
    session: AsyncSession, film_id: UUID, changes: list[CreditChange]
) -> None:
    """Append the diff to `catalog.film_credit_change`. Pure DB I/O — the caller commits."""
    if not changes:
        return
    await session.execute(
        insert(FilmCreditChange).values(
            [
                {
                    "film_id": film_id,
                    "person_id": c.credit.person_id,
                    "credit_type": c.credit.credit_type,
                    "job": c.credit.job,
                    "change": c.change,
                }
                for c in changes
            ]
        )
    )


async def mark_credits_observed(session: AsyncSession, film_id: UUID) -> None:
    """Record that the catalog has now seen this film's credits, if it had not already.

    Write-once, guarded on the column rather than on the caller remembering the order: the
    marker's whole job is to be the thing the baseline rule cannot lose, so it must not be
    resettable by a later ingest. Pure DB I/O — the caller commits.
    """
    await session.execute(
        update(Film)
        .where(Film.id == film_id, Film.credits_observed_at.is_(None))
        .values(credits_observed_at=func.now())
    )
