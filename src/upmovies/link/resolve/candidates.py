"""Candidate generation for one person mention on a linked film (D-21).

The first half of person resolution: given a `news.story_person` row — a name a trade story
wrote, on a story the linker has already attached to a film — produce the small set of TMDB
people it could plausibly be, each tagged with where it came from and the raw facts the
scorer will read. **Nothing here scores, ranks or decides anything** (NEU-1363 does that), so
no threshold, no name comparison and no confidence lives in this module. The only judgement
it makes is *who gets to be considered at all*.

Three sources, unioned and de-duplicated by TMDB person id:

- **`/search/person` on the name** — the only source that can reach somebody the catalog has
  never held, which is the common case for a casting announcement.
- **The film's current `film_credit` people** — a name in a story about a film is very often
  somebody already on it.
- **People in the film's `film_credit_change` rows from the last 14 days** — someone who
  *just* joined or left. Detachments count: "X exits the sequel" names a person the current
  credits no longer hold, and dropping removals would make exactly those mentions
  unresolvable.

**Being credited and earning a place in the set are two different things.** Every current
`film_credit` row counts for the `credited` flag and for the credit facts a candidate carries,
whoever they are: a composer or a sixth-billed actor the article names is credited on that
film, and D-21 scores both "already credited" and "department vs role" off exactly that. But
`film_credit` holds the film's whole cast and crew — a hundred-odd rows, most of them a grip
or a second AD — so *whose presence on the film alone is enough to claim one of ten places*
is the narrower question, and the answer is the seed-grade credits (director,
Writer/Screenplay, top-5 billed — `catalog.seed_grade`). Without that cut the cap would go
entirely to people no trade story will ever name and evict the search hits that carry the
actual answer; it is also already the cut `film_credit_change` records, so it keeps the two
film-anchored sources talking about the same people rather than drifting apart. The composer
still becomes a candidate the moment TMDB's name search returns them — and arrives flagged
`credited`, with their credit on this film attached.

**The cap is anchored-first** (D-21): seed-grade credited and change-stream candidates fill it
before any search hit, because being on the film is the strongest thing the catalog knows
about a person and a name search ranks by TMDB's popularity rather than by this film. One
consequence worth knowing when the scorer lands: a film whose seed-grade credits and recent
change stream already number ten leaves no room for search hits at all, so a mention of
someone genuinely new to that film can only route to `unlinked`. Seed grade bounds the
credited side at roughly eight, which is what keeps that from being the normal case.

Facts, not scores, are what each candidate carries: the person row's own fields, whatever
credits they hold on *this* film, whatever change rows named them, and their filmography as
TMDB ids (from the search hit's `known_for` and from every catalog film they are credited on)
so D-21's "filmography overlap with other titles named in the article" has something to
overlap. Birthday and deathday are listed in D-21's feature set and are deliberately absent
here: neither `catalog.person` nor anything this repo fetches from TMDB holds them, so the
age/alive plausibility feature has no input until a schema change supplies one. Inventing
always-NULL columns for it here would only make the gap harder to see.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit, FilmCreditChange, Person
from upmovies.catalog.seed_grade import ROLE_ORDER, crew_role, is_top_billed
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.schemas import TMDBPersonSearchHit

CANDIDATE_CAP = 10
"""Most candidates one mention may carry (D-21). The resolve stage shows this shortlist to a
model in a closed-set prompt (D-22), so it is a prompt-size bound as much as a scoring one."""

CHANGE_STREAM_WINDOW_DAYS = 14
"""How far back the change-stream source reads. A fixed rolling window, like the sweep's
credit phases: a watermark would advance past changes a failed run never used."""


@dataclass(frozen=True)
class CreditFact:
    """One credit a candidate currently holds on the film being written about."""

    credit_type: str
    department: str | None
    job: str | None
    credit_order: int | None


@dataclass(frozen=True)
class ChangeFact:
    """One `catalog.film_credit_change` row from the window that named a candidate."""

    credit_type: str
    job: str | None
    change: str
    changed_at: datetime


@dataclass(frozen=True)
class CatalogPerson:
    """A person one of the two film-anchored sources found, with that source's facts.

    One type for both sources rather than two near-identical ones: the person fields are the
    same `catalog.person` columns either way, and which source found them is not a property
    of the person — it is which argument of `build_candidates` they arrive in.
    """

    person_id: int
    name: str
    original_name: str | None = None
    known_for_department: str | None = None
    popularity: float | None = None
    credits: tuple[CreditFact, ...] = ()
    changes: tuple[ChangeFact, ...] = ()


@dataclass(frozen=True)
class CandidateSet:
    """What one gather produced: the capped candidate list, and the raw `/search/person`
    hits behind it.

    The hits are kept alongside rather than discarded because two things downstream need
    them and neither can recover them from the candidates. The scorer's `not_in_tmdb` route
    (INV-8) turns on the search having returned *nothing*, which the list cannot say — an
    empty list is equally a film with no credits and a name nobody holds, and a full one may
    have evicted every hit at the cap. And accepting a candidate TMDB found but the catalog
    has never held means writing `catalog.person` first, which `ingest.tmdb.upsert.
    upsert_people` does from the hit itself rather than from the seven fields a `Candidate`
    happens to have kept.
    """

    candidates: list["Candidate"]
    search_hits: list[TMDBPersonSearchHit]

    def hit_for(self, person_id: int) -> TMDBPersonSearchHit | None:
        return next((hit for hit in self.search_hits if hit.id == person_id), None)


@dataclass(frozen=True)
class Candidate:
    """One person a mention could be, with its provenance and the facts scoring reads."""

    person_id: int
    name: str
    original_name: str | None
    known_for_department: str | None
    popularity: float | None
    from_search: bool
    credited: bool
    in_change_stream: bool
    credits: tuple[CreditFact, ...] = ()
    changes: tuple[ChangeFact, ...] = ()
    filmography_tmdb_ids: tuple[int, ...] = ()

    @property
    def anchored(self) -> bool:
        """Whether the film itself, rather than a name search, ties this person to the story.

        A fact about the person, which is what D-21 scores — not a claim that they took one
        of the cap's film-anchored places. Only a seed-grade credit or a change row does
        that, so a candidate can be `credited` and still owe its place to the name search.
        """
        return self.credited or self.in_change_stream


def build_candidates(
    *,
    search_hits: Sequence[TMDBPersonSearchHit],
    credited: Sequence[CatalogPerson],
    change_stream: Sequence[CatalogPerson],
    catalog_filmography: Mapping[int, Sequence[int]] | None = None,
    cap: int = CANDIDATE_CAP,
) -> list[Candidate]:
    """Union the three sources, de-duplicate by person id, tag provenance, cap.

    Pure: every read the sources need has already happened by the time this is called, which
    is what lets the union, the flags and the cap be tested without a database or a network.
    `gather_candidates` is the half that fetches.

    `credited` is the film's *whole* current credit list. Each of those people is flagged
    `credited` and carries their credits wherever they end up in the set, but only the ones
    holding a seed-grade credit claim a place on the strength of being credited at all — see
    the module docstring for why the cap cannot be spent on a hundred-row crew list.

    Order is anchored-first: seed-grade credited people, strongest attachment first in the
    order `catalog.seed_grade.ROLE_ORDER` already defines; then the change stream, most recent
    change first; then the search hits in TMDB's own relevance order. Within a tier that order
    is a *tiebreak deciding who survives the cap*, not a score — nothing here claims the
    director is likelier than the writer to be the person the story named.
    """
    filmography = catalog_filmography or {}
    credited_by_id = _by_person_id(credited)
    changed_by_id = _by_person_id(change_stream)
    hit_by_id: dict[int, TMDBPersonSearchHit] = {}
    for hit in search_hits:
        hit_by_id.setdefault(hit.id, hit)

    seed_graded = sorted(
        (person for person in credited_by_id.values() if _seed_roles(person)),
        key=_attachment_rank,
    )
    ordered_ids = _dedupe(
        [*(person.person_id for person in seed_graded), *changed_by_id, *hit_by_id]
    )[:cap]
    return [
        _candidate(
            person_id=person_id,
            hit=hit_by_id.get(person_id),
            credited=credited_by_id.get(person_id),
            changed=changed_by_id.get(person_id),
            catalog_filmography=filmography.get(person_id, ()),
        )
        for person_id in ordered_ids
    ]


async def gather_candidates(
    session: AsyncSession,
    client: TMDBClient,
    *,
    film_id: UUID,
    name_as_written: str,
    now: datetime | None = None,
    window_days: int = CHANGE_STREAM_WINDOW_DAYS,
    cap: int = CANDIDATE_CAP,
) -> CandidateSet:
    """Read all three sources for one mention and build its candidate set.

    One TMDB request per mention and three queries, none of them per candidate: the
    filmography read is a single `IN` over the whole union. `now` is injectable so a test
    can place the 14-day window around fixture rows rather than around the clock.

    Returns the search hits with the candidates: they are this function's only reader of
    TMDB, and what the scorer and the accept path need from them cannot be reconstructed
    from the capped list — see `CandidateSet`.
    """
    search_hits = await client.search_person(name_as_written)
    credited = await load_credited_people(session, film_id)
    since = (now or datetime.now(UTC)) - timedelta(days=window_days)
    change_stream = await load_change_stream_people(session, film_id, since=since)
    person_ids = {
        *(hit.id for hit in search_hits),
        *(person.person_id for person in credited),
        *(person.person_id for person in change_stream),
    }
    return CandidateSet(
        candidates=build_candidates(
            search_hits=search_hits,
            credited=credited,
            change_stream=change_stream,
            catalog_filmography=await load_catalog_filmography(session, person_ids),
            cap=cap,
        ),
        search_hits=list(search_hits),
    )


async def load_credited_people(session: AsyncSession, film_id: UUID) -> list[CatalogPerson]:
    """The film's current credits — all of them — one `CatalogPerson` per person.

    Not narrowed to seed grade: which credits *earn a place* in a capped candidate set is
    `build_candidates`' question, while "is this person credited on the film, and as what?"
    is a fact D-21 scores for whoever ends up in the set, including someone the name search
    found. Narrowing here would answer the second question with the first one's cut.

    A person holding two credits on the film (directed and wrote it) is one `CatalogPerson`
    carrying both `CreditFact`s. The SQL order is deterministic and nothing more — the tier
    order the cap spends is computed in `build_candidates`.
    """
    stmt = (
        select(
            FilmCredit.person_id,
            FilmCredit.credit_type,
            FilmCredit.department,
            FilmCredit.job,
            FilmCredit.credit_order,
            Person.name,
            Person.original_name,
            Person.known_for_department,
            Person.popularity,
        )
        .join(Person, Person.id == FilmCredit.person_id)
        .where(FilmCredit.film_id == film_id)
        .order_by(FilmCredit.person_id, FilmCredit.credit_id)
    )
    people: dict[int, CatalogPerson] = {}
    credits: dict[int, list[CreditFact]] = {}
    for row in await session.execute(stmt):
        credits.setdefault(row.person_id, []).append(
            CreditFact(
                credit_type=row.credit_type,
                department=row.department,
                job=row.job,
                credit_order=row.credit_order,
            )
        )
        people.setdefault(row.person_id, _catalog_person(row))
    return [replace(person, credits=tuple(credits[pid])) for pid, person in people.items()]


async def load_change_stream_people(
    session: AsyncSession, film_id: UUID, *, since: datetime
) -> list[CatalogPerson]:
    """People named in the film's credit changes since `since`, most recent change first.

    Attachments and detachments alike: both say a story about this film may be about this
    person. `catalog.film_credit_change` only ever holds seed-grade rows, so no grade filter
    is needed on this side.
    """
    stmt = (
        select(
            FilmCreditChange.person_id,
            FilmCreditChange.credit_type,
            FilmCreditChange.job,
            FilmCreditChange.change,
            FilmCreditChange.changed_at,
            Person.name,
            Person.original_name,
            Person.known_for_department,
            Person.popularity,
        )
        .join(Person, Person.id == FilmCreditChange.person_id)
        .where(FilmCreditChange.film_id == film_id, FilmCreditChange.changed_at >= since)
        .order_by(FilmCreditChange.changed_at.desc(), FilmCreditChange.id.desc())
    )
    people: dict[int, CatalogPerson] = {}
    changes: dict[int, list[ChangeFact]] = {}
    for row in await session.execute(stmt):
        changes.setdefault(row.person_id, []).append(
            ChangeFact(
                credit_type=row.credit_type,
                job=row.job,
                change=row.change,
                changed_at=row.changed_at,
            )
        )
        people.setdefault(row.person_id, _catalog_person(row))
    return [replace(person, changes=tuple(changes[pid])) for pid, person in people.items()]


async def load_catalog_filmography(
    session: AsyncSession, person_ids: Iterable[int]
) -> dict[int, tuple[int, ...]]:
    """TMDB film ids each of these people is credited on anywhere in the catalog.

    TMDB ids rather than `catalog.film.id`, because the other half of the overlap the scorer
    computes is a search hit's `known_for`, which is TMDB ids — a UUID here would make the
    two sides incomparable. Every credit counts, not just seed-grade ones: the question is
    whether this person's filmography touches a title the article named, and a second-unit
    job on that title answers it as well as a starring role.
    """
    ids = list(dict.fromkeys(person_ids))
    if not ids:
        return {}
    stmt = (
        select(FilmCredit.person_id, Film.tmdb_id)
        .join(Film, Film.id == FilmCredit.film_id)
        .where(FilmCredit.person_id.in_(ids))
        .order_by(FilmCredit.person_id, Film.tmdb_id)
    )
    filmography: dict[int, list[int]] = {}
    for row in await session.execute(stmt):
        filmography.setdefault(row.person_id, []).append(row.tmdb_id)
    return {person_id: tuple(_dedupe(films)) for person_id, films in filmography.items()}


def _seed_roles(person: CatalogPerson) -> list[str]:
    """The seed grades this person's credits on the film carry — `director`, `writer`,
    `cast` — and an empty list when none do.

    Composed from `catalog.seed_grade`'s own two primitives rather than re-deciding the cut:
    a second encoding of what seed grade means is exactly what that module exists to prevent.
    It returns the roles instead of a boolean because the cap's ordering needs to know *which*
    grade, not only that there was one.
    """
    roles = []
    for credit in person.credits:
        if credit.credit_type == "cast" and is_top_billed(credit.credit_order):
            roles.append("cast")
        elif credit.credit_type == "crew":
            role = crew_role(credit.job)
            if role is not None:
                roles.append(role)
    return roles


def _attachment_rank(person: CatalogPerson) -> tuple[int, int, int]:
    """Where a seed-grade credited person sits in the anchored tier: strongest attachment
    first, which is what `catalog.seed_grade.ROLE_ORDER` already means by its own ordering,
    then billing position, then person id so the result never depends on row order."""
    ranks = [ROLE_ORDER.index(role) for role in _seed_roles(person)]
    billings = [c.credit_order for c in person.credits if c.credit_order is not None]
    return (min(ranks), min(billings, default=0), person.person_id)


def _by_person_id(people: Sequence[CatalogPerson]) -> dict[int, CatalogPerson]:
    """Index one source's rows by person id, keeping the first of any repeat so the source's
    own ordering survives de-duplication."""
    indexed: dict[int, CatalogPerson] = {}
    for person in people:
        indexed.setdefault(person.person_id, person)
    return indexed


def _dedupe(values: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(values))


def _catalog_person(row: Row[Any]) -> CatalogPerson:
    """The `catalog.person` half of a credit or change row. Both loaders select the same four
    person columns, and one spelling of that mapping keeps them from disagreeing about which
    stored fact reaches the scorer."""
    return CatalogPerson(
        person_id=row.person_id,
        name=row.name,
        original_name=row.original_name,
        known_for_department=row.known_for_department,
        popularity=row.popularity,
    )


def _candidate(
    *,
    person_id: int,
    hit: TMDBPersonSearchHit | None,
    credited: CatalogPerson | None,
    changed: CatalogPerson | None,
    catalog_filmography: Sequence[int],
) -> Candidate:
    """Merge whatever the three sources hold about one person into a single candidate.

    Where a search hit and a stored person row both carry a fact, the hit wins: it was
    fetched for this mention, while `catalog.person` was last written whenever some film
    crediting them was last ingested. Where the hit carries nothing — TMDB omits
    `known_for_department` and `popularity` often enough — the stored row fills in.
    """
    stored = credited or changed
    known_for_ids = [title.id for title in hit.known_for] if hit is not None else []
    stored_name = stored.name if stored is not None else None
    return Candidate(
        # A candidate only exists because some source produced it, so one of these two is
        # always a name; the empty fallback is there for the type checker, not for a case.
        person_id=person_id,
        name=(hit.name if hit is not None else None) or stored_name or "",
        original_name=_fresher(
            hit.original_name if hit is not None else None,
            stored.original_name if stored is not None else None,
        ),
        known_for_department=_fresher(
            hit.known_for_department if hit is not None else None,
            stored.known_for_department if stored is not None else None,
        ),
        popularity=_fresher(
            hit.popularity if hit is not None else None,
            stored.popularity if stored is not None else None,
        ),
        from_search=hit is not None,
        credited=credited is not None,
        in_change_stream=changed is not None,
        credits=credited.credits if credited is not None else (),
        changes=changed.changes if changed is not None else (),
        filmography_tmdb_ids=tuple(_dedupe([*known_for_ids, *catalog_filmography])),
    )


def _fresher[T](live: T | None, stored: T | None) -> T | None:
    """The value the live search hit carried, falling back to the stored person row's."""
    return live if live is not None else stored
