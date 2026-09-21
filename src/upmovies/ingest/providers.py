"""The watch-provider poll: who is carrying a film at home, read once a day for the films
anyone could plausibly be waiting on (D-27).

**Its own run kind, not a sweep phase.** `python -m upmovies.pipeline_run providers` opens an
`ingest_run` of kind `providers` and finalizes it, the way the other stage runners do. The
sweep's phases share one run row because they are one pass over one working set; this poll has
a working set of its own — films *past* their theatrical date, which `in_play_clause` has
already dropped from everything the sweep does — so folding it in would report two unrelated
passes under one status and make a provider outage read as a sweep failure.

**What it shares with the sweep is the per-item contract**, imported from
`ingest.sweep.phase`: one session per film so a failure never rolls back the films before it,
`record_progress` against the run, a consecutive-failure abort, and the heartbeat that keeps a
long quiet pass from being cancelled as an orphan (NEU-1117). Those are pipeline conventions
the sweep happens to host, not sweep-specific behaviour, and a second copy here would be a
second definition of "gave up".

**The scoped set is two rules, ORed** (D-27):

1. the film's US theatrical governing date is between `min_age_days` and `max_age_days` old —
   the window where a home release is plausible but not yet ancient history; and
2. *anybody* follows the title or has it on their watchlist — somebody is waiting on this
   answer, so it is polled whether or not its date says it is due, and whether or not it has a
   theatrical date at all.

Rule 2 is not an optimisation of rule 1, it is the reason the feature feels alive: a
watchlisted film that went straight to streaming has no theatrical date to age, and rule 1
alone would never poll it.

**Insert-only ledger, delete-and-rebuild snapshot.** Each poll writes new
`availability_first_seen` rows for offers it has never seen before and rebuilds
`film_availability_current` for the region wholesale. The first is what `now_available` cards
off (D-28); the second is what the where-to-watch box renders (D-29). Nothing tracks churn: a
provider dropping a film removes a current row and leaves the ledger untouched.

**The card is per monetization type, not per provider or per row** (D-28). A new ledger row is
necessary but not sufficient: Netflix handing a film to Hulu writes a row — Hulu has never
carried it — and must card nothing, because the *type* was first seen months ago and
service-to-service churn is the thing this product does not report. So the trigger is the set
of types this film had no ledger row under before the insert, read in the same transaction as
the insert itself. Everything downstream follows from that: the body names the services on the
rows that made the type new, `occurred_at` is their `first_seen_at`, and a re-poll of an
unchanged film finds no new type and writes nothing.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import ColumnElement, Date, and_, cast, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import covered_by_any_user_clause
from upmovies.catalog.models import (
    MONETIZATION_TYPES,
    AvailabilityFirstSeen,
    Film,
    FilmAvailabilityCurrent,
    FilmReleaseDate,
    WatchProvider,
)
from upmovies.catalog.release_grade import PRIMARY_REGION, THEATRICAL_RELEASE_TYPES
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.schemas import TMDBWatchProviderRegion
from upmovies.ingest.tmdb.upsert import mark_film_missing
from upmovies.news.catalog_events import NOW_AVAILABLE_EVENT_TYPE
from upmovies.news.models import Event
from upmovies.synthesize.deterministic import (
    AvailableOn,
    NowAvailable,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)

# The monetization types are also the TMDB response keys they are read from, so the tables'
# CHECK constraint and this loop are one list rather than two that can drift. The order is the
# order the where-to-watch box lists them in (D-29), and so the order rows are written in.
MONETIZATION_FIELDS: tuple[str, ...] = MONETIZATION_TYPES


@dataclass(frozen=True)
class PollTarget:
    """One film this pass will read providers for, and the ids both halves of the write need:
    `tmdb_id` addresses TMDB, `film_id` addresses our own rows."""

    film_id: UUID
    tmdb_id: int


@dataclass(frozen=True)
class Offer:
    """One (provider, monetization type) pair observed for a film in a region."""

    provider_id: int
    provider_name: str
    logo_path: str | None
    monetization_type: str


@dataclass
class ProvidersResult:
    """What one provider poll selected, read and wrote."""

    selected: int = 0
    polled: int = 0
    offers: int = 0
    """Current offers observed across every film — the size of the rebuilt snapshot."""
    first_seen: int = 0
    """Rows newly inserted into the ledger. In steady state this is near zero, which is why it
    is reported apart from `offers` rather than folded into it."""
    cards: int = 0
    """`now_available` events raised (D-28). Never more than one per film per poll and always
    at most `first_seen`, and the gap between the two is the churn this product declines to
    report: a film moving from one service to another inserts a row and cards nothing."""
    missing: int = 0
    """Films TMDB answered 404 for, and this pass tombstoned. Reported apart from `failures`
    for the reason the refresh phase reports it apart: a failure is a reason to worry about
    TMDB, a missing film is a reason to stop asking about it (NEU-1124)."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def poll_set_clause(*, today: date, min_age_days: int, max_age_days: int) -> ColumnElement[bool]:
    """WHERE predicate selecting the films this poll owes a read — D-27's two rules, ORed.

    The theatrical rule is an EXISTS over the film's US theatrical rows **grouped by release
    type**, so what it tests is each subject's *governing* date — the earliest date within it,
    the same reading `catalog.headline_release` takes (NEU-1206) — rather than a raw row. A
    film in scope on any one of its US theatrical subjects is in scope: a title that opened
    limited 210 days ago and wide 150 days ago is squarely in the window on the beat an
    audience would name, and taking the earliest subject across the whole film would drop it.

    **Rule 2 is the computed watchlist, asked of everybody at once** (D-1414.3):
    `follow_queries.covered_by_any_user_clause` — a film any user's follows cover, inside the
    alert window, and that they have not muted. One predicate shared with the alerts, so the
    poll cannot come to a different answer about what somebody is waiting on than the pass that
    tells them about it. It reaches further than the two `EXISTS` it replaces: a film followed
    only through its director is polled now, which is what makes a `now_available` beat
    possible for it at all.

    **The alert window's date ceiling is the only bound left** (EF-1, EF-2). A person follow
    used to be cut to the credits its `coverage` named, and `lead` being the default is what
    kept the indirect reach small; a binary follow reaches every credit, so every film of every
    followed person — at any billing position, any crew job — is in this set while it is inside
    the window. That is a real widening of the poll's request volume and is the thing to watch
    on the first pass after NEU-1432 deploys.

    The window's status term is not rule 1's absence of one *or* in-play's: it ends at
    `Canceled` (D-46), so a `Released` film an indirect follow reaches stays in the set until
    the date ceiling. That agrees with rule 1, which has never filtered on status — a film past
    its theatrical date is `Released`, and that is the state in which looking for offers pays.

    Tombstoned films are excluded. Their theatrical date keeps ageing inside the window, so
    without this a deleted id costs a request every day until it falls out the far end — and it
    is the poll, not the refresh phase, that would pay: a film past its theatrical release is
    `Released`, which `in_play_clause` has already dropped from the refresh set. The exclusion
    is not permanent — `upsert_film` clears the tombstone whenever TMDB answers for the id
    again (NEU-1124).
    """
    # `between` is inclusive at both ends: a film exactly `min_age_days` old is due today, and
    # one exactly `max_age_days` old gets its last poll rather than falling out a day early.
    oldest_due = today - timedelta(days=max_age_days)
    newest_due = today - timedelta(days=min_age_days)
    theatrical_due = (
        select(literal(1))
        .select_from(FilmReleaseDate)
        .where(
            FilmReleaseDate.film_id == Film.id,
            FilmReleaseDate.iso_3166_1 == PRIMARY_REGION,
            FilmReleaseDate.release_type.in_(tuple(sorted(THEATRICAL_RELEASE_TYPES))),
        )
        .group_by(FilmReleaseDate.release_type)
        .having(
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).between(
                oldest_due, newest_due
            )
        )
        .exists()
    )
    return and_(
        Film.tmdb_missing_at.is_(None),
        or_(
            theatrical_due,
            covered_by_any_user_clause(today=today, max_age_days=max_age_days),
        ),
    )


async def load_poll_set(
    session: AsyncSession,
    *,
    today: date,
    min_age_days: int,
    max_age_days: int,
) -> list[PollTarget]:
    """The films due a provider read, in a stable order.

    Ordered by `Film.id` — deterministic, but a UUID, so arbitrary rather than stalest-first
    the way the refresh set is. Nothing here records when a film was last polled, so there is
    no staleness to sort on, and inventing one (say, oldest theatrical date first) would bias
    the head of the queue towards the films least likely to still be moving. What the fixed
    order does buy is a poll that aborts mid-pass covering the same prefix on its next run
    instead of a reshuffled sample — and what it costs is that a *sustained* outage never
    reaches the tail of the set, which is the abort guard working as intended (the films it
    never reaches are still due tomorrow)."""
    stmt = (
        select(Film.id, Film.tmdb_id)
        .where(
            poll_set_clause(
                today=today,
                min_age_days=min_age_days,
                max_age_days=max_age_days,
            )
        )
        .order_by(Film.id)
    )
    rows = await session.execute(stmt)
    return [PollTarget(film_id=film_id, tmdb_id=tmdb_id) for film_id, tmdb_id in rows]


def offers_for_region(region: TMDBWatchProviderRegion | None) -> list[Offer]:
    """Flatten one region's payload into the offers this project stores.

    `None` — TMDB holding no entry for the region at all — flattens to no offers rather than
    raising, and is the ordinary answer for a film nobody carries in the US. The caller treats
    it exactly like an empty list, which is what makes a film leaving every provider empty its
    where-to-watch box instead of freezing it at the last poll that found something.

    **Deduplicated on `(provider_id, monetization_type)`**, keeping the first sighting. That
    pair is the natural key of both tables, so a provider TMDB happens to list twice inside one
    monetization list — regional duplicates do occur in the JustWatch data — would otherwise
    reach the snapshot rebuild as two rows with the same key, raise a unique violation, fail
    the film, and spend the abort budget on a payload that was never ambiguous. Deduplicating
    once here rather than at each of the three writes keeps one definition of "an offer".
    """
    if region is None:
        return []
    deduplicated: dict[tuple[int, str], Offer] = {}
    for field in MONETIZATION_FIELDS:
        for provider in getattr(region, field):
            offer = Offer(
                provider_id=provider.provider_id,
                provider_name=provider.provider_name,
                logo_path=provider.logo_path,
                monetization_type=field,
            )
            deduplicated.setdefault((offer.provider_id, offer.monetization_type), offer)
    return list(deduplicated.values())


async def _upsert_providers(session: AsyncSession, offers: list[Offer]) -> None:
    """Record every provider named by this film's offers. Caller commits.

    Upsert rather than insert-if-absent: TMDB renames services and moves their logos, and the
    box renders whatever is stored, so a name observed today is the name to hold. Deduplicated
    on the way in because one provider commonly appears under two monetization types, and a
    statement that names the same key twice raises `CardinalityViolation` rather than folding.
    """
    seen: dict[int, Offer] = {}
    for offer in offers:
        seen[offer.provider_id] = offer
    if not seen:
        return
    stmt = insert(WatchProvider).values(
        [
            {"id": o.provider_id, "name": o.provider_name, "logo_path": o.logo_path}
            for o in seen.values()
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[WatchProvider.id],
            set_={"name": stmt.excluded.name, "logo_path": stmt.excluded.logo_path},
        )
    )


async def _ledger_types(session: AsyncSession, *, film_id: UUID, region_code: str) -> set[str]:
    """The monetization types this film already has a ledger row under, in this region.

    Read *before* the insert, in the insert's own transaction, because it is the only way to
    tell a genuinely new type from a new provider under an old one once `ON CONFLICT DO
    NOTHING` has folded the two together.

    **What makes the read-then-write safe is the schedule, not a lock.** One Coolify slot runs
    this poll, so in practice it is the table's only writer; nothing here enforces that, and two
    overlapping runs would each read `known_types` empty for one film and card it twice. The
    structural backstop the release-date path leans on does not reach this case either: those
    two cards would carry each run's own `datetime.now(UTC)`, and `uq_event_catalog_change` keys
    on `occurred_at`, so it would let both through. Worth a lock if a second slot is ever added
    — not worth one for a poll that is scheduled once a day.
    """
    rows = await session.execute(
        select(AvailabilityFirstSeen.monetization_type)
        .where(
            AvailabilityFirstSeen.film_id == film_id,
            AvailabilityFirstSeen.region == region_code,
        )
        .distinct()
    )
    return set(rows.scalars())


async def _insert_first_seen(
    session: AsyncSession, *, film_id: UUID, region_code: str, offers: list[Offer], now: datetime
) -> list[Offer]:
    """Insert the ledger rows this poll has not seen before; return the offers that were new,
    in the order they were observed. Caller commits.

    `ON CONFLICT DO NOTHING` over the natural key is the whole insert-only rule (D-27): the
    second sighting of an offer writes nothing, so `first_seen_at` keeps saying *first*. The
    new set comes from `RETURNING`, which yields only rows the statement actually inserted — so
    it is exactly what `now_available` cards off (D-28), with no read-then-write race to lose a
    row to.
    """
    if not offers:
        return []
    stmt = insert(AvailabilityFirstSeen).values(
        [
            {
                "film_id": film_id,
                "region": region_code,
                "provider_id": o.provider_id,
                "monetization_type": o.monetization_type,
                "first_seen_at": now,
            }
            for o in offers
        ]
    )
    rows = await session.execute(
        stmt.on_conflict_do_nothing(
            index_elements=["film_id", "region", "provider_id", "monetization_type"]
        ).returning(AvailabilityFirstSeen.provider_id, AvailabilityFirstSeen.monetization_type)
    )
    inserted = set(rows.all())
    return [o for o in offers if (o.provider_id, o.monetization_type) in inserted]


async def _card_now_available(
    session: AsyncSession,
    *,
    film_id: UUID,
    region_code: str,
    new_offers: list[Offer],
    known_types: set[str],
    first_seen_at: datetime,
) -> bool:
    """Raise the one `now_available` event this film's newly first-seen offers are owed, if
    any (D-28). Returns whether an event was written. Caller owns the commit.

    One event, not one per type: `uq_event_catalog_change` permits a single catalog event per
    (film, type, timestamp), and every row this poll inserted for this film shares
    `first_seen_at` — so a title turning up to rent and to buy in one observation is one card
    carrying a `US:rent`-style token per type, exactly as a US limited and US wide date moving
    together are (`sweep.release_events`). The per-type grain D-28 cards on lives in
    `subject_key`, and the insert-only rule lives in `known_types`.

    The event and its summary are written together, so an event never reaches the feed without
    the summary row every read path inner-joins.
    """
    new_types = [
        kind
        for kind in MONETIZATION_FIELDS
        if kind not in known_types and any(o.monetization_type == kind for o in new_offers)
    ]
    if not new_types:
        return False
    event = Event(
        film_id=film_id,
        event_type=NOW_AVAILABLE_EVENT_TYPE,
        # TMDB is the system of record for who is carrying a film, the same standing the field
        # phase gives a status change — there is nothing here for a trade story to corroborate.
        confidence="confirmed",
        provenance="catalog",
        # When the film landed, not when the poll ran: a pass that catches up on a backlog after
        # an outage still dates each card to the observation that produced it.
        occurred_at=first_seen_at,
        region=region_code,
        subject_key=[f"{region_code}:{kind}" for kind in new_types],
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=NowAvailable(
            offers=tuple(
                AvailableOn(
                    monetization_type=kind,
                    providers=tuple(
                        o.provider_name for o in new_offers if o.monetization_type == kind
                    ),
                )
                for kind in new_types
            )
        ),
        source_updated_at=event.updated_at,
    )
    return True


async def _rebuild_current(
    session: AsyncSession,
    *,
    film_id: UUID,
    region_code: str,
    offers: list[Offer],
    link: str | None,
) -> None:
    """Replace this film's current availability in this region with what the poll just saw.
    Caller commits.

    Delete-and-rebuild rather than a diff: the snapshot has no history to preserve — that is
    the ledger's job — and a rebuild is the only write that cannot leave a stale row behind
    when a provider drops the film. Scoped to the one region so a later region's rows are not
    collateral of a US poll.
    """
    await session.execute(
        delete(FilmAvailabilityCurrent).where(
            FilmAvailabilityCurrent.film_id == film_id,
            FilmAvailabilityCurrent.region == region_code,
        )
    )
    if not offers:
        return
    await session.execute(
        insert(FilmAvailabilityCurrent).values(
            [
                {
                    "film_id": film_id,
                    "region": region_code,
                    "provider_id": o.provider_id,
                    "monetization_type": o.monetization_type,
                    "link": link,
                }
                for o in offers
            ]
        )
    )


async def run_provider_poll(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    min_age_days: int,
    max_age_days: int,
    now: datetime | None = None,
    region_code: str = PRIMARY_REGION,
    failure_threshold: int = 10,
    log_every: int = 250,
) -> ProvidersResult:
    """Read `/movie/{id}/watch/providers` for every film in the scoped set, one at a time."""
    result = ProvidersResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        targets = await load_poll_set(
            s,
            today=today,
            min_age_days=min_age_days,
            max_age_days=max_age_days,
        )
    result.selected = len(targets)
    log.info("providers: %d films due in %s", result.selected, region_code)

    for i, target in enumerate(targets, start=1):
        await heartbeat.tick()
        try:
            payload = await client.watch_providers(target.tmdb_id)
            region = payload.results.get(region_code)
            offers = offers_for_region(region)
            # Stamped per film rather than once for the whole pass: a poll runs for as long as
            # the set takes, and `first_seen_at` is the ledger's only payload — it is what D-28
            # reports as when the film landed, and what the card's `occurred_at` becomes, so it
            # has to say when this film was seen, not when the run began. `now` pins it for
            # tests.
            seen_at = now if now is not None else datetime.now(UTC)
            async with owned_session(session_factory) as s:
                await _upsert_providers(s, offers)
                known_types = await _ledger_types(
                    s, film_id=target.film_id, region_code=region_code
                )
                new_offers = await _insert_first_seen(
                    s,
                    film_id=target.film_id,
                    region_code=region_code,
                    offers=offers,
                    now=seen_at,
                )
                carded = await _card_now_available(
                    s,
                    film_id=target.film_id,
                    region_code=region_code,
                    new_offers=new_offers,
                    known_types=known_types,
                    first_seen_at=seen_at,
                )
                await _rebuild_current(
                    s,
                    film_id=target.film_id,
                    region_code=region_code,
                    offers=offers,
                    link=region.link if region is not None else None,
                )
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.polled += 1
            result.offers += len(offers)
            result.first_seen += len(new_offers)
            result.cards += 1 if carded else 0
            guard.succeeded()
            if i % log_every == 0:
                log.info("providers: %d/%d films", i, len(targets))
            continue
        except TMDBNotFound:
            # Terminal, not an outage — tombstoned rather than retried, and it touches `guard`
            # in neither direction, for the reasons `refresh_phase` gives at the same call.
            async with owned_session(session_factory) as s:
                await mark_film_missing(s, target.tmdb_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.missing += 1
            log.info("providers: film %d is gone from TMDB (404); tombstoned", target.tmdb_id)
            continue
        except httpx.HTTPError as e:
            log.warning("polling providers for film %d failed: %s", target.tmdb_id, e)
        except Exception:
            # One malformed payload must not cost the rest of the poll.
            log.exception("unexpected error polling providers for film %d", target.tmdb_id)
        result.failures += 1
        if await guard.failed():
            result.aborted = True
            result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
            log.error("providers: %s", result.abort_error)
            break

    log.info(
        "providers: %d polled, %d offers, %d first seen, %d carded, %d missing, %d failed",
        result.polled,
        result.offers,
        result.first_seen,
        result.cards,
        result.missing,
        result.failures,
    )
    return result


def providers_detail(result: ProvidersResult) -> str:
    """The run's `ingest_run.detail` line.

    `carded` is the number worth reading: it is the beat the milestone exists to deliver
    (D-28), and in steady state a healthy poll reports a handful against thousands of offers.
    It sits beside `first seen` rather than replacing it because the two answer different
    questions — a gap between them is churn the product deliberately swallowed, and a `first
    seen` that climbs while `carded` stays flat is exactly what a healthy catalogue looks like.
    `missing` sits apart from `failed` here for the same reason it does on the sweep's line —
    one is catalog hygiene, the other is an outage.
    """
    line = (
        f"providers: {result.polled}/{result.selected} polled, "
        f"{result.offers} offers, {result.first_seen} first seen, "
        f"{result.cards} carded, {result.missing} missing, {result.failures} failed"
    )
    if result.aborted:
        line += f"; providers aborted: {result.abort_error}"
    return line
