"""The M7 decision pass (NEU-1379): who gets a digest line, and who gets a `suppressed` row
instead of mail. The digest is the only delivery (ADR-0021), so a `digest`/`email` row is the
only row the pass writes.

Every test here seeds a watermark by hand — a `succeeded` notify run at a fixed instant — and
dates its events either side of it, so the window is explicit rather than a function of when
the suite happened to run. `run_notify_pass` reads that watermark exactly as production does.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update

from upmovies.app.models import Follow, Notification
from upmovies.app.services.notify_service import NotifyResult, run_notify_pass
from upmovies.catalog.models import FilmCompanyChange, FilmProductionCompany, ProductionCompany
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.ingest.sweep import confirm_stamped_cards
from upmovies.news.models import EventStory, Story, StoryEntity, StoryPerson
from upmovies.news.subject_key import (
    COLLECTION_SUBJECT_PREFIX,
    COMPANY_SUBJECT_PREFIX,
    normalize_name,
)

WATERMARK = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
"""When the last successful notify run began. Events created after it are this pass's work."""
NEW = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
OLD = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
BETWEEN = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
"""Where the watermark lands after a first pass — between the two windows below."""
LATER = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
TODAY = date(2026, 9, 18)
"""Only a fixture date now. The pass itself takes no `today`, no excluded statuses and no age
bound since M3: the pass reads the timeline's own clause, and neither half of it has a
window to bound (EF-3, D-1437.7)."""
GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def seed_watermark(session):
    async def _seed(at: datetime = WATERMARK) -> None:
        session.add(IngestRun(kind="notify", status="succeeded", started_at=at, finished_at=at))
        await session.commit()

    return _seed


@pytest.fixture
def move_watermark(session):
    """Re-date every notify run to `at`, so a second pass's window is a fact of the fixture
    rather than of the wall clock the first run stamped itself with."""

    async def _move(at: datetime) -> None:
        await session.execute(
            update(IngestRun).where(IngestRun.kind == "notify").values(started_at=at)
        )
        await session.commit()

    return _move


@pytest.fixture
def run_pass(session_factory):
    """Run the pass the way `pipeline_run.run_notify_stage` does — its own run row, its own
    sessions — so the watermark this run leaves behind is the real one."""

    async def _run() -> NotifyResult:
        async with session_factory() as s:
            run_id = await create_run(s, kind="notify")
            await s.commit()
        result = await run_notify_pass(session_factory=session_factory, run_id=run_id)
        async with session_factory() as s:
            await finalize_run(s, run_id, status="failed" if result.aborted else "succeeded")
            await s.commit()
        return result

    return _run


@pytest.fixture
def subscriber(make_user):
    """The ordinary recipient: verified and holding a live grant."""

    async def _make(email: str = "sub@example.com", **kwargs):
        kwargs.setdefault("entitled_until", GRANTED)
        kwargs.setdefault("email_verified_at", VERIFIED)
        return await make_user(email=email, **kwargs)

    return _make


async def _rows(session) -> list[Notification]:
    return list(
        (
            await session.execute(
                select(Notification).order_by(Notification.kind, Notification.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _follow_title(session, *, user_id: UUID, film_id: UUID) -> None:
    """Follow a film by title — which, since M8, is what puts it on the watchlist *and* on the
    timeline. There is no way to have one without the other, and that is the model: a follow is
    the only thing a user keeps (D-42)."""
    session.add(
        Follow(user_id=user_id, entity_type="title", entity_id=str(film_id), source="manual")
    )
    await session.commit()


async def _follow_person(session, *, user_id: UUID, person_id: int) -> None:
    """Follow a person — which, since M3, puts that person's *attachment cards* on this user's
    timeline and digest, and nothing else about the films they are on (EF-3)."""
    session.add(
        Follow(
            user_id=user_id,
            entity_type="person",
            entity_id=str(person_id),
            source="manual",
        )
    )
    await session.commit()


async def _attach_card(add_event, film, *, person_name: str, event_type: str = "casting", **kw):
    """A catalog credit card as the sweep writes one: `rumored` until quarantine clears, with
    the person's normalized name in `subject_key` and nothing else identifying them."""
    kw.setdefault("provenance", "catalog")
    kw.setdefault("confidence", "rumored")
    return await add_event(
        film=film,
        event_type=event_type,
        subject_key=[normalize_name(person_name)],
        **kw,
    )


async def test_a_title_follow_queues_one_digest_line_and_nothing_else(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """EF-14's whole shape: the film is on this user's calendar *and* on their timeline because
    they follow it. A release date — once an alert beat too (D-32) — earns exactly one row, the
    digest line: the digest is the only delivery (ADR-0021), and this is the case that used to
    send two mails for one beat."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    event = await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.digests_queued, result.suppressed) == (1, 0)
    (digest,) = await _rows(session)
    assert (digest.user_id, digest.event_id) == (user.id, event.id)
    assert (digest.kind, digest.channel, digest.status) == ("digest", "email", "queued")
    assert digest.sent_at is None


async def test_a_followed_persons_attachment_card_lines_the_digest(
    session,
    session_factory,
    subscriber,
    make_user,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """What an entity follow earns from this pass, in full (EF-3): the attachment card, as a
    digest line.

    The card is `rumored`, which is what every catalog attachment is until its quarantine
    clears, and the digest carries it all the same (D-1437.7). The non-follower is the control:
    one card, one recipient."""
    await seed_watermark()
    user = await subscriber()
    stranger = await subscriber(email="stranger@example.com")
    film = await make_film(slug="dune", title="Dune")
    await make_person(id=488, name="A Director")
    await _follow_person(session, user_id=user.id, person_id=488)
    await _follow_title(
        session,
        user_id=stranger.id,
        film_id=(await make_film(slug="something-else", title="Something Else")).id,
    )
    card = await _attach_card(
        add_event, film, person_name="A Director", event_type="crew_attached", created_at=NEW
    )

    result = await run_pass()

    assert result.digests_queued == 1
    assert {(row.user_id, row.event_id, row.kind) for row in await _rows(session)} == {
        (user.id, card.id, "digest"),
    }


async def test_a_person_follow_no_longer_reaches_the_films_own_beats(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The cutover's consequence for this pass (EF-3). Under D-11 a credit put the whole film on
    the user's timeline, so a release-date change earned them a digest line; now
    a person follow delivers attachments and the film's own beats belong to its own followers.

    Every credit is checked in `test_follow_queries.py`; what is pinned here is that the branch
    reading the film is the *title* branch and a person follow does not reach it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", release_date=date(2099, 1, 1))
    await attach_credits(film, crew=[{"id": 488, "name": "A Writer", "job": "Screenplay"}])
    await _follow_person(session, user_id=user.id, person_id=488)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.digests_queued == 0
    assert await _rows(session) == []


@pytest.mark.parametrize(
    ("event_type", "confidence"),
    [
        pytest.param("casting", "rumored", id="a casting attachment"),
        pytest.param("crew_attached", "rumored", id="a crew attachment"),
        pytest.param("credit_removed", "rumored", id="a detachment"),
        pytest.param("canceled", "confirmed", id="a cancellation"),
    ],
)
async def test_an_entity_follower_gets_every_card_their_follow_delivers(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
    event_type,
    confidence,
):
    """The four person cards an entity follow delivers (the studio and franchise pair are
    `test_a_company_follower_gets_the_studio_joining` below), `rumored` or not: each is one
    digest line."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    # Credited as well as named, so the `canceled` case has an attachment to be found through.
    # `attach_credits` writes the person row, which is why this test does not also `make_person`.
    await attach_credits(film, crew=[{"id": 488, "name": "A Director", "job": "Director"}])
    await _follow_person(session, user_id=user.id, person_id=488)
    await _attach_card(
        add_event,
        film,
        person_name="A Director",
        event_type=event_type,
        confidence=confidence,
        created_at=NEW,
    )

    result = await run_pass()

    assert result.digests_queued == 1
    assert [row.kind for row in await _rows(session)] == ["digest"]


@pytest.mark.parametrize(
    "event_type",
    ["company_attached", "company_removed", "collection_attached", "collection_removed"],
)
async def test_a_company_follower_gets_the_studio_joining(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    event_type,
):
    """The organisation half of what an entity follow delivers (EF-3)."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    entity_type = "company" if event_type.startswith("company") else "franchise"
    prefix = COMPANY_SUBJECT_PREFIX if entity_type == "company" else COLLECTION_SUBJECT_PREFIX
    session.add(Follow(user_id=user.id, entity_type=entity_type, entity_id="33", source="manual"))
    await session.commit()
    await add_event(
        film=film,
        event_type=event_type,
        provenance="catalog",
        confidence="rumored",
        subject_key=[f"{prefix}33"],
        created_at=NEW,
    )

    result = await run_pass()

    assert result.digests_queued == 1
    assert [row.kind for row in await _rows(session)] == ["digest"]


async def test_a_title_follows_digest_carries_a_studio_joining(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """A title follow delivers every published beat on its film (EF-3), a studio joining
    included — the digest is the timeline, not a whitelist of it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film,
        event_type="company_attached",
        provenance="catalog",
        confidence="rumored",
        subject_key=[f"{COMPANY_SUBJECT_PREFIX}33"],
        created_at=NEW,
    )

    result = await run_pass()

    assert result.digests_queued == 1


async def test_unfollowing_the_film_earns_neither_kind(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """EF-14: the mute that used to silence a film without touching its follow is gone with the
    watchlist it corrected, so the only way a followed film stops earning deliveries is the
    follow itself going."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)
    await session.execute(sa_delete(Follow).where(Follow.user_id == user.id))
    await session.commit()

    result = await run_pass()

    assert result.digests_queued == 0
    assert await _rows(session) == []


async def test_a_rumored_card_is_queued_as_a_digest_line(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The floor that moved (D-1437.7). It used to sit in `deliverable_events`, so a `rumored`
    card was queued in no kind at all — which, once every catalog attachment arrived `rumored`,
    would have left an entity follower's digest empty of the only thing their follow delivers.

    EF-7: the digest carries everything the timeline carries, and since ADR-0021 nothing waits
    for confirmation — the digest marks the line Unconfirmed (DC-5) instead."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    event = await add_event(
        film=film, event_type="release_date", confidence="rumored", created_at=NEW
    )

    result = await run_pass()

    assert result.events_considered == 1, "the shared selector no longer cuts on confidence"
    assert result.digests_queued == 1
    (row,) = await _rows(session)
    assert (row.kind, row.event_id) == ("digest", event.id)


async def test_a_superseded_event_is_not_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """A superseded card is still *rendered* everywhere (ADR-0017), but the correction that
    replaced it is the one worth mailing about."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", status="superseded", created_at=NEW)

    assert (await run_pass()).digests_queued == 0
    assert await _rows(session) == []


async def test_events_published_before_the_watermark_are_left_alone(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The window is the publication axis (ADR-0016) — anything the previous run could have
    seen is its business, not this one's."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=OLD)
    await add_event(film=film, event_type="trailer", created_at=NEW)

    result = await run_pass()

    assert result.events_considered == 1
    assert [row.kind for row in await _rows(session)] == ["digest"]


async def test_re_deciding_the_same_window_writes_nothing_twice(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The idempotence that matters: a run that failed leaves its window undecided, so the next
    one re-reads it in full. `uq_notification_user_event_kind_channel` is what makes that free.

    The second pass here sees the *same* watermark, because the first run's own `succeeded` row
    is backdated to it — exactly the overlap a crash produces."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    first = await run_pass()
    await move_watermark(WATERMARK)
    second = await run_pass()

    assert first.digests_queued == 1
    assert second.digests_queued == 0
    assert second.events_considered == 1, "the window really was re-read"
    assert len(await _rows(session)) == 1


async def test_a_card_both_follows_reach_earns_one_row(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The user follows the film *and* its director, so the attachment card reaches them twice
    over — through `title_follow_film_ids` and through `entity_attachment_event_ids`.
    `follow_scope` OR-s the two, so the reach that admitted the card leaves no trace in what is
    written: one digest line."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, crew=[{"id": 488, "name": "A Director", "job": "Director"}])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _follow_person(session, user_id=user.id, person_id=488)
    card = await _attach_card(
        add_event, film, person_name="A Director", event_type="crew_attached", created_at=NEW
    )

    result = await run_pass()

    assert result.digests_queued == 1
    (row,) = await _rows(session)
    assert (row.kind, row.channel, row.event_id) == ("digest", "email", card.id)


async def test_the_first_run_ever_establishes_the_watermark_and_queues_nothing(
    session, session_factory, subscriber, make_film, add_event, run_pass
):
    """No `seed_watermark` here. "Everything since the beginning of time" is the whole ledger,
    so a cold start deliberately mails nobody rather than a year of news at once."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.cold_start
    assert await _rows(session) == []


async def test_an_unentitled_user_is_suppressed(
    session, session_factory, make_user, make_film, add_event, seed_watermark, run_pass
):
    """D-39's batch checkpoint. A row, not an absence: "we decided not to mail you" has to be
    distinguishable from "nobody considered you"."""
    await seed_watermark()
    user = await make_user(email="lapsed@example.com", email_verified_at=VERIFIED)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.digests_queued, result.suppressed) == (0, 1)
    (row,) = await _rows(session)
    assert (row.kind, row.status) == ("digest", "suppressed")


async def test_an_unverified_user_is_suppressed(
    session, session_factory, make_user, make_film, add_event, seed_watermark, run_pass
):
    """D-31's half of the same decision — an entitled subscriber who never confirmed their
    address. Verification gates outbound mail and nothing else, so they keep the app."""
    await seed_watermark()
    user = await make_user(email="unconfirmed@example.com", entitled_until=GRANTED)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.digests_queued, result.suppressed) == (0, 1)
    assert {row.status for row in await _rows(session)} == {"suppressed"}


async def test_a_grant_that_lapses_between_runs_suppresses_the_next_window(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The case a single-run test cannot show: entitlement is read per run, not per account, so
    the same user is queued on one pass and suppressed on the next. The first window's row is
    left exactly as it was — revoking suppresses, it never destroys (D-40)."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    first_event = await add_event(film=film, event_type="release_date", created_at=NEW)

    assert (await run_pass()).digests_queued == 1
    await move_watermark(BETWEEN)

    user.entitled_until = LAPSED
    await session.commit()
    second_event = await add_event(film=film, event_type="trailer", created_at=LATER)

    result = await run_pass()

    assert (result.digests_queued, result.suppressed) == (0, 1)
    rows = {row.event_id: row for row in await _rows(session)}
    assert rows[first_event.id].status == "queued"
    assert rows[second_event.id].status == "suppressed"


async def test_a_story_that_merely_names_a_followed_person_is_not_in_their_digest(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """The negative of what this test used to assert, and the heart of EF-13.

    Under D-11 any event whose story named a resolved followed person was theirs — so a date
    change reported in an article that mentions the director was a digest line about a film
    they may have nothing to do with. Now a mention reaches them only as the person's *first
    association* with the film, and a `release_date` card is not an attach card at all, so
    `first_association_clause` declines it on its first term."""
    await seed_watermark()
    user = await subscriber()
    person = await make_person(id=525, name="A Director")
    film = await make_film(slug="uncredited", title="Uncredited")
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person.id), source="manual")
    )
    await session.commit()
    await add_event(
        film=film,
        event_type="release_date",
        created_at=NEW,
        sources=({"url": "https://deadline.example/story"},),
    )
    story_id = (
        await session.execute(select(Story.id).where(Story.url == "https://deadline.example/story"))
    ).scalar_one()
    session.add(
        StoryPerson(
            story_id=story_id,
            person_id=person.id,
            name_as_written="A Director",
            path="accepted",
            features={"title_mentioned": None, "event_type": "casting"},
            prompt_version="1",
        )
    )
    await session.commit()

    result = await run_pass()

    assert result.digests_queued == 0
    assert await _rows(session) == []


async def test_a_first_association_is_in_the_digest(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """The other side: the same mention on a `casting` card, for somebody holding no credit on
    the film, is the first-association beat — and it is digest material, `rumored` and all."""
    await seed_watermark()
    user = await subscriber()
    person = await make_person(id=525, name="A Director")
    film = await make_film(slug="uncredited", title="Uncredited")
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person.id), source="manual")
    )
    await session.commit()
    event = await add_event(
        film=film,
        event_type="casting",
        confidence="rumored",
        created_at=NEW,
        sources=({"url": "https://deadline.example/scoop"},),
    )
    story_id = (
        await session.execute(select(Story.id).where(Story.url == "https://deadline.example/scoop"))
    ).scalar_one()
    session.add(
        StoryPerson(
            story_id=story_id,
            person_id=person.id,
            name_as_written="A Director",
            path="accepted",
            features={"title_mentioned": None, "event_type": "casting"},
            prompt_version="1",
        )
    )
    await session.commit()

    result = await run_pass()

    assert result.digests_queued == 1
    (row,) = await _rows(session)
    assert (row.event_id, row.kind) == (event.id, "digest")


async def test_the_pass_reaches_the_one_first_association_builder(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    monkeypatch,
):
    """The contract NEU-1446 inherits: EF-13's predicate is **one** builder, and this pass
    reaches it through `entity_attachment_event_ids` rather than spelling the rule again. M4
    extends the builder to `story_entity`; a second copy of the predicate grown here is what M4
    would quietly leave behind.

    Asserted by counting calls rather than by comparing SQL: what matters is that the digest
    branch goes through it, once, for the user being decided for."""
    from upmovies.app import follow_queries

    calls: list[dict] = []
    real = follow_queries.first_association_clause

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(follow_queries, "first_association_clause", spy)

    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.digests_queued == 1
    assert len(calls) == 1, "once, from the digest branch"
    assert calls[0]["user_id"] == user.id


async def test_a_user_with_no_graph_is_never_considered(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The pass is proportional to the follow graph, not to signups: an account that follows
    nothing is owed no decision, so it buys no statements."""
    await seed_watermark()
    await subscriber(email="lurker@example.com")
    film = await make_film(slug="dune", title="Dune")
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.users_considered == 0
    assert await _rows(session) == []


async def test_a_hidden_event_type_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """`other` is the uncategorized catch-all, hidden from every surface (`news.visibility`).
    A mail about a card the product will not show is worse than no mail."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="other", created_at=NEW)

    result = await run_pass()

    assert result.events_considered == 0
    assert await _rows(session) == []


async def test_an_event_with_no_summary_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The sender's precondition: a notification with no summary behind it is a mail with
    nothing to say."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="casting", summary=None, created_at=NEW)

    assert (await run_pass()).digests_queued == 0
    assert await _rows(session) == []


async def test_a_release_date_outside_the_films_visible_markets_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """An Indian date change on a US film is hidden from the feed and the film page by
    `region_visible()`, so queueing it would mail news the product then refuses to show."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", origin_country=["US"])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film, event_type="release_date", provenance="catalog", region="IN", created_at=NEW
    )

    result = await run_pass()

    assert result.digests_queued == 0
    assert await _rows(session) == []


async def test_a_release_date_in_the_films_origin_country_is_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The other side of the same rule — the film's own market is visible, so it is queued."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="rrr", title="RRR", origin_country=["IN"])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film, event_type="release_date", provenance="catalog", region="IN", created_at=NEW
    )

    assert (await run_pass()).digests_queued == 1


async def test_a_film_with_no_slug_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """No slug, no page to link the mail at."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    film.slug = None
    await session.commit()
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="trailer", created_at=NEW)

    assert (await run_pass()).digests_queued == 0
    assert await _rows(session) == []


async def test_a_studio_scoop_lines_the_digest_once_and_its_confirmation_adds_nothing(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The confirmation flip is a state change on the card, not a second delivery (ADR-0021).

    The story card lines the digest the night it publishes, `rumored`. The catalog then
    confirms it — the stamped change, `confirm_stamped_cards` at the end of the sweep — which
    moves the card's `confidence` and `updated_at` but not its `created_at`, so the next pass's
    window does not reach it and the reader gets no second line."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    session.add(ProductionCompany(id=3172, name="Blumhouse"))
    session.add(Follow(user_id=user.id, entity_type="company", entity_id="3172", source="manual"))
    await session.commit()

    # The card as the cluster stage published it: story provenance, `rumored`, and no
    # organisation token — resolution runs later, which is why the follow reaches it through
    # `first_association_clause` rather than through the token branch.
    card = await add_event(
        film=film,
        event_type="company_attached",
        provenance="story",
        confidence="rumored",
        created_at=NEW,
        sources=({"url": "https://deadline.test/scoop"},),
    )
    story_id = await session.scalar(
        select(EventStory.story_id).where(EventStory.event_id == card.id)
    )
    session.add(
        StoryEntity(
            story_id=story_id,
            kind="company",
            entity_id=3172,
            name_as_written="Blumhouse",
            path="accepted",
            features={"title_mentioned": None, "event_type": "company_attached"},
            prompt_version="1",
        )
    )
    await session.commit()

    rumored = await run_pass()

    assert rumored.digests_queued == 1

    # TMDB observes the attachment; the sweep's backward pass stamps it with the card that
    # published it, and the confirmation phase flips the card once the window has passed.
    session.add(FilmProductionCompany(film_id=film.id, company_id=3172))
    session.add(
        FilmCompanyChange(
            film_id=film.id,
            company_id=3172,
            change="added",
            changed_at=LATER - timedelta(days=4),
            carded_by_event_id=card.id,
        )
    )
    await session.commit()
    await move_watermark(BETWEEN)
    assert await confirm_stamped_cards(session, now=LATER, quarantine=timedelta(hours=72)) == (
        1,
        1,
        0,
    )
    await session.commit()

    confirmed = await run_pass()

    assert confirmed.digests_queued == 0
    assert [row.kind for row in await _rows(session)] == ["digest"]
