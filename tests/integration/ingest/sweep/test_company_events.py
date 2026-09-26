"""The sweep's company phase end to end (EF-5, NEU-1433): which `catalog.film_company_change`
rows become cards, what quarantine withholds, and what a detachment does to the attach card it
corrects.

The rule carrying the most weight is the one not implemented here at all — first observation is
a baseline (§5.3), which `ingest.tmdb.company_history` guarantees upstream by writing no history
row for a film's first company set. It is asserted anyway, because this is the phase where
getting it wrong would surface: a `company_attached` card for every studio on every film in the
catalog, in one pass.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import FilmCompanyChange, FilmProductionCompany, ProductionCompany
from upmovies.ingest.sweep import run_company_events
from upmovies.ingest.tmdb.company_history import COMPANY_ADDED, COMPANY_REMOVED
from upmovies.news.models import Event, EventStory, EventSummary, Story, StoryEntity
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7
QUARANTINE_HOURS = 72
# Old enough to have cleared a 72h hold, young enough to still be inside the 7-day window.
AGED = NOW - timedelta(hours=QUARANTINE_HOURS + 1)
OLDER = NOW - timedelta(hours=QUARANTINE_HOURS + 30)


async def _company(session, company_id: int, name: str) -> ProductionCompany:
    company = ProductionCompany(id=company_id, name=name)
    session.add(company)
    await session.flush()
    return company


async def _change(session, film, company, *, change=COMPANY_ADDED, changed_at=AGED):
    session.add(
        FilmCompanyChange(
            film_id=film.id, company_id=company.id, change=change, changed_at=changed_at
        )
    )
    await session.flush()


async def _attach_live(session, film, company):
    session.add(FilmProductionCompany(film_id=film.id, company_id=company.id))
    await session.flush()


async def _run(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
    }
    return await run_company_events(**{**kwargs, **overrides})


async def _events(session, film):
    return (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id).order_by(Event.created_at),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def _summary(session, event):
    return (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()


async def test_a_studio_attaching_cards_a_rumored_catalog_event(session, session_factory, run_id):
    film = await add_film(session, 1)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company)
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "company_attached"
    assert event.confidence == "rumored"
    assert event.provenance == "catalog"
    assert event.occurred_at == AGED
    assert event.subject_key == ["company:100"]


async def test_the_card_carries_a_deterministic_summary(session, session_factory, run_id):
    """Every read path inner-joins `event_summary`, so a card written without one is invisible
    on every surface."""
    film = await add_film(session, 2)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company)
    await _attach_live(session, film, company)
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    summary = await _summary(session, event)
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.summary == "Legendary Pictures joins the production."


async def test_a_film_with_no_history_rows_cards_nothing(session, session_factory, run_id):
    """First observation is a baseline: the upstream diff writes no rows, so there is nothing
    here to read however many companies the film holds."""
    film = await add_film(session, 3)
    company = await _company(session, 100, "Legendary Pictures")
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 0
    assert await _events(session, film) == []


async def test_one_pass_cards_a_burst_as_one_event(session, session_factory, run_id):
    """D-7: three studios coming off quarantine together are one card naming all three."""
    film = await add_film(session, 4)
    for company_id, name in ((100, "Legendary Pictures"), (101, "Atlas"), (102, "Warner Bros.")):
        company = await _company(session, company_id, name)
        await _change(session, film, company, changed_at=AGED if company_id == 100 else OLDER)
        await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert set(event.subject_key or []) == {"company:100", "company:101", "company:102"}
    # Dated by the latest change in the group, so a later pass collapsing a larger burst
    # cannot collide with this card under `uq_event_catalog_change`.
    assert event.occurred_at == AGED
    assert (await _summary(session, event)).summary == (
        "Atlas, Legendary Pictures and Warner Bros. join the production."
    )


async def test_an_attachment_inside_the_quarantine_window_is_held(session, session_factory, run_id):
    film = await add_film(session, 5)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=NOW - timedelta(hours=1))
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)
    assert await _events(session, film) == []


async def test_an_attachment_reverted_inside_the_window_never_cards(
    session, session_factory, run_id
):
    """The quarantine revert: the company is no longer on the film, so the edit was never
    true and must publish nothing at all rather than publish and be corrected."""
    film = await add_film(session, 6)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company)
    await session.commit()  # no live `film_production_company` row

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)
    assert await _events(session, film) == []


async def test_a_detachment_reverted_inside_the_window_never_cards(
    session, session_factory, run_id
):
    """The mirror, and the whole flap gate for the removal half: the studio came back, so the
    departure is not news that arrived late — it is news that never happened."""
    film = await add_film(session, 7)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, change=COMPANY_REMOVED)
    await _attach_live(session, film, company)  # back on the film
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)
    assert await _events(session, film) == []


async def test_a_reverted_attachment_cards_neither_direction(session, session_factory, run_id):
    """The whole revert, both rows. `_rebuild_joins` diffs on every ingest, so an edit made and
    undone writes an `added` *and* a `removed`, and live state cannot tell the second from a
    genuine departure — the company really is gone. Carding it would announce that a studio had
    left a film it was never reported as joining, which is the correction card the quarantine
    window exists to avoid."""
    film = await add_film(session, 12)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=OLDER)
    await _change(session, film, company, change=COMPANY_REMOVED, changed_at=AGED)
    await session.commit()  # no live row: the edit was undone

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 0
    assert await _events(session, film) == []


async def test_a_baseline_studio_leaving_still_cards(session, session_factory, run_id):
    """The other side of that rule, and why there is no prior-attach-card gate: a company on
    the film since its baseline has no `added` row anywhere, so its departure is a complete
    beat and must reach its followers."""
    film = await add_film(session, 13)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, change=COMPANY_REMOVED, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "company_removed"


async def test_a_departure_after_a_published_attachment_still_cards(
    session, session_factory, run_id
):
    """The round-trip rule must not silence a real flap: once the attachment has published,
    the reader knows the studio joined, so its later departure is owed a card whatever the
    window holds."""
    film = await add_film(session, 14)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=NOW - timedelta(days=6))
    await _attach_live(session, film, company)
    await session.commit()
    await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    await session.execute(
        delete(FilmProductionCompany).where(FilmProductionCompany.film_id == film.id)
    )
    await _change(session, film, company, change=COMPANY_REMOVED, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    assert [e.event_type for e in await _events(session, film)] == [
        "company_attached",
        "company_removed",
    ]


async def test_a_detachment_cards_and_supersedes_the_attach_card_it_corrects(
    session, session_factory, run_id
):
    """D-2. The attach card keeps its place on every surface; only its status moves, and its
    `superseded_by` names the removal that corrected it."""
    film = await add_film(session, 8)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=OLDER)
    await _attach_live(session, film, company)
    await session.commit()
    await _run(session_factory, run_id)

    await session.execute(
        delete(FilmProductionCompany).where(FilmProductionCompany.film_id == film.id)
    )
    await _change(session, film, company, change=COMPANY_REMOVED, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    attached, removed = await _events(session, film)
    assert (attached.event_type, attached.status) == ("company_attached", "superseded")
    assert attached.superseded_by == removed.id
    assert removed.event_type == "company_removed"
    assert removed.subject_key == ["company:100"]
    assert (await _summary(session, removed)).summary == (
        "Legendary Pictures is no longer attached."
    )


async def test_a_re_attachment_after_a_removal_cards_again(session, session_factory, run_id):
    """attach → detach → re-attach reads superseded, published, published: the suppression
    check keys on what the reader was last told, so a studio coming back is news again."""
    film = await add_film(session, 9)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=NOW - timedelta(days=5))
    await _attach_live(session, film, company)
    await session.commit()
    await _run(session_factory, run_id)

    await session.execute(
        delete(FilmProductionCompany).where(FilmProductionCompany.film_id == film.id)
    )
    await _change(session, film, company, change=COMPANY_REMOVED, changed_at=OLDER)
    await session.commit()
    await _run(session_factory, run_id)

    await _attach_live(session, film, company)
    await _change(session, film, company, changed_at=AGED)
    await session.commit()
    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    events = await _events(session, film)
    assert [e.event_type for e in events] == [
        "company_attached",
        "company_removed",
        "company_attached",
    ]
    assert [e.status for e in events] == ["superseded", "published", "published"]


async def test_the_same_window_read_twice_cards_once(session, session_factory, run_id):
    """The rolling window is re-read on every pass, so idempotence is the steady state rather
    than an edge case."""
    film = await add_film(session, 10)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company)
    await _attach_live(session, film, company)
    await session.commit()

    await _run(session_factory, run_id)
    result = await _run(session_factory, run_id)

    assert (result.events_created, result.skipped) == (0, 1)
    assert len(await _events(session, film)) == 1


async def test_a_change_older_than_the_lookback_is_not_read(session, session_factory, run_id):
    film = await add_film(session, 11)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company, changed_at=NOW - timedelta(days=LOOKBACK_DAYS + 1))
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0
    assert await _events(session, film) == []


async def test_a_company_reaching_too_many_films_in_one_day_is_held(
    session, session_factory, run_id
):
    """D-8's shape: one company attaching across a slate in a single observation day is the
    defacement the check is for, and every one of those attachments is withheld."""
    company = await _company(session, 100, "Legendary Pictures")
    films = []
    for tmdb_id in range(20, 23):
        film = await add_film(session, tmdb_id)
        films.append(film)
        await _change(session, film, company)
        await _attach_live(session, film, company)
    await session.commit()

    result = await _run(session_factory, run_id, max_films_per_day=3)

    assert (result.events_created, result.bursts_held) == (0, 3)
    for film in films:
        assert await _events(session, film) == []


async def test_a_burst_reverted_below_the_threshold_cards_its_survivors(
    session, session_factory, run_id
):
    """The stateless release: the check counts live attachments, so TMDB reverting most of a
    run lets the rest through on the next pass with no hold row to clear."""
    company = await _company(session, 100, "Legendary Pictures")
    films = []
    for tmdb_id in range(30, 33):
        film = await add_film(session, tmdb_id)
        films.append(film)
        await _change(session, film, company)
        await _attach_live(session, film, company)
    await session.commit()
    await _run(session_factory, run_id, max_films_per_day=3)

    # TMDB reverts two of the three; the survivor is what it still asserts.
    await session.execute(
        delete(FilmProductionCompany).where(
            FilmProductionCompany.film_id.in_([films[0].id, films[1].id])
        )
    )
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, max_films_per_day=3
    )

    assert result.events_created == 1
    assert len(await _events(session, films[2])) == 1


async def test_detachments_are_never_burst_held(session, session_factory, run_id):
    """A company leaving many films in one day is a recatalogue, not defacement — and
    withholding those would suppress the signal a studio follower is owed."""
    company = await _company(session, 100, "Legendary Pictures")
    films = []
    for tmdb_id in range(40, 43):
        film = await add_film(session, tmdb_id)
        films.append(film)
        await _change(session, film, company, change=COMPANY_REMOVED)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, max_films_per_day=2
    )

    assert (result.events_created, result.bursts_held) == (3, 0)


async def test_a_change_already_carded_by_a_story_is_read_past(session, session_factory, run_id):
    """The EF-12 seam: a stamped row is published already and must never card a second time."""
    film = await add_film(session, 50)
    company = await _company(session, 100, "Legendary Pictures")
    await _change(session, film, company)
    await _attach_live(session, film, company)
    story_card = Event(
        film_id=film.id,
        event_type="company_attached",
        confidence="rumored",
        provenance="story",
        occurred_at=OLDER,
        subject_key=["company:100"],
    )
    session.add(story_card)
    await session.flush()
    await session.execute(
        update(FilmCompanyChange)
        .where(FilmCompanyChange.film_id == film.id)
        .values(carded_by_event_id=story_card.id)
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created) == (0, 0)
    assert len(await _events(session, film)) == 1


async def _story_card_with_mention(
    session, film, *, company_id: int, event_type: str = "company_attached", occurred_at=OLDER
) -> Event:
    """What the cluster stage plus the resolve stage leave behind: a story card with no
    `subject_key` of its own — an organisation token cannot be written at clustering — and a
    resolved `news.story_entity` row on its story naming the studio."""
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="rumored",
        provenance="story",
        occurred_at=occurred_at,
    )
    session.add(event)
    await session.flush()
    story = Story(source="Deadline", url=f"https://deadline.test/{event.id}", title="Story")
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    session.add(
        StoryEntity(
            story_id=story.id,
            kind="company",
            entity_id=company_id,
            name_as_written="Legendary Pictures",
            path="accepted",
            features={"title_mentioned": None, "event_type": event_type},
            prompt_version="1",
        )
    )
    await session.flush()
    return event


async def test_a_studio_the_trades_broke_first_cards_once(session, session_factory, run_id):
    """EF-13 end to end for studios: story day 0, TMDB day 2. The phase stamps the change
    before it reads its own backlog, so the change never enters it and exactly one card exists
    — the one the trades ran."""
    film = await add_film(session, 60)
    company = await _company(session, 100, "Legendary Pictures")
    card = await _story_card_with_mention(session, film, company_id=company.id)
    await _change(session, film, company)
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, story_confirm_days=14
    )

    assert (result.story_published, result.changes_read, result.events_created) == (1, 0, 0)
    assert [e.id for e in await _events(session, film)] == [card.id]
    stamped = (
        await session.execute(
            select(FilmCompanyChange.carded_by_event_id).where(
                FilmCompanyChange.film_id == film.id
            ),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert stamped == card.id


async def test_a_story_about_another_studio_leaves_the_change_to_the_sweep(
    session, session_factory, run_id
):
    """Per studio, not per film: one story is never confirmation of every company change TMDB
    has pending on a film."""
    film = await add_film(session, 61)
    company = await _company(session, 100, "Legendary Pictures")
    other = await _company(session, 101, "Blumhouse")
    await _story_card_with_mention(session, film, company_id=other.id)
    await _change(session, film, company)
    await _attach_live(session, film, company)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, story_confirm_days=14
    )

    assert (result.story_published, result.events_created) == (0, 1)
    catalog_cards = [e for e in await _events(session, film) if e.provenance == "catalog"]
    assert len(catalog_cards) == 1
